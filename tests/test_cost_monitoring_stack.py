"""
Tests for the cost monitoring resources in gco/stacks/monitoring_stack.py.

Synthesizes GCOMonitoringStack (reusing the mock scaffolding from
tests/test_monitoring_stack.py) and asserts the cost pipeline half of the
template: a CloudFormation-generated bucket name (never a reconstructable
one) published through the ``/<project>/cost-report-bucket/*`` SSM contract,
the principal-based bucket and key policy grants for every regional
cost-monitor role, KMS encryption + insecure-transport deny, the three
lifecycle rules driven by cdk.json, the Glue database/table with partition
projection matching the service's write layout, the Athena workgroup with
enforced KMS-encrypted results, and complete absence of every cost resource
when the toggle is off.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from tests.test_monitoring_stack import (
    MockConfigLoader,
    create_mock_api_gateway_stack,
    create_mock_global_stack,
    create_mock_regional_stack,
)

ACCOUNT = "123456789012"
MONITORING_REGION = "us-east-2"


def _synth(*, cost_monitoring_enabled: bool = True) -> assertions.Template:
    from gco.stacks.monitoring_stack import GCOMonitoringStack

    app = cdk.App()
    stack = GCOMonitoringStack(
        app,
        "TestCostMonitoringStack",
        config=MockConfigLoader(cost_monitoring_enabled=cost_monitoring_enabled),
        global_stack=create_mock_global_stack(),
        regional_stacks=[
            create_mock_regional_stack("us-east-1"),
            create_mock_regional_stack("us-west-2"),
        ],
        api_gateway_stack=create_mock_api_gateway_stack(),
        env=cdk.Environment(account=ACCOUNT, region=MONITORING_REGION),
    )
    return assertions.Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> assertions.Template:
    return _synth(cost_monitoring_enabled=True)


EXPECTED_COST_MONITOR_ROLE_ARNS = [
    "arn:aws:iam::123456789012:role/gco-test-us-east-1-CostMonitorRole",
    "arn:aws:iam::123456789012:role/gco-test-us-west-2-CostMonitorRole",
]


def _cost_report_bucket(template: assertions.Template) -> dict:
    """The primary cost bucket: the one KMS bucket that ships access logs."""
    buckets = template.find_resources("AWS::S3::Bucket")
    matches = [
        bucket for bucket in buckets.values() if "LoggingConfiguration" in bucket["Properties"]
    ]
    assert len(matches) == 1, f"expected exactly one logged bucket, found {len(matches)}"
    return matches[0]


def _statements_by_sid(policy_document: dict) -> dict[str, dict]:
    return {
        statement["Sid"]: statement
        for statement in policy_document["Statement"]
        if "Sid" in statement
    }


class TestCostReportBucket:
    def test_no_bucket_carries_an_explicit_name(self, template):
        """S3 names are global and deleted names are not reliably reusable.

        Every bucket in the stack must be CloudFormation-named so a
        destroy-and-redeploy can never collide with its own previous
        incarnation (the failure mode that motivated dropping the old
        ``<project>-cost-reports-<account>-<region>`` name).
        """
        buckets = template.find_resources("AWS::S3::Bucket")
        assert len(buckets) == 2
        for bucket in buckets.values():
            assert "BucketName" not in bucket["Properties"]

    def test_bucket_identity_is_published_through_ssm(self, template):
        from gco.stacks.constants import cost_report_ssm_parameter_prefix

        prefix = cost_report_ssm_parameter_prefix("gco-test")
        assert prefix == "/gco-test/cost-report-bucket"
        parameters = {
            param["Properties"]["Name"]: param["Properties"]
            for param in template.find_resources("AWS::SSM::Parameter").values()
        }
        assert {f"{prefix}/name", f"{prefix}/arn", f"{prefix}/region"} <= set(parameters)
        (bucket_logical_id,) = [
            logical_id
            for logical_id, bucket in template.find_resources("AWS::S3::Bucket").items()
            if "LoggingConfiguration" in bucket["Properties"]
        ]
        assert parameters[f"{prefix}/name"]["Value"] == {"Ref": bucket_logical_id}
        assert parameters[f"{prefix}/arn"]["Value"] == {"Fn::GetAtt": [bucket_logical_id, "Arn"]}
        assert parameters[f"{prefix}/region"]["Value"] == MONITORING_REGION

    def test_bucket_policy_admits_every_regional_cost_monitor_role(self, template):
        policies = template.find_resources("AWS::S3::BucketPolicy")
        grants = [
            statement
            for policy in policies.values()
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
            if statement.get("Sid") == "AllowRegionalCostMonitorReports"
        ]
        (grant,) = grants
        assert grant["Effect"] == "Allow"
        assert sorted(grant["Action"]) == [
            "s3:GetBucketLocation",
            "s3:GetObject",
            "s3:ListBucket",
            "s3:PutObject",
        ]
        assert grant["Principal"] == {"AWS": EXPECTED_COST_MONITOR_ROLE_ARNS}
        # Bucket + object-key space of this bucket only.
        resources = grant["Resource"]
        assert len(resources) == 2
        assert resources[0] == {"Fn::GetAtt": [_bucket_logical_id(template), "Arn"]}
        assert resources[1]["Fn::Join"][1][0] == {
            "Fn::GetAtt": [_bucket_logical_id(template), "Arn"]
        }
        assert resources[1]["Fn::Join"][1][1] == "/*"

    def test_bucket_policy_has_no_wildcard_principal_allow(self, template):
        policies = template.find_resources("AWS::S3::BucketPolicy")
        for policy in policies.values():
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
                if statement["Effect"] == "Allow":
                    assert statement["Principal"] != {"AWS": "*"}
                    assert statement["Principal"] != "*"

    def test_key_policy_admits_the_roles_only_through_s3_in_this_region(self, template):
        keys = template.find_resources("AWS::KMS::Key")
        (key,) = keys.values()
        assert key["Properties"]["EnableKeyRotation"] is True
        assert key["Properties"]["PendingWindowInDays"] == 7
        statements = _statements_by_sid(key["Properties"]["KeyPolicy"])
        grant = statements["AllowRegionalCostMonitorsViaS3"]
        assert grant["Effect"] == "Allow"
        assert sorted(grant["Action"]) == ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"]
        assert grant["Principal"] == {"AWS": EXPECTED_COST_MONITOR_ROLE_ARNS}
        via_service = grant["Condition"]["StringEquals"]["kms:ViaService"]
        # Rendered with the URL-suffix pseudo parameter: s3.<region>.<suffix>.
        assert via_service["Fn::Join"][1][0] == f"s3.{MONITORING_REGION}."
        assert "AllowS3ServiceEncryptDecrypt" in statements

    def test_bucket_is_kms_encrypted_with_rotating_cmk(self, template):
        bucket = _cost_report_bucket(template)
        sse = bucket["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"]
        assert sse[0]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
        assert sse[0]["BucketKeyEnabled"] is True

    def test_lifecycle_rules_carry_the_configured_policy(self, template):
        bucket = _cost_report_bucket(template)
        rules = {
            rule["Id"]: rule for rule in bucket["Properties"]["LifecycleConfiguration"]["Rules"]
        }
        assert set(rules) == {
            "CostReportRetention",
            "AdhocReportRetention",
            "ExpireAthenaResults",
        }
        scheduled = rules["CostReportRetention"]
        assert scheduled["Prefix"] == "reports/"
        assert scheduled["ExpirationInDays"] == 365
        assert scheduled["Transitions"][0] == {
            "StorageClass": "STANDARD_IA",
            "TransitionInDays": 90,
        }
        assert rules["AdhocReportRetention"]["Prefix"] == "adhoc/"
        athena_rule = rules["ExpireAthenaResults"]
        assert athena_rule["Prefix"] == "athena-results/"
        assert athena_rule["ExpirationInDays"] == 30

    def test_bucket_denies_insecure_transport_under_a_known_sid(self, template):
        policies = template.find_resources("AWS::S3::BucketPolicy")
        sids = {
            statement.get("Sid")
            for policy in policies.values()
            for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        }
        assert "DenyInsecureTransport" in sids

    def test_access_logs_bucket_receives_server_access_logs(self, template):
        bucket = _cost_report_bucket(template)
        logging = bucket["Properties"]["LoggingConfiguration"]
        assert logging["LogFilePrefix"] == "cost-reports/"


def _bucket_logical_id(template: assertions.Template) -> str:
    (logical_id,) = [
        logical_id
        for logical_id, bucket in template.find_resources("AWS::S3::Bucket").items()
        if "LoggingConfiguration" in bucket["Properties"]
    ]
    return logical_id


class TestCostAnalytics:
    def test_glue_database_uses_underscored_project_name(self, template):
        from gco.stacks.constants import cost_glue_database_name

        assert cost_glue_database_name("gco-test") == "gco_test_cost"
        template.has_resource_properties(
            "AWS::Glue::Database",
            {"DatabaseInput": {"Name": "gco_test_cost"}},
        )

    def test_allocation_table_columns_match_the_service_contract(self, template):
        from gco.services.cost_monitor import ALLOCATION_REPORT_FIELDS

        tables = template.find_resources("AWS::Glue::Table")
        (table,) = tables.values()
        table_input = table["Properties"]["TableInput"]
        assert table_input["Name"] == "allocation_reports"
        columns = [column["Name"] for column in table_input["StorageDescriptor"]["Columns"]]
        assert columns == list(ALLOCATION_REPORT_FIELDS)
        partition_keys = [key["Name"] for key in table_input["PartitionKeys"]]
        assert partition_keys == ["region", "date"]

    def test_partition_projection_covers_the_deployment_regions(self, template):
        import json

        tables = template.find_resources("AWS::Glue::Table")
        (table,) = tables.values()
        parameters = table["Properties"]["TableInput"]["Parameters"]
        assert parameters["projection.enabled"] == "true"
        assert parameters["projection.region.type"] == "enum"
        assert parameters["projection.region.values"] == "us-east-1,us-west-2"
        assert parameters["projection.date.type"] == "date"
        # The template embeds the bucket Ref, so it renders as an Fn::Join
        # intrinsic; assert on its serialized form.
        template_value = json.dumps(parameters["storage.location.template"])
        assert "/reports/region=${region}/date=${date}" in template_value

    def test_athena_workgroup_enforces_kms_encrypted_results(self, template):
        from gco.stacks.constants import cost_athena_workgroup_name

        assert cost_athena_workgroup_name("gco-test") == "gco-test-cost"
        workgroups = template.find_resources("AWS::Athena::WorkGroup")
        (workgroup,) = workgroups.values()
        properties = workgroup["Properties"]
        assert properties["Name"] == "gco-test-cost"
        configuration = properties["WorkGroupConfiguration"]
        assert configuration["EnforceWorkGroupConfiguration"] is True
        result_config = configuration["ResultConfiguration"]
        import json

        assert "/athena-results/" in json.dumps(result_config["OutputLocation"])
        assert result_config["EncryptionConfiguration"]["EncryptionOption"] == "SSE_KMS"

    def test_outputs_name_the_analytics_entry_points(self, template):
        outputs = template.to_json().get("Outputs", {})
        assert "CostReportBucketName" in outputs
        assert "CostAthenaWorkGroupName" in outputs
        assert "CostGlueDatabaseName" in outputs


class TestCostMonitoringDisabled:
    def test_no_cost_resources_synthesize_when_disabled(self):
        template = _synth(cost_monitoring_enabled=False)
        template.resource_count_is("AWS::Glue::Database", 0)
        template.resource_count_is("AWS::Glue::Table", 0)
        template.resource_count_is("AWS::Athena::WorkGroup", 0)
        template.resource_count_is("AWS::S3::Bucket", 0)
        template.resource_count_is("AWS::KMS::Key", 0)
        template.resource_count_is("AWS::SSM::Parameter", 0)
        outputs = template.to_json().get("Outputs", {})
        assert "CostReportBucketName" not in outputs
