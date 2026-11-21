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

import logging
import time
from typing import Any

import boto3
from botocore.config import Config

from cli.aws_client import GCOAWSClient

logger = logging.getLogger(__name__)

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


# AWS WAF answers a request its web ACL blocks through API Gateway's
# ``WAF_FILTERED`` gateway response: HTTP 403 with this body. WAF decides
# before API Gateway authorizes or integrates anything, so a blocked request
# never reached GCO and sending it again is safe for every method. An IAM
# refusal names the caller ("User: ... is not authorized ..."), and an
# unknown route says "Missing Authentication Token", so neither matches.
_WAF_BLOCK_BODY = {"message": "Forbidden"}
#: Wait between attempts while the web ACL blocks this host.
_WAF_RETRY_INTERVAL_SECONDS = 30.0
#: GCO's per-IP rate rule counts a trailing five-minute window and WAF
#: re-evaluates it about every 30 seconds, so a block clears within about six
#: minutes of the burst that caused it; seven bounds the wait.
_WAF_RETRY_DEADLINE_SECONDS = 420.0


def is_waf_block(response: Any) -> bool:
    """Whether ``response`` is API Gateway relaying an AWS WAF block."""
    if getattr(response, "status_code", None) != 403:
        return False
    try:
        return bool(response.json() == _WAF_BLOCK_BODY)
    except ValueError:
        return False


class WafPatientApiClient(GCOAWSClient):
    """The CLI's API client, waiting out the API's per-IP AWS WAF rate rule.

    The global API's web ACL blocks a source IP that sends more than
    ``waf.per_ip_rate_limit`` requests (100 by default) in five minutes. The
    harness polls jobs, add-ons and backends from one machine, and a busy
    action crossed that limit live: the ``schedulers`` action's PCS status
    read got the WAF's 403 and failed the run. Every request, including the
    ones the client's other methods make, goes through this method: a WAF
    block is retried every 30 seconds for up to seven minutes, and the last
    response is returned when the block outlasts that, so a real outage still
    fails. Any other response, a 403 that is not WAF's included, is returned
    as it is.
    """

    def make_authenticated_request(self, *args: Any, **kwargs: Any) -> Any:
        deadline = time.monotonic() + _WAF_RETRY_DEADLINE_SECONDS
        while True:
            response = super().make_authenticated_request(*args, **kwargs)
            if not is_waf_block(response) or time.monotonic() >= deadline:
                return response
            logger.warning(
                "The API's AWS WAF rate rule blocked this host; retrying in %.0fs",
                _WAF_RETRY_INTERVAL_SECONDS,
            )
            response.close()
            time.sleep(_WAF_RETRY_INTERVAL_SECONDS)
