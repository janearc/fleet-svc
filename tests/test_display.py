import pytest
from fleet.display import (
    render_fleet_state,
    render_pause_result,
    render_pr_report,
    render_selfcheck,
)
from fleet.pr_report import PRReport, PullRequest, RepoReport
from unittest.mock import patch

def test_render_fleet_state_json(sample_fleet_state, capsys):
    render_fleet_state(sample_fleet_state, as_json=True)
    captured = capsys.readouterr()
    assert "app-web" in captured.out
    assert "delightd" in captured.out

def test_render_fleet_state_table(sample_fleet_state, capsys):
    render_fleet_state(sample_fleet_state, as_json=False)
    captured = capsys.readouterr()
    assert "Fleet State" in captured.out
    assert "app-web" in captured.out

def test_render_selfcheck(sample_source_health, capsys):
    render_selfcheck(sample_source_health)
    captured = capsys.readouterr()
    assert "Selfcheck" in captured.out
    assert "docker" in captured.out
    assert "kube" in captured.out


def _pull(number, classification, base="main", blocked_on=None):
    return PullRequest(
        number=number,
        title=f"pr {number}",
        url=f"https://github.com/o/r/pull/{number}",
        head=f"feat/{number}",
        base=base,
        draft=False,
        review_decision="",
        merge_state="CLEAN",
        classification=classification,
        blocked_on=blocked_on,
    )


def test_render_pr_report_empty(capsys):
    render_pr_report(PRReport())
    assert "No GitHub roster" in capsys.readouterr().out


def test_render_pr_report_with_prs(capsys):
    report = PRReport(
        repos=[
            RepoReport(
                name="fleet",
                path="~/work/fleet",
                slug="janearc/fleet-svc",
                open_prs=[
                    _pull(2, "stacked-approve-only", base="feat/1", blocked_on=1),
                    _pull(3, "needs-review"),
                ],
            )
        ]
    )
    render_pr_report(report)
    out = capsys.readouterr().out
    assert "janearc/fleet-svc" in out
    assert "stacked-approve-only" in out
    assert "#1" in out


def test_render_pr_report_errored_repo(capsys):
    report = PRReport(
        repos=[RepoReport(name="x", path="~/x", slug="o/x", error="gh down")]
    )
    render_pr_report(report)
    assert "gh down" in capsys.readouterr().out


def test_render_pr_report_no_open_prs(capsys):
    report = PRReport(
        repos=[RepoReport(name="x", path="~/x", slug="o/x", open_prs=[])]
    )
    render_pr_report(report)
    assert "no open PRs" in capsys.readouterr().out
