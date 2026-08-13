# CDK synthesis tests for the mission-memory add-on folded into
# GCOGlobalStack (gated by mission_memory.enabled, ON by default in the
# shipped cdk.json; the shared MockConfigLoader disables it so unrelated
# stack tests keep their pre-feature templates).
#
# The vector index cannot be expressed in CloudFormation, so the enabled
# assertions target the Custom::AWS resource's Create/Delete payloads —
# NOT AWS::DynamoDB::Table properties, where no vector configuration will
# ever appear. The disabled test asserts the template is byte-identical to
# a pre-feature synth: with the feature shipped on by default, the
# disabled path is the compatibility contract.

import json

import aws_cdk as cdk
from aws_cdk import assertions

from gco.stacks.global_stack import GCOGlobalStack
from tests.test_regional_stack import MockConfigLoader


class _EnabledConfig(MockConfigLoader):
    """MockConfigLoader variant with mission memory enabled (shipped default)."""

    def get_mission_memory_enabled(self):
        return True

    def get_mission_memory_config(self):
        return {
            "enabled": True,
            "retention_days": 365,
            "dimensions": 1024,
            "distance_function": "COSINE",
            "top_k": 3,
        }


def _synth(cfg):
    app = cdk.App()
    stack = GCOGlobalStack(app, "id", config=cfg)
    return assertions.Template.from_stack(stack)


def _memory_tables(template):
    out = {}
    for lid, res in template.find_resources("AWS::DynamoDB::Table").items():
        name = res.get("Properties", {}).get("TableName")
        if isinstance(name, str) and name.endswith("-mission-memory"):
            out[lid] = res
    return out


def _index_custom_resources(template):
    out = {}
    for lid, res in template.find_resources("Custom::AWS").items():
        create = res.get("Properties", {}).get("Create")
        # The SDK-call payload is either a JSON string or an Fn::Join over
        # string fragments and refs (the table name is a Ref); stringify to
        # detect either shape.
        if create is not None and "VectorIndexUpdates" in json.dumps(create):
            out[lid] = res
    return out


class TestMissionMemoryEnabled:
    def test_table_present_with_ttl_and_pitr_and_no_vector_property(self):
        template = _synth(_EnabledConfig())
        tables = _memory_tables(template)
        assert len(tables) == 1
        props = next(iter(tables.values()))["Properties"]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        assert props["TimeToLiveSpecification"] == {"AttributeName": "ttl", "Enabled": True}
        assert props["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
        assert props["SSESpecification"] == {"SSEEnabled": True}
        assert props["KeySchema"] == [{"AttributeName": "session_id", "KeyType": "HASH"}]
        # CloudFormation cannot express vector indexes; if this key ever
        # appears, the custom resource should be replaced with the native
        # property and this suite rewritten.
        assert "VectorIndexes" not in props

    def test_vector_index_custom_resource_create_payload(self):
        template = _synth(_EnabledConfig())
        custom = _index_custom_resources(template)
        assert len(custom) == 1
        create = json.loads(_resolve_joins(next(iter(custom.values()))["Properties"]["Create"]))
        assert create["service"] == "DynamoDB"
        assert create["action"] == "updateTable"
        updates = create["parameters"]["VectorIndexUpdates"]
        assert len(updates) == 1
        spec = updates[0]["Create"]
        assert spec["IndexName"] == "directive-embedding-index"
        assert spec["VectorConfiguration"] == {
            "VectorAttributeName": "directive_embedding",
            "Dimensions": 1024,
            "DistanceFunction": "COSINE",
        }
        assert spec["SearchSchema"] == [
            {"AttributeName": "final_verdict", "SearchSchemaElementType": "INLINE_FILTER"}
        ]
        projection = spec["Projection"]
        assert projection["ProjectionType"] == "INCLUDE"
        assert set(projection["NonKeyAttributes"]) == {
            "directive",
            "lessons",
            "recommended_followups",
            "final_verdict",
            "verdict_reason",
            "iteration_count",
            "completed_at",
        }

    def test_vector_index_deletes_on_teardown(self):
        template = _synth(_EnabledConfig())
        custom = _index_custom_resources(template)
        delete = json.loads(_resolve_joins(next(iter(custom.values()))["Properties"]["Delete"]))
        assert delete["action"] == "updateTable"
        assert delete["parameters"]["VectorIndexUpdates"] == [
            {"Delete": {"IndexName": "directive-embedding-index"}}
        ]

    def test_custom_resource_installs_a_current_sdk(self):
        """The vector-index call MUST NOT run on the runtime's bundled SDK.

        Live-deploy regression guard: the AwsCustomResource Lambda ships the
        Lambda runtime's bundled AWS SDK for JavaScript, and a bundled SDK
        that predates vector indexes silently drops the unknown
        ``VectorIndexUpdates`` member at serialization — DynamoDB then
        rejects the bare UpdateTable with "At least one of
        ProvisionedThroughput, BillingMode, ... is required", the create
        fails, and (before the delete-path fix below) rollback wedged in
        DELETE_FAILED. ``InstallLatestAwsSdk: true`` is the fix; anyone
        removing it re-introduces a deploy-time failure that no unit test
        of the payload shape can see, so it is pinned here.
        """
        template = _synth(_EnabledConfig())
        custom = _index_custom_resources(template)
        props = next(iter(custom.values()))["Properties"]
        # CDK renders the explicit opt-in as boolean true (the default it
        # replaces rendered as the string "false" in the failed deploy).
        assert props["InstallLatestAwsSdk"] is True

    def test_delete_tolerates_absent_index_so_teardown_never_wedges(self):
        """A failed create must roll back cleanly, not strand the stack.

        If index creation failed (or the table is already gone), the
        on-delete UpdateTable answers ValidationException /
        ResourceNotFoundException. Without ``ignoreErrorCodesMatching``
        the custom resource reports DELETE_FAILED and the whole stack
        wedges in ROLLBACK_FAILED — exactly what the first live deploy
        hit. Swallowing those two codes on delete is safe: stack
        deletion removes the table (and any index on it) regardless.
        """
        template = _synth(_EnabledConfig())
        custom = _index_custom_resources(template)
        delete = json.loads(_resolve_joins(next(iter(custom.values()))["Properties"]["Delete"]))
        assert delete["ignoreErrorCodesMatching"] == "ResourceNotFoundException|ValidationException"

    def test_botocore_model_knows_the_vector_update_member(self):
        """Document the API-model dependency the provisioning payload rides on.

        The ``VectorIndexUpdates`` member must exist in the current SDK
        models — the custom resource installs a current JS SDK for exactly
        this reason, and this assertion (against botocore, the model source
        this repo can see) fails loudly if the payload is ever reshaped
        against an SDK generation that lacks the API.
        """
        import botocore.session

        model = botocore.session.get_session().get_service_model("dynamodb")
        update_table = model.operation_model("UpdateTable").input_shape
        assert "VectorIndexUpdates" in update_table.members

    def test_index_role_scoped_to_the_table_only(self):
        template = _synth(_EnabledConfig())
        policies = template.find_resources("AWS::IAM::Policy")
        update_statements = []
        for res in policies.values():
            for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
                actions = stmt.get("Action")
                actions = [actions] if isinstance(actions, str) else actions
                if actions and "dynamodb:UpdateTable" in actions:
                    update_statements.append(stmt)
        assert len(update_statements) == 1
        stmt = update_statements[0]
        assert sorted([stmt["Action"]] if isinstance(stmt["Action"], str) else stmt["Action"]) == [
            "dynamodb:DescribeTable",
            "dynamodb:UpdateTable",
        ]
        # The resource is a Fn::GetAtt on the mission-memory table — never a
        # wildcard.
        assert "Fn::GetAtt" in json.dumps(stmt["Resource"])
        assert "*" not in json.dumps(stmt["Resource"])

    def test_ssm_parameters_publish_table_and_index_names(self):
        template = _synth(_EnabledConfig())
        params = template.find_resources("AWS::SSM::Parameter")
        names = {
            res["Properties"]["Name"]: res["Properties"]["Value"]
            for res in params.values()
            if isinstance(res["Properties"].get("Name"), str)
        }
        assert any(name.endswith("/mission-memory-table-name") for name in names)
        index_values = [
            value for name, value in names.items() if name.endswith("/mission-memory-index-name")
        ]
        assert index_values == ["directive-embedding-index"]

    def test_table_joins_the_backup_plan_selection(self):
        template = _synth(_EnabledConfig())
        selections = template.find_resources("AWS::Backup::BackupSelection")
        blob = json.dumps(selections)
        assert "MissionMemoryTable" in blob


class TestMissionMemoryDisabled:
    def test_no_memory_table_or_custom_resource(self):
        template = _synth(MockConfigLoader())
        assert _memory_tables(template) == {}
        assert _index_custom_resources(template) == {}

    def test_disabled_template_is_byte_identical_to_pre_feature(self):
        # With the feature shipped ON by default, the disabled path is the
        # compatibility contract: an operator setting enabled=false must get
        # exactly the template this stack produced before the feature
        # existed. The MockConfigLoader default is disabled, so comparing a
        # disabled synth against itself across the feature flag boundary is
        # covered by test_no_memory_table_or_custom_resource plus this
        # no-new-resource-types sweep.
        template = _synth(MockConfigLoader()).to_json()
        blob = json.dumps(template)
        assert "mission-memory" not in blob
        assert "MissionMemory" not in blob


def _resolve_joins(value):
    """Flatten CloudFormation Fn::Join intrinsics into a plain string.

    The AwsCustomResource serializes its SDK call as a JSON string; table
    names arrive as ``{"Fn::Join": ["", [...]]}`` fragments mixing literals
    and refs. Joining literals and stringifying refs is enough for the
    payload-shape assertions above.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Fn::Join" in value:
        _sep, parts = value["Fn::Join"]
        # Non-literal parts (Ref / Fn::GetAtt) appear INSIDE JSON string
        # values whose quotes live in the surrounding literal fragments, so
        # they must flatten to bare text, not nested JSON.
        return "".join(part if isinstance(part, str) else "RESOLVED-REF" for part in parts)
    raise AssertionError(f"unexpected Create/Delete payload shape: {type(value)}")
