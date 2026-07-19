"""Shared Bedrock defaults loaded from the canonical ``cdk.json`` context."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

_BEDROCK_CONTEXT_KEY = "bedrock"
_DEFAULT_MODEL_ID_KEY = "default_model_id"
_THINKING_KEY = "thinking"
_THINKING_EFFORT_KEY = "effort"
_SUPPORTED_THINKING_EFFORTS = frozenset({"low", "medium", "high"})
_NOVA_2_MODEL_ID_RE = re.compile(r"(?:^|/)(?:[a-z0-9-]+\.)?amazon\.nova-2-[a-z0-9-]+-v\d+:\d+$")
_HIGH_REASONING_INCOMPATIBLE_FIELDS = frozenset({"maxTokens", "temperature", "topP"})
BEDROCK_READ_TIMEOUT_SECONDS = 3600
_DISTRIBUTION_NAME = "gco-cli"
_SOURCE_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_CDK_JSON = _SOURCE_ROOT / "cdk.json"
_SOURCE_CHECKOUT_MARKERS = (_SOURCE_ROOT / "app.py", _SOURCE_ROOT / "pyproject.toml")
_INSTALLED_DATA_PARTS = ("share", "gco", "cdk.json")

if TYPE_CHECKING:
    # Runtime access is provided lazily by ``__getattr__`` below.
    DEFAULT_BEDROCK_MODEL_ID: str


class BedrockModelConfigurationError(RuntimeError):
    """The shared Bedrock model default could not be resolved safely."""


@dataclass(frozen=True)
class BedrockDefaultConfiguration:
    """Validated canonical Bedrock model and reasoning preferences."""

    model_id: str
    thinking_effort: str


def _source_cdk_json_path() -> Path | None:
    """Return the checkout-owned config path, never an ambient ancestor file."""
    if all(marker.is_file() for marker in _SOURCE_CHECKOUT_MARKERS):
        # Return the expected path even when it is missing so selection is
        # fail-closed instead of falling through to an older installed copy.
        return _SOURCE_CDK_JSON
    return None


def _installed_cdk_json_path() -> Path | None:
    """Locate installed data through this distribution's recorded file list.

    ``setuptools`` data files follow the installer's selected scheme, which may
    differ from the interpreter's default ``sysconfig`` scheme for ``--user``,
    ``--prefix``, ``--target``, pipx, or uvx installs. Distribution metadata
    records the actual relocated path and therefore remains authoritative.
    """
    try:
        distribution = metadata.distribution(_DISTRIBUTION_NAME)
        files = distribution.files or ()
        for relative_path in files:
            if tuple(relative_path.parts[-3:]) == _INSTALLED_DATA_PARTS:
                return Path(str(distribution.locate_file(relative_path))).resolve()
    except metadata.PackageNotFoundError:
        return None
    except Exception as exc:
        raise BedrockModelConfigurationError(
            f"Unable to inspect installed {_DISTRIBUTION_NAME} package data: {exc}"
        ) from exc
    return None


def _canonical_cdk_json_path() -> Path:
    """Select exactly one checkout-owned or distribution-owned config file."""
    source_path = _source_cdk_json_path()
    if source_path is not None:
        return source_path.resolve()

    installed_path = _installed_cdk_json_path()
    if installed_path is not None:
        return installed_path

    raise BedrockModelConfigurationError(
        "Could not locate canonical cdk.json in a GCO source checkout or the "
        f"installed {_DISTRIBUTION_NAME} distribution"
    )


def _bedrock_configuration_from_payload(
    payload: Any,
    path: Path,
) -> BedrockDefaultConfiguration:
    """Extract and strictly validate the canonical Bedrock configuration."""
    if not isinstance(payload, dict):
        raise BedrockModelConfigurationError(f"{path}: document root must be an object")

    context = payload.get("context")
    if not isinstance(context, dict):
        raise BedrockModelConfigurationError(f"{path}: context must be an object")

    bedrock = context.get(_BEDROCK_CONTEXT_KEY)
    if not isinstance(bedrock, dict):
        raise BedrockModelConfigurationError(
            f"{path}: context.{_BEDROCK_CONTEXT_KEY} must be an object"
        )

    model_id = bedrock.get(_DEFAULT_MODEL_ID_KEY)
    if not isinstance(model_id, str) or not model_id.strip():
        raise BedrockModelConfigurationError(
            f"{path}: context.{_BEDROCK_CONTEXT_KEY}.{_DEFAULT_MODEL_ID_KEY} "
            "must be a non-empty string"
        )

    thinking = bedrock.get(_THINKING_KEY)
    thinking_path = f"context.{_BEDROCK_CONTEXT_KEY}.{_THINKING_KEY}"
    if not isinstance(thinking, dict):
        raise BedrockModelConfigurationError(f"{path}: {thinking_path} must be an object")
    if set(thinking) != {_THINKING_EFFORT_KEY}:
        raise BedrockModelConfigurationError(
            f"{path}: {thinking_path} must contain only {_THINKING_EFFORT_KEY!r}"
        )

    effort = thinking.get(_THINKING_EFFORT_KEY)
    if not isinstance(effort, str) or effort not in _SUPPORTED_THINKING_EFFORTS:
        supported = ", ".join(sorted(_SUPPORTED_THINKING_EFFORTS))
        raise BedrockModelConfigurationError(
            f"{path}: {thinking_path}.{_THINKING_EFFORT_KEY} must be one of {supported}"
        )

    return BedrockDefaultConfiguration(
        model_id=model_id.strip(),
        thinking_effort=effort,
    )


def _model_id_from_payload(payload: Any, path: Path) -> str:
    """Extract the model id through the full canonical validation contract."""
    return _bedrock_configuration_from_payload(payload, path).model_id


def get_default_bedrock_configuration(
    cdk_json_path: Path | None = None,
) -> BedrockDefaultConfiguration:
    """Return the validated canonical Bedrock configuration from ``cdk.json``.

    An explicit path is strict. Without one, resolution uses only the config
    owned by this GCO source checkout or the config recorded in the installed
    ``gco-cli`` distribution. Current-working-directory and ancestor files are
    deliberately ignored so an unrelated project cannot change model routing.
    Once selected, a missing, unreadable, malformed, or incomplete canonical
    file fails closed rather than falling through to a stale copy.
    """
    path = cdk_json_path.resolve() if cdk_json_path is not None else _canonical_cdk_json_path()
    if not path.is_file():
        raise BedrockModelConfigurationError(f"Canonical Bedrock config is not a file: {path}")

    try:
        raw_payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BedrockModelConfigurationError(f"Unable to read {path}: {exc}") from exc

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise BedrockModelConfigurationError(f"Invalid JSON in {path}: {exc}") from exc

    return _bedrock_configuration_from_payload(payload, path)


def get_default_bedrock_model_id(cdk_json_path: Path | None = None) -> str:
    """Return the sole checked-in Bedrock model default from ``cdk.json``."""
    return get_default_bedrock_configuration(cdk_json_path).model_id


def get_default_bedrock_thinking_effort(cdk_json_path: Path | None = None) -> str:
    """Return the canonical default model's validated reasoning effort."""
    return get_default_bedrock_configuration(cdk_json_path).thinking_effort


def _supports_nova_2_reasoning(model_id: str) -> bool:
    """Return whether a model/profile identifier accepts Nova 2 reasoningConfig."""
    return _NOVA_2_MODEL_ID_RE.search(model_id) is not None


def build_bedrock_converse_options(
    model_id: str,
    *,
    inference_config: Mapping[str, Any] | None = None,
    cdk_json_path: Path | None = None,
    apply_default_reasoning: bool | None = None,
) -> dict[str, Any]:
    """Build model-safe optional kwargs for ``bedrock-runtime:Converse``.

    Canonical reasoning preferences apply only when ``model_id`` is the
    configured default. Explicit third-party or other-model overrides retain
    their caller-provided inference controls and never receive Nova-specific
    ``reasoningConfig`` fields. Callers that know whether the model was
    defaulted should pass ``apply_default_reasoning``; an explicit override
    then remains independent of canonical configuration even when its model ID
    happens to match the default. With no provenance flag, model-ID equality
    preserves the compatibility behavior. Nova 2 high reasoning rejects
    ``maxTokens``, ``temperature``, and ``topP``, so those fields are omitted in
    that mode.
    """
    resolved_inference = dict(inference_config or {})
    if apply_default_reasoning is False:
        return {"inferenceConfig": resolved_inference} if resolved_inference else {}
    if not _supports_nova_2_reasoning(model_id):
        return {"inferenceConfig": resolved_inference} if resolved_inference else {}

    configuration = get_default_bedrock_configuration(cdk_json_path)

    if model_id != configuration.model_id:
        if apply_default_reasoning is True:
            raise BedrockModelConfigurationError(
                "Default Bedrock model changed while building its Converse request"
            )
        return {"inferenceConfig": resolved_inference} if resolved_inference else {}

    if configuration.thinking_effort == "high":
        resolved_inference = {
            key: value
            for key, value in resolved_inference.items()
            if key not in _HIGH_REASONING_INCOMPATIBLE_FIELDS
        }

    options: dict[str, Any] = {}
    if resolved_inference:
        options["inferenceConfig"] = resolved_inference
    options["additionalModelRequestFields"] = {
        "reasoningConfig": {
            "type": "enabled",
            "maxReasoningEffort": configuration.thinking_effort,
        }
    }
    return options


def extract_bedrock_converse_text(response: Mapping[str, Any]) -> str:
    """Return the first non-empty text block, skipping reasoning content."""
    content = response["output"]["message"]["content"]
    if not isinstance(content, list):
        raise TypeError("Bedrock response content must be a list")

    for block in content:
        if not isinstance(block, Mapping):
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            return text

    raise IndexError("Bedrock response contains no non-empty text block")


def __getattr__(name: str) -> Any:
    """Resolve the historical module constant only when it is requested."""
    if name == "DEFAULT_BEDROCK_MODEL_ID":
        return get_default_bedrock_model_id()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Advertise the lazy compatibility alias to introspection tools."""
    return sorted({*globals(), "DEFAULT_BEDROCK_MODEL_ID"})


__all__ = [
    "DEFAULT_BEDROCK_MODEL_ID",
    "BEDROCK_READ_TIMEOUT_SECONDS",
    "BedrockDefaultConfiguration",
    "BedrockModelConfigurationError",
    "build_bedrock_converse_options",
    "extract_bedrock_converse_text",
    "get_default_bedrock_configuration",
    "get_default_bedrock_model_id",
    "get_default_bedrock_thinking_effort",
]
