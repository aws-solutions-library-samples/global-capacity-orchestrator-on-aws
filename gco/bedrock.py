"""Shared Bedrock defaults loaded from the canonical ``cdk.json`` context."""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

_BEDROCK_CONTEXT_KEY = "bedrock"
_DEFAULT_MODEL_ID_KEY = "default_model_id"
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


def _model_id_from_payload(payload: Any, path: Path) -> str:
    """Extract and strictly validate the configured model id."""
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
    return model_id.strip()


def get_default_bedrock_model_id(cdk_json_path: Path | None = None) -> str:
    """Return the sole checked-in Bedrock default from ``cdk.json``.

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

    return _model_id_from_payload(payload, path)


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
    "BedrockModelConfigurationError",
    "get_default_bedrock_model_id",
]
