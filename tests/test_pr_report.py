from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from fleet import pr_report
from fleet.pr_report import (
    PRReport,
    RepoReport,
    build_report,
    classify_prs,
    fetch_open_prs,
    github_slug,
    load_roster,
    _slug_from_remote,
)


# --- slug resolution -------------------------------------------------------


@pytest.mark.parametrize(
    "remote,expected",
    [
        ("git@github.com:janearc/fleet-svc.git", "janearc/fleet-svc"),
        ("git@github.com:janearc/fleet-svc", "janearc/fleet-svc"),
        ("https://github.com/janearc/fleet-svc.git", "janearc/fleet-svc"),
        ("ssh://git@github.com/janearc/fleet-svc.git", "janearc/fleet-svc"),
        ("git@gitlab.com:owner/repo.git", None),
        ("https://example.com/x.git", None),
        ("", None),
        ("git@github.com:", None),
    ],
)
def test_slug_from_remote(remote, expected):
    assert _slug_from_remote(remote) == expected


def test_github_slug_missing_dir():
    assert github_slug("/nope/does/not/exist") is None


def test_github_slug_resolves(tmp_path):
    # a real repo dir with origin pointed at github resolves to its slug
    repo = tmp_path / "r"
    repo.mkdir()
    fake = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="git@github.com:janearc/fleet-svc.git\n", stderr=""
    )
    with patch.object(pr_report.subprocess, "run", return_value=fake):
        assert github_slug(str(repo)) == "janearc/fleet-svc"


def test_github_slug_no_remote(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="no remote")
    with patch.object(pr_report.subprocess, "run", return_value=fail):
        assert github_slug(str(repo)) is None


def test_github_slug_oserror(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    with patch.object(pr_report.subprocess, "run", side_effect=OSError("boom")):
        assert github_slug(str(repo)) is None


# --- roster ----------------------------------------------------------------


_CONFIG = """
version: "1.0"
host:
  os: darwin
  arch: arm64
  daemons: [docker]
repositories:
  - name: fleet
    origin: git@github.com:janearc/fleet-svc.git
    path: ~/work/fleet
    essential: true
  - name: paling
    origin: git@github.com:janearc/paling.git
    path: ~/work/paling
"""


def _write_config(tmp_path):
    p = tmp_path / "WorkstationConfig.yaml"
    p.write_text(_CONFIG)
    return str(p)


def test_load_roster(tmp_path):
    path = _write_config(tmp_path)
    assert load_roster(path) == [("fleet", "~/work/fleet"), ("paling", "~/work/paling")]


def test_locate_config_default(monkeypatch, tmp_path):
    # with no explicit path and nothing on disk, falls back to the bare filename
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pr_report.os.path, "exists", lambda _p: False)
    assert pr_report._locate_workstation_config() == "WorkstationConfig.yaml"


# --- gh fetch --------------------------------------------------------------


def test_fetch_open_prs_ok():
    payload = [{"number": 1, "headRefName": "x", "baseRefName": "main"}]
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")
    with patch.object(pr_report.subprocess, "run", return_value=fake):
        assert fetch_open_prs("o/r") == payload


def test_fetch_open_prs_empty():
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(pr_report.subprocess, "run", return_value=fake):
        assert fetch_open_prs("o/r") == []


def test_fetch_open_prs_failure_raises():
    fail = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="auth required")
    with patch.object(pr_report.subprocess, "run", return_value=fail):
        with pytest.raises(RuntimeError, match="auth required"):
            fetch_open_prs("o/r")


# --- classification --------------------------------------------------------


def _pr(number, head, base, decision="", draft=False):
    return {
        "number": number,
        "title": f"pr {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "headRefName": head,
        "baseRefName": base,
        "isDraft": draft,
        "reviewDecision": decision,
        "mergeStateStatus": "CLEAN",
    }


def test_classify_needs_review():
    [pr] = classify_prs([_pr(1, "feat", "main")])
    assert pr.classification == "needs-review"
    assert pr.stacked_on is None and pr.blocked_on is None


def test_classify_ready_to_land():
    [pr] = classify_prs([_pr(1, "feat", "main", decision="APPROVED")])
    assert pr.classification == "ready-to-land"


def test_classify_changes_requested_is_blocked():
    [pr] = classify_prs([_pr(1, "feat", "main", decision="CHANGES_REQUESTED")])
    assert pr.classification == "blocked"


def test_classify_draft_is_blocked():
    [pr] = classify_prs([_pr(1, "feat", "main", draft=True)])
    assert pr.classification == "blocked"


def test_classify_stacked_approve_only():
    # base pr (#1) is open and unapproved; child (#2) is approved and stacked on it
    base = _pr(1, "feat/base", "main")
    child = _pr(2, "feat/child", "feat/base", decision="APPROVED")
    prs = {p.number: p for p in classify_prs([base, child])}
    assert prs[2].classification == "stacked-approve-only"
    assert prs[2].stacked_on == 1
    assert prs[2].blocked_on == 1
    # the base itself still just needs review
    assert prs[1].classification == "needs-review"


def test_classify_stacked_blocked_when_child_unapproved():
    base = _pr(1, "feat/base", "main")
    child = _pr(2, "feat/child", "feat/base")
    prs = {p.number: p for p in classify_prs([base, child])}
    assert prs[2].classification == "blocked"
    assert prs[2].blocked_on == 1


def test_classify_stack_ready_when_both_approved():
    base = _pr(1, "feat/base", "main", decision="APPROVED")
    child = _pr(2, "feat/child", "feat/base", decision="APPROVED")
    prs = {p.number: p for p in classify_prs([base, child])}
    assert prs[2].classification == "ready-to-land"
    assert prs[2].stacked_on == 1
    assert prs[2].blocked_on is None


def test_classify_no_self_stack():
    # a malformed pr whose base equals its own head must not stack on itself
    weird = _pr(1, "same", "same")
    [pr] = classify_prs([weird])
    assert pr.stacked_on is None


# --- build_report ----------------------------------------------------------


def test_build_report_full(tmp_path):
    path = _write_config(tmp_path)

    def fake_slug(p):
        # fleet is github; paling has no github remote -> omitted
        return "janearc/fleet-svc" if "fleet" in p else None

    raw = [_pr(7, "feat", "main", decision="APPROVED")]
    with patch.object(pr_report, "github_slug", side_effect=fake_slug), \
         patch.object(pr_report, "fetch_open_prs", return_value=raw):
        report = build_report(config_path=path)

    assert isinstance(report, PRReport)
    assert len(report.repos) == 1
    assert report.repos[0].name == "fleet"
    assert report.repos[0].open_prs[0].classification == "ready-to-land"


def test_build_report_marks_errored_repo(tmp_path):
    path = _write_config(tmp_path)
    with patch.object(pr_report, "github_slug", return_value="o/r"), \
         patch.object(pr_report, "fetch_open_prs", side_effect=RuntimeError("gh down")):
        report = build_report(config_path=path)

    assert all(r.error == "gh down" for r in report.repos)
    assert all(r.open_prs == [] for r in report.repos)
