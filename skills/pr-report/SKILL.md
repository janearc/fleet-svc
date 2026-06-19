---
name: pr-report
description: Survey open pull requests across the fleet roster and classify each (needs-review / ready-to-land / stacked-approve-only / blocked). Use when asked what PRs are open across the fleet, what needs review, what is ready to merge, or to triage a stack. Reads the WorkstationConfig roster — never globs ~/work.
---

# pr-report

`pr-report` walks the declared roster (`WorkstationConfig.repositories`, name/path
— never a glob) and, for every repository with a GitHub remote, lists the open
PRs and classifies each one.

JSON by default (machine-first); `--table` for a human view. It shells `gh`, so
it inherits the operator's GitHub auth.

## Operations

```sh
./pr-report                 # JSON: every roster repo, every open PR, classified
./pr-report --table         # human table, one per repo
./pr-report --config PATH   # use a specific WorkstationConfig.yaml
```

## Classifications

| Class | Meaning |
|-------|---------|
| `needs-review` | open, not a draft, no decision yet — a reviewer is required |
| `ready-to-land` | approved and mergeable (and, if stacked, its base is approved) |
| `stacked-approve-only` | this PR is approved, but its base is another open PR that is not — only the base needs action |
| `blocked` | draft, changes-requested, or waiting on an unapproved base PR (see `blocked_on`) |

## Stacks

A PR whose base branch is the head branch of another open PR in the same repo is
a stack. The JSON carries `stacked_on` (the base PR number) and, when the PR
cannot land yet, `blocked_on` (the PR that must land first). The table shows the
blocking PR in the "Waits On" column.

## JSON shape

```json
{
  "repos": [
    {
      "name": "fleet",
      "path": "~/work/fleet",
      "slug": "janearc/fleet-svc",
      "open_prs": [
        {
          "number": 12,
          "title": "...",
          "url": "https://github.com/...",
          "head": "feat/x",
          "base": "main",
          "draft": false,
          "review_decision": "",
          "merge_state": "CLEAN",
          "classification": "needs-review",
          "stacked_on": null,
          "blocked_on": null
        }
      ],
      "error": null
    }
  ]
}
```

A repo with no GitHub remote is omitted. A repo `gh` could not answer for is
reported with a non-null `error` rather than silently dropped — the survey is
never short.
