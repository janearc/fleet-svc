import pytest

from fleet.lifecycle import (
    DEV_FLEET_NETWORK,
    TIER_BACKBONE,
    TIER_CONTROL_PLANE,
    TIER_NETWORK_OWNER,
    TIER_WORKLOAD,
    LifecyclePlan,
    LifecycleUnit,
    build_plan,
    load_plan,
)
from fleet.models import WorkstationConfig

# a compose file that DEFINES dev-fleet (the network owner) vs one that consumes
# it as external. the classifier must tell these apart structurally.
_OWNER_COMPOSE = (
    "services:\n  traefik:\n    image: traefik:v2.11\n"
    "networks:\n"
    f"  {DEV_FLEET_NETWORK}:\n    name: {DEV_FLEET_NETWORK}\n    driver: bridge\n"
)
_DEPENDENT_COMPOSE = (
    "services:\n  svc:\n    image: busybox\n"
    f"networks:\n  {DEV_FLEET_NETWORK}:\n    external: true\n"
)
# a backbone repo that SELF-DECLARES its tier via the fleet.tier label (mapping
# form) rather than relying on its name. the service is deliberately named
# something the fallback list would not match.
_BACKBONE_LABELLED_COMPOSE = (
    "services:\n"
    "  bus:\n"
    "    image: redpanda\n"
    "    labels:\n"
    "      fleet.tier: backbone\n"
    f"networks:\n  {DEV_FLEET_NETWORK}:\n    external: true\n"
)
# same declaration, list form (- KEY=VALUE), which compose also accepts.
_BACKBONE_LABELLED_LIST_COMPOSE = (
    "services:\n"
    "  bus:\n"
    "    image: redpanda\n"
    "    labels:\n"
    '      - "fleet.tier=backbone"\n'
    f"networks:\n  {DEV_FLEET_NETWORK}:\n    external: true\n"
)


def _repo(tmp_path, name, compose_body):
    d = tmp_path / name
    d.mkdir()
    (d / "docker-compose.yml").write_text(compose_body)
    return {"name": name, "origin": "git", "path": str(d)}


def _config(tmp_path):
    # a faithful miniature of the real roster: traefik (owner), kafka-logging
    # (backbone), delightd (control plane), two workloads, and fleet (no compose
    # -> excluded). essential flags mirror WorkstationConfig.
    traefik = _repo(tmp_path, "traefik", _OWNER_COMPOSE)
    kafka = _repo(tmp_path, "kafka-logging", _DEPENDENT_COMPOSE)
    delightd = _repo(tmp_path, "delightd", _DEPENDENT_COMPOSE)
    obs = _repo(tmp_path, "obs-svc", _DEPENDENT_COMPOSE)
    paling = _repo(tmp_path, "paling", _DEPENDENT_COMPOSE)
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir()

    return WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[
            {**traefik, "essential": True},
            {**kafka, "essential": True},
            {**delightd, "essential": True},
            {"name": "fleet", "origin": "git", "path": str(fleet_dir), "essential": True},
            {**obs, "essential": False},
            {**paling, "essential": False},
        ],
    )


def test_build_plan_excludes_repos_without_compose(tmp_path):
    plan = build_plan(_config(tmp_path))
    names = {u.name for u in plan.units}
    # fleet is essential but ships no compose file -> it is the orchestrator and
    # must never be in its own teardown/bootstrap set.
    assert "fleet" not in names
    assert names == {"traefik", "kafka-logging", "delightd", "obs-svc", "paling"}


def test_network_owner_detected_structurally(tmp_path):
    plan = build_plan(_config(tmp_path))
    owner = plan.network_owner
    assert owner is not None
    assert owner.name == "traefik"
    assert owner.tier == TIER_NETWORK_OWNER
    assert owner.is_network_owner


def test_tiers_assigned_by_role(tmp_path):
    by_name = {u.name: u for u in build_plan(_config(tmp_path)).units}
    assert by_name["traefik"].tier == TIER_NETWORK_OWNER
    assert by_name["kafka-logging"].tier == TIER_BACKBONE
    assert by_name["delightd"].tier == TIER_CONTROL_PLANE
    assert by_name["obs-svc"].tier == TIER_WORKLOAD
    assert by_name["paling"].tier == TIER_WORKLOAD


def test_up_order_network_owner_first(tmp_path):
    order = [u.name for u in build_plan(_config(tmp_path)).up_order]
    assert order[0] == "traefik"
    # backbone before control plane before workloads
    assert order.index("kafka-logging") < order.index("delightd")
    assert order.index("delightd") < order.index("obs-svc")


def test_down_order_is_reverse_of_up(tmp_path):
    plan = build_plan(_config(tmp_path))
    up = [u.name for u in plan.up_order]
    down = [u.name for u in plan.down_order]
    assert down == list(reversed(up))
    # network owner is torn down LAST
    assert down[-1] == "traefik"
    # a workload is torn down FIRST
    assert plan.down_order[0].tier == TIER_WORKLOAD


def test_down_includes_non_essential_compose_workload(tmp_path):
    # regression: a non-essential roster repo that ships a compose file (here
    # obs-svc, which consumes dev-fleet exactly like the real obs-svc-agg
    # project) MUST land in the teardown plan as a workload. the live failure was
    # that obs-svc was absent from the roster entirely, so build_plan never saw
    # it and `down` orphaned its running container. with it in the roster, the
    # workload tier captures it and it is torn down before the control plane and
    # before the network owner.
    plan = build_plan(_config(tmp_path))
    obs = next(u for u in plan.units if u.name == "obs-svc")
    assert obs.tier == TIER_WORKLOAD
    assert obs.essential is False
    down = [u.name for u in plan.down_order]
    # the workload is stopped before the control plane and the network owner
    assert down.index("obs-svc") < down.index("delightd")
    assert down.index("obs-svc") < down.index("traefik")


def test_down_stops_all_compose_workloads(tmp_path):
    # the contract for `down`: every roster repo that ships a compose file and is
    # not the network owner / backbone / control plane is a workload and is in
    # the teardown set. neither non-essential workload (obs-svc, paling) may fall
    # through into "no tier".
    plan = build_plan(_config(tmp_path))
    workloads = {u.name for u in plan.units if u.tier == TIER_WORKLOAD}
    assert workloads == {"obs-svc", "paling"}
    # both appear in the actual teardown order (not silently dropped)
    down = {u.name for u in plan.down_order}
    assert {"obs-svc", "paling"} <= down


def test_classifier_name_fallback_when_no_structural_owner(tmp_path):
    # if NO compose file defines the network (degenerate roster), the named
    # network owner still classifies as tier-0 by role.
    traefik = _repo(tmp_path, "traefik", _DEPENDENT_COMPOSE)
    cfg = WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[{**traefik, "essential": True}],
    )
    plan = build_plan(cfg)
    assert plan.network_owner is not None
    assert plan.network_owner.name == "traefik"


def test_load_plan_reads_yaml(tmp_path):
    cfg = _config(tmp_path)
    cfg_path = tmp_path / "WorkstationConfig.yaml"
    cfg_path.write_text(cfg.model_dump_json())
    # model_dump_json is valid yaml (json is a yaml subset); load_plan parses it.
    plan = load_plan(str(cfg_path))
    assert plan.network_owner.name == "traefik"


def test_kafka_svc_alias_classifies_as_backbone(tmp_path):
    # the backbone repo is named kafka-logging on this host but kafka-svc on its
    # remote; both must classify as backbone.
    kafka = _repo(tmp_path, "kafka-svc", _DEPENDENT_COMPOSE)
    cfg = WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[{**kafka, "essential": True}],
    )
    unit = build_plan(cfg).units[0]
    assert unit.tier == TIER_BACKBONE


def test_empty_plan_has_no_network_owner():
    plan = LifecyclePlan(units=[])
    assert plan.network_owner is None
    assert plan.up_order == []
    assert plan.down_order == []


def test_unparseable_compose_is_not_network_owner(tmp_path):
    # a compose file that does not parse must not be mistaken for the network
    # owner; classification degrades to role-by-name (here: workload).
    bad = _repo(tmp_path, "broken", "this: is: not: valid: yaml: {[}")
    cfg = WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[{**bad, "essential": False}],
    )
    plan = build_plan(cfg)
    assert plan.network_owner is None
    assert plan.units[0].tier == TIER_WORKLOAD
    assert plan.units[0].tier_label == "workload"


def test_compose_with_unrelated_network_is_workload(tmp_path):
    # a compose file that declares some OTHER network (not dev-fleet) is not the
    # owner; the loop skips the non-matching entry and falls through to workload.
    other = _repo(
        tmp_path, "svc",
        "services:\n  svc:\n    image: busybox\nnetworks:\n  some-other-net:\n    driver: bridge\n",
    )
    cfg = WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[{**other, "essential": False}],
    )
    plan = build_plan(cfg)
    assert plan.units[0].tier == TIER_WORKLOAD


def test_backbone_discovered_from_compose_label(tmp_path):
    # PRIMARY path: a repo NOT on the name fallback list is classified as
    # backbone purely because its compose declares fleet.tier: backbone.
    bus = _repo(tmp_path, "redpanda-svc", _BACKBONE_LABELLED_COMPOSE)
    cfg = WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[{**bus, "essential": True}],
    )
    unit = build_plan(cfg).units[0]
    assert unit.tier == TIER_BACKBONE
    assert unit.tier_label == "backbone"


def test_backbone_label_list_form(tmp_path):
    # compose accepts labels as a list of KEY=VALUE strings as well as a mapping;
    # both must be recognised.
    bus = _repo(tmp_path, "redpanda-svc", _BACKBONE_LABELLED_LIST_COMPOSE)
    cfg = WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[{**bus, "essential": True}],
    )
    assert build_plan(cfg).units[0].tier == TIER_BACKBONE


def test_backbone_name_fallback_warns_when_label_absent(tmp_path, caplog):
    # FALLBACK path: a repo on the name list but WITHOUT the label still
    # classifies as backbone (so ordering survives rollout), and the fallback is
    # logged so the missing label is visible.
    kafka = _repo(tmp_path, "kafka-logging", _DEPENDENT_COMPOSE)
    cfg = WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[{**kafka, "essential": True}],
    )
    with caplog.at_level("WARNING"):
        unit = build_plan(cfg).units[0]
    assert unit.tier == TIER_BACKBONE
    assert any(
        "backbone by name fallback" in rec.message for rec in caplog.records
    )


def test_backbone_label_does_not_warn(tmp_path, caplog):
    # the label path must NOT emit the fallback warning -- the whole point is
    # that discovery is silent and only the gap is noisy.
    bus = _repo(tmp_path, "redpanda-svc", _BACKBONE_LABELLED_COMPOSE)
    cfg = WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[{**bus, "essential": True}],
    )
    with caplog.at_level("WARNING"):
        build_plan(cfg)
    assert not any(
        "name fallback" in rec.message for rec in caplog.records
    )


def test_delightd_name_classifies_as_control_plane(tmp_path):
    # delightd consuming the external network classifies as control plane by role
    # (structural owner detection returns false for external networks).
    d = _repo(tmp_path, "delightd", _DEPENDENT_COMPOSE)
    cfg = WorkstationConfig(
        version="1.0",
        host={"os": "darwin", "arch": "arm64", "daemons": ["docker"]},
        repositories=[{**d, "essential": True}],
    )
    assert build_plan(cfg).units[0].tier == TIER_CONTROL_PLANE
