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

Services start detached and the registry reloads its routing table on change, so
start ordering is not load-bearing: a service that comes up before the registry
is reachable is picked up on the next reload.

## Container runtime

`fleet bootstrap` starts the runtime when it is down. It fails hard on a node
with less than 32 GB of memory. If it finds Docker Desktop it warns and asks for
confirmation before continuing, since a headless, resource-pinned runtime is
preferred for anything beyond casual use.
