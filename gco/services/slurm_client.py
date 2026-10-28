"""slurmrestd client behind the GCO API's ``/api/v1/slurm`` routes.

The manifest-processor (the authenticated API ingress) talks to Slurm through
slurmrestd, the Slurm REST daemon the Slinky chart runs in ``gco-jobs``
(``gco.slurm_config.SLURM_RESTAPI_NAME``). Authentication is Slurm's own
``auth/jwt``: the slurm-operator mints a JWT for the API's Slurm identity into
a Kubernetes Secret through a ``Token`` custom resource
(post-helm-slurm-api-access.yaml) and refreshes it before it expires; this
module reads that Secret (a scoped RBAC grant on exactly that one Secret) and
sends it as ``X-SLURM-USER-TOKEN``. The API never sees the cluster's JWT
signing key.

The hop is plain HTTP inside the namespace boundary (the NetworkPolicies in
post-helm-slurm-network.yaml admit only the manifest-processor pods to the
REST port); slurmrestd has no TLS listener of its own, and the ALB-facing side
of the API is the TLS boundary callers see.

Errors map to the API the same way the cost proxy's do: Slurm not enabled for
the deployment -> 404 with a remedy, slurmrestd unreachable or the token
unavailable -> 503, a Slurm-side rejection -> the status slurmrestd meant (404
for an unknown job, 400 for a bad submission, 502 for anything else).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

import httpx2
from fastapi import HTTPException

from gco.services import tracing
from gco.slurm_config import SLURM_NAMESPACE, SLURM_RESTAPI_NAME, SLURM_RESTAPI_PORT

logger = logging.getLogger(__name__)

#: The slurmrestd OpenAPI plugin version the Slurm 26.05 images serve; bump in
#: lockstep with the image tag in charts.yaml (``slurmrestd -d list`` shows the
#: versions an image ships).
SLURM_API_VERSION = "v0.0.44"

#: The Slurm identity the API acts as. ``slurm`` is SlurmUser: it may submit,
#: inspect and cancel any job, which is what a single authenticated front door
#: needs; the caller's identity travels in each job's ``comment`` for
#: accounting attribution.
SLURM_API_USER = "slurm"

#: The Token custom resource and the Secret it writes (post-helm-slurm-api-access.yaml).
SLURM_API_TOKEN_NAME = "slinky-slurm-token-gco-api"
SLURM_API_TOKEN_SECRET = SLURM_API_TOKEN_NAME  # nosec B105  # Kubernetes Secret name
SLURM_API_TOKEN_KEY = "auth-token"  # nosec B105  # Secret data key, not a credential

DEFAULT_RESTAPI_URL = (
    f"http://{SLURM_RESTAPI_NAME}.{SLURM_NAMESPACE}.svc.cluster.local:{SLURM_RESTAPI_PORT}"
)

#: How long a token read from the Secret is reused before it is re-read. The
#: operator refreshes the Secret at 80% of the token lifetime, so re-reading
#: every few minutes (and on any 401) always finds a valid token.
TOKEN_REFRESH_SECONDS = 300.0

REQUEST_TIMEOUT_SECONDS = 30.0

DISABLED_DETAIL = (
    "Slurm is not enabled in this region. Set helm.slurm.enabled to true in cdk.json "
    "(or deploy with --enable slurm) and redeploy the regional stack."
)
UNAVAILABLE_DETAIL = (
    "Slurm's REST API is unavailable in this region. Check the slinky-slurm-restapi "
    "Deployment and the slinky-slurm-controller StatefulSet in gco-jobs."
)
TOKEN_UNAVAILABLE_DETAIL = (
    "The GCO API's Slurm credential is unavailable. Check the Token "
    f"{SLURM_API_TOKEN_NAME} in gco-jobs and the slurm-operator logs."
)


class SlurmDisabledError(Exception):
    """Slurm is not deployed in this region (cdk.json helm.slurm.enabled false)."""


class SlurmUnavailableError(Exception):
    """slurmrestd or the API's token cannot be reached right now."""


class SlurmApiError(Exception):
    """slurmrestd answered with an error for the request."""

    def __init__(self, status_code: int, errors: list[Any], detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.errors = errors
        self.detail = detail


def slurm_enabled() -> bool:
    """Whether the regional stack deployed Slurm (``GCO_SLURM_ENABLED`` on the API pod)."""
    return os.getenv("GCO_SLURM_ENABLED", "false").strip().lower() == "true"


def restapi_base_url() -> str:
    return os.getenv("SLURM_RESTAPI_URL", DEFAULT_RESTAPI_URL).rstrip("/")


def api_version() -> str:
    return os.getenv("SLURM_API_VERSION", SLURM_API_VERSION).strip() or SLURM_API_VERSION


class SecretTokenProvider:
    """Reads the operator-minted JWT from its Kubernetes Secret, with caching.

    The Secret is read with the manifest-processor's own ServiceAccount, which
    the post-Helm manifest grants ``get`` on exactly this Secret in
    ``gco-jobs``. ``invalidate()`` after a 401 forces the next call to re-read.
    """

    def __init__(
        self,
        *,
        namespace: str = SLURM_NAMESPACE,
        name: str = SLURM_API_TOKEN_SECRET,
        key: str = SLURM_API_TOKEN_KEY,
        refresh_seconds: float = TOKEN_REFRESH_SECONDS,
    ) -> None:
        self.namespace = namespace
        self.name = name
        self.key = key
        self.refresh_seconds = refresh_seconds
        self._token: str | None = None
        self._read_at = 0.0

    def invalidate(self) -> None:
        self._token = None
        self._read_at = 0.0

    def _read_secret(self) -> str:
        from kubernetes import client as kubernetes_client

        secret = kubernetes_client.CoreV1Api().read_namespaced_secret(self.name, self.namespace)
        data = getattr(secret, "data", None) or {}
        encoded = data.get(self.key)
        if not encoded:
            raise SlurmUnavailableError(
                f"Secret {self.namespace}/{self.name} has no {self.key!r} key yet"
            )
        token = base64.b64decode(encoded).decode("utf-8").strip()
        if not token:
            raise SlurmUnavailableError(f"Secret {self.namespace}/{self.name} holds an empty token")
        return token

    async def token(self) -> str:
        now = time.monotonic()
        if self._token is not None and now - self._read_at < self.refresh_seconds:
            return self._token
        try:
            token = await asyncio.to_thread(self._read_secret)
        except SlurmUnavailableError:
            raise
        except Exception as exc:
            raise SlurmUnavailableError(
                f"could not read Secret {self.namespace}/{self.name}: {exc}"
            ) from exc
        self._token = token
        self._read_at = time.monotonic()
        return token


class SlurmRestClient:
    """A thin, typed wrapper over the slurmrestd endpoints the API exposes."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        version: str | None = None,
        token_provider: SecretTokenProvider | None = None,
        user: str = SLURM_API_USER,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = (base_url or restapi_base_url()).rstrip("/")
        self.version = version or api_version()
        self.token_provider = token_provider or SecretTokenProvider()
        self.user = user
        self.timeout = timeout

    # -- transport -----------------------------------------------------------

    async def _send(
        self, method: str, path: str, *, params: Mapping[str, Any] | None, json: Any
    ) -> httpx2.Response:
        token = await self.token_provider.token()
        headers = {"X-SLURM-USER-NAME": self.user, "X-SLURM-USER-TOKEN": token}
        url = f"{self.base_url}{path}"
        try:
            transport = httpx2.AsyncHTTPTransport(trust_env=False)
            async with httpx2.AsyncClient(
                transport=tracing.wrap_async_transport(transport),
                timeout=self.timeout,
                trust_env=False,
            ) as client:
                return await client.request(
                    method, url, headers=headers, params=dict(params or {}), json=json
                )
        except httpx2.HTTPError as exc:
            logger.warning("slurmrestd unreachable at %s: %s", url, exc)
            raise SlurmUnavailableError(f"slurmrestd unreachable at {self.base_url}") from exc

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        """Send one request; retry once with a fresh token on 401; decode the body."""
        response = await self._send(method, path, params=params, json=json)
        if response.status_code == 401:
            self.token_provider.invalidate()
            response = await self._send(method, path, params=params, json=json)
        return self._decode(response)

    @staticmethod
    def _decode(response: httpx2.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SlurmApiError(
                502, [], f"slurmrestd returned a non-JSON body (HTTP {response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise SlurmApiError(502, [], "slurmrestd returned an unexpected JSON shape")
        errors = payload.get("errors") or []
        if response.status_code >= 400 or errors:
            raise SlurmApiError(
                _map_slurm_status(response.status_code, errors), errors, _describe_errors(errors)
            )
        return payload

    # -- endpoints -------------------------------------------------------------

    def _slurm(self, suffix: str) -> str:
        return f"/slurm/{self.version}/{suffix}"

    def _slurmdb(self, suffix: str) -> str:
        return f"/slurmdb/{self.version}/{suffix}"

    async def ping(self) -> dict[str, Any]:
        return await self.request("GET", self._slurm("ping"))

    async def diag(self) -> dict[str, Any]:
        return await self.request("GET", self._slurm("diag"))

    async def partitions(self) -> dict[str, Any]:
        return await self.request("GET", self._slurm("partitions"))

    async def nodes(self) -> dict[str, Any]:
        return await self.request("GET", self._slurm("nodes"))

    async def jobs(self) -> dict[str, Any]:
        return await self.request("GET", self._slurm("jobs"))

    async def job(self, job_id: int) -> dict[str, Any]:
        return await self.request("GET", self._slurm(f"job/{int(job_id)}"))

    async def submit(self, job: Mapping[str, Any], script: str) -> dict[str, Any]:
        return await self.request(
            "POST", self._slurm("job/submit"), json={"job": dict(job), "script": script}
        )

    async def cancel(self, job_id: int, *, signal: str | None = None) -> dict[str, Any]:
        params = {"signal": signal} if signal else None
        return await self.request("DELETE", self._slurm(f"job/{int(job_id)}"), params=params)

    async def accounting_jobs(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return await self.request("GET", self._slurmdb("jobs"), params=params)

    async def accounting_job(self, job_id: int) -> dict[str, Any]:
        """One slurmdbd record; an unknown id answers an empty ``jobs`` list, not an error."""
        return await self.request("GET", self._slurmdb(f"job/{int(job_id)}"))


def _describe_errors(errors: list[Any]) -> str:
    parts: list[str] = []
    for item in errors:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or item.get("error") or "").strip()
        source = str(item.get("source") or "").strip()
        number = item.get("error_number")
        text = description or "Slurm returned an error"
        if source:
            text = f"{text} ({source})"
        if number is not None:
            text = f"{text} [rc {number}]"
        parts.append(text)
    return "; ".join(parts) or "Slurm returned an error"


#: Slurm error numbers slurmrestd reports for "no such job" lookups and cancels.
_SLURM_INVALID_JOB_ID_ERRORS = {2017}  # ESLURM_INVALID_JOB_ID


def _map_slurm_status(http_status: int, errors: list[Any]) -> int:
    """Pick the HTTP status the GCO API answers with for a Slurm-side failure."""
    numbers = {
        item.get("error_number")
        for item in errors
        if isinstance(item, dict) and isinstance(item.get("error_number"), int)
    }
    if numbers & _SLURM_INVALID_JOB_ID_ERRORS or http_status == 404:
        return 404
    if http_status == 400 or any(
        "invalid" in str(item.get("description", "")).lower()
        or "partition" in str(item.get("description", "")).lower()
        for item in errors
        if isinstance(item, dict)
    ):
        return 400
    return 502


def http_exception_for(error: Exception, *, context: str = "talking to Slurm") -> HTTPException:
    """Translate a client error into the HTTPException the route raises.

    Anything that is not one of this module's errors is a bug or an
    unexpected transport failure; it becomes a 500 whose detail names only
    the operation (``context``), never the exception text.
    """
    if isinstance(error, SlurmDisabledError):
        return HTTPException(status_code=404, detail=DISABLED_DETAIL)
    if isinstance(error, SlurmUnavailableError):
        detail = TOKEN_UNAVAILABLE_DETAIL if "Secret" in str(error) else UNAVAILABLE_DETAIL
        return HTTPException(status_code=503, detail=detail)
    if isinstance(error, SlurmApiError):
        return HTTPException(status_code=error.status_code, detail=error.detail)
    return HTTPException(status_code=500, detail=f"Internal error while {context}")


_client: SlurmRestClient | None = None


def get_client() -> SlurmRestClient:
    """The process-wide client; raises :class:`SlurmDisabledError` when Slurm is off."""
    global _client
    if not slurm_enabled():
        raise SlurmDisabledError(DISABLED_DETAIL)
    if _client is None:
        _client = SlurmRestClient()
    return _client


def reset_client() -> None:
    """Drop the cached client (tests, configuration reloads)."""
    global _client
    _client = None
