# `fleet deploy <project>` -- roll a single fleet project, udeploy-style.
#
# The contract Max set: fleet does not blindly restart things. It first asks
# delightd -- the source of truth -- "do you know this project, and does it look
# healthy?" delightd only READS (git/config state); it never mutates. If delightd
# cannot answer, we fail closed (same posture as `fleet sync` / `fleet down`):
# refusing to roll a service we cannot certify beats rolling blind.
#
# Once the gate passes, the roll itself is a non-event by design -- every fleet
# service is stateless, so killing one because it is Tuesday must Just Work. How
# it rolls is declared per project in WorkstationConfig (`deploy:`):
#   kube    -> rollout-restart a Deployment (the normal containerised path)
#   launchd -> (re)load a launchd-managed command (the bare-metal "eel": a daemon
#              that needs Metal/MLX and so cannot be a pod, e.g. `paling serve`).
#
# JSON by default for the agent-first contract; the CLI renders a human line.

from __future__ import annotations

import os
import subprocess

from pydantic import BaseModel

from fleet.git_state import DelightdUnavailable, fetch_git_state
from fleet.models import WorkstationRepo


class DeployError(Exception):
    # a refusal the operator must act on (unknown project, no descriptor, an
    # unhealthy gate, a failed roll). carries an exit-worthy message.
    pass


class DeployResult(BaseModel):
    project: str
    kind: str
    rolled: bool
    dry_run: bool = False
    detail: str
    # delightd's read-only verdict, surfaced for the record.
    delightd_known: bool
    warnings: list[str] = []


def _find_repo(name: str, config_path: str | None) -> WorkstationRepo:
    import yaml

    from fleet.models import WorkstationConfig

    path = config_path or _locate_config()
    with open(path) as handle:
        config = WorkstationConfig(**yaml.safe_load(handle))
    for repo in config.repositories:
        if repo.name == name:
            return repo
    known = ", ".join(r.name for r in config.repositories)
    raise DeployError(f"unknown project '{name}'. roster: {known}")


def _locate_config() -> str:
    for candidate in ("WorkstationConfig.yaml", os.path.expanduser("~/work/fleet/WorkstationConfig.yaml")):
        if os.path.exists(candidate):
            return candidate
    return "WorkstationConfig.yaml"


def _gate_via_delightd(name: str) -> tuple[bool, list[str]]:
    # ask the SOT: known + healthy? returns (known, warnings); raises DeployError
    # for a hard refusal. delightd unreachable -> fail closed.
    try:
        projects = fetch_git_state()
    except DelightdUnavailable as exc:
        raise DeployError(
            f"delightd unreachable ({exc}); refusing to deploy without the source of truth"
        ) from exc

    record = next((p for p in projects if p.get("name") == name), None)
    if record is None:
        raise DeployError(
            f"delightd does not recognise '{name}' -- register it before deploying"
        )

    # missing_path / unreadable repo == cannot certify the project is deployable.
    if record.get("missing_path"):
        raise DeployError(f"delightd reports '{name}' path is missing on disk; not deployable")
    if record.get("error") and not record.get("missing_path"):
        raise DeployError(f"delightd could not read '{name}' ({record['error']}); failing closed")

    # dirty / unpushed is not fatal -- you may be rolling WIP on purpose -- but
    # the operator should know the deployed code is not the committed code.
    warnings: list[str] = []
    if record.get("dirty"):
        warnings.append(f"{name} has uncommitted changes (deploying a dirty tree)")
    if record.get("unpushed", 0):
        warnings.append(f"{name} has {record['unpushed']} unpushed commit(s)")
    return True, warnings


def _roll_kube(repo: WorkstationRepo, dry_run: bool) -> str:
    dep = repo.deploy.deployment or repo.name
    ns = repo.deploy.namespace
    cmd = ["kubectl", "rollout", "restart", f"deployment/{dep}", "-n", ns]
    if dry_run:
        return f"would run: {' '.join(cmd)}"
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeployError(f"kubectl rollout restart failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip() or f"rolled deployment/{dep} in {ns}"


def _roll_launchd(repo: WorkstationRepo, dry_run: bool) -> str:
    if not repo.deploy.command:
        raise DeployError(f"'{repo.name}' deploy.kind=launchd but no deploy.command set")
    cwd = os.path.expanduser(repo.path)
    cmd = repo.deploy.command
    if dry_run:
        return f"would run (cwd={repo.path}): {' '.join(cmd)}"
    if not os.path.isdir(cwd):
        raise DeployError(f"project path not found: {cwd}")
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeployError(f"launchd roll failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip() or "launchd agent (re)loaded"


def deploy(name: str, config_path: str | None = None, dry_run: bool = False) -> DeployResult:
    repo = _find_repo(name, config_path)
    if repo.deploy is None:
        raise DeployError(
            f"'{name}' has no deploy descriptor in WorkstationConfig (add a `deploy:` block)"
        )

    known, warnings = _gate_via_delightd(name)

    if repo.deploy.kind == "kube":
        detail = _roll_kube(repo, dry_run)
    elif repo.deploy.kind == "launchd":
        detail = _roll_launchd(repo, dry_run)
    else:  # pragma: no cover - pydantic Literal already constrains this
        raise DeployError(f"unknown deploy.kind '{repo.deploy.kind}'")

    return DeployResult(
        project=name,
        kind=repo.deploy.kind,
        rolled=not dry_run,
        dry_run=dry_run,
        detail=detail,
        delightd_known=known,
        warnings=warnings,
    )
