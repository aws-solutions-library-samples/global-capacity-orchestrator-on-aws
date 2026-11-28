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
* Another replica's hold expires when this replica has seen its Lease record
  stay unchanged for ``leaseDurationSeconds``, timed on this replica's own
  monotonic clock (client-go's ``leaderelection`` works the same way). The
  holder's ``renewTime`` is never compared with the local wall clock, so a
  clock that runs ahead cannot take a Lease that is still being renewed. A
  holder that never recorded a ``renewTime`` cannot be renewing and may be
  replaced at once.
* Every API or RBAC failure returns :attr:`LeaseOutcome.NOT_HELD`. Losing the
  side effect for one cycle is always safer than performing it twice.

A :class:`LeaseElector` remembers its observations between attempts, so each
caller keeps one per Lease and calls :meth:`LeaseElector.try_acquire` on every
cycle. The outcome says whether the attempt renewed a hold this replica
already had or acquired the Lease anew; an acquisition can follow another
replica's tenure that this one never saw, so a caller that caches state
derived from its duty rebuilds it on :attr:`LeaseOutcome.ACQUIRED`.

The expiry rule itself is :class:`LeaseObserver`. The inference monitor runs
its own election on the pre-created ``inference-monitor-leader`` Lease,
because it also fences reconciliation on an epoch the Lease carries, and
judges every record it did not write with a :class:`LeaseObserver` too.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from kubernetes.client.exceptions import ApiException

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


class LeaseOutcome(StrEnum):
    """What one election attempt did."""

    #: This replica became the holder: the Lease was free, expired, or held
    #: by another replica since this one last held it.
    ACQUIRED = "acquired"
    #: This replica already held the Lease and extended its hold.
    RENEWED = "renewed"
    #: Another replica holds the Lease, a racing writer won, or the attempt failed.
    NOT_HELD = "not-held"

    @property
    def held(self) -> bool:
        """Whether this replica holds the Lease after the attempt."""
        return self is not LeaseOutcome.NOT_HELD


def _lease_record(spec: Any) -> tuple[Any, ...]:
    """The fields a holder rewrites on every renewal, for change detection."""
    return (
        spec.holder_identity,
        spec.acquire_time,
        spec.renew_time,
        spec.lease_transitions,
        spec.lease_duration_seconds,
    )


class LeaseObserver:
    """When this replica first saw a Lease's current record, on its own monotonic clock.

    A holder renews by rewriting the record (its ``renewTime`` changes on
    every renewal), so a record this replica has seen unchanged for the lease
    duration belongs to a holder that stopped renewing. :meth:`expired`
    applies that rule to a hold this replica does not own. ``clock`` must be
    monotonic. An observer is not thread-safe: use it from one thread, or
    under the caller's own lock as :class:`LeaseElector` does.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # The Lease record as last observed, and when it first appeared (on ``clock``).
        self._record: tuple[Any, ...] | None = None
        self._first_seen_at = 0.0

    def observe(self, spec: Any) -> None:
        """Note the record ``spec`` carries, restarting the timer when it changed."""
        record = _lease_record(spec)
        if record != self._record:
            self._record = record
            self._first_seen_at = self._clock()

    def expired(self, spec: Any, duration_seconds: float) -> bool:
        """Whether the hold ``spec`` records has lapsed, for a replica that does not own it.

        Observes ``spec`` first. The hold lapses once this replica has seen
        the same record for ``duration_seconds``. A record without a
        ``renewTime`` cannot be renewing, so its hold has lapsed already.
        """
        self.observe(spec)
        if spec.renew_time is None:
            return True
        return self._clock() - self._first_seen_at >= duration_seconds


class LeaseElector:
    """This replica's side of one Lease election, remembered between attempts.

    ``label`` names the elected duty in log lines (for example ``"ALB-sync"``
    or ``"webhook"``) so two elections in one process stay distinguishable.
    ``clock`` is the monotonic clock that times how long another holder's
    record has stayed unchanged.
    """

    def __init__(
        self,
        identity: LeaseIdentity,
        *,
        label: str,
        request_timeout: tuple[int, int] = LEASE_REQUEST_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.identity = identity
        self.label = label
        self.request_timeout = request_timeout
        # The Lease record as last read or written, and when it first appeared.
        self._observer = LeaseObserver(clock)
        # Attempts run in worker threads; one at a time per elector.
        self._lock = threading.Lock()

    def try_acquire(self, coordination_v1: Any) -> LeaseOutcome:
        """Acquire or renew the Lease for this replica's holder identity."""
        with self._lock:
            return self._try_acquire(coordination_v1)

    def release(self, coordination_v1: Any) -> bool:
        """Give the Lease up if this replica holds it; ``True`` when it was released.

        Clears the holder (as client-go's ``ReleaseOnCancel`` does), so another
        replica acquires on its next attempt instead of waiting out the lease
        duration. Never touches a Lease another replica holds, and every
        failure (a racing write included) only means the Lease expires on
        its own.
        """
        with self._lock:
            identity = self.identity
            try:
                lease = coordination_v1.read_namespaced_lease(
                    identity.name, identity.namespace, _request_timeout=self.request_timeout
                )
                spec = lease.spec
                if spec.holder_identity != identity.holder:
                    return False
                spec.holder_identity = None
                spec.renew_time = datetime.now(UTC)
                coordination_v1.replace_namespaced_lease(
                    identity.name, identity.namespace, lease, _request_timeout=self.request_timeout
                )
            except Exception as exc:
                logger.warning(
                    "Could not release the %s Lease (it will expire): %s", self.label, exc
                )
                return False
            self._observer.observe(spec)
            logger.info("Released %s leader Lease held by %s", self.label, identity.holder)
            return True

    def _try_acquire(self, coordination_v1: Any) -> LeaseOutcome:
        identity = self.identity
        label = self.label
        try:
            lease = coordination_v1.read_namespaced_lease(
                identity.name,
                identity.namespace,
                _request_timeout=self.request_timeout,
            )
            spec = lease.spec
            self._observer.observe(spec)
            current_holder = spec.holder_identity
            lease_duration = spec.lease_duration_seconds or identity.duration_seconds

            held_by_another = current_holder not in (None, "", identity.holder)
            if held_by_another and not self._observer.expired(spec, lease_duration):
                return LeaseOutcome.NOT_HELD

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
                    _request_timeout=self.request_timeout,
                )
            except ApiException as exc:
                if exc.status == 409:
                    logger.debug("Lost %s Lease race to another replica", label)
                    return LeaseOutcome.NOT_HELD
                raise
            self._observer.observe(spec)

            if acquiring:
                logger.info("Acquired %s leader Lease as %s", label, identity.holder)
                return LeaseOutcome.ACQUIRED
            return LeaseOutcome.RENEWED

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
            return LeaseOutcome.NOT_HELD
        except Exception as exc:
            logger.warning("%s Lease check failed (non-fatal): %s", label, exc)
            return LeaseOutcome.NOT_HELD
