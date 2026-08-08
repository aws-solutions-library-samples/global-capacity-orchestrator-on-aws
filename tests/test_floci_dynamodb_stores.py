"""Floci layer: the DynamoDB store classes over the real wire protocol.

The moto-based unit tests validate store logic in-process; this module runs
the same production classes (``TemplateStore``, ``WebhookStore``, ``JobStore``
from ``gco/services/template_store.py`` and ``InferenceEndpointStore`` from
``gco/services/inference_store.py``) against an emulator speaking the actual
DynamoDB HTTP protocol, with tables shaped exactly like
``gco/stacks/global_stack.py`` provisions them. That covers what in-process
mocks structurally cannot: real request signing and serialization, server-side
conditional-write enforcement, GSI queries, and scan pagination cursors
crossing a network boundary.
"""

from __future__ import annotations

import boto3
import pytest

from tests._floci import (
    create_jobs_table,
    create_templates_table,
    create_webhooks_table,
    floci_test_markers,
    unique_name,
)

pytestmark = floci_test_markers()


@pytest.fixture(scope="module")
def dynamodb(verified_floci_endpoint: str):
    return boto3.client("dynamodb")


class TestTemplateStore:
    @pytest.fixture()
    def store(self, dynamodb):
        from gco.services.template_store import TemplateStore

        table_name = unique_name("gco-job-templates")
        create_templates_table(dynamodb, table_name)
        yield TemplateStore(table_name=table_name)
        dynamodb.delete_table(TableName=table_name)

    def test_create_get_update_delete_round_trip(self, store):
        manifest = {"kind": "Job", "apiVersion": "batch/v1"}
        created = store.create_template(
            name="ml-training", manifest=manifest, description="training job template"
        )
        assert created["name"] == "ml-training"

        fetched = store.get_template("ml-training")
        assert fetched is not None, "created template must be readable back over the wire"
        assert fetched["manifest"] == manifest
        assert fetched["description"] == "training job template"

        store.update_template("ml-training", description="updated")
        assert store.get_template("ml-training")["description"] == "updated"

        assert store.delete_template("ml-training") is True
        assert store.get_template("ml-training") is None

    def test_duplicate_create_is_rejected_server_side(self, store):
        store.create_template(name="dup", manifest={"kind": "Job"})
        with pytest.raises(ValueError, match="already exists"):
            store.create_template(name="dup", manifest={"kind": "Job"})

    def test_list_templates_paginates_past_one_scan_page(self, store):
        # A >1MB scan page cannot be faked from the client side: the store
        # must actually follow the LastEvaluatedKey cursors the server
        # returns. 60 items with 64KiB bodies force at least four pages.
        payload = "x" * (64 * 1024)
        for index in range(60):
            store.create_template(name=f"bulk-{index:03d}", manifest={"blob": payload})
        names = {template["name"] for template in store.list_templates()}
        assert names == {f"bulk-{index:03d}" for index in range(60)}, (
            "list_templates() must stitch every scan page together"
        )


class TestWebhookStore:
    @pytest.fixture()
    def store(self, dynamodb):
        from gco.services.template_store import WebhookStore

        table_name = unique_name("gco-webhooks")
        create_webhooks_table(dynamodb, table_name)
        yield WebhookStore(table_name=table_name)
        dynamodb.delete_table(TableName=table_name)

    def test_namespace_index_query_matches_created_rows(self, store):
        for namespace, count in (("gco-jobs", 3), ("default", 2)):
            for index in range(count):
                store.create_webhook(
                    webhook_id=f"{namespace}-hook-{index}",
                    url=f"https://hooks.example/{namespace}/{index}",
                    events=["job.completed"],
                    namespace=namespace,
                )
        scoped = store.list_webhooks(namespace="gco-jobs")
        assert len(scoped) == 3, "namespace GSI query must return exactly that namespace's rows"
        assert {hook["namespace"] for hook in scoped} == {"gco-jobs"}
        assert len(store.list_webhooks()) == 5

    def test_event_routing_uses_persisted_subscriptions(self, store):
        store.create_webhook(
            webhook_id="completed-hook",
            url="https://hooks.example/completed",
            events=["job.completed"],
            namespace="gco-jobs",
        )
        matches = store.get_webhooks_for_event("job.completed", namespace="gco-jobs")
        assert [hook["id"] for hook in matches] == ["completed-hook"]
        assert store.get_webhooks_for_event("job.failed", namespace="gco-jobs") == []


class TestJobStore:
    @pytest.fixture()
    def store(self, dynamodb):
        from gco.services.template_store import JobStore

        table_name = unique_name("gco-jobs")
        create_jobs_table(dynamodb, table_name)
        yield JobStore(table_name=table_name)
        dynamodb.delete_table(TableName=table_name)

    def test_submission_idempotency_replay_and_conflict_are_server_enforced(self, store):
        from gco.services.template_store import JobSubmissionConflict

        manifest = {"kind": "Job", "metadata": {"name": "train"}}
        job = store.submit_job(
            job_id="job-idem-1",
            manifest=manifest,
            target_region="us-east-1",
            idempotency_key="idem-1",
            request_hash="hash-a",
        )
        assert job["status"] == "queued"

        # Byte-identical replay (same id + key + request hash) returns the
        # original record flagged as a replay instead of double-submitting.
        replay = store.submit_job(
            job_id="job-idem-1",
            manifest=manifest,
            target_region="us-east-1",
            idempotency_key="idem-1",
            request_hash="hash-a",
        )
        assert replay["job_id"] == "job-idem-1"
        assert replay.get("idempotent_replay") is True

        # A DIFFERENT payload reusing the id must be refused. The guarantee
        # rests on DynamoDB's attribute_not_exists conditional write, so it
        # has to hold on the server, not in client-side bookkeeping.
        with pytest.raises(JobSubmissionConflict):
            store.submit_job(
                job_id="job-idem-1",
                manifest={"kind": "Job", "metadata": {"name": "other"}},
                target_region="us-east-1",
                idempotency_key="idem-1",
                request_hash="hash-b",
            )

    def test_region_status_index_feeds_the_queue_worker(self, store):
        submitted = [
            store.submit_job(
                job_id=f"job-work-{index}",
                manifest={"kind": "Job"},
                target_region="us-east-1",
            )["job_id"]
            for index in range(3)
        ]
        store.submit_job(
            job_id="job-elsewhere",
            manifest={"kind": "Job"},
            target_region="eu-west-1",
        )
        # The regional worker discovers work through the region-status GSI;
        # exactly this region's queued jobs must be visible through it.
        queued = store.list_jobs(target_region="us-east-1", status="queued")
        assert {job["job_id"] for job in queued} == set(submitted)

    def test_claim_is_exclusive_and_fenced(self, store):
        store.submit_job(
            job_id="job-claim-1",
            manifest={"kind": "Job"},
            target_region="us-east-1",
        )
        first = store.claim_job("job-claim-1", target_region="us-east-1", claimed_by="worker-a")
        assert first is not None, "first claim of a queued job must succeed"
        assert first["status"] == "claimed"
        second = store.claim_job("job-claim-1", target_region="us-east-1", claimed_by="worker-b")
        assert second is None, (
            "second claim must lose: the job is no longer 'queued', and the fencing "
            "generation moved with the first claim"
        )


class TestInferenceEndpointStore:
    @pytest.fixture()
    def store(self, dynamodb):
        from gco.services.inference_store import InferenceEndpointStore

        table_name = unique_name("gco-inference-endpoints")
        dynamodb.create_table(
            TableName=table_name,
            AttributeDefinitions=[{"AttributeName": "endpoint_name", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "endpoint_name", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.get_waiter("table_exists").wait(TableName=table_name)
        yield InferenceEndpointStore(table_name=table_name)
        dynamodb.delete_table(TableName=table_name)

    def test_desired_state_round_trip_for_reconciler(self, store):
        spec = {"image": "vllm/vllm-openai:v0.11.0", "model": "meta/llama", "replicas": 2}
        created = store.create_endpoint(
            endpoint_name="llm-a", spec=spec, target_regions=["us-east-1"]
        )
        assert created["ingress_path"] == "/inference/llm-a"

        listed = store.list_endpoints()
        assert [endpoint["endpoint_name"] for endpoint in listed] == ["llm-a"]

        fetched = store.get_endpoint("llm-a")
        assert fetched is not None
        assert fetched["spec"]["replicas"] == 2

        with pytest.raises(ValueError, match="already exists"):
            store.create_endpoint(endpoint_name="llm-a", spec=spec, target_regions=["us-east-1"])

        updated = store.update_desired_state("llm-a", "stopped")
        assert updated is not None and updated["desired_state"] == "stopped"

        assert store.delete_endpoint("llm-a") is True
        assert store.get_endpoint("llm-a") is None
