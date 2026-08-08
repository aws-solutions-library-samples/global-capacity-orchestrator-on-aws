"""Floci layer: the SQS job path with production queue wiring.

``integration:docker:queue-processor`` already proves the container consumes
one message against a moto server. This module covers what that job cannot:
the queue *pair* as the regional stack actually provisions it — main queue
plus dead-letter queue joined by a RedrivePolicy — and the security
consequence GCO's design leans on: a policy-rejected job is never
acknowledged, so after ``maxReceiveCount`` receives the emulator's redrive
machinery must move it to the DLQ where operators inspect it. That
end-to-end retention path spans server-side receive counting, visibility
timeouts, and redrive — none of which client-side mocks exercise.

``queue_processor.process_one_message`` runs unmodified: it builds its own
boto3 SQS client from the session environment.
"""

from __future__ import annotations

import importlib
import json
import os

import boto3
import pytest

from tests._floci import create_job_queue, floci_test_markers, unique_name

pytestmark = floci_test_markers()


@pytest.fixture()
def sqs(verified_floci_endpoint: str):
    return boto3.client("sqs")


@pytest.fixture()
def job_queue(sqs):
    queues = create_job_queue(sqs, unique_name("gco-jobs"), max_receive_count=2)
    yield queues
    for url in (queues["queue_url"], queues["dlq_url"]):
        sqs.delete_queue(QueueUrl=url)


@pytest.fixture()
def fast_retry_job_queue(sqs):
    """Queue pair whose retained messages are immediately receivable again.

    Production uses a 300s visibility timeout; waiting that out in a test is
    unacceptable, and changing queue attributes mid-flight does not affect a
    message already received. A zero visibility timeout at creation keeps the
    redrive semantics identical while making each retry instantaneous.
    """
    queues = create_job_queue(
        sqs, unique_name("gco-jobs-fast"), max_receive_count=2, visibility_timeout=0
    )
    yield queues
    for url in (queues["queue_url"], queues["dlq_url"]):
        sqs.delete_queue(QueueUrl=url)


def _run_queue_processor(monkeypatch, queue_url: str) -> tuple[bool, object]:
    """Reload and run the production consumer against the session's queue.

    The module reads JOB_QUEUE_URL and its policy toggles at import time, so
    a reload after the env changes is the honest way to run it — the same
    thing a fresh KEDA pod does.
    """
    monkeypatch.setenv("JOB_QUEUE_URL", queue_url)
    monkeypatch.setenv("AWS_REGION", os.environ["AWS_DEFAULT_REGION"])
    import gco.services.queue_processor as queue_processor

    module = importlib.reload(queue_processor)
    # No cluster in this layer: the K8s apply path belongs to the kind E2E.
    # Rejection happens strictly before any Kubernetes call, which is the
    # portion under test here.
    return module.process_one_message(), module


def _approximate_counts(sqs, queue_url: str) -> tuple[int, int]:
    attributes = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return (
        int(attributes["ApproximateNumberOfMessages"]),
        int(attributes["ApproximateNumberOfMessagesNotVisible"]),
    )


class TestQueueProcessorAgainstRealQueues:
    def test_empty_poll_is_a_clean_success(self, monkeypatch, sqs, job_queue):
        ok, _ = _run_queue_processor(monkeypatch, job_queue["queue_url"])
        assert ok is True, "an empty poll must be a success (KEDA scale-down semantics)"

    def test_valid_job_is_consumed_and_deleted(self, monkeypatch, sqs, job_queue):
        # A manifest that passes every validation gate; the K8s apply is the
        # next boundary, patched to a no-op success so the SQS acknowledge
        # path (delete_message) runs for real.
        body = {
            "job_id": "floci-ok",
            "manifests": [
                {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "ok", "namespace": "gco-jobs"},
                    "spec": {
                        "template": {
                            "spec": {
                                "containers": [{"name": "main", "image": "busybox"}],
                                "restartPolicy": "Never",
                            }
                        }
                    },
                }
            ],
        }
        sqs.send_message(QueueUrl=job_queue["queue_url"], MessageBody=json.dumps(body))

        monkeypatch.setenv("JOB_QUEUE_URL", job_queue["queue_url"])
        import gco.services.queue_processor as queue_processor

        module = importlib.reload(queue_processor)

        from gco.models import ResourceStatus

        def fake_apply(manifest):
            return ResourceStatus(
                api_version=manifest.get("apiVersion", ""),
                kind=manifest.get("kind", ""),
                name=manifest.get("metadata", {}).get("name", ""),
                namespace=manifest.get("metadata", {}).get("namespace", "gco-jobs"),
                status="created",
            )

        monkeypatch.setattr(module, "apply_manifest", fake_apply)
        assert module.process_one_message() is True

        visible, in_flight = _approximate_counts(sqs, job_queue["queue_url"])
        assert (visible, in_flight) == (0, 0), (
            "a fully processed job must be deleted from the queue, not left in flight"
        )

    def test_rejected_job_is_retained_then_lands_in_the_dlq(
        self, monkeypatch, sqs, fast_retry_job_queue
    ):
        job_queue = fast_retry_job_queue
        privileged = {
            "job_id": "floci-privileged",
            "manifests": [
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {"name": "evil", "namespace": "gco-jobs"},
                    "spec": {
                        "containers": [
                            {
                                "name": "c",
                                "image": "busybox",
                                "securityContext": {"privileged": True},
                            }
                        ]
                    },
                }
            ],
        }
        sqs.send_message(QueueUrl=job_queue["queue_url"], MessageBody=json.dumps(privileged))

        # First consume: prevalidation rejects, message is NOT acknowledged.
        ok, _ = _run_queue_processor(monkeypatch, job_queue["queue_url"])
        assert ok is False, "a policy-violating job must fail the consumer"
        visible, in_flight = _approximate_counts(sqs, job_queue["queue_url"])
        assert visible + in_flight == 1, "rejected message must stay in the main queue"

        # Second rejected consume reaches maxReceiveCount=2.
        ok, _ = _run_queue_processor(monkeypatch, job_queue["queue_url"])
        assert ok is False

        # Redrive fires when a subsequent receive would exceed the policy:
        # further main-queue receives must come back empty while the message
        # surfaces in the DLQ carrying the exact rejected payload.
        dlq_payload = None
        for _ in range(10):
            main_response = sqs.receive_message(
                QueueUrl=job_queue["queue_url"], WaitTimeSeconds=0, MaxNumberOfMessages=1
            )
            assert not main_response.get("Messages"), (
                "a message past maxReceiveCount must never be served from the main queue again"
            )
            dlq_response = sqs.receive_message(
                QueueUrl=job_queue["dlq_url"], WaitTimeSeconds=1, MaxNumberOfMessages=1
            )
            if dlq_response.get("Messages"):
                dlq_payload = json.loads(dlq_response["Messages"][0]["Body"])
                break
        assert dlq_payload is not None, (
            "after maxReceiveCount receives the rejected job must land in the DLQ "
            "(server-side redrive), not vanish and not stay receivable"
        )
        assert dlq_payload["job_id"] == "floci-privileged", (
            "the DLQ must receive the exact rejected payload for operator inspection"
        )
