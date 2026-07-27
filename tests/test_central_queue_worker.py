"""Behavior tests for the lease-fenced central queue worker."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from kubernetes.client.rest import ApiException

import gco.services.central_queue_worker as worker_module
from gco.services.central_queue_worker import (
    CentralQueueWorker,
    _lease_heartbeat,
    _observed_job_state,
    process_queued_jobs_once,
    reconcile_active_jobs_once,
)
from gco.services.manifest_processor import (
    QueuedJobNotCreatedError,
    RetryableQueuedJobApplyError,
)
from gco.services.template_store import JobStatus

REGION = "us-east-1"
OWNER = "worker-a"
CLAIM_TOKEN = "claim-token-a"
CLAIM_GENERATION = 7


def _manifest() -> dict[str, object]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "training-job"},
    }


def _claimed_job(**overrides: object) -> dict[str, object]:
    job: dict[str, object] = {
        "job_id": "job-1",
        "manifest": _manifest(),
        "namespace": "gco-jobs",
        "claim_token": CLAIM_TOKEN,
        "claim_generation": CLAIM_GENERATION,
    }
    job.update(overrides)
    return job


def _resource() -> SimpleNamespace:
    return SimpleNamespace(name="training-job-fenced", namespace="gco-jobs", uid="uid-1")


def _condition(
    condition_type: str,
    *,
    status: str = "True",
    message: str | None = None,
    reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(type=condition_type, status=status, message=message, reason=reason)


def _k8s_job(
    *,
    uid: str = "uid-1",
    active: int = 0,
    conditions: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(uid=uid),
        status=SimpleNamespace(active=active, conditions=conditions or []),
    )


def _active_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "job_id": "job-1",
        "k8s_job_name": "training-job-fenced",
        "k8s_job_namespace": "gco-jobs",
        "k8s_job_uid": "uid-1",
        "status": JobStatus.PENDING.value,
    }
    record.update(overrides)
    return record


async def _inline_to_thread(function, /, *args, **kwargs):
    return function(*args, **kwargs)


async def _run_heartbeat(
    store: MagicMock,
    done: asyncio.Event,
    lost: asyncio.Event,
    *,
    interval_seconds: float = 0,
) -> None:
    await _lease_heartbeat(
        store,
        job_id="job-1",
        target_region=REGION,
        claimed_by=OWNER,
        claim_token=CLAIM_TOKEN,
        claim_generation=CLAIM_GENERATION,
        interval_seconds=interval_seconds,
        done=done,
        lost=lost,
    )


@pytest.fixture
def processor() -> MagicMock:
    value = MagicMock()
    value.region = REGION
    return value


@pytest.fixture
def store() -> MagicMock:
    value = MagicMock()
    value.claim_lease_seconds = 300
    value.migrate_legacy_records_for_region.return_value = {
        "evaluated": 0,
        "migrated": 0,
        "failed": 0,
        "complete": True,
    }
    value.get_queued_jobs_for_region.return_value = []
    value.get_active_jobs_for_region.return_value = []
    return value


class TestLeaseHeartbeat:
    @pytest.mark.asyncio
    async def test_done_heartbeat_exits_without_renewing(self, store):
        done = asyncio.Event()
        lost = asyncio.Event()
        asyncio.get_running_loop().call_soon(done.set)

        await _run_heartbeat(store, done, lost, interval_seconds=1)

        store.renew_claim.assert_not_called()
        assert not lost.is_set()

    @pytest.mark.asyncio
    async def test_heartbeat_renews_until_work_is_done(self, store):
        done = asyncio.Event()
        lost = asyncio.Event()

        def renew_and_finish(*args):
            done.set()
            return True

        store.renew_claim.side_effect = renew_and_finish
        with patch.object(worker_module.asyncio, "to_thread", new=_inline_to_thread):
            await _run_heartbeat(store, done, lost)

        store.renew_claim.assert_called_once_with(
            "job-1",
            REGION,
            OWNER,
            CLAIM_TOKEN,
            CLAIM_GENERATION,
        )
        assert not lost.is_set()

    @pytest.mark.asyncio
    async def test_heartbeat_fences_work_when_renewal_loses_claim(self, store):
        done = asyncio.Event()
        lost = asyncio.Event()
        store.renew_claim.return_value = False

        with patch.object(worker_module.asyncio, "to_thread", new=_inline_to_thread):
            await _run_heartbeat(store, done, lost)

        assert lost.is_set()

    @pytest.mark.asyncio
    async def test_heartbeat_fences_work_when_renewal_errors(self, store):
        done = asyncio.Event()
        lost = asyncio.Event()
        store.renew_claim.side_effect = RuntimeError("DynamoDB unavailable")

        with patch.object(worker_module.asyncio, "to_thread", new=_inline_to_thread):
            await _run_heartbeat(store, done, lost)

        assert lost.is_set()

    @pytest.mark.asyncio
    async def test_heartbeat_propagates_cancellation_without_marking_claim_lost(self, store):
        done = asyncio.Event()
        lost = asyncio.Event()
        store.renew_claim.side_effect = asyncio.CancelledError

        with (
            patch.object(worker_module.asyncio, "to_thread", new=_inline_to_thread),
            pytest.raises(asyncio.CancelledError),
        ):
            await _run_heartbeat(store, done, lost)

        assert not lost.is_set()


class TestProcessQueuedJobsOnce:
    @pytest.mark.asyncio
    async def test_successfully_applies_with_fenced_transitions(self, processor, store):
        store.get_queued_jobs_for_region.return_value = [{"job_id": "job-1"}]
        store.claim_job.return_value = _claimed_job()
        store.transition_job.side_effect = [
            {"status": JobStatus.APPLYING.value},
            {"status": JobStatus.PENDING.value},
        ]
        processor.apply_queued_job.return_value = _resource()
        heartbeat = AsyncMock()

        with patch.object(worker_module, "_lease_heartbeat", heartbeat):
            polled, processed = await process_queued_jobs_once(
                processor,
                store,
                limit=4,
                owner_id=OWNER,
                lease_renewal_seconds=12,
            )

        assert polled == 1
        assert processed == [
            {
                "job_id": "job-1",
                "status": "applied",
                "k8s_job_name": "training-job-fenced",
                "k8s_job_uid": "uid-1",
            }
        ]
        processor.apply_queued_job.assert_called_once_with(_manifest(), "gco-jobs", "job-1")
        assert store.transition_job.call_args_list == [
            call(
                "job-1",
                target_region=REGION,
                expected_status=JobStatus.CLAIMED,
                status=JobStatus.APPLYING,
                message="Applying deterministic Kubernetes Job",
                claimed_by=OWNER,
                claim_token=CLAIM_TOKEN,
                claim_generation=CLAIM_GENERATION,
            ),
            call(
                "job-1",
                target_region=REGION,
                expected_status=JobStatus.APPLYING,
                status=JobStatus.PENDING,
                message="Applied to Kubernetes, waiting for scheduling",
                k8s_job_name="training-job-fenced",
                k8s_job_namespace="gco-jobs",
                k8s_job_uid="uid-1",
                claimed_by=OWNER,
                claim_token=CLAIM_TOKEN,
                claim_generation=CLAIM_GENERATION,
            ),
        ]
        heartbeat.assert_awaited_once()
        assert heartbeat.await_args.kwargs["interval_seconds"] == 12
        assert heartbeat.await_args.kwargs["claim_token"] == CLAIM_TOKEN

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fence_at", ["applying", "pending"])
    async def test_reports_fencing_before_or_after_apply(self, processor, store, fence_at):
        store.get_queued_jobs_for_region.return_value = [{"job_id": "job-1"}]
        store.claim_job.return_value = _claimed_job()
        processor.apply_queued_job.return_value = _resource()
        heartbeat = AsyncMock()
        if fence_at == "applying":
            store.transition_job.side_effect = [None]
        else:
            store.transition_job.side_effect = [
                {"status": JobStatus.APPLYING.value},
                None,
            ]

        with patch.object(worker_module, "_lease_heartbeat", heartbeat):
            result = await process_queued_jobs_once(
                processor,
                store,
                limit=1,
                owner_id=OWNER,
                lease_renewal_seconds=10,
            )

        assert result == (1, [{"job_id": "job-1", "status": "fenced"}])
        if fence_at == "applying":
            processor.apply_queued_job.assert_not_called()
            heartbeat.assert_not_awaited()
        else:
            processor.apply_queued_job.assert_called_once()
            heartbeat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_discards_apply_result_after_heartbeat_loses_claim(self, processor, store):
        store.get_queued_jobs_for_region.return_value = [{"job_id": "job-1"}]
        store.claim_job.return_value = _claimed_job()
        store.transition_job.return_value = {"status": JobStatus.APPLYING.value}
        processor.apply_queued_job.return_value = _resource()

        async def lose_claim(*args, **kwargs):
            kwargs["lost"].set()

        with patch.object(worker_module, "_lease_heartbeat", new=lose_claim):
            result = await process_queued_jobs_once(
                processor,
                store,
                limit=1,
                owner_id=OWNER,
                lease_renewal_seconds=10,
            )

        assert result == (1, [{"job_id": "job-1", "status": "fenced"}])
        processor.apply_queued_job.assert_called_once()
        assert store.transition_job.call_count == 1

    @pytest.mark.asyncio
    async def test_skips_malformed_and_race_lost_queue_records(self, processor, store):
        store.get_queued_jobs_for_region.return_value = [
            {"namespace": "gco-jobs"},
            {"job_id": "race-lost"},
        ]
        store.claim_job.return_value = None

        with patch.object(worker_module, "_worker_identity", return_value="generated-owner"):
            result = await process_queued_jobs_once(processor, store, limit=2)

        assert result == (2, [])
        store.claim_job.assert_called_once_with("race-lost", REGION, "generated-owner")
        store.transition_job.assert_not_called()
        processor.apply_queued_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_incomplete_claim_is_fenced_without_unfenced_failure_write(
        self, processor, store
    ):
        store.get_queued_jobs_for_region.return_value = [{"job_id": "job-1"}]
        store.claim_job.return_value = _claimed_job(claim_token="", claim_generation=0)

        result = await process_queued_jobs_once(
            processor,
            store,
            limit=1,
            owner_id=OWNER,
        )

        assert result[0] == 1
        assert result[1][0]["status"] == "fenced"
        assert "incomplete fenced claim" in result[1][0]["error"]
        store.transition_job.assert_not_called()
        processor.apply_queued_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_claimed_payload_is_terminally_failed(self, processor, store):
        store.get_queued_jobs_for_region.return_value = [{"job_id": "job-1"}]
        store.claim_job.return_value = _claimed_job(manifest=["not", "a", "mapping"])
        store.transition_job.side_effect = [
            {"status": JobStatus.APPLYING.value},
            {"status": JobStatus.FAILED.value},
        ]

        polled, processed = await process_queued_jobs_once(
            processor,
            store,
            limit=1,
            owner_id=OWNER,
        )

        assert polled == 1
        assert processed == [
            {
                "job_id": "job-1",
                "status": "failed",
                "error": "Queued job contains an invalid manifest or namespace",
            }
        ]
        processor.apply_queued_job.assert_not_called()
        terminal_write = store.transition_job.call_args_list[-1]
        assert terminal_write.kwargs["expected_status"] is JobStatus.APPLYING
        assert terminal_write.kwargs["status"] is JobStatus.FAILED
        assert terminal_write.kwargs["claim_token"] == CLAIM_TOKEN
        assert terminal_write.kwargs["claim_generation"] == CLAIM_GENERATION
        assert terminal_write.kwargs["workload_not_created"] is True

    @pytest.mark.asyncio
    async def test_preflight_rejection_persists_explicit_no_workload_proof(self, processor, store):
        store.get_queued_jobs_for_region.return_value = [{"job_id": "job-1"}]
        store.claim_job.return_value = _claimed_job()
        store.transition_job.side_effect = [
            {"status": JobStatus.APPLYING.value},
            {"status": JobStatus.FAILED.value},
        ]
        processor.apply_queued_job.side_effect = QueuedJobNotCreatedError(
            "Queued Job validation failed: policy denied"
        )
        heartbeat = AsyncMock()

        with patch.object(worker_module, "_lease_heartbeat", heartbeat):
            result = await process_queued_jobs_once(
                processor,
                store,
                limit=1,
                owner_id=OWNER,
            )

        assert result == (
            1,
            [
                {
                    "job_id": "job-1",
                    "status": "failed",
                    "error": "Queued Job validation failed: policy denied",
                }
            ],
        )
        terminal_write = store.transition_job.call_args_list[-1]
        assert terminal_write.kwargs["workload_not_created"] is True
        heartbeat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retryable_apply_remains_applying_for_lease_recovery(self, processor, store):
        store.get_queued_jobs_for_region.return_value = [{"job_id": "job-1"}]
        store.claim_job.return_value = _claimed_job()
        store.transition_job.return_value = {"status": JobStatus.APPLYING.value}
        processor.apply_queued_job.side_effect = RetryableQueuedJobApplyError(
            "Kubernetes create result was inconclusive"
        )
        heartbeat = AsyncMock()

        with patch.object(worker_module, "_lease_heartbeat", heartbeat):
            result = await process_queued_jobs_once(
                processor,
                store,
                limit=1,
                owner_id=OWNER,
            )

        assert result == (
            1,
            [
                {
                    "job_id": "job-1",
                    "status": "retryable",
                    "error": "Kubernetes create result was inconclusive",
                }
            ],
        )
        assert store.transition_job.call_count == 1
        heartbeat.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure_write", "expected_status"),
        [
            pytest.param({"status": JobStatus.FAILED.value}, "failed", id="failure-recorded"),
            pytest.param(None, "fenced", id="failure-write-fenced"),
            pytest.param(RuntimeError("DynamoDB write failed"), "fenced", id="write-error"),
        ],
    )
    async def test_permanent_apply_failure_uses_only_fenced_terminal_write(
        self,
        processor,
        store,
        failure_write,
        expected_status,
    ):
        store.get_queued_jobs_for_region.return_value = [{"job_id": "job-1"}]
        store.claim_job.return_value = _claimed_job()
        store.transition_job.side_effect = [
            {"status": JobStatus.APPLYING.value},
            failure_write,
        ]
        raw_error = "apply denied: " + "x" * 2_100
        processor.apply_queued_job.side_effect = RuntimeError(raw_error)
        heartbeat = AsyncMock()

        with patch.object(worker_module, "_lease_heartbeat", heartbeat):
            polled, processed = await process_queued_jobs_once(
                processor,
                store,
                limit=1,
                owner_id=OWNER,
                lease_renewal_seconds=10,
            )

        assert polled == 1
        assert processed[0]["job_id"] == "job-1"
        assert processed[0]["status"] == expected_status
        assert processed[0]["error"].endswith("...[truncated]")
        assert processed[0]["error"] == f"{raw_error[:2_000]}...[truncated]"
        terminal_write = store.transition_job.call_args_list[-1]
        assert terminal_write.kwargs["expected_status"] is JobStatus.APPLYING
        assert terminal_write.kwargs["status"] is JobStatus.FAILED
        assert terminal_write.kwargs["error"] == processed[0]["error"]
        assert terminal_write.kwargs["claim_token"] == CLAIM_TOKEN
        assert "workload_not_created" not in terminal_write.kwargs

    @pytest.mark.asyncio
    async def test_pre_set_stop_does_not_claim_any_polled_jobs(self, processor, store):
        store.get_queued_jobs_for_region.return_value = [
            {"job_id": "job-1"},
            {"job_id": "job-2"},
        ]
        stop_event = asyncio.Event()
        stop_event.set()

        result = await process_queued_jobs_once(
            processor,
            store,
            limit=2,
            owner_id=OWNER,
            stop_event=stop_event,
        )

        assert result == (2, [])
        store.claim_job.assert_not_called()
        processor.apply_queued_job.assert_not_called()


class TestObservedJobState:
    @pytest.mark.parametrize(
        ("job", "expected"),
        [
            pytest.param(
                _k8s_job(conditions=[_condition("Complete")]),
                (JobStatus.SUCCEEDED.value, None),
                id="complete",
            ),
            pytest.param(
                _k8s_job(
                    conditions=[_condition("Failed", message=None, reason="BackoffLimitExceeded")]
                ),
                (JobStatus.FAILED.value, "BackoffLimitExceeded"),
                id="failed",
            ),
            pytest.param(
                _k8s_job(
                    active=2,
                    conditions=[_condition("Complete", status="False")],
                ),
                (JobStatus.RUNNING.value, None),
                id="active",
            ),
            pytest.param(
                _k8s_job(),
                (JobStatus.PENDING.value, None),
                id="pending",
            ),
        ],
    )
    def test_maps_kubernetes_lifecycle(self, job, expected):
        assert _observed_job_state(job) == expected


class TestReconcileActiveJobsOnce:
    @pytest.mark.asyncio
    async def test_invalid_identities_are_ignored_without_kubernetes_reads(self, processor, store):
        store.get_active_jobs_for_region.return_value = [
            _active_record(job_id=""),
            _active_record(k8s_job_name=None),
            _active_record(k8s_job_namespace=3),
            _active_record(k8s_job_uid=""),
            _active_record(status=JobStatus.SUCCEEDED.value),
        ]

        transitions = await reconcile_active_jobs_once(processor, store, limit=10)

        assert transitions == 0
        processor.read_queued_job.assert_not_called()
        store.transition_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_kubernetes_job_transitions_to_failed(self, processor, store):
        store.get_active_jobs_for_region.return_value = [_active_record()]
        processor.read_queued_job.side_effect = ApiException(status=404, reason="Not Found")
        store.transition_job.return_value = {"status": JobStatus.FAILED.value}

        transitions = await reconcile_active_jobs_once(processor, store, limit=5)

        assert transitions == 1
        store.transition_job.assert_called_once_with(
            "job-1",
            target_region=REGION,
            expected_status=JobStatus.PENDING.value,
            status=JobStatus.FAILED.value,
            message="Kubernetes Job failed",
            error="Kubernetes Job no longer exists",
            expected_k8s_uid="uid-1",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "read_error",
        [
            pytest.param(ApiException(status=503, reason="Unavailable"), id="api-error"),
            pytest.param(RuntimeError("connection reset"), id="generic-error"),
        ],
    )
    async def test_transient_read_errors_leave_queue_state_unchanged(
        self, processor, store, read_error
    ):
        store.get_active_jobs_for_region.return_value = [_active_record()]
        processor.read_queued_job.side_effect = read_error

        transitions = await reconcile_active_jobs_once(processor, store, limit=5)

        assert transitions == 0
        store.transition_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_uid_mismatch_fails_instead_of_adopting_replacement(self, processor, store):
        store.get_active_jobs_for_region.return_value = [_active_record()]
        processor.read_queued_job.return_value = _k8s_job(
            uid="replacement-uid",
            conditions=[_condition("Complete")],
        )
        store.transition_job.return_value = {"status": JobStatus.FAILED.value}

        transitions = await reconcile_active_jobs_once(processor, store, limit=5)

        assert transitions == 1
        terminal_write = store.transition_job.call_args
        assert terminal_write.kwargs["status"] == JobStatus.FAILED.value
        assert terminal_write.kwargs["error"] == "Kubernetes Job identity changed"
        assert terminal_write.kwargs["expected_k8s_uid"] == "uid-1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("current", "job", "observed", "message", "error"),
        [
            pytest.param(
                JobStatus.PENDING.value,
                _k8s_job(active=1),
                JobStatus.RUNNING.value,
                "Kubernetes Job is running",
                None,
                id="running",
            ),
            pytest.param(
                JobStatus.PENDING.value,
                _k8s_job(conditions=[_condition("Complete")]),
                JobStatus.SUCCEEDED.value,
                "Kubernetes Job completed successfully",
                None,
                id="succeeded",
            ),
            pytest.param(
                JobStatus.RUNNING.value,
                _k8s_job(conditions=[_condition("Failed", message="Pod exceeded retry limit")]),
                JobStatus.FAILED.value,
                "Kubernetes Job failed",
                "Pod exceeded retry limit",
                id="failed",
            ),
        ],
    )
    async def test_reconciles_running_and_terminal_states(
        self,
        processor,
        store,
        current,
        job,
        observed,
        message,
        error,
    ):
        store.get_active_jobs_for_region.return_value = [_active_record(status=current)]
        processor.read_queued_job.return_value = job
        store.transition_job.return_value = {"status": observed}

        transitions = await reconcile_active_jobs_once(processor, store, limit=5)

        assert transitions == 1
        store.transition_job.assert_called_once_with(
            "job-1",
            target_region=REGION,
            expected_status=current,
            status=observed,
            message=message,
            error=error,
            expected_k8s_uid="uid-1",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "current",
        [JobStatus.PENDING.value, JobStatus.RUNNING.value],
        ids=["unchanged-pending", "running-does-not-regress"],
    )
    async def test_pending_observation_never_regresses_state(self, processor, store, current):
        store.get_active_jobs_for_region.return_value = [_active_record(status=current)]
        processor.read_queued_job.return_value = _k8s_job()

        transitions = await reconcile_active_jobs_once(processor, store, limit=5)

        assert transitions == 0
        store.transition_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_transition_race_does_not_count_as_reconciled(self, processor, store):
        store.get_active_jobs_for_region.return_value = [_active_record()]
        processor.read_queued_job.return_value = _k8s_job(active=1)
        store.transition_job.return_value = None

        transitions = await reconcile_active_jobs_once(processor, store, limit=5)

        assert transitions == 0
        store.transition_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_pre_set_stop_skips_active_records(self, processor, store):
        store.get_active_jobs_for_region.return_value = [_active_record()]
        stop_event = asyncio.Event()
        stop_event.set()

        transitions = await reconcile_active_jobs_once(
            processor,
            store,
            limit=5,
            stop_event=stop_event,
        )

        assert transitions == 0
        processor.read_queued_job.assert_not_called()


class TestCentralQueueWorker:
    @pytest.mark.parametrize(
        ("settings", "claim_lease_seconds", "expected"),
        [
            pytest.param(
                {
                    "poll_interval_seconds": 0,
                    "batch_size": 0,
                    "reconcile_limit": 0,
                    "lease_renewal_seconds": 0,
                    "owner_id": "explicit-owner",
                },
                300,
                (1.0, 1, 1, 1.0, "explicit-owner"),
                id="lower-bounds",
            ),
            pytest.param(
                {
                    "poll_interval_seconds": 999,
                    "batch_size": 999,
                    "reconcile_limit": 999,
                    "lease_renewal_seconds": 999,
                    "owner_id": "explicit-owner",
                },
                30,
                (300.0, 20, 500, 15.0, "explicit-owner"),
                id="upper-bounds",
            ),
            pytest.param(
                {
                    "poll_interval_seconds": 10,
                    "batch_size": 5,
                    "reconcile_limit": 100,
                    "lease_renewal_seconds": None,
                    "owner_id": None,
                },
                300,
                (10.0, 5, 100, 60.0, "generated-owner"),
                id="default-renewal-and-owner",
            ),
        ],
    )
    def test_clamps_configuration_and_assigns_owner(self, settings, claim_lease_seconds, expected):
        processor = SimpleNamespace(region=REGION)
        store = SimpleNamespace(claim_lease_seconds=claim_lease_seconds)

        with patch.object(worker_module, "_worker_identity", return_value="generated-owner"):
            worker = CentralQueueWorker(processor=processor, store=store, **settings)

        assert (
            worker.poll_interval_seconds,
            worker.batch_size,
            worker.reconcile_limit,
            worker.lease_renewal_seconds,
            worker.owner_id,
        ) == expected

    def test_health_and_stop_expose_safe_operator_state(self):
        worker = CentralQueueWorker(
            processor=SimpleNamespace(region=REGION),
            store=SimpleNamespace(claim_lease_seconds=300),
            owner_id=OWNER,
        )

        assert worker.health() == {
            "enabled": True,
            "running": False,
            "stopping": False,
            "owner": OWNER,
            "last_pass_started_at": None,
            "last_successful_pass_at": None,
            "last_error": None,
        }

        worker.stop()

        health = worker.health()
        assert health["stopping"] is True
        assert health["running"] is False
        assert worker._stop_event.is_set()
        assert set(health) == {
            "enabled",
            "running",
            "stopping",
            "owner",
            "last_pass_started_at",
            "last_successful_pass_at",
            "last_error",
        }

    @pytest.mark.asyncio
    async def test_run_completes_one_bounded_successful_pass(self, processor, store):
        store.claim_lease_seconds = 120
        store.requeue_expired_jobs.return_value = 1
        worker = CentralQueueWorker(
            processor=processor,
            store=store,
            poll_interval_seconds=1,
            batch_size=2,
            reconcile_limit=3,
            lease_renewal_seconds=5,
            owner_id=OWNER,
        )
        process = AsyncMock(return_value=(2, [{"job_id": "job-1", "status": "applied"}]))

        async def reconcile_and_stop(*args, **kwargs):
            worker.stop()
            return 1

        reconcile = AsyncMock(side_effect=reconcile_and_stop)
        with (
            patch.object(worker_module, "process_queued_jobs_once", process),
            patch.object(worker_module, "reconcile_active_jobs_once", reconcile),
        ):
            await worker.run()

        store.requeue_expired_jobs.assert_called_once_with(REGION, 3)
        process.assert_awaited_once_with(
            processor,
            store,
            limit=2,
            owner_id=OWNER,
            lease_renewal_seconds=5,
            stop_event=worker._stop_event,
            spot_gate=worker._spot_gate,
        )
        reconcile.assert_awaited_once_with(
            processor,
            store,
            limit=3,
            stop_event=worker._stop_event,
        )
        health = worker.health()
        assert health["running"] is False
        assert health["stopping"] is True
        assert health["last_pass_started_at"] is not None
        assert health["last_successful_pass_at"] is not None
        assert health["last_error"] is None

    @pytest.mark.asyncio
    async def test_run_records_bounded_error_and_stops_cleanly(self, processor, store):
        store.requeue_expired_jobs.return_value = 0
        worker = CentralQueueWorker(
            processor=processor,
            store=store,
            poll_interval_seconds=1,
            owner_id=OWNER,
        )

        async def fail_and_stop(*args, **kwargs):
            worker.stop()
            raise RuntimeError("queue pass unavailable")

        process = AsyncMock(side_effect=fail_and_stop)
        reconcile = AsyncMock()
        with (
            patch.object(worker_module, "process_queued_jobs_once", process),
            patch.object(worker_module, "reconcile_active_jobs_once", reconcile),
        ):
            await worker.run()

        process.assert_awaited_once()
        reconcile.assert_not_awaited()
        assert worker.running is False
        assert worker.last_successful_pass_at is None
        assert worker.last_error == "queue pass unavailable"
        assert worker.health()["last_error"] == "queue pass unavailable"

    @pytest.mark.asyncio
    async def test_run_retries_after_error_without_real_sleep(self, processor, store):
        store.requeue_expired_jobs.return_value = 0
        worker = CentralQueueWorker(
            processor=processor,
            store=store,
            poll_interval_seconds=1,
            owner_id=OWNER,
        )
        attempts = 0

        async def fail_then_succeed(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient pass failure")
            worker.stop()
            return 0, []

        async def elapse_poll_interval(awaitable, *, timeout):
            awaitable.close()
            raise TimeoutError

        process = AsyncMock(side_effect=fail_then_succeed)
        reconcile = AsyncMock()
        with (
            patch.object(worker_module, "process_queued_jobs_once", process),
            patch.object(worker_module, "reconcile_active_jobs_once", reconcile),
            patch.object(worker_module.asyncio, "wait_for", new=elapse_poll_interval),
        ):
            await worker.run()

        assert store.requeue_expired_jobs.call_count == 2
        assert process.await_count == 2
        reconcile.assert_not_awaited()
        assert worker.running is False
        assert worker.last_successful_pass_at is not None
        assert worker.last_error is None

    @pytest.mark.asyncio
    async def test_run_propagates_cancellation_and_resets_running(self, processor, store):
        store.requeue_expired_jobs.return_value = 0
        worker = CentralQueueWorker(processor=processor, store=store, owner_id=OWNER)
        process = AsyncMock(side_effect=asyncio.CancelledError)

        with (
            patch.object(worker_module, "process_queued_jobs_once", process),
            pytest.raises(asyncio.CancelledError),
        ):
            await worker.run()

        assert worker.running is False
        assert worker.last_error is None
