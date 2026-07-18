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
                    "created_at": "2024-01-01T00:00:00Z",
                }
            ]
        }

        result = webhook_store.list_webhooks(namespace="default")

        assert len(result) == 1
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
        store.get_queued_jobs_for_region.assert_called_once_with("us-east-1", 5)


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
