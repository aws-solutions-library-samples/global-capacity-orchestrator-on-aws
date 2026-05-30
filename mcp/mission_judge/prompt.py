"""Assemble the deterministic prompt the judge sends to the model.

The judge scores progress from a directive plus optional recent progress
context, embedding the fixed rubric. The single non-deterministic step in
the whole flow is the model's answer; everything that builds the request is
pure and reproducible. This module owns that pure half:

* :func:`truncate_context` — bounds arbitrarily large progress context to a
  fixed character budget by keeping the most recent content and discarding
  the oldest, so a long metric history can never produce an unbounded prompt.
* :class:`JudgePrompt` — a frozen bundle of the directive, the (already
  truncated) context, and the rubric, with an :meth:`~JudgePrompt.assemble`
  method that renders them into a fixed-layout string.
* :func:`build_prompt` — the entry point the tool wrapper calls: truncate the
  context, bind the rubric, and return a ready-to-assemble prompt.

The rendered prompt is a pure function of its inputs — no clock, no random
value, no ambient state — so two identical inputs always produce a
byte-identical string. The model is instructed to answer with a single JSON
object carrying exactly one numeric ``score`` field and one ``rationale``
field, which keeps the downstream parse step deterministic.

The duck-typed :class:`JudgePrompt` exposes only ``assemble() -> str``, which
is the entire surface a sampling backend touches, so it drives either backend
without importing the sampling module.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rubric import RUBRIC

# Maximum characters of recent progress context folded into the prompt. An
# arbitrarily large metric history or observation set cannot grow the prompt
# without bound because anything past this budget is discarded oldest-first.
MAX_CONTEXT_CHARS: int = 8000

# Maximum characters of model rationale retained in provenance. Kept here
# beside the context budget so both prompt-size knobs live in one place.
MAX_RATIONALE_CHARS: int = 2000

# Prepended to the retained tail whenever context is truncated, so a reader
# can see at a glance that older content was dropped.
TRUNCATION_MARKER: str = "...[older context truncated]"


def truncate_context(context: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    """Bound ``context`` to ``limit`` characters, keeping the most recent tail.

    When ``context`` is already at or under ``limit`` it is returned
    unchanged. When it is longer, the oldest characters (the head) are
    discarded and the most recent characters (the tail) are retained, with
    :data:`TRUNCATION_MARKER` prepended so the result reads as a clipped view.

    The returned string — marker included — is always at most ``limit``
    characters: the retained tail is sized to ``limit - len(TRUNCATION_MARKER)``
    so prepending the marker lands exactly on ``limit``. In the degenerate
    case where ``limit`` is too small to hold even the marker
    (``limit <= len(TRUNCATION_MARKER)``), the marker is omitted and the most
    recent ``limit`` characters are returned, so ``len(result) <= limit`` holds
    for every input and every non-negative ``limit``.

    The operation is deterministic: two identical oversized inputs yield a
    byte-identical result.
    """
    if len(context) <= limit:
        return context

    tail_length = limit - len(TRUNCATION_MARKER)
    if tail_length <= 0:
        # Not enough room for the marker; keep the most recent ``limit``
        # characters so the length bound still holds.
        return context[len(context) - limit :]

    tail = context[len(context) - tail_length :]
    return TRUNCATION_MARKER + tail


@dataclass(frozen=True)
class JudgePrompt:
    """A frozen, deterministic prompt for the progress judge.

    The dataclass is ``frozen`` so the inputs cannot mutate between
    construction and :meth:`assemble`, which guarantees the rendered string
    is a stable function of the bound fields. ``context`` is expected to be
    already truncated (an empty string when no context was supplied).
    """

    directive: str
    context: str
    rubric: str
    rubric_version: str

    def assemble(self) -> str:
        """Render the fixed-layout prompt as a byte-deterministic string.

        Sections are delimited by ``=== <name> ===`` headers emitted in a
        fixed order: the scoring rubric (tagged with its version), the
        directive, the recent progress context, and the output-format
        instruction. The body contains no clock-derived, random, or ambient
        content, so identical ``(directive, context, rubric, rubric_version)``
        inputs always produce an identical byte sequence.

        The output-format section instructs the model to answer with a single
        JSON object carrying exactly one numeric ``score`` field (in the
        closed range ``0.0`` to ``1.0``) and one free-text ``rationale``
        field, so the score-extraction step downstream is deterministic.
        """
        sections: list[str] = []
        sections.append(
            "You are scoring how close a goal-directed automation run is to "
            "satisfying its stated objective. Read the scoring rubric, the "
            "objective, and the recent progress context, then return your "
            "judgement in the required output format."
        )
        sections.append("")
        sections.append(f"=== Scoring rubric (version {self.rubric_version}) ===")
        sections.append(self.rubric)
        sections.append("")
        sections.append("=== Mission directive ===")
        sections.append(self.directive)
        sections.append("")
        sections.append("=== Recent progress context ===")
        sections.append(self.context)
        sections.append("")
        sections.append("=== Output format ===")
        sections.append(
            "Respond with a single JSON object and no prose outside it. The "
            "object must contain exactly two fields: a numeric `score` field "
            "holding your progress score as a number in the closed range 0.0 "
            "to 1.0, and a `rationale` field holding a brief free-text "
            "explanation of that score."
        )
        return "\n".join(sections)


def build_prompt(directive: str, recent_context: str | None, rubric_version: str) -> JudgePrompt:
    """Build a :class:`JudgePrompt` from the directive and optional context.

    ``recent_context`` is bounded with :func:`truncate_context` (keeping the
    most recent tail); when it is ``None`` or empty the prompt's context
    becomes an empty string and the judgement proceeds from the directive
    alone. The module rubric is bound verbatim and paired with
    ``rubric_version``. The result is a pure function of the three inputs.
    """
    context = truncate_context(recent_context) if recent_context else ""
    return JudgePrompt(
        directive=directive,
        context=context,
        rubric=RUBRIC,
        rubric_version=rubric_version,
    )
