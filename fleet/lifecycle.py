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

# repos fleet recognises by role. membership is DISCOVERED, not hardcoded into
# the ordering: we read the roster and the compose files to decide which repo
# owns the network and which carries the message backbone. these names are the
# only place a role is associated with a repo name, and they are overridable by
# what the compose files actually declare (see _classify_repo).
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


def _defines_dev_fleet_network(compose_path: str) -> bool:
    # the network owner is the repo whose compose file DEFINES dev-fleet rather
    # than consuming it as external. we detect that structurally: a top-level
    # networks: entry for dev-fleet that is not marked `external: true`. parsing
    # with yaml keeps this honest if the repo set ever changes.
    import yaml

    try:
        with open(compose_path) as handle:
            doc = yaml.safe_load(handle) or {}
    except Exception as exc:
        log.warning("could not parse compose file %s: %s", compose_path, exc)
        return False

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


def _classify_repo(name: str, expanded_path: str, compose_path: str) -> int:
    # decide a repo's tier. structural signal (does this compose file CREATE the
    # shared network?) wins over name matching for the network-owner role, so the
    # registry is identified by what it does, not what it is called. backbone and
    # control-plane fall back to the known role names; everything else is workload.
    if _defines_dev_fleet_network(compose_path):
        return TIER_NETWORK_OWNER
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
