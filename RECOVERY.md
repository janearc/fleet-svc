# Post-Meteor Recovery

The procedure for bringing the fleet back from a cold host: the Docker engine is
down, no containers exist, and the mesh is unreachable. fleet-svc is the tool
that performs this recovery.

## Preconditions

A cold host is identified by a missing Docker socket:

```
docker info
# -> Cannot connect to the Docker daemon ... Is the docker daemon running?
```

There is nothing to resume — `fleet resume` un-pauses existing stopped
containers, which do not exist after a cold start. Use `fleet bootstrap`.

## Procedure

1. **Start the Docker runtime.** Prefer colima (see below); otherwise Docker
   Desktop. `fleet bootstrap` will start whichever it finds, then exit without
   blocking on VM boot:

   ```
   fleet bootstrap            # detects no engine, starts it, and stops here
   ```

2. **Wait for the engine, then confirm it answers:**

   ```
   docker info --format '{{.ServerVersion}}'
   ```

3. **Ignite tier-0:**

   ```
   fleet bootstrap            # second pass: engine is up, services start
   ```

   Tier-0 is derived from `WorkstationConfig.yaml`: every repository marked
   `essential: true` that ships a compose file. Today that is `traefik`,
   `delightd`, `transparent`, and `kafka-logging`. The orchestrator itself
   (`fleet`) is essential but has no compose target, so it is excluded.

   Services start detached (`docker compose up -d`) and Traefik reloads its file
   provider live, so strict start ordering is not required: a service that
   registers before Traefik is ready is picked up on the next reload.

4. **Verify:**

   ```
   fleet show
   ```

## Two-pass bootstrap

On a fully cold host the first `fleet bootstrap` only starts the engine and
returns; it does not wait for the VM to finish booting. Re-run it once the
engine reports ready. On a host where the engine is already up, a single pass
both checks daemons and starts tier-0.

## Docker runtime: prefer colima

On a memory-constrained host, Docker Desktop's idle footprint is a liability.
colima runs the same engine headless in a Lima VM whose RAM and CPU ceilings are
pinned in its own configuration, which is the control this host needs. Neither
option passes Metal through to Linux containers, so there is no capability
regression in the swap.

Migration:

```
brew install colima docker
colima start --cpu 4 --memory 8        # size to the host's budget
docker info                            # confirm the CLI is talking to colima
```

`fleet bootstrap` prefers colima automatically when the `colima` binary is on
`PATH`, falling back to `/Applications/Docker.app` otherwise. Once colima is
verified, Docker Desktop can be removed.

## Cold start from source

If the `fleet` entrypoint is not present on disk, build it before recovery. The
service is a `uv` project:

```
cd ~/work/fleet
uv sync
uv run fleet bootstrap
```

A cold datacenter is assumed capable of producing the entrypoint from source
under these steps with no prior toolchain state beyond `uv` itself. Where even
that is not guaranteed, vendor the dependencies ahead of the recovery window.
