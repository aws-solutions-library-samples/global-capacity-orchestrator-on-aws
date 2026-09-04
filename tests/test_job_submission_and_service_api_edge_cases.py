"""Focused behavior coverage for the service/API baseline gaps.

Every external boundary is replaced with a deterministic in-memory fake.  The
module intentionally exercises public behavior and meaningful failure modes
rather than importing source lines solely to mark them as executed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException
from starlette.requests import Request


def _client_error(code: str = "InternalError", operation: str = "Operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def _bare_job_store() -> Any:
    from gco.services.template_store import JobStore

    store = object.__new__(JobStore)
    store._table = MagicMock()
    store.claim_lease_seconds = 300
    store._legacy_migration_cursors = {}
    store._legacy_migration_completed_in_sweep = set()
    store._legacy_migration_next_status = {}
    return store


def _base_job(name: str = "trainer") -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name},
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": "main", "image": "python:3.14"}],
                    "restartPolicy": "Never",
                }
            }
        },
    }


def _bare_processor() -> Any:
    from gco.services.manifest_processor import ManifestProcessor

    processor = object.__new__(ManifestProcessor)
    processor.cluster_id = "cluster"
    processor.region = "us-east-1"
    processor._k8s_timeout = 7
    processor.batch_v1 = MagicMock()
    processor.core_v1 = MagicMock()
    processor.custom_objects = MagicMock()
    processor.allowed_namespaces = {"gco-jobs", "default"}
    processor.validate_manifest = MagicMock(return_value=(True, None))
    processor._inject_security_defaults = MagicMock()
    return processor


def _queued_job_object(
    queue_id: str = "queue-1", *, uid: str = "uid-1", annotation: str | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            annotations={"gco.io/queue-job-id": annotation or queue_id},
            uid=uid,
            name="trainer-deterministic",
            namespace="gco-jobs",
        )
    )


def _api_processor() -> MagicMock:
    processor = MagicMock()
    processor.cluster_id = "cluster"
    processor.region = "us-east-1"
    processor.allowed_namespaces = {"gco-jobs", "default"}
    processor.core_v1 = MagicMock()
    processor.batch_v1 = MagicMock()
    processor.custom_objects = MagicMock()
    return processor


def _pod(name: str = "pod-1", phase: str = "Running") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace="gco-jobs",
            labels={"job-name": "trainer"},
            uid=f"uid-{name}",
            creation_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=[],
            init_container_statuses=[],
            pod_ip="203.0.113.1",
            host_ip="203.0.113.2",
            start_time=None,
        ),
        spec=SimpleNamespace(
            containers=[SimpleNamespace(name="main", image="python:3.14")],
            init_containers=[],
            node_name=None,
        ),
    )


def _request(headers: dict[str, str], body: bytes = b"") -> Request:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/v1/test",
        "raw_path": b"/api/v1/test",
        "query_string": b"",
        "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 443),
    }
    return Request(scope, receive)


# ---------------------------------------------------------------------------
# DynamoDB stores
# ---------------------------------------------------------------------------


def test_template_and_webhook_store_error_and_optional_update_paths() -> None:
    from gco.services.template_store import TemplateStore, WebhookStore

    templates = object.__new__(TemplateStore)
    templates._table = MagicMock()
    templates._table.scan.side_effect = _client_error()
    with pytest.raises(ClientError):
        templates.list_templates()

    templates._table = MagicMock()
    templates._table.update_item.return_value = {
        "Attributes": {
            "template_name": "t",
            "manifest": "{}",
            "parameters": '{"epochs": 2}',
        }
    }
    updated = templates.update_template("t", parameters={"epochs": 2})
    assert updated is not None and updated["parameters"] == {"epochs": 2}
    assert (
        templates._table.update_item.call_args.kwargs["ExpressionAttributeValues"][":parameters"]
        == '{"epochs": 2}'
    )

    templates._table.get_item.side_effect = _client_error()
    with pytest.raises(ClientError):
        templates.template_exists("t")

    webhooks = object.__new__(WebhookStore)
    webhooks._table = MagicMock()
    webhooks._table.scan.side_effect = [
        {
            "Items": [{"webhook_id": "a", "url": "https://a.example", "events": "[]"}],
            "LastEvaluatedKey": {"webhook_id": "a"},
        },
        {"Items": [{"webhook_id": "b", "url": "https://b.example", "events": "[]"}]},
    ]
    assert [item["id"] for item in webhooks.list_webhooks()] == ["a", "b"]
    assert webhooks._table.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"webhook_id": "a"}

    webhooks._table.scan.side_effect = _client_error()
    with pytest.raises(ClientError):
        webhooks.list_webhooks()

    webhooks._table = MagicMock()
    created = webhooks.create_webhook("id", "https://example.com", ["job.completed"])
    assert created["namespace"] is None
    assert "namespace" not in webhooks._table.put_item.call_args.kwargs["Item"]
    assert "secret" not in webhooks._table.put_item.call_args.kwargs["Item"]


def test_job_store_configuration_and_normalization_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    from gco.services.template_store import JobStore

    monkeypatch.setenv("CENTRAL_QUEUE_LEASE_SECONDS", "not-an-integer")
    with patch("gco.services.template_store.boto3.resource"):
        store = JobStore(table_name="jobs", region="us-east-1")
    assert store.claim_lease_seconds == 300

    with patch("gco.services.template_store.boto3.resource"):
        explicit = JobStore(table_name="jobs", region="us-east-1", claim_lease_seconds=45)
    assert explicit.claim_lease_seconds == 45

    sentinel = {"fallback": True}
    assert JobStore._decode_json("{bad", sentinel) is sentinel
    raw = {"already": "decoded"}
    assert JobStore._decode_json(raw, sentinel) is raw

    history = json.loads(
        JobStore._history_with(
            {"status_history": '{"not": "a list"}'},
            status="running",
            timestamp="now",
        )
    )
    assert history == [{"status": "running", "timestamp": "now"}]
    assert JobStore._legacy_priority({"priority": "bad"}) == 0


def test_job_store_legacy_migration_outcomes_and_failures() -> None:
    from gco.services.template_store import JobStatus

    store = _bare_job_store()
    transient = {
        "job_id": "legacy-claimed",
        "priority": 1,
        "submitted_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
        "claimed_by": "worker",
        "claim_token": "token",
        "claim_generation": "bad",
        "lease_expires_at": "2025-01-01T01:00:00Z",
    }
    assert store._migrate_legacy_record(transient, "us-east-1", JobStatus.CLAIMED.value) == "failed"
    assert ":failed" in store._table.update_item.call_args.kwargs["ExpressionAttributeValues"]

    store._table.reset_mock()
    active = {
        "job_id": "legacy-running",
        "priority": 2,
        "submitted_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
        "k8s_job_name": "trainer",
        "k8s_job_namespace": "gco-jobs",
        "k8s_job_uid": "uid-1",
    }
    assert store._migrate_legacy_record(active, "us-east-1", JobStatus.RUNNING.value) == "migrated"

    store._table.update_item.side_effect = _client_error("AccessDeniedException")
    with pytest.raises(ClientError):
        store._migrate_legacy_record(active, "us-east-1", JobStatus.RUNNING.value)


def test_job_store_migration_sweep_rotation_and_zero_scan() -> None:
    from gco.services.template_store import JobStatus

    store = _bare_job_store()
    statuses = (
        JobStatus.QUEUED.value,
        JobStatus.CLAIMED.value,
        JobStatus.APPLYING.value,
        JobStatus.PENDING.value,
        JobStatus.RUNNING.value,
    )
    store._legacy_migration_completed_in_sweep = {("us-east-1", status) for status in statuses}
    result = store.migrate_legacy_records_for_region("us-east-1", evaluation_limit=1)
    assert result["complete"] is True
    store._table.query.assert_not_called()

    store = _bare_job_store()
    store._table.query.return_value = {
        "Items": [],
        "ScannedCount": 0,
        "LastEvaluatedKey": {"job_id": "cursor"},
    }
    result = store.migrate_legacy_records_for_region("us-east-1", evaluation_limit=1)
    assert result == {"evaluated": 0, "migrated": 0, "failed": 0, "complete": False}
    assert store._legacy_migration_cursors[("us-east-1", JobStatus.QUEUED.value)] == {
        "job_id": "cursor"
    }


def test_job_store_worker_queries_paginate_sort_and_deduplicate() -> None:
    from gco.services.template_store import JobStatus

    store = _bare_job_store()
    store._table.query.side_effect = [
        {"Items": [{"job_id": "a"}], "LastEvaluatedKey": {"job_id": "a"}},
        {
            "Items": [{"job_id": "b"}],
            "LastEvaluatedKey": {"job_id": "b"},
        },
    ]
    page = store._query_worker_index(
        index_name="worker-index",
        region="us-east-1",
        status=JobStatus.QUEUED.value,
        limit=2,
    )
    assert [item["job_id"] for item in page] == ["a", "b"]
    assert store._table.query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"job_id": "a"}

    store._query_worker_index = MagicMock(
        return_value=[
            {"job_id": None},
            {"job_id": "b", "priority": "bad", "submitted_at": "2"},
            {"job_id": "a", "priority_sort": "001#a"},
            {"job_id": "a", "priority_sort": "999#duplicate"},
        ]
    )
    ordered = store._query_region_status("us-east-1", JobStatus.QUEUED.value, 5)
    assert [item["job_id"] for item in ordered] == ["a", "b"]

    store._query_worker_index.return_value = [
        {"not_job_id": True},
        {"job_id": "a", "lease_expires_at": "1"},
        {"job_id": "a", "lease_expires_at": "2"},
        {"job_id": "b", "lease_expires_at": "3"},
    ]
    expired = store._query_expired_claims("us-east-1", JobStatus.CLAIMED.value, "9", 10)
    assert [(item["job_id"], item["lease_expires_at"]) for item in expired] == [
        ("a", "2"),
        ("b", "3"),
    ]


def test_job_store_claim_and_transition_guards_and_update_shapes() -> None:
    from gco.services.template_store import JobStatus

    store = _bare_job_store()
    store._get_raw_job = MagicMock(
        return_value={"status": JobStatus.RUNNING.value, "target_region": "us-east-1"}
    )
    assert store.claim_job("id", "us-east-1", "worker") is None

    with pytest.raises(ValueError, match="cannot accompany Kubernetes identity"):
        store.transition_job(
            "id",
            target_region="us-east-1",
            expected_status=JobStatus.APPLYING,
            status=JobStatus.FAILED,
            workload_not_created=True,
            k8s_job_uid="uid",
        )

    pending = {
        "job_id": "id",
        "status": JobStatus.PENDING.value,
        "target_region": "us-east-1",
        "updated_at": "then",
        "submitted_at": "before",
        "priority": 0,
        "k8s_job_uid": "uid-1",
    }
    store._get_raw_job.return_value = pending
    store._table.update_item.return_value = {"Attributes": pending | {"status": "running"}}
    result = store.transition_job(
        "id",
        target_region="us-east-1",
        expected_status=JobStatus.PENDING,
        status=JobStatus.RUNNING,
        expected_k8s_uid="uid-1",
    )
    assert result is not None
    values = store._table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":expected_k8s_uid"] == "uid-1"

    store._table.reset_mock()
    store._table.update_item.return_value = {"Attributes": pending | {"status": "failed"}}
    store.transition_job(
        "id",
        target_region="us-east-1",
        expected_status=JobStatus.PENDING,
        status=JobStatus.FAILED,
    )
    expression = store._table.update_item.call_args.kwargs["UpdateExpression"]
    assert "error_message =" not in expression

    claimed = pending | {
        "status": JobStatus.CLAIMED.value,
        "claimed_by": "worker",
        "claim_token": "token",
        "claim_generation": 2,
        "lease_expires_at": "9999-01-01T00:00:00Z",
    }
    store._get_raw_job.return_value = claimed
    store._table.update_item.return_value = {
        "Attributes": claimed | {"status": JobStatus.APPLYING.value}
    }
    store.transition_job(
        "id",
        target_region="us-east-1",
        expected_status=JobStatus.CLAIMED,
        status=JobStatus.APPLYING,
        claimed_by="worker",
        claim_token="token",
        claim_generation=2,
        error="diagnostic",
    )
    assert " REMOVE " not in store._table.update_item.call_args.kwargs["UpdateExpression"]


def test_job_store_regional_query_errors_recovery_limits_counts_and_cancel() -> None:
    from gco.services.template_store import JobStatus

    store = _bare_job_store()
    store._query_region_status = MagicMock(side_effect=_client_error())
    with pytest.raises(ClientError):
        store.get_queued_jobs_for_region("us-east-1")

    store._query_region_status = MagicMock(return_value=[])
    assert store.get_active_jobs_for_region("us-east-1", limit=2) == []

    store._query_expired_claims = MagicMock(side_effect=_client_error())
    with pytest.raises(ClientError):
        store.requeue_expired_jobs("us-east-1")

    candidate = {
        "job_id": "a",
        "claimed_by": "worker",
        "claim_token": "token",
        "claim_generation": 1,
        "status": JobStatus.CLAIMED.value,
        "target_region": "us-east-1",
        "updated_at": "then",
        "submitted_at": "before",
        "lease_expires_at": "2000-01-01T00:00:00Z",
    }
    store._query_expired_claims = MagicMock(return_value=[candidate, candidate | {"job_id": "b"}])
    assert store.requeue_expired_jobs("us-east-1", limit=1) == 1
    assert store._table.update_item.call_count == 1

    store = _bare_job_store()
    store._table.scan.side_effect = [
        {
            "Items": [None, {"target_region": "us-east-1", "status": "queued"}],
            "ScannedCount": 2,
            "LastEvaluatedKey": {"job_id": "a"},
        },
        {
            "Items": [{"target_region": "us-west-2", "status": "running"}],
            "ScannedCount": 1,
        },
    ]
    counts, evaluated, truncated = store.get_job_count_summary(max_evaluated=10)
    assert counts == {
        "us-east-1": {"queued": 1},
        "us-west-2": {"running": 1},
    }
    assert (evaluated, truncated) == (3, False)
    assert store._table.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {"job_id": "a"}

    store._table.scan.side_effect = _client_error()
    with pytest.raises(ClientError):
        store.get_job_count_summary()

    store.get_job_count_summary = MagicMock(return_value=({"r": {"queued": 1}}, 20_000, True))
    with patch("gco.services.template_store.logger.warning") as warning:
        assert store.get_job_counts_by_region() == {"r": {"queued": 1}}
    warning.assert_called_once()

    store._get_raw_job = MagicMock(
        return_value={
            "job_id": "id",
            "status": JobStatus.QUEUED.value,
            "target_region": "us-east-1",
            "updated_at": "then",
        }
    )
    store._table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
    assert store.cancel_job("id") is False


# ---------------------------------------------------------------------------
# Manifest processor and SQS processor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [(None, True), ("bad", True), (408, True), (429, True), (500, True), (403, False)],
)
def test_retryable_kubernetes_error_classifier(status: Any, expected: bool) -> None:
    from gco.services.manifest_processor import _is_retryable_kubernetes_api_error

    assert _is_retryable_kubernetes_api_error(ApiException(status=status)) is expected


def test_manifest_processor_warns_for_org_in_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    from gco.services.manifest_processor import ManifestProcessor

    config_values = {
        "max_cpu_per_manifest": "1",
        "max_memory_per_manifest": "1Gi",
        "max_gpu_per_manifest": 1,
        "trusted_registries": ["dockerhuborg"],
    }
    with (
        patch("gco.services.manifest_processor.config") as config,
        patch("gco.services.manifest_processor.client"),
        patch("gco.services.manifest_processor.logger.warning") as warning,
    ):
        config.ConfigException = k8s_config.ConfigException
        ManifestProcessor("cluster", "us-east-1", config_values)
    assert "moving it to trusted_dockerhub_orgs" in warning.call_args.args[0]


@pytest.mark.parametrize(
    "manifest",
    [
        {"kind": "Job", "spec": {"template": None}},
        {"kind": "Job", "spec": {"template": []}},
        {"kind": "Job", "spec": {"template": {"spec": None}}},
        {"kind": "Job", "spec": {"template": {"spec": []}}},
        {"kind": "CronJob", "spec": {"jobTemplate": None}},
        {"kind": "CronJob", "spec": {"jobTemplate": {"spec": None}}},
        {
            "kind": "CronJob",
            "spec": {"jobTemplate": {"spec": {"template": None}}},
        },
    ],
)
def test_manifest_processor_malformed_workload_shapes_return_none(
    manifest: dict[str, Any],
) -> None:
    from gco.services.manifest_processor import ManifestProcessor

    assert ManifestProcessor._extract_pod_spec(manifest) is None


def test_manifest_processor_container_delegates_cover_accelerators_and_ephemeral() -> None:
    from gco.services.manifest_processor import ManifestProcessor

    pod_spec = {
        "containers": [{"name": "main"}],
        "ephemeralContainers": [
            {
                "name": "debug",
                "resources": {"limits": {"nvidia.com/gpu": "1"}},
            }
        ],
    }
    processor = object.__new__(ManifestProcessor)
    assert processor._requested_accelerators(pod_spec) == {"nvidia.com/gpu"}
    assert [kind for kind, _ in processor._get_all_containers(pod_spec)] == [
        "container",
        "ephemeralContainer",
    ]


def test_manifest_processor_toleration_check_can_be_disabled() -> None:
    from gco.services.manifest_processor import ManifestProcessor

    with (
        patch("gco.services.manifest_processor.config") as config,
        patch("gco.services.manifest_processor.client"),
    ):
        config.ConfigException = k8s_config.ConfigException
        processor = ManifestProcessor(
            "cluster",
            "us-east-1",
            {
                "max_cpu_per_manifest": "10",
                "max_memory_per_manifest": "32Gi",
                "max_gpu_per_manifest": 4,
                "require_accelerator_toleration": False,
                "allowed_namespaces": ["gco-jobs"],
            },
        )
    manifest = _base_job()
    manifest["spec"]["template"]["spec"]["containers"][0]["resources"] = {
        "limits": {"nvidia.com/gpu": "1"}
    }
    valid, error = processor.validate_manifest(manifest, "gco-jobs")
    assert valid is True and error is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda manifest: manifest.update(apiVersion="v1"), "accepts only"),
        (lambda manifest: manifest.update(metadata=[]), "metadata must be an object"),
        (
            lambda manifest: manifest["metadata"].update(namespace="other"),
            "namespace does not match",
        ),
        (lambda manifest: manifest["metadata"].pop("name"), "metadata.name is required"),
        (lambda manifest: manifest["metadata"].update(labels=[]), "must be objects"),
    ],
)
def test_apply_queued_job_rejects_invalid_envelopes(mutate: Any, message: str) -> None:
    from gco.services.manifest_processor import QueuedJobNotCreatedError

    processor = _bare_processor()
    manifest = _base_job()
    mutate(manifest)
    with pytest.raises(QueuedJobNotCreatedError, match=message):
        processor.apply_queued_job(manifest, "gco-jobs", "queue-1")


def test_apply_queued_job_create_and_adopt_successes() -> None:
    processor = _bare_processor()
    processor.batch_v1.read_namespaced_job.side_effect = ApiException(status=404)
    processor.batch_v1.create_namespaced_job.return_value = _queued_job_object()
    created = processor.apply_queued_job(_base_job(), "gco-jobs", "queue-1")
    assert (created.status, created.uid) == ("created", "uid-1")

    processor = _bare_processor()
    processor.batch_v1.read_namespaced_job.side_effect = [
        ApiException(status=404),
        _queued_job_object(),
    ]
    processor.batch_v1.create_namespaced_job.side_effect = ApiException(status=409)
    adopted = processor.apply_queued_job(_base_job(), "gco-jobs", "queue-1")
    assert adopted.status == "unchanged"


@pytest.mark.parametrize(
    ("lookup_error", "expected_exception"),
    [
        (ApiException(status=500), "RetryableQueuedJobApplyError"),
        (ApiException(status=403), "ApiException"),
        (RuntimeError("socket closed"), "RetryableQueuedJobApplyError"),
    ],
)
def test_apply_queued_job_lookup_error_matrix(
    lookup_error: Exception, expected_exception: str
) -> None:
    from gco.services.manifest_processor import RetryableQueuedJobApplyError

    processor = _bare_processor()
    processor.batch_v1.read_namespaced_job.side_effect = lookup_error
    exception = (
        RetryableQueuedJobApplyError if expected_exception.startswith("Retryable") else ApiException
    )
    with pytest.raises(exception):
        processor.apply_queued_job(_base_job(), "gco-jobs", "queue-1")


@pytest.mark.parametrize(
    ("create_error", "expected_exception"),
    [
        (ApiException(status=500), "retryable"),
        (ApiException(status=403), "api"),
        (RuntimeError("connection reset"), "retryable"),
    ],
)
def test_apply_queued_job_create_error_matrix(
    create_error: Exception, expected_exception: str
) -> None:
    from gco.services.manifest_processor import RetryableQueuedJobApplyError

    processor = _bare_processor()
    processor.batch_v1.read_namespaced_job.side_effect = ApiException(status=404)
    processor.batch_v1.create_namespaced_job.side_effect = create_error
    exception = RetryableQueuedJobApplyError if expected_exception == "retryable" else ApiException
    with pytest.raises(exception):
        processor.apply_queued_job(_base_job(), "gco-jobs", "queue-1")


@pytest.mark.parametrize(
    ("adoption_error", "expected_exception"),
    [
        (ApiException(status=404), "retryable"),
        (ApiException(status=500), "retryable"),
        (ApiException(status=403), "api"),
        (RuntimeError("read failed"), "retryable"),
    ],
)
def test_apply_queued_job_concurrent_adoption_error_matrix(
    adoption_error: Exception, expected_exception: str
) -> None:
    from gco.services.manifest_processor import RetryableQueuedJobApplyError

    processor = _bare_processor()
    processor.batch_v1.read_namespaced_job.side_effect = [
        ApiException(status=404),
        adoption_error,
    ]
    processor.batch_v1.create_namespaced_job.side_effect = ApiException(status=409)
    exception = RetryableQueuedJobApplyError if expected_exception == "retryable" else ApiException
    with pytest.raises(exception):
        processor.apply_queued_job(_base_job(), "gco-jobs", "queue-1")


def test_apply_queued_job_identity_guards_and_read_delegate() -> None:
    processor = _bare_processor()
    processor.batch_v1.read_namespaced_job.return_value = _queued_job_object(annotation="other")
    with pytest.raises(RuntimeError, match="name collision"):
        processor.apply_queued_job(_base_job(), "gco-jobs", "queue-1")

    processor.batch_v1.read_namespaced_job.return_value = _queued_job_object(uid="")
    with pytest.raises(RuntimeError, match="without a UID"):
        processor.apply_queued_job(_base_job(), "gco-jobs", "queue-1")

    expected = MagicMock()
    processor.batch_v1.read_namespaced_job.return_value = expected
    assert processor.read_queued_job("name", "gco-jobs") is expected
    processor.batch_v1.read_namespaced_job.assert_called_with(
        name="name", namespace="gco-jobs", _request_timeout=7
    )


@pytest.mark.asyncio
async def test_manifest_processor_missing_dynamic_resource_and_condition_scan() -> None:
    processor = _bare_processor()
    api_resource = MagicMock(namespaced=True)
    api_resource.get.return_value = None
    assert (
        await processor._get_existing_resource(
            "batch/v1", "Job", "trainer", "gco-jobs", api_resource=api_resource
        )
        is None
    )

    job = SimpleNamespace(
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(type="Other", status="True"),
                SimpleNamespace(type="Failed", status="True"),
            ],
            active=0,
        )
    )
    assert processor._get_job_status(job) == "failed"


def test_manifest_processor_environment_accepts_nonempty_trust_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gco.services.manifest_processor as module

    monkeypatch.setenv("TRUSTED_REGISTRIES", "registry.example, second.example")
    monkeypatch.setenv("TRUSTED_DOCKERHUB_ORGS", "trustedorg")
    with (
        patch.object(module, "configure_structured_logging"),
        patch.object(module, "ManifestProcessor", return_value=MagicMock()) as constructor,
    ):
        module.create_manifest_processor_from_env()
    config = constructor.call_args.args[2]
    assert config["trusted_registries"] == ["registry.example", "second.example"]
    assert config["trusted_dockerhub_orgs"] == ["trustedorg"]


def test_queue_processor_helpers_cover_suffixes_tolerations_and_ephemeral() -> None:
    import gco.services.queue_processor as queue

    assert queue._parse_memory_string("2k") == 2_000
    assert queue._parse_memory_string("3M") == 3_000_000
    assert queue._positive_quantity("not-a-number") is True
    assert queue._toleration_matches(
        [
            "bad",
            {"key": "other"},
            {"key": "nvidia.com/gpu", "operator": "Equal", "value": "false"},
            {"key": "nvidia.com/gpu", "operator": "Equal", "value": "true"},
        ],
        "nvidia.com/gpu",
    )
    assert queue._iter_containers({"ephemeralContainers": [{"name": "debug"}]}) == [
        ("ephemeralContainer", {"name": "debug"})
    ]
    assert queue._is_image_trusted("") is True


def test_queue_processor_validation_shape_toggle_and_memory_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gco.services.queue_processor as queue

    monkeypatch.setattr(queue, "REQUIRE_ACCELERATOR_TOLERATION", False)
    monkeypatch.setattr(queue, "MAX_MEMORY", 10**15)
    trainjob = {
        "apiVersion": "trainer.kubeflow.org/v1alpha1",
        "kind": "TrainJob",
        "metadata": {"name": "train", "namespace": "gco-jobs"},
        "spec": {
            "runtimePatches": [
                {
                    "spec": {
                        "containers": [
                            {
                                "name": "one",
                                "image": "python:3.14",
                                "resources": {"requests": {"memory": "1Gi"}},
                            },
                            {
                                "name": "two",
                                "image": "python:3.14",
                                "resources": {"requests": {"memory": "2Ki"}},
                            },
                            {
                                "name": "three",
                                "image": "python:3.14",
                                "resources": {"requests": {"memory": "3"}},
                            },
                        ],
                        "volumes": [{"emptyDir": {}}, {"configMap": {"name": "x"}}],
                    }
                }
            ]
        },
    }
    valid, error = queue.validate_manifest(trainjob)
    assert valid is True and error == ""

    monkeypatch.setattr(queue, "MAX_MEMORY", 1)
    valid, error = queue.validate_manifest(trainjob)
    assert valid is False and "Memory" in error


@pytest.mark.parametrize(
    "manifest",
    [
        {"kind": "Job", "spec": {"template": None}},
        {"kind": "Job", "spec": {"template": {"spec": None}}},
        {"kind": "CronJob", "spec": {"jobTemplate": None}},
        {"kind": "CronJob", "spec": {"jobTemplate": {"spec": None}}},
        {"kind": "Pod", "spec": {}},
    ],
)
def test_queue_processor_malformed_pod_shapes(manifest: dict[str, Any]) -> None:
    import gco.services.queue_processor as queue

    assert queue._extract_pod_spec(manifest) is None


def test_queue_processor_unexpected_apply_failure_is_a_failed_status() -> None:
    import gco.services.queue_processor as queue

    resource = MagicMock(namespaced=True)
    resource.create.side_effect = RuntimeError("boom")
    dynamic_client = MagicMock()
    dynamic_client.resources.get.return_value = resource
    with patch.object(queue.dynamic, "DynamicClient", return_value=dynamic_client):
        result = queue.apply_manifest(_base_job())
    assert result.status == "failed"
    assert result.message == "Unexpected apply error: boom"


@pytest.mark.parametrize(
    ("message", "validate_error", "apply_error"),
    [
        ({"Body": "{}"}, None, None),
        ({"ReceiptHandle": "r", "Body": "[]"}, None, None),
        (
            {
                "ReceiptHandle": "r",
                "Body": json.dumps({"job_id": "x", "manifests": ["bad"]}),
            },
            None,
            None,
        ),
        (
            {
                "ReceiptHandle": "r",
                "Body": json.dumps({"job_id": "x", "manifests": [_base_job()]}),
            },
            RuntimeError("validator crashed"),
            None,
        ),
        (
            {
                "ReceiptHandle": "r",
                "Body": json.dumps({"job_id": "x", "manifests": [_base_job()]}),
            },
            None,
            RuntimeError("apply crashed"),
        ),
    ],
)
def test_queue_processor_poison_messages_are_retained(
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, Any],
    validate_error: Exception | None,
    apply_error: Exception | None,
) -> None:
    import gco.services.queue_processor as queue

    monkeypatch.setattr(queue, "QUEUE_URL", "https://queue")
    sqs = MagicMock()
    sqs.receive_message.return_value = {"Messages": [message]}
    with (
        patch.object(queue.boto3, "client", return_value=sqs),
        patch.object(
            queue,
            "validate_manifest",
            side_effect=validate_error,
            return_value=(True, ""),
        ),
        patch.object(queue, "apply_manifest", side_effect=apply_error),
    ):
        assert queue.process_one_message() is False
    sqs.delete_message.assert_not_called()


# ---------------------------------------------------------------------------
# Jobs, queue, template, and manifest routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector",
    ["a/b/c=v", "Bad_Prefix/name=v", "good/name=bad/value", "=value"],
)
def test_jobs_selector_rejects_malformed_keys_and_values(selector: str) -> None:
    from gco.services.api_routes.jobs import _parse_exact_label_selector

    with pytest.raises(ValueError):
        _parse_exact_label_selector(selector)


def test_jobs_label_match_fails_closed_for_non_mapping() -> None:
    from gco.services.api_routes.jobs import _labels_match

    assert _labels_match([], [("team", "ml")]) is False


@pytest.mark.asyncio
async def test_jobs_listing_sort_defaults_status_and_unknown_fields() -> None:
    import gco.services.api_routes.jobs as routes

    processor = _api_processor()
    processor.list_jobs = AsyncMock(
        return_value=[
            {"metadata": {"name": "b", "creationTimestamp": "2"}, "status": {"active": 0}},
            {"metadata": {"name": "a", "creationTimestamp": "1"}, "status": {"active": 2}},
        ]
    )
    with patch.object(routes, "_check_processor", return_value=processor):
        default_response = await routes.list_jobs(
            namespace=None,
            status=None,
            limit=50,
            offset=0,
            sort="name",
            label_selector=None,
        )
        status_response = await routes.list_jobs(
            namespace=None,
            status=None,
            limit=50,
            offset=0,
            sort="status:asc",
            label_selector=None,
        )
        unknown_response = await routes.list_jobs(
            namespace=None,
            status=None,
            limit=50,
            offset=0,
            sort="unknown:asc",
            label_selector=None,
        )
    assert _body(default_response)["jobs"][0]["metadata"]["creationTimestamp"] == "2"
    assert _body(status_response)["jobs"][0]["status"]["active"] == 0
    assert _body(unknown_response)["count"] == 2


@pytest.mark.asyncio
async def test_jobs_get_and_log_lookup_error_translation() -> None:
    import gco.services.api_routes.jobs as routes

    processor = _api_processor()
    processor.batch_v1.read_namespaced_job.side_effect = RuntimeError("permission denied")
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        with pytest.raises(HTTPException) as error:
            await routes.get_job("gco-jobs", "trainer")
        assert error.value.status_code == 500

    processor.batch_v1.read_namespaced_job.side_effect = ApiException(status=404)
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        with pytest.raises(HTTPException) as error:
            await routes.get_job_logs("gco-jobs", "trainer", None, 10, False, None, False)
        assert error.value.status_code == 404

    processor.batch_v1.read_namespaced_job.side_effect = ApiException(status=500, reason="down")
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        with pytest.raises(HTTPException) as error:
            await routes.get_job_logs("gco-jobs", "trainer", None, 10, False, None, False)
        assert error.value.status_code == 502


@pytest.mark.asyncio
async def test_jobs_logs_forward_options_and_classify_bad_requests() -> None:
    import gco.services.api_routes.jobs as routes

    processor = _api_processor()
    processor.batch_v1.read_namespaced_job.return_value = MagicMock()
    processor.core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[_pod()])
    processor.core_v1.read_namespaced_pod_log.return_value = SimpleNamespace(data=b"hello")
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        response = await routes.get_job_logs("gco-jobs", "trainer", "main", 10, False, 60, False)
    assert _body(response)["logs"] == "hello"
    kwargs = processor.core_v1.read_namespaced_pod_log.call_args.kwargs
    assert (kwargs["container"], kwargs["since_seconds"]) == ("main", 60)

    for body, expected_detail in [
        ("container is waiting", "Available containers"),
        ("invalid tail", "Bad request"),
    ]:
        api_error = ApiException(status=400, reason="bad")
        api_error.body = body
        processor.core_v1.read_namespaced_pod_log.side_effect = api_error
        with (
            patch.object(routes, "_check_processor", return_value=processor),
            patch.object(routes, "_check_namespace"),
            pytest.raises(HTTPException) as error,
        ):
            await routes.get_job_logs("gco-jobs", "trainer", None, 10, False, None, False)
        assert expected_detail in str(error.value.detail)


@pytest.mark.asyncio
async def test_jobs_events_pods_and_specific_pod_log_paths() -> None:
    import gco.services.api_routes.jobs as routes

    processor = _api_processor()
    pod = _pod()
    processor.core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[pod])
    processor.core_v1.list_namespaced_event.side_effect = [
        SimpleNamespace(items=[]),
        SimpleNamespace(items=[]),
    ]
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        response = await routes.get_job_events("gco-jobs", "trainer")
    assert _body(response)["count"] == 0
    assert processor.core_v1.list_namespaced_event.call_count == 2

    processor.core_v1.list_namespaced_pod.side_effect = RuntimeError("pods unavailable")
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
        pytest.raises(HTTPException) as error,
    ):
        await routes.get_job_pods("gco-jobs", "trainer")
    assert error.value.status_code == 500

    processor.core_v1.list_namespaced_pod.side_effect = None
    processor.core_v1.read_namespaced_pod.return_value = pod
    processor.core_v1.read_namespaced_pod_log.return_value = SimpleNamespace(data=b"pod log")
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        response = await routes.get_pod_logs("gco-jobs", "trainer", "pod-1", "main", 20, False)
    assert _body(response)["logs"] == "pod log"
    assert processor.core_v1.read_namespaced_pod_log.call_args.kwargs["container"] == "main"

    processor.core_v1.read_namespaced_pod.side_effect = RuntimeError("forbidden")
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
        pytest.raises(HTTPException) as error,
    ):
        await routes.get_pod_logs("gco-jobs", "trainer", "pod-1", None, 20, False)
    assert error.value.status_code == 500


@pytest.mark.asyncio
async def test_jobs_metrics_bare_units_iteration_failure_and_outer_failure() -> None:
    import gco.services.api_routes.jobs as routes

    processor = _api_processor()
    processor.core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[_pod()])
    processor.custom_objects.get_namespaced_custom_object.return_value = {
        "containers": [{"name": "main", "usage": {"cpu": "2", "memory": "123"}}]
    }
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        response = await routes.get_job_metrics("gco-jobs", "trainer")
    assert _body(response)["summary"] == {
        "total_cpu_millicores": 2000,
        "total_memory_bytes": 123,
        "total_memory_mib": 0.0,
        "pod_count": 1,
    }

    class ExplodingItems:
        def __bool__(self) -> bool:
            return True

        def __iter__(self):
            raise RuntimeError("iterator failed")

        def __len__(self) -> int:
            return 0

    processor.core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=ExplodingItems())
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        response = await routes.get_job_metrics("gco-jobs", "trainer")
    assert _body(response)["pods"] == []

    processor.core_v1.list_namespaced_pod.side_effect = RuntimeError("api down")
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
        pytest.raises(HTTPException) as error,
    ):
        await routes.get_job_metrics("gco-jobs", "trainer")
    assert error.value.status_code == 500


@pytest.mark.asyncio
async def test_jobs_bulk_delete_missing_timestamp_and_per_job_failure() -> None:
    import gco.services.api_routes.jobs as routes
    from gco.services.api_shared import BulkDeleteRequest

    processor = _api_processor()
    processor.list_jobs = AsyncMock(
        return_value=[{"metadata": {"name": "trainer", "namespace": "gco-jobs"}}]
    )
    processor.batch_v1.delete_namespaced_job.side_effect = RuntimeError("race")
    request = BulkDeleteRequest(older_than_days=1, dry_run=False)
    with patch.object(routes, "_check_processor", return_value=processor):
        response = await routes.bulk_delete_jobs(request)
    payload = _body(response)
    assert payload["total_matched"] == 1
    assert payload["failed"] == [{"name": "trainer", "namespace": "gco-jobs", "error": "race"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("template", [{"status": {"phase": "old"}}, "not-a-mapping"])
async def test_jobs_retry_sanitizes_template_status_when_possible(template: Any) -> None:
    import gco.services.api_routes.jobs as routes
    from gco.models import ManifestSubmissionResponse

    processor = _api_processor()
    original = SimpleNamespace(
        metadata=SimpleNamespace(labels={}, annotations={}),
        spec=SimpleNamespace(
            parallelism=1,
            completions=1,
            backoff_limit=2,
            template=SimpleNamespace(to_dict=lambda: template),
        ),
    )
    processor.batch_v1.read_namespaced_job.return_value = original
    processor.process_manifest_submission = AsyncMock(
        return_value=ManifestSubmissionResponse(True, "cluster", "us-east-1", [])
    )
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        response = await routes.retry_job("gco-jobs", "trainer")
    assert response.status_code == 201
    submitted = processor.process_manifest_submission.call_args.args[0].manifests[0]
    if isinstance(template, dict):
        assert "status" not in submitted["spec"]["template"]
    else:
        assert submitted["spec"]["template"] == "not-a-mapping"


def _queue_request(**overrides: Any) -> Any:
    from gco.services.api_shared import QueuedJobRequest

    values = {
        "manifest": _base_job(),
        "target_region": "us-east-1",
        "namespace": "gco-jobs",
    }
    values.update(overrides)
    return QueuedJobRequest(**values)


@pytest.mark.parametrize(
    ("queued_request", "configured", "detail"),
    [
        (_queue_request(target_region="us-west-2"), "us-east-1", "not deployed"),
        (_queue_request(target_region="invalid"), "", "valid AWS region"),
        (_queue_request(namespace="other"), "", "namespace is not allowed"),
        (_queue_request(namespace="Bad_Name"), "", "valid Kubernetes name"),
        (
            _queue_request(manifest={"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "x"}}),
            "",
            "accepts only",
        ),
        (
            _queue_request(manifest={"apiVersion": "batch/v1", "kind": "Job", "metadata": []}),
            "",
            "must be an object",
        ),
        (
            _queue_request(
                manifest={"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "Bad_Name"}}
            ),
            "",
            "name is invalid",
        ),
        (
            _queue_request(
                manifest={
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {"name": "x", "namespace": "default"},
                }
            ),
            "",
            "must match",
        ),
    ],
)
def test_queue_route_validation_matrix(queued_request: Any, configured: str, detail: str) -> None:
    import gco.services.api_routes.queue as routes

    processor = _api_processor()
    if detail == "valid Kubernetes name":
        processor.allowed_namespaces.add(queued_request.namespace)
    with (
        patch.dict(os.environ, {"QUEUE_TARGET_REGIONS": configured}),
        patch.object(routes, "_check_processor", return_value=processor),
        pytest.raises(HTTPException) as error,
    ):
        routes._validated_queue_manifest(queued_request)
    assert detail in str(error.value.detail)


@pytest.mark.asyncio
async def test_queue_route_idempotency_conflict_and_cursor_validation() -> None:
    import gco.services.api_routes.queue as routes
    from gco.services.template_store import JobSubmissionConflict

    with pytest.raises(HTTPException) as error:
        await routes.submit_job_to_queue(_queue_request(), "bad key!")
    assert error.value.status_code == 422

    store = MagicMock()
    store.submit_job.side_effect = JobSubmissionConflict("already used")
    with (
        patch.object(routes, "_validated_queue_manifest", return_value=_base_job()),
        patch.object(routes, "_validated_spot_gate", return_value=None),
        patch.object(routes, "_get_job_store", return_value=store),
        pytest.raises(HTTPException) as error,
    ):
        await routes.submit_job_to_queue(_queue_request(), "stable-key")
    assert error.value.status_code == 409

    store.list_jobs_page.side_effect = ValueError("cursor mismatch")
    with (
        patch.object(routes, "_get_job_store", return_value=store),
        pytest.raises(HTTPException) as error,
    ):
        await routes.list_queued_jobs(None, None, None, 10, "cursor")
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_template_routes_create_failure_and_manifest_injection() -> None:
    import gco.services.api_routes.templates as routes
    from gco.models import ManifestSubmissionResponse
    from gco.services.api_shared import JobFromTemplateRequest, JobTemplateRequest

    store = MagicMock()
    store.create_template.side_effect = RuntimeError("ddb down")
    with (
        patch.object(routes, "_get_template_store", return_value=store),
        pytest.raises(HTTPException) as error,
    ):
        await routes.create_template(JobTemplateRequest(name="t", manifest=_base_job()))
    assert error.value.status_code == 500

    processor = _api_processor()
    processor.process_manifest_submission = AsyncMock(
        return_value=ManifestSubmissionResponse(True, "cluster", "us-east-1", [])
    )
    store.get_template.return_value = {
        "manifest": {"apiVersion": "batch/v1", "kind": "Job", "spec": {}}
    }
    with (
        patch.object(routes, "_get_template_store", return_value=store),
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
    ):
        response = await routes.create_job_from_template(
            "t", JobFromTemplateRequest(name="run", namespace="gco-jobs")
        )
    assert response.status_code == 201
    manifest = processor.process_manifest_submission.call_args.args[0].manifests[0]
    assert manifest["metadata"]["labels"] == {"gco.io/template": "t"}

    store.get_template.return_value = {
        "manifest": {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"labels": {"team": "ml"}},
            "spec": {},
        }
    }
    processor.process_manifest_submission.side_effect = RuntimeError("processor failed")
    with (
        patch.object(routes, "_get_template_store", return_value=store),
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
        pytest.raises(HTTPException) as error,
    ):
        await routes.create_job_from_template(
            "t", JobFromTemplateRequest(name="run", namespace="gco-jobs")
        )
    assert error.value.status_code == 500


@pytest.mark.asyncio
async def test_manifest_route_dry_run_skips_metrics_and_metric_failure_is_nonfatal() -> None:
    import gco.services.api_routes.manifests as routes
    import gco.services.manifest_api as api
    from gco.models import ManifestSubmissionResponse, ResourceStatus
    from gco.services.api_shared import ManifestSubmissionAPIRequest

    processor = _api_processor()
    result = ManifestSubmissionResponse(
        True,
        "cluster",
        "us-east-1",
        [ResourceStatus("batch/v1", "Job", "trainer", "gco-jobs", "created")],
    )
    processor.process_manifest_submission = AsyncMock(return_value=result)
    metrics = MagicMock()
    metrics.publish_submission_metrics.side_effect = RuntimeError("cloudwatch down")
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(api, "manifest_metrics", metrics),
    ):
        dry = await routes.submit_manifests(
            ManifestSubmissionAPIRequest(manifests=[_base_job()], dry_run=True)
        )
        live = await routes.submit_manifests(
            ManifestSubmissionAPIRequest(manifests=[_base_job()], dry_run=False)
        )
    assert dry.status_code == live.status_code == 200
    metrics.publish_submission_metrics.assert_called_once()


# ---------------------------------------------------------------------------
# API lifecycle, health, auth, webhooks, and request middleware
# ---------------------------------------------------------------------------


def test_manifest_api_environment_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    import gco.services.manifest_api as api

    monkeypatch.setenv("BOOL", "YES")
    monkeypatch.setenv("NUMBER", "bad")
    assert api._env_bool("BOOL") is True
    assert api._env_number("NUMBER", 5.0, 1.0, 10.0) == 5.0
    monkeypatch.setenv("NUMBER", "50")
    assert api._env_number("NUMBER", 5.0, 1.0, 10.0) == 5.0


@pytest.mark.asyncio
async def test_manifest_api_enabled_worker_lifecycle_times_out_and_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gco.services.manifest_api as api

    processor = _api_processor()
    worker = MagicMock()

    async def never() -> None:
        await asyncio.Event().wait()

    worker.run = MagicMock(side_effect=never)
    real_create_task = asyncio.create_task
    monkeypatch.setenv("CENTRAL_QUEUE_WORKER_ENABLED", "true")
    with (
        patch.object(api, "create_manifest_processor_from_env", return_value=processor),
        patch.object(api, "configure_structured_logging"),
        patch.object(api, "ManifestProcessorMetrics", return_value=MagicMock()),
        patch.object(api, "get_template_store", return_value=MagicMock()),
        patch.object(api, "get_webhook_store", return_value=MagicMock()),
        patch.object(api, "get_job_store", return_value=MagicMock()),
        patch.object(api, "CentralQueueWorker", return_value=worker) as constructor,
        patch.object(
            api.asyncio,
            "create_task",
            side_effect=lambda coroutine, name=None: real_create_task(coroutine),
        ),
        patch.object(api.asyncio, "wait_for", new=AsyncMock(side_effect=TimeoutError)),
    ):
        async with api.lifespan(api.app):
            assert api.app.state.central_queue_worker is worker
    constructor.assert_called_once()
    worker.stop.assert_called_once()
    assert api.app.state.central_queue_worker_task.cancelled()


@pytest.mark.asyncio
async def test_manifest_api_readiness_health_status_and_policy_edges() -> None:
    import gco.services.manifest_api as api

    api.manifest_processor = _api_processor()
    api.app.state.central_queue_worker_task = MagicMock(done=lambda: True)
    with pytest.raises(HTTPException) as error:
        await api.kubernetes_readiness_check()
    assert error.value.status_code == 503

    processor = MagicMock()
    processor.core_v1.list_namespace.return_value = None
    type(processor).cluster_id = PropertyMock(side_effect=RuntimeError("vanished"))
    api.manifest_processor = processor
    response = await api.health_check()
    assert response.status_code == 503
    assert _body(response)["error"] == "manifest processor unavailable"

    api.manifest_processor = None
    api.template_store = MagicMock()
    api.template_store.list_templates.return_value = [{"name": "a"}]
    api.webhook_store = None
    status = await api.get_service_status()
    assert (status["templates_count"], status["webhooks_count"]) == (1, 0)

    api.template_store = None
    api.webhook_store = MagicMock()
    api.webhook_store.list_webhooks.return_value = [{"id": "w"}]
    status = await api.get_service_status()
    assert (status["templates_count"], status["webhooks_count"]) == (0, 1)
    assert "cluster_id" not in status

    with pytest.raises(HTTPException) as error:
        await api.get_job_validation_policy()
    assert error.value.status_code == 503


def _new_health_monitor() -> Any:
    from gco.services.health_monitor import HealthMonitor

    monitor = object.__new__(HealthMonitor)
    monitor.cluster_id = "cluster"
    monitor.region = "us-east-1"
    monitor._k8s_timeout = 7
    monitor._alb_sync_lease_name = "lease"
    monitor._alb_sync_lease_namespace = "gco-system"
    monitor._alb_sync_lease_duration = 90
    monitor._alb_sync_holder = "pod-a"
    monitor._alb_sync_interval = 300
    monitor._last_alb_sync = None
    monitor.core_v1 = MagicMock()
    monitor.coordination_v1 = MagicMock()
    monitor.metrics_v1beta1 = MagicMock()
    return monitor


def test_health_monitor_clamps_short_alb_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    from gco.models import ResourceThresholds
    from gco.services.health_monitor import _ALB_SYNC_LEASE_MIN_SECONDS, HealthMonitor

    monkeypatch.setenv("ALB_SYNC_LEASE_DURATION", "1")
    with (
        patch("gco.services.health_monitor.config") as config,
        patch("gco.services.health_monitor.client"),
        patch("gco.services.health_monitor.logger.warning") as warning,
    ):
        config.ConfigException = k8s_config.ConfigException
        monitor = HealthMonitor("cluster", "us-east-1", ResourceThresholds(80, 80, 80))
    assert monitor._alb_sync_lease_duration == _ALB_SYNC_LEASE_MIN_SECONDS
    warning.assert_called_once()


@pytest.mark.asyncio
async def test_health_monitor_loops_multiple_pending_pods_and_containers() -> None:
    monitor = _new_health_monitor()

    def resources(cpu, memory):
        return SimpleNamespace(resources=SimpleNamespace(requests={"cpu": cpu, "memory": memory}))

    first = SimpleNamespace(
        metadata=SimpleNamespace(namespace="gco-jobs"),
        status=SimpleNamespace(phase="Pending"),
        spec=SimpleNamespace(containers=[resources("100m", "1Ki"), resources("200m", "2Ki")]),
    )
    second = SimpleNamespace(
        metadata=SimpleNamespace(namespace="default"),
        status=SimpleNamespace(phase="Pending"),
        spec=SimpleNamespace(containers=[resources("300m", "3Ki")]),
    )
    monitor.core_v1.list_pod_for_all_namespaces.return_value = SimpleNamespace(
        items=[first, second]
    )
    result = await monitor._calculate_pending_requested_resources()
    assert result.cpu_vcpus == pytest.approx(0.6)
    assert result.memory_gb == pytest.approx(6 * 1024 / 1024**3)


@pytest.mark.asyncio
async def test_health_monitor_all_thresholds_can_be_disabled() -> None:
    from gco.models import RequestedResources, ResourceThresholds, ResourceUtilization

    monitor = _new_health_monitor()
    monitor.thresholds = ResourceThresholds(
        cpu_threshold=-1,
        memory_threshold=-1,
        gpu_threshold=-1,
        pending_pods_threshold=-1,
        pending_requested_cpu_vcpus=-1,
        pending_requested_memory_gb=-1,
        pending_requested_gpus=-1,
    )
    monitor.get_cluster_metrics = AsyncMock(
        return_value=(
            ResourceUtilization(cpu=99, memory=99, gpu=99),
            1,
            999,
            RequestedResources(cpu_vcpus=999, memory_gb=999, gpus=999),
        )
    )
    status = await monitor.get_health_status()
    assert status.status == "healthy"


@pytest.mark.parametrize(
    ("holder", "renew_time"),
    [
        (None, None),
        ("other", None),
        ("other", datetime.now() - timedelta(seconds=120)),
    ],
)
def test_health_monitor_lease_acquires_unowned_missing_or_naive_expired(
    holder: str | None, renew_time: datetime | None
) -> None:
    monitor = _new_health_monitor()
    spec = SimpleNamespace(
        holder_identity=holder,
        renew_time=renew_time,
        lease_duration_seconds=90,
        acquire_time=None,
        lease_transitions=0,
    )
    monitor.coordination_v1.read_namespaced_lease.return_value = SimpleNamespace(spec=spec)
    assert monitor._try_acquire_alb_sync_lease() is True
    assert spec.holder_identity == "pod-a"


def test_health_monitor_lease_api_and_generic_failures_are_nonfatal() -> None:
    monitor = _new_health_monitor()
    lease = SimpleNamespace(
        spec=SimpleNamespace(
            holder_identity=None,
            renew_time=None,
            lease_duration_seconds=90,
            acquire_time=None,
            lease_transitions=0,
        )
    )
    monitor.coordination_v1.read_namespaced_lease.return_value = lease
    monitor.coordination_v1.replace_namespaced_lease.side_effect = ApiException(status=500)
    assert monitor._try_acquire_alb_sync_lease() is False

    monitor.coordination_v1.read_namespaced_lease.side_effect = ApiException(status=403)
    assert monitor._try_acquire_alb_sync_lease() is False
    monitor.coordination_v1.read_namespaced_lease.side_effect = RuntimeError("broken client")
    assert monitor._try_acquire_alb_sync_lease() is False


@pytest.mark.parametrize("code", ["ParameterNotFound", "AccessDeniedException"])
def test_health_monitor_ssm_missing_and_nonmissing_parameter_errors(code: str) -> None:
    monitor = _new_health_monitor()
    monitor._try_acquire_alb_sync_lease = MagicMock(side_effect=[True, True])
    monitor.metrics_v1beta1.get_namespaced_custom_object.return_value = {
        "status": {"addresses": [{"type": "Hostname", "value": "alb.example"}]}
    }
    ssm = MagicMock()
    ssm.get_parameter.side_effect = _client_error(code)
    with patch("gco.services.health_monitor.boto3.client", return_value=ssm):
        monitor._sync_alb_registration()
    if code == "ParameterNotFound":
        ssm.put_parameter.assert_called_once()
    else:
        ssm.put_parameter.assert_not_called()


@pytest.mark.asyncio
async def test_health_monitor_main_loop_logs_error_and_stops_dispatcher() -> None:
    import gco.services.health_monitor as module

    monitor = MagicMock()
    monitor.get_health_status = AsyncMock(side_effect=RuntimeError("metrics failed"))
    dispatcher = MagicMock()
    dispatcher.start = AsyncMock()
    dispatcher.stop = AsyncMock()
    with (
        patch.object(module, "create_health_monitor_from_env", return_value=monitor),
        patch(
            "gco.services.webhook_dispatcher.create_webhook_dispatcher_from_env",
            return_value=dispatcher,
        ),
        patch.object(module, "configure_structured_logging"),
        patch.object(module.asyncio, "sleep", new=AsyncMock(side_effect=KeyboardInterrupt)),
    ):
        await module.main()
    dispatcher.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_api_lifespan_tolerates_metrics_failure_and_stops_webhooks() -> None:
    import gco.services.health_api as api

    monitor = MagicMock(cluster_id="cluster", region="us-east-1")
    dispatcher = MagicMock()
    dispatcher.start = AsyncMock()
    dispatcher.stop = AsyncMock()

    async def never() -> None:
        await asyncio.Event().wait()

    real_create_task = asyncio.create_task
    with (
        patch.object(api, "create_health_monitor_from_env", return_value=monitor),
        patch.object(api, "configure_structured_logging"),
        patch.object(api, "HealthMonitorMetrics", side_effect=RuntimeError("no credentials")),
        patch.object(api, "background_health_monitor", side_effect=never),
        patch.object(api, "create_webhook_dispatcher_from_env", return_value=dispatcher),
        patch.object(
            api.asyncio,
            "create_task",
            side_effect=lambda coroutine: real_create_task(coroutine),
        ),
    ):
        async with api.lifespan(api.app):
            assert api.health_metrics is None
    dispatcher.start.assert_awaited_once()
    dispatcher.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_api_background_monitor_wait_publish_failure_and_cancel_paths() -> None:
    import gco.services.health_api as api

    api.health_monitor = None
    with patch.object(api.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        await api.background_health_monitor()

    status = MagicMock()
    status.status = "healthy"
    status.resource_utilization.cpu = 1
    status.resource_utilization.memory = 2
    status.resource_utilization.gpu = 3
    status.active_jobs = 4
    status.get_threshold_violations.return_value = []
    monitor = MagicMock()
    monitor.get_health_status = AsyncMock(return_value=status)
    monitor.sync_alb_registration = AsyncMock()
    metrics = MagicMock()
    api.health_monitor = monitor
    api.health_metrics = metrics
    with patch.object(api.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        await api.background_health_monitor()
    metrics.publish_resource_utilization.assert_called_once()
    metrics.publish_health_status.assert_called_once()

    metrics.reset_mock()
    metrics.publish_resource_utilization.side_effect = RuntimeError("cloudwatch down")
    with patch.object(api.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        await api.background_health_monitor()
    metrics.publish_resource_utilization.assert_called_once()

    api.health_metrics = None
    with patch.object(api.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
        await api.background_health_monitor()


@pytest.mark.asyncio
async def test_health_api_uncached_health_and_dispatcher_status_metrics() -> None:
    import gco.services.health_api as api
    from gco.models import HealthStatus, RequestedResources, ResourceThresholds, ResourceUtilization

    status = HealthStatus(
        cluster_id="cluster",
        region="us-east-1",
        timestamp=datetime.now(),
        status="healthy",
        resource_utilization=ResourceUtilization(1, 2, 3),
        thresholds=ResourceThresholds(80, 80, 80),
        active_jobs=1,
        pending_pods=0,
        pending_requested=RequestedResources(0, 0, 0),
    )
    monitor = MagicMock()
    monitor.get_health_status = AsyncMock(return_value=status)
    api.health_monitor = monitor
    api.current_health_status = None
    response = await api.health_check()
    assert response.status_code == 200

    dispatcher = MagicMock()
    dispatcher.get_metrics.return_value = {
        "running": True,
        "deliveries_total": 3,
        "deliveries_success": 2,
        "deliveries_failed": 1,
    }
    api.webhook_dispatcher = dispatcher
    service = await api.get_status()
    assert service["webhook_dispatcher"]["deliveries_total"] == 3


@pytest.mark.asyncio
async def test_auth_configuration_cache_nonce_and_signature_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gco.services.auth_middleware as auth

    monkeypatch.setenv("BAD_FLOAT", "not-a-float")
    assert auth._bounded_env_float("BAD_FLOAT", 4.0, 1.0, 5.0) == 4.0

    auth._secrets_client = None
    monkeypatch.setenv("AUTH_SECRET_ARN", "not-an-arn")
    with patch.object(auth.boto3, "client", return_value=MagicMock()) as client:
        auth.get_secrets_client()
    assert client.call_args.kwargs["region_name"] is None

    auth.clear_token_cache()
    missing = MagicMock()
    missing.get_secret_value.return_value = {"SecretString": "{}"}
    monkeypatch.setenv("AUTH_SECRET_ARN", "arn:test")
    with patch.object(auth, "get_secrets_client", return_value=missing):
        assert auth._refresh_cache() is False

    resource_not_found = type("ResourceNotFoundException", (Exception,), {})
    overlap = MagicMock()
    overlap.exceptions.ResourceNotFoundException = resource_not_found
    overlap.get_secret_value.side_effect = [
        {"SecretString": '{"token":"current"}'},
        {"SecretString": '{"token":""}'},
        resource_not_found(),
    ]
    with patch.object(auth, "get_secrets_client", return_value=overlap):
        assert auth._refresh_cache() is True
    assert auth._cached_tokens == {"current"}

    auth._seen_nonces = {"expired": 1.0, "old": 50.0}
    monkeypatch.setattr(auth, "_MAX_TRACKED_NONCES", 1)
    assert auth._accept_nonce("new", 10.0) is True
    assert auth._seen_nonces == {"new": 10.0 + auth.SIGNATURE_MAX_AGE_SECONDS}

    base = {
        "x-gco-signature-version": "v1",
        "x-gco-signature": "0" * 64,
        "x-gco-nonce": "a" * 32,
        "x-gco-content-sha256": hashlib.sha256(b"body").hexdigest(),
    }
    assert (
        await auth._has_valid_signature(_request(base | {"x-gco-timestamp": "bad"}), {"k"}) is False
    )
    stale = str(int(time.time() - auth.SIGNATURE_MAX_AGE_SECONDS - 5))
    assert (
        await auth._has_valid_signature(_request(base | {"x-gco-timestamp": stale}), {"k"}) is False
    )
    current = str(int(time.time()))
    mismatch = base | {"x-gco-timestamp": current, "x-gco-content-sha256": "f" * 64}
    assert await auth._has_valid_signature(_request(mismatch, b"body"), {"k"}) is False


def _bare_dispatcher() -> Any:
    from gco.services.webhook_dispatcher import WebhookDispatcher

    dispatcher = object.__new__(WebhookDispatcher)
    dispatcher.cluster_id = "cluster"
    dispatcher.region = "us-east-1"
    dispatcher.timeout = 1
    dispatcher.max_retries = 1
    dispatcher.retry_delay = 0
    dispatcher.allowed_domains = []
    dispatcher.namespaces = ["gco-jobs"]
    dispatcher.webhook_store = MagicMock()
    dispatcher._deliveries_total = 0
    dispatcher._deliveries_success = 0
    dispatcher._deliveries_failed = 0
    dispatcher._running = False
    dispatcher._job_state_cache = MagicMock()
    dispatcher.batch_v1 = MagicMock()
    return dispatcher


def test_webhook_resolution_rejects_invalid_ip_port_family_and_address() -> None:
    import gco.services.webhook_dispatcher as webhooks

    assert webhooks._globally_routable_unicast("not-an-ip") is None
    target, error = webhooks._resolve_webhook_target("https://example.com:bad")
    assert target is None and "invalid port" in str(error)

    with patch.object(
        webhooks.socket,
        "getaddrinfo",
        return_value=[(socket.AF_UNIX, 0, 0, "", ("8.8.8.8", 443))],
    ):
        target, error = webhooks._resolve_webhook_target("https://example.com")
    assert target is None and "unsupported" in str(error)

    with patch.object(
        webhooks.socket,
        "getaddrinfo",
        return_value=[(socket.AF_INET, 0, 0, "", (123, 443))],
    ):
        target, error = webhooks._resolve_webhook_target("https://example.com")
    assert target is None and "non-text" in str(error)

    class TruthyEmpty:
        def __bool__(self) -> bool:
            return True

        def __iter__(self):
            return iter(())

    with patch.object(webhooks.socket, "getaddrinfo", return_value=TruthyEmpty()):
        target, error = webhooks._resolve_webhook_target("https://example.com")
    assert target is None and "no usable addresses" in str(error)


def test_webhook_job_status_scans_past_unmatched_failure() -> None:
    dispatcher = _bare_dispatcher()
    job = SimpleNamespace(
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(type="Failed", status="False"),
                SimpleNamespace(type="Failed", status="True"),
            ],
            active=0,
            succeeded=0,
            failed=0,
        )
    )
    assert dispatcher._compute_job_status(job) == "failed"


@pytest.mark.asyncio
async def test_webhook_nonstring_url_cancel_and_dispatch_cancellation() -> None:
    import gco.services.webhook_dispatcher as webhooks

    dispatcher = _bare_dispatcher()
    result = await dispatcher._deliver_webhook({"id": "w", "url": 123}, {"event": "job.failed"})
    assert result.success is False and "must be a string" in str(result.error)

    target = SimpleNamespace(
        host_header="example.com",
        hostname="example.com",
        log_identity="host=example.com",
        pinned_url=lambda attempt: "https://8.8.8.8:443/hook",
    )
    http_client = MagicMock()
    http_client.post = AsyncMock(side_effect=asyncio.CancelledError)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=http_client)
    context.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(webhooks, "_resolve_webhook_target", return_value=(target, None)),
        patch.object(webhooks.httpx, "AsyncClient", return_value=context),
        pytest.raises(asyncio.CancelledError),
    ):
        await dispatcher._deliver_webhook(
            {"id": "w", "url": "https://example.com/hook"},
            {"event": "job.failed"},
        )
    assert dispatcher._deliveries_failed == 2

    dispatcher.webhook_store.get_webhooks_for_event.side_effect = [
        [{"id": "one", "url": "https://one.example"}],
        [{"id": "two", "url": "https://two.example", "namespace": None}],
    ]
    job = SimpleNamespace(metadata=SimpleNamespace(namespace="gco-jobs", name="job"))
    delivered = webhooks.WebhookDeliveryResult("one", "u", "job.failed", True)
    with (
        patch.object(dispatcher, "_build_payload", return_value={"event": "job.failed"}),
        patch.object(
            dispatcher,
            "_deliver_webhook",
            new=MagicMock(side_effect=lambda *_args, **_kwargs: object()),
        ),
        patch.object(
            webhooks.asyncio,
            "gather",
            new=AsyncMock(return_value=[delivered, asyncio.CancelledError()]),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await dispatcher._dispatch_event(webhooks.WebhookEvent.JOB_FAILED, job)


def test_webhook_sync_watch_stops_before_buffering_when_not_running() -> None:
    import gco.services.webhook_dispatcher as webhooks

    dispatcher = _bare_dispatcher()
    watch = MagicMock()
    watch.stream.return_value = [{"type": "ADDED", "object": MagicMock()}]
    with patch.object(webhooks, "Watch", return_value=watch):
        assert dispatcher._sync_watch_jobs() == []


@pytest.mark.asyncio
async def test_request_size_middleware_replays_mixed_messages_and_falls_back() -> None:
    from gco.services.request_size_middleware import RequestSizeLimitMiddleware

    with pytest.raises(ValueError):
        RequestSizeLimitMiddleware(AsyncMock(), -1)

    received_by_app: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        received_by_app.extend([await receive(), await receive(), await receive(), await receive()])

    messages = iter(
        [
            {"type": "custom.event"},
            {"type": "http.request", "body": b"a", "more_body": True},
            {"type": "http.request", "body": b"b", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> dict[str, Any]:
        return next(messages)

    middleware = RequestSizeLimitMiddleware(app, 10)
    scope = {"type": "http", "headers": [(b"content-length", b"not-ascii-\xff")]}
    await middleware(scope, receive, AsyncMock())
    assert [message["type"] for message in received_by_app] == [
        "custom.event",
        "http.request",
        "http.request",
        "http.disconnect",
    ]

    disconnected: list[dict[str, Any]] = []

    async def disconnect_app(scope: Any, receive: Any, send: Any) -> None:
        disconnected.append(await receive())

    async def disconnect_receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    await RequestSizeLimitMiddleware(disconnect_app, 10)(
        {"type": "http", "headers": []}, disconnect_receive, AsyncMock()
    )
    assert disconnected == [{"type": "http.disconnect"}]


# ---------------------------------------------------------------------------
# Shared formatting, cost, spot, worker, rotator, and admission helpers
# ---------------------------------------------------------------------------


def test_api_shared_condition_and_container_state_fallthroughs() -> None:
    from gco.services.api_shared import _parse_job_to_dict, _parse_pod_to_dict

    job = SimpleNamespace(
        metadata=SimpleNamespace(
            name="j",
            namespace="gco-jobs",
            creation_timestamp=None,
            labels=None,
            annotations=None,
            uid="uid",
        ),
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(
                    type="Other",
                    status="True",
                    reason=None,
                    message=None,
                    last_transition_time=None,
                ),
                SimpleNamespace(
                    type="Failed",
                    status="True",
                    reason="failed",
                    message="boom",
                    last_transition_time=None,
                ),
            ],
            active=0,
            succeeded=0,
            failed=1,
            start_time=None,
            completion_time=None,
        ),
        spec=SimpleNamespace(
            parallelism=1,
            completions=1,
            backoff_limit=1,
            template=SimpleNamespace(spec=SimpleNamespace(containers=[], init_containers=[])),
        ),
    )
    assert _parse_job_to_dict(job)["computed_status"] == "failed"

    states = [
        SimpleNamespace(name="none", ready=False, restart_count=0, image="x", state=None),
        SimpleNamespace(
            name="empty",
            ready=False,
            restart_count=0,
            image="x",
            state=SimpleNamespace(running=None, waiting=None, terminated=None),
        ),
    ]
    pod = _pod()
    pod.status.container_statuses = states
    parsed = _parse_pod_to_dict(pod)
    assert parsed["status"]["containerStatuses"] == [
        {"name": "none", "ready": False, "restartCount": 0, "image": "x"},
        {"name": "empty", "ready": False, "restartCount": 0, "image": "x"},
    ]


@pytest.mark.asyncio
async def test_cost_api_cancel_paths_lifespan_timeout_and_factory() -> None:
    import gco.services.cost_api as cost

    with (
        patch.object(
            cost.asyncio,
            "to_thread",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await cost._scheduled_report_loop(MagicMock(), asyncio.Event())

    monitor = MagicMock(cluster="cluster", region="us-east-1")
    task = MagicMock()
    task.cancel = MagicMock()

    def capture_task(coroutine: Any, **_kwargs: Any) -> MagicMock:
        coroutine.close()
        return task

    with (
        patch.object(cost, "create_cost_monitor_from_env", return_value=monitor),
        patch.object(cost, "configure_structured_logging"),
        patch.object(cost.asyncio, "create_task", side_effect=capture_task),
        patch.object(cost.asyncio, "wait_for", new=AsyncMock(side_effect=TimeoutError)),
    ):
        async with cost.lifespan(cost.app):
            assert cost.app.state.scheduled_report_task is task
    task.cancel.assert_called_once()
    assert cost.create_app() is cost.app


def test_spot_price_gate_lazily_creates_bounded_ec2_client() -> None:
    import gco.services.spot_price_gate as spot

    ec2 = MagicMock()
    ec2.describe_spot_price_history.return_value = {
        "SpotPriceHistory": [{"AvailabilityZone": "us-east-1a", "SpotPrice": "0.42"}]
    }
    with patch("boto3.client", return_value=ec2) as client:
        gate = spot.SpotPriceGate("us-east-1")
        assert gate.current_min_spot_price("g5.xlarge") == 0.42
    assert client.call_args.args[0] == "ec2"
    assert client.call_args.kwargs["region_name"] == "us-east-1"
    assert gate._ec2 is ec2


def test_structured_logging_custom_extra_and_text_formatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gco.services.structured_logging import (
        StructuredJsonFormatter,
        configure_structured_logging,
    )

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    record.request_id = "req-1"
    assert json.loads(StructuredJsonFormatter().format(record))["request_id"] == "req-1"

    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    try:
        monkeypatch.setenv("LOG_FORMAT", "text")
        configure_structured_logging(service_name="test")
        assert not isinstance(root.handlers[0].formatter, StructuredJsonFormatter)
        assert "%(asctime)s" in root.handlers[0].formatter._fmt
    finally:
        root.handlers[:] = old_handlers
        root.setLevel(old_level)


@pytest.mark.asyncio
async def test_central_worker_migration_warning_reconcile_pending_skip_and_pre_stopped_run() -> (
    None
):
    import gco.services.central_queue_worker as worker

    processor = MagicMock(region="us-east-1")
    store = MagicMock(claim_lease_seconds=300)
    store.migrate_legacy_records_for_region.return_value = {
        "migrated": 1,
        "failed": 1,
        "complete": False,
    }
    store.get_queued_jobs_for_region.return_value = []

    async def inline(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    with (
        patch.object(worker.asyncio, "to_thread", side_effect=inline),
        patch.object(worker.logger, "warning") as warning,
    ):
        assert await worker.process_queued_jobs_once(processor, store, limit=1) == (0, [])
    warning.assert_called_once()

    store.get_active_jobs_for_region.return_value = [
        {
            "job_id": "id",
            "k8s_job_name": "trainer",
            "k8s_job_namespace": "gco-jobs",
            "k8s_job_uid": "uid",
            "status": "running",
        }
    ]
    processor.read_queued_job.return_value = SimpleNamespace(
        metadata=SimpleNamespace(uid="uid"),
        status=SimpleNamespace(active=0, conditions=[]),
    )
    with patch.object(worker.asyncio, "to_thread", side_effect=inline):
        assert await worker.reconcile_active_jobs_once(processor, store, limit=1) == 0
    store.transition_job.assert_not_called()

    central = worker.CentralQueueWorker(processor=processor, store=store)
    central.stop()
    await central.run()
    assert central.running is False


def test_grafana_rotator_falls_back_to_kubeconfig() -> None:
    import gco.services.grafana_rotator as rotator

    with (
        patch.object(
            rotator.config,
            "load_incluster_config",
            side_effect=rotator.config.ConfigException("outside cluster"),
        ),
        patch.object(rotator.config, "load_kube_config") as kubeconfig,
        patch.object(rotator.client, "CoreV1Api", return_value=MagicMock()),
        patch.object(rotator, "rotate") as rotate,
    ):
        assert rotator.main() == 0
    kubeconfig.assert_called_once()
    rotate.assert_called_once()


def test_opencost_client_skips_nonmapping_allocation_sets() -> None:
    import gco.services.cost_monitor as costs

    response = MagicMock(status_code=200)
    response.json.return_value = {"data": ["bad", {"ml": {"cpuCost": 1}, "also-bad": []}]}
    with patch.object(costs.httpx, "get", return_value=response):
        allocations = costs.OpenCostClient("http://opencost").get_allocation(
            datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 2, tzinfo=UTC)
        )
    assert allocations == {"ml": {"cpuCost": 1}}


def test_job_admission_trainjob_quantity_toleration_and_shape_edges() -> None:
    import gco.job_admission as admission

    trainjob = {
        "kind": "TrainJob",
        "spec": {
            "trainer": {"numNodes": "bad"},
            "runtimePatches": [{"spec": {"containers": [{"name": "embedded"}]}}],
        },
    }
    specs = admission.extract_trainjob_pod_specs(trainjob)
    assert specs.trainer is None and specs.num_nodes == 1 and len(specs.embedded) == 1
    assert admission._positive_quantity({"invalid": True}) is True
    assert admission._toleration_matches(
        [
            [],
            {"key": "other"},
            {"key": "nvidia.com/gpu", "operator": "Equal", "value": "false"},
            {"key": "nvidia.com/gpu", "operator": "Equal", "value": "true"},
        ],
        "nvidia.com/gpu",
    )

    malformed = [
        {"kind": "Job", "spec": {"template": None}},
        {"kind": "Job", "spec": {"template": {"spec": None}}},
        {"kind": "CronJob", "spec": {"jobTemplate": None}},
        {"kind": "CronJob", "spec": {"jobTemplate": {"spec": None}}},
    ]
    assert all(admission.extract_pod_spec(item) is None for item in malformed)

    weighted = admission.weighted_pod_specs(
        {
            "kind": "TrainJob",
            "spec": {"runtimePatches": [{"spec": {"containers": []}}]},
        }
    )
    assert weighted == [({"containers": []}, 1)]
    assert admission.weighted_pod_specs({"kind": "CronJob", "spec": {}}) == [({}, 1)]
    assert admission.weighted_pod_specs(
        {"kind": "Pod", "spec": {"containers": [{"name": "x"}]}}
    ) == [({"containers": [{"name": "x"}]}, 1)]


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [("1k", 1e3), ("2M", 2e6), ("3G", 3e9), ("4T", 4e12), ("5P", 5e15)],
)
def test_resource_governance_decimal_suffixes(quantity: str, expected: float) -> None:
    from gco.resource_governance import parse_k8s_quantity

    assert parse_k8s_quantity(quantity) == expected


def test_resource_governance_empty_quantity_and_manifest_model_conversion() -> None:
    from gco.models.manifest_models import ManifestSubmissionRequest
    from gco.resource_governance import parse_k8s_quantity

    with pytest.raises(ValueError, match="empty resource quantity"):
        parse_k8s_quantity("  ")
    request = ManifestSubmissionRequest(
        manifests=[
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a"}, "data": {}},
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "b"}},
        ]
    )
    converted = request.get_kubernetes_manifests()
    assert [manifest.get_name() for manifest in converted] == ["a", "b"]


# ---------------------------------------------------------------------------
# CLI job manager and jobs commands
# ---------------------------------------------------------------------------


def _manager() -> Any:
    from cli.jobs import JobManager

    manager = object.__new__(JobManager)
    manager.config = SimpleNamespace(
        default_namespace="gco-jobs", default_region="us-east-1", project_name="gco"
    )
    manager._aws_client = MagicMock()
    return manager


def test_cli_identity_namespace_resource_mapping_and_image_shape_helpers() -> None:
    from cli.jobs import _extract_image_refs, _first_manifest_namespace, resolve_submission_identity

    assert _first_manifest_namespace([{}, {"metadata": {"namespace": "ml"}}]) == "ml"
    assert resolve_submission_identity(
        {"resources": {"kind": "Job", "name": "trainer", "namespace": "ml"}}
    ) == ("trainer", "ml")
    assert _extract_image_refs({"template": {"spec": {"containers": {"bad": True}}}}) == []


def test_cli_submit_job_creates_metadata_and_updates_existing_labels() -> None:
    manager = _manager()
    manifests = [
        {"apiVersion": "batch/v1", "kind": "Job", "spec": {}},
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"labels": {"existing": "yes"}},
            "data": {},
        },
    ]
    manager._aws_client.submit_manifests.return_value = {"success": True}
    result = manager.submit_job(manifests, labels={"team": "ml"})
    assert result == {"success": True}
    assert manifests[0]["metadata"] == {
        "namespace": "gco-jobs",
        "labels": {"team": "ml"},
    }
    assert manifests[1]["metadata"]["labels"] == {"existing": "yes", "team": "ml"}


def test_cli_direct_submission_handles_nonjobs_multiple_output_and_no_job() -> None:
    manager = _manager()
    manager._aws_client.get_regional_stack.return_value = SimpleNamespace(cluster_name="cluster")
    manifests = [
        {"apiVersion": "v1", "kind": "ConfigMap", "data": {}},
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"labels": {"existing": "yes"}},
            "data": {},
        },
    ]
    completed = SimpleNamespace(
        returncode=0,
        stdout="configmap/a created\nsecret/b created\n",
        stderr="",
    )
    with (
        patch("cli.kubectl_helpers.update_kubeconfig"),
        patch("subprocess.run", return_value=completed),
    ):
        result = manager.submit_job_direct(manifests, "us-east-1", labels={"team": "ml"})
    assert result["resources"] == ["configmap/a created", "secret/b created"]
    assert result["job_name"] is None
    assert manifests[0]["metadata"]["namespace"] == "gco-jobs"
    assert manifests[1]["metadata"]["labels"] == {"existing": "yes", "team": "ml"}


def test_cli_list_parse_trainjob_logs_cloudwatch_and_wait_paths(capsys: Any) -> None:
    from cli.jobs import JobInfo

    manager = _manager()
    manager._aws_client.discover_regional_stacks.return_value = ["bad", "good"]
    manager._query_jobs_in_region = MagicMock(
        side_effect=[RuntimeError("region unavailable"), [MagicMock(name="job")]]
    )
    assert len(manager.list_jobs(all_regions=True)) == 1

    parsed = manager._parse_job_info(
        {
            "metadata": {"name": "j"},
            "status": {
                "conditions": [
                    {"type": "Other", "status": "True"},
                    {"type": "Failed", "status": "True"},
                ]
            },
        },
        "us-east-1",
    )
    assert parsed.status == "failed"

    manager._aws_client.call_api.return_value = {"resource": {}}
    assert manager._get_trainjob_info("train", "gco-jobs", "us-east-1") is None
    train = manager._parse_trainjob_info(
        {
            "metadata": {"name": "train"},
            "status": {
                "conditions": [
                    {"type": "Failed", "status": "False"},
                    {"type": "Other", "status": "True"},
                    {"type": "Failed", "status": "True"},
                ]
            },
            "spec": {"trainer": {"numNodes": "bad"}},
        },
        "us-east-1",
    )
    assert (train.status, train.parallelism, train.created_time) == ("failed", 1, None)

    manager._aws_client.get_job_pods.return_value = {"pods": []}
    with pytest.raises(RuntimeError, match="no pods found"):
        manager._get_trainjob_node_logs("train", "gco-jobs", "us-east-1", 0, 10)

    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q"}
    logs.get_query_results.return_value = {
        "status": "Complete",
        "results": [
            [
                {"field": "other", "value": "ignored"},
                {"field": "@message", "value": '{"log":"first"}'},
            ],
            [{"field": "@message", "value": "second"}],
        ],
    }
    manager._aws_client._session.client.return_value = logs
    with patch("time.sleep"):
        output = manager._get_cloudwatch_logs("j", "us-east-1")
    assert output.endswith("first\nsecond")

    pending = JobInfo(
        "j",
        "gco-jobs",
        "us-east-1",
        "running",
        failed_pods=2,
        completions=1,
    )
    complete = JobInfo("j", "gco-jobs", "us-east-1", "succeeded")
    manager.get_job = MagicMock(side_effect=[pending, complete])
    with patch("time.time", side_effect=[0, 1, 2]), patch("time.sleep"):
        assert manager.wait_for_job("j", "gco-jobs", poll_interval=0) is complete
    assert "2 failed" in capsys.readouterr().err


def test_cli_sqs_submission_and_queue_status_without_dlq() -> None:
    manager = _manager()
    stack = SimpleNamespace(stack_name="stack")
    manager._aws_client.get_regional_stack.return_value = stack
    cloudformation = MagicMock()
    cloudformation.describe_stacks.return_value = {
        "Stacks": [{"Outputs": [{"OutputKey": "JobQueueUrl", "OutputValue": "queue"}]}]
    }
    sqs = MagicMock()
    sqs.send_message.return_value = {"MessageId": "message"}
    sqs.get_queue_attributes.return_value = {
        "Attributes": {
            "ApproximateNumberOfMessages": "2",
            "ApproximateNumberOfMessagesNotVisible": "1",
            "ApproximateNumberOfMessagesDelayed": "0",
        }
    }

    def client(service: str, region_name: str) -> MagicMock:
        return cloudformation if service == "cloudformation" else sqs

    manifests = [
        {"apiVersion": "v1", "kind": "ConfigMap", "data": {}},
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"labels": {"existing": "yes"}},
            "data": {},
        },
    ]
    with patch("boto3.client", side_effect=client):
        submitted = manager.submit_job_sqs(
            manifests,
            "us-east-1",
            namespace="ml",
            labels={"team": "training"},
        )
        status = manager.get_queue_status("us-east-1")
    assert submitted["job_name"] is None
    assert manifests[0]["metadata"]["namespace"] == "ml"
    assert manifests[1]["metadata"]["labels"] == {
        "existing": "yes",
        "team": "training",
    }
    assert status == {
        "region": "us-east-1",
        "queue_url": "queue",
        "messages_available": 2,
        "messages_in_flight": 1,
        "messages_delayed": 0,
    }


def test_cli_job_manager_thin_delegates_forward_defaults_and_arguments() -> None:
    manager = _manager()
    aws = manager._aws_client
    methods = [
        (
            manager.list_jobs_global,
            (),
            {"namespace": "ml", "status": "running", "limit": 7},
            aws.get_global_jobs,
        ),
        (manager.get_global_health, (), {}, aws.get_global_health),
        (manager.get_global_status, (), {}, aws.get_global_status),
        (
            manager.bulk_delete_global,
            (),
            {
                "namespace": "ml",
                "status": "failed",
                "older_than_days": 3,
                "label_selector": "team=ml",
                "dry_run": False,
            },
            aws.bulk_delete_global,
        ),
        (manager.get_job_events, ("j", "ml"), {}, aws.get_job_events),
        (manager.get_job_pods, ("j", "ml"), {}, aws.get_job_pods),
        (
            manager.get_pod_logs,
            ("j", "pod", "ml"),
            {"tail_lines": 8, "container": "main"},
            aws.get_pod_logs,
        ),
        (manager.get_job_metrics, ("j", "ml"), {}, aws.get_job_metrics),
        (manager.retry_job, ("j", "ml"), {}, aws.retry_job),
        (
            manager.bulk_delete_jobs,
            (),
            {
                "namespace": "ml",
                "status": "failed",
                "older_than_days": 2,
                "label_selector": "team=ml",
                "dry_run": False,
            },
            aws.bulk_delete_jobs,
        ),
    ]
    for method, args, kwargs, delegate in methods:
        delegate.return_value = {"ok": delegate._mock_name}
        assert method(*args, **kwargs) == {"ok": delegate._mock_name}
        delegate.assert_called_once()


def _invoke_jobs(args: list[str], manager: MagicMock, formatter: MagicMock | None = None) -> Any:
    from click.testing import CliRunner

    from cli.main import cli

    formatter = formatter or MagicMock()
    with (
        patch("cli.commands.jobs_cmd.get_job_manager", return_value=manager),
        patch("cli.commands.jobs_cmd.get_output_formatter", return_value=formatter),
    ):
        return CliRunner().invoke(cli, args), formatter


def test_jobs_policy_table_handles_absent_security_and_enforcement() -> None:
    manager = MagicMock()
    manager._aws_client.get_job_validation_policy.return_value = {
        "region": "us-east-1",
        "cluster_id": "cluster",
        "policy": {
            "manifest_caps": {},
            "allowed_namespaces": [],
            "allowed_kinds": [],
            "trusted_registries": [],
            "trusted_dockerhub_orgs": [],
        },
    }
    result, _ = _invoke_jobs(["jobs", "policy", "--region", "us-east-1"], manager)
    assert result.exit_code == 0, result.output
    assert "Job Validation Policy" in result.output


def test_jobs_check_policy_clean_offline_structured_and_table(tmp_path: Any) -> None:
    path = tmp_path / "job.yaml"
    path.write_text("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: j\nspec: {}\n")
    manager = MagicMock()
    manager.load_manifests.return_value = [_base_job()]
    patches = (
        patch("cli.commands.jobs_cmd._cdk_job_validation_policy", return_value=({}, "cdk.json")),
        patch("gco.job_admission.JobValidationPolicy.from_cdk_context", return_value=MagicMock()),
        patch("cli.job_policy.evaluate_manifests", return_value=[]),
    )
    with patches[0], patches[1], patches[2]:
        result, formatter = _invoke_jobs(
            ["--output", "json", "jobs", "check-policy", str(path), "--offline"], manager
        )
    assert result.exit_code == 0
    assert formatter.print.call_args.args[0]["admissible"] is True

    with (
        patch("cli.commands.jobs_cmd._cdk_job_validation_policy", return_value=({}, "cdk.json")),
        patch("gco.job_admission.JobValidationPolicy.from_cdk_context", return_value=MagicMock()),
        patch("cli.job_policy.evaluate_manifests", return_value=[]),
    ):
        result, _ = _invoke_jobs(["jobs", "check-policy", str(path), "--offline"], manager)
    assert result.exit_code == 0
    assert "no violations" in result.output


def test_jobs_check_policy_online_multiple_manifests_reject_is_advisory(tmp_path: Any) -> None:
    from cli.job_policy import VERDICT_REJECT, RegionPolicy, RegionVerdict

    path = tmp_path / "job.yaml"
    path.write_text("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: j\nspec: {}\n")
    manager = MagicMock()
    manifests = [_base_job("one"), _base_job("two")]
    manager.load_manifests.return_value = manifests
    policies = [RegionPolicy(region="us-east-1", status="ok", policy=MagicMock())]
    verdicts = [RegionVerdict(region="us-east-1", verdict=VERDICT_REJECT)]
    with (
        patch("cli.job_policy.fetch_region_policies", return_value=policies),
        patch("cli.job_policy.region_verdicts", return_value=verdicts),
        patch("cli.job_policy.detect_policy_drift", return_value=[]),
        patch("cli.job_policy.registry_drift", return_value=None),
        patch("cli.job_policy.ecr_augmentation", return_value={}),
    ):
        result, formatter = _invoke_jobs(
            [
                "--output",
                "json",
                "jobs",
                "check-policy",
                str(path),
                "--region",
                "us-east-1",
            ],
            manager,
        )
    assert result.exit_code == 0
    assert formatter.print.call_args.args[0]["verdicts"][0]["verdict"] == VERDICT_REJECT
    assert all(manifest["metadata"]["namespace"] == "gco-jobs" for manifest in manifests)


# ---------------------------------------------------------------------------
# Cross-boundary regression cases discovered while closing baseline gaps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "spec"),
    [
        ("Job", None),
        ("Job", {"template": None}),
        ("Job", {"template": []}),
        ("Job", {"template": {"spec": None}}),
        ("CronJob", {"jobTemplate": None}),
        ("CronJob", {"jobTemplate": {"spec": None}}),
        ("CronJob", {"jobTemplate": {"spec": {"template": None}}}),
    ],
)
def test_queue_processor_rejects_malformed_workload_shapes_without_raising(
    kind: str, spec: Any
) -> None:
    import gco.services.queue_processor as queue

    manifest = {
        "apiVersion": "batch/v1",
        "kind": kind,
        "metadata": {"name": "broken", "namespace": "gco-jobs"},
        "spec": spec,
    }
    valid, error = queue.validate_manifest(manifest)
    assert valid is False
    assert "valid pod spec" in error


def test_queue_processor_uses_kubernetes_memory_parser_and_retains_bad_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gco.services.queue_processor as queue

    manifest = _base_job()
    manifest["spec"]["template"]["spec"]["containers"][0]["resources"] = {
        "requests": {"memory": "2M"}
    }
    monkeypatch.setattr(queue, "MAX_MEMORY", 2_000_000)
    assert queue.validate_manifest(manifest) == (True, "")
    monkeypatch.setattr(queue, "MAX_MEMORY", 1_999_999)
    valid, error = queue.validate_manifest(manifest)
    assert valid is False and "Memory" in error

    malformed = _base_job("malformed")
    malformed["spec"]["template"] = None
    monkeypatch.setattr(queue, "QUEUE_URL", "https://queue")
    sqs = MagicMock()
    sqs.receive_message.return_value = {
        "Messages": [
            {
                "ReceiptHandle": "receipt",
                "Body": json.dumps({"job_id": "bad-shape", "manifests": [malformed]}),
            }
        ]
    }
    with (
        patch.object(queue.boto3, "client", return_value=sqs),
        patch.object(queue, "apply_manifest") as apply_manifest,
    ):
        assert queue.process_one_message() is False
    apply_manifest.assert_not_called()
    sqs.delete_message.assert_not_called()


@pytest.mark.parametrize(
    "manifest",
    [
        {"kind": "Job", "spec": None},
        {"kind": "Job", "spec": {"template": None}},
        {"kind": "Job", "spec": {"template": []}},
        {"kind": "CronJob", "spec": {"jobTemplate": None}},
        {"kind": "CronJob", "spec": {"jobTemplate": {"spec": None}}},
    ],
)
def test_job_admission_weights_malformed_workloads_without_crashing(
    manifest: dict[str, Any],
) -> None:
    from gco.job_admission import weighted_pod_specs

    assert weighted_pod_specs(manifest) == [({}, 1)]


@pytest.mark.asyncio
async def test_health_api_lifespan_cleans_up_when_consumer_raises() -> None:
    import gco.services.health_api as api

    monitor = MagicMock(cluster_id="cluster", region="us-east-1")
    dispatcher = MagicMock()
    dispatcher.start = AsyncMock()
    dispatcher.stop = AsyncMock()

    async def never() -> None:
        await asyncio.Event().wait()

    real_create_task = asyncio.create_task
    with (
        patch.object(api, "create_health_monitor_from_env", return_value=monitor),
        patch.object(api, "configure_structured_logging"),
        patch.object(api, "HealthMonitorMetrics", return_value=MagicMock()),
        patch.object(api, "background_health_monitor", side_effect=never),
        patch.object(api, "create_webhook_dispatcher_from_env", return_value=dispatcher),
        patch.object(
            api.asyncio,
            "create_task",
            side_effect=lambda coroutine: real_create_task(coroutine),
        ),
        pytest.raises(RuntimeError, match="consumer failed"),
    ):
        async with api.lifespan(api.app):
            raise RuntimeError("consumer failed")

    assert api.health_check_task.cancelled()
    dispatcher.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_dispatches_multiple_results_and_buffers_live_watch_event() -> None:
    import gco.services.webhook_dispatcher as webhooks

    dispatcher = _bare_dispatcher()
    dispatcher.webhook_store.get_webhooks_for_event.side_effect = [
        [{"id": "namespace", "url": "https://namespace.example"}],
        [{"id": "global", "url": "https://global.example", "namespace": None}],
    ]
    job = SimpleNamespace(metadata=SimpleNamespace(namespace="gco-jobs", name="trainer"))

    async def deliver(webhook: dict[str, Any], payload: dict[str, Any]) -> Any:
        assert payload == {"event": "job.completed"}
        return webhooks.WebhookDeliveryResult(webhook["id"], webhook["url"], "job.completed", True)

    with (
        patch.object(dispatcher, "_build_payload", return_value={"event": "job.completed"}),
        patch.object(dispatcher, "_deliver_webhook", side_effect=deliver),
    ):
        results = await dispatcher._dispatch_event(webhooks.WebhookEvent.JOB_COMPLETED, job)
    assert [result.webhook_id for result in results] == ["namespace", "global"]

    watched_job = MagicMock()
    dispatcher._running = True
    watch = MagicMock()
    watch.stream.return_value = [{"type": "ADDED", "object": watched_job}]
    with patch.object(webhooks, "Watch", return_value=watch):
        assert dispatcher._sync_watch_jobs() == [("ADDED", watched_job)]


@pytest.mark.asyncio
async def test_central_worker_reconciles_terminal_uid_and_stops_after_requeue() -> None:
    import gco.services.central_queue_worker as worker

    processor = MagicMock(region="us-east-1")
    store = MagicMock(claim_lease_seconds=300)
    store.get_active_jobs_for_region.return_value = [
        {
            "job_id": "queue-1",
            "k8s_job_name": "trainer",
            "k8s_job_namespace": "gco-jobs",
            "k8s_job_uid": "uid-1",
            "status": "running",
        }
    ]
    processor.read_queued_job.return_value = SimpleNamespace(
        metadata=SimpleNamespace(uid="uid-1"),
        status=SimpleNamespace(
            active=0,
            conditions=[SimpleNamespace(type="Complete", status="True", reason=None, message=None)],
        ),
    )
    store.transition_job.return_value = {"status": "succeeded"}

    async def inline(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    with patch.object(worker.asyncio, "to_thread", side_effect=inline):
        assert await worker.reconcile_active_jobs_once(processor, store, limit=1) == 1
    transition = store.transition_job.call_args
    assert transition.args == ("queue-1",)
    assert transition.kwargs["expected_status"] == "running"
    assert transition.kwargs["status"] == "succeeded"
    assert transition.kwargs["expected_k8s_uid"] == "uid-1"

    store.reset_mock()
    store.requeue_expired_jobs.return_value = 0
    central = worker.CentralQueueWorker(processor=processor, store=store)

    async def stop_after_requeue(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        result = function(*args, **kwargs)
        central.stop()
        return result

    with (
        patch.object(worker.asyncio, "to_thread", side_effect=stop_after_requeue),
        patch.object(worker, "process_queued_jobs_once", new=AsyncMock()) as process,
    ):
        await central.run()
    process.assert_not_awaited()
    assert central.running is False


def test_queue_route_accepts_a_configured_target_region() -> None:
    import gco.services.api_routes.queue as routes

    request = _queue_request(target_region="us-east-1")
    with (
        patch.dict(os.environ, {"QUEUE_TARGET_REGIONS": "us-west-2, us-east-1"}),
        patch.object(routes, "_check_processor", return_value=_api_processor()),
    ):
        manifest = routes._validated_queue_manifest(request)
    assert manifest["metadata"]["namespace"] == "gco-jobs"
    assert manifest is not request.manifest


@pytest.mark.asyncio
async def test_template_route_preserves_processor_http_error() -> None:
    import gco.services.api_routes.templates as routes
    from gco.services.api_shared import JobFromTemplateRequest

    store = MagicMock()
    store.get_template.return_value = {
        "manifest": {"apiVersion": "batch/v1", "kind": "Job", "spec": {}},
    }
    processor = _api_processor()
    processor.process_manifest_submission = AsyncMock(
        side_effect=HTTPException(status_code=409, detail="submission conflict")
    )
    with (
        patch.object(routes, "_get_template_store", return_value=store),
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
        pytest.raises(HTTPException) as error,
    ):
        await routes.create_job_from_template(
            "t", JobFromTemplateRequest(name="run", namespace="gco-jobs")
        )
    assert (error.value.status_code, error.value.detail) == (409, "submission conflict")


def test_cli_cloudwatch_logs_preserve_non_object_and_non_string_envelopes() -> None:
    manager = _manager()
    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q"}
    logs.get_query_results.return_value = {
        "status": "Complete",
        "results": [
            [{"field": "@message", "value": "42"}],
            [{"field": "@message", "value": '["list"]'}],
            [{"field": "@message", "value": '{"log":42}'}],
            [{"field": "@message", "value": '{"log":"clean"}'}],
        ],
    }
    manager._aws_client._session.client.return_value = logs
    with patch("time.sleep"):
        output = manager._get_cloudwatch_logs("trainer", "us-east-1")
    assert output.endswith('42\n["list"]\n{"log":42}\nclean')


def test_cli_sqs_labels_create_metadata_without_namespace_override() -> None:
    manager = _manager()
    manager._aws_client.get_regional_stack.return_value = SimpleNamespace(stack_name="stack")
    cloudformation = MagicMock()
    cloudformation.describe_stacks.return_value = {
        "Stacks": [{"Outputs": [{"OutputKey": "JobQueueUrl", "OutputValue": "queue"}]}]
    }
    sqs = MagicMock()
    sqs.send_message.return_value = {"MessageId": "message"}

    def client(service: str, region_name: str) -> MagicMock:
        assert region_name == "us-east-1"
        return cloudformation if service == "cloudformation" else sqs

    manifests = [{"apiVersion": "v1", "kind": "ConfigMap", "data": {"key": "value"}}]
    with patch("boto3.client", side_effect=client):
        result = manager.submit_job_sqs(manifests, "us-east-1", labels={"team": "training"})
    assert manifests[0]["metadata"] == {"labels": {"team": "training"}}
    assert result["namespace"] == "gco-jobs"
    envelope = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
    assert envelope["namespace"] == "gco-jobs"


@pytest.mark.parametrize("pod_spec", [None, []])
def test_pod_extractors_reject_deeply_malformed_cronjob_specs(pod_spec: Any) -> None:
    import gco.job_admission as admission
    import gco.services.queue_processor as queue
    from gco.services.manifest_processor import ManifestProcessor

    manifest = {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": "broken", "namespace": "gco-jobs"},
        "spec": {"jobTemplate": {"spec": {"template": {"spec": pod_spec}}}},
    }
    assert ManifestProcessor._extract_pod_spec(manifest) is None
    assert admission.weighted_pod_specs(manifest) == [({}, 1)]
    valid, error = queue.validate_manifest(manifest)
    assert valid is False and "valid pod spec" in error


def test_queue_processor_accepts_multiple_explicitly_non_root_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gco.services.queue_processor as queue

    manifest = _base_job()
    manifest["spec"]["template"]["spec"]["containers"] = [
        {
            "name": name,
            "image": "python:3.14",
            "securityContext": {"runAsUser": 1000},
        }
        for name in ("trainer", "sidecar")
    ]
    monkeypatch.setattr(queue, "BLOCK_RUN_AS_ROOT", True)
    assert queue.validate_manifest(manifest) == (True, "")


@pytest.mark.asyncio
async def test_health_api_uninitialized_monitor_waits_again_until_cancelled() -> None:
    import gco.services.health_api as api

    api.health_monitor = None
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
    with patch.object(api.asyncio, "sleep", new=sleep):
        await api.background_health_monitor()
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_webhook_isolates_delivery_failure_and_processes_live_watch_event() -> None:
    import gco.services.webhook_dispatcher as webhooks

    dispatcher = _bare_dispatcher()
    dispatcher.webhook_store.get_webhooks_for_event.side_effect = [
        [{"id": "broken", "url": "https://broken.example"}],
        [{"id": "healthy", "url": "https://healthy.example", "namespace": None}],
    ]
    job = SimpleNamespace(metadata=SimpleNamespace(namespace="gco-jobs", name="trainer"))

    async def deliver(webhook: dict[str, Any], _payload: dict[str, Any]) -> Any:
        if webhook["id"] == "broken":
            raise RuntimeError("delivery implementation escaped")
        return webhooks.WebhookDeliveryResult(webhook["id"], webhook["url"], "job.failed", True)

    with (
        patch.object(dispatcher, "_build_payload", return_value={"event": "job.failed"}),
        patch.object(dispatcher, "_deliver_webhook", side_effect=deliver),
    ):
        results = await dispatcher._dispatch_event(webhooks.WebhookEvent.JOB_FAILED, job)
    assert [(result.webhook_id, result.success) for result in results] == [
        ("broken", False),
        ("healthy", True),
    ]

    dispatcher._running = True

    async def process(event_type: str, watched_job: Any) -> None:
        assert (event_type, watched_job) == ("MODIFIED", job)
        dispatcher._running = False

    with (
        patch.object(
            webhooks.asyncio,
            "to_thread",
            new=AsyncMock(return_value=[("MODIFIED", job)]),
        ),
        patch.object(dispatcher, "_process_job_event", side_effect=process) as process_event,
    ):
        await dispatcher._watch_jobs()
    process_event.assert_awaited_once_with("MODIFIED", job)


@pytest.mark.asyncio
async def test_jobs_log_read_kubernetes_failure_is_reported_as_bad_gateway() -> None:
    import gco.services.api_routes.jobs as routes

    processor = _api_processor()
    processor.batch_v1.read_namespaced_job.return_value = MagicMock()
    processor.core_v1.list_namespaced_pod.return_value = SimpleNamespace(items=[_pod()])
    processor.core_v1.read_namespaced_pod_log.side_effect = ApiException(
        status=503, reason="unavailable"
    )
    with (
        patch.object(routes, "_check_processor", return_value=processor),
        patch.object(routes, "_check_namespace"),
        pytest.raises(HTTPException) as error,
    ):
        await routes.get_job_logs("gco-jobs", "trainer", None, 10, False, None, False)
    assert error.value.status_code == 502


def test_cli_cloudwatch_logs_ignore_rows_without_a_message_field() -> None:
    manager = _manager()
    logs = MagicMock()
    logs.start_query.return_value = {"queryId": "q"}
    logs.get_query_results.return_value = {
        "status": "Complete",
        "results": [
            [{"field": "other", "value": "metadata"}],
            [{"field": "@message", "value": '{"log":"visible"}'}],
        ],
    }
    manager._aws_client._session.client.return_value = logs
    with patch("time.sleep"):
        output = manager._get_cloudwatch_logs("trainer", "us-east-1")
    assert output.endswith("\nvisible")
    assert "metadata" not in output


def test_cli_direct_submission_ignores_blank_kubectl_output_lines() -> None:
    manager = _manager()
    manager._aws_client.get_regional_stack.return_value = SimpleNamespace(cluster_name="cluster")
    completed = SimpleNamespace(
        returncode=0,
        stdout="configmap/a created\n\nsecret/b created\n",
        stderr="",
    )
    manifests = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "a"}},
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "b"}},
    ]
    with (
        patch("cli.kubectl_helpers.update_kubeconfig"),
        patch("subprocess.run", return_value=completed),
    ):
        result = manager.submit_job_direct(manifests, "us-east-1")
    assert result["resources"] == ["configmap/a created", "secret/b created"]


def test_job_store_expired_claim_dedup_ignores_an_older_reappearance() -> None:
    from gco.services.template_store import JobStatus

    store = _bare_job_store()
    store._query_worker_index = MagicMock(
        return_value=[
            {"job_id": "a", "lease_expires_at": "1"},
            {"job_id": "a", "lease_expires_at": "3"},
            {"job_id": "a", "lease_expires_at": "2"},
            {"job_id": "b", "lease_expires_at": "4"},
        ]
    )
    expired = store._query_expired_claims("us-east-1", JobStatus.CLAIMED.value, "9", 10)
    assert [(item["job_id"], item["lease_expires_at"]) for item in expired] == [
        ("a", "3"),
        ("b", "4"),
    ]


@pytest.mark.asyncio
async def test_health_monitor_counts_multiple_pods_and_skips_unrequested_sidecar() -> None:
    monitor = _new_health_monitor()

    def container(cpu: str, memory: str) -> Any:
        return SimpleNamespace(resources=SimpleNamespace(requests={"cpu": cpu, "memory": memory}))

    pods = [
        SimpleNamespace(
            metadata=SimpleNamespace(namespace="gco-jobs"),
            status=SimpleNamespace(phase="Pending"),
            spec=SimpleNamespace(
                containers=[container("100m", "1Ki"), SimpleNamespace(resources=None)]
            ),
        ),
        SimpleNamespace(
            metadata=SimpleNamespace(namespace="default"),
            status=SimpleNamespace(phase="Running"),
            spec=SimpleNamespace(containers=[container("200m", "2Ki")]),
        ),
        SimpleNamespace(
            metadata=SimpleNamespace(namespace="gco-jobs"),
            status=SimpleNamespace(phase="Succeeded"),
            spec=None,
        ),
    ]
    monitor.core_v1.list_pod_for_all_namespaces.return_value = SimpleNamespace(items=pods)
    assert await monitor._get_pod_counts() == (1, 1)
    requested = await monitor._calculate_pending_requested_resources()
    assert requested.cpu_vcpus == pytest.approx(0.1)
    assert requested.memory_gb == pytest.approx(1024 / 1024**3)


def test_job_admission_rejects_cronjob_with_nonmapping_template() -> None:
    import gco.job_admission as admission

    manifest = {
        "kind": "CronJob",
        "spec": {"jobTemplate": {"spec": {"template": None}}},
    }
    assert admission.extract_pod_spec(manifest) is None
    assert admission.weighted_pod_specs(manifest) == [({}, 1)]


@pytest.mark.asyncio
async def test_webhook_accounts_for_invalid_delivery_result_and_shutdown_race() -> None:
    import gco.services.webhook_dispatcher as webhooks

    dispatcher = _bare_dispatcher()
    dispatcher.webhook_store.get_webhooks_for_event.side_effect = [
        [{"id": "invalid", "url": "https://invalid.example"}],
        [{"id": "healthy", "url": "https://healthy.example", "namespace": None}],
    ]
    job = SimpleNamespace(metadata=SimpleNamespace(namespace="gco-jobs", name="trainer"))

    async def deliver(webhook: dict[str, Any], _payload: dict[str, Any]) -> Any:
        if webhook["id"] == "invalid":
            return None
        return webhooks.WebhookDeliveryResult(webhook["id"], webhook["url"], "job.failed", True)

    with (
        patch.object(dispatcher, "_build_payload", return_value={"event": "job.failed"}),
        patch.object(dispatcher, "_deliver_webhook", side_effect=deliver),
    ):
        results = await dispatcher._dispatch_event(webhooks.WebhookEvent.JOB_FAILED, job)
    assert [(result.webhook_id, result.success) for result in results] == [
        ("invalid", False),
        ("healthy", True),
    ]
    assert (dispatcher._deliveries_total, dispatcher._deliveries_failed) == (1, 1)

    dispatcher._running = True

    async def stop_before_events_return(*_args: Any, **_kwargs: Any) -> Any:
        dispatcher._running = False
        return [("MODIFIED", job)]

    with (
        patch.object(webhooks.asyncio, "to_thread", side_effect=stop_before_events_return),
        patch.object(dispatcher, "_process_job_event", new=AsyncMock()) as process_event,
    ):
        await dispatcher._watch_jobs()
    process_event.assert_not_awaited()
