from __future__ import annotations

import os
import subprocess

import httpx

# delightd is the source of truth for git state. this module is fleet's client
# for GET /git, plus a fail-safe that reads git directly when the daemon is down
# -- `fleet sync` must never pass a teardown gate it could not verify.

_DEFAULT_DELIGHTD_URL = "http://127.0.0.1:8088"
_DEFAULT_TIMEOUT = 5.0


def fetch_git_state(
    repos: list[tuple[str, str]],
    base_url: str = _DEFAULT_DELIGHTD_URL,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[list[dict], str]:
    # returns (projects, source); source is "delightd" or "local"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base_url.rstrip('/')}/git")
            resp.raise_for_status()
            payload = resp.json()
    except Exception:
        return [_local_project_state(name, path) for name, path in repos], "local"

    # delightd nests git under each project; flatten to fleet's flat shape
    projects = []
    for p in payload.get("projects", []):
        git = p.get("git") or {}
        projects.append({"name": p.get("name", ""), **git})
    return projects, "delightd"


def _local_project_state(name: str, path: str) -> dict:
    # ask the git cli directly. git is always present on the workstation, so this
    # answers even with delightd down. @{u} resolves the branch's upstream
    # regardless of remote name (some of the fleet's repos use "github").
    repo = os.path.expanduser(path)

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)

    state = {"name": name, "branch": "", "dirty": False, "unpushed": 0,
             "has_upstream": False, "remote_url": "", "error": ""}

    head = git("rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode != 0:
        state["error"] = head.stderr.strip() or "not a git checkout"
        return state

    state["branch"] = head.stdout.strip()
    state["dirty"] = bool(git("status", "--porcelain").stdout.strip())

    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    state["has_upstream"] = upstream.returncode == 0
    if state["has_upstream"]:
        state["unpushed"] = _count(git("rev-list", "--count", "@{u}..HEAD"))
    else:
        # never pushed: treat every local commit as unpushed work
        state["unpushed"] = _count(git("rev-list", "--count", "HEAD"))

    state["remote_url"] = _remote_url(git)
    return state


def _remote_url(git) -> str:
    # prefer origin, else the first remote
    origin = git("remote", "get-url", "origin")
    if origin.returncode == 0:
        return origin.stdout.strip()
    remotes = git("remote").stdout.split()
    if remotes:
        return git("remote", "get-url", remotes[0]).stdout.strip()
    return ""


def _count(proc: subprocess.CompletedProcess) -> int:
    try:
        return int(proc.stdout.strip() or 0)
    except ValueError:
        return 0


def roster_name_paths() -> list[tuple[str, str]]:
    # the declared roster as (name, path) pairs, from WorkstationConfig -- never
    # by globbing ~/work. an unreadable config yields an empty roster; delightd
    # stays primary, so reach degrades rather than guessing at a repo set.
    import yaml

    from fleet.models import WorkstationConfig

    for candidate in ("WorkstationConfig.yaml",
                      os.path.expanduser("~/work/fleet/WorkstationConfig.yaml")):
        if os.path.exists(candidate):
            try:
                with open(candidate) as handle:
                    config = WorkstationConfig(**yaml.safe_load(handle))
                return [(r.name, r.path) for r in config.repositories]
            except Exception:
                return []
    return []
