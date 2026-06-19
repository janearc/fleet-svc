from __future__ import annotations

import os
import subprocess

# model-svc is a sibling project subordinate to fleet, being scaffolded by another
# agent. fleet does not reimplement it -- it dispatches to model-svc's bash
# wrapper, treating that wrapper as the contract (handoff/model-svc.md).
#
# CONTRACT (note the argument swap): the operator-facing form is
#   fleet model-svc <deployment> <command> [args]
# but the wrapper's own form is `model-svc <command> <deployment> [args]`, so the
# dispatcher swaps the first two tokens. fleet exposes the deployment-first form
# because the deployment is the noun fleet/delightd address; the wrapper is
# command-first. one swap, documented here, keeps both ergonomic.

# the wrapper ships at ~/work/model-svc/bin/model-svc; the symlink into ~/var/bin
# (delightd-managed) is a pending publish decision, so probe both. $FLEET_MODEL_SVC_WRAPPER
# overrides everything for a borrowed host / CI / an early --apply build.
_WRAPPER_CANDIDATES = (
    os.path.expanduser("~/var/bin/model-svc"),
    os.path.expanduser("~/work/model-svc/bin/model-svc"),
)
_WRAPPER_ENV = "FLEET_MODEL_SVC_WRAPPER"


class ModelSvcNotInstalled(Exception):
    # raised when no model-svc wrapper can be found. fleet cannot stand in for it;
    # the operator must install model-svc (or point $FLEET_MODEL_SVC_WRAPPER at it).
    pass


def _is_runnable(path: str) -> bool:
    # a usable wrapper exists and is executable; a dangling symlink or a non-exec
    # file is not a contract fleet can dispatch to.
    return os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_wrapper() -> str | None:
    # precedence: $FLEET_MODEL_SVC_WRAPPER > ~/var/bin/model-svc > the in-repo
    # ~/work/model-svc/bin/model-svc. returns the first runnable path, or None if
    # model-svc is not installed anywhere fleet knows to look.
    override = os.environ.get(_WRAPPER_ENV)
    if override:
        return override if _is_runnable(override) else None
    for candidate in _WRAPPER_CANDIDATES:
        if _is_runnable(candidate):
            return candidate
    return None


def wrapper_available() -> bool:
    return resolve_wrapper() is not None


def dispatch(deployment: str, command: str, args: list[str] | None = None) -> int:
    # forward to the model-svc wrapper. note the swap: fleet takes deployment-first
    # (fleet model-svc <deployment> <command>), the wrapper is command-first
    # (model-svc <command> <deployment>). returns the wrapper's exit code; raises
    # ModelSvcNotInstalled if no wrapper is found so the caller prints one clear
    # message instead of leaking a shell FileNotFoundError.
    wrapper = resolve_wrapper()
    if wrapper is None:
        searched = os.environ.get(_WRAPPER_ENV) or ", ".join(_WRAPPER_CANDIDATES)
        raise ModelSvcNotInstalled(
            f"model-svc not installed: no executable wrapper found ({searched}). "
            f"install the model-svc project (it ships bin/model-svc; the ~/var/bin "
            f"symlink is delightd-managed) or set ${_WRAPPER_ENV} to its wrapper."
        )

    # swap deployment/command to the wrapper's command-first contract
    argv = [wrapper, command, deployment, *(args or [])]
    # check=False: model-svc owns its own exit semantics; fleet is a transparent
    # forwarder and must propagate, not swallow, the wrapper's status.
    result = subprocess.run(argv, check=False)
    return result.returncode
