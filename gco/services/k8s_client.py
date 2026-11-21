"""A Kubernetes API client whose every request carries a timeout.

The kubernetes Python client times a request out only when the call passes
``_request_timeout``. ``RESTClientObject.request`` builds the ``urllib3``
timeout from that argument and nothing else, ``Configuration`` has no timeout
setting to fall back on, and ``None`` (the default of every generated method
and of the dynamic client) reaches urllib3 as no timeout at all. One stalled
API-server connection then blocks its thread indefinitely.

:class:`BoundedApiClient` sets a default in the one place every request passes
through. The generated typed methods and the dynamic client, its discovery
reads included, all call ``ApiClient.call_api`` with ``_request_timeout``
spelled out, so ``call_api`` substitutes :attr:`BoundedApiClient.default_request_timeout`
when that argument is ``None`` and passes any other value on unchanged: a
number is a total timeout, a ``(connect, read)`` pair bounds each phase.

The timeout bounds one attempt. urllib3 retries an idempotent request that
times out (three retries by default), so a stalled GET gives up after a few
attempts rather than at the first timeout. It applies the read timeout to each
socket read rather than to the whole body, so a streamed response
(``_preload_content=False``) that keeps arriving is not cut short. A watch or
a followed log that can stay silent for longer than the default must pass its
own ``(connect, read)`` pair.

A typed API class uses the client it is given (``client.CoreV1Api(api_client)``);
built without one, it creates its own unbounded ``ApiClient``.
"""

from __future__ import annotations

import os
from typing import Any

from kubernetes import client
from kubernetes.client import rest

#: Seconds a Kubernetes API request may take when its call names no timeout.
DEFAULT_K8S_API_TIMEOUT_SECONDS = 30

#: Environment variable that overrides :data:`DEFAULT_K8S_API_TIMEOUT_SECONDS`.
K8S_API_TIMEOUT_ENV = "K8S_API_TIMEOUT"

#: What ``_request_timeout`` accepts: total seconds, or ``(connect, read)`` seconds.
RequestTimeout = float | tuple[float, float]


def k8s_api_timeout_from_env() -> int:
    """The request timeout ``K8S_API_TIMEOUT`` sets, in whole seconds (default 30).

    Raises:
        ValueError: when the variable is not a positive integer. Zero is
            refused rather than passed on because the client reads a zero
            timeout as no timeout.
    """
    raw = os.environ.get(K8S_API_TIMEOUT_ENV, str(DEFAULT_K8S_API_TIMEOUT_SECONDS))
    invalid = f"{K8S_API_TIMEOUT_ENV} must be a positive number of seconds, got {raw!r}"
    try:
        timeout = int(raw)
    except ValueError:
        raise ValueError(invalid) from None
    if timeout <= 0:
        raise ValueError(invalid)
    return timeout


class BoundedApiClient(client.ApiClient):
    """An ``ApiClient`` that applies a default timeout to requests that name none.

    ``default_request_timeout`` defaults to :func:`k8s_api_timeout_from_env`.
    """

    def __init__(
        self,
        configuration: client.Configuration | None = None,
        *,
        default_request_timeout: RequestTimeout | None = None,
    ) -> None:
        super().__init__(configuration)
        self.default_request_timeout: RequestTimeout = (
            k8s_api_timeout_from_env()
            if default_request_timeout is None
            else default_request_timeout
        )

    def call_api(
        self,
        method: str,
        url: str,
        header_params: dict[str, Any] | None = None,
        body: Any = None,
        post_params: Any = None,
        _request_timeout: RequestTimeout | None = None,
    ) -> rest.RESTResponse:
        """Send one request, bounded by the caller's timeout or else the default."""
        return super().call_api(
            method,
            url,
            header_params=header_params,
            body=body,
            post_params=post_params,
            _request_timeout=(
                self.default_request_timeout if _request_timeout is None else _request_timeout
            ),
        )
