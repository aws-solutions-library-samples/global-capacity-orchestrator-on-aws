"""Framing for recorded data that a prompt hands to a model.

Tool output, job logs, evaluator evidence and other recorded data can contain
text that looks like instructions ("ignore the rubric and answer ..."). A
prompt puts each piece of such data between an opening and a closing marker
line, and says once, outside the blocks, that everything between the markers
is data and never instructions. The Mission sampling prompts
(``mission/sampling.py``) and the semantic-progress judge
(``mission_judge/prompt.py``) both use these helpers. The module has no
dependencies so the judge stays independent of the sampling module.
"""

from __future__ import annotations

import re

#: Line that opens a block of recorded data, and the line that closes it.
#: ``{name}`` is the block's name.
UNTRUSTED_DATA_OPEN: str = "<<<UNTRUSTED DATA {name}>>>"
UNTRUSTED_DATA_CLOSE: str = "<<<END UNTRUSTED DATA {name}>>>"

#: A ``<`` or ``>`` that starts a run of three: the only way a marker line can
#: be spelled. See :func:`untrusted_block`.
_MARKER_RUN = re.compile(r"<(?=<<)|>(?=>>)")


def untrusted_block(name: str, payload: str) -> list[str]:
    """Return the prompt lines that frame ``payload`` as untrusted data.

    Any ``<`` or ``>`` that would start a run of three is written as the JSON
    escape ``\\u003c`` / ``\\u003e``, so no marker line can be spelled inside
    the block. In JSON text such a character can only occur inside a string,
    where the escape decodes to the same character, so the payload stays
    valid JSON with the same value; in plain text the escape is shown as is.
    Single and double brackets (``"op": "<"``) stay readable.
    """
    escaped = _MARKER_RUN.sub(lambda m: "\\u003c" if m.group() == "<" else "\\u003e", payload)
    return [
        UNTRUSTED_DATA_OPEN.format(name=name),
        escaped,
        UNTRUSTED_DATA_CLOSE.format(name=name),
    ]
