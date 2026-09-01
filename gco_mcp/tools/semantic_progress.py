"""Read-only LLM-as-judge tool that scores Mission progress.

The single ``metrics_semantic_progress`` tool scores how close a Mission is
to satisfying its directive and returns that score in the canonical
``{"metrics": {"progress_score": <number>}}`` shape the Observe_Phase merges,
so a plain ``metric_threshold`` or ``metric_trend`` criterion can read it by
dot-path with no special handling.

The whole tool registration is gated by ``GCO_ENABLE_SEMANTIC_PROGRESS`` so the
``@mcp.tool`` decorator only fires when the flag (or the umbrella
``GCO_ENABLE_ALL_TOOLS``) is enabled. With the flag unset this module imports
cleanly and FastMCP never sees the tool. Each invocation incurs one LLM call
via the existing sampling seam, which is why the tool is default-off.

[gated by GCO_ENABLE_SEMANTIC_PROGRESS]
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from audit import audit_logged
from feature_flags import FLAG_SEMANTIC_PROGRESS, is_enabled
from server import mcp

# The pure judge package and the sampling seam live under ``gco_mcp/``; the
# path-injection pattern matches the rest of the MCP module surface so
# ``import mission_judge.*`` and ``import mission.*`` resolve without making
# the ``mcp`` directory a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The sampling seam — reused, not reconstructed.
from mission.sampling import (  # noqa: E402
    SamplingTransportError,
    select_sampling_backend,
)
from mission_judge import prompt as judge_prompt  # noqa: E402
from mission_judge import rubric as judge_rubric  # noqa: E402
from mission_judge import score as judge_score  # noqa: E402
from mission_judge.shape import (  # noqa: E402
    ErrorCode,
    JudgeError,
    error_envelope,
    metrics_result,
    validate_output_name,
)

# Registration is entirely gated by the feature flag. When the flag is unset,
# the decorator below never fires and FastMCP never sees the tool, so it does
# not appear in ``mcp.list_tools()``. The gate is evaluated only through
# ``feature_flags.is_enabled`` — never by reading ``os.environ`` here.
if is_enabled(FLAG_SEMANTIC_PROGRESS):

    @mcp.tool(tags={"safe", "metrics"})
    @audit_logged
    async def metrics_semantic_progress(
        directive: str,
        recent_context: str | None = None,
        output_name: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """[gated by GCO_ENABLE_SEMANTIC_PROGRESS] [read-only] Score Mission progress.

        Scores how close a Mission is to satisfying ``directive`` against a
        fixed, versioned rubric via the existing sampling backend, and returns
        the canonical ``{"metrics": {"progress_score": <float 0.0-1.0>}}`` shape
        consumable by a ``metric_threshold`` (e.g. ``progress_score >= 0.8``) or
        ``metric_trend`` (e.g. ``progress_score`` increasing) criterion. Incurs
        one LLM call per invocation. Mutates nothing — it only reads its inputs
        and asks the model for a score. Provenance (rationale, source,
        backend_name, model_id, rubric_version, raw_score) is returned outside
        the ``metrics`` object.

        Args:
            directive: The natural-language objective the Mission is pursuing.
                Must be non-empty and not whitespace-only.
            recent_context: Optional recent progress context (recent
                observations and/or metric-history series the caller selects).
                Truncated keep-newest to a fixed character budget; omit it to
                score from the directive alone.
            output_name: Optional metric key under ``metrics`` (default
                ``"progress_score"``). Must be a single path segment of 1..128
                characters with no ``.`` separator and no whitespace.
            model_id: Optional concrete model identifier forwarded to the
                sampling seam; ``None`` uses the seam's resolved default.

        Returns the canonical metrics shape on success, or a structured
        ``{"code", "details"}`` error envelope (never carrying a top-level
        ``metrics`` key) on any failure, so the Mission loop keeps running.
        """
        try:
            key = validate_output_name(output_name) if output_name else "progress_score"
            if not directive or not directive.strip():
                raise JudgeError(ErrorCode.MISSING_DIRECTIVE)

            prompt = judge_prompt.build_prompt(
                directive, recent_context, judge_rubric.RUBRIC_VERSION
            )

            # Bedrock is the only sampling transport (MCP client sampling
            # left the protocol with FastMCP 4), so the judge samples
            # server-side for CLI and MCP callers alike.
            backend = select_sampling_backend(model_id)
            if backend is None:  # defensive: seam stubs may return None
                raise JudgeError(ErrorCode.NO_SAMPLING_BACKEND)

            try:
                # The ONLY non-determinism; no retry. Both shipped backends
                # call only ``prompt.assemble()``, so the duck-typed JudgePrompt
                # drives either of them — same shim pattern as the sampling
                # module's own ``_PreRendered`` look-alike.
                raw_text = await backend.sample(prompt)  # type: ignore[arg-type]
            except SamplingTransportError as err:
                raise JudgeError(
                    ErrorCode.SAMPLING_TRANSPORT_ERROR,
                    {
                        "transport_code": err.code,
                        "backend_name": backend.backend_name,
                        "model_id": backend.model_id,
                    },
                ) from err

            raw_score, rationale = judge_score.parse_score(raw_text)  # raises INVALID_MODEL_SCORE
            value = judge_score.clamp_score(raw_score)

            return metrics_result(
                key,
                value,
                rationale=rationale[: judge_prompt.MAX_RATIONALE_CHARS],
                source=f"{backend.backend_name}:{backend.model_id}",
                backend_name=backend.backend_name,
                model_id=backend.model_id,
                rubric_version=judge_rubric.RUBRIC_VERSION,
                raw_score=raw_score,
            )
        except JudgeError as err:
            return error_envelope(err.code, **err.details)
        except Exception as err:  # noqa: BLE001 - defensive: nothing escapes the tool
            return error_envelope(
                ErrorCode.SAMPLING_TRANSPORT_ERROR, reason="unexpected", detail=str(err)
            )
