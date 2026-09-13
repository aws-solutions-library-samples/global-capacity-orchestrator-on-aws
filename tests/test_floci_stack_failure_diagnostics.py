"""Floci layer: deploy-failure diagnostics and stuck-stack recovery on a real rollback.

When a stack rolls back, ``StackManager`` has two jobs: tell the operator
which resource failed and why (``_collect_operation_events`` →
``_summarize_failure_events`` → ``_diagnose_deploy_failure``), and, on the
next deploy, clear the ``ROLLBACK_COMPLETE`` carcass after re-validating its
identity (``_check_and_fix_stuck_stack``). The unit suite drives both with
hand-written event pages; nothing there proves the filters against the event
stream CloudFormation really emits — newest first, the resource's
``CREATE_FAILED`` buried under the rollback's ``DELETE_*`` cascade, the
stack-level verdicts, the "User Initiated" start marker.

Here the rollback is real. A bucket name is taken first, then a stack that
tries to create a bucket under that name (behind a fan of parameters so the
rollback produces a long cascade) is created in the emulator and rolls back
on S3's own refusal — the failure family that motivated the diagnostics
rewrite. The diagnostics run unmodified against that stream, and the
stuck-stack pre-check deletes the carcass only when the checkpointed stack id
matches. Event pagination stays in the unit suite: the emulator answers every
event on one page.
"""

from __future__ import annotations

import json
import time
from contextlib import suppress

import boto3
import pytest
from botocore.exceptions import ClientError

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

#: Parameters created before the doomed bucket; each one becomes two
#: ``DELETE_*`` cascade rows the summary must look past.
_CASCADE_WIDTH = 12


@pytest.fixture()
def manager(verified_floci_endpoint):
    from cli.config import GCOConfig
    from cli.stacks import StackManager

    return StackManager(
        GCOConfig(
            project_name=unique_name("gcofail").replace("-", "")[:16],
            default_region="us-east-1",
            api_gateway_region="us-east-2",
            global_region="us-east-2",
            monitoring_region="us-east-2",
            output_format="json",
        )
    )


@pytest.fixture()
def rolled_back_stack(manager):
    """A ``<project>-monitoring`` stack that rolled back on a bucket-name collision."""
    stack_name = f"{manager.config.project_name}-monitoring"
    # The Region the diagnostics will look in — resolved the way deploy does,
    # so the stack is created exactly where the code under test expects it.
    region = manager._get_deploy_region(stack_name)
    assert isinstance(region, str) and region
    cloudformation = boto3.client("cloudformation", region_name=region)
    s3 = boto3.client("s3", region_name=region)

    taken = unique_name("gcofail-taken")
    s3.create_bucket(Bucket=taken, CreateBucketConfiguration={"LocationConstraint": region})
    parameters = {
        f"Fan{index:02d}": {
            "Type": "AWS::SSM::Parameter",
            "Properties": {"Name": f"/{stack_name}/fan/{index}", "Type": "String", "Value": "1"},
        }
        for index in range(_CASCADE_WIDTH)
    }
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {
            **parameters,
            "CostReportBucketA1B2C3D4": {
                "Type": "AWS::S3::Bucket",
                "DependsOn": sorted(parameters),
                "Properties": {"BucketName": taken},
            },
        },
    }
    stack_id = cloudformation.create_stack(StackName=stack_name, TemplateBody=json.dumps(template))[
        "StackId"
    ]
    for _ in range(240):
        status = cloudformation.describe_stacks(StackName=stack_id)["Stacks"][0]["StackStatus"]
        if not status.endswith("IN_PROGRESS"):
            break
        time.sleep(0.5)
    assert status == "ROLLBACK_COMPLETE", status

    yield {
        "stack_name": stack_name,
        "stack_id": stack_id,
        "region": region,
        "cloudformation": cloudformation,
        "taken_bucket": taken,
    }

    with suppress(ClientError):
        cloudformation.delete_stack(StackName=stack_id)
        cloudformation.get_waiter("stack_delete_complete").wait(
            StackName=stack_id, WaiterConfig={"Delay": 1, "MaxAttempts": 120}
        )
    s3.delete_bucket(Bucket=taken)


def _stack_exists(cloudformation, stack_name: str) -> bool:
    try:
        cloudformation.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        assert error.get("Code") == "ValidationError", exc
        assert "does not exist" in str(error.get("Message")), exc
        return False
    return True


class TestRolledBackStackDiagnostics:
    def test_the_event_walk_reaches_the_operations_user_initiated_start(self, rolled_back_stack):
        from cli.stacks import StackManager

        events = StackManager._collect_operation_events(
            rolled_back_stack["cloudformation"], rolled_back_stack["stack_name"]
        )

        start = events[-1]
        assert start["LogicalResourceId"] == rolled_back_stack["stack_name"]
        assert start["ResourceStatus"].endswith("_IN_PROGRESS")
        assert "User Initiated" in start["ResourceStatusReason"]
        statuses = {(event["LogicalResourceId"], event["ResourceStatus"]) for event in events}
        assert ("CostReportBucketA1B2C3D4", "CREATE_FAILED") in statuses
        assert (rolled_back_stack["stack_name"], "ROLLBACK_COMPLETE") in statuses
        cascade = [event for event in events if event["ResourceStatus"].startswith("DELETE_")]
        assert len(cascade) >= 2 * _CASCADE_WIDTH, (
            "the rollback must have produced the cascade the summary has to look past"
        )

    def test_the_summary_leads_with_the_resource_s3_refused(self, rolled_back_stack):
        from cli.stacks import StackManager

        cloudformation = rolled_back_stack["cloudformation"]
        stack_name = rolled_back_stack["stack_name"]
        events = StackManager._collect_operation_events(cloudformation, stack_name)

        selected = StackManager._summarize_failure_events(events, stack_name)

        root_cause, *rest = selected
        assert root_cause["LogicalResourceId"] == "CostReportBucketA1B2C3D4"
        assert root_cause["ResourceType"] == "AWS::S3::Bucket"
        assert root_cause["ResourceStatus"] == "CREATE_FAILED"
        assert "bucket" in str(root_cause["ResourceStatusReason"]).lower(), (
            "the reason must be S3's own refusal, verbatim"
        )
        (verdict,) = rest
        assert verdict["LogicalResourceId"] == stack_name
        assert verdict["ResourceStatusReason"], "the stack verdict carries CloudFormation's words"
        assert all(not event["ResourceStatus"].startswith("DELETE_") for event in selected), (
            "rollback cascade rows are noise next to the real failure"
        )

    def test_diagnose_prints_the_cause_before_the_advice(self, rolled_back_stack, manager, capsys):
        manager._diagnose_deploy_failure(rolled_back_stack["stack_name"])

        out = capsys.readouterr().out
        stack_name = rolled_back_stack["stack_name"]
        assert f"CloudFormation failure details for {stack_name}:" in out
        cause_line = "    CostReportBucketA1B2C3D4 (AWS::S3::Bucket): CREATE_FAILED"
        assert cause_line in out
        assert "DELETE_COMPLETE" not in out and "DELETE_IN_PROGRESS" not in out
        assert (
            "Suggested fix: Stack rolled back. Delete it and retry: "
            f"aws cloudformation delete-stack --stack-name {stack_name} "
            f"--region {rolled_back_stack['region']}"
        ) in out
        assert out.index(cause_line) < out.index("Suggested fix"), (
            "the operator must read the cause before the remedy"
        )


class TestStuckStackRecoveryOverTheWire:
    """Owns ``_check_and_fix_stuck_stack`` for real (see ``conftest`` allow-list)."""

    def test_a_mismatched_checkpoint_identity_refuses_to_delete(self, rolled_back_stack, manager):
        cloudformation = rolled_back_stack["cloudformation"]
        stack_name = rolled_back_stack["stack_name"]
        foreign_id = rolled_back_stack["stack_id"].rsplit("/", 1)[0] + (
            "/00000000-0000-4000-8000-000000000000"
        )

        with pytest.raises(RuntimeError, match="Stack identity changed"):
            manager._check_and_fix_stuck_stack(
                stack_name, expected_stack_id=foreign_id, strict_ownership=True
            )

        assert _stack_exists(cloudformation, stack_name), (
            "an identity mismatch must leave the stack alone: it is not the one we made"
        )

    def test_strict_mode_refuses_an_uncheckpointed_stack(self, rolled_back_stack, manager):
        with pytest.raises(RuntimeError, match="Refusing to adopt uncheckpointed stack"):
            manager._check_and_fix_stuck_stack(
                rolled_back_stack["stack_name"], expected_stack_id=None, strict_ownership=True
            )
        assert _stack_exists(rolled_back_stack["cloudformation"], rolled_back_stack["stack_name"])

    def test_the_checkpointed_carcass_is_deleted_then_nothing_remains_to_fix(
        self, rolled_back_stack, manager, capsys
    ):
        cloudformation = rolled_back_stack["cloudformation"]
        stack_name = rolled_back_stack["stack_name"]
        authorized: list[tuple[str, str, str]] = []

        manager._check_and_fix_stuck_stack(
            stack_name,
            expected_stack_id=rolled_back_stack["stack_id"],
            authorize_stack=lambda name, region, stack_id: authorized.append(
                (name, region, stack_id)
            ),
            strict_ownership=True,
        )

        assert authorized == [
            (stack_name, rolled_back_stack["region"], rolled_back_stack["stack_id"])
        ], "deletion must be authorized against the exact identity CloudFormation returned"
        assert not _stack_exists(cloudformation, stack_name)
        deleted = cloudformation.describe_stacks(StackName=rolled_back_stack["stack_id"])
        assert deleted["Stacks"][0]["StackStatus"] == "DELETE_COMPLETE"
        out = capsys.readouterr().out
        assert f"Stack {stack_name} is in ROLLBACK_COMPLETE state, cleaning up..." in out
        assert f"Stack {stack_name} cleaned up, will recreate on deploy" in out

        # The absent-stack answer is the real ValidationError, and it ends the
        # pre-check quietly even in strict mode: there is nothing left to own.
        manager._check_and_fix_stuck_stack(
            stack_name, expected_stack_id=rolled_back_stack["stack_id"], strict_ownership=True
        )
        assert capsys.readouterr().out == ""
