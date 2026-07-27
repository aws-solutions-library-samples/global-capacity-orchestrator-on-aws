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
# Effort levels accepted in ``cdk.json``. This is deliberately the
# *intersection* of what the supported reasoning dialects accept: Nova 2 tops
# out at ``high``, and while Claude Opus 4.6/5 also accept ``xhigh`` and
# ``max``, allowing them here would let a config value that is valid for one
# default model become a hard ValidationException the moment the default moves
# to another family.
_SUPPORTED_THINKING_EFFORTS = frozenset({"low", "medium", "high"})
_NOVA_2_MODEL_ID_RE = re.compile(r"(?:^|/)(?:[a-z0-9-]+\.)?amazon\.nova-2-[a-z0-9-]+-v\d+:\d+$")
# Nova 2 rejects these three at ``high`` effort only (lower efforts keep them).
_NOVA_HIGH_EFFORT_UNSUPPORTED_FIELDS = frozenset({"maxTokens", "temperature", "topP"})
# Strip the geography scope from a system-defined inference-profile id so the
# adaptive-thinking allowlist below is written once per model line rather than
# once per (geography, model) pair.
_INFERENCE_PROFILE_GEO_PREFIX_RE = re.compile(r"^(?:global|us|us-gov|eu|apac|jp|au|ca|sa|il|mx)\.")
# Claude model lines that accept ``thinking.type = "adaptive"``. Enumerated
# rather than pattern-matched because the distinction is not inferable from the
# id: Opus/Sonnet 4.6+ and the Mythos/Fable lines take adaptive thinking, while
# older Claude models (Sonnet 4.5, Opus 4.5, ...) require the legacy
# ``enabled`` + ``budget_tokens`` form and reject ``adaptive`` outright. An
# unlisted model therefore falls through to "no reasoning translation", which
# is the safe default rather than a guessed request shape.
# Source: https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html
_CLAUDE_ADAPTIVE_THINKING_MODELS = frozenset(
    {
        "anthropic.claude-opus-5",
        "anthropic.claude-mythos-5",
        "anthropic.claude-fable-5",
        "anthropic.claude-opus-4-7",
        "anthropic.claude-mythos-preview",
        "anthropic.claude-opus-4-6-v1",
        "anthropic.claude-sonnet-4-6",
    }
)
# Claude dropped sampling controls starting with Opus 4.7 ("temperature,
# top_p, and top_k parameters are no longer supported"); verified live against
# the Opus 5 global profile, which answers a ValidationException for each.
# ``maxTokens`` is still required and stays.
_CLAUDE_UNSUPPORTED_SAMPLING_FIELDS = frozenset({"temperature", "topP", "topK"})
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


def _supports_claude_adaptive_thinking(model_id: str) -> bool:
    """Return whether the identifier names a Claude line taking adaptive thinking."""
    base = _INFERENCE_PROFILE_GEO_PREFIX_RE.sub("", model_id.rsplit("/", 1)[-1])
    return base in _CLAUDE_ADAPTIVE_THINKING_MODELS


def _nova_reasoning_options(
    inference_config: dict[str, Any],
    effort: str,
) -> dict[str, Any]:
    """Translate the canonical effort into Nova 2 ``reasoningConfig`` fields."""
    resolved = inference_config
    if effort == "high":
        resolved = {
            key: value
            for key, value in resolved.items()
            if key not in _NOVA_HIGH_EFFORT_UNSUPPORTED_FIELDS
        }
    options: dict[str, Any] = {}
    if resolved:
        options["inferenceConfig"] = resolved
    options["additionalModelRequestFields"] = {
        "reasoningConfig": {"type": "enabled", "maxReasoningEffort": effort}
    }
    return options


def _claude_reasoning_options(
    inference_config: dict[str, Any],
    effort: str,
) -> dict[str, Any]:
    """Translate the canonical effort into Claude adaptive-thinking fields.

    ``effort`` must ride in its own ``output_config`` object; Bedrock answers a
    ValidationException when it is nested inside ``thinking``. Unsupported
    sampling controls are dropped at every effort level because their removal
    is a model-wide change, not an effort-dependent one.
    """
    resolved = {
        key: value
        for key, value in inference_config.items()
        if key not in _CLAUDE_UNSUPPORTED_SAMPLING_FIELDS
    }
    options: dict[str, Any] = {}
    if resolved:
        options["inferenceConfig"] = resolved
    options["additionalModelRequestFields"] = {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }
    return options


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
    their caller-provided inference controls and never receive model-specific
    reasoning fields. Callers that know whether the model was defaulted should
    pass ``apply_default_reasoning``; an explicit override then remains
    independent of canonical configuration even when its model ID happens to
    match the default. With no provenance flag, model-ID equality preserves the
    compatibility behavior.

    Two reasoning dialects are translated, selected from the model id:

    * Claude adaptive thinking — ``thinking.type = "adaptive"`` plus the effort
      in its own ``output_config`` object. ``temperature``, ``topP``, and
      ``topK`` are dropped because Claude removed them from Opus 4.7 onward.
    * Nova 2 ``reasoningConfig`` — ``maxReasoningEffort``, with ``maxTokens``,
      ``temperature``, and ``topP`` dropped at ``high`` effort only.

    A default model in neither dialect keeps its caller-supplied inference
    controls and receives no reasoning fields.
    """
    resolved_inference = dict(inference_config or {})
    inference_only = {"inferenceConfig": resolved_inference} if resolved_inference else {}
    if apply_default_reasoning is False:
        return inference_only

    if _supports_claude_adaptive_thinking(model_id):
        translate = _claude_reasoning_options
    elif _supports_nova_2_reasoning(model_id):
        translate = _nova_reasoning_options
    else:
        return inference_only

    configuration = get_default_bedrock_configuration(cdk_json_path)

    if model_id != configuration.model_id:
        if apply_default_reasoning is True:
            raise BedrockModelConfigurationError(
                "Default Bedrock model changed while building its Converse request"
            )
        return inference_only

    return translate(resolved_inference, configuration.thinking_effort)


#: Bedrock error code returned (with HTTP 404) when the account has never
#: submitted the Anthropic first-time-use case form. Anthropic models are gated
#: behind it; first-party models are not.
BEDROCK_FTU_FORM_ERROR_CODE = "FTUFormNotFilled"

#: Remediation shown when an Anthropic model is invoked before the one-time
#: use-case form has been submitted. Deliberately names both paths: the console
#: is the usual route, the API is what automation needs.
BEDROCK_FTU_REMEDIATION = (
    "Amazon Bedrock rejected the request because this AWS account has not "
    "submitted Anthropic's one-time first-time-use (FTU) case form, which is "
    "required before any Anthropic model can be invoked.\n"
    "Submit it once per account (or organization) either way:\n"
    "  - Console: Amazon Bedrock > Model access > request access to the "
    "Anthropic model and complete the use case details form.\n"
    "  - CLI: aws bedrock put-use-case-for-model-access "
    "--form-data <base64-encoded-json>\n"
    "See https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html "
    "for the form fields. Alternatively, point GCO at a model that needs no FTU "
    "form (for example an Amazon Nova profile) with --model, "
    "GCO_MISSION_BEDROCK_MODEL_ID, or cdk.json context.bedrock.default_model_id."
)


class BedrockFTUFormNotAcceptedError(RuntimeError):
    """Anthropic's one-time first-time-use case form has not been submitted.

    Deliberately **not** a transport error. Every advisory Bedrock path in GCO
    degrades gracefully when a model is briefly unreachable — throttling, a
    dropped connection, a malformed response — because retrying or falling back
    to deterministic templates is the right answer for a transient fault. A
    missing FTU form is the opposite: it is a permanent, account-scoped
    misconfiguration that fails every subsequent call identically, so a silent
    fallback would quietly downgrade an entire Mission run (or hand back a
    template-derived answer) while hiding a one-line fix. This type therefore
    propagates through the fallback handlers and surfaces the remediation.

    It subclasses ``RuntimeError`` so existing callers that catch ``RuntimeError``
    around the capacity advisor keep working.
    """

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or BEDROCK_FTU_REMEDIATION)


def raise_if_bedrock_ftu_form_error(error: BaseException) -> None:
    """Convert an FTU-gated Bedrock failure into a hard, actionable error.

    Call this at the top of a ``ClientError`` handler that would otherwise
    degrade gracefully, so the FTU case is escalated instead of absorbed.
    Non-FTU errors return without raising, leaving the caller's own handling
    untouched.
    """
    if is_bedrock_ftu_form_error(error):
        raise BedrockFTUFormNotAcceptedError() from error


def is_bedrock_ftu_form_error(error: BaseException | None) -> bool:
    """Return whether ``error`` (or anything it was raised from) is the FTU gate.

    The exception chain is walked because the capacity advisor re-raises the
    underlying ``ClientError`` as a ``RuntimeError``, so the CLI layer only ever
    sees the original code through ``__cause__``.
    """
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        if isinstance(response, Mapping):
            error_block = response.get("Error")
            if (
                isinstance(error_block, Mapping)
                and error_block.get("Code") == BEDROCK_FTU_FORM_ERROR_CODE
            ):
                return True
        if BEDROCK_FTU_FORM_ERROR_CODE in str(current):
            return True
        current = current.__cause__
    return False


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
    "BEDROCK_FTU_FORM_ERROR_CODE",
    "BEDROCK_FTU_REMEDIATION",
    "BEDROCK_READ_TIMEOUT_SECONDS",
    "BedrockDefaultConfiguration",
    "BedrockFTUFormNotAcceptedError",
    "BedrockModelConfigurationError",
    "build_bedrock_converse_options",
    "extract_bedrock_converse_text",
    "get_default_bedrock_configuration",
    "get_default_bedrock_model_id",
    "get_default_bedrock_thinking_effort",
    "is_bedrock_ftu_form_error",
    "raise_if_bedrock_ftu_form_error",
]
