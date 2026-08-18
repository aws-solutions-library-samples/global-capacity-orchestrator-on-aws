"""Directive embedding for mission memory.

One free function, :func:`embed_text`, turns a Mission directive into the
vector that the ``{project}-mission-memory`` DynamoDB vector index stores
and queries. The Bedrock client is constructed exactly the way
:class:`mcp.mission.sampling.BedrockSamplingBackend` builds its client
(``boto3.Session().client("bedrock-runtime", config=Config(read_timeout=
BEDROCK_READ_TIMEOUT_SECONDS))``), and the model id resolves through
:func:`gco.bedrock.get_default_embedding_model_id` so the runtime and the
deployed index share one configuration source.

Failure contract: every failure raises the typed :class:`EmbeddingError`
so callers decide whether to swallow. The engine's memory write/read
paths are best-effort and do swallow; the CLI surfaces the message. The
one deliberate exception is the Bedrock first-time-use gate —
:func:`gco.bedrock.raise_if_bedrock_ftu_form_error` escalates that case
to :class:`gco.bedrock.BedrockFTUFormNotAcceptedError` because it is a
permanent, account-scoped misconfiguration with a one-line fix, and the
established posture (see ``sampling.py``) is that it must never be
absorbed by a graceful-degradation handler.

Request shape note: the body follows the Amazon Titan Text Embeddings
contract (``inputText`` plus, when supplied, the V2-only ``dimensions``
key). ``dimensions`` is omitted from the request when the caller passes
``None`` so V1-family models — which reject the key — keep working.

``boto3`` / ``botocore`` are imported lazily inside the functions so
pure-Python consumers of the mission package do not pay for the SDK
import, and so tests can inject a fake ``boto3`` module through
``sys.modules`` (the same pattern ``sampling.py`` uses).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gco.bedrock import (
    BEDROCK_READ_TIMEOUT_SECONDS,
    get_default_embedding_model_id,
    raise_if_bedrock_ftu_form_error,
)

logger = logging.getLogger(__name__)

__all__ = [
    "EmbeddingError",
    "embed_text",
]


class EmbeddingError(RuntimeError):
    """A directive embedding could not be produced.

    The message is a short machine-matchable code, mirroring the
    ``SamplingTransportError`` convention in ``sampling.py``:

    * ``embedding_empty_text`` — the input was empty or whitespace-only.
    * ``embedding_no_credentials`` — ``boto3`` could not resolve
      credentials at client-construction time.
    * ``embedding_bedrock_<ErrorCode>`` — ``InvokeModel`` raised a
      ``ClientError``; ``<ErrorCode>`` is the AWS error code.
    * ``embedding_transport_failure`` — a non-``ClientError`` botocore
      transport fault (endpoint unreachable, read timeout, ...).
    * ``embedding_malformed_response`` — the response body was not JSON
      or did not carry a non-empty numeric ``embedding`` list.
    """


def _build_client() -> Any:
    """Return a fresh ``bedrock-runtime`` client.

    Split out of :func:`embed_text` so tests can monkeypatch this one
    seam instead of faking the whole ``boto3`` module (both work). The
    imports are local — see the module docstring.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import (
        NoCredentialsError,
        PartialCredentialsError,
    )

    try:
        return boto3.Session().client(
            "bedrock-runtime",
            config=Config(read_timeout=BEDROCK_READ_TIMEOUT_SECONDS),
        )
    except (NoCredentialsError, PartialCredentialsError) as err:
        raise EmbeddingError("embedding_no_credentials") from err


def embed_text(
    text: str,
    *,
    model_id: str | None = None,
    dimensions: int | None = None,
) -> list[float]:
    """Embed ``text`` and return the vector as a plain list of floats.

    Args:
        text: The text to embed — for mission memory, the verbatim
            operator directive. Must be non-empty.
        model_id: Bedrock model id override. ``None`` resolves the
            checked-in default via
            :func:`gco.bedrock.get_default_embedding_model_id`.
        dimensions: Requested output width, passed through to the model
            (Titan Text Embeddings V2 accepts 256/512/1024). ``None``
            omits the key so the model's own default width applies.
            The deployed vector index width is a one-way door — query
            vectors must match it, so callers that know the configured
            width should pass it.

    Raises:
        EmbeddingError: On any embedding failure; see the class
            docstring for the code taxonomy.
        gco.bedrock.BedrockFTUFormNotAcceptedError: The account has not
            submitted Anthropic's first-time-use form (only reachable
            when the embedding model is Anthropic-gated).
        gco.bedrock.BedrockModelConfigurationError: The canonical
            ``cdk.json`` could not supply a default model id.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    if not isinstance(text, str) or not text.strip():
        raise EmbeddingError("embedding_empty_text")

    resolved_model_id = model_id or get_default_embedding_model_id()

    body: dict[str, Any] = {"inputText": text}
    if dimensions is not None:
        body["dimensions"] = int(dimensions)

    client = _build_client()
    try:
        response = client.invoke_model(
            modelId=resolved_model_id,
            body=json.dumps(body),
            contentType="application/json",
            accept="application/json",
        )
    except ClientError as err:
        raise_if_bedrock_ftu_form_error(err)
        code = err.response.get("Error", {}).get("Code") or "ClientError"
        raise EmbeddingError(f"embedding_bedrock_{code}") from err
    except BotoCoreError as err:
        raise EmbeddingError("embedding_transport_failure") from err

    try:
        payload = json.loads(response["body"].read())
    except (AttributeError, KeyError, TypeError, ValueError) as err:
        raise EmbeddingError("embedding_malformed_response") from err

    vector = payload.get("embedding") if isinstance(payload, dict) else None
    if (
        not isinstance(vector, list)
        or not vector
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vector)
    ):
        raise EmbeddingError("embedding_malformed_response")
    return [float(v) for v in vector]
