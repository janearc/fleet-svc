"""Workstation git state for fleet's safety surfaces.

delightd is the source of truth: ``GET /git`` returns live branch/dirty/unpushed
state for every managed project. When the daemon is unreachable we fall back to
computing the same shape locally with ``git`` over the roster paths.

The fallback is deliberate and must never be dropped: ``fleet sync`` gates
destructive host-migration on this answer, and a teardown is exactly the kind of
moment the daemon might be down. A safety gate that can only answer when a daemon
is up is a gate that fails open. Reading git directly is always available and
always current.
"""

from __future__ import annotations

import os
import subprocess

import httpx

# delightd's control port. The daemon knows its own managed projects, so the
# happy path needs no roster argument; the roster is only used by the local
# fallback to know which paths to inspect.
_DEFAULT_DELIGHTD_URL = "http://127.0.0.1:8088"
_DEFAULT_TIMEOUT = 5.0

# The wire shape, snake_case, shared by the delightd endpoint and the local
# fallback so callers never branch on the source.
_FIELDS = ("name", "branch", "dirty", "unpushed", "has_upstream", "remote_url", "error")


def fetch_git_state(
    repos: list[tuple[str, str]],
    base_url: str = _DEFAULT_DELIGHTD_URL,
    timeout: float = _DEFAULT_TIMEOUT,
) -> tuple[list[dict], str]:
    """Return ``(repos, source)`` where source is ``"delightd"`` or ``"local"``.

    Tries delightd first; on any failure computes the same shape locally over the
    provided ``(name, path)`` roster.
    """
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base_url.rstrip('/')}/git")
            resp.raise_for_status()
            # delightd returns git as an element of a project: {name, git:{...}}.
            # Flatten it into fleet's internal flat shape so consumers (and the
            # local fallback below) speak one vocabulary.
            projects = [
                {"name": p.get("name", ""), **(p.get("git") or {})}
                for p in resp.json().get("projects", [])
            ]
            return projects, "delightd"
    except Exception:
        return [_local_project_state(name, path) for name, path in repos], "local"


def _local_project_state(name: str, path: str) -> dict:
    """Compute one repo's git state with the git CLI, mirroring delightd's
    pkg/gitstate semantics (including ``@{u}`` upstream resolution, so a remote
    named ``github`` rather than ``origin`` still resolves)."""
    repo_path = os.path.expanduser(path)

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
        )

    state = {"name": name, "branch": "", "dirty": False, "unpushed": 0,
             "has_upstream": False, "remote_url": "", "error": ""}

    head = git("rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode != 0:
        state["error"] = head.stderr.strip() or "not a git repository"
        return state
    state["branch"] = head.stdout.strip()
    state["dirty"] = bool(git("status", "--porcelain").stdout.strip())

    upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode == 0:
        state["has_upstream"] = True
        state["unpushed"] = _count(git("rev-list", "--count", "@{u}..HEAD"))
    else:
        # Never pushed: every local commit is unpushed work (conservative).
        state["has_upstream"] = False
        state["unpushed"] = _count(git("rev-list", "--count", "HEAD"))

    origin = git("remote", "get-url", "origin")
    if origin.returncode == 0:
        state["remote_url"] = origin.stdout.strip()
    else:
        remotes = git("remote").stdout.split()
        if remotes:
            state["remote_url"] = git("remote", "get-url", remotes[0]).stdout.strip()

    return state


def _count(proc: subprocess.CompletedProcess) -> int:
    try:
        return int(proc.stdout.strip() or 0)
    except ValueError:
        return 0


def roster_name_paths() -> list[tuple[str, str]]:
    """Return the declared roster as ``(name, path)`` pairs, sourced from
    WorkstationConfig -- never by globbing ~/work. Used by the local git-state
    fallback to know which paths to inspect. An unreadable config yields an empty
    roster: delightd stays the primary source, so reach degrades gracefully
    rather than guessing at a repo set."""
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
