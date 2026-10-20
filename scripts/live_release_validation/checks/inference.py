"""Owned, sequential managed-inference endpoint lifecycle and evidence checks."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.example_job_validation.kube import KubectlRunner

from ..inference_contract import (
    MOONCAKE_CONFIG_DIGEST_ANNOTATION,
    MOONCAKE_STORE_RECONFIGURED_LOCAL_BUFFER_BYTES,
    TRITON_INPUT_DATA,
    TRITON_MODEL_NAME,
    TRITON_MODEL_VERSION,
    write_triton_model_repository,
)
from ..models import (
    INFERENCE_OWNER_LABEL,
    EndpointRole,
    InferenceRuntimeSpec,
    RunContext,
    RunSettings,
)
from .inference_common import (
    InferenceCommandFailure as _CommandFailure,
)
from .inference_common import (
    ManagedInferenceValidationError,
)
from .inference_inventory import KUBERNETES_INVENTORY_KINDS, InferenceInventoryMixin
from .inference_runtime import InferenceRuntimeMixin

__all__ = [
    "KUBERNETES_INVENTORY_KINDS",
    "OWNER_LABEL",
    "EndpointPlan",
    "ManagedInferenceLifecycle",
    "ManagedInferenceValidationError",
    "build_configure_store_command",
    "build_delete_command",
    "build_deploy_command",
    "build_endpoint_plans",
    "build_health_command",
    "build_invoke_command",
    "build_model_delete_command",
    "build_model_list_command",
    "build_model_upload_command",
    "build_model_uri_command",
    "build_models_command",
    "extract_generated_text",
    "initialize_run_state",
]

OWNER_LABEL = INFERENCE_OWNER_LABEL

_STATE_KEY = "inference_validation"
_MAX_PRIVATE_OUTPUT = 64 * 1024


@dataclass(frozen=True)
class EndpointPlan:
    """One immutable framework/role scenario in the sequential matrix."""

    ordinal: int
    role: EndpointRole
    name: str
    replicas: int
    autoscaling: bool
    runtime: InferenceRuntimeSpec

    @property
    def mooncake_store(self) -> bool:
        """Whether this scenario is the Mooncake shared KV-cache store."""
        return self.role == "store"

    @property
    def registry_model_name(self) -> str | None:
        """The model-registry name this scenario uploads its weights under, if any."""
        if not self.runtime.uses_model_registry:
            return None
        return f"{self.name}-repo"

    def private_dict(self, owner_nonce: str) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "framework": self.runtime.framework,
            "role": self.role,
            "name": self.name,
            "replicas": self.replicas,
            "autoscaling": self.autoscaling,
            "registry_model_name": self.registry_model_name,
            "owner_nonce": owner_nonce,
        }


def build_endpoint_plans(
    settings: RunSettings,
    owner_nonce: str,
) -> tuple[EndpointPlan, ...]:
    """Build every runtime's scenarios in matrix order with peak concurrency of one.

    vLLM runs baseline, CPU-HPA and the Mooncake store; SGLang runs baseline
    and CPU-HPA; Triton runs the baseline, served from the model registry.
    """
    token = owner_nonce[:16]
    plans: list[EndpointPlan] = []
    for runtime in settings.inference_runtimes:
        for role in runtime.roles:
            autoscaling = role == "hpa"
            plans.append(
                EndpointPlan(
                    ordinal=len(plans) + 1,
                    role=role,
                    name=f"gco-mi-{token}-{runtime.framework}-{role}",
                    replicas=(
                        settings.autoscale_initial_replicas
                        if autoscaling
                        else settings.baseline_replicas
                    ),
                    autoscaling=autoscaling,
                    runtime=runtime,
                )
            )
    return tuple(plans)


def initialize_run_state(
    ctx: RunContext,
    settings: RunSettings,
) -> tuple[tuple[EndpointPlan, ...], dict[str, Any]]:
    """Persist a random owner nonce and immutable plan before any create check."""
    raw_state = ctx.checkpoint.state.get(_STATE_KEY)
    if raw_state is None:
        owner_nonce = secrets.token_hex(32)
        plans = build_endpoint_plans(settings, owner_nonce)
        expected_plan = [plan.private_dict(owner_nonce) for plan in plans]
        state: dict[str, Any] = {
            "contract_version": 3,
            "owner_nonce": owner_nonce,
            "phase": "planned",
            "plan": expected_plan,
            "endpoints": [
                {
                    **plan.private_dict(owner_nonce),
                    "phase": "planned",
                    "incarnation": 1,
                    "closed_incarnations": [],
                    "cleanup_phase": "not-started",
                    "validation_complete": False,
                    "absence_proven": False,
                    "owned": False,
                    "commands": [],
                    "cleanup_attempts": [],
                    "failures": [],
                }
                for plan in plans
            ],
        }
        ctx.checkpoint.state[_STATE_KEY] = state
        ctx.persist()
        return plans, state

    if not isinstance(raw_state, dict):
        raise ManagedInferenceValidationError("inference checkpoint state is invalid")
    state = cast(dict[str, Any], raw_state)
    raw_owner_nonce = state.get("owner_nonce")
    if (
        state.get("contract_version") != 3
        or not isinstance(raw_owner_nonce, str)
        or len(raw_owner_nonce) != 64
        or any(character not in "0123456789abcdef" for character in raw_owner_nonce)
    ):
        raise ManagedInferenceValidationError("inference checkpoint owner nonce is invalid")
    owner_nonce = raw_owner_nonce
    plans = build_endpoint_plans(settings, owner_nonce)
    expected_plan = [plan.private_dict(owner_nonce) for plan in plans]
    if state.get("plan") != expected_plan:
        raise ManagedInferenceValidationError("inference checkpoint plan changed")
    records = state.get("endpoints")
    if not isinstance(records, list) or len(records) != len(plans):
        raise ManagedInferenceValidationError("inference endpoint checkpoint is invalid")
    for plan, raw_record in zip(plans, records, strict=True):
        if not isinstance(raw_record, dict):
            raise ManagedInferenceValidationError("inference endpoint checkpoint is invalid")
        expected = plan.private_dict(owner_nonce)
        if any(raw_record.get(key) != value for key, value in expected.items()):
            raise ManagedInferenceValidationError("inference checkpoint endpoint identity changed")
        if (
            not isinstance(raw_record.get("incarnation"), int)
            or raw_record["incarnation"] < 1
            or not isinstance(raw_record.get("closed_incarnations"), list)
        ):
            raise ManagedInferenceValidationError("inference checkpoint incarnation state changed")
    return plans, state


def _checkout_cli_prefix() -> list[str]:
    """Bind every side effect to the interpreter and checkout preflight attested."""
    return [sys.executable, "-m", "cli.main"]


def build_deploy_command(
    settings: RunSettings,
    plan: EndpointPlan,
    owner_nonce: str,
    *,
    model_source: str | None = None,
) -> list[str]:
    """Build the real argv-only ``gco inference deploy`` command.

    ``model_source`` is the registry URI a Triton scenario serves from (the
    plan's uploaded repository); it is required for such a plan and rejected
    for every other one, so a deploy can never silently fall back to a
    launcher that has no weights.
    """
    runtime = plan.runtime
    if runtime.uses_model_registry != bool(model_source):
        raise ManagedInferenceValidationError(
            "managed inference deploy needs a model-registry source exactly when the "
            "runtime serves from the registry"
        )
    command = [
        *_checkout_cli_prefix(),
        "--output",
        "json",
        "inference",
        "deploy",
        plan.name,
        "--image",
        runtime.image,
    ]
    # ``--framework`` is the strict adapter contract the CLI knows for the two
    # Hugging Face launchers; Triton is recognised from its image.
    if runtime.framework in ("vllm", "sglang"):
        command.extend(("--framework", runtime.framework))
    command.extend(
        (
            "--region",
            settings.selected_region,
            "--replicas",
            str(plan.replicas),
            "--gpu-count",
            str(settings.gpu_count),
            "--port",
            str(runtime.port),
            "--health-path",
            settings.runtime_health_path(runtime),
            "--namespace",
            settings.namespace,
            "--label",
            f"{OWNER_LABEL}={owner_nonce}",
            "--no-rewrite-image",
        )
    )
    if model_source:
        command.extend(("--model-source", model_source))
    for key, value in sorted(settings.framework_env(runtime).items()):
        command.extend(("--env", f"{key}={value}"))
    for value in settings.deploy_extra_args(runtime, endpoint_name=plan.name):
        if value.startswith("-"):
            command.append(f"--extra-args={value}")
        else:
            command.extend(("--extra-args", value))
    if plan.mooncake_store:
        # TCP keeps the scenario off the EFA pool; the cold tier is what
        # exercises the regional bucket discovery grant and its rendering.
        command.extend(
            (
                "--mooncake-mode",
                "store",
                "--mooncake-protocol",
                "tcp",
                "--mooncake-cold-tier",
            )
        )
    if plan.autoscaling:
        command.extend(
            (
                "--autoscale-metric",
                f"cpu:{settings.hpa_cpu_target}",
                "--min-replicas",
                str(settings.hpa_min_replicas),
                "--max-replicas",
                str(settings.hpa_max_replicas),
            )
        )
    return command


def build_invoke_command(
    settings: RunSettings,
    plan: EndpointPlan,
) -> list[str]:
    """Build the deterministic, buffered real CLI invocation."""
    return [
        *_checkout_cli_prefix(),
        "--output",
        "json",
        "inference",
        "invoke",
        plan.name,
        "--data",
        json.dumps(
            settings.request_body(plan.runtime),
            sort_keys=True,
            separators=(",", ":"),
        ),
        "--path",
        plan.runtime.request_path,
        "--region",
        settings.selected_region,
    ]


def build_health_command(settings: RunSettings, plan: EndpointPlan) -> list[str]:
    """Build an authenticated health probe through the public CLI path."""
    return [
        *_checkout_cli_prefix(),
        "--output",
        "json",
        "inference",
        "health",
        plan.name,
        "--region",
        settings.selected_region,
    ]


def build_models_command(settings: RunSettings, plan: EndpointPlan) -> list[str]:
    """Build the framework-aware authenticated model-identity probe.

    ``--framework`` names the two launcher contracts the CLI accepts; for
    Triton the command resolves the KServe statistics path from the image.
    """
    command = [
        *_checkout_cli_prefix(),
        "--output",
        "json",
        "inference",
        "models",
        plan.name,
    ]
    if plan.runtime.framework in ("vllm", "sglang"):
        command.extend(("--framework", plan.runtime.framework))
    command.extend(("--region", settings.selected_region))
    return command


def build_model_upload_command(plan: EndpointPlan, repository_root: Path) -> list[str]:
    """Build the registry upload of the plan's generated model repository."""
    return [
        *_checkout_cli_prefix(),
        "--output",
        "json",
        "models",
        "upload",
        str(repository_root),
        "--name",
        str(plan.registry_model_name),
    ]


def build_model_uri_command(plan: EndpointPlan) -> list[str]:
    """Build the lookup of the plan's registry URI (the deploy's ``--model-source``)."""
    return [
        *_checkout_cli_prefix(),
        "--output",
        "json",
        "models",
        "uri",
        str(plan.registry_model_name),
    ]


def build_model_delete_command(plan: EndpointPlan) -> list[str]:
    """Build the non-interactive removal of the plan's registry model."""
    return [
        *_checkout_cli_prefix(),
        "models",
        "delete",
        str(plan.registry_model_name),
        "--yes",
    ]


def build_model_list_command() -> list[str]:
    """Build the registry listing used to prove a model's absence."""
    return [*_checkout_cli_prefix(), "--output", "json", "models", "list"]


def build_configure_store_command(plan: EndpointPlan) -> list[str]:
    """Build the store reconfiguration whose re-rendered file must roll the role pods."""
    return [
        *_checkout_cli_prefix(),
        "--output",
        "json",
        "inference",
        "configure-store",
        plan.name,
        "--local-buffer-size",
        str(MOONCAKE_STORE_RECONFIGURED_LOCAL_BUFFER_BYTES),
    ]


def build_delete_command(
    settings: RunSettings,
    plan: EndpointPlan,
    owner_nonce: str,
    lifecycle_id: str,
) -> list[str]:
    """Build deletion atomically bound to owner nonce and endpoint incarnation."""
    del settings  # Kept for a parallel command-builder interface.
    return [
        *_checkout_cli_prefix(),
        "inference",
        "delete",
        plan.name,
        "--expected-owner-label",
        f"{OWNER_LABEL}={owner_nonce}",
        "--expected-lifecycle-id",
        lifecycle_id,
        "--yes",
    ]


def _mapping(value: Any) -> dict[str, Any]:
    """A dict payload as itself, anything else as an empty dict."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _last_json_document(output: str) -> Any:
    decoder = json.JSONDecoder()
    candidate: Any = None
    found = False
    for index, character in enumerate(output):
        if character not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(output, index)
        except json.JSONDecodeError:
            continue
        if output[end:].strip():
            continue
        candidate = value
        found = True
    if not found:
        raise ManagedInferenceValidationError(
            "managed inference invoke returned no terminal JSON document"
        )
    return candidate


def extract_generated_text(output: str, framework: str) -> str:
    """Require the exact response schema for the request adapter in use."""
    payload = _last_json_document(output)
    text: Any = None
    if framework == "vllm":
        if isinstance(payload, dict):
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                text = choices[0].get("text")
    elif framework == "sglang":
        # Native ``/generate`` answers a single prompt with one object; a
        # batched prompt would answer with a list, which this adapter never
        # sends and therefore never accepts.
        if isinstance(payload, dict):
            text = payload.get("text")
    elif framework == "triton":
        # KServe v2: the identity model must echo the request tensor exactly,
        # so this is an equality check on ``outputs[0].data`` rather than a
        # non-emptiness check on generated text.
        outputs = payload.get("outputs") if isinstance(payload, dict) else None
        first = outputs[0] if isinstance(outputs, list) and outputs else None
        data = first.get("data") if isinstance(first, dict) else None
        if (
            isinstance(first, dict)
            and first.get("name") == "OUTPUT0"
            and isinstance(data, list)
            and len(data) == len(TRITON_INPUT_DATA)
            and all(
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and float(value) == expected
                for value, expected in zip(data, TRITON_INPUT_DATA, strict=True)
            )
        ):
            text = json.dumps([float(value) for value in data])
    else:
        raise ManagedInferenceValidationError("unknown managed inference response framework")
    if not isinstance(text, str) or not text.strip():
        raise ManagedInferenceValidationError(
            f"managed {framework} inference response did not match its non-empty text schema"
        )
    return text.strip()


class ManagedInferenceLifecycle(InferenceInventoryMixin, InferenceRuntimeMixin):
    """Run and clean the runtime matrix with durable ownership.

    Six strictly sequential scenarios: vLLM baseline, CPU-HPA and Mooncake
    store (TCP, cold tier, reconfigured mid-leg), SGLang baseline and CPU-HPA,
    and Triton baseline served from a model repository the run uploads to the
    registry and removes afterwards.
    """

    def __init__(
        self,
        *,
        ctx: RunContext,
        settings: RunSettings,
        plans: tuple[EndpointPlan, ...],
        state: dict[str, Any],
        kubectl: KubectlRunner,
        kubeconfig_path: Path,
    ) -> None:
        self.ctx = ctx
        self.settings = settings
        self.plans = plans
        self.state = state
        self.kubectl = kubectl
        self.kubeconfig_path = kubeconfig_path
        owner_nonce = state.get("owner_nonce")
        if not isinstance(owner_nonce, str) or not owner_nonce:
            raise ManagedInferenceValidationError("inference owner nonce is missing")
        self.owner_nonce = owner_nonce
        records = state.get("endpoints")
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ManagedInferenceValidationError("managed inference endpoint state is invalid")
        self.records = cast(list[dict[str, Any]], records)
        self._table: Any | None = None

    def _persist(self) -> None:
        self.ctx.persist()

    def _set_phase(
        self,
        record: dict[str, Any],
        phase: str,
        **values: Any,
    ) -> None:
        record["phase"] = phase
        record.update(values)
        self.state["phase"] = f"endpoint-{record['ordinal']}:{phase}"
        self._persist()

    def _record_failure(
        self,
        record: dict[str, Any],
        stage: str,
        exc: BaseException,
    ) -> None:
        failures = record.setdefault("failures", [])
        if not isinstance(failures, list):
            failures = []
            record["failures"] = failures
        failures.append({"stage": stage, "error": f"{type(exc).__name__}: {exc}"})
        record["last_failed_phase"] = stage
        self._persist()

    @property
    def table(self) -> Any:
        if self._table is None:
            try:
                resource = self.ctx.session.resource(
                    "dynamodb", region_name=self.ctx.config.global_region
                )
                self._table = resource.Table(f"{self.ctx.config.project_name}-inference-endpoints")
            except Exception as exc:
                self.state["ddb_setup_error"] = f"{type(exc).__name__}: {exc}"
                self._persist()
                raise ManagedInferenceValidationError(
                    "managed inference state store could not be opened"
                ) from None
        return self._table

    def _strong_get(self, record: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self.table.get_item(
                Key={"endpoint_name": record["name"]},
                ConsistentRead=True,
            )
        except Exception as exc:
            self._record_failure(record, "ddb-strong-read", exc)
            raise ManagedInferenceValidationError(
                "managed inference strong state read failed; inspect the private checkpoint"
            ) from None
        item = response.get("Item") if isinstance(response, dict) else None
        if item is None:
            return None
        if not isinstance(item, dict):
            raise ManagedInferenceValidationError("managed inference state record is malformed")
        return cast(dict[str, Any], item)

    def _is_owned(self, item: dict[str, Any]) -> bool:
        labels = item.get("labels")
        return isinstance(labels, dict) and labels.get(OWNER_LABEL) == self.owner_nonce

    def _verify_item_contract(
        self,
        plan: EndpointPlan,
        item: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        lifecycle_id = item.get("lifecycle_id")
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            raise ManagedInferenceValidationError(
                "inference stored endpoint has no immutable lifecycle identity"
            )
        closed = record.get("closed_incarnations")
        closed_ids = (
            {
                entry.get("lifecycle_id")
                for entry in closed
                if isinstance(entry, dict) and isinstance(entry.get("lifecycle_id"), str)
            }
            if isinstance(closed, list)
            else set()
        )
        if lifecycle_id in closed_ids:
            raise ManagedInferenceValidationError(
                "inference endpoint reverted to a closed lifecycle incarnation"
            )
        observed_lifecycle = record.get("lifecycle_id")
        if observed_lifecycle is None:
            record["lifecycle_id"] = lifecycle_id
            self._persist()
        elif observed_lifecycle != lifecycle_id:
            raise ManagedInferenceValidationError(
                "inference endpoint incarnation changed; refusing replacement ownership"
            )
        spec = item.get("spec")
        if not isinstance(spec, dict):
            raise ManagedInferenceValidationError("managed inference stored spec is malformed")
        runtime = plan.runtime
        expected_base: dict[str, Any] = {
            "image": runtime.image,
            "port": runtime.port,
            "replicas": plan.replicas,
            "gpu_count": self.settings.gpu_count,
            "health_check_path": self.settings.runtime_health_path(runtime),
        }
        # The two launcher contracts are persisted explicitly; Triton has no
        # ``--framework`` and is recognised from its image.
        expected_base["framework"] = (
            runtime.framework if runtime.framework in ("vllm", "sglang") else None
        )
        # Every launcher receives its arguments on argv, so a stored record
        # without exactly those args is not this run's endpoint.
        expected_base["args"] = list(
            self.settings.deploy_extra_args(runtime, endpoint_name=plan.name)
        )
        # The scenario's Mooncake block (store mode with the cold tier, plus
        # the buffer size once ``configure-store`` has run) or none at all.
        expected_base["mooncake"] = self.settings.mooncake_block(
            plan.role, reconfigured=record.get("store_reconfigured") is True
        )
        for key, value in expected_base.items():
            if spec.get(key) != value:
                raise ManagedInferenceValidationError(
                    "managed inference stored endpoint contract does not match this run"
                )
        # A runtime that takes no environment persists none (``None`` or an
        # empty map); the launchers persist exactly ``MODEL``.
        expected_env = self.settings.framework_env(runtime)
        if (spec.get("env") or {}) != expected_env:
            raise ManagedInferenceValidationError(
                "managed inference stored endpoint contract does not match this run"
            )
        if runtime.uses_model_registry:
            model_source = record.get("model_source")
            if not isinstance(model_source, str) or spec.get("model_source") != model_source:
                raise ManagedInferenceValidationError(
                    "managed inference stored model source does not match this run's upload"
                )
        elif spec.get("model_source") is not None:
            raise ManagedInferenceValidationError(
                "managed inference stored endpoint unexpectedly serves from a model source"
            )
        if item.get("target_regions") != [self.settings.selected_region]:
            raise ManagedInferenceValidationError(
                "managed inference stored target region does not match this run"
            )
        if item.get("namespace") != self.settings.namespace:
            raise ManagedInferenceValidationError(
                "managed inference stored namespace does not match this run"
            )
        autoscaling = spec.get("autoscaling")
        if plan.autoscaling:
            expected_autoscaling = {
                "enabled": True,
                "min_replicas": self.settings.hpa_min_replicas,
                "max_replicas": self.settings.hpa_max_replicas,
                "metrics": [{"type": "cpu", "target": self.settings.hpa_cpu_target}],
            }
            if autoscaling != expected_autoscaling:
                raise ManagedInferenceValidationError(
                    "managed inference stored HPA contract does not match this run"
                )
        elif autoscaling is not None:
            raise ManagedInferenceValidationError(
                "managed inference baseline unexpectedly has autoscaling configured"
            )

    @staticmethod
    def _truncated(value: str) -> str:
        return value[-_MAX_PRIVATE_OUTPUT:]

    def _set_invoke_journal_outcome(
        self,
        record: dict[str, Any],
        status: str,
        **values: Any,
    ) -> None:
        """Update the durable non-replay journal in the same checkpoint write."""
        journal = record.get("invoke_journal")
        if not isinstance(journal, dict):
            raise ManagedInferenceValidationError("managed inference invoke journal is invalid")
        journal["status"] = status
        journal.update(values)

    def _run_command(
        self,
        record: dict[str, Any],
        stage: str,
        command: list[str],
        *,
        deadline: float | None = None,
    ) -> str:
        environment = dict(os.environ)
        environment["KUBECONFIG"] = str(self.kubeconfig_path)
        command_timeout = float(self.settings.command_timeout_seconds)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                commands = cast(list[dict[str, Any]], record.setdefault("commands", []))
                commands.append({"stage": stage, "argv": command, "deadline_exhausted": True})
                self._persist()
                raise _CommandFailure(f"{stage} phase deadline exhausted")
            command_timeout = min(command_timeout, remaining)
        try:
            result = subprocess.run(
                command,
                cwd=self.settings.repo_root,
                capture_output=True,
                text=True,
                timeout=command_timeout,
                env=environment,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            commands = cast(list[dict[str, Any]], record.setdefault("commands", []))
            commands.append(
                {
                    "stage": stage,
                    "argv": command,
                    "timed_out": True,
                    "timeout_seconds": command_timeout,
                    "stdout": self._truncated(str(exc.stdout or "")),
                    "stderr": self._truncated(str(exc.stderr or "")),
                }
            )
            if stage == "invoke":
                self._set_invoke_journal_outcome(
                    record,
                    "ambiguous",
                    reason="timeout",
                    stdout=self._truncated(str(exc.stdout or "")),
                )
            self._persist()
            raise _CommandFailure(f"{stage} timed out") from None
        except (OSError, UnicodeError) as exc:
            commands = cast(list[dict[str, Any]], record.setdefault("commands", []))
            commands.append(
                {
                    "stage": stage,
                    "argv": command,
                    "launch_error": f"{type(exc).__name__}: {exc}",
                }
            )
            if stage == "invoke":
                self._set_invoke_journal_outcome(
                    record,
                    "failed",
                    reason="launch-error",
                )
            self._persist()
            raise _CommandFailure(f"{stage} could not start") from None

        commands = cast(list[dict[str, Any]], record.setdefault("commands", []))
        commands.append(
            {
                "stage": stage,
                "argv": command,
                "returncode": result.returncode,
                "stdout": self._truncated(result.stdout),
                "stderr": self._truncated(result.stderr),
            }
        )
        if stage == "invoke":
            self._set_invoke_journal_outcome(
                record,
                "succeeded" if result.returncode == 0 else "failed",
                returncode=result.returncode,
                stdout=self._truncated(result.stdout),
                stderr=self._truncated(result.stderr),
            )
        self._persist()
        if result.returncode != 0:
            raise _CommandFailure(f"{stage} exited nonzero")
        return result.stdout

    def ensure_owned_endpoint(
        self,
        plan: EndpointPlan,
        record: dict[str, Any],
    ) -> None:
        """Adopt only this run's marker, or prove collision-free absence then create."""
        item = self._strong_get(record)
        if item is not None:
            if not self._is_owned(item):
                raise ManagedInferenceValidationError(
                    "managed inference endpoint collision detected; refusing ownership"
                )
            self._verify_item_contract(plan, item, record)
            record["owned"] = True
            self._set_phase(record, "ownership-confirmed")
            return

        inventory = self.kubernetes_inventory(
            record,
            deadline=time.monotonic() + self.settings.readiness_timeout_seconds,
        )
        if any(inventory.values()):
            raise ManagedInferenceValidationError(
                "managed inference Kubernetes name collision detected; refusing creation"
            )
        self._set_phase(record, "collision-checked")
        model_source = self.ensure_model_repository(plan, record)
        self._set_phase(record, "deploy-started")
        try:
            self._run_command(
                record,
                "deploy",
                build_deploy_command(
                    self.settings, plan, self.owner_nonce, model_source=model_source
                ),
            )
        except _CommandFailure as exc:
            self._record_failure(record, "deploy", exc)
            item = self._strong_get(record)
            if item is None or not self._is_owned(item):
                raise ManagedInferenceValidationError(
                    "managed inference deploy failed; inspect the private checkpoint"
                ) from None
        self._wait_for_owned_record(plan, record)

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

    def _wait_for_healthy_backend(
        self,
        plan: EndpointPlan,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        attempts = record.setdefault("backend_probe_attempts", [])
        if not isinstance(attempts, list):
            raise ManagedInferenceValidationError("managed backend probe history is invalid")
        deadline = time.monotonic() + self.settings.readiness_timeout_seconds
        heartbeat_at = time.monotonic()
        while True:
            if time.monotonic() >= deadline:
                raise self._timeout_error(
                    plan,
                    record,
                    "managed health did not converge before timeout",
                    reason="health-timeout",
                )
            heartbeat_at = self.keep_cluster_tunnel_alive(record, heartbeat_at, deadline=deadline)
            item = self._strong_get(record)
            if item is None or not self._is_owned(item):
                raise ManagedInferenceValidationError("managed ownership changed during health")
            self._verify_item_contract(plan, item, record)
            statuses = item.get("region_status")
            regional = (
                statuses.get(self.settings.selected_region) if isinstance(statuses, dict) else None
            )
            if (
                item.get("desired_state") != "running"
                or not isinstance(regional, dict)
                or regional.get("state") != "running"
            ):
                raise ManagedInferenceValidationError(
                    "managed endpoint stopped running during health"
                )
            attempt: dict[str, Any] = {
                "attempt": len(attempts) + 1,
                "started_at_monotonic": time.monotonic(),
                "classification": "started",
            }
            attempts.append(attempt)

            def finish(classification: str, **values: Any) -> None:
                attempt.update(  # noqa: B023 - helper is called synchronously in this iteration
                    ended_at_monotonic=time.monotonic(), classification=classification, **values
                )
                self._persist()

            self._persist()
            try:
                output = self._run_command(
                    record,
                    "health",
                    build_health_command(self.settings, plan),
                    deadline=deadline,
                )
            except _CommandFailure as exc:
                finish("command-failed", error=str(exc))
                raise
            try:
                health = _last_json_document(output)
            except ManagedInferenceValidationError as exc:
                finish("malformed-output", error=str(exc))
                raise
            status = health.get("status") if isinstance(health, dict) else None
            http_status = health.get("http_status") if isinstance(health, dict) else None
            if (
                not isinstance(status, str)
                or isinstance(http_status, bool)
                or not isinstance(http_status, int)
            ):
                finish("malformed-contract")
                raise ManagedInferenceValidationError(
                    "managed inference health probe returned a malformed contract"
                )
            attempt.update(
                {
                    "status": status,
                    "http_status": http_status,
                    "path": health.get("path"),
                    "latency_ms": health.get("latency_ms"),
                    "body_summary": self._truncated(
                        json.dumps(health.get("body"), sort_keys=True, default=str)
                    ),
                }
            )
            if status == "healthy" and 200 <= http_status < 300:
                finish("healthy")
                return cast(dict[str, Any], health)
            retryable = status == "unhealthy" and (
                http_status in {404, 429} or 500 <= http_status < 600
            )
            if not retryable:
                finish("terminal-contract")
                raise ManagedInferenceValidationError(
                    "managed inference health probe returned a terminal status or contract"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                finish("deadline-exhausted")
                raise ManagedInferenceValidationError(
                    "managed inference health endpoint did not converge before timeout"
                )
            finish("retryable-unhealthy")
            time.sleep(min(float(self.settings.poll_interval_seconds), remaining))

    def verify_backend_probes(
        self,
        plan: EndpointPlan,
        record: dict[str, Any],
    ) -> None:
        """Require converged authenticated health and exact model discovery."""
        try:
            health = self._wait_for_healthy_backend(plan, record)
            evidence: dict[str, Any] = {
                "health": {
                    "healthy": True,
                    "http_status": health["http_status"],
                    "path": self.settings.runtime_health_path(plan.runtime),
                }
            }

            runtime = plan.runtime
            model_output = self._run_command(
                record,
                "model-info",
                build_models_command(self.settings, plan),
            )
            model_info = _last_json_document(model_output)
            if runtime.framework == "vllm":
                data = model_info.get("data") if isinstance(model_info, dict) else None
                model_ids = (
                    [
                        item.get("id")
                        for item in data
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    ]
                    if isinstance(data, list)
                    else []
                )
                if runtime.model_id not in model_ids:
                    raise ManagedInferenceValidationError(
                        "managed vLLM model inventory omitted the configured model"
                    )
                evidence["model_info"] = {
                    "path": runtime.model_info_path,
                    "configured_model_present": True,
                    "model_revision_pinned": True,
                    "model_count": len(model_ids),
                }
            elif runtime.framework == "triton":
                # ``/v2/models/stats`` is Triton's inventory: every loaded
                # model by name and version. The generated identity model must
                # be loaded at its single version.
                stats = model_info.get("model_stats") if isinstance(model_info, dict) else None
                loaded = (
                    {
                        (entry.get("name"), str(entry.get("version")))
                        for entry in stats
                        if isinstance(entry, dict)
                    }
                    if isinstance(stats, list)
                    else set()
                )
                if (runtime.model_id, TRITON_MODEL_VERSION) not in loaded:
                    raise ManagedInferenceValidationError(
                        "managed Triton model statistics omitted the generated model"
                    )
                evidence["model_info"] = {
                    "path": runtime.model_info_path,
                    "configured_model_present": True,
                    "model_version": TRITON_MODEL_VERSION,
                    "model_count": len(loaded),
                }
            else:
                # ``gco inference models --framework sglang`` projects the
                # server's resolved launcher arguments (``/server_info``), so
                # the running process itself reports which model path and
                # which revision it was started with.
                if (
                    not isinstance(model_info, dict)
                    or model_info.get("model_path") != runtime.model_id
                    or model_info.get("revision") != runtime.model_revision
                ):
                    raise ManagedInferenceValidationError(
                        "managed SGLang /server_info did not report the exact model path "
                        "and revision"
                    )
                evidence["model_info"] = {
                    "path": runtime.model_info_path,
                    "configured_model_present": True,
                    "configured_revision_present": True,
                }
        except (ManagedInferenceValidationError, _CommandFailure) as exc:
            self._record_failure(record, "backend-probes", exc)
            raise ManagedInferenceValidationError(
                "managed inference health/model probe failed its contract"
            ) from None

        record["backend_probe_evidence"] = evidence
        self._set_phase(record, "backend-probes-verified")

    def invoke(self, plan: EndpointPlan, record: dict[str, Any]) -> None:
        """Invoke once, or recover a durable successful outcome without replay."""
        command = build_invoke_command(self.settings, plan)
        journal = record.get("invoke_journal")
        if journal is None:
            journal = {
                "status": "started",
                "framework": plan.runtime.framework,
                "request_path": plan.runtime.request_path,
                "argv": command,
            }
            record["invoke_journal"] = journal
            self._persist()
            output: str | None = None
        elif not isinstance(journal, dict):
            raise ManagedInferenceValidationError("managed inference invoke journal is invalid")
        else:
            if (
                journal.get("framework") != plan.runtime.framework
                or journal.get("request_path") != plan.runtime.request_path
                or journal.get("argv") != command
            ):
                raise ManagedInferenceValidationError(
                    "managed inference invoke journal identity changed"
                )
            status = journal.get("status")
            if status == "succeeded" and isinstance(journal.get("stdout"), str):
                output = journal["stdout"]
            elif status in {"started", "ambiguous", "failed"}:
                raise ManagedInferenceValidationError(
                    "managed inference invocation has a non-replayable persisted outcome"
                )
            else:
                raise ManagedInferenceValidationError(
                    "managed inference invoke journal status is invalid"
                )
        try:
            if output is None:
                output = self._run_command(record, "invoke", command)
            generated_text = extract_generated_text(output, plan.runtime.framework)
        except (ManagedInferenceValidationError, _CommandFailure) as exc:
            self._record_failure(record, "invoke", exc)
            raise ManagedInferenceValidationError(
                "managed inference invocation failed its response contract"
            ) from None
        record["invoke_evidence"] = {
            "framework": plan.runtime.framework,
            "generated_text_non_empty": True,
            "generated_text_length": len(generated_text),
            "replayed": False,
        }
        self._set_phase(record, "invoked")

    def _prepare_incarnation_for_resume(
        self,
        plan: EndpointPlan,
        record: dict[str, Any],
    ) -> None:
        """Rotate a fully cleaned pre-invocation lifecycle before redeploying."""
        lifecycle_id = record.get("lifecycle_id")
        if (
            record.get("cleanup_phase") != "absent"
            or not isinstance(lifecycle_id, str)
            or not lifecycle_id
            or isinstance(record.get("invoke_evidence"), dict)
            or isinstance(record.get("invoke_journal"), dict)
        ):
            return
        # Re-prove strong DDB and Kubernetes absence before closing the old incarnation.
        evidence = self.prove_absence(record)
        if evidence.get("stable_absence_observations") != 2:
            raise ManagedInferenceValidationError(
                "cleaned inference incarnation did not retain stable absence"
            )
        closed = record.setdefault("closed_incarnations", [])
        if not isinstance(closed, list):
            raise ManagedInferenceValidationError("inference closed-incarnation state is invalid")
        closed.append(
            {
                "number": record.get("incarnation", 1),
                "lifecycle_id": lifecycle_id,
                "invoked": False,
                "cleanup_phase": "absent",
                "absence_evidence": evidence,
                "commands": record.get("commands", []),
                "backend_probe_attempts": record.get("backend_probe_attempts", []),
                "tunnel_heartbeats": record.get("tunnel_heartbeats", []),
                "cleanup_attempts": record.get("cleanup_attempts", []),
                "failures": record.get("failures", []),
            }
        )
        record.update(
            {
                "incarnation": int(record.get("incarnation", 1)) + 1,
                "lifecycle_id": None,
                "owned": False,
                "phase": "planned-resume-incarnation",
                "cleanup_phase": "not-started",
                "validation_complete": False,
                "validation_steps_complete": False,
                "absence_proven": True,
                "commands": [],
                "cleanup_attempts": [],
                "failures": [],
            }
        )
        for key in (
            "backend_probe_evidence",
            "backend_probe_attempts",
            "tunnel_heartbeats",
            "invoke_evidence",
            "invoke_journal",
            "hpa_stability_observations",
            "last_hpa_replica_observation",
            "last_readiness",
            "last_failed_phase",
        ):
            record.pop(key, None)
        self.state["phase"] = f"endpoint-{plan.ordinal}:incarnation-rotated"
        self._persist()

    def run_endpoint(self, plan: EndpointPlan, record: dict[str, Any]) -> bool:
        """Run one endpoint's validation phases, or safely continue a durable resume."""
        if record.get("validation_complete") is True:
            self.prove_absence(record)
            return False
        invocation_finished = (
            record.get("phase") == "invoked"
            and isinstance(record.get("invoke_evidence"), dict)
            and record["invoke_evidence"].get("generated_text_non_empty") is True
        )
        if record.get("validation_steps_complete") is True or invocation_finished:
            # A crash after invoke or cleanup must never recreate or reinvoke;
            # the caller's finally only needs to finish/prove cleanup.
            record["validation_steps_complete"] = True
            record["phase"] = "validation-complete-resume"
            self._persist()
            return False
        journal = record.get("invoke_journal")
        if isinstance(journal, dict) and not isinstance(record.get("invoke_evidence"), dict):
            # Invocation intent is one-way: recover durable success without a request;
            # any other persisted state fails closed for cleanup.
            self.invoke(plan, record)
            record["validation_steps_complete"] = True
            self._persist()
            return False
        self._prepare_incarnation_for_resume(plan, record)
        record["absence_proven"] = False
        self._set_phase(record, "starting")
        self.ensure_owned_endpoint(plan, record)
        self.wait_for_ddb_running(plan, record)
        self.wait_for_kubernetes_ready(plan, record)
        if plan.autoscaling:
            self.verify_hpa_stability(plan, record)
        self.verify_backend_probes(plan, record)
        self.invoke(plan, record)
        # The store scenario then changes its configuration and must roll
        # onto it; the rolled pods are what the restart audit below sees.
        self.verify_store_reconfigure(plan, record)
        # Last, so it covers the whole leg: a container that crashed or was
        # killed by a probe anywhere between creation and invocation fails
        # the leg even though every wait above was satisfied in between.
        self.verify_no_container_restarts(plan, record)
        record["validation_steps_complete"] = True
        self._persist()
        return True

    def cleanup_endpoint(self, plan: EndpointPlan, record: dict[str, Any]) -> dict[str, Any]:
        """Delete only a matching marker, then prove strong DDB and full K8s absence."""
        attempts = record.setdefault("cleanup_attempts", [])
        if not isinstance(attempts, list):
            attempts = []
            record["cleanup_attempts"] = attempts
        attempt: dict[str, Any] = {"started_at_monotonic": time.monotonic()}
        attempts.append(attempt)
        record["cleanup_phase"] = "checking-ownership"
        self._persist()

        delete_error: Exception | None = None
        item = self._strong_get(record)
        if item is not None:
            if not self._is_owned(item):
                attempt["refused_collision"] = True
                self._persist()
                raise ManagedInferenceValidationError(
                    "managed inference cleanup refused a colliding endpoint"
                )
            self._verify_item_contract(plan, item, record)
            record["owned"] = True
            if item.get("desired_state") != "deleted":
                record["cleanup_phase"] = "delete-requested"
                self._persist()
                lifecycle_id = record.get("lifecycle_id")
                if not isinstance(lifecycle_id, str) or not lifecycle_id:
                    raise ManagedInferenceValidationError(
                        "inference cleanup has no checkpointed lifecycle identity"
                    )
                try:
                    self._run_command(
                        record,
                        "delete",
                        build_delete_command(
                            self.settings,
                            plan,
                            self.owner_nonce,
                            lifecycle_id,
                        ),
                    )
                except _CommandFailure as exc:
                    delete_error = exc
                    self._record_failure(record, "delete", exc)
                    replacement = self._strong_get(record)
                    if replacement is not None and (
                        not self._is_owned(replacement)
                        or replacement.get("lifecycle_id") != record.get("lifecycle_id")
                    ):
                        attempt["refused_replacement_race"] = True
                        self._persist()
                        raise ManagedInferenceValidationError(
                            "managed inference cleanup refused a replacement endpoint"
                        ) from None

        try:
            evidence = self.prove_absence(record)
            repository = self.cleanup_model_repository(plan, record)
        except (Exception, KeyboardInterrupt) as exc:
            attempt["completed"] = False
            attempt["error"] = f"{type(exc).__name__}: {exc}"
            if delete_error is not None:
                attempt["delete_error"] = f"{type(delete_error).__name__}: {delete_error}"
            self._persist()
            raise
        if repository is not None:
            evidence = {**evidence, "model_repository": repository.get("cleanup")}
        attempt["completed"] = True
        attempt["delete_recovered"] = delete_error is not None
        record["cleanup_phase"] = "absent"
        self._persist()
        return evidence

    def execute(self) -> dict[str, Any]:
        """Run strictly sequential endpoints and aggregate only after all cleanup attempts."""
        primary_error: Exception | KeyboardInterrupt | None = None
        cleanup_errors: list[tuple[int, Exception | KeyboardInterrupt]] = []
        validated = 0
        self.state["phase"] = "running"
        self._persist()

        try:
            for plan, record in zip(self.plans, self.records, strict=True):
                endpoint_error: Exception | KeyboardInterrupt | None = None
                ran = False
                try:
                    ran = self.run_endpoint(plan, record)
                except (Exception, KeyboardInterrupt) as exc:
                    endpoint_error = exc
                    self._record_failure(record, str(record.get("phase", "endpoint")), exc)
                finally:
                    try:
                        self.cleanup_endpoint(plan, record)
                    except (Exception, KeyboardInterrupt) as exc:
                        cleanup_errors.append((plan.ordinal, exc))
                        self._record_failure(record, "endpoint-finally-cleanup", exc)
                        if endpoint_error is None:
                            endpoint_error = ManagedInferenceValidationError(
                                "managed inference endpoint cleanup failed"
                            )
                if endpoint_error is not None:
                    primary_error = endpoint_error
                    break
                record["validation_complete"] = True
                record["phase"] = "complete"
                record["absence_proven"] = True
                self._persist()
                validated += 1 if ran else 0
        finally:
            self.state["phase"] = "final-cleanup"
            self._persist()
            for plan, record in zip(self.plans, self.records, strict=True):
                try:
                    self.cleanup_endpoint(plan, record)
                except (Exception, KeyboardInterrupt) as exc:
                    cleanup_errors.append((plan.ordinal, exc))
                    self._record_failure(record, "aggregate-finally-cleanup", exc)

        if primary_error is not None or cleanup_errors:
            self.state["phase"] = "failed"
            self.state["cleanup_failure_count"] = len(cleanup_errors)
            self._persist()
            if primary_error is not None and not isinstance(primary_error, Exception):
                raise primary_error
            raise ManagedInferenceValidationError(
                "managed inference validation failed after all cleanup attempts; "
                f"cleanup failures: {len(cleanup_errors)}; inspect the private checkpoint"
            ) from None

        self.state["phase"] = "complete"
        self._persist()
        completed = sum(1 for record in self.records if record.get("validation_complete") is True)
        frameworks: dict[str, dict[str, Any]] = {}
        model_registry = {"uploaded": 0, "removed": 0}
        mooncake_store = {"cold_tier_verified": 0, "reconfigure_rolled": 0}
        for plan, record in zip(self.plans, self.records, strict=True):
            framework = frameworks.setdefault(
                plan.runtime.framework,
                {**dict.fromkeys(plan.runtime.roles, False), "invocations": 0, "model_info": 0},
            )
            framework[plan.role] = record.get("validation_complete") is True
            if isinstance(record.get("invoke_evidence"), dict):
                framework["invocations"] += 1
            probes = record.get("backend_probe_evidence")
            if isinstance(probes, dict) and isinstance(probes.get("model_info"), dict):
                framework["model_info"] += 1
            repository = record.get("model_repository")
            if isinstance(repository, dict):
                model_registry["uploaded"] += repository.get("phase") == "uploaded"
                model_registry["removed"] += repository.get("cleanup") == "absent"
            if plan.mooncake_store:
                mooncake_store["cold_tier_verified"] += record.get("cold_tier_verified") is True
                reconfigure = record.get("store_reconfigure")
                mooncake_store["reconfigure_rolled"] += (
                    isinstance(reconfigure, dict) and reconfigure.get("phase") == "verified"
                )
        shared_proxy = self.state.get("shared_proxy_autoscaling")
        return {
            "endpoint_count": len(self.plans),
            "validated_or_resumed": completed,
            "newly_validated": validated,
            "execution": "strictly-sequential",
            "frameworks": frameworks,
            "model_registry": model_registry,
            "mooncake_store": mooncake_store,
            "shared_proxy_autoscaling_verified": (
                isinstance(shared_proxy, dict) and shared_proxy.get("phase") == "verified"
            ),
            "all_endpoints_absent": all(
                record.get("absence_proven") is True for record in self.records
            ),
            "invocations": {
                "required_non_empty_generated_text": True,
                "completed": sum(
                    1 for record in self.records if isinstance(record.get("invoke_evidence"), dict)
                ),
            },
            "hpa": {
                "target_kind": "Deployment",
                "min_replicas": self.settings.hpa_min_replicas,
                "max_replicas": self.settings.hpa_max_replicas,
                "stable_monitor_intervals": self.settings.hpa_stability_intervals,
            },
        }
