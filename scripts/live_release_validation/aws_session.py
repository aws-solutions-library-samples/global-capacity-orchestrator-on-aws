"""Throttle-resilient boto3 session facade for the live-validation harness.

Every inventory scanner fans out across all enabled Regions and issues one
metadata read per resource (``ListTagsForResource`` on each CloudWatch log
group, per-role IAM tag lookups, and so on). Under botocore's default retry
budget a Regional TPS squeeze surfaces as a hard failure — a real
``final-inventory`` run died with ``ThrottlingException … reached max
retries: 4`` while scanning 17 Regions even though the account state was
clean.

The facade below gives every client the harness creates botocore's
``adaptive`` retry mode: client-side rate limiting that paces request bursts
before they trip the service, plus a much deeper retry budget for the
throttling errors that still get through. Correctness is unchanged — after
the budget is exhausted the original ``ClientError`` still propagates, so
every fail-closed path behaves exactly as before; the run just no longer
fails on a transient rate spike that a bounded wait absorbs.

Callers that pass their own ``config`` keep every field they set — the
retry defaults only fill the gaps (``botocore.config.Config.merge`` gives
the *other* config precedence on conflicts).
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config

# ``adaptive`` includes everything ``standard`` retries (throttling errors,
# transient 5xx, timeouts) and adds client-side rate limiting. The attempt
# budget is deliberately deep: with exponential backoff it absorbs a
# sustained Regional throttle window, while a genuine outage still fails
# within a bounded, observable number of attempts.
_RETRY_MAX_ATTEMPTS = 12

_ADAPTIVE_RETRY_CONFIG = Config(
    retries={
        "mode": "adaptive",
        "max_attempts": _RETRY_MAX_ATTEMPTS,
    }
)


class ThrottleResilientSession:
    """Delegate to a real ``boto3.Session``, injecting adaptive retries.

    Only ``client`` and ``resource`` construction is intercepted; every other
    attribute (``get_credentials``, ``get_available_regions``,
    ``get_partition_for_region``, ``region_name``, …) resolves on the wrapped
    session unchanged.
    """

    def __init__(self, session: Any | None = None) -> None:
        self._session = session if session is not None else boto3.Session()

    @staticmethod
    def _merged_config(config: Any | None) -> Any:
        if config is None:
            return _ADAPTIVE_RETRY_CONFIG
        # ``merge`` returns a new Config whose fields prefer ``config`` —
        # a caller that sets its own ``retries`` wins over the default.
        return _ADAPTIVE_RETRY_CONFIG.merge(config)

    def client(self, *args: Any, config: Any | None = None, **kwargs: Any) -> Any:
        return self._session.client(*args, config=self._merged_config(config), **kwargs)

    def resource(self, *args: Any, config: Any | None = None, **kwargs: Any) -> Any:
        return self._session.resource(*args, config=self._merged_config(config), **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)
