"""
Tests for the DynamoDB-backed stores in gco/services/template_store.py.

Covers TemplateStore (list/get/create/update/delete with pagination
and duplicate-name guard), WebhookStore (namespace-scoped queries,
event-filtered fanout, HMAC secret round-trip), and JobStore (idempotent
submission, renewable fenced claims, compare-and-set lifecycle transitions,
priority-indexed polling, opaque bounded pagination and counts, legacy-record
migration, and queued-only cancellation). Also pins the JobStatus enum values
and the module-level singleton getters (get_template_store, get_webhook_store,
get_job_store), including ClientError propagation across store operations.
"""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from gco.services.template_store import (
    JobStatus,
    JobStore,
    TemplateStore,
    WebhookStore,
    get_job_store,
    get_template_store,
    get_webhook_store,
)

# =============================================================================
# TemplateStore Tests
# =============================================================================


class TestTemplateStore:
    """Tests for TemplateStore class."""

    @pytest.fixture
    def mock_dynamodb(self):
        """Create a mock DynamoDB resource."""
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            yield mock_table

    @pytest.fixture
    def template_store(self, mock_dynamodb):
        """Create a TemplateStore with mocked DynamoDB."""
        store = TemplateStore(table_name="test-templates", region="us-east-1")
        store._table = mock_dynamodb
        return store

    def test_init_with_defaults(self):
        """Test TemplateStore initialization with default values."""
        with patch("boto3.resource"):
            store = TemplateStore()
            assert store.table_name == "gco-job-templates"
            assert store.region == "us-east-1"

    def test_init_with_custom_values(self):
        """Test TemplateStore initialization with custom values."""
        with patch("boto3.resource"):
            store = TemplateStore(table_name="custom-table", region="eu-west-1")
            assert store.table_name == "custom-table"
            assert store.region == "eu-west-1"

    def test_list_templates_empty(self, template_store, mock_dynamodb):
        """Test listing templates when none exist."""
        mock_dynamodb.scan.return_value = {"Items": []}

        result = template_store.list_templates()

        assert result == []
        mock_dynamodb.scan.assert_called_once()

    def test_list_templates_with_items(self, template_store, mock_dynamodb):
        """Test listing templates with existing items."""
        mock_dynamodb.scan.return_value = {
            "Items": [
                {
                    "template_name": "template-1",
                    "description": "First template",
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                },
                {
                    "template_name": "template-2",
                    "description": "Second template",
                    "created_at": "2024-01-02T00:00:00Z",
                    "updated_at": "2024-01-02T00:00:00Z",
                },
            ]
        }

        result = template_store.list_templates()

        assert len(result) == 2
        assert result[0]["name"] == "template-1"
        assert result[1]["name"] == "template-2"

    def test_list_templates_with_pagination(self, template_store, mock_dynamodb):
        """Test listing templates handles pagination."""
        mock_dynamodb.scan.side_effect = [
            {
                "Items": [{"template_name": "template-1"}],
                "LastEvaluatedKey": {"template_name": "template-1"},
            },
            {"Items": [{"template_name": "template-2"}]},
        ]

        result = template_store.list_templates()

        assert len(result) == 2
        assert mock_dynamodb.scan.call_count == 2

    def test_get_template_found(self, template_store, mock_dynamodb):
        """Test getting an existing template."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "template_name": "my-template",
                "description": "Test template",
                "manifest": '{"apiVersion": "batch/v1"}',
                "parameters": '{"image": "test:latest"}',
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        }

        result = template_store.get_template("my-template")

        assert result is not None
        assert result["name"] == "my-template"
        assert result["manifest"] == {"apiVersion": "batch/v1"}
        assert result["parameters"] == {"image": "test:latest"}

    def test_get_template_not_found(self, template_store, mock_dynamodb):
        """Test getting a non-existent template."""
        mock_dynamodb.get_item.return_value = {}

        result = template_store.get_template("nonexistent")

        assert result is None

    def test_create_template_success(self, template_store, mock_dynamodb):
        """Test creating a new template."""
        mock_dynamodb.put_item.return_value = {}

        result = template_store.create_template(
            name="new-template",
            manifest={"apiVersion": "batch/v1", "kind": "Job"},
            description="A new template",
            parameters={"image": "default:latest"},
        )

        assert result["name"] == "new-template"
        assert result["description"] == "A new template"
        mock_dynamodb.put_item.assert_called_once()

    def test_create_template_duplicate(self, template_store, mock_dynamodb):
        """Test creating a duplicate template raises error."""
        error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        mock_dynamodb.put_item.side_effect = ClientError(error_response, "PutItem")

        with pytest.raises(ValueError, match="already exists"):
            template_store.create_template(
                name="existing-template",
                manifest={"apiVersion": "batch/v1"},
            )

    def test_update_template_success(self, template_store, mock_dynamodb):
        """Test updating an existing template."""
        mock_dynamodb.update_item.return_value = {
            "Attributes": {
                "template_name": "my-template",
                "description": "Updated description",
                "manifest": '{"apiVersion": "batch/v1"}',
                "parameters": "{}",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
            }
        }

        result = template_store.update_template(
            name="my-template",
            description="Updated description",
        )

        assert result is not None
        assert result["description"] == "Updated description"

    def test_update_template_not_found(self, template_store, mock_dynamodb):
        """Test updating a non-existent template."""
        error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        mock_dynamodb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        result = template_store.update_template(
            name="nonexistent",
            description="New description",
        )

        assert result is None

    def test_delete_template_success(self, template_store, mock_dynamodb):
        """Test deleting an existing template."""
        mock_dynamodb.delete_item.return_value = {}

        result = template_store.delete_template("my-template")

        assert result is True
        mock_dynamodb.delete_item.assert_called_once()

    def test_delete_template_not_found(self, template_store, mock_dynamodb):
        """Test deleting a non-existent template."""
        error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        mock_dynamodb.delete_item.side_effect = ClientError(error_response, "DeleteItem")

        result = template_store.delete_template("nonexistent")

        assert result is False

    def test_template_exists_true(self, template_store, mock_dynamodb):
        """Test checking if template exists when it does."""
        mock_dynamodb.get_item.return_value = {"Item": {"template_name": "exists"}}

        result = template_store.template_exists("exists")

        assert result is True

    def test_template_exists_false(self, template_store, mock_dynamodb):
        """Test checking if template exists when it doesn't."""
        mock_dynamodb.get_item.return_value = {}

        result = template_store.template_exists("nonexistent")

        assert result is False


# =============================================================================
# WebhookStore Tests
# =============================================================================


class TestWebhookStore:
    """Tests for WebhookStore class."""

    @pytest.fixture
    def mock_dynamodb(self):
        """Create a mock DynamoDB resource."""
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            yield mock_table

    @pytest.fixture
    def webhook_store(self, mock_dynamodb):
        """Create a WebhookStore with mocked DynamoDB."""
        store = WebhookStore(table_name="test-webhooks", region="us-east-1")
        store._table = mock_dynamodb
        return store

    def test_init_with_defaults(self):
        """Test WebhookStore initialization with default values."""
        with patch("boto3.resource"):
            store = WebhookStore()
            assert store.table_name == "gco-webhooks"

    def test_list_webhooks_empty(self, webhook_store, mock_dynamodb):
        """Test listing webhooks when none exist."""
        mock_dynamodb.scan.return_value = {"Items": []}

        result = webhook_store.list_webhooks()

        assert result == []

    def test_list_webhooks_with_namespace_filter(self, webhook_store, mock_dynamodb):
        """Test listing webhooks filtered by namespace."""
        mock_dynamodb.query.return_value = {
            "Items": [
                {
                    "webhook_id": "wh-1",
                    "url": "https://example.com/webhook",
                    "events": '["job.completed"]',
                    "namespace": "default",
                    "secret": "redacted-secret",
                    "created_at": "2024-01-01T00:00:00Z",
                }
            ]
        }

        result = webhook_store.list_webhooks(namespace="default")

        assert len(result) == 1
        assert "secret" not in result[0]
        mock_dynamodb.query.assert_called_once()

    def test_get_webhook_found(self, webhook_store, mock_dynamodb):
        """Test getting an existing webhook."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "webhook_id": "wh-123",
                "url": "https://example.com/webhook",
                "events": '["job.completed", "job.failed"]',
                "namespace": "default",
                "secret": "my-secret",
                "created_at": "2024-01-01T00:00:00Z",
            }
        }

        result = webhook_store.get_webhook("wh-123")

        assert result is not None
        assert result["id"] == "wh-123"
        assert result["events"] == ["job.completed", "job.failed"]

    def test_get_webhook_not_found(self, webhook_store, mock_dynamodb):
        """Test getting a non-existent webhook."""
        mock_dynamodb.get_item.return_value = {}

        result = webhook_store.get_webhook("nonexistent")

        assert result is None

    def test_create_webhook_success(self, webhook_store, mock_dynamodb):
        """Test creating a new webhook."""
        mock_dynamodb.put_item.return_value = {}

        result = webhook_store.create_webhook(
            webhook_id="wh-new",
            url="https://example.com/webhook",
            events=["job.completed"],
            namespace="default",
            secret="my-secret",  # nosec B106 - test fixture value for webhook HMAC secret, not a real credential
        )

        assert result["id"] == "wh-new"
        assert result["url"] == "https://example.com/webhook"
        mock_dynamodb.put_item.assert_called_once()

    def test_delete_webhook_success(self, webhook_store, mock_dynamodb):
        """Test deleting an existing webhook."""
        mock_dynamodb.delete_item.return_value = {}

        result = webhook_store.delete_webhook("wh-123")

        assert result is True

    def test_delete_webhook_not_found(self, webhook_store, mock_dynamodb):
        """Test deleting a non-existent webhook."""
        error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        mock_dynamodb.delete_item.side_effect = ClientError(error_response, "DeleteItem")

        result = webhook_store.delete_webhook("nonexistent")

        assert result is False

    def test_get_webhooks_for_event(self, webhook_store, mock_dynamodb):
        """Test getting webhooks subscribed to a specific event."""
        mock_dynamodb.scan.return_value = {
            "Items": [
                {
                    "webhook_id": "wh-1",
                    "url": "https://example1.com",
                    "events": '["job.completed", "job.failed"]',
                    "secret": "delivery-secret",
                },
                {
                    "webhook_id": "wh-2",
                    "url": "https://example2.com",
                    "events": '["job.started"]',
                },
            ]
        }

        result = webhook_store.get_webhooks_for_event("job.completed")

        assert len(result) == 1
        assert result[0]["id"] == "wh-1"
        assert result[0]["secret"] == "delivery-secret"


# =============================================================================
# JobStore Tests
# =============================================================================


class TestJobStore:
    """Tests for JobStore class."""

    @pytest.fixture
    def mock_dynamodb(self):
        """Create a mock DynamoDB resource."""
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            yield mock_table

    @pytest.fixture
    def job_store(self, mock_dynamodb):
        """Create a JobStore with mocked DynamoDB."""
        store = JobStore(table_name="test-jobs", region="us-east-1")
        store._table = mock_dynamodb
        return store

    def test_init_with_defaults(self):
        """Test JobStore initialization with default values."""
        with patch("boto3.resource"):
            store = JobStore()
            assert store.table_name == "gco-jobs"

    def test_submit_job_success(self, job_store, mock_dynamodb):
        """Test submitting a new job."""
        mock_dynamodb.put_item.return_value = {}

        result = job_store.submit_job(
            job_id="job-123",
            manifest={"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "test-job"}},
            target_region="us-east-1",
            namespace="gco-jobs",
            priority=10,
            labels={"team": "ml"},
            submitted_by="user@example.com",
        )

        assert result["job_id"] == "job-123"
        assert result["job_name"] == "test-job"
        assert result["target_region"] == "us-east-1"
        assert result["status"] == "queued"
        assert result["priority"] == 10
        mock_dynamodb.put_item.assert_called_once()

    def test_claim_job_success(self, job_store, mock_dynamodb):
        """A queued record is claimed with region and fencing identity."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-123",
                "status": "queued",
                "target_region": "us-east-1",
                "updated_at": "2024-01-01T00:00:00Z",
                "claim_generation": 2,
                "status_history": "[]",
            }
        }
        mock_dynamodb.update_item.return_value = {
            "Attributes": {
                "job_id": "job-123",
                "job_name": "test-job",
                "target_region": "us-east-1",
                "namespace": "gco-jobs",
                "status": "claimed",
                "priority": 0,
                "manifest": "{}",
                "labels": "{}",
                "status_history": "[]",
                "claim_generation": 3,
                "claim_token": "claim-token",
            }
        }

        result = job_store.claim_job("job-123", "us-east-1", "worker-1")

        assert result is not None
        assert result["status"] == "claimed"
        assert result["claim_generation"] == 3
        assert result["claim_token"] == "claim-token"
        values = mock_dynamodb.update_item.call_args.kwargs["ExpressionAttributeValues"]
        expression = mock_dynamodb.update_item.call_args.kwargs["UpdateExpression"]
        assert values[":target_region"] == "us-east-1"
        assert values[":claimed_by"] == "worker-1"
        assert values[":work_sort"] == values[":lease_expires_at"]
        assert "work_sort = :work_sort" in expression

    def test_claim_job_already_claimed(self, job_store, mock_dynamodb):
        """A conditional race loss returns None without stealing the claim."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-123",
                "status": "queued",
                "target_region": "us-east-1",
                "updated_at": "2024-01-01T00:00:00Z",
                "claim_generation": 0,
                "status_history": "[]",
            }
        }
        error_response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        mock_dynamodb.update_item.side_effect = ClientError(error_response, "UpdateItem")

        result = job_store.claim_job("job-123", "us-east-1", "worker-2")

        assert result is None

    def test_transition_job_status(self, job_store, mock_dynamodb):
        """A compare-and-set lifecycle transition appends status atomically."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-123",
                "status": "pending",
                "target_region": "us-east-1",
                "updated_at": "2024-01-01T00:00:00Z",
                "status_history": "[]",
            }
        }
        mock_dynamodb.update_item.return_value = {
            "Attributes": {
                "job_id": "job-123",
                "target_region": "us-east-1",
                "status": "running",
                "priority": 0,
                "manifest": "{}",
                "labels": "{}",
                "status_history": "[]",
            }
        }

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.PENDING,
            status=JobStatus.RUNNING,
            message="Job is now running",
        )

        assert result is not None
        assert result["status"] == "running"
        condition = mock_dynamodb.update_item.call_args.kwargs["ConditionExpression"]
        assert "updated_at = :expected_updated_at" in condition

    def test_transition_job_with_k8s_identity(self, job_store, mock_dynamodb):
        """Applying to pending persists the deterministic Kubernetes identity."""
        raw_item = {
            "job_id": "job-123",
            "status": "applying",
            "target_region": "us-east-1",
            "updated_at": "2024-01-01T00:00:00Z",
            "status_history": "[]",
            "claimed_by": "worker-1",
            "claim_token": "claim-token",
            "claim_generation": 4,
            "lease_expires_at": "2099-01-01T00:00:00Z",
        }
        mock_dynamodb.get_item.return_value = {"Item": raw_item}
        mock_dynamodb.update_item.return_value = {
            "Attributes": {
                **raw_item,
                "status": "pending",
                "priority": 0,
                "manifest": "{}",
                "labels": "{}",
                "k8s_job_name": "test-job",
                "k8s_job_namespace": "gco-jobs",
                "k8s_job_uid": "abc-123-def",
            }
        }

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.APPLYING,
            status=JobStatus.PENDING,
            k8s_job_name="test-job",
            k8s_job_namespace="gco-jobs",
            k8s_job_uid="abc-123-def",
            claimed_by="worker-1",
            claim_token="claim-token",
            claim_generation=4,
        )

        assert result is not None
        assert result["k8s_job_uid"] == "abc-123-def"
        expression = mock_dynamodb.update_item.call_args.kwargs["UpdateExpression"]
        assert "k8s_job_namespace = :k8s_job_namespace" in expression
        remove_fields = set(expression.split(" REMOVE ", 1)[1].split(", "))
        assert remove_fields == {
            "error_message",
            "claimed_by",
            "claim_token",
            "lease_expires_at",
        }

    def test_transition_job_failed(self, job_store, mock_dynamodb):
        """A failed transition records a terminal timestamp and bounded error field."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-123",
                "status": "running",
                "target_region": "us-east-1",
                "updated_at": "2024-01-01T00:00:00Z",
                "status_history": "[]",
            }
        }
        mock_dynamodb.update_item.return_value = {
            "Attributes": {
                "job_id": "job-123",
                "target_region": "us-east-1",
                "status": "failed",
                "priority": 0,
                "manifest": "{}",
                "labels": "{}",
                "status_history": "[]",
                "error_message": "Pod crashed",
                "completed_at": "2024-01-01T00:00:00Z",
            }
        }

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.RUNNING,
            status=JobStatus.FAILED,
            error="Pod crashed",
        )

        assert result is not None
        assert result["status"] == "failed"
        assert result["error_message"] == "Pod crashed"
        expression = mock_dynamodb.update_item.call_args.kwargs["UpdateExpression"]
        assert "completed_at = :now" in expression

    def test_transition_failed_can_atomically_prove_no_workload_was_created(
        self, job_store, mock_dynamodb
    ):
        """A fenced pre-create rejection records proof only while identity is absent."""
        raw_item = self._leased_job(
            "job-123",
            "2099-01-01T00:00:00Z",
            status="applying",
            claim_token="claim-token",
        )
        mock_dynamodb.get_item.return_value = {"Item": raw_item}
        mock_dynamodb.update_item.return_value = {
            "Attributes": {
                **raw_item,
                "status": "failed",
                "workload_not_created": True,
            }
        }

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.APPLYING,
            status=JobStatus.FAILED,
            error="Queued Job validation failed",
            claimed_by="worker-1",
            claim_token="claim-token",
            claim_generation=3,
            workload_not_created=True,
        )

        assert result is not None
        assert result["workload_not_created"] is True
        update = mock_dynamodb.update_item.call_args.kwargs
        assert "workload_not_created = :workload_not_created" in update["UpdateExpression"]
        assert update["ExpressionAttributeValues"][":workload_not_created"] is True
        conditions = set(update["ConditionExpression"].split(" AND "))
        assert {
            "attribute_not_exists(workload_not_created)",
            "attribute_not_exists(k8s_job_name)",
            "attribute_not_exists(k8s_job_namespace)",
            "attribute_not_exists(k8s_job_uid)",
        }.issubset(conditions)

    @pytest.mark.parametrize(
        ("source", "destination", "proof", "message"),
        [
            (JobStatus.APPLYING, JobStatus.PENDING, True, "valid only for failed"),
            (JobStatus.APPLYING, JobStatus.FAILED, False, "must be exactly true"),
            (JobStatus.PENDING, JobStatus.FAILED, True, "only from the applying"),
            (JobStatus.RUNNING, JobStatus.FAILED, True, "only from the applying"),
        ],
    )
    def test_transition_rejects_invalid_no_workload_proof_contract(
        self,
        job_store,
        mock_dynamodb,
        source,
        destination,
        proof,
        message,
    ):
        """No-workload evidence cannot be false or attached to a nonfailed state."""
        with pytest.raises(ValueError, match=message):
            job_store.transition_job(
                "job-123",
                target_region="us-east-1",
                expected_status=source,
                status=destination,
                workload_not_created=proof,
            )

        mock_dynamodb.get_item.assert_not_called()
        mock_dynamodb.update_item.assert_not_called()

    def test_transition_rejects_no_workload_proof_after_identity_exists(
        self, job_store, mock_dynamodb
    ):
        """A record with any Kubernetes identity can never claim non-creation."""
        raw_item = self._leased_job(
            "job-123",
            "2099-01-01T00:00:00Z",
            status="applying",
            claim_token="claim-token",
        )
        raw_item["k8s_job_uid"] = "uid-existing"
        mock_dynamodb.get_item.return_value = {"Item": raw_item}

        with pytest.raises(ValueError, match="without Kubernetes identity"):
            job_store.transition_job(
                "job-123",
                target_region="us-east-1",
                expected_status=JobStatus.APPLYING,
                status=JobStatus.FAILED,
                claimed_by="worker-1",
                claim_token="claim-token",
                claim_generation=3,
                workload_not_created=True,
            )

        mock_dynamodb.update_item.assert_not_called()

    def test_get_job_found(self, job_store, mock_dynamodb):
        """Test getting an existing job."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-123",
                "job_name": "test-job",
                "target_region": "us-east-1",
                "namespace": "gco-jobs",
                "status": "running",
                "priority": 5,
                "manifest": '{"apiVersion": "batch/v1"}',
                "labels": '{"team": "ml"}',
                "submitted_at": "2024-01-01T00:00:00Z",
                "status_history": '[{"status": "queued", "timestamp": "2024-01-01T00:00:00Z"}]',
            }
        }

        result = job_store.get_job("job-123")

        assert result is not None
        assert result["job_id"] == "job-123"
        assert result["status"] == "running"
        assert result["labels"] == {"team": "ml"}

    def test_get_job_not_found(self, job_store, mock_dynamodb):
        """Test getting a non-existent job."""
        mock_dynamodb.get_item.return_value = {}

        result = job_store.get_job("nonexistent")

        assert result is None

    def test_list_jobs_no_filters(self, job_store, mock_dynamodb):
        """Test listing jobs without filters."""
        mock_dynamodb.scan.return_value = {
            "Items": [
                {
                    "job_id": "job-1",
                    "job_name": "test-1",
                    "target_region": "us-east-1",
                    "namespace": "default",
                    "status": "running",
                    "priority": 0,
                    "manifest": "{}",
                    "labels": "{}",
                    "status_history": "[]",
                }
            ]
        }

        result = job_store.list_jobs()

        assert len(result) == 1
        assert result[0]["job_id"] == "job-1"

    def test_list_jobs_with_region_filter(self, job_store, mock_dynamodb):
        """Test listing jobs filtered by region."""
        mock_dynamodb.scan.return_value = {"Items": []}

        job_store.list_jobs(target_region="us-east-1")

        call_args = mock_dynamodb.scan.call_args
        assert "target_region = :region" in call_args.kwargs.get("FilterExpression", "")

    def test_list_jobs_with_status_filter(self, job_store, mock_dynamodb):
        """Test listing jobs filtered by status."""
        mock_dynamodb.scan.return_value = {"Items": []}

        job_store.list_jobs(status="running")

        call_args = mock_dynamodb.scan.call_args
        assert "#status = :status" in call_args.kwargs.get("FilterExpression", "")

    def test_get_queued_jobs_for_region(self, job_store, mock_dynamodb):
        """Test getting queued jobs for a specific region."""
        mock_dynamodb.query.return_value = {
            "Items": [
                {
                    "job_id": "job-1",
                    "job_name": "high-priority",
                    "target_region": "us-east-1",
                    "namespace": "default",
                    "status": "queued",
                    "priority": 10,
                    "manifest": "{}",
                    "labels": "{}",
                    "status_history": "[]",
                },
                {
                    "job_id": "job-2",
                    "job_name": "low-priority",
                    "target_region": "us-east-1",
                    "namespace": "default",
                    "status": "queued",
                    "priority": 1,
                    "manifest": "{}",
                    "labels": "{}",
                    "status_history": "[]",
                },
            ]
        }

        result = job_store.get_queued_jobs_for_region("us-east-1")

        assert len(result) == 2
        queries = [call.kwargs for call in mock_dynamodb.query.call_args_list]
        assert [query["IndexName"] for query in queries] == [
            "region-status-work-index",
        ]
        assert all(
            query["KeyConditionExpression"] == "region_status = :region_status" for query in queries
        )
        # Results from the work index are sorted by priority.
        assert result[0]["priority"] == 10
        assert result[1]["priority"] == 1

    def test_get_job_counts_by_region(self, job_store, mock_dynamodb):
        """Test getting job counts grouped by region and status."""
        mock_dynamodb.scan.return_value = {
            "Items": [
                {"target_region": "us-east-1", "status": "running"},
                {"target_region": "us-east-1", "status": "running"},
                {"target_region": "us-east-1", "status": "queued"},
                {"target_region": "us-west-2", "status": "succeeded"},
            ]
        }

        result = job_store.get_job_counts_by_region()

        assert result["us-east-1"]["running"] == 2
        assert result["us-east-1"]["queued"] == 1
        assert result["us-west-2"]["succeeded"] == 1

    def test_list_jobs_page_returns_filter_bound_cursor(self, job_store, mock_dynamodb):
        """Opaque cursors continue only the exact filter identity that created them."""
        item = {
            "job_id": "job-1",
            "target_region": "us-east-1",
            "status": "queued",
            "namespace": "gco-jobs",
            "submitted_at": "2024-01-01T00:00:00Z",
        }
        mock_dynamodb.scan.return_value = {
            "Items": [item],
            "ScannedCount": 1,
            "LastEvaluatedKey": {"job_id": "job-1"},
        }

        jobs, cursor, partial = job_store.list_jobs_page(
            target_region="us-east-1",
            status="queued",
            namespace="gco-jobs",
            limit=1,
        )

        assert [job["job_id"] for job in jobs] == ["job-1"]
        assert cursor is not None
        assert partial is False

        mock_dynamodb.scan.reset_mock()
        mock_dynamodb.scan.return_value = {"Items": [], "ScannedCount": 0}
        assert job_store.list_jobs_page(
            target_region="us-east-1",
            status="queued",
            namespace="gco-jobs",
            limit=1,
            cursor=cursor,
        ) == ([], None, False)
        assert mock_dynamodb.scan.call_args.kwargs["ExclusiveStartKey"] == {"job_id": "job-1"}

        with pytest.raises(ValueError, match="does not match"):
            job_store.list_jobs_page(
                target_region="us-east-1",
                status="running",
                namespace="gco-jobs",
                limit=1,
                cursor=cursor,
            )

    def test_job_count_summary_reports_bounded_partial_scan(self, job_store, mock_dynamodb):
        """Count summaries disclose when their evaluation budget truncates the scan."""
        mock_dynamodb.scan.return_value = {
            "Items": [
                {"target_region": "us-east-1", "status": "queued"},
                {"target_region": "us-west-2", "status": "running"},
            ],
            "ScannedCount": 2,
            "LastEvaluatedKey": {"job_id": "job-2"},
        }

        counts, evaluated, truncated = job_store.get_job_count_summary(max_evaluated=2)

        assert counts == {
            "us-east-1": {"queued": 1},
            "us-west-2": {"running": 1},
        }
        assert evaluated == 2
        assert truncated is True
        assert mock_dynamodb.scan.call_args.kwargs["Limit"] == 2

    def test_migrates_legacy_records_and_fences_unsafe_states(self, job_store, mock_dynamodb):
        """Legacy active records without fencing or K8s identity fail instead of replaying."""
        mock_dynamodb.query.side_effect = [
            {
                "Items": [
                    {
                        "job_id": "queued-legacy",
                        "status": "queued",
                        "target_region": "us-east-1",
                        "priority": 9,
                        "submitted_at": "2024-01-01T00:00:00Z",
                        "status_history": "[]",
                    }
                ],
                "ScannedCount": 1,
            },
            {
                "Items": [
                    {
                        "job_id": "claimed-legacy",
                        "status": "claimed",
                        "target_region": "us-east-1",
                        "status_history": "[]",
                    }
                ],
                "ScannedCount": 1,
            },
            {"Items": [], "ScannedCount": 0},
            {
                "Items": [
                    {
                        "job_id": "pending-legacy",
                        "status": "pending",
                        "target_region": "us-east-1",
                        "status_history": "[]",
                    }
                ],
                "ScannedCount": 1,
            },
            {"Items": [], "ScannedCount": 0},
        ]
        mock_dynamodb.update_item.return_value = {}

        result = job_store.migrate_legacy_records_for_region("us-east-1")

        assert result == {"evaluated": 3, "migrated": 1, "failed": 2, "complete": True}
        assert mock_dynamodb.update_item.call_count == 3
        migrated_call = mock_dynamodb.update_item.call_args_list[0]
        migrated_condition = migrated_call.kwargs["ConditionExpression"]
        assert "region_status <> :region_status" in migrated_condition
        assert "work_sort <> :work_sort" in migrated_condition
        for call in mock_dynamodb.update_item.call_args_list:
            condition = call.kwargs["ConditionExpression"]
            assert "#status = :expected" in condition
            assert "target_region = :target_region" in condition
            snapshot_fields = set(call.kwargs["ExpressionAttributeNames"].values())
            assert {"priority", "submitted_at", "updated_at"}.issubset(snapshot_fields)
            assert ":work_sort" in call.kwargs["ExpressionAttributeValues"]
        failed_calls = mock_dynamodb.update_item.call_args_list[1:]
        assert all(":failed" in call.kwargs["ExpressionAttributeValues"] for call in failed_calls)
        assert all(
            "Record fenced during queue schema migration"
            in call.kwargs["ExpressionAttributeValues"][":history"]
            for call in failed_calls
        )

    def test_completed_migration_restarts_to_catch_a_late_legacy_writer(
        self, job_store, mock_dynamodb
    ):
        """A full sweep resets so a later base-version write is backfilled."""
        empty_page = {"Items": [], "ScannedCount": 0}
        mock_dynamodb.query.return_value = empty_page

        first_sweep = job_store.migrate_legacy_records_for_region("us-east-1")

        assert first_sweep["complete"] is True
        assert mock_dynamodb.query.call_count == 5

        mock_dynamodb.query.reset_mock()
        mock_dynamodb.query.side_effect = [
            {
                "Items": [
                    {
                        "job_id": "late-legacy",
                        "job_name": "late-legacy",
                        "target_region": "us-east-1",
                        "namespace": "gco-jobs",
                        "status": "queued",
                        "priority": 50,
                        "manifest": "{}",
                        "submitted_at": "2024-01-01T00:00:00Z",
                        "updated_at": "2024-01-01T00:00:00Z",
                        "status_history": "[]",
                    }
                ],
                "ScannedCount": 1,
            },
            empty_page,
            empty_page,
            empty_page,
            empty_page,
        ]

        second_sweep = job_store.migrate_legacy_records_for_region("us-east-1")

        assert second_sweep == {
            "evaluated": 1,
            "migrated": 1,
            "failed": 0,
            "complete": True,
        }
        mock_dynamodb.update_item.assert_called_once()
        assert [call.kwargs["IndexName"] for call in mock_dynamodb.query.call_args_list] == [
            "region-status-index",
            "region-status-index",
            "region-status-index",
            "region-status-index",
            "region-status-index",
        ]

    def test_migration_repairs_stale_claim_keys_with_lease_snapshot_fencing(
        self, job_store, mock_dynamodb
    ):
        """Present-but-stale derived keys are repaired without racing a renewal."""
        item = {
            "job_id": "claimed-stale",
            "status": "claimed",
            "target_region": "us-east-1",
            "priority": 75,
            "submitted_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:01Z",
            "claimed_by": "worker-a",
            "claim_token": "token-a",
            "claim_generation": 3,
            "lease_expires_at": "2026-01-01T00:05:00Z",
            "region_status": "us-east-1#queued",
            "priority_sort": "stale-priority",
            "work_sort": "2026-01-01T00:00:02Z",
        }
        mock_dynamodb.update_item.return_value = {}

        assert job_store._migrate_legacy_record(item, "us-east-1", "claimed") == "migrated"

        update = mock_dynamodb.update_item.call_args.kwargs
        values = update["ExpressionAttributeValues"]
        assert values[":region_status"] == "us-east-1#claimed"
        assert values[":work_sort"] == item["lease_expires_at"]
        snapshot_fields = set(update["ExpressionAttributeNames"].values())
        assert {
            "claimed_by",
            "claim_token",
            "claim_generation",
            "lease_expires_at",
            "updated_at",
        }.issubset(snapshot_fields)
        lease_name_token = next(
            token
            for token, field_name in update["ExpressionAttributeNames"].items()
            if field_name == "lease_expires_at"
        )
        assert f"{lease_name_token} = " in update["ConditionExpression"]

    def test_migration_fairly_services_active_statuses_behind_queued_backlog(
        self, job_store, mock_dynamodb
    ):
        """A queued page cannot consume the whole migration evaluation budget."""
        submitted_at = "2026-01-01T00:00:00Z"
        current_items = []
        for job_id in ("queued-1", "queued-2"):
            priority_sort = f"050#{submitted_at}#{job_id}"
            current_items.append(
                {
                    "job_id": job_id,
                    "status": "queued",
                    "target_region": "us-east-1",
                    "priority": 50,
                    "submitted_at": submitted_at,
                    "region_status": "us-east-1#queued",
                    "priority_sort": priority_sort,
                    "work_sort": priority_sort,
                }
            )
        mock_dynamodb.query.side_effect = [
            {
                "Items": current_items,
                "ScannedCount": 2,
                "LastEvaluatedKey": {"job_id": "queued-2"},
            },
            {"Items": [], "ScannedCount": 0},
            {"Items": [], "ScannedCount": 0},
            {"Items": [], "ScannedCount": 0},
            {"Items": [], "ScannedCount": 0},
        ]

        result = job_store.migrate_legacy_records_for_region("us-east-1", evaluation_limit=10)

        assert result == {"evaluated": 2, "migrated": 0, "failed": 0, "complete": False}
        queried_statuses = [
            call.kwargs["ExpressionAttributeValues"][":status"]
            for call in mock_dynamodb.query.call_args_list
        ]
        assert queried_statuses == ["queued", "claimed", "applying", "pending", "running"]
        assert all(
            "FilterExpression" not in call.kwargs for call in mock_dynamodb.query.call_args_list
        )
        mock_dynamodb.update_item.assert_not_called()

    def test_lease_recovery_uses_the_status_work_index(self, job_store, mock_dynamodb):
        """Expired claims are ordered by work_sort within transient partitions."""
        mock_dynamodb.query.return_value = {"Items": []}

        assert job_store.requeue_expired_jobs("us-east-1", limit=5) == 0

        assert mock_dynamodb.query.call_count == 2
        queries = [call.kwargs for call in mock_dynamodb.query.call_args_list]
        assert [query["IndexName"] for query in queries] == [
            "region-status-work-index",
            "region-status-work-index",
        ]
        for query in queries:
            assert "work_sort <= :upper_bound" in query["KeyConditionExpression"]

    def test_expired_claims_use_the_single_work_index(self, job_store, mock_dynamodb):
        """The unified work index returns expired claims in lease order."""
        mock_dynamodb.query.return_value = {
            "Items": [
                {
                    "job_id": "work-latest",
                    "lease_expires_at": "2026-01-01T00:00:03Z",
                },
                {
                    "job_id": "work-earliest",
                    "lease_expires_at": "2026-01-01T00:00:00Z",
                },
                {
                    "job_id": "work-middle",
                    "lease_expires_at": "2026-01-01T00:00:02Z",
                },
            ]
        }

        claims = job_store._query_expired_claims(
            "us-east-1",
            "claimed",
            "2099-01-01T00:00:00Z",
            10,
        )

        assert [claim["job_id"] for claim in claims] == [
            "work-earliest",
            "work-middle",
            "work-latest",
        ]
        query = mock_dynamodb.query.call_args.kwargs
        assert query["IndexName"] == "region-status-work-index"
        assert "work_sort <= :upper_bound" in query["KeyConditionExpression"]

    def test_cancel_job_success(self, job_store, mock_dynamodb):
        """Only a still-queued record can be cancelled with an atomic history CAS."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-123",
                "status": "queued",
                "target_region": "us-east-1",
                "updated_at": "2024-01-01T00:00:00Z",
                "status_history": "[]",
            }
        }
        mock_dynamodb.update_item.return_value = {}

        result = job_store.cancel_job("job-123", reason="No longer needed")

        assert result is True
        condition = mock_dynamodb.update_item.call_args.kwargs["ConditionExpression"]
        assert "#status = :queued" in condition
        assert "updated_at = :expected_updated_at" in condition

    def test_cancel_job_not_cancellable(self, job_store, mock_dynamodb):
        """Test cancelling a job that's already running."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-123",
                "status": "running",
                "target_region": "us-east-1",
                "updated_at": "2024-01-01T00:00:00Z",
            }
        }

        result = job_store.cancel_job("job-123")

        assert result is False
        mock_dynamodb.update_item.assert_not_called()

    @staticmethod
    def _leased_job(
        job_id,
        lease_expires_at,
        *,
        status="claimed",
        claimed_by="worker-1",
        claim_token=None,
        claim_generation=3,
    ):
        """Build a fully fenced transient record for focused JobStore tests."""
        return {
            "job_id": job_id,
            "job_name": job_id,
            "target_region": "us-east-1",
            "namespace": "gco-jobs",
            "status": status,
            "priority": 50,
            "priority_sort": f"050#2026-01-01T00:00:00Z#{job_id}",
            "manifest": "{}",
            "labels": "{}",
            "submitted_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:01Z",
            "claimed_by": claimed_by,
            "claim_token": claim_token or f"token-{job_id}",
            "claim_generation": claim_generation,
            "lease_expires_at": lease_expires_at,
            "status_history": "[]",
        }

    def test_submit_job_replays_only_identical_idempotent_request(self, job_store, mock_dynamodb):
        """A conditional collision replays the persisted request only when both keys match."""
        mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "PutItem",
        )
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-replayed",
                "job_name": "persisted-name",
                "target_region": "us-east-1",
                "namespace": "gco-jobs",
                "status": "queued",
                "priority": 20,
                "manifest": '{"kind":"Job"}',
                "labels": "{}",
                "submitted_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "claim_generation": 0,
                "status_history": "[]",
                "idempotency_key": "request-123",
                "request_hash": "sha256:identical",
            }
        }

        result = job_store.submit_job(
            job_id="job-replayed",
            manifest={"kind": "Job", "metadata": {"name": "new-name-is-not-returned"}},
            target_region="us-east-1",
            priority=20,
            idempotency_key="request-123",
            request_hash="sha256:identical",
        )

        assert result["job_name"] == "persisted-name"
        assert result["submitted_at"] == "2026-01-01T00:00:00Z"
        assert result["idempotent_replay"] is True
        assert mock_dynamodb.put_item.call_args.kwargs["ConditionExpression"] == (
            "attribute_not_exists(job_id)"
        )
        mock_dynamodb.get_item.assert_called_once_with(
            Key={"job_id": "job-replayed"}, ConsistentRead=True
        )

    @pytest.mark.parametrize(
        ("idempotency_key", "request_hash", "existing"),
        [
            (None, None, {"job_id": "job-conflict"}),
            (
                "request-123",
                "sha256:new",
                {
                    "job_id": "job-conflict",
                    "idempotency_key": "request-123",
                    "request_hash": "sha256:old",
                },
            ),
        ],
        ids=("job-id-reuse", "idempotency-payload-mismatch"),
    )
    def test_submit_job_rejects_nonidentical_conditional_collisions(
        self,
        job_store,
        mock_dynamodb,
        idempotency_key,
        request_hash,
        existing,
    ):
        """A reused job or idempotency identity cannot authorize a different submission."""
        mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "PutItem",
        )
        mock_dynamodb.get_item.return_value = {"Item": existing}

        with pytest.raises(RuntimeError, match="already in use"):
            job_store.submit_job(
                job_id="job-conflict",
                manifest={"kind": "Job"},
                target_region="us-east-1",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )

        mock_dynamodb.get_item.assert_called_once_with(
            Key={"job_id": "job-conflict"}, ConsistentRead=True
        )

    def test_renew_claim_updates_only_the_matching_unexpired_fence(self, job_store, mock_dynamodb):
        """A valid lease owner renews both lease ordering fields under a full fence."""
        mock_dynamodb.update_item.return_value = {}

        assert job_store.renew_claim("job-123", "us-east-1", "worker-1", "claim-token", 7) is True

        update = mock_dynamodb.update_item.call_args.kwargs
        assert update["UpdateExpression"] == (
            "SET lease_expires_at = :lease_expires_at, work_sort = :work_sort, "
            "lease_renewed_at = :now"
        )
        assert (
            update["ExpressionAttributeValues"][":work_sort"]
            == update["ExpressionAttributeValues"][":lease_expires_at"]
        )
        assert {
            "attribute_exists(job_id)",
            "target_region = :target_region",
            "#status IN (:claimed, :applying)",
            "claimed_by = :claimed_by",
            "claim_token = :claim_token",
            "claim_generation = :generation",
            "lease_expires_at > :now",
        }.issubset(set(update["ConditionExpression"].split(" AND ")))
        assert update["ExpressionAttributeValues"][":generation"] == 7

    def test_renew_claim_returns_false_when_the_fence_loses(self, job_store, mock_dynamodb):
        """A conditional failure cannot revive an expired or superseded lease."""
        mock_dynamodb.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )

        assert job_store.renew_claim("job-123", "us-east-1", "worker-1", "stale-token", 2) is False

    def test_renew_claim_propagates_nonconditional_errors(self, job_store, mock_dynamodb):
        """Infrastructure failures remain distinguishable from an expected fence loss."""
        error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "UpdateItem",
        )
        mock_dynamodb.update_item.side_effect = error

        with pytest.raises(ClientError) as raised:
            job_store.renew_claim("job-123", "us-east-1", "worker-1", "claim-token", 7)

        assert raised.value is error

    def test_transition_rejects_invalid_lifecycle_edge_before_reading(
        self, job_store, mock_dynamodb
    ):
        """The lifecycle matrix rejects regressions without consulting mutable storage."""
        with pytest.raises(ValueError, match="running -> claimed"):
            job_store.transition_job(
                "job-123",
                target_region="us-east-1",
                expected_status=JobStatus.RUNNING,
                status=JobStatus.CLAIMED,
            )

        mock_dynamodb.get_item.assert_not_called()
        mock_dynamodb.update_item.assert_not_called()

    @pytest.mark.parametrize(
        "raw_item",
        [
            None,
            {
                "job_id": "job-123",
                "status": "queued",
                "target_region": "us-east-1",
            },
            {
                "job_id": "job-123",
                "status": "pending",
                "target_region": "us-west-2",
            },
        ],
        ids=("absent", "wrong-current-state", "wrong-region"),
    )
    def test_transition_ignores_records_outside_the_expected_state_and_region(
        self, job_store, mock_dynamodb, raw_item
    ):
        """A stale regional observer cannot transition an absent or mismatched record."""
        mock_dynamodb.get_item.return_value = {} if raw_item is None else {"Item": raw_item}

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.PENDING,
            status=JobStatus.RUNNING,
        )

        assert result is None
        mock_dynamodb.update_item.assert_not_called()

    @pytest.mark.parametrize(
        ("claimed_by", "claim_token", "claim_generation"),
        [
            (None, "claim-token", 3),
            ("worker-1", None, 3),
            ("worker-1", "claim-token", None),
        ],
        ids=("missing-owner", "missing-token", "missing-generation"),
    )
    def test_transition_requires_complete_claim_fencing(
        self,
        job_store,
        mock_dynamodb,
        claimed_by,
        claim_token,
        claim_generation,
    ):
        """Every transition out of a transient state requires all fencing components."""
        mock_dynamodb.get_item.return_value = {
            "Item": self._leased_job("job-123", "2099-01-01T00:00:00Z", claim_token="claim-token")
        }

        with pytest.raises(ValueError, match="requires complete claim fencing"):
            job_store.transition_job(
                "job-123",
                target_region="us-east-1",
                expected_status=JobStatus.CLAIMED,
                status=JobStatus.APPLYING,
                claimed_by=claimed_by,
                claim_token=claim_token,
                claim_generation=claim_generation,
            )

        mock_dynamodb.update_item.assert_not_called()

    @pytest.mark.parametrize(
        ("claimed_by", "claim_token", "claim_generation"),
        [
            ("worker-2", "claim-token", 3),
            ("worker-1", "stale-token", 3),
            ("worker-1", "claim-token", 2),
        ],
        ids=("wrong-owner", "wrong-token", "wrong-generation"),
    )
    def test_transition_returns_none_for_mismatched_claim_fencing(
        self,
        job_store,
        mock_dynamodb,
        claimed_by,
        claim_token,
        claim_generation,
    ):
        """A complete but stale fence cannot mutate the current worker's record."""
        mock_dynamodb.get_item.return_value = {
            "Item": self._leased_job("job-123", "2099-01-01T00:00:00Z", claim_token="claim-token")
        }

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.CLAIMED,
            status=JobStatus.APPLYING,
            claimed_by=claimed_by,
            claim_token=claim_token,
            claim_generation=claim_generation,
        )

        assert result is None
        mock_dynamodb.update_item.assert_not_called()

    def test_transition_rejects_unexpected_k8s_uid(self, job_store, mock_dynamodb):
        """A recreated Kubernetes Job cannot be mistaken for the previously observed object."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-123",
                "status": "pending",
                "target_region": "us-east-1",
                "updated_at": "2026-01-01T00:00:00Z",
                "k8s_job_uid": "uid-current",
                "status_history": "[]",
            }
        }

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.PENDING,
            status=JobStatus.RUNNING,
            expected_k8s_uid="uid-stale",
        )

        assert result is None
        mock_dynamodb.update_item.assert_not_called()

    def test_transition_returns_none_after_conditional_race(self, job_store, mock_dynamodb):
        """A compare-and-set race loss is a benign no-op rather than an overwritten state."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "job-123",
                "status": "pending",
                "target_region": "us-east-1",
                "updated_at": "2026-01-01T00:00:00Z",
                "status_history": "[]",
            }
        }
        mock_dynamodb.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.PENDING,
            status=JobStatus.RUNNING,
        )

        assert result is None

    def test_transition_transient_expression_preserves_claim_fence(self, job_store, mock_dynamodb):
        """Claimed-to-applying keeps lease ownership and its lease-ordered work key."""
        raw_item = self._leased_job("job-123", "2099-01-01T00:00:00Z", claim_token="claim-token")
        mock_dynamodb.get_item.return_value = {"Item": raw_item}
        mock_dynamodb.update_item.return_value = {"Attributes": {**raw_item, "status": "applying"}}

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.CLAIMED,
            status=JobStatus.APPLYING,
            claimed_by="worker-1",
            claim_token="claim-token",
            claim_generation=3,
        )

        assert result is not None and result["status"] == "applying"
        update = mock_dynamodb.update_item.call_args.kwargs
        assert "completed_at = :now" not in update["UpdateExpression"]
        assert update["UpdateExpression"].endswith(" REMOVE error_message")
        assert update["ExpressionAttributeValues"][":work_sort"] == raw_item["lease_expires_at"]
        assert {
            "claimed_by = :claimed_by",
            "claim_token = :claim_token",
            "claim_generation = :generation",
            "lease_expires_at > :now",
        }.issubset(set(update["ConditionExpression"].split(" AND ")))

    def test_transition_terminal_expression_completes_and_clears_stale_lease(
        self, job_store, mock_dynamodb
    ):
        """Successful completion is terminal and removes reusable lease credentials."""
        raw_item = {
            "job_id": "job-123",
            "status": "running",
            "target_region": "us-east-1",
            "priority": 50,
            "priority_sort": "050#2026-01-01T00:00:00Z#job-123",
            "updated_at": "2026-01-01T00:00:01Z",
            "claimed_by": "stale-worker",
            "claim_token": "stale-token",
            "lease_expires_at": "2099-01-01T00:00:00Z",
            "error_message": "stale error",
            "status_history": "[]",
        }
        mock_dynamodb.get_item.return_value = {"Item": raw_item}
        mock_dynamodb.update_item.return_value = {
            "Attributes": {
                "job_id": "job-123",
                "status": "succeeded",
                "target_region": "us-east-1",
                "priority": 50,
                "status_history": "[]",
            }
        }

        result = job_store.transition_job(
            "job-123",
            target_region="us-east-1",
            expected_status=JobStatus.RUNNING,
            status=JobStatus.SUCCEEDED,
        )

        assert result is not None and result["status"] == "succeeded"
        expression = mock_dynamodb.update_item.call_args.kwargs["UpdateExpression"]
        assert "completed_at = :now" in expression
        assert set(expression.split(" REMOVE ", 1)[1].split(", ")) == {
            "error_message",
            "claimed_by",
            "claim_token",
            "lease_expires_at",
        }

    def test_get_active_jobs_allocates_fairly_and_reuses_spare_capacity(
        self, job_store, mock_dynamodb
    ):
        """Running work gets a fair share while unused capacity flows to pending work."""
        running = [{"job_id": "running-1", "status": "running"}]
        pending = [{"job_id": f"pending-{index}", "status": "pending"} for index in range(1, 5)]

        with patch.object(
            job_store,
            "_query_region_status",
            side_effect=[running, pending],
        ) as query:
            result = job_store.get_active_jobs_for_region("us-east-1", limit=5)

        assert [job["job_id"] for job in result] == [
            "running-1",
            "pending-1",
            "pending-2",
            "pending-3",
            "pending-4",
        ]
        assert [call.args for call in query.call_args_list] == [
            ("us-east-1", "running", 2),
            ("us-east-1", "pending", 4),
        ]
        mock_dynamodb.query.assert_not_called()

    def test_get_active_jobs_never_exceeds_total_limit(self, job_store, mock_dynamodb):
        """The public result remains bounded even if an index adapter over-returns."""
        overfull_page = [{"job_id": f"running-{index}", "status": "running"} for index in range(5)]

        with patch.object(
            job_store,
            "_query_region_status",
            return_value=overfull_page,
        ) as query:
            result = job_store.get_active_jobs_for_region("us-east-1", limit=3)

        assert [job["job_id"] for job in result] == ["running-0", "running-1", "running-2"]
        query.assert_called_once_with("us-east-1", "running", 1)
        mock_dynamodb.query.assert_not_called()

    def test_get_active_jobs_propagates_client_error(self, job_store, mock_dynamodb):
        """Index outages are not misreported as an empty active-job set."""
        error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "Query",
        )
        mock_dynamodb.query.side_effect = error

        with pytest.raises(ClientError) as raised:
            job_store.get_active_jobs_for_region("us-east-1", limit=4)

        assert raised.value is error

    def test_requeue_expired_jobs_recovers_in_global_lease_order(self, job_store, mock_dynamodb):
        """Expired records from both transient states are fenced oldest-first."""
        claimed_later = self._leased_job("claimed-later", "2026-01-01T00:00:08Z")
        claimed_earliest = self._leased_job("claimed-earliest", "2026-01-01T00:00:01Z")
        applying_middle = self._leased_job(
            "applying-middle", "2026-01-01T00:00:05Z", status="applying"
        )
        mock_dynamodb.query.side_effect = [
            {"Items": [claimed_later, claimed_earliest], "ScannedCount": 2},
            {"Items": [applying_middle], "ScannedCount": 1},
        ]
        mock_dynamodb.update_item.return_value = {}

        with patch(
            "gco.services.template_store._utc_now_iso",
            return_value="2026-01-01T00:00:10Z",
        ):
            recovered = job_store.requeue_expired_jobs("us-east-1", limit=4)

        assert recovered == 3
        assert [
            call.kwargs["Key"]["job_id"] for call in mock_dynamodb.update_item.call_args_list
        ] == [
            "claimed-earliest",
            "applying-middle",
            "claimed-later",
        ]
        for call in mock_dynamodb.update_item.call_args_list:
            update = call.kwargs
            assert "REMOVE claimed_by, claim_token, lease_expires_at" in update["UpdateExpression"]
            assert {
                "claimed_by = :owner",
                "claim_token = :token",
                "claim_generation = :generation",
                "updated_at = :expected_updated_at",
                "lease_expires_at <= :now",
            }.issubset(set(update["ConditionExpression"].split(" AND ")))

    def test_requeue_expired_jobs_skips_malformed_and_future_candidates(
        self, job_store, mock_dynamodb
    ):
        """Recovery refuses unfenced or unexpired records even if an index mock returns them."""
        missing_lease = self._leased_job("missing-lease", "2026-01-01T00:00:01Z")
        missing_lease["lease_expires_at"] = None
        missing_token = self._leased_job("missing-token", "2026-01-01T00:00:01Z")
        missing_token["claim_token"] = None
        future = self._leased_job("future", "2026-01-01T00:00:20Z")

        with (
            patch.object(
                job_store,
                "_query_expired_claims",
                side_effect=[[missing_lease, missing_token, future], []],
            ) as query,
            patch(
                "gco.services.template_store._utc_now_iso",
                return_value="2026-01-01T00:00:10Z",
            ),
        ):
            recovered = job_store.requeue_expired_jobs("us-east-1", limit=6)

        assert recovered == 0
        assert [call.args for call in query.call_args_list] == [
            ("us-east-1", "claimed", "2026-01-01T00:00:10Z", 3),
            ("us-east-1", "applying", "2026-01-01T00:00:10Z", 3),
        ]
        mock_dynamodb.update_item.assert_not_called()

    def test_requeue_expired_jobs_ignores_conditional_recovery_race(self, job_store, mock_dynamodb):
        """A renewed or transitioned claim wins over the recovery worker's stale snapshot."""
        candidate = self._leased_job("job-raced", "2026-01-01T00:00:01Z")
        mock_dynamodb.query.side_effect = [
            {"Items": [candidate], "ScannedCount": 1},
            {"Items": [], "ScannedCount": 0},
        ]
        mock_dynamodb.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )

        with patch(
            "gco.services.template_store._utc_now_iso",
            return_value="2026-01-01T00:00:10Z",
        ):
            assert job_store.requeue_expired_jobs("us-east-1", limit=2) == 0

    def test_requeue_expired_jobs_propagates_nonconditional_error(self, job_store, mock_dynamodb):
        """A write outage aborts recovery instead of reporting a successful requeue."""
        candidate = self._leased_job("job-error", "2026-01-01T00:00:01Z")
        mock_dynamodb.query.side_effect = [
            {"Items": [candidate], "ScannedCount": 1},
            {"Items": [], "ScannedCount": 0},
        ]
        error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "UpdateItem",
        )
        mock_dynamodb.update_item.side_effect = error

        with (
            patch(
                "gco.services.template_store._utc_now_iso",
                return_value="2026-01-01T00:00:10Z",
            ),
            pytest.raises(ClientError) as raised,
        ):
            job_store.requeue_expired_jobs("us-east-1", limit=2)

        assert raised.value is error

    def test_requeue_expired_jobs_enforces_limit_before_second_partition(
        self, job_store, mock_dynamodb
    ):
        """The recovery limit bounds both writes and transient partitions queried."""
        earliest = self._leased_job("earliest", "2026-01-01T00:00:01Z")
        mock_dynamodb.query.return_value = {
            "Items": [earliest],
            "ScannedCount": 1,
        }
        mock_dynamodb.update_item.return_value = {}

        with patch(
            "gco.services.template_store._utc_now_iso",
            return_value="2026-01-01T00:00:10Z",
        ):
            recovered = job_store.requeue_expired_jobs("us-east-1", limit=1)

        assert recovered == 1
        mock_dynamodb.query.assert_called_once()
        mock_dynamodb.update_item.assert_called_once()
        assert mock_dynamodb.update_item.call_args.kwargs["Key"] == {"job_id": "earliest"}

    def test_list_jobs_page_scans_multiple_filtered_pages(self, job_store, mock_dynamodb):
        """Filtered pagination continues across empty pages and preserves all filter bindings."""
        mock_dynamodb.scan.side_effect = [
            {
                "Items": [],
                "ScannedCount": 2,
                "LastEvaluatedKey": {"job_id": "scanned-2"},
            },
            {
                "Items": [
                    {
                        "job_id": "older",
                        "target_region": "us-east-1",
                        "status": "queued",
                        "namespace": "gco-jobs",
                        "submitted_at": "2026-01-01T00:00:00Z",
                    },
                    {
                        "job_id": "newer",
                        "target_region": "us-east-1",
                        "status": "queued",
                        "namespace": "gco-jobs",
                        "submitted_at": "2026-01-02T00:00:00Z",
                    },
                ],
                "ScannedCount": 2,
            },
        ]

        jobs, cursor, partial = job_store.list_jobs_page(
            target_region="us-east-1",
            status="queued",
            namespace="gco-jobs",
            limit=2,
        )

        assert [job["job_id"] for job in jobs] == ["newer", "older"]
        assert cursor is None
        assert partial is False
        assert mock_dynamodb.scan.call_count == 2
        assert mock_dynamodb.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == {
            "job_id": "scanned-2"
        }
        for call in mock_dynamodb.scan.call_args_list:
            assert call.kwargs["FilterExpression"] == (
                "target_region = :region AND #status = :status AND #namespace = :namespace"
            )
            assert call.kwargs["ExpressionAttributeValues"] == {
                ":region": "us-east-1",
                ":status": "queued",
                ":namespace": "gco-jobs",
            }
            assert call.kwargs["ExpressionAttributeNames"] == {
                "#status": "status",
                "#namespace": "namespace",
            }

    @pytest.mark.parametrize(
        ("second_item", "expected_key"),
        [
            ({"job_id": "job-2"}, {"job_id": "job-2"}),
            ({"status": "queued"}, {"job_id": "physical-page-end"}),
        ],
        ids=("last-returned-key", "physical-page-fallback"),
    )
    def test_list_jobs_page_uses_safe_cursor_for_oversized_mock_page(
        self, job_store, mock_dynamodb, second_item, expected_key
    ):
        """An overfull response resumes after returned data, with a safe fallback for bad rows."""
        mock_dynamodb.scan.return_value = {
            "Items": [
                {"job_id": "job-1"},
                second_item,
                {"job_id": "job-3"},
            ],
            "ScannedCount": 3,
            "LastEvaluatedKey": {"job_id": "physical-page-end"},
        }

        jobs, cursor, partial = job_store.list_jobs_page(limit=2)

        assert len(jobs) == 2
        assert cursor is not None
        assert partial is False
        filters = {"target_region": None, "status": None, "namespace": None}
        assert job_store._decode_list_cursor(cursor, filters) == expected_key

    def test_list_jobs_page_marks_evaluation_budget_as_partial(self, job_store, mock_dynamodb):
        """A bounded scan discloses that more matching data may exist beyond its budget."""
        mock_dynamodb.scan.return_value = {
            "Items": [],
            "ScannedCount": 2,
            "LastEvaluatedKey": {"job_id": "budget-end"},
        }

        with patch("gco.services.template_store._MAX_LIST_EVALUATED_ITEMS", 2):
            jobs, cursor, partial = job_store.list_jobs_page(limit=1)

        assert jobs == []
        assert cursor is not None
        assert partial is True
        assert mock_dynamodb.scan.call_args.kwargs["Limit"] == 2
        filters = {"target_region": None, "status": None, "namespace": None}
        assert job_store._decode_list_cursor(cursor, filters) == {"job_id": "budget-end"}

    def test_list_jobs_page_rejects_malformed_cursor_payloads(self, job_store, mock_dynamodb):
        """Opaque cursors reject oversized, undecodable, versionless, and invalid-key inputs."""
        import base64
        import json

        filters = {"target_region": None, "status": None, "namespace": None}

        def encode(payload):
            return (
                base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
                .decode("ascii")
                .rstrip("=")
            )

        invalid_cursors = [
            "x" * 2_049,
            "%%%",
            encode({"version": 2, "key": {"job_id": "job-1"}, "filters": filters}),
            encode({"version": 1, "key": {"other": "job-1"}, "filters": filters}),
        ]

        for cursor in invalid_cursors:
            with pytest.raises(ValueError, match="Invalid queue cursor"):
                job_store.list_jobs_page(limit=1, cursor=cursor)

        mock_dynamodb.scan.assert_not_called()

    def test_migration_skips_invalid_current_and_conditionally_raced_records(
        self, job_store, mock_dynamodb
    ):
        """Migration never writes invalid IDs, current keys, or a concurrently changed snapshot."""
        assert job_store._migrate_legacy_record({"job_id": ""}, "us-east-1", "queued") == (
            "skipped"
        )

        priority_sort = "050#2026-01-01T00:00:00Z#job-current"
        current = {
            "job_id": "job-current",
            "status": "queued",
            "target_region": "us-east-1",
            "priority": 50,
            "submitted_at": "2026-01-01T00:00:00Z",
            "region_status": "us-east-1#queued",
            "priority_sort": priority_sort,
            "work_sort": priority_sort,
        }
        assert job_store._migrate_legacy_record(current, "us-east-1", "queued") == "skipped"

        mock_dynamodb.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )
        stale = {**current, "job_id": "job-raced", "region_status": "stale"}
        assert job_store._migrate_legacy_record(stale, "us-east-1", "queued") == "skipped"
        mock_dynamodb.update_item.assert_called_once()

    def test_migration_ignores_invalid_page_items_and_cursor(self, job_store, mock_dynamodb):
        """Malformed query rows and continuation keys cannot trigger writes or poison a sweep."""
        mock_dynamodb.query.side_effect = [
            {
                "Items": [None, "not-a-record"],
                "LastEvaluatedKey": ["not", "a", "key"],
            },
            {"Items": [], "ScannedCount": 0},
            {"Items": [], "ScannedCount": 0},
            {"Items": [], "ScannedCount": 0},
            {"Items": [], "ScannedCount": 0},
        ]

        result = job_store.migrate_legacy_records_for_region("us-east-1", evaluation_limit=10)

        assert result == {"evaluated": 2, "migrated": 0, "failed": 0, "complete": True}
        assert job_store._legacy_migration_cursors == {}
        mock_dynamodb.update_item.assert_not_called()

    def test_migration_rotates_small_budget_and_reuses_partition_cursor(
        self, job_store, mock_dynamodb
    ):
        """Tiny invocations rotate fairly, then resume each partition from its own cursor."""
        cursor = {"job_id": "partition-cursor"}
        mock_dynamodb.query.return_value = {
            "Items": [],
            "ScannedCount": 1,
            "LastEvaluatedKey": cursor,
        }

        results = [
            job_store.migrate_legacy_records_for_region("us-east-1", evaluation_limit=1)
            for _ in range(6)
        ]

        assert all(result["complete"] is False for result in results)
        assert [
            call.kwargs["ExpressionAttributeValues"][":status"]
            for call in mock_dynamodb.query.call_args_list
        ] == ["queued", "claimed", "applying", "pending", "running", "queued"]
        assert all(
            "ExclusiveStartKey" not in call.kwargs
            for call in mock_dynamodb.query.call_args_list[:5]
        )
        assert mock_dynamodb.query.call_args_list[5].kwargs["ExclusiveStartKey"] == cursor
        mock_dynamodb.update_item.assert_not_called()


# =============================================================================
# Central Queue Worker Ordering Tests
# =============================================================================


class TestCentralQueueWorkerOrdering:
    """The worker must migrate pre-GSI records before it polls the priority GSI."""

    @pytest.mark.asyncio
    async def test_migration_precedes_queue_poll(self):
        from gco.services.central_queue_worker import process_queued_jobs_once

        events: list[str] = []
        processor = MagicMock(region="us-east-1")
        store = MagicMock(claim_lease_seconds=300)
        store.migrate_legacy_records_for_region.side_effect = lambda *args: (
            events.append("migrate")
            or {"evaluated": 0, "migrated": 0, "failed": 0, "complete": True}
        )
        store.get_queued_jobs_for_region.side_effect = lambda *args: events.append("poll") or []

        polled, processed = await process_queued_jobs_once(processor, store, limit=5)

        assert (polled, processed) == (0, [])
        assert events == ["migrate", "poll"]
        store.migrate_legacy_records_for_region.assert_called_once_with("us-east-1", 100)
        # The candidate fetch is wider than the apply limit (limit*4, floor 20)
        # so price-gated jobs cannot starve dispatchable work behind them.
        store.get_queued_jobs_for_region.assert_called_once_with("us-east-1", 20)


# =============================================================================
# Singleton Tests
# =============================================================================


class TestSingletons:
    """Tests for singleton getter functions."""

    def test_get_template_store_singleton(self):
        """Test that get_template_store returns a singleton."""
        import gco.services.template_store as module

        # Reset singleton
        module._template_store = None

        with patch("boto3.resource"):
            store1 = get_template_store()
            store2 = get_template_store()

            assert store1 is store2

        # Clean up
        module._template_store = None

    def test_get_webhook_store_singleton(self):
        """Test that get_webhook_store returns a singleton."""
        import gco.services.template_store as module

        # Reset singleton
        module._webhook_store = None

        with patch("boto3.resource"):
            store1 = get_webhook_store()
            store2 = get_webhook_store()

            assert store1 is store2

        # Clean up
        module._webhook_store = None

    def test_get_job_store_singleton(self):
        """Test that get_job_store returns a singleton."""
        import gco.services.template_store as module

        # Reset singleton
        module._job_store = None

        with patch("boto3.resource"):
            store1 = get_job_store()
            store2 = get_job_store()

            assert store1 is store2

        # Clean up
        module._job_store = None


# =============================================================================
# JobStatus Enum Tests
# =============================================================================


class TestJobStatusEnum:
    """Tests for JobStatus enum."""

    def test_job_status_values(self):
        """Test JobStatus enum has expected values."""
        assert JobStatus.QUEUED.value == "queued"
        assert JobStatus.CLAIMED.value == "claimed"
        assert JobStatus.APPLYING.value == "applying"
        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.RUNNING.value == "running"
        assert JobStatus.SUCCEEDED.value == "succeeded"
        assert JobStatus.FAILED.value == "failed"
        assert JobStatus.CANCELLED.value == "cancelled"

    def test_job_status_is_string_enum(self):
        """Test JobStatus values can be used as strings."""
        assert str(JobStatus.RUNNING) == "running"
        assert JobStatus.RUNNING.value == "running"


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestTemplateStoreErrors:
    """Tests for TemplateStore error handling."""

    @pytest.fixture
    def mock_dynamodb(self):
        """Create a mock DynamoDB resource."""
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            yield mock_table

    @pytest.fixture
    def template_store(self, mock_dynamodb):
        """Create a TemplateStore with mocked DynamoDB."""
        store = TemplateStore(table_name="test-templates", region="us-east-1")
        store._table = mock_dynamodb
        return store

    def test_get_template_client_error(self, template_store, mock_dynamodb):
        """Test get_template raises on ClientError."""
        mock_dynamodb.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "GetItem",
        )

        with pytest.raises(ClientError):
            template_store.get_template("test-template")

    def test_create_template_client_error(self, template_store, mock_dynamodb):
        """Test create_template raises on ClientError."""
        mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "PutItem",
        )

        with pytest.raises(ClientError):
            template_store.create_template("test", {"apiVersion": "v1"})

    def test_update_template_client_error(self, template_store, mock_dynamodb):
        """Test update_template raises on ClientError."""
        mock_dynamodb.update_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "UpdateItem",
        )

        with pytest.raises(ClientError):
            template_store.update_template("test", manifest={"apiVersion": "v1"})

    def test_delete_template_client_error(self, template_store, mock_dynamodb):
        """Test delete_template raises on ClientError."""
        mock_dynamodb.delete_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "DeleteItem",
        )

        with pytest.raises(ClientError):
            template_store.delete_template("test")


class TestWebhookStoreErrors:
    """Tests for WebhookStore error handling."""

    @pytest.fixture
    def mock_dynamodb(self):
        """Create a mock DynamoDB resource."""
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            yield mock_table

    @pytest.fixture
    def webhook_store(self, mock_dynamodb):
        """Create a WebhookStore with mocked DynamoDB."""
        store = WebhookStore(table_name="test-webhooks", region="us-east-1")
        store._table = mock_dynamodb
        return store

    def test_get_webhook_client_error(self, webhook_store, mock_dynamodb):
        """Test get_webhook raises on ClientError."""
        mock_dynamodb.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "GetItem",
        )

        with pytest.raises(ClientError):
            webhook_store.get_webhook("test-webhook-id")

    def test_create_webhook_client_error(self, webhook_store, mock_dynamodb):
        """Test create_webhook raises on ClientError."""
        mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "PutItem",
        )

        with pytest.raises(ClientError):
            webhook_store.create_webhook(
                webhook_id="test-webhook-id",
                url="https://example.com/webhook",
                events=["job.completed"],
                namespace="default",
            )

    def test_delete_webhook_client_error(self, webhook_store, mock_dynamodb):
        """Test delete_webhook raises on ClientError."""
        mock_dynamodb.delete_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "DeleteItem",
        )

        with pytest.raises(ClientError):
            webhook_store.delete_webhook("test-webhook-id")


class TestJobStoreErrors:
    """Tests for JobStore error handling."""

    @pytest.fixture
    def mock_dynamodb(self):
        """Create a mock DynamoDB resource."""
        with patch("boto3.resource") as mock_resource:
            mock_table = MagicMock()
            mock_resource.return_value.Table.return_value = mock_table
            yield mock_table

    @pytest.fixture
    def job_store(self, mock_dynamodb):
        """Create a JobStore with mocked DynamoDB."""
        store = JobStore(table_name="test-jobs", region="us-east-1")
        store._table = mock_dynamodb
        return store

    def test_submit_job_client_error(self, job_store, mock_dynamodb):
        """Test submit_job raises on ClientError."""
        mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "PutItem",
        )

        with pytest.raises(ClientError):
            job_store.submit_job(
                job_id="test-job-id",
                manifest={"apiVersion": "batch/v1", "kind": "Job"},
                namespace="default",
                target_region="us-east-1",
            )

    def test_get_job_client_error(self, job_store, mock_dynamodb):
        """Test get_job raises on ClientError."""
        mock_dynamodb.get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "GetItem",
        )

        with pytest.raises(ClientError):
            job_store.get_job("test-job-id")

    def test_transition_job_client_error(self, job_store, mock_dynamodb):
        """Test transition_job raises on non-conditional ClientError."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "test-job-id",
                "status": "pending",
                "target_region": "us-east-1",
                "updated_at": "2024-01-01T00:00:00Z",
                "status_history": "[]",
            }
        }
        mock_dynamodb.update_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "UpdateItem",
        )

        with pytest.raises(ClientError):
            job_store.transition_job(
                "test-job-id",
                target_region="us-east-1",
                expected_status=JobStatus.PENDING,
                status=JobStatus.RUNNING,
            )

    def test_cancel_job_client_error(self, job_store, mock_dynamodb):
        """Test cancel_job raises on non-conditional ClientError."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "test-job-id",
                "status": "queued",
                "target_region": "us-east-1",
                "updated_at": "2024-01-01T00:00:00Z",
                "status_history": "[]",
            }
        }
        mock_dynamodb.update_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "UpdateItem",
        )

        with pytest.raises(ClientError):
            job_store.cancel_job("test-job-id")

    def test_list_jobs_client_error(self, job_store, mock_dynamodb):
        """Test list_jobs raises on ClientError."""
        mock_dynamodb.scan.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "Scan",
        )

        with pytest.raises(ClientError):
            job_store.list_jobs()

    def test_claim_job_client_error(self, job_store, mock_dynamodb):
        """Test claim_job raises on non-conditional ClientError."""
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "job_id": "test-job-id",
                "status": "queued",
                "target_region": "us-east-1",
                "updated_at": "2024-01-01T00:00:00Z",
                "claim_generation": 0,
                "status_history": "[]",
            }
        }
        mock_dynamodb.update_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test error"}},
            "UpdateItem",
        )

        with pytest.raises(ClientError):
            job_store.claim_job("test-job-id", "us-east-1", "worker-1")
