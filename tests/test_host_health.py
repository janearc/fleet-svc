"""Host-health classifier tests.

The regression cases pin the real failures this machine has actually shown, so
the "fan is a smell" instinct becomes a tested guard. The throughline: separate
OS/library *pathology* (the thing to fix) from legitimate *load* (a 24B model
resident), and never confuse one for the other.
"""

from fleet.host_health import (
    HostSnapshot, Proc, VmSnapshot, Severity, Kind, classify_host_health,
)


def _codes(health):
    return {f.code for f in health.findings}


def _find(health, code):
    return next(f for f in health.findings if f.code == code)


# --- regression: the failures we have actually hit -----------------------------

def test_logind_runaway_poll_is_critical_pathology():
    # the systemd-logind poll bug: a near-idle daemon pegged at ~100% doing
    # nothing useful. this is the canonical OS/lib pathology.
    snap = HostSnapshot(
        mem_total_gb=48, mem_free_pct=40, swap_total_mb=5120, swap_used_mb=500,
        processes=[
            Proc(pid=512, name="systemd-logind", cpu_pct=99.8),
            Proc(pid=1, name="k3s", cpu_pct=3.9),
        ],
    )
    health = classify_host_health(snap)
    assert "runaway_idle_daemon" in _codes(health)
    f = _find(health, "runaway_idle_daemon")
    assert f.severity == Severity.CRITICAL and f.kind == Kind.PATHOLOGY
    assert health.pathology is True and health.overall == Severity.CRITICAL


def test_vm_hot_but_interior_calm_is_thrash_not_compute():
    # the VM drew 109% on the host while inside it the busiest process (k3s) was
    # 3.9% -- host-side thrash/accounting, NOT real cluster compute.
    snap = HostSnapshot(
        mem_total_gb=48, mem_free_pct=35, swap_total_mb=5120, swap_used_mb=4100,
        vm=VmSnapshot(host_cpu_pct=109.6, interior_top_cpu_pct=3.9, interior_top_name="k3s"),
    )
    health = classify_host_health(snap)
    f = _find(health, "vm_host_interior_mismatch")
    assert f.severity == Severity.WARN and f.kind == Kind.PATHOLOGY
    assert health.pathology is True


def test_resident_mistral_under_swap_is_load_not_pathology():
    # the live state during mistral curation: llama-server resident, swap ~80%
    # used, but 35% memory free and the machine responsive. "sad, but still good
    # with mistral" -- pressure, NOT a fault. the guard must NOT cry wolf here.
    snap = HostSnapshot(
        mem_total_gb=48, mem_free_pct=35, swap_total_mb=5120, swap_used_mb=4100,
        processes=[
            Proc(pid=9444, name="/opt/homebrew/.../llama-server", cpu_pct=180, rss_gb=2.0),
            Proc(pid=5066, name="ollama", cpu_pct=2.0),
        ],
    )
    health = classify_host_health(snap)
    assert health.model_resident is True
    f = _find(health, "swap_saturated")
    assert f.kind == Kind.LOAD            # attributed to the model, not a leak
    assert f.severity == Severity.WARN    # not critical: memory still has headroom
    assert health.pathology is False      # the key assertion: no false alarm


def test_same_swap_without_a_model_is_suspect_pathology():
    # identical swap saturation but with NO model server resident reads very
    # differently: nothing legitimate should be eating that memory -> suspect.
    snap = HostSnapshot(
        mem_total_gb=48, mem_free_pct=35, swap_total_mb=5120, swap_used_mb=4100,
        processes=[Proc(pid=1, name="k3s", cpu_pct=4.0)],
    )
    health = classify_host_health(snap)
    f = _find(health, "swap_saturated")
    assert f.kind == Kind.PATHOLOGY
    assert health.pathology is True


# --- the ordinary cases --------------------------------------------------------

def test_healthy_idle_host_is_clean():
    snap = HostSnapshot(
        mem_total_gb=48, mem_free_pct=62, swap_total_mb=5120, swap_used_mb=200,
        processes=[Proc(pid=1, name="k3s", cpu_pct=3.0)],
        vm=VmSnapshot(host_cpu_pct=12, interior_top_cpu_pct=3.0, interior_top_name="k3s"),
    )
    health = classify_host_health(snap)
    assert health.overall == Severity.OK
    assert health.pathology is False
    assert health.findings == []


def test_memory_critical_is_flagged_even_under_load():
    # at the wall, model resident or not, we want a CRITICAL -- swap can't save us.
    snap = HostSnapshot(
        mem_total_gb=48, mem_free_pct=4, swap_total_mb=5120, swap_used_mb=4900,
        processes=[Proc(pid=9444, name="llama-server", cpu_pct=150)],
    )
    health = classify_host_health(snap)
    assert _find(health, "mem_critical").severity == Severity.CRITICAL
    assert _find(health, "swap_saturated").severity == Severity.CRITICAL
    assert health.overall == Severity.CRITICAL


def test_busy_model_server_alone_is_not_a_pathology():
    # a model server at 300% CPU with healthy memory is pure load -- info only.
    snap = HostSnapshot(
        mem_total_gb=48, mem_free_pct=55, swap_total_mb=5120, swap_used_mb=300,
        processes=[Proc(pid=9444, name="llama-server", cpu_pct=300)],
    )
    health = classify_host_health(snap)
    assert health.model_resident is True
    assert health.pathology is False
    assert health.overall == Severity.INFO  # just the model_resident note


def test_health_serializes_to_json():
    # agent-first: the verdict has to be machine-readable.
    snap = HostSnapshot(
        mem_total_gb=48, mem_free_pct=35, swap_total_mb=5120, swap_used_mb=4100,
        processes=[Proc(pid=9444, name="llama-server", cpu_pct=180)],
    )
    payload = classify_host_health(snap).model_dump(mode="json")
    assert payload["pathology"] is False
    assert payload["model_resident"] is True
    assert isinstance(payload["findings"], list)
