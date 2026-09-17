"""Floci layer: the central queue worker's dispatch pass on real DynamoDB.

``test_floci_dynamodb_stores.py`` proves the ``JobStore`` primitives —
including that claims are exclusive and fenced. This module covers the layer
above: ``central_queue_worker.process_queued_jobs_once`` orchestrating those
primitives into one dispatch pass. Every store interaction (queued-job GSI
query, fenced claim, CLAIMED→APPLYING→PENDING transitions, failure
persistence, spot-gate observations) runs against the emulator's DynamoDB
over the wire; only the two boundaries the worker is *designed* to seam —
the Kubernetes apply (``processor.apply_queued_job``) and the EC2 spot-price
gate — are doubled, mirroring the queue-processor Floci module's philosophy.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import boto3
import pytest

from tests._floci import create_jobs_table, floci_test_markers, unique_name

pytestmark = floci_test_markers()

_REGION = "us-east-1"


def _manifest(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": "gco-jobs"},
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": "main", "image": "busybox"}],
                    "restartPolicy": "Never",
                }
            }
        },
    }


@pytest.fixture
def store(verified_floci_endpoint):
    from gco.services.template_store import JobStore

    table_name = unique_name("gco-central-jobs")
    dynamodb = boto3.client("dynamodb")
    create_jobs_table(dynamodb, table_name)
    yield JobStore(table_name=table_name)
    dynamodb.delete_table(TableName=table_name)


class _RecordingProcessor:
    """The worker's Kubernetes seam: records applies, returns a resource."""

    region = _REGION

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.applied: list[tuple[dict[str, Any], str, str]] = []

    def apply_queued_job(self, manifest: dict[str, Any], namespace: str, job_id: str) -> Any:
        self.applied.append((manifest, namespace, job_id))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            name=manifest["metadata"]["name"],
            namespace=namespace,
            uid=f"uid-{job_id}",
        )


class _OpenGate:
    """Spot gate double whose answer is 'no gate applies' for every job."""

    def evaluate(self, queued_job: dict[str, Any]) -> Any:
        del queued_job
        return None


class _ClosedGate:
    """Spot gate double that reports the cap unmet for every gated job."""

    def evaluate(self, queued_job: dict[str, Any]) -> Any:
        return SimpleNamespace(
            gated=True,
            instance_type=str(queued_job.get("spot_instance_type", "")),
            max_price=str(queued_job.get("spot_max_price", "")),
            observed_price=0.5,
            reason="observed 0.500000 above cap",
        )


@pytest.mark.asyncio
async def test_queued_job_is_claimed_applied_and_marked_pending(store):
    from gco.services.central_queue_worker import process_queued_jobs_once

    store.submit_job("job-applied", _manifest("central-ok"), _REGION, namespace="gco-jobs")
    processor = _RecordingProcessor()

    total, processed = await process_queued_jobs_once(
        processor,
        store,
        limit=5,
        owner_id="floci-worker-a",
        spot_gate=_OpenGate(),
    )

    assert total == 1
    assert [(entry["job_id"], entry["status"]) for entry in processed] == [
        ("job-applied", "applied")
    ]
    assert processed[0]["k8s_job_name"] == "central-ok"
    assert processed[0]["k8s_job_uid"] == "uid-job-applied"
    assert [(m["metadata"]["name"], ns, jid) for m, ns, jid in processor.applied] == [
        ("central-ok", "gco-jobs", "job-applied")
    ]

    record = store.get_job("job-applied")
    assert record is not None
    assert record["status"] == "pending", (
        "a successfully applied job must land in PENDING with its Kubernetes identity"
    )
    assert record["k8s_job_name"] == "central-ok"
    assert record["k8s_job_uid"] == "uid-job-applied"
    # The pass is drained: a second run finds nothing queued.
    total_again, processed_again = await process_queued_jobs_once(
        processor, store, limit=5, owner_id="floci-worker-a", spot_gate=_OpenGate()
    )
    assert (total_again, processed_again) == (0, [])


@pytest.mark.asyncio
async def test_price_gated_job_is_deferred_without_claiming(store):
    from gco.services.central_queue_worker import process_queued_jobs_once

    store.submit_job(
        "job-gated",
        _manifest("central-gated"),
        _REGION,
        namespace="gco-jobs",
        spot_max_price="0.10",
        spot_instance_type="g5.xlarge",
    )
    processor = _RecordingProcessor()

    total, processed = await process_queued_jobs_once(
        processor,
        store,
        limit=5,
        owner_id="floci-worker-a",
        spot_gate=_ClosedGate(),
    )

    assert total == 1
    assert [(entry["job_id"], entry["status"]) for entry in processed] == [
        ("job-gated", "price_gated")
    ]
    assert processed[0]["instance_type"] == "g5.xlarge"
    assert processed[0]["max_spot_price"] == "0.10"
    assert processor.applied == [], "a closed gate must defer before any Kubernetes call"

    record = store.get_job("job-gated")
    assert record is not None
    assert record["status"] == "queued", (
        "deferral must leave the job claimable — QUEUED, not CLAIMED or FAILED"
    )

    # The deferral consumed no apply budget: once the gate opens, the very
    # next pass dispatches the same record.
    total, processed = await process_queued_jobs_once(
        processor, store, limit=5, owner_id="floci-worker-a", spot_gate=_OpenGate()
    )
    assert [(entry["job_id"], entry["status"]) for entry in processed] == [("job-gated", "applied")]
    record = store.get_job("job-gated")
    assert record is not None and record["status"] == "pending"


@pytest.mark.asyncio
async def test_apply_failure_is_persisted_as_failed(store):
    from gco.services.central_queue_worker import process_queued_jobs_once

    store.submit_job("job-broken", _manifest("central-broken"), _REGION, namespace="gco-jobs")
    processor = _RecordingProcessor(error=RuntimeError("api server said no"))

    total, processed = await process_queued_jobs_once(
        processor,
        store,
        limit=5,
        owner_id="floci-worker-a",
        spot_gate=_OpenGate(),
    )

    assert total == 1
    assert [(entry["job_id"], entry["status"]) for entry in processed] == [("job-broken", "failed")]
    assert "api server said no" in processed[0]["error"]

    record = store.get_job("job-broken")
    assert record is not None
    assert record["status"] == "failed", (
        "a permanent apply error must persist FAILED loudly instead of "
        "leaving the record CLAIMED for lease recovery to guess about"
    )
    assert "api server said no" in str(record.get("error_message", ""))
