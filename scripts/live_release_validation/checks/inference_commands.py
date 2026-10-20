"""Plans and argv-only ``gco`` commands for the live inference matrix.

Everything here is pure: the scenario plan derived from the runtime matrix,
the exact command arrays the lifecycle runs (deploy, probes, invoke, delete,
the model-registry upload/lookup/removal the Triton scenario needs, and the
``configure-store`` the Mooncake store scenario applies), and the response
parsing that holds each runtime to its own schema. The lifecycle in
``inference.py`` executes them; nothing here touches a subprocess.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import (
    INFERENCE_OWNER_LABEL,
    MOONCAKE_STORE_RECONFIGURED_LOCAL_BUFFER_BYTES,
    TRITON_INPUT_DATA,
    EndpointRole,
    InferenceRuntimeSpec,
    RunSettings,
)
from .inference_common import ManagedInferenceValidationError

OWNER_LABEL = INFERENCE_OWNER_LABEL


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
