from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fleet.models import PauseResult, ServiceRecord
from fleet.state import PauseJournal

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# Fleet annotations/labels written to k8s objects
_K8S_ANN_PAUSED = "fleet.local/paused"
_K8S_ANN_PAUSED_AT = "fleet.local/paused-at"
_K8S_ANN_PREV_REPLICAS = "fleet.local/prev-replicas"


class PauseManager:
    def __init__(self, journal: PauseJournal) -> None:
        self._journal = journal

    # ── Docker ───────────────────────────────────────────────

    async def pause_docker(self, container_id: str, docker_client: object) -> None:
        # Docker SDK client (docker.DockerClient)
        # Labels can't be mutated on existing containers without recreate,
        # so the journal is the canonical pause store for Docker.
        container = docker_client.containers.get(container_id)  # type: ignore[union-attr]
        svc_name = container.name

        prev_state = {
            "status": container.status,
            "image": container.image.tags[0] if container.image.tags else None,
            "ports": list((container.ports or {}).keys()),
        }
        self._journal.record_intent(svc_name, "docker", json.dumps(prev_state))

        container.stop(timeout=30)
        self._journal.mark_applied(svc_name)
        log.info("paused docker container: %s (%s)", svc_name, container_id)

    async def resume_docker(self, container_id: str, docker_client: object) -> None:
        container = docker_client.containers.get(container_id)  # type: ignore[union-attr]
        svc_name = container.name

        container.start()
        self._journal.mark_resumed(svc_name)
        log.info("resumed docker container: %s (%s)", svc_name, container_id)

    # ── Kubernetes ───────────────────────────────────────────

    async def pause_kube(
        self,
        deployment_name: str,
        namespace: str,
        k8s_client: object,
    ) -> None:
        # k8s_client: kubernetes.client.AppsV1Api
        apps_api = k8s_client  # type: ignore[assignment]

        deployment = apps_api.read_namespaced_deployment(  # type: ignore[union-attr]
            name=deployment_name,
            namespace=namespace,
        )
        current_replicas = deployment.spec.replicas or 1

        # Record intent before mutating
        prev_state = {
            "replicas": current_replicas,
            "namespace": namespace,
        }
        self._journal.record_intent(
            deployment_name, "kube", json.dumps(prev_state)
        )

        # Write annotations then scale to 0
        annotations = deployment.metadata.annotations or {}
        annotations[_K8S_ANN_PAUSED] = "true"
        annotations[_K8S_ANN_PAUSED_AT] = datetime.now(timezone.utc).isoformat()
        annotations[_K8S_ANN_PREV_REPLICAS] = str(current_replicas)

        body = {
            "metadata": {"annotations": annotations},
            "spec": {"replicas": 0},
        }
        apps_api.patch_namespaced_deployment(  # type: ignore[union-attr]
            name=deployment_name,
            namespace=namespace,
            body=body,
        )

        self._journal.mark_applied(deployment_name)
        log.info(
            "paused k8s deployment: %s/%s (was %d replicas)",
            namespace,
            deployment_name,
            current_replicas,
        )

    async def resume_kube(
        self,
        deployment_name: str,
        namespace: str,
        k8s_client: object,
    ) -> None:
        apps_api = k8s_client  # type: ignore[assignment]

        deployment = apps_api.read_namespaced_deployment(  # type: ignore[union-attr]
            name=deployment_name,
            namespace=namespace,
        )
        annotations = deployment.metadata.annotations or {}

        # Read prev-replicas from annotation (canonical for k8s),
        # fall back to journal
        prev_replicas_str = annotations.get(_K8S_ANN_PREV_REPLICAS)
        if prev_replicas_str is not None:
            prev_replicas = int(prev_replicas_str)
        else:
            journal_state = self._journal.get_prev_state(deployment_name)
            prev_replicas = (journal_state or {}).get("replicas", 1)

        # Remove fleet annotations, restore replicas
        for key in (_K8S_ANN_PAUSED, _K8S_ANN_PAUSED_AT, _K8S_ANN_PREV_REPLICAS):
            annotations.pop(key, None)

        body = {
            "metadata": {"annotations": annotations},
            "spec": {"replicas": prev_replicas},
        }
        apps_api.patch_namespaced_deployment(  # type: ignore[union-attr]
            name=deployment_name,
            namespace=namespace,
            body=body,
        )

        self._journal.mark_resumed(deployment_name)
        log.info(
            "resumed k8s deployment: %s/%s (restored to %d replicas)",
            namespace,
            deployment_name,
            prev_replicas,
        )

    # ── Orchestration ────────────────────────────────────────

    async def execute_pause(
        self,
        services: list[ServiceRecord],
        dry_run: bool = False,
    ) -> PauseResult:
        affected: list[ServiceRecord] = []
        skipped: list[ServiceRecord] = []
        errors: list[dict] = []

        for svc in services:
            if svc.essential:
                skipped.append(svc)
                continue

            if svc.paused_by_fleet:
                # Already paused by us — skip
                continue

            if dry_run:
                affected.append(svc)
                continue

            try:
                if svc.source == "docker":
                    # Caller must set metadata["docker_client"] and
                    # metadata["container_id"] before calling execute_pause
                    docker_client = svc.metadata.get("docker_client")
                    container_id = svc.metadata.get("container_id", svc.name)
                    if docker_client is None:
                        raise RuntimeError(
                            f"no docker_client in metadata for {svc.name}"
                        )
                    await self.pause_docker(container_id, docker_client)
                    affected.append(svc)

                elif svc.source == "kube":
                    k8s_client = svc.metadata.get("k8s_client")
                    ns = svc.namespace or "default"
                    if k8s_client is None:
                        raise RuntimeError(
                            f"no k8s_client in metadata for {svc.name}"
                        )
                    await self.pause_kube(svc.name, ns, k8s_client)
                    affected.append(svc)

                else:
                    # Non-lifecycle sources (traefik, envoy, delightd,
                    # transparent) don't support pause — record intent only
                    prev = {"status": svc.status, "source": svc.source}
                    self._journal.record_intent(
                        svc.name, svc.source, json.dumps(prev)
                    )
                    self._journal.mark_applied(svc.name)
                    affected.append(svc)

            except Exception as exc:
                log.exception("failed to pause %s", svc.name)
                errors.append({"name": svc.name, "error": str(exc)})

        return PauseResult(
            action="pause",
            dry_run=dry_run,
            affected=affected,
            skipped=skipped,
            errors=errors,
        )

    async def execute_resume(self, dry_run: bool = False) -> PauseResult:
        affected: list[ServiceRecord] = []
        skipped: list[ServiceRecord] = []
        errors: list[dict] = []

        paused = self._journal.get_paused_services()

        for row in paused:
            svc_name = row["service_name"]
            source = row["source"]
            prev = json.loads(row["prev_state"])

            svc_record = ServiceRecord(
                name=svc_name,
                source=source if source != "unknown" else "docker",
                status="paused",
                paused_by_fleet=True,
                prev_state=json.dumps(prev),
            )

            if dry_run:
                affected.append(svc_record)
                continue

            try:
                if source == "docker":
                    # Requires docker client to be available
                    try:
                        import docker as docker_lib
                        client = docker_lib.from_env()
                    except Exception as exc:
                        raise RuntimeError(
                            f"cannot connect to Docker daemon: {exc}"
                        ) from exc
                    await self.resume_docker(svc_name, client)
                    affected.append(svc_record)

                elif source == "kube":
                    try:
                        from kubernetes import client as k8s_lib, config as k8s_config
                        k8s_config.load_config()
                        apps_api = k8s_lib.AppsV1Api()
                    except Exception as exc:
                        raise RuntimeError(
                            f"cannot connect to k8s: {exc}"
                        ) from exc
                    ns = prev.get("namespace", "default")
                    await self.resume_kube(svc_name, ns, apps_api)
                    affected.append(svc_record)

                else:
                    # Non-lifecycle source — just clear journal
                    self._journal.mark_resumed(svc_name)
                    affected.append(svc_record)

            except Exception as exc:
                log.exception("failed to resume %s", svc_name)
                errors.append({"name": svc_name, "error": str(exc)})

        return PauseResult(
            action="resume",
            dry_run=dry_run,
            affected=affected,
            skipped=skipped,
            errors=errors,
        )
