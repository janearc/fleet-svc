from __future__ import annotations

import os
import subprocess
import stat
from unittest.mock import patch

import pytest

from fleet import model_svc
from fleet.model_svc import (
    ModelSvcNotInstalled,
    dispatch,
    resolve_wrapper,
    wrapper_available,
)


def _make_wrapper(tmp_path, name="model-svc", executable=True):
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\nexit 0\n")
    if executable:
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


# --- resolution ------------------------------------------------------------


def test_resolve_wrapper_env_precedence(monkeypatch, tmp_path):
    wrapper = _make_wrapper(tmp_path)
    monkeypatch.setenv("FLEET_MODEL_SVC_WRAPPER", wrapper)
    assert resolve_wrapper() == wrapper


def test_resolve_wrapper_env_not_runnable_is_none(monkeypatch, tmp_path):
    # an override that does not exist resolves to None, not a doomed dispatch
    monkeypatch.setenv("FLEET_MODEL_SVC_WRAPPER", str(tmp_path / "absent"))
    assert resolve_wrapper() is None


def test_resolve_wrapper_candidate_chain(monkeypatch, tmp_path):
    monkeypatch.delenv("FLEET_MODEL_SVC_WRAPPER", raising=False)
    wrapper = _make_wrapper(tmp_path)
    # only the second candidate exists -> it is chosen
    monkeypatch.setattr(model_svc, "_WRAPPER_CANDIDATES", ("/nope/model-svc", wrapper))
    assert resolve_wrapper() == wrapper


def test_resolve_wrapper_none_when_nothing_installed(monkeypatch):
    monkeypatch.delenv("FLEET_MODEL_SVC_WRAPPER", raising=False)
    monkeypatch.setattr(model_svc, "_WRAPPER_CANDIDATES", ("/nope/a", "/nope/b"))
    assert resolve_wrapper() is None


def test_wrapper_available(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_MODEL_SVC_WRAPPER", _make_wrapper(tmp_path))
    assert wrapper_available() is True


def test_wrapper_not_available_when_not_executable(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "FLEET_MODEL_SVC_WRAPPER", _make_wrapper(tmp_path, executable=False)
    )
    assert wrapper_available() is False


# --- dispatch --------------------------------------------------------------


def test_dispatch_not_installed_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_MODEL_SVC_WRAPPER", str(tmp_path / "absent"))
    with pytest.raises(ModelSvcNotInstalled, match="model-svc not installed"):
        dispatch("dev", "health")


def test_dispatch_swaps_to_command_first_and_propagates_code(monkeypatch, tmp_path):
    # fleet takes <deployment> <command>; the wrapper is <command> <deployment>
    wrapper = _make_wrapper(tmp_path)
    monkeypatch.setenv("FLEET_MODEL_SVC_WRAPPER", wrapper)

    captured = {}

    def fake_run(argv, check):
        captured["argv"] = argv
        captured["check"] = check
        return subprocess.CompletedProcess(args=argv, returncode=2)

    with patch.object(model_svc.subprocess, "run", side_effect=fake_run):
        code = dispatch("mistral-24b", "up", ["--verbose"])

    assert code == 2
    # swapped: command first, then deployment, then passthrough args
    assert captured["argv"] == [wrapper, "up", "mistral-24b", "--verbose"]
    assert captured["check"] is False


def test_dispatch_no_args(monkeypatch, tmp_path):
    wrapper = _make_wrapper(tmp_path)
    monkeypatch.setenv("FLEET_MODEL_SVC_WRAPPER", wrapper)
    with patch.object(
        model_svc.subprocess,
        "run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0),
    ) as run:
        assert dispatch("dev", "health") == 0
    assert run.call_args.args[0] == [wrapper, "health", "dev"]
