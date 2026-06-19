# Post-Meteor Recovery

The runbook for restoring the fleet on a node whose state has been lost. The
common case is a crash or a forced restart; a freshly provisioned node is the
same problem with less history. Either way the node is up, the fleet is not, and
the control plane reconstructs it.

## Restore

One command. `fleet bootstrap` inspects the node, determines what already
exists, and starts only what is missing — the container runtime included. It
does not start the world unconditionally and it is safe to re-run: on a
partially-recovered node it converges the remainder.

```
fleet bootstrap
fleet show          # confirm convergence
```

## What bootstrap brings up

Tier-0 is the set of services that must exist before anything else can register
or route. Services self-identify as tier-0; the control plane discovers that
membership rather than carrying a hardcoded list. Tier-0 currently resolves to
the message backbone and the designated dynamic port registry service — the
component other services register their routes with. That role is configurable
(Traefik in this deployment; it may be Envoy or another registry elsewhere) and
is never assumed by name.

## Start ordering IS load-bearing

The registry reloads its routing table on change, so a service that comes up
before the registry is *reachable* is still picked up on the next reload — route
discovery itself is order-tolerant. **Network creation is not.** Every compose
file in the fleet attaches to one shared docker network, `dev-fleet`, and marks
it `external: true` — except the registry's, which is the one file that *defines*
the network (`driver: bridge`). A dependent started before that network exists
fails outright with `network dev-fleet not found`; the next reload cannot fix a
container that never started.

So the registry must come up first (it creates the network and owns routing) and
go down last. `fleet bootstrap` enforces this: it derives a tier-ordered plan
from the roster, brings the network owner up first, and refuses to start a
dependent while `dev-fleet` is absent (one actionable error instead of the raw
compose failure). The ordering model lives in one place (`fleet/lifecycle.py`)
and the network owner is discovered structurally — the repo whose compose file
defines `dev-fleet` rather than consuming it — not assumed by name.

## Graceful teardown: `fleet down`

`fleet down` is the graceful, dependency-ordered, fleet-scoped counterpart to
bootstrap. It stops the fleet's own compose services in the **reverse** of the
start order: workloads (obs-svc, paling) first → control plane (delightd) → the
message backbone → the network owner (traefik) **last**, since it owns the
network and the route registry.

- It is **scoped** to the fleet's compose projects: it runs `docker compose stop`
  rooted in each roster repo, so it never touches the k3d cluster containers
  (`k3d-fleet-*`) and never stops colima. (The blunt lever that *does* stop
  everything is `fleet emergency-stop`.)
- It **gates on a clean git tree** (the same check as `fleet sync`) and refuses
  to tear down uncommitted/unpushed/unverifiable state; pass `--skip-sync` to
  override, or `--dry-run` to print the plan without acting.
- It does **not** journal. The declared essential set in `WorkstationConfig` is
  the single source of truth, and `fleet bootstrap` reconverges to it. The
  down → bootstrap round-trip is a mirror: stop reverse, start forward, back to
  the declared set. (The separate `pause`/`resume` journal — the selective
  non-essential lever — is untouched.)

## Container runtime

`fleet bootstrap` starts the runtime when it is down. It fails hard on a node
with less than 32 GB of memory. If it finds Docker Desktop it warns and asks for
confirmation before continuing, since a headless, resource-pinned runtime is
preferred for anything beyond casual use.
