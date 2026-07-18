"""Generation-time metadata shared by every code-diagram artifact."""

from __future__ import annotations

import os
from datetime import UTC, datetime


def generation_timestamp_utc() -> str:
    """Return one ISO-8601 UTC timestamp for a generator invocation.

    ``SOURCE_DATE_EPOCH`` makes regeneration reproducible when supplied;
    otherwise the current UTC time records when the artifacts were produced.
    Timestamps intentionally use whole seconds and a trailing ``Z`` so they
    remain compact and unambiguous in HTML, PNGs, READMEs, and source markers.
    """
    source_date_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if source_date_epoch is None:
        generated_at = datetime.now(UTC)
    else:
        try:
            generated_at = datetime.fromtimestamp(int(source_date_epoch), UTC)
        except (OSError, OverflowError, ValueError) as exc:
            raise ValueError(
                "SOURCE_DATE_EPOCH must be an integer Unix timestamp",
            ) from exc
    return generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
