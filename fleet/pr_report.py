from __future__ import annotations

import json
import os
import subprocess
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from fleet.models import WorkstationConfig

# pr-report surveys every roster repository that has a GitHub remote and
# classifies each open PR into one of a small, deterministic set of states. the
# roster is the declared WorkstationConfig.repositories set (name/path) -- never a
# glob of ~/work. classification is data-over-presentation: this module returns
# pydantic structs; the cli decides table vs json.

PRClass = Literal[
    "needs-review",
    "ready-to-land",
    "stacked-approve-only",
    "blocked",
]


class PullRequest(BaseModel):
    model_config = {"extra": "forbid"}

    number: int
    title: str
    url: str
    head: str
    base: str
    draft: bool
    review_decision: str
    merge_state: str
    classification: PRClass
    # set when the pr's base is another open pr in the same repo (a stack)
    stacked_on: int | None = None
    # set when this pr cannot land until another open pr (its base) lands first
    blocked_on: int | None = None


class RepoReport(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    path: str
    # the owner/repo slug resolved from the github remote
    slug: str
    open_prs: list[PullRequest] = Field(default_factory=list)
    # populated when gh could not answer for this repo (not on github, gh
    # missing, auth failure); the repo is reported, never silently dropped
    error: str | None = None


class PRReport(BaseModel):
    model_config = {"extra": "forbid"}

    repos: list[RepoReport] = Field(default_factory=list)


def _locate_workstation_config(config_path: str | None = None) -> str:
    # same resolution order as the rest of fleet: cwd first, then the canonical
    # checkout. callers may pin an explicit path (tests, a borrowed host).
    if config_path:
        return config_path
    for candidate in (
        "WorkstationConfig.yaml",
        os.path.expanduser("~/work/fleet/WorkstationConfig.yaml"),
    ):
        if os.path.exists(candidate):
            return candidate
    return "WorkstationConfig.yaml"


def load_roster(config_path: str | None = None) -> list[tuple[str, str]]:
    # the declared roster as (name, path) pairs from WorkstationConfig. raises if
    # the config cannot be parsed -- pr-report should fail loud, not survey an
    # empty fleet and report "all clear".
    path = _locate_workstation_config(config_path)
    with open(path) as handle:
        config = WorkstationConfig(**yaml.safe_load(handle))
    return [(repo.name, repo.path) for repo in config.repositories]


def github_slug(path: str) -> str | None:
    # resolve a repo's github owner/name from its origin remote. returns None for
    # a repo that has no github remote (or no git at all) so the caller can skip
    # the gh query rather than spawn a doomed subprocess.
    expanded = os.path.expanduser(path)
    if not os.path.isdir(expanded):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", expanded, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    remote = result.stdout.strip()
    return _slug_from_remote(remote)


def _slug_from_remote(remote: str) -> str | None:
    # accept both ssh (git@github.com:owner/repo.git) and https
    # (https://github.com/owner/repo.git) github remotes; everything else is not
    # a github repo as far as pr-report is concerned.
    if "github.com" not in remote:
        return None
    if remote.startswith("git@github.com:"):
        tail = remote.split("git@github.com:", 1)[1]
    elif remote.startswith("https://github.com/"):
        tail = remote.split("https://github.com/", 1)[1]
    elif remote.startswith("ssh://git@github.com/"):
        tail = remote.split("ssh://git@github.com/", 1)[1]
    else:
        return None
    return tail.removesuffix(".git").strip("/") or None


_GH_FIELDS = "number,title,url,headRefName,baseRefName,isDraft,reviewDecision,mergeStateStatus"


def fetch_open_prs(slug: str) -> list[dict]:
    # list open prs for a slug via the gh cli (json). raises on any gh failure so
    # the repo is marked errored rather than reported clean. gh is the contract:
    # it carries the user's auth and is already the fleet's github tool.
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", slug, "--state", "open",
         "--json", _GH_FIELDS, "--limit", "100"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh pr list failed")
    return json.loads(result.stdout or "[]")


def classify_prs(raw_prs: list[dict]) -> list[PullRequest]:
    # turn gh's raw pr dicts into classified PullRequest structs. the stack
    # relationship is resolved within a repo: a pr whose base branch is the head
    # branch of another open pr is "stacked". classification rules, in order:
    #   - draft                              -> blocked (not soliciting review yet)
    #   - base is another open pr (a stack):
    #       approved + base not approved     -> stacked-approve-only
    #       not approved                     -> blocked (blocked_on the base pr)
    #       approved + base approved         -> ready-to-land
    #   - standalone pr:
    #       approved                         -> ready-to-land
    #       changes requested                -> blocked
    #       otherwise                        -> needs-review
    head_to_number = {pr["headRefName"]: pr["number"] for pr in raw_prs}
    approved = {
        pr["number"]
        for pr in raw_prs
        if pr.get("reviewDecision") == "APPROVED"
    }

    out: list[PullRequest] = []
    for pr in raw_prs:
        number = pr["number"]
        base = pr["baseRefName"]
        draft = bool(pr.get("isDraft"))
        decision = pr.get("reviewDecision") or ""
        stacked_on = head_to_number.get(base)
        # a pr cannot be stacked on itself
        if stacked_on == number:
            stacked_on = None

        classification: PRClass
        blocked_on: int | None = None

        if draft:
            classification = "blocked"
        elif stacked_on is not None:
            base_ready = stacked_on in approved
            if number in approved and base_ready:
                classification = "ready-to-land"
            elif number in approved and not base_ready:
                # this pr is itself approved but its base is not -- landing it
                # would land unreviewed base commits; only the base needs action
                classification = "stacked-approve-only"
                blocked_on = stacked_on
            else:
                classification = "blocked"
                blocked_on = stacked_on
        elif decision == "APPROVED":
            classification = "ready-to-land"
        elif decision == "CHANGES_REQUESTED":
            classification = "blocked"
        else:
            classification = "needs-review"

        out.append(
            PullRequest(
                number=number,
                title=pr.get("title", ""),
                url=pr.get("url", ""),
                head=pr["headRefName"],
                base=base,
                draft=draft,
                review_decision=decision,
                merge_state=pr.get("mergeStateStatus") or "",
                classification=classification,
                stacked_on=stacked_on,
                blocked_on=blocked_on,
            )
        )
    return out


def build_report(config_path: str | None = None) -> PRReport:
    # survey the whole roster. each repo is reported even when it has no github
    # remote (skipped, no error) or gh fails (errored, surfaced) -- the report is
    # never silently short.
    report = PRReport()
    for name, path in load_roster(config_path):
        slug = github_slug(path)
        if slug is None:
            # not a github repo: omit from the report rather than emit a noisy
            # error row for something that is correctly out of scope
            continue
        repo_report = RepoReport(name=name, path=path, slug=slug)
        try:
            raw = fetch_open_prs(slug)
            repo_report.open_prs = classify_prs(raw)
        except Exception as exc:
            repo_report.error = str(exc)
        report.repos.append(repo_report)
    return report
