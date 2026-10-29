"""The registry of Slurm backends behind ``/api/v1/slurm``: build one, cache one per backend.

:class:`gco.services.slurm_client.SlurmRestClient` speaks slurmrestd's HTTP
API; what differs between a Slurm cluster GCO runs on the EKS cluster
(Slinky, ``helm.slurm``) and one AWS runs for it (AWS PCS, ``pcs``) is only
where the daemon listens and what credential it accepts. A
:class:`~gco.services.slurm_client.SlurmBackend` answers exactly those
questions, plus the defaults a submission needs when the caller leaves them
out (partition, working directory) and a description for
``GET /api/v1/slurm/backends``.

- :class:`~gco.services.slurm_client.SlinkyBackend`: the in-cluster
  ``slinky-slurm-restapi`` Service and the JWT the slurm-operator mints into a
  Kubernetes Secret for the API's Slurm identity (``slurm``, SlurmUser), sent
  with the impersonation header Slinky's slurmrestd accepts.
- :class:`~gco.services.pcs_backend.PcsBackend`: the SLURMRESTD endpoint
  ``pcs:GetCluster`` reports, authenticated with a short-lived HS256 JWT the
  API mints from the signing key PCS keeps in Secrets Manager, for the fixed
  POSIX identity in ``pcs.identity``. PCS does not accept the impersonation
  header, so only the token travels.

Both may be enabled in one region (``GCO_SLURM_BACKENDS``); the client layer
resolves which one a request means
(:func:`gco.services.slurm_client.resolve_backend`) and this module turns the
name into the process-wide client for it (:func:`get_client`).

The import graph is one-directional: the contract and the Slinky backend live
in :mod:`gco.services.slurm_client`, :mod:`gco.services.pcs_backend` builds
on that module, and this registry imports both. ``BackendDefaults``,
``SlurmBackend`` and ``SlinkyBackend`` are re-exported here for callers that
think in terms of the seam.
"""

from __future__ import annotations

import logging

from gco.pcs_config import PCS_BACKEND, SLINKY_BACKEND
from gco.services.pcs_backend import PcsBackend
from gco.services.slurm_client import (
    BackendDefaults,
    SlinkyBackend,
    SlurmBackend,
    SlurmRestClient,
    resolve_backend,
)

__all__ = [
    "BackendDefaults",
    "PcsBackend",
    "SlinkyBackend",
    "SlurmBackend",
    "build_backend",
    "get_client",
    "reset_client",
]

logger = logging.getLogger(__name__)


def build_backend(name: str) -> SlurmBackend:
    """Construct the named backend from the API pod's environment."""
    if name == SLINKY_BACKEND:
        return SlinkyBackend()
    if name == PCS_BACKEND:
        return PcsBackend.from_environment()
    raise ValueError(f"unknown Slurm backend {name!r}")


_clients: dict[str, SlurmRestClient] = {}


def get_client(backend: str | None = None) -> SlurmRestClient:
    """The process-wide client for a backend (see :func:`~gco.services.slurm_client.resolve_backend`).

    Raises :class:`~gco.services.slurm_client.SlurmDisabledError` when no
    backend (or the named one) is deployed and
    :class:`~gco.services.slurm_client.SlurmBackendSelectionError` when the
    caller must name one.
    """
    name = resolve_backend(backend)
    client = _clients.get(name)
    if client is None:
        client = SlurmRestClient(backend=build_backend(name))
        _clients[name] = client
    return client


def reset_client() -> None:
    """Drop the cached clients (tests, configuration reloads)."""
    _clients.clear()
