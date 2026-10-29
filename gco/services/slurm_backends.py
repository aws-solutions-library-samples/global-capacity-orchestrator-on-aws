"""The Slurm backends behind ``/api/v1/slurm``: where slurmrestd is and how to authenticate.

:class:`gco.services.slurm_client.SlurmRestClient` speaks slurmrestd's HTTP
API; what differs between a Slurm cluster GCO runs on the EKS cluster
(Slinky, ``helm.slurm``) and one AWS runs for it (AWS PCS, ``pcs``) is only
where the daemon listens and what credential it accepts. A
:class:`SlurmBackend` answers exactly those questions, plus the defaults a
submission needs when the caller leaves them out (partition, working
directory) and a description for ``GET /api/v1/slurm/backends``.

- :class:`SlinkyBackend`: the in-cluster ``slinky-slurm-restapi`` Service and
  the JWT the slurm-operator mints into a Kubernetes Secret for the API's
  Slurm identity (``slurm``, SlurmUser), sent with the impersonation header
  Slinky's slurmrestd accepts.
- :class:`PcsBackend` (:mod:`gco.services.pcs_backend`): the SLURMRESTD
  endpoint ``pcs:GetCluster`` reports, authenticated with a short-lived HS256
  JWT the API mints from the signing key PCS keeps in Secrets Manager, for the
  fixed POSIX identity in ``pcs.identity``. PCS does not accept the
  impersonation header, so only the token travels.

Both may be enabled in one region (``GCO_SLURM_BACKENDS``); the client layer
resolves which one a request means (:func:`gco.services.slurm_client.resolve_backend`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from gco.pcs_config import PCS_BACKEND, SLINKY_BACKEND
from gco.services import slurm_client
from gco.slurm_config import (
    SLURM_DEFAULT_WORKING_DIRECTORY,
    SLURM_LOGIN_NAME,
    SLURM_NAMESPACE,
    SLURM_RESTAPI_NAME,
)

logger = logging.getLogger(__name__)


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
    """The Slinky cluster on the regional EKS cluster (``helm.slurm``)."""

    name = SLINKY_BACKEND

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token_provider: slurm_client.SecretTokenProvider | None = None,
        user: str = slurm_client.SLURM_API_USER,
    ) -> None:
        self._base_url = (base_url or slurm_client.restapi_base_url()).rstrip("/")
        self.token_provider = token_provider or slurm_client.SecretTokenProvider()
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
        return slurm_client.UNAVAILABLE_DETAIL

    def api_version_detail(self, version: str) -> str:
        return slurm_client.API_VERSION_DETAIL.format(version=version)

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


def build_backend(name: str) -> SlurmBackend:
    """Construct the named backend from the API pod's environment."""
    if name == SLINKY_BACKEND:
        return SlinkyBackend()
    if name == PCS_BACKEND:
        from gco.services.pcs_backend import PcsBackend

        return PcsBackend.from_environment()
    raise ValueError(f"unknown Slurm backend {name!r}")
