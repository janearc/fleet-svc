"""Host/VM resource-health classification.

This is the detection core behind `fleet top`: given a snapshot of host and VM
resource state, decide whether the machine is in trouble AND -- critically --
whether that trouble is an OS/library *pathology* (a daemon spinning on nothing,
a hypervisor thrashing) or just legitimate *load* (a 24B model resident, real
compute working the GPU).

That distinction is the whole point. A guard that screams every time we load a
big model is noise; a guard that stays silent through a runaway logind poll is
blind. The fan whirr that kept surprising us is the physical symptom -- this
turns it into a tested signal. The classifier is a pure function over a
snapshot, so the pathologies we have actually hit are pinned as regression cases
in tests/test_host_health.py.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel


class Severity(str, Enum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class Kind(str, Enum):
    # expected resource use: a model server resident, real compute. not a fault.
    LOAD = "load"
    # OS/library misbehavior: a near-idle daemon spinning, hypervisor thrash,
    # memory leaking with nothing to show for it. the compute path is innocent.
    PATHOLOGY = "pathology"


# daemons that have no business burning a core; over the runaway threshold they
# are spinning, not working (the systemd-logind poll bug is the canonical case).
_IDLE_DAEMONS = frozenset({
    "systemd-logind", "logind", "mdnsresponder", "diagnosticd",
    "coreduetd", "powerd", "lima-guestagent",
})
_RUNAWAY_CPU_PCT = 80.0       # a single-purpose daemon over this is spinning
_VM_HOT_PCT = 100.0           # VM consuming more than one host core
_VM_INTERIOR_CALM_PCT = 20.0  # ...while nothing inside the VM is actually busy
_SWAP_SATURATED = 0.80        # swap used / total
_MEM_FREE_CRITICAL = 7.0      # percent free
_MEM_FREE_WARN = 15.0

# process names whose large footprint is expected model-serving load, not a leak.
DEFAULT_MODEL_SERVERS = ("ollama", "llama-server", "mlx")


class Proc(BaseModel):
    model_config = {"extra": "forbid"}
    pid: int
    name: str
    cpu_pct: float        # host-relative; may exceed 100 across cores
    rss_gb: float = 0.0


class VmSnapshot(BaseModel):
    model_config = {"extra": "forbid"}
    host_cpu_pct: float          # CPU the VM process draws as seen from the host
    interior_top_cpu_pct: float  # busiest single process INSIDE the VM
    interior_top_name: str = ""


class HostSnapshot(BaseModel):
    model_config = {"extra": "forbid"}
    mem_total_gb: float
    mem_free_pct: float
    swap_total_mb: float
    swap_used_mb: float
    processes: List[Proc] = []
    vm: Optional[VmSnapshot] = None
    # substrings identifying legitimate model servers; their footprint is load.
    model_servers: List[str] = list(DEFAULT_MODEL_SERVERS)


class Finding(BaseModel):
    model_config = {"extra": "forbid"}
    code: str
    severity: Severity
    kind: Kind
    detail: str


class HostHealth(BaseModel):
    model_config = {"extra": "forbid"}
    overall: Severity            # max severity across findings
    pathology: bool              # any OS/lib failure present -- the alarm to act on
    model_resident: bool         # a model server is loaded (footprint is expected)
    findings: List[Finding] = []


_SEV_ORDER = {Severity.OK: 0, Severity.INFO: 1, Severity.WARN: 2, Severity.CRITICAL: 3}


def _basename(name: str) -> str:
    return name.rsplit("/", 1)[-1].lower()


def classify_host_health(snap: HostSnapshot) -> HostHealth:
    """Pure classification of a host/VM snapshot into health findings."""
    findings: List[Finding] = []
    servers = [s.lower() for s in snap.model_servers]
    model_resident = any(
        any(s in p.name.lower() for s in servers) for p in snap.processes
    )
    if model_resident:
        findings.append(Finding(
            code="model_resident", severity=Severity.INFO, kind=Kind.LOAD,
            detail="a model server is resident; heavy GPU/memory use here is "
                   "expected load, not a fault",
        ))

    # 1. a near-idle daemon pegged on the CPU -- pure OS/lib pathology (logind).
    for p in snap.processes:
        if _basename(p.name) in _IDLE_DAEMONS and p.cpu_pct >= _RUNAWAY_CPU_PCT:
            findings.append(Finding(
                code="runaway_idle_daemon", severity=Severity.CRITICAL,
                kind=Kind.PATHOLOGY,
                detail=f"{p.name} (pid {p.pid}) at {p.cpu_pct:.0f}% CPU -- a "
                       "near-idle daemon spinning on nothing; restart it",
            ))

    # 2. VM hot on the host while its interior is calm -> host-side thrash, not
    #    in-VM compute. distinguishes a hypervisor problem from real cluster work.
    vm = snap.vm
    if vm and vm.host_cpu_pct >= _VM_HOT_PCT and vm.interior_top_cpu_pct < _VM_INTERIOR_CALM_PCT:
        findings.append(Finding(
            code="vm_host_interior_mismatch", severity=Severity.WARN,
            kind=Kind.PATHOLOGY,
            detail=f"VM drawing {vm.host_cpu_pct:.0f}% on the host while its "
                   f"busiest interior process ({vm.interior_top_name or 'n/a'}) "
                   f"is {vm.interior_top_cpu_pct:.0f}% -- host-side thrash/"
                   "accounting, not work happening inside the cluster",
        ))

    # 3. swap saturation. under a resident model it is load-driven; with no model
    #    server present the same saturation is suspect (a leak somewhere).
    if snap.swap_total_mb > 0:
        ratio = snap.swap_used_mb / snap.swap_total_mb
        if ratio >= _SWAP_SATURATED:
            sev = Severity.CRITICAL if snap.mem_free_pct < _MEM_FREE_CRITICAL else Severity.WARN
            kind = Kind.LOAD if model_resident else Kind.PATHOLOGY
            why = ("driven by resident model load -- expected, watch it"
                   if model_resident else
                   "no model server resident -- suspect a leak, not legitimate load")
            findings.append(Finding(
                code="swap_saturated", severity=sev, kind=kind,
                detail=f"swap {ratio * 100:.0f}% used "
                       f"({snap.swap_used_mb:.0f}/{snap.swap_total_mb:.0f}MB); {why}",
            ))

    # 4. memory free pressure. critical even under load means we are near the wall.
    if snap.mem_free_pct < _MEM_FREE_CRITICAL:
        findings.append(Finding(
            code="mem_critical", severity=Severity.CRITICAL,
            kind=Kind.LOAD if model_resident else Kind.PATHOLOGY,
            detail=f"only {snap.mem_free_pct:.0f}% memory free -- at the wall",
        ))
    elif snap.mem_free_pct < _MEM_FREE_WARN:
        findings.append(Finding(
            code="mem_warn", severity=Severity.WARN,
            kind=Kind.LOAD if model_resident else Kind.PATHOLOGY,
            detail=f"{snap.mem_free_pct:.0f}% memory free -- under pressure",
        ))

    overall = max((f.severity for f in findings), key=lambda s: _SEV_ORDER[s], default=Severity.OK)
    pathology = any(
        f.kind == Kind.PATHOLOGY and f.severity in (Severity.WARN, Severity.CRITICAL)
        for f in findings
    )
    return HostHealth(
        overall=overall, pathology=pathology,
        model_resident=model_resident, findings=findings,
    )
