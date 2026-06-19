from __future__ import annotations

import logging
import os

from pydantic import BaseModel, Field

from fleet.models import WorkstationConfig

log = logging.getLogger(__name__)

# compose filenames fleet recognises when deciding whether a repo participates
# in the docker mesh. kept in sync with cli._COMPOSE_FILENAMES.
_COMPOSE_FILENAMES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

# the shared docker network every fleet compose file attaches to. exactly one
# repo (traefik) DEFINES it (driver: bridge); all others mark it external: true
# and depend on it already existing. that asymmetry is what makes start order
# load-bearing: the network owner must be up before any dependent, and is the
# last thing torn down.
DEV_FLEET_NETWORK = "dev-fleet"

# the compose label a service sets to declare its lifecycle tier. backbone
# services (the message bus) carry `fleet.tier: backbone`. this is the PRIMARY
# signal for backbone membership: a repo announces its own role in its compose
# file, the same way the network owner announces itself structurally (by
# DEFINING dev-fleet). the control plane discovers membership; it does not carry
# a hardcoded list of which repos are backbone.
FLEET_TIER_LABEL = "fleet.tier"
FLEET_TIER_BACKBONE_VALUE = "backbone"

# tier ordinals. forward (bootstrap/up) order is ascending; down order is the
# exact reverse. the ordering lives HERE, in one place, and both directions
# consume it -- nothing hardcodes a service name into an ordering decision.
#
#   0 NETWORK_OWNER  traefik: creates dev-fleet + is the route registry.
#                    first up, last down.
#   1 BACKBONE       the message backbone (kafka/zookeeper/schema-registry).
#                    everything that produces/consumes events needs it.
#   2 CONTROL_PLANE  delightd: the control-plane daemon other tooling calls.
#   3 WORKLOAD       non-essential application services (obs-svc, paling).
#                    brought up last, torn down first.
TIER_NETWORK_OWNER = 0
TIER_BACKBONE = 1
TIER_CONTROL_PLANE = 2
TIER_WORKLOAD = 3

_TIER_LABELS = {
    TIER_NETWORK_OWNER: "network-owner",
    TIER_BACKBONE: "backbone",
    TIER_CONTROL_PLANE: "control-plane",
    TIER_WORKLOAD: "workload",
}

# repos fleet recognises by role. membership is DISCOVERED first -- the network
# owner structurally (it DEFINES dev-fleet) and the backbone by a self-declared
# `fleet.tier: backbone` compose label. these name lists are a DEFENSIVE
# FALLBACK only: they keep ordering correct on a roster repo that has not yet
# adopted the label (e.g. during rollout). a fallback hit is logged so the gap
# is visible. control-plane has no structural/label signal yet, so it still
# resolves by name.
_NETWORK_OWNER_REPOS = ("traefik",)
_BACKBONE_REPOS = ("kafka-logging", "kafka-svc")
_CONTROL_PLANE_REPOS = ("delightd",)


class LifecycleUnit(BaseModel):
    # one compose-bearing repo in the fleet, tagged with the tier that fixes its
    # position in the start/stop ordering. `tier` is the only ordering input;
    # `essential` mirrors WorkstationConfig and drives whether `down` treats the
    # unit as backbone-or-above vs. a disposable workload.
    model_config = {"extra": "forbid"}

    name: str
    path: str
    tier: int
    essential: bool = False

    @property
    def tier_label(self) -> str:
        return _TIER_LABELS.get(self.tier, "unknown")

    @property
    def is_network_owner(self) -> bool:
        return self.tier == TIER_NETWORK_OWNER


class LifecyclePlan(BaseModel):
    # the ordered set of units fleet will act on, plus the network owner pulled
    # out for the bootstrap network-precedence check. `up_order` is tier-ascending
    # (network owner first); `down_order` is the exact reverse (network owner last).
    model_config = {"extra": "forbid"}

    units: list[LifecycleUnit] = Field(default_factory=list)

    @property
    def network_owner(self) -> LifecycleUnit | None:
        owners = [u for u in self.units if u.is_network_owner]
        return owners[0] if owners else None

    @property
    def up_order(self) -> list[LifecycleUnit]:
        # stable sort by tier ascending; within a tier, declaration order is
        # preserved (roster order), which is deterministic.
        return sorted(self.units, key=lambda u: u.tier)

    @property
    def down_order(self) -> list[LifecycleUnit]:
        return list(reversed(self.up_order))


def _compose_path(expanded_repo_path: str) -> str | None:
    for name in _COMPOSE_FILENAMES:
        candidate = os.path.join(expanded_repo_path, name)
        if os.path.exists(candidate):
            return candidate
    return None


def _load_compose(compose_path: str) -> dict | None:
    # parse a compose file once; both the network-owner and backbone-tier
    # discovery read from the result. a parse failure is logged and treated as
    # "no structural signal" (the caller degrades to role-by-name).
    import yaml

    try:
        with open(compose_path) as handle:
            return yaml.safe_load(handle) or {}
    except Exception as exc:
        log.warning("could not parse compose file %s: %s", compose_path, exc)
        return None


def _defines_dev_fleet_network(doc: dict) -> bool:
    # the network owner is the repo whose compose file DEFINES dev-fleet rather
    # than consuming it as external. we detect that structurally: a top-level
    # networks: entry for dev-fleet that is not marked `external: true`. parsing
    # with yaml keeps this honest if the repo set ever changes.
    networks = doc.get("networks") or {}
    for key, spec in networks.items():
        spec = spec or {}
        declared_name = spec.get("name", key)
        if declared_name != DEV_FLEET_NETWORK:
            continue
        # external: true means "consume an existing network" -> not the owner.
        if spec.get("external"):
            return False
        # a non-external definition of dev-fleet -> this repo creates it.
        return True
    return False


def _service_labels(service: dict) -> dict[str, str]:
    # compose accepts labels in two shapes: a mapping ({fleet.tier: backbone})
    # or a list of KEY=VALUE strings (- "fleet.tier=backbone"). normalise both
    # to a dict so callers can look a label up uniformly.
    labels = (service or {}).get("labels")
    if isinstance(labels, dict):
        return {str(k): str(v) for k, v in labels.items()}
    if isinstance(labels, list):
        out: dict[str, str] = {}
        for item in labels:
            text = str(item)
            key, sep, value = text.partition("=")
            out[key.strip()] = value.strip() if sep else ""
        return out
    return {}


def _declares_backbone_tier(doc: dict) -> bool:
    # the backbone tier is self-declared: any service in this compose file
    # carrying `fleet.tier: backbone` marks the repo as backbone. this is the
    # PRIMARY signal -- a repo announces its own role rather than fleet matching
    # it by name.
    services = doc.get("services") or {}
    for spec in services.values():
        if _service_labels(spec).get(FLEET_TIER_LABEL) == FLEET_TIER_BACKBONE_VALUE:
            return True
    return False


def _classify_repo(name: str, expanded_path: str, compose_path: str) -> int:
    # decide a repo's tier. discovery beats name matching:
    #   - network owner: structural -- does this compose file CREATE the shared
    #     network? the registry is identified by what it does, not its name.
    #   - backbone: self-declared via the `fleet.tier: backbone` compose label.
    # the name lists are a defensive fallback for repos that have not adopted the
    # label/structure yet; a fallback hit is logged so the gap stays visible.
    # everything unrecognised is a workload.
    doc = _load_compose(compose_path)
    if doc is None:
        # unparseable compose: no structural/label signal. fall back to name.
        return _classify_repo_by_name(name)

    if _defines_dev_fleet_network(doc):
        return TIER_NETWORK_OWNER
    if _declares_backbone_tier(doc):
        return TIER_BACKBONE

    fallback = _classify_repo_by_name(name)
    if fallback == TIER_BACKBONE:
        # the repo classifies as backbone only because its name is on the
        # fallback list -- it has not declared `fleet.tier: backbone` yet.
        log.warning(
            "repo %s classified as backbone by name fallback; it does not "
            "declare the %s: %s compose label",
            name,
            FLEET_TIER_LABEL,
            FLEET_TIER_BACKBONE_VALUE,
        )
    return fallback


def _classify_repo_by_name(name: str) -> int:
    # defensive role-by-name fallback. used when the compose file carries no
    # structural/label signal (or does not parse). overridden by discovery in
    # _classify_repo whenever a signal is present.
    if name in _NETWORK_OWNER_REPOS:
        return TIER_NETWORK_OWNER
    if name in _BACKBONE_REPOS:
        return TIER_BACKBONE
    if name in _CONTROL_PLANE_REPOS:
        return TIER_CONTROL_PLANE
    return TIER_WORKLOAD


def build_plan(config: WorkstationConfig) -> LifecyclePlan:
    # turn the declarative roster into an ordered lifecycle plan. only repos that
    # actually ship a compose file participate (fleet itself is essential but has
    # no compose target -- it is the orchestrator running this command). the
    # roster is the source of truth for membership; the compose files supply the
    # ordering signal. nothing here touches the k3d cluster or colima: those are
    # not roster repos and have no compose file under ~/work/<repo>.
    units: list[LifecycleUnit] = []
    for repo in config.repositories:
        expanded = os.path.expanduser(repo.path)
        compose_path = _compose_path(expanded)
        if compose_path is None:
            continue
        tier = _classify_repo(repo.name, expanded, compose_path)
        units.append(
            LifecycleUnit(
                name=repo.name,
                path=repo.path,
                tier=tier,
                essential=repo.essential,
            )
        )
    return LifecyclePlan(units=units)


def load_plan(config_path: str) -> LifecyclePlan:
    import yaml

    with open(config_path) as handle:
        config = WorkstationConfig(**yaml.safe_load(handle))
    return build_plan(config)
