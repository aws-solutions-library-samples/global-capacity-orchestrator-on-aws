"""Single-writer election on a pre-created Kubernetes Lease.

The platform services run with two or more replicas for availability, but
several of their side effects must happen exactly once per cluster — the
ALB-hostname write to SSM, and every webhook delivery. Both elect a single
writer through a ``coordination.k8s.io`` Lease with the same rules:

* The Lease is **pre-created by the manifests** (``02-rbac.yaml``), so RBAC
  grants ``get``/``update`` on one named object instead of ``create`` on every
  Lease in the namespace. A missing Lease disables the side effect rather than
  letting two replicas perform it.
* Acquisition is a read followed by ``replace`` carrying the read's
  ``resourceVersion``; Kubernetes rejects a racing writer with HTTP 409, and
  the loser simply does not act this cycle.
* A holder whose ``renewTime`` is older than ``leaseDurationSeconds`` — or that
  never recorded one — is treated as gone and may be replaced.
* Every API or RBAC failure returns ``False``. Losing the side effect for one
  cycle is always safer than performing it twice.

Callers re-run :func:`try_acquire_lease` on every cycle; a ``True`` result is
a renewal for the current holder and an acquisition for a new one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

#: Shortest lease any caller may configure. Below this a slow API call or a
#: paused process could let a second replica win the Lease while the first
#: still believes it holds it.
LEASE_MIN_DURATION_SECONDS = 60

#: (connect, read) timeouts for the two Lease calls, kept short so an
#: unreachable API server costs one cycle, not the whole loop.
LEASE_REQUEST_TIMEOUT: tuple[int, int] = (3, 10)


@dataclass(frozen=True)
class LeaseIdentity:
    """Which Lease to hold, as whom, and for how long."""

    name: str
    namespace: str
    holder: str
    duration_seconds: int


def lease_duration_from_env(value: str | None, *, label: str) -> int:
    """Parse a configured lease duration, enforcing the shared floor."""
    configured = int(value) if value is not None else LEASE_MIN_DURATION_SECONDS
    if configured < LEASE_MIN_DURATION_SECONDS:
        logger.warning(
            "%s=%s is too short; enforcing %s seconds",
            label,
            configured,
            LEASE_MIN_DURATION_SECONDS,
        )
        return LEASE_MIN_DURATION_SECONDS
    return configured


def try_acquire_lease(
    coordination_v1: Any,
    identity: LeaseIdentity,
    *,
    label: str,
    request_timeout: tuple[int, int] = LEASE_REQUEST_TIMEOUT,
) -> bool:
    """Acquire or renew ``identity`` for its holder; ``False`` means do not act.

    ``label`` names the elected duty in log lines (for example ``"ALB-sync"``
    or ``"webhook"``) so two elections in one process stay distinguishable.
    """
    observed_at = datetime.now(UTC)

    try:
        lease = coordination_v1.read_namespaced_lease(
            identity.name,
            identity.namespace,
            _request_timeout=request_timeout,
        )
        spec = lease.spec
        current_holder = spec.holder_identity
        renew_time = spec.renew_time
        lease_duration = spec.lease_duration_seconds or identity.duration_seconds

        expired = False
        if current_holder:
            if renew_time is None:
                # A holder without a renewal timestamp cannot prove it still
                # owns the lease. Treat it as expired so the named Lease
                # cannot remain wedged indefinitely.
                expired = True
            else:
                if renew_time.tzinfo is None:
                    renew_time = renew_time.replace(tzinfo=UTC)
                expired = (observed_at - renew_time).total_seconds() >= lease_duration

        if current_holder not in (None, "", identity.holder) and not expired:
            return False

        acquiring = current_holder != identity.holder
        renewed_at = datetime.now(UTC)
        if acquiring:
            spec.holder_identity = identity.holder
            spec.acquire_time = renewed_at
            spec.lease_transitions = (spec.lease_transitions or 0) + 1
        spec.lease_duration_seconds = identity.duration_seconds
        spec.renew_time = renewed_at

        try:
            coordination_v1.replace_namespaced_lease(
                identity.name,
                identity.namespace,
                lease,
                _request_timeout=request_timeout,
            )
        except ApiException as exc:
            if exc.status == 409:
                logger.debug("Lost %s Lease race to another replica", label)
                return False
            raise

        if acquiring:
            logger.info("Acquired %s leader Lease as %s", label, identity.holder)
        return True

    except ApiException as exc:
        if exc.status == 404:
            logger.warning(
                "%s Lease %s/%s is missing; the elected duty is disabled until it is restored",
                label,
                identity.namespace,
                identity.name,
            )
        else:
            logger.warning("%s Lease check failed (non-fatal): %s", label, exc)
        return False
    except Exception as exc:
        logger.warning("%s Lease check failed (non-fatal): %s", label, exc)
        return False
