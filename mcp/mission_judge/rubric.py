"""The fixed, versioned scoring guidance the judge embeds in every prompt.

The judge scores how close a goal-directed run is to satisfying its objective
on a single ``0.0``-``1.0`` scale. That scale is only meaningful if every
invocation is judged against the *same* yardstick, so this module owns two
module-level constants and nothing else:

* :data:`RUBRIC` — the scoring guidance folded verbatim into each prompt. It
  is byte-identical across all invocations within a shipped build and is
  never interpolated with a clock, a random value, or any other ambient
  state, so two identical inputs always produce an identical prompt.
* :data:`RUBRIC_VERSION` — a short, stable identifier of the rubric text in
  effect. It is recorded in a result's provenance so an operator can later
  interpret a historical score against the exact guidance that produced it.

The two are kept side by side on purpose: the version identifies the text, so
**any edit to** :data:`RUBRIC` **must be paired with a new** :data:`RUBRIC_VERSION`
**value**. A reader (or reviewer) changing one is meant to change the other in
the same edit.
"""

from __future__ import annotations

# Stable identifier of the rubric text below. A non-empty string of at most
# 64 characters with no whitespace. Bump this whenever RUBRIC changes so a
# recorded score can be traced back to the exact guidance that produced it.
RUBRIC_VERSION: str = "spj-v1"

# Fixed scoring guidance embedded in every prompt. Byte-identical across all
# invocations within a shipped build and never interpolated with any
# clock-derived, random, or ambient value. Maps the score onto [0.0, 1.0]:
# 0.0 = no progress toward the objective, 1.0 = objective fully satisfied.
# Editing this text requires bumping RUBRIC_VERSION above.
RUBRIC: str = """\
You are scoring how close a goal-directed automation run is to satisfying its
stated objective. Return a single progress score in the closed range 0.0 to 1.0:
  - 0.0  no meaningful progress toward the objective
  - 0.25 early progress; foundational steps done, objective far off
  - 0.5  roughly half the observable work toward the objective is complete
  - 0.75 most of the objective is satisfied; minor work remains
  - 1.0  the objective is fully satisfied
Judge only against the objective and the supplied progress context. Do not
reward activity that does not advance the objective.
"""
