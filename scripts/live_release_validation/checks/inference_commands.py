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
    STRUCTURED_DECISION_PATH,
    STRUCTURED_DECISION_QUESTIONS,
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
    def structured_decision(self) -> bool:
        """Whether this scenario also calls vLLM's ``/v1/systemone``."""
        return self.role in self.runtime.structured_decision_roles

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


def build_structured_decision_command(
    settings: RunSettings,
    plan: EndpointPlan,
) -> list[str]:
    """Build the buffered ``/v1/systemone`` call through the same authenticated path."""
    return [
        *_checkout_cli_prefix(),
        "--output",
        "json",
        "inference",
        "invoke",
        plan.name,
        "--data",
        json.dumps(
            settings.structured_decision_body(plan.runtime),
            sort_keys=True,
            separators=(",", ":"),
        ),
        "--path",
        STRUCTURED_DECISION_PATH,
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


def _probability(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if 0.0 <= number <= 1.0 else None


def extract_structured_decision(output: str) -> dict[str, Any]:
    """Require vLLM's typed ``/v1/systemone`` answer for every question asked.

    Each answer must carry the type it was asked as, a value of that type in
    range, label probabilities over exactly the asked labels that sum to one,
    and a ``confidence``; every question needs a ``label_mass`` diagnostic. The
    state is written so the answers are unambiguous, so the chosen ``topic``
    and the ``late`` probability are checked too. Returns sanitized evidence.
    """
    payload = _last_json_document(output)

    def fail(reason: str) -> ManagedInferenceValidationError:
        return ManagedInferenceValidationError(
            f"managed vLLM structured decision response did not match its schema: {reason}"
        )

    if not isinstance(payload, dict) or payload.get("object") != "structured_decision":
        raise fail("object is not structured_decision")
    answers = payload.get("answers")
    diagnostics = payload.get("diagnostics")
    if not isinstance(answers, dict) or set(answers) != set(STRUCTURED_DECISION_QUESTIONS):
        raise fail("answers do not cover exactly the asked questions")
    if not isinstance(diagnostics, dict) or set(diagnostics) != set(STRUCTURED_DECISION_QUESTIONS):
        raise fail("diagnostics do not cover exactly the asked questions")
    label_mass: dict[str, float] = {}
    for name, question in STRUCTURED_DECISION_QUESTIONS.items():
        answer = answers[name]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise fail(f"{name} is not a {kind} answer")
        probabilities = answer.get("probabilities")
        if kind == "choice":
            labels = set(question["criteria"])
        elif kind == "noul":
            labels = {"yes", "no"}
        else:
            labels = {str(index) for index in range(len(question["criteria"]))}
        if not isinstance(probabilities, dict) or set(probabilities) != labels:
            raise fail(f"{name} probabilities do not cover exactly its labels")
        values = [_probability(value) for value in probabilities.values()]
        if any(value is None for value in values) or abs(sum(values) - 1.0) > 1e-3:  # type: ignore[arg-type]
            raise fail(f"{name} probabilities are not a distribution")
        if _probability(answer.get("confidence")) is None:
            raise fail(f"{name} confidence is not a probability")
        if kind == "choice" and answer.get("choice") not in labels:
            raise fail(f"{name} choice is not one of its options")
        if kind == "noul" and _probability(answer.get("noul")) is None:
            raise fail(f"{name} noul is not a probability")
        if kind == "score":
            score = answer.get("score")
            top = len(question["criteria"]) - 1
            if (
                isinstance(score, bool)
                or not isinstance(score, int | float)
                or not 0 <= score <= top
            ):
                raise fail(f"{name} score is outside its scale")
        diagnostic = diagnostics[name]
        mass = _probability(diagnostic.get("label_mass")) if isinstance(diagnostic, dict) else None
        if mass is None:
            raise fail(f"{name} label_mass is not a probability")
        label_mass[name] = mass
    if answers["topic"].get("choice") != "shipping":
        raise fail("topic was not answered as shipping")
    if float(answers["late"]["noul"]) <= 0.5:
        raise fail("late was not answered yes")
    return {
        "object": "structured_decision",
        "questions": len(answers),
        "types": sorted(question["type"] for question in STRUCTURED_DECISION_QUESTIONS.values()),
        "topic": answers["topic"]["choice"],
        "late_probability": round(float(answers["late"]["noul"]), 4),
        "urgency_score": round(float(answers["urgency"]["score"]), 4),
        "min_label_mass": round(min(label_mass.values()), 4),
    }
