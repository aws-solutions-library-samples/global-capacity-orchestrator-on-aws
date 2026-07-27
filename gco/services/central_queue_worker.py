"""Lease-fenced regional worker for the global DynamoDB Job queue."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes.client.rest import ApiException

from gco.services.manifest_processor import (
    ManifestProcessor,
    QueuedJobNotCreatedError,
    RetryableQueuedJobApplyError,
)
from gco.services.spot_price_gate import SpotPriceGate, should_persist_observation
from gco.services.structured_logging import sanitize_log_value
from gco.services.template_store import JobStatus, JobStore

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``process_queued_jobs_once`` -> ``diagrams/code_diagrams/gco/services/central_queue_worker.process_queued_jobs_once.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/central_queue_worker.process_queued_jobs_once.png``)
#   * ``reconcile_active_jobs_once`` -> ``diagrams/code_diagrams/gco/services/central_queue_worker.reconcile_active_jobs_once.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/central_queue_worker.reconcile_active_jobs_once.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED.value, JobStatus.FAILED.value})
_MAX_ERROR_LENGTH = 2_000


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_error(value: object) -> str:
    """Bound user/runtime error text before persisting it in DynamoDB."""
    text = str(value)
    return text if len(text) <= _MAX_ERROR_LENGTH else f"{text[:_MAX_ERROR_LENGTH]}...[truncated]"


def _worker_identity(region: str) -> str:
    """Return a process-unique owner including region and Kubernetes pod identity."""
    pod_identity = os.getenv("POD_UID") or os.getenv("HOSTNAME") or "local"
    return f"{region}/{pod_identity}/{uuid.uuid4().hex}"


async def _lease_heartbeat(
    store: JobStore,
    *,
    job_id: str,
    target_region: str,
    claimed_by: str,
    claim_token: str,
    claim_generation: int,
    interval_seconds: float,
    done: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    """Renew one active apply lease until completion or fencing."""
    while not done.is_set():
        try:
            await asyncio.wait_for(done.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass

        try:
            renewed = await asyncio.to_thread(
                store.renew_claim,
                job_id,
                target_region,
                claimed_by,
                claim_token,
                claim_generation,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - loss must fence the in-flight result
            logger.exception(
                "Lease renewal failed for central queue job %s",
                sanitize_log_value(job_id),
            )
            lost.set()
            return
        if not renewed:
            logger.warning(
                "Central queue job %s lost claim generation %d",
                sanitize_log_value(job_id),
                claim_generation,
            )
            lost.set()
            return


async def _stop_heartbeat(task: asyncio.Task[None], done: asyncio.Event) -> None:
    done.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _defer_price_gated_job(
    store: JobStore,
    gate: SpotPriceGate,
    queued_job: dict[str, Any],
    job_id: str,
) -> dict[str, Any] | None:
    """Return a deferral record when the job's spot price gate is closed.

    ``None`` means the job carries no gate or its gate is open — dispatch
    proceeds. Closed gates optionally persist a throttled observation so
    ``gco queue get`` can show why the job is waiting.
    """
    decision = await asyncio.to_thread(gate.evaluate, queued_job)
    if decision is None or not decision.gated:
        return None
    if should_persist_observation(queued_job):
        observed = (
            f"{decision.observed_price:.6f}" if decision.observed_price is not None else "unknown"
        )
        try:
            await asyncio.to_thread(
                store.record_spot_gate_observation,
                job_id,
                observed_price=observed,
            )
        except Exception:  # noqa: BLE001 - observations are advisory only
            logger.exception(
                "Failed to persist spot gate observation for %s",
                sanitize_log_value(job_id),
            )
    logger.info(
        "Deferring price-gated central queue job %s: %s",
        sanitize_log_value(job_id),
        decision.reason,
    )
    return {
        "job_id": job_id,
        "status": "price_gated",
        "instance_type": decision.instance_type,
        "max_spot_price": decision.max_price,
        "observed_spot_price": decision.observed_price,
        "reason": decision.reason,
    }


async def process_queued_jobs_once(
    processor: ManifestProcessor,
    store: JobStore,
    *,
    limit: int,
    owner_id: str | None = None,
    lease_renewal_seconds: float | None = None,
    stop_event: asyncio.Event | None = None,
    spot_gate: SpotPriceGate | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Claim and apply one bounded batch for the processor's region.

    Price-gated jobs whose spot cap is not currently met are deferred (left
    queued) without consuming the apply budget. To keep a run of gated
    high-priority jobs from starving dispatchable work behind them, the
    candidate fetch is wider than ``limit``; at most ``limit`` jobs are
    claimed/applied per pass.
    """
    owner = owner_id or _worker_identity(processor.region)
    renewal_seconds = lease_renewal_seconds or max(
        5.0,
        min(float(store.claim_lease_seconds) / 3.0, 60.0),
    )
    gate = spot_gate or SpotPriceGate(processor.region)
    migration = await asyncio.to_thread(
        store.migrate_legacy_records_for_region,
        processor.region,
        max(limit * 20, 100),
    )
    migrated = int(migration.get("migrated", 0))
    migration_failed = int(migration.get("failed", 0))
    if migrated or migration_failed:
        logger.warning(
            "Central queue migration for %s: backfilled=%d safely_failed=%d complete=%s",
            processor.region,
            migrated,
            migration_failed,
            migration.get("complete", False),
        )
    fetch_limit = min(max(limit * 4, 20), 100)
    queued_jobs = await asyncio.to_thread(
        store.get_queued_jobs_for_region,
        processor.region,
        fetch_limit,
    )
    processed: list[dict[str, Any]] = []
    attempted = 0

    for queued_job in queued_jobs:
        if stop_event is not None and stop_event.is_set():
            break
        if attempted >= limit:
            break
        job_id = str(queued_job.get("job_id", ""))
        if not job_id:
            logger.error("Ignoring central queue record without a job_id")
            continue

        deferral = await _defer_price_gated_job(store, gate, queued_job, job_id)
        if deferral is not None:
            processed.append(deferral)
            continue
        attempted += 1

        claimed: dict[str, Any] | None = None
        try:
            claimed = await asyncio.to_thread(
                store.claim_job,
                job_id,
                processor.region,
                owner,
            )
            if not claimed:
                continue
            claim_token = str(claimed.get("claim_token") or "")
            claim_generation = int(claimed.get("claim_generation", 0))
            if not claim_token or claim_generation <= 0:
                raise RuntimeError("JobStore returned an incomplete fenced claim")

            applying = await asyncio.to_thread(
                store.transition_job,
                job_id,
                target_region=processor.region,
                expected_status=JobStatus.CLAIMED,
                status=JobStatus.APPLYING,
                message="Applying deterministic Kubernetes Job",
                claimed_by=owner,
                claim_token=claim_token,
                claim_generation=claim_generation,
            )
            if not applying:
                processed.append({"job_id": job_id, "status": "fenced"})
                continue

            manifest = claimed.get("manifest")
            namespace = claimed.get("namespace")
            if not isinstance(manifest, dict) or not isinstance(namespace, str):
                raise QueuedJobNotCreatedError(
                    "Queued job contains an invalid manifest or namespace"
                )

            heartbeat_done = asyncio.Event()
            claim_lost = asyncio.Event()
            heartbeat = asyncio.create_task(
                _lease_heartbeat(
                    store,
                    job_id=job_id,
                    target_region=processor.region,
                    claimed_by=owner,
                    claim_token=claim_token,
                    claim_generation=claim_generation,
                    interval_seconds=renewal_seconds,
                    done=heartbeat_done,
                    lost=claim_lost,
                ),
                name=f"central-queue-lease-{job_id}",
            )
            try:
                resource = await asyncio.to_thread(
                    processor.apply_queued_job,
                    manifest,
                    namespace,
                    job_id,
                )
            finally:
                await _stop_heartbeat(heartbeat, heartbeat_done)

            if claim_lost.is_set():
                processed.append({"job_id": job_id, "status": "fenced"})
                continue

            pending = await asyncio.to_thread(
                store.transition_job,
                job_id,
                target_region=processor.region,
                expected_status=JobStatus.APPLYING,
                status=JobStatus.PENDING,
                message="Applied to Kubernetes, waiting for scheduling",
                k8s_job_name=resource.name,
                k8s_job_namespace=resource.namespace,
                k8s_job_uid=resource.uid,
                claimed_by=owner,
                claim_token=claim_token,
                claim_generation=claim_generation,
            )
            if pending:
                processed.append(
                    {
                        "job_id": job_id,
                        "status": "applied",
                        "k8s_job_name": resource.name,
                        "k8s_job_uid": resource.uid,
                    }
                )
            else:
                processed.append({"job_id": job_id, "status": "fenced"})
        except RetryableQueuedJobApplyError as exc:
            # The API may have accepted a deterministic create before the
            # response was lost. Keep APPLYING fenced and let lease recovery
            # return it to QUEUED, where the next attempt adopts by full queue ID.
            error = _bounded_error(exc)
            logger.warning(
                "Deferring central queue job %s after an inconclusive Kubernetes result",
                sanitize_log_value(job_id),
            )
            processed.append({"job_id": job_id, "status": "retryable", "error": error})
        except Exception as exc:  # noqa: BLE001 - isolate malformed/permanent records
            error = _bounded_error(exc)
            logger.exception(
                "Failed to process central queue job %s",
                sanitize_log_value(job_id),
            )
            claim = claimed
            token = claim.get("claim_token") if isinstance(claim, dict) else None
            generation = claim.get("claim_generation") if isinstance(claim, dict) else None
            if token and generation:
                transition_options: dict[str, Any] = {}
                if isinstance(exc, QueuedJobNotCreatedError):
                    transition_options["workload_not_created"] = True
                try:
                    failed = await asyncio.to_thread(
                        store.transition_job,
                        job_id,
                        target_region=processor.region,
                        expected_status=JobStatus.APPLYING,
                        status=JobStatus.FAILED,
                        message="Failed to apply deterministic Kubernetes Job",
                        error=error,
                        claimed_by=owner,
                        claim_token=str(token),
                        claim_generation=int(generation),
                        **transition_options,
                    )
                except Exception:  # noqa: BLE001 - lease recovery remains the fallback
                    logger.exception(
                        "Unable to persist failure for central queue job %s",
                        sanitize_log_value(job_id),
                    )
                    failed = None
                processed.append(
                    {"job_id": job_id, "status": "failed" if failed else "fenced", "error": error}
                )
            else:
                processed.append({"job_id": job_id, "status": "fenced", "error": error})

    return len(queued_jobs), processed


def _observed_job_state(job: Any) -> tuple[str, str | None]:
    """Map a Kubernetes Job object to the central queue lifecycle."""
    status = job.status
    for condition in status.conditions or []:
        if condition.type == "Complete" and condition.status == "True":
            return JobStatus.SUCCEEDED.value, None
        if condition.type == "Failed" and condition.status == "True":
            detail = condition.message or condition.reason or "Kubernetes Job failed"
            return JobStatus.FAILED.value, _bounded_error(detail)
    if (status.active or 0) > 0:
        return JobStatus.RUNNING.value, None
    return JobStatus.PENDING.value, None


async def reconcile_active_jobs_once(
    processor: ManifestProcessor,
    store: JobStore,
    *,
    limit: int,
    stop_event: asyncio.Event | None = None,
) -> int:
    """Reconcile pending/running queue records with their exact Kubernetes Jobs."""
    jobs = await asyncio.to_thread(store.get_active_jobs_for_region, processor.region, limit)
    transitions = 0

    for queued_job in jobs:
        if stop_event is not None and stop_event.is_set():
            break
        job_id = str(queued_job.get("job_id", ""))
        job_name = queued_job.get("k8s_job_name")
        namespace = queued_job.get("k8s_job_namespace")
        expected_uid = str(queued_job.get("k8s_job_uid") or "")
        current = str(queued_job.get("status") or "")
        if (
            not job_id
            or not isinstance(job_name, str)
            or not isinstance(namespace, str)
            or not expected_uid
            or current not in {JobStatus.PENDING.value, JobStatus.RUNNING.value}
        ):
            logger.error(
                "Ignoring active queue record %s with incomplete Kubernetes identity", job_id
            )
            continue

        observed: str
        error: str | None
        try:
            k8s_job = await asyncio.to_thread(
                processor.read_queued_job,
                job_name,
                namespace,
            )
        except ApiException as exc:
            if exc.status == 404:
                observed, error = JobStatus.FAILED.value, "Kubernetes Job no longer exists"
            else:
                logger.warning(
                    "Unable to reconcile central queue job %s: %s",
                    sanitize_log_value(job_id),
                    exc,
                )
                continue
        except Exception as exc:  # noqa: BLE001 - reconciliation is best-effort per record
            logger.warning(
                "Unable to reconcile central queue job %s: %s",
                sanitize_log_value(job_id),
                exc,
            )
            continue
        else:
            actual_uid = str(getattr(k8s_job.metadata, "uid", "") or "")
            if actual_uid != expected_uid:
                observed, error = JobStatus.FAILED.value, "Kubernetes Job identity changed"
            else:
                observed, error = _observed_job_state(k8s_job)

        if observed == current:
            continue
        if observed not in {
            JobStatus.PENDING.value,
            JobStatus.RUNNING.value,
            *_TERMINAL_STATUSES,
        }:
            continue
        if observed not in {
            status.value
            for status in (
                JobStatus.RUNNING,
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
            )
        }:
            # Running Jobs never regress to pending.
            continue

        message = {
            JobStatus.RUNNING.value: "Kubernetes Job is running",
            JobStatus.SUCCEEDED.value: "Kubernetes Job completed successfully",
            JobStatus.FAILED.value: "Kubernetes Job failed",
        }[observed]
        updated = await asyncio.to_thread(
            store.transition_job,
            job_id,
            target_region=processor.region,
            expected_status=current,
            status=observed,
            message=message,
            error=error,
            expected_k8s_uid=expected_uid,
        )
        if updated:
            transitions += 1

    return transitions


@dataclass
class CentralQueueWorker:
    """Continuously activate and reconcile Jobs for one regional cluster."""

    processor: ManifestProcessor
    store: JobStore
    poll_interval_seconds: float = 10.0
    batch_size: int = 5
    reconcile_limit: int = 100
    lease_renewal_seconds: float | None = None
    owner_id: str | None = None

    def __post_init__(self) -> None:
        self.poll_interval_seconds = min(max(float(self.poll_interval_seconds), 1.0), 300.0)
        self.batch_size = min(max(int(self.batch_size), 1), 20)
        self.reconcile_limit = min(max(int(self.reconcile_limit), 1), 500)
        default_renewal = max(5.0, min(float(self.store.claim_lease_seconds) / 3.0, 60.0))
        requested_renewal = (
            default_renewal
            if self.lease_renewal_seconds is None
            else float(self.lease_renewal_seconds)
        )
        self.lease_renewal_seconds = min(
            max(requested_renewal, 1.0),
            max(float(self.store.claim_lease_seconds) / 2.0, 1.0),
        )
        self.owner_id = self.owner_id or _worker_identity(self.processor.region)
        self._stop_event = asyncio.Event()
        # One gate per worker so its price cache spans polling passes.
        self._spot_gate = SpotPriceGate(self.processor.region)
        self.running = False
        self.stopping = False
        self.last_pass_started_at: str | None = None
        self.last_successful_pass_at: str | None = None
        self.last_error: str | None = None

    def stop(self) -> None:
        """Stop accepting new records and finish the current bounded SDK call."""
        self.stopping = True
        self._stop_event.set()

    def health(self) -> dict[str, Any]:
        """Return operator-safe worker health without exposing claim tokens."""
        return {
            "enabled": True,
            "running": self.running,
            "stopping": self.stopping,
            "owner": self.owner_id,
            "last_pass_started_at": self.last_pass_started_at,
            "last_successful_pass_at": self.last_successful_pass_at,
            "last_error": self.last_error,
        }

    async def run(self) -> None:
        """Run until stopped; isolate pass failures and keep polling."""
        self.running = True
        logger.info(
            "Central queue worker started for %s (interval=%ss, batch=%d)",
            self.processor.region,
            self.poll_interval_seconds,
            self.batch_size,
        )
        try:
            while not self._stop_event.is_set():
                self.last_pass_started_at = _utc_now()
                try:
                    recovered = await asyncio.to_thread(
                        self.store.requeue_expired_jobs,
                        self.processor.region,
                        self.reconcile_limit,
                    )
                    if self._stop_event.is_set():
                        break
                    polled, processed = await process_queued_jobs_once(
                        self.processor,
                        self.store,
                        limit=self.batch_size,
                        owner_id=self.owner_id,
                        lease_renewal_seconds=self.lease_renewal_seconds,
                        stop_event=self._stop_event,
                        spot_gate=self._spot_gate,
                    )
                    transitions = 0
                    if not self._stop_event.is_set():
                        transitions = await reconcile_active_jobs_once(
                            self.processor,
                            self.store,
                            limit=self.reconcile_limit,
                            stop_event=self._stop_event,
                        )
                    self.last_successful_pass_at = _utc_now()
                    self.last_error = None
                    if recovered or polled or transitions:
                        logger.info(
                            "Central queue pass: recovered=%d polled=%d processed=%d transitions=%d",
                            recovered,
                            polled,
                            len(processed),
                            transitions,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - transient pass failures are retried
                    self.last_error = _bounded_error(exc)
                    logger.exception("Central queue worker pass failed")

                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.poll_interval_seconds,
                    )
        finally:
            self.running = False
            logger.info("Central queue worker stopped for %s", self.processor.region)
