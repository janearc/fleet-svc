# fleet

Fleet is a standalone observability and lifecycle management tool for the local machine fleet. It aggregates service state from multiple native sources and provides unified lifecycle management (pause/resume) with label-based essentiality filtering.

## Overview

It provides both a Python CLI and an HTTP service that aggregate state from:
- **delightd**
- **Docker**
- **Kubernetes**
- **Traefik**
- **Envoy**

Git state (branch / dirty / unpushed) for managed repositories is sourced live from delightd's `GET /git` endpoint, with a local-git fallback when the daemon is unreachable so `fleet sync` can still gate a teardown.

It uses a canonical essentiality model. By labelling services with `fleet.essential="true"`, you ensure `fleet pause` will gracefully bring down all other, non-essential workloads to conserve resources.

## Essentiality Labels

Opt-in your core workloads to ensure they survive a `fleet pause`:

```yaml
# Docker Compose
services:
  delightd:
    labels:
      fleet.essential: "true"
```

```yaml
# Kubernetes
metadata:
  annotations:
    fleet.essential: "true"
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `fleet show` | Display full fleet status from all sources |
| `fleet pause` | Stop non-essential services |
| `fleet pause --dry-run` | Preview what would be paused without mutating state |
| `fleet resume` | Restore paused services |
| `fleet selfcheck` | Tool readiness (check reachability for all sources) |
| `fleet serve` | Start HTTP server on port 9400 |

## HTTP API & Auth

The HTTP server uses the same core functionality and requires SSH agent authentication for mutating requests (like `/api/pause`).

Trusted public keys should be configured in your environment. Mutating endpoints will issue an authentication challenge to the client requiring an SSH signature.

## Installation & Development

This project uses `uv` for dependency management. To run the tool locally:

```bash
uv run fleet show
uv run fleet selfcheck
```

### Running Tests

We enforce a strict 80% test coverage floor. Ensure tests pass before pushing:

```bash
uv run pytest tests/ --cov=fleet
```
