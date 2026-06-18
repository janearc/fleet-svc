from __future__ import annotations

import os

import httpx

# delightd is the single source of truth for git state. fleet does not compute
# git itself: if delightd cannot answer, callers fail closed rather than guess.
# the corollary is that delightd must come up in any condition we can envision --
# that resilience lives in delightd, not in a fleet-side fallback.

_DEFAULT_DELIGHTD_URL = "http://127.0.0.1:8088"
_DEFAULT_TIMEOUT = 5.0


class DelightdUnavailable(Exception):
    # raised when delightd cannot be reached or refuses the request; callers must
    # fail closed and never assume "clean"
    pass


def fetch_git_state(
    base_url: str = _DEFAULT_DELIGHTD_URL,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict]:
    # one flat dict per project; raises DelightdUnavailable on any failure
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{base_url.rstrip('/')}/git")
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        raise DelightdUnavailable(str(exc)) from exc

    # delightd nests git under each project; flatten to fleet's flat shape
    projects = []
    for p in payload.get("projects", []):
        git = p.get("git") or {}
        projects.append({"name": p.get("name", ""), **git})
    return projects


def roster_name_paths() -> list[tuple[str, str]]:
    # the declared roster as (name, path) pairs, from WorkstationConfig -- never
    # by globbing ~/work. used by `git push` to resolve a project name to a path.
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
