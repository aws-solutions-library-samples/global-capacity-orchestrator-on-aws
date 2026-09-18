"""Floci layer: the SQS job submission path with production discovery wiring.

``test_floci_sqs_job_path.py`` proves the consumer half of the regional job
queue. This module covers the producer half and the seam between them:
``JobManager.submit_job_sqs`` discovering the regional stack over real
CloudFormation, reading its ``JobQueueUrl`` output, and sending the envelope
over real SQS — then the produce→consume contract, where the exact message
the production submitter wrote is drained by the unmodified production
consumer. The envelope schema (body keys plus the ``Priority``/``JobId``
message attributes) is what both sides independently depend on; pinning it
against real wire traffic means neither side can drift without this module
failing.
"""

from __future__ import annotations

import importlib
import json

import boto3
import pytest
import yaml

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

_REGION = "us-east-1"


def _job_manifest(name: str, namespace: str | None) -> dict:
    """A batch Job that passes every queue-processor validation gate."""
    metadata: dict = {"name": name}
    if namespace is not None:
        metadata["namespace"] = namespace
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata,
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
def project(verified_floci_endpoint):
    """A uniquely named GCO project with its config object."""
    from cli.config import GCOConfig

    name = unique_name("gcotest").replace("-", "")[:16]
    config = GCOConfig(
        project_name=name,
        default_region=_REGION,
        api_gateway_region="us-east-2",
        global_region="us-east-2",
        monitoring_region="us-east-2",
        output_format="json",
    )
    yield config
    cloudformation = boto3.client("cloudformation", region_name=_REGION)
    for summary in cloudformation.list_stacks().get("StackSummaries", []):
        if summary["StackName"].startswith(name) and "DELETE" not in summary["StackStatus"]:
            cloudformation.delete_stack(StackName=summary["StackName"])


@pytest.fixture
def regional_stack_queue(project):
    """A regional GCO stack whose JobQueueUrl output names a real queue.

    ``Ref`` on an ``AWS::SQS::Queue`` resolves to the queue URL, so the
    template publishes exactly what the deployed regional stack publishes
    and the submitter's CloudFormation-driven discovery runs unmodified.
    """
    cloudformation = boto3.client("cloudformation", region_name=_REGION)
    stack_name = f"{project.regional_stack_prefix}-{_REGION}"
    cloudformation.create_stack(
        StackName=stack_name,
        TemplateBody=json.dumps(
            {
                "AWSTemplateFormatVersion": "2010-09-09",
                "Resources": {
                    "JobsQueue": {
                        "Type": "AWS::SQS::Queue",
                        "Properties": {"QueueName": f"{project.project_name}-jobs"},
                    }
                },
                "Outputs": {
                    "ClusterName": {"Value": f"{project.project_name}-{_REGION}"},
                    "JobQueueUrl": {"Value": {"Ref": "JobsQueue"}},
                },
            }
        ),
    )
    cloudformation.get_waiter("stack_create_complete").wait(StackName=stack_name)
    outputs = cloudformation.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in outputs}["JobQueueUrl"]


def _receive_one(sqs, queue_url: str) -> dict:
    response = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=2,
        MessageAttributeNames=["All"],
    )
    messages = response.get("Messages", [])
    assert len(messages) == 1, f"expected exactly one queued message, got {len(messages)}"
    return messages[0]


class TestSubmitJobSqsProducer:
    def test_envelope_and_attributes_reach_the_wire(self, project, regional_stack_queue, tmp_path):
        from cli.jobs import JobManager

        manifest = _job_manifest("wire-shape", namespace="ml-team")
        manifest_file = tmp_path / "job.yaml"
        manifest_file.write_text(yaml.safe_dump(manifest), encoding="utf-8")

        result = JobManager(project).submit_job_sqs(
            manifests=str(manifest_file),
            region=_REGION,
            priority=7,
        )

        assert result["status"] == "queued"
        assert result["method"] == "sqs"
        assert result["region"] == _REGION
        assert result["priority"] == 7
        assert result["queue_url"] == regional_stack_queue
        assert result["job_name"] == "wire-shape"
        # The manifest declared its own namespace; with no fallback given,
        # the envelope must report that declared namespace.
        assert result["namespace"] == "ml-team"

        sqs = boto3.client("sqs", region_name=_REGION)
        message = _receive_one(sqs, regional_stack_queue)
        body = json.loads(message["Body"])
        assert set(body) == {"job_id", "manifests", "namespace", "priority", "submitted_at"}, (
            "the envelope schema is the producer/consumer contract — "
            "additions or removals must update both sides and this test"
        )
        assert body["job_id"] == result["job_id"]
        assert body["priority"] == 7
        assert body["namespace"] == "ml-team"
        assert body["manifests"] == [manifest], "manifests must round-trip byte-for-byte"

        attributes = message["MessageAttributes"]
        assert attributes["JobId"]["StringValue"] == result["job_id"]
        assert attributes["Priority"]["DataType"] == "Number"
        assert attributes["Priority"]["StringValue"] == "7"

        sqs.delete_message(QueueUrl=regional_stack_queue, ReceiptHandle=message["ReceiptHandle"])

    def test_namespace_fallback_fills_only_missing(self, project, regional_stack_queue, tmp_path):
        from cli.jobs import JobManager

        declared = _job_manifest("keeps-own-namespace", namespace="declared-ns")
        missing = _job_manifest("gets-fallback", namespace=None)
        manifest_file = tmp_path / "jobs.yaml"
        manifest_file.write_text(yaml.safe_dump_all([declared, missing]), encoding="utf-8")

        result = JobManager(project).submit_job_sqs(
            manifests=str(manifest_file),
            region=_REGION,
            namespace="fallback-ns",
        )
        assert result["namespace"] == "fallback-ns"

        sqs = boto3.client("sqs", region_name=_REGION)
        message = _receive_one(sqs, regional_stack_queue)
        submitted = {
            m["metadata"]["name"]: m["metadata"]["namespace"]
            for m in json.loads(message["Body"])["manifests"]
        }
        assert submitted == {
            "keeps-own-namespace": "declared-ns",
            "gets-fallback": "fallback-ns",
        }, "an explicit --namespace is a fallback, never an override"

        sqs.delete_message(QueueUrl=regional_stack_queue, ReceiptHandle=message["ReceiptHandle"])

    def test_missing_stack_is_a_loud_error(self, project, tmp_path):
        from cli.jobs import JobManager

        manifest_file = tmp_path / "job.yaml"
        manifest_file.write_text(
            yaml.safe_dump(_job_manifest("nowhere", namespace="gco-jobs")), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="No GCO stack found"):
            JobManager(project).submit_job_sqs(manifests=str(manifest_file), region="us-west-2")


class TestProduceConsumeContract:
    def test_produced_message_is_consumed_by_the_production_processor(
        self, monkeypatch, project, regional_stack_queue, tmp_path
    ):
        """The exact message the submitter wrote drains through the consumer.

        Both halves run production code against the same real queue: only the
        Kubernetes apply — the boundary after SQS — is doubled, exactly as in
        the consumer-side module.
        """
        from cli.jobs import JobManager

        manifest_file = tmp_path / "job.yaml"
        manifest_file.write_text(
            yaml.safe_dump(_job_manifest("contract", namespace="gco-jobs")), encoding="utf-8"
        )
        result = JobManager(project).submit_job_sqs(manifests=str(manifest_file), region=_REGION)

        monkeypatch.setenv("JOB_QUEUE_URL", regional_stack_queue)
        monkeypatch.setenv("AWS_REGION", _REGION)
        import gco.services.queue_processor as queue_processor

        module = importlib.reload(queue_processor)

        from gco.models import ResourceStatus

        applied: list[str] = []

        def fake_apply(manifest):
            applied.append(manifest.get("metadata", {}).get("name", ""))
            return ResourceStatus(
                api_version=manifest.get("apiVersion", ""),
                kind=manifest.get("kind", ""),
                name=manifest.get("metadata", {}).get("name", ""),
                namespace=manifest.get("metadata", {}).get("namespace", "gco-jobs"),
                status="created",
            )

        monkeypatch.setattr(module, "apply_manifest", fake_apply)

        assert module.process_one_message() is True, (
            f"the consumer must accept the producer's envelope for job {result['job_id']}"
        )
        assert applied == ["contract"], "the submitted manifest must be the one applied"

        sqs = boto3.client("sqs", region_name=_REGION)
        attributes = sqs.get_queue_attributes(
            QueueUrl=regional_stack_queue,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        assert (
            attributes["ApproximateNumberOfMessages"],
            attributes["ApproximateNumberOfMessagesNotVisible"],
        ) == ("0", "0"), "a consumed job must be deleted from the queue, not left in flight"
