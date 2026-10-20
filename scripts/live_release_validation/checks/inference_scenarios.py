"""Scenario-specific phases of the live inference matrix.

Two scenarios carry steps the classic baseline/HPA legs do not:

- The Triton leg serves a model repository the harness generates and uploads
  to the model registry (``gco models upload``), binds the registry URI to the
  deploy's ``--model-source``, and after the endpoint is proven absent removes
  the model and requires ``gco models list`` to no longer name it.
- The Mooncake store leg reconfigures the store after its first invocation
  (``configure-store``) and requires the role Deployment to roll onto the
  re-rendered ``mooncake.json`` (a new ``gco.io/mooncake-config-digest`` on
  the pod template), the endpoint to be promoted back to ``running``, and a
  second invocation to be answered by the rolled pod.

The mixin relies on the lifecycle for command execution, checkpointing and the
DDB wait, exactly like the runtime and inventory mixins.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, cast

from ..models import (
    MOONCAKE_CONFIG_DIGEST_ANNOTATION,
    TRITON_MODEL_NAME,
    TRITON_MODEL_VERSION,
    write_triton_model_repository,
)
from .inference_commands import (
    EndpointPlan,
    _last_json_document,
    build_configure_store_command,
    build_invoke_command,
    build_model_delete_command,
    build_model_list_command,
    build_model_upload_command,
    build_model_uri_command,
    extract_generated_text,
)
from .inference_common import InferenceCommandFailure as _CommandFailure
from .inference_common import ManagedInferenceValidationError


def _mapping(value: Any) -> dict[str, Any]:
    """A dict payload as itself, anything else as an empty dict."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


class InferenceScenarioMixin:
    """Mixin for the model-registry and Mooncake-store phases."""

    settings: Any

    def _persist(self) -> None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    def _record_failure(
        self, record: dict[str, Any], stage: str, exc: BaseException
    ) -> None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    def _run_command(
        self,
        record: dict[str, Any],
        stage: str,
        command: list[str],
        *,
        deadline: float | None = None,
    ) -> str:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    def _set_phase(
        self, record: dict[str, Any], phase: str, **values: Any
    ) -> None:  # pragma: no cover - implemented by lifecycle
        raise NotImplementedError

    # Supplied by the sibling mixins the lifecycle composes (inventory:
    # ``_kubectl_json``; runtime: the rest). Declared as annotations only, never
    # as values, so no base class carries a competing definition and the
    # resolution cannot depend on base-class order; the same convention the
    # runtime mixin uses for ``_kubectl_json``.
    _kubectl_json: Callable[..., Any | None]
    keep_cluster_tunnel_alive: Callable[..., float]
    _diagnostics_due: Callable[..., float]
    _timeout_error: Callable[..., ManagedInferenceValidationError]
    wait_for_ddb_running: Callable[..., None]

    def ensure_model_repository(
        self,
        plan: EndpointPlan,
        record: dict[str, Any],
    ) -> str | None:
        """Upload the plan's generated model repository and return its registry URI.

        Only runtimes served from the registry (Triton) have one. The repository
        is written into the private report directory, published with
        ``gco models upload`` under a run-owned name, and its URI read back with
        ``gco models uri`` so the deploy's ``--model-source`` is exactly what the
        registry resolves. Re-running after a crash re-uploads the same files,
        which is idempotent; the recorded URI is the one the stored spec is
        later checked against.
        """
        if not plan.runtime.uses_model_registry:
            return None
        repository_root = write_triton_model_repository(
            self.settings.report_dir / "model-repositories" / plan.name
        )
        record["model_repository"] = {
            "registry_model_name": plan.registry_model_name,
            "phase": "uploading",
            "model": TRITON_MODEL_NAME,
            "version": TRITON_MODEL_VERSION,
        }
        self._persist()
        try:
            self._run_command(
                record,
                "model-upload",
                build_model_upload_command(plan, repository_root),
            )
            uri_output = self._run_command(record, "model-uri", build_model_uri_command(plan))
        except _CommandFailure as exc:
            self._record_failure(record, "model-upload", exc)
            raise ManagedInferenceValidationError(
                "managed inference model repository upload failed; inspect the private checkpoint"
            ) from None
        uri_document = _last_json_document(uri_output)
        model_source = uri_document.get("s3_uri") if isinstance(uri_document, dict) else None
        if not isinstance(model_source, str) or not model_source.startswith("s3://"):
            raise ManagedInferenceValidationError(
                "managed inference model registry did not resolve an S3 URI for the upload"
            )
        record["model_source"] = model_source
        record["model_repository"]["phase"] = "uploaded"
        self._persist()
        return model_source

    def cleanup_model_repository(
        self,
        plan: EndpointPlan,
        record: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Remove the plan's registry model and prove the registry no longer lists it.

        Runs only after the endpoint itself is proven absent, and only when an
        upload was attempted, so a leg that failed before uploading never
        touches the registry. A model that was already removed is still proven
        absent through the listing.
        """
        if not plan.runtime.uses_model_registry or "model_repository" not in record:
            return None
        repository = record["model_repository"]
        if repository.get("cleanup") == "absent":
            return cast(dict[str, Any], repository)
        try:
            self._run_command(record, "model-delete", build_model_delete_command(plan))
            listing_output = self._run_command(record, "model-list", build_model_list_command())
        except _CommandFailure as exc:
            self._record_failure(record, "model-cleanup", exc)
            raise ManagedInferenceValidationError(
                "managed inference model repository cleanup failed; inspect the private checkpoint"
            ) from None
        listing = _last_json_document(listing_output)
        names = (
            {
                entry.get("model_name")
                for entry in listing
                if isinstance(entry, dict) and isinstance(entry.get("model_name"), str)
            }
            if isinstance(listing, list)
            else set()
        )
        if plan.registry_model_name in names:
            repository["cleanup"] = "listed-after-delete"
            self._persist()
            raise ManagedInferenceValidationError(
                "managed inference model registry still lists the deleted repository"
            )
        repository["cleanup"] = "absent"
        self._persist()
        return cast(dict[str, Any], repository)

    def _store_deployment_rollout(
        self,
        plan: EndpointPlan,
        record: dict[str, Any],
        *,
        deadline: float,
    ) -> tuple[str | None, bool]:
        """Return the store Deployment's config digest and whether its rollout is complete."""
        deployment = self._kubectl_json(
            record,
            "get",
            "deployment",
            plan.name,
            "--namespace",
            self.settings.namespace,
            "--output",
            "json",
            deadline=deadline,
        )
        if not isinstance(deployment, dict):
            return None, False
        metadata = _mapping(deployment.get("metadata"))
        spec = _mapping(deployment.get("spec"))
        status = _mapping(deployment.get("status"))
        annotations = _mapping(
            _mapping(_mapping(spec.get("template")).get("metadata")).get("annotations")
        )
        digest = annotations.get(MOONCAKE_CONFIG_DIGEST_ANNOTATION)
        desired = int(spec.get("replicas") or 0)
        complete = (
            desired > 0
            and int(status.get("observedGeneration") or 0) >= int(metadata.get("generation") or 0)
            and int(status.get("updatedReplicas") or 0) == desired
            and int(status.get("readyReplicas") or 0) == desired
            and int(status.get("availableReplicas") or 0) == desired
            and int(status.get("replicas") or 0) == desired
        )
        return (digest if isinstance(digest, str) else None), complete

    def verify_store_reconfigure(
        self,
        plan: EndpointPlan,
        record: dict[str, Any],
    ) -> None:
        """Reconfigure the store and require the role pods to roll onto the new file.

        The Mooncake connectors read ``mooncake.json`` once at start-up, so a
        ``configure-store`` that only re-rendered the ConfigMap would leave the
        running pod on its old settings. The monitor stamps a digest of the
        rendered file on the pod template and rolls the Deployment when it
        changes; this phase proves that: the digest observed before the change
        must differ from the one after it, the rollout must complete with the
        same replica count, the endpoint must be promoted back to ``running``
        (``update_spec`` re-enters ``deploying``), and a second invocation must
        succeed against the rolled pod.
        """
        if not plan.mooncake_store:
            return
        deadline = time.monotonic() + self.settings.readiness_timeout_seconds
        digest_before, _complete = self._store_deployment_rollout(plan, record, deadline=deadline)
        if digest_before is None:
            raise ManagedInferenceValidationError(
                "managed store Deployment carries no mooncake config digest before reconfigure"
            )
        evidence: dict[str, Any] = {"digest_before": digest_before, "phase": "configuring"}
        record["store_reconfigure"] = evidence
        self._persist()
        try:
            self._run_command(record, "configure-store", build_configure_store_command(plan))
        except _CommandFailure as exc:
            self._record_failure(record, "configure-store", exc)
            raise ManagedInferenceValidationError(
                "managed store reconfiguration command failed; inspect the private checkpoint"
            ) from None
        # From here the stored spec carries the new buffer size.
        record["store_reconfigured"] = True
        evidence["phase"] = "rolling"
        self._persist()
        heartbeat_at = time.monotonic()
        diagnostics_at = time.monotonic()
        while True:
            digest, complete = self._store_deployment_rollout(plan, record, deadline=deadline)
            evidence["digest_after"] = digest
            evidence["rollout_complete"] = complete
            self._persist()
            if digest is not None and digest != digest_before and complete:
                break
            if time.monotonic() >= deadline:
                raise self._timeout_error(
                    plan,
                    record,
                    "managed store Deployment did not roll onto the re-rendered mooncake.json",
                    reason="store-reconfigure-timeout",
                )
            heartbeat_at = self.keep_cluster_tunnel_alive(record, heartbeat_at, deadline=deadline)
            diagnostics_at = self._diagnostics_due(
                record, plan, diagnostics_at, "store-reconfigure-wait"
            )
            time.sleep(
                min(
                    float(self.settings.poll_interval_seconds),
                    max(0.0, deadline - time.monotonic()),
                )
            )
        evidence["phase"] = "rolled"
        self._persist()
        # The spec update parked the endpoint in ``deploying``; the monitor must
        # promote it again before the rolled pod is proven to answer.
        self.wait_for_ddb_running(plan, record)
        try:
            output = self._run_command(
                record, "invoke-after-reconfigure", build_invoke_command(self.settings, plan)
            )
            generated_text = extract_generated_text(output, plan.runtime.framework)
        except (ManagedInferenceValidationError, _CommandFailure) as exc:
            self._record_failure(record, "invoke-after-reconfigure", exc)
            raise ManagedInferenceValidationError(
                "managed store invocation after reconfigure failed its response contract"
            ) from None
        evidence.update(
            {
                "phase": "verified",
                "invoked_after_rollout": True,
                "generated_text_length": len(generated_text),
            }
        )
        self._set_phase(record, "store-reconfigured")
