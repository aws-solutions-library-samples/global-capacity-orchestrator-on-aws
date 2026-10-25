"""Strict immutable runtime contracts for the live inference matrix.

The matrix proves three serving runtimes end to end: vLLM through its
OpenAI-compatible surface, SGLang through its native surface, and Triton
Inference Server through the KServe v2 protocol with weights served from the
model registry (``gco models upload`` and ``--model-source``). vLLM is also
proven as a Mooncake shared KV-cache store (``--mooncake-mode store`` with the
S3 cold tier), which is the path that exercises the inference monitor's
role-keyed reconciliation, the per-region ``mooncake-master``, the regional
bucket discovery grant, and the rollout that follows ``configure-store``.
Hugging Face Text Generation Inference used to be the second runtime; it
entered maintenance mode on 2025-12-11 and its repository was archived
read-only on 2026-03-21, so it is no longer something a release should be
proven against.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cli._image_reference import immutable_sha256_digest

Framework = Literal["vllm", "sglang", "triton"]
#: One scenario of the matrix. ``baseline`` is a static single replica,
#: ``hpa`` a CPU-autoscaled pair, ``store`` a Mooncake shared KV-cache store
#: with the cold tier (vLLM only).
EndpointRole = Literal["baseline", "hpa", "store"]

INFERENCE_OWNER_LABEL = "gco-managed-inference-validation-owner"
# Bump whenever the matrix below changes shape: the version is part of the
# resume identity, so a checkpoint written under an older contract refuses to
# resume against a newer one instead of silently mixing adapters.
INFERENCE_CONTRACT_VERSION = 5
_FRAMEWORK_ORDER: tuple[Framework, ...] = ("vllm", "sglang", "triton")
#: The scenarios each runtime runs, in order. The classic baseline/HPA pair
#: covers the single-Deployment path; vLLM adds the Mooncake store scenario.
#: Triton runs the baseline only: its HPA path is the same Deployment HPA the
#: vLLM scenario already proves, and a second GPU replica would buy nothing.
_ROLES_BY_FRAMEWORK: dict[Framework, tuple[EndpointRole, ...]] = {
    "vllm": ("baseline", "hpa", "store"),
    "sglang": ("baseline", "hpa"),
    "triton": ("baseline",),
}
# Each runtime's documented default listener: vLLM's OpenAI server,
# ``sglang.launch_server`` (``--port`` default 30000) and Triton's HTTP
# endpoint.
_DEFAULT_PORTS: dict[Framework, int] = {"vllm": 8000, "sglang": 30000, "triton": 8000}
_DEFAULT_REQUEST_PATHS: dict[Framework, str] = {
    "vllm": "/v1/completions",
    "sglang": "/generate",
}
_RESPONSE_CONTRACTS: dict[Framework, str] = {
    "vllm": "choices[0].text:non-empty-string",
    "sglang": "text:non-empty-string",
    "triton": "outputs[0].data:echoes-input",
}
# vLLM lists the served model through the OpenAI inventory; SGLang's
# ``/server_info`` reports the resolved launcher arguments, so it carries
# both ``model_path`` and the ``revision`` the server was started with; Triton
# has no model list, so its statistics extension names every loaded model.
_MODEL_INFO_PATHS: dict[Framework, str] = {
    "vllm": "/v1/models",
    "sglang": "/server_info",
    "triton": "/v2/models/stats",
}
#: Triton reports readiness on its own KServe path; the other runtimes use the
#: run-level ``/health``.
_HEALTH_PATHS: dict[Framework, str] = {"triton": "/v2/health/ready"}

#: vLLM's structured decisions API (v0.32.0+). The vLLM baseline scenario
#: calls it after its completion through the same authenticated path, which
#: proves the inference proxy forwards it and the pinned image serves it.
STRUCTURED_DECISION_PATH = "/v1/systemone"
#: The scenarios that also run the structured decision request. One is enough:
#: the HPA and store scenarios run the same server with the same arguments.
_STRUCTURED_DECISION_ROLES: dict[Framework, tuple[str, ...]] = {"vllm": ("baseline",)}
#: vLLM serves ``/v1/systemone`` for a generation model only when its log
#: probabilities are real probabilities (``label_mass`` sums them), so every
#: vLLM scenario starts the server this way.
STRUCTURED_DECISION_LOGPROBS_MODE = "raw_logprobs"
#: Model ids vLLM v0.32.0 registers a structured decision read strategy for:
#: the Qwen3 / Qwen3.5 families, LiquidAI's d1 and Cloudflare's clef-omni
#: (``READ_STRATEGIES`` in vllm/entrypoints/generate/structured_decisions/
#: strategies.py, keyed by architecture). An id outside them would start
#: normally and then answer ``/v1/systemone`` with "does not support", hours
#: into a run, so the matrix refuses it up front.
_STRUCTURED_DECISION_MODEL_RE = re.compile(r"(?i)(?:^|/)(?:qwen3|d1-|clef-omni)")
#: One question of each type, over a state whose answers are unambiguous, so a
#: served answer is checked for its shape and for the obvious label.
STRUCTURED_DECISION_STATE = (
    "Order 1182 was promised for Monday and shipped nine days late. "
    "The customer has asked for a refund twice."
)
STRUCTURED_DECISION_QUESTIONS: dict[str, dict[str, Any]] = {
    "topic": {
        "type": "choice",
        "instructions": "What is this message mainly about?",
        "criteria": {
            "shipping": "Delivery dates or delays",
            "weather": "The weather forecast",
        },
    },
    "late": {"type": "noul", "instructions": "Did the order ship late?"},
    "urgency": {
        "type": "score",
        "instructions": "How urgent is a reply?",
        "criteria": ["low", "medium", "high"],
    },
}
_STRUCTURED_DECISION_RESPONSE_CONTRACT = (
    "object==structured_decision; answers[topic]=choice in criteria, "
    "answers[late]=noul in [0,1], answers[urgency]=score in [0,2]; "
    "probabilities sum to 1; diagnostics.label_mass in [0,1] per question"
)

#: The Triton model the harness serves. It is a Python-backend identity model
#: generated by the harness itself (see :data:`TRITON_MODEL_REPOSITORY_FILES`),
#: uploaded to the model registry before the scenario and removed after it, so
#: the run needs no third-party weights and the response can be checked
#: exactly: the output tensor must echo the input tensor.
TRITON_MODEL_NAME = "identity"
TRITON_MODEL_VERSION = "1"
TRITON_INPUT_DATA: tuple[float, ...] = (1.0, 2.0, 3.0)
TRITON_MODEL_REPOSITORY_FILES: dict[str, str] = {
    f"{TRITON_MODEL_NAME}/config.pbtxt": (
        f'name: "{TRITON_MODEL_NAME}"\n'
        'backend: "python"\n'
        "max_batch_size: 0\n"
        'input [ { name: "INPUT0" data_type: TYPE_FP32 dims: [ -1 ] } ]\n'
        'output [ { name: "OUTPUT0" data_type: TYPE_FP32 dims: [ -1 ] } ]\n'
        "instance_group [ { kind: KIND_CPU count: 1 } ]\n"
    ),
    f"{TRITON_MODEL_NAME}/{TRITON_MODEL_VERSION}/model.py": (
        "import triton_python_backend_utils as pb_utils\n"
        "\n"
        "\n"
        "class TritonPythonModel:\n"
        "    def execute(self, requests):\n"
        "        responses = []\n"
        "        for request in requests:\n"
        '            tensor = pb_utils.get_input_tensor_by_name(request, "INPUT0")\n'
        '            output = pb_utils.Tensor("OUTPUT0", tensor.as_numpy())\n'
        "            responses.append(pb_utils.InferenceResponse(output_tensors=[output]))\n"
        "        return responses\n"
    ),
}
#: Where the deploy's init container syncs ``--model-source`` to, and
#: therefore the ``--model-repository`` Triton is started with. The last path
#: element is the endpoint name.
TRITON_MODEL_REPOSITORY_MOUNT = "/models"
#: The pod-template annotation the inference monitor stamps with the digest of
#: the rendered ``mooncake.json`` (mirrors
#: ``gco.services.inference_monitor.MOONCAKE_CONFIG_DIGEST_ANNOTATION``; the
#: unit tests pin the two together). A changed digest is how the harness
#: proves a ``configure-store`` rolled the role pods.
MOONCAKE_CONFIG_DIGEST_ANNOTATION = "gco.io/mooncake-config-digest"
#: The byte-size ``configure-store`` writes during the store scenario so the
#: re-rendered ``mooncake.json`` differs from the deploy-time file (the default
#: local buffer is 2 GiB) and the role pods must roll.
MOONCAKE_STORE_RECONFIGURED_LOCAL_BUFFER_BYTES = 1073741824


def triton_model_repository_revision() -> str:
    """Return the content digest of the generated Triton repository, as a 40-hex pin.

    The Triton runtime has no upstream model commit to pin; its model is the
    repository this module generates. Hashing every file path and content gives
    the same immutable-revision shape the Hugging Face runtimes carry, so the
    identity fields detect a changed repository exactly as they detect a
    changed model commit.
    """
    digest = hashlib.sha256()
    for path, content in sorted(TRITON_MODEL_REPOSITORY_FILES.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()[:40]


def write_triton_model_repository(root: Path) -> Path:
    """Materialize the generated Triton model repository under ``root``.

    Returns the repository root, laid out as Triton expects
    (``<model>/config.pbtxt`` and ``<model>/<version>/model.py``) so
    ``gco models upload <root> --name <model>`` publishes a repository the
    endpoint's ``--model-repository`` can serve directly.
    """
    for relative, content in TRITON_MODEL_REPOSITORY_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


@dataclass(frozen=True)
class InferenceRuntimeSpec:
    """One digest-pinned server and immutable model revision.

    For Triton ``model_id`` is the generated repository's model name and
    ``model_revision`` its content digest (:func:`triton_model_repository_revision`).
    """

    framework: Framework
    image: str
    model_id: str
    model_revision: str
    port: int

    @property
    def request_path(self) -> str:
        if self.framework == "triton":
            return f"/v2/models/{self.model_id}/infer"
        return _DEFAULT_REQUEST_PATHS[self.framework]

    @property
    def model_info_path(self) -> str:
        return _MODEL_INFO_PATHS[self.framework]

    @property
    def roles(self) -> tuple[EndpointRole, ...]:
        """The scenarios this runtime runs, in matrix order."""
        return _ROLES_BY_FRAMEWORK[self.framework]

    @property
    def structured_decision_roles(self) -> tuple[str, ...]:
        """The scenarios that also call ``/v1/systemone`` (vLLM baseline only)."""
        return _STRUCTURED_DECISION_ROLES.get(self.framework, ())

    @property
    def uses_model_registry(self) -> bool:
        """Whether the deploy serves weights from the model registry (``--model-source``)."""
        return self.framework == "triton"


def expected_endpoint_count(runtimes: tuple[InferenceRuntimeSpec, ...]) -> int:
    """The number of scenarios the matrix runs for ``runtimes``."""
    return sum(len(runtime.roles) for runtime in runtimes)


def _plain_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_runtime(runtime: InferenceRuntimeSpec) -> None:
    if runtime.framework not in _FRAMEWORK_ORDER:
        raise ValueError("inference runtime framework must be 'vllm', 'sglang' or 'triton'")
    if immutable_sha256_digest(runtime.image) is None:
        raise ValueError(
            f"{runtime.framework} image must be an immutable lowercase @sha256: reference"
        )
    if not runtime.model_id.strip() or runtime.model_id != runtime.model_id.strip():
        raise ValueError(f"{runtime.framework} model_id must be a non-empty trimmed value")
    if not re.fullmatch(r"[0-9a-f]{40}", runtime.model_revision):
        raise ValueError(
            f"{runtime.framework} model_revision must be a full lowercase 40-hex commit"
        )
    if runtime.framework == "triton" and (
        runtime.model_id != TRITON_MODEL_NAME
        or runtime.model_revision != triton_model_repository_revision()
    ):
        raise ValueError(
            "triton model identity must be the harness-generated repository "
            f"({TRITON_MODEL_NAME} at revision {triton_model_repository_revision()})"
        )
    if runtime.port != _DEFAULT_PORTS[runtime.framework]:
        raise ValueError(
            f"{runtime.framework} live validation port must be {_DEFAULT_PORTS[runtime.framework]}"
        )
    if runtime.structured_decision_roles and not _STRUCTURED_DECISION_MODEL_RE.search(
        runtime.model_id
    ):
        raise ValueError(
            f"{runtime.framework} model_id must be a model vLLM serves /v1/systemone for "
            "(a Qwen3 or Qwen3.5 model, LiquidAI/d1-*, or Cloudflare/clef-omni)"
        )


def validate_inference_settings(settings: Any) -> None:
    """Validate every run-level and per-runtime inference input."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", settings.selected_region):
        raise ValueError("selected_region must be a lowercase AWS Region name")
    runtimes = settings.inference_runtimes
    if (
        not isinstance(runtimes, tuple)
        or tuple(runtime.framework for runtime in runtimes) != _FRAMEWORK_ORDER
    ):
        raise ValueError(
            "managed inference validation requires vLLM, SGLang then Triton runtime specs"
        )
    for runtime in runtimes:
        _validate_runtime(runtime)
    image_digests = [runtime.image.rsplit("@sha256:", 1)[1] for runtime in runtimes]
    if len(set(image_digests)) != len(image_digests):
        raise ValueError("every runtime image must have a distinct immutable digest")
    if not settings.request_prompt.strip():
        raise ValueError("request_prompt must be non-empty")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", settings.namespace):
        raise ValueError("namespace must be a DNS-safe Kubernetes name")
    if (
        not isinstance(settings.gpu_count, int)
        or isinstance(settings.gpu_count, bool)
        or settings.gpu_count < 0
    ):
        raise ValueError("gpu_count must be a non-negative integer")
    for field_name in (
        "request_max_tokens",
        "baseline_replicas",
        "autoscale_initial_replicas",
        "hpa_min_replicas",
        "hpa_max_replicas",
        "command_timeout_seconds",
        "readiness_timeout_seconds",
        "hpa_timeout_seconds",
        "deletion_timeout_seconds",
        "monitor_interval_seconds",
        "hpa_stability_intervals",
        "job_timeout_seconds",
        "queue_timeout_seconds",
        "poll_interval_seconds",
        "destroy_attempts",
        "destroy_retry_delay_seconds",
    ):
        if not _plain_positive_int(getattr(settings, field_name)):
            raise ValueError(f"{field_name} must be a positive integer")
    expected_count = expected_endpoint_count(runtimes)
    if settings.endpoint_count != expected_count:
        raise ValueError(
            f"managed inference validation requires exactly {expected_count} endpoints "
            "(every scenario of every runtime)"
        )
    if settings.baseline_replicas != 1 or settings.autoscale_initial_replicas != 1:
        raise ValueError("all managed inference endpoints must start with one replica")
    if settings.hpa_min_replicas != 2 or settings.hpa_max_replicas != 2:
        raise ValueError("the CPU HPA validation requires min_replicas=max_replicas=2")
    if not isinstance(settings.hpa_cpu_target, int) or isinstance(settings.hpa_cpu_target, bool):
        raise ValueError("hpa_cpu_target must be an integer")
    if not 1 <= settings.hpa_cpu_target <= 100:
        raise ValueError("hpa_cpu_target must be from 1 through 100")
    if settings.hpa_stability_intervals < 2:
        raise ValueError("hpa_stability_intervals must be at least two")
    if settings.health_path != "/health":
        raise ValueError("managed inference validation requires the official /health path")
    if not re.fullmatch(r"[1-9][0-9]*m", settings.proxy_tls_cpu_request):
        raise ValueError("proxy_tls_cpu_request must be a positive millicore quantity")
    if (
        not isinstance(settings.proxy_tls_cpu_target, int)
        or isinstance(settings.proxy_tls_cpu_target, bool)
        or not 1 <= settings.proxy_tls_cpu_target <= 100
    ):
        raise ValueError("proxy_tls_cpu_target must be an integer from 1 through 100")
    if not settings.consent:
        raise ValueError("explicit managed inference deployment/destruction consent is required")


def inference_request_body(settings: Any, runtime: InferenceRuntimeSpec) -> dict[str, Any]:
    """Return the deterministic request body for one exact framework adapter.

    SGLang is driven through its native ``/generate`` API rather than its
    OpenAI-compatible one on purpose: the two runtimes must exercise
    different wire contracts, otherwise a response that only satisfies the
    OpenAI shape would pass for both and the matrix would prove one adapter
    twice.
    """
    if runtime.framework == "sglang":
        return {
            "sampling_params": {
                "max_new_tokens": settings.request_max_tokens,
                "temperature": 0,
            },
            "text": settings.request_prompt,
        }
    if runtime.framework == "triton":
        # KServe v2: one FP32 input tensor the identity model echoes back, so
        # the response is checked for equality rather than for non-emptiness.
        return {
            "inputs": [
                {
                    "name": "INPUT0",
                    "shape": [len(TRITON_INPUT_DATA)],
                    "datatype": "FP32",
                    "data": list(TRITON_INPUT_DATA),
                }
            ]
        }
    return {
        "max_tokens": settings.request_max_tokens,
        "model": runtime.model_id,
        "prompt": settings.request_prompt,
        "stream": False,
        "temperature": 0,
    }


def inference_framework_env(runtime: InferenceRuntimeSpec) -> dict[str, str]:
    """Return the environment the deploy carries for the selected runtime.

    ``MODEL`` is the GCO convention every renderer and ``gco inference invoke``
    understand; both Hugging Face launchers receive the model itself on argv
    (see :func:`inference_deploy_extra_args`). Triton serves whatever the
    synced model repository holds and takes no environment.
    """
    if runtime.framework == "triton":
        return {}
    return {"MODEL": runtime.model_id}


def inference_health_path(settings: Any, runtime: InferenceRuntimeSpec) -> str:
    """The readiness path the deploy probes and ``gco inference health`` reads."""
    return _HEALTH_PATHS.get(runtime.framework, str(settings.health_path))


def inference_model_repository_path(endpoint_name: str) -> str:
    """Where the init container lands ``--model-source`` for ``endpoint_name``."""
    return f"{TRITON_MODEL_REPOSITORY_MOUNT}/{endpoint_name}"


def inference_deploy_extra_args(
    runtime: InferenceRuntimeSpec, *, endpoint_name: str = "<endpoint>"
) -> tuple[str, ...]:
    """Return the official launcher arguments for the selected runtime.

    The Hugging Face runtimes receive their immutable model on argv. Triton's
    image has no default command, so the deploy passes ``tritonserver`` with
    the model repository the init container synced for ``endpoint_name``
    (the placeholder stands in when only the shape is being recorded).
    """
    if runtime.framework == "sglang":
        return (
            "--model-path",
            runtime.model_id,
            "--revision",
            runtime.model_revision,
        )
    if runtime.framework == "triton":
        return (
            "tritonserver",
            f"--model-repository={inference_model_repository_path(endpoint_name)}",
        )
    return (
        "--model",
        runtime.model_id,
        "--revision",
        runtime.model_revision,
        "--logprobs-mode",
        STRUCTURED_DECISION_LOGPROBS_MODE,
    )


def inference_structured_decision_body(runtime: InferenceRuntimeSpec) -> dict[str, Any]:
    """The ``/v1/systemone`` request the vLLM baseline scenario sends after its completion."""
    return {
        "model": runtime.model_id,
        "state": STRUCTURED_DECISION_STATE,
        "questions": copy.deepcopy(STRUCTURED_DECISION_QUESTIONS),
    }


def inference_mooncake_block(
    role: EndpointRole, *, reconfigured: bool = False
) -> dict[str, Any] | None:
    """The ``spec.mooncake`` block the store scenario persists, or ``None`` for others.

    ``reconfigured`` adds the ``local_buffer_size`` the scenario writes with
    ``configure-store`` after its first invocation.
    """
    if role != "store":
        return None
    store: dict[str, Any] = {"enabled": True, "cold_tier_enabled": True}
    if reconfigured:
        store["local_buffer_size"] = str(MOONCAKE_STORE_RECONFIGURED_LOCAL_BUFFER_BYTES)
    return {"mode": "store", "store": store, "transfer": {"protocol": "tcp"}}


def _runtime_identity(settings: Any, runtime: InferenceRuntimeSpec) -> dict[str, Any]:
    return {
        "framework": runtime.framework,
        "image": runtime.image,
        "model": {"id": runtime.model_id, "revision": runtime.model_revision},
        "roles": list(runtime.roles),
        "server": {
            "port": runtime.port,
            "health_path": inference_health_path(settings, runtime),
            "model_source": "model-registry" if runtime.uses_model_registry else "launcher",
        },
        "request_contract": {
            "path": runtime.request_path,
            "body": inference_request_body(settings, runtime),
            "response": _RESPONSE_CONTRACTS[runtime.framework],
        },
        "probe_contract": {
            "health_path": inference_health_path(settings, runtime),
            "model_info_path": runtime.model_info_path,
            "expected_model_id": runtime.model_id,
            "expected_model_revision": runtime.model_revision,
        },
        "deploy_contract": {
            "framework_env": inference_framework_env(runtime),
            "extra_args": list(inference_deploy_extra_args(runtime)),
        },
        "structured_decision_contract": (
            {
                "path": STRUCTURED_DECISION_PATH,
                "roles": list(runtime.structured_decision_roles),
                "body": inference_structured_decision_body(runtime),
                "response": _STRUCTURED_DECISION_RESPONSE_CONTRACT,
            }
            if runtime.structured_decision_roles
            else None
        ),
    }


def inference_identity_fields(settings: Any) -> dict[str, Any]:
    """Return every inference input that must match on resume."""
    return {
        "contract_version": INFERENCE_CONTRACT_VERSION,
        "selected_region": settings.selected_region,
        "runtimes": [
            _runtime_identity(settings, runtime) for runtime in settings.inference_runtimes
        ],
        "endpoint_contract": {
            "count": settings.endpoint_count,
            "namespace": settings.namespace,
            "gpu_count": settings.gpu_count,
            "accelerator": "nvidia",
            "rewrite_image": False,
            "roles_per_runtime": {
                runtime.framework: list(runtime.roles) for runtime in settings.inference_runtimes
            },
            "mooncake_store": {
                "mode": "store",
                "protocol": "tcp",
                "cold_tier": True,
                "reconfigured_local_buffer_size": MOONCAKE_STORE_RECONFIGURED_LOCAL_BUFFER_BYTES,
            },
            "baseline_replicas": settings.baseline_replicas,
            "autoscale_initial_replicas": settings.autoscale_initial_replicas,
            "hpa": {
                "metric": "cpu",
                "target": settings.hpa_cpu_target,
                "min_replicas": settings.hpa_min_replicas,
                "max_replicas": settings.hpa_max_replicas,
            },
        },
        "shared_proxy_contract": {
            "namespace": "gco-system",
            "deployment": "inference-proxy",
            "hpa": "inference-proxy-hpa",
            "tls_container": "api-tls-proxy",
            "tls_cpu_request": settings.proxy_tls_cpu_request,
            "tls_cpu_target": settings.proxy_tls_cpu_target,
            "metric_type": "ContainerResource",
        },
        "timeouts": {
            "command_seconds": settings.command_timeout_seconds,
            "readiness_seconds": settings.readiness_timeout_seconds,
            "hpa_seconds": settings.hpa_timeout_seconds,
            "deletion_seconds": settings.deletion_timeout_seconds,
            "poll_seconds": settings.poll_interval_seconds,
            "monitor_interval_seconds": settings.monitor_interval_seconds,
            "hpa_stability_intervals": settings.hpa_stability_intervals,
            "job_seconds": settings.job_timeout_seconds,
            "queue_seconds": settings.queue_timeout_seconds,
            "destroy_attempts": settings.destroy_attempts,
            "destroy_retry_delay_seconds": settings.destroy_retry_delay_seconds,
        },
        "consent": settings.consent,
    }
