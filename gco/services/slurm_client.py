"""slurmrestd client behind the GCO API's ``/api/v1/slurm`` routes.

The manifest-processor (the authenticated API ingress) talks to Slurm through
slurmrestd, the Slurm REST daemon. Two backends can stand behind the same
routes; this module defines the contract (:class:`SlurmBackend`) and the
Slinky implementation, :mod:`gco.services.pcs_backend` the PCS one, and
:mod:`gco.services.slurm_backends` the registry that builds and caches one
client per enabled backend:

- **Slinky** (``helm.slurm``): the chart runs slurmrestd in ``gco-jobs``
  (``gco.slurm_config.SLURM_RESTAPI_NAME``). Authentication is Slurm's own
  ``auth/jwt``: the slurm-operator mints a JWT for the API's Slurm identity
  into a Kubernetes Secret through a ``Token`` custom resource
  (post-helm-slurm-api-access.yaml) and refreshes it before it expires;
  :class:`SecretTokenProvider` reads that Secret (a scoped RBAC grant on
  exactly that one Secret) and the backend sends it as ``X-SLURM-USER-TOKEN``
  beside ``X-SLURM-USER-NAME``. The API never sees the cluster's JWT signing
  key.
- **AWS PCS** (``pcs``): the managed controller's slurmrestd in the regional
  VPC; the API mints its own short-lived JWT from the signing key PCS keeps in
  Secrets Manager (:mod:`gco.services.pcs_backend`).

Either hop is plain HTTP inside a boundary the deployment draws (the
NetworkPolicies in post-helm-slurm-network.yaml and post-helm-pcs-network.yaml
admit only the manifest-processor pods to the REST port; PCS's security group
admits only the EKS cluster); slurmrestd has no TLS listener of its own, and
the ALB-facing side of the API is the TLS boundary callers see.

``GCO_SLURM_BACKENDS`` on the API pod names the enabled backends
(``slinky``, ``pcs``, or both); a request names one with ``backend=`` only
when both are on (:func:`resolve_backend`). Errors map to the API the same
way the cost proxy's do: no backend enabled, or the named one off ->
404 with a remedy; slurmrestd unreachable or the credential unavailable ->
503; an ambiguous or unknown backend name -> 400; a Slurm-side rejection ->
the status slurmrestd meant (404 for an unknown job, 400 for a bad
submission, 502 for anything else).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2
from fastapi import HTTPException

from gco.pcs_config import PCS_BACKEND, SLINKY_BACKEND, SLURM_BACKENDS
from gco.services import tracing
from gco.slurm_config import (
    SLURM_API_VERSION,
    SLURM_DEFAULT_WORKING_DIRECTORY,
    SLURM_LOGIN_NAME,
    SLURM_NAMESPACE,
    SLURM_RESTAPI_NAME,
    SLURM_RESTAPI_PORT,
)

logger = logging.getLogger(__name__)

#: ``SLURM_API_VERSION`` (the slurmrestd plugin version the paths below carry)
#: lives in :mod:`gco.slurm_config` beside the image tag it moves with; it is
#: re-exported here because this module is where the version is used.

#: The Slurm identity the API acts as toward Slinky. ``slurm`` is SlurmUser:
#: it may submit, inspect and cancel any job, which is what a single
#: authenticated front door needs; the caller's identity travels in each job's
#: ``comment`` for accounting attribution. (PCS uses the configured
#: ``pcs.identity`` instead; see :mod:`gco.services.pcs_backend`.)
SLURM_API_USER = "slurm"

#: The Token custom resource and the Secret it writes (post-helm-slurm-api-access.yaml).
SLURM_API_TOKEN_NAME = "slinky-slurm-token-gco-api"  # nosec B105  # object name, not a credential
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

#: The API pod's backend list (``{{SLURM_BACKENDS}}`` in 31-manifest-processor.yaml).
BACKENDS_ENV = "GCO_SLURM_BACKENDS"
#: The pre-PCS boolean; still honored so an API pod rendered by an older
#: manifest keeps serving Slinky.
LEGACY_ENABLED_ENV = "GCO_SLURM_ENABLED"

DISABLED_DETAIL = (
    "Slurm is not enabled in this region. Set helm.slurm.enabled (Slinky on the EKS cluster) "
    "or pcs.enabled (AWS PCS) to true in cdk.json (or deploy with --enable slurm / "
    "--enable pcs) and redeploy the regional stack."
)
BACKEND_DISABLED_DETAILS: dict[str, str] = {
    SLINKY_BACKEND: (
        "The Slinky Slurm cluster is not enabled in this region. Set helm.slurm.enabled to "
        "true in cdk.json (or deploy with --enable slurm) and redeploy the regional stack."
    ),
    PCS_BACKEND: (
        "AWS PCS is not enabled in this region. Set pcs.enabled to true in cdk.json (or "
        "deploy with --enable pcs) and redeploy the regional stack."
    ),
}
UNAVAILABLE_DETAIL = (
    "Slurm's REST API is unavailable in this region. Check the slinky-slurm-restapi "
    "Deployment and the slinky-slurm-controller StatefulSet in gco-jobs."
)
TOKEN_UNAVAILABLE_DETAIL = (
    "The GCO API's Slurm credential is unavailable. Check the Token "
    f"{SLURM_API_TOKEN_NAME} in gco-jobs and the slurm-operator logs."
)
#: slurmrestd answered 404 for ``/slurm/<version>/ping``, an endpoint every
#: plugin version serves: the deployed image does not ship the version this
#: API addresses (``{version}`` is filled in from the client).
API_VERSION_DETAIL = (
    "Slurm's REST API does not serve OpenAPI plugin version {version}: slurmrestd "
    "answered 404 for /slurm/{version}/ping. The slurmrestd image in charts.yaml and "
    "SLURM_API_VERSION in gco/slurm_config.py (or the SLURM_API_VERSION override on the "
    "API pod) have drifted apart; `slurmrestd -d list` inside the image names the "
    "versions it ships."
)


class SlurmDisabledError(Exception):
    """No Slurm backend is deployed here, or the one the caller named is not.

    ``detail`` is the enablement remedy for the backend the caller named;
    without one :func:`http_exception_for` answers the generic remedy that
    names both backends.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class SlurmBackendSelectionError(Exception):
    """The caller must (or must not) name a backend: ambiguous or unknown ``backend=``."""


class SlurmUnavailableError(Exception):
    """slurmrestd or the API's credential cannot be reached right now.

    ``detail`` is the operator-facing remedy the API answers with when the
    cause is known precisely; :func:`http_exception_for` falls back to the
    generic transport / credential remedies otherwise.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class SlurmApiError(Exception):
    """slurmrestd answered with an error for the request.

    ``status_code`` is the status the GCO API answers with (see
    :func:`_map_slurm_status`); ``http_status`` is what slurmrestd itself
    answered, kept for callers that need the raw transport fact.
    """

    def __init__(
        self,
        status_code: int,
        errors: list[Any],
        detail: str,
        *,
        http_status: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.errors = errors
        self.detail = detail
        self.http_status = http_status


def enabled_backends() -> list[str]:
    """The backends this API pod serves, in canonical order.

    ``GCO_SLURM_BACKENDS`` is the comma-separated list the regional stack
    renders from the two enablement flags; when it is absent the legacy
    ``GCO_SLURM_ENABLED=true`` means Slinky alone. Unknown names are ignored
    with a warning rather than failing every request: the manifest is
    GCO-rendered, so a bad name is a deployment bug the log should show once.
    """
    raw = os.getenv(BACKENDS_ENV)
    if raw is None:
        if os.getenv(LEGACY_ENABLED_ENV, "false").strip().lower() == "true":
            return [SLINKY_BACKEND]
        return []
    requested = {part.strip().lower() for part in raw.split(",") if part.strip()}
    unknown = sorted(requested - set(SLURM_BACKENDS))
    if unknown:
        logger.warning("%s names unknown Slurm backend(s): %s", BACKENDS_ENV, ", ".join(unknown))
    return [name for name in SLURM_BACKENDS if name in requested]


def slurm_enabled() -> bool:
    """Whether any Slurm backend is deployed in this region."""
    return bool(enabled_backends())


def resolve_backend(name: str | None) -> str:
    """Decide which backend a request means.

    Explicit ``name``: that backend, or 404 (:class:`SlurmDisabledError`)
    with its enablement remedy when it is off, or 400 when it is not a backend
    GCO knows. Omitted: the single enabled backend; with both enabled the
    caller must choose (400), because silently picking one would send jobs to
    a cluster the caller did not mean.
    """
    enabled = enabled_backends()
    if name is not None:
        candidate = name.strip().lower()
        if candidate not in SLURM_BACKENDS:
            raise SlurmBackendSelectionError(
                f"backend must be one of {', '.join(SLURM_BACKENDS)}, got {name!r}"
            )
        if candidate not in enabled:
            raise SlurmDisabledError(
                f"Slurm backend {candidate!r} is not enabled",
                detail=BACKEND_DISABLED_DETAILS[candidate],
            )
        return candidate
    if not enabled:
        raise SlurmDisabledError(DISABLED_DETAIL)
    if len(enabled) > 1:
        raise SlurmBackendSelectionError(
            "Both Slurm backends are enabled in this region; pass backend=slinky or "
            "backend=pcs to say which cluster you mean (GET /api/v1/slurm/backends lists them)."
        )
    return enabled[0]


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


# ---------------------------------------------------------------------------
# The backend contract the client speaks through
# ---------------------------------------------------------------------------
# Defined here, beside the client, so the layering stays one-directional:
# this module knows nothing about the concrete PCS backend, and
# :mod:`gco.services.slurm_backends` (the registry of enabled backends) and
# :mod:`gco.services.pcs_backend` import *from* here, never the other way.


@dataclass(frozen=True)
class BackendDefaults:
    """What a submission falls back to when the caller leaves a field out.

    ``partition`` ``None`` means "let slurmctld apply its own default
    partition"; ``working_directory`` is where ``slurm-<jobid>.out`` lands and
    must exist on every compute node (the shared ``/home`` on both backends).
    """

    partition: str | None
    working_directory: str


class SlurmBackend(Protocol):
    """One place slurmrestd lives and one way to authenticate to it."""

    #: ``slinky`` or ``pcs``: the ``backend`` query parameter value.
    name: str
    #: Submission defaults for this backend.
    defaults: BackendDefaults

    async def base_url(self) -> str:
        """``http://host:port`` of slurmrestd, resolved if the backend discovers it."""

    async def auth_headers(self) -> dict[str, str]:
        """The headers that authenticate one request (never logged)."""

    def invalidate(self) -> None:
        """Forget cached credentials after a 401 so the next request re-fetches them."""

    def on_transport_error(self) -> None:
        """Forget cached discovery after a connection failure (the endpoint may have moved)."""

    def endpoint_hint(self) -> str:
        """The best-known endpoint for log and error messages (never a credential)."""

    def unavailable_detail(self) -> str:
        """The operator-facing remedy when slurmrestd cannot be reached."""

    def api_version_detail(self, version: str) -> str:
        """The remedy when slurmrestd does not serve the pinned OpenAPI plugin version."""

    async def describe(self) -> dict[str, Any]:
        """Facts for ``/backends`` and ``/status``: what cluster this is and where."""


class SlinkyBackend:
    """The Slinky cluster on the regional EKS cluster (``helm.slurm``).

    The in-cluster ``slinky-slurm-restapi`` Service and the JWT the
    slurm-operator mints into a Kubernetes Secret for the API's Slurm identity
    (``slurm``, SlurmUser), sent with the impersonation header Slinky's
    slurmrestd accepts.
    """

    name = SLINKY_BACKEND

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token_provider: SecretTokenProvider | None = None,
        user: str = SLURM_API_USER,
    ) -> None:
        self._base_url = (base_url or restapi_base_url()).rstrip("/")
        self.token_provider = token_provider or SecretTokenProvider()
        self.user = user
        self.defaults = BackendDefaults(
            partition=None, working_directory=SLURM_DEFAULT_WORKING_DIRECTORY
        )

    async def base_url(self) -> str:
        return self._base_url

    async def auth_headers(self) -> dict[str, str]:
        token = await self.token_provider.token()
        return {"X-SLURM-USER-NAME": self.user, "X-SLURM-USER-TOKEN": token}

    def invalidate(self) -> None:
        self.token_provider.invalidate()

    def on_transport_error(self) -> None:
        """The in-cluster Service name is static; nothing to re-discover."""

    def endpoint_hint(self) -> str:
        return self._base_url

    def unavailable_detail(self) -> str:
        return UNAVAILABLE_DETAIL

    def api_version_detail(self, version: str) -> str:
        return API_VERSION_DETAIL.format(version=version)

    async def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": "slinky",
            "description": "Slinky slurm-operator cluster on the regional EKS cluster",
            "endpoint": self._base_url,
            "restapi_service": f"{SLURM_RESTAPI_NAME}.{SLURM_NAMESPACE}",
            "login": {"kind": "pod", "namespace": SLURM_NAMESPACE, "deployment": SLURM_LOGIN_NAME},
            "default_partition": self.defaults.partition,
            "working_directory": self.defaults.working_directory,
        }


class SlurmRestClient:
    """A thin, typed wrapper over the slurmrestd endpoints the API exposes.

    ``backend`` says where slurmrestd is and how to authenticate; the keyword
    arguments ``base_url``, ``token_provider`` and ``user`` build a
    :class:`SlinkyBackend` when no backend is given, which keeps the
    one-cluster call shape working.
    """

    def __init__(
        self,
        *,
        backend: SlurmBackend | None = None,
        base_url: str | None = None,
        version: str | None = None,
        token_provider: SecretTokenProvider | None = None,
        user: str = SLURM_API_USER,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if backend is None:
            backend = SlinkyBackend(base_url=base_url, token_provider=token_provider, user=user)
        self.backend = backend
        self.version = version or api_version()
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        """The best-known slurmrestd address (for messages; the request resolves it live)."""
        return self.backend.endpoint_hint()

    # -- transport -----------------------------------------------------------

    async def _send(
        self, method: str, path: str, *, params: Mapping[str, Any] | None, json: Any
    ) -> httpx2.Response:
        headers = await self.backend.auth_headers()
        base_url = await self.backend.base_url()
        url = f"{base_url}{path}"
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
            self.backend.on_transport_error()
            raise SlurmUnavailableError(
                f"slurmrestd unreachable at {base_url}",
                detail=self.backend.unavailable_detail(),
            ) from exc

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        """Send one request; retry once with a fresh credential on 401; decode the body."""
        response = await self._send(method, path, params=params, json=json)
        if response.status_code == 401:
            self.backend.invalidate()
            response = await self._send(method, path, params=params, json=json)
        return self._decode(response)

    @staticmethod
    def _decode(response: httpx2.Response) -> dict[str, Any]:
        status = response.status_code
        try:
            payload = response.json()
        except ValueError as exc:
            raise SlurmApiError(
                502, [], f"slurmrestd returned a non-JSON body (HTTP {status})", http_status=status
            ) from exc
        if not isinstance(payload, dict):
            raise SlurmApiError(
                502, [], "slurmrestd returned an unexpected JSON shape", http_status=status
            )
        errors = payload.get("errors") or []
        if status >= 400 or errors:
            raise SlurmApiError(
                _map_slurm_status(status, errors),
                errors,
                _describe_errors(errors),
                http_status=status,
            )
        return payload

    # -- endpoints -------------------------------------------------------------

    def _slurm(self, suffix: str) -> str:
        return f"/slurm/{self.version}/{suffix}"

    def _slurmdb(self, suffix: str) -> str:
        return f"/slurmdb/{self.version}/{suffix}"

    async def ping(self) -> dict[str, Any]:
        """``/ping`` exists in every plugin version, so a 404 here is a version mismatch.

        The status route calls this first; translating the 404 into an
        unavailable-with-remedy error is what turns a silent image/pin drift
        into one actionable message instead of a generic "not found".
        """
        try:
            return await self.request("GET", self._slurm("ping"))
        except SlurmApiError as exc:
            if exc.http_status == 404:
                raise SlurmUnavailableError(
                    f"slurmrestd at {self.base_url} does not serve {self.version}",
                    detail=self.backend.api_version_detail(self.version),
                ) from exc
            raise

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
        return HTTPException(status_code=404, detail=error.detail or DISABLED_DETAIL)
    if isinstance(error, SlurmBackendSelectionError):
        return HTTPException(status_code=400, detail=str(error))
    if isinstance(error, SlurmUnavailableError):
        detail = error.detail or (
            TOKEN_UNAVAILABLE_DETAIL if "Secret" in str(error) else UNAVAILABLE_DETAIL
        )
        return HTTPException(status_code=503, detail=detail)
    if isinstance(error, SlurmApiError):
        return HTTPException(status_code=error.status_code, detail=error.detail)
    return HTTPException(status_code=500, detail=f"Internal error while {context}")
