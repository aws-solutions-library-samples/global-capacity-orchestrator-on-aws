# CDK synthesis tests for the vector-store add-on folded into
# GCOGlobalStack (gated by vector_store.enabled, OFF by default; the
# shared MockConfigLoader mirrors that so unrelated stack tests keep
# their pre-feature templates).
#
# The table is a TableV2 — AWS::DynamoDB::GlobalTable, not
# AWS::DynamoDB::Table — so replica assertions read the Replicas list
# (which always includes the stack's own region as the primary replica).
# TableV2 replicas require a region-bound stack, so the enabled synths
# here pass an explicit env, exactly as app.py does for the real global
# stack. The vector index cannot be expressed in CloudFormation, so the
# index assertions target the Custom::AWS resource's Create/Delete
# payloads, mirroring tests/test_mission_memory_stack.py (which also
# documents the three live-earned custom-resource gotchas pinned again
# here: flat action + same-call AttributeDefinitions, InstallLatestAwsSdk,
# and delete-path error swallowing).

import json
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
from aws_cdk import assertions

from gco.stacks.global_stack import GCOGlobalStack
from tests.test_mission_memory_stack import _resolve_joins
from tests.test_regional_stack import MockConfigLoader

_ENV = cdk.Environment(account="111111111111", region="us-east-2")


class _EnabledConfig(MockConfigLoader):
    """MockConfigLoader variant with the vector store enabled.

    ``replica_regions`` stays empty, exercising the derivation default:
    follow ``deployment_regions.regional`` (us-east-1 in this mock) minus
    the global region (us-east-2, the primary).
    """

    def get_vector_store_config(self):
        return {
            "enabled": True,
            "dimensions": 1024,
            "distance_function": "COSINE",
            "embedding_model_id": "amazon.titan-embed-text-v2:0",
            "replica_regions": [],
            "corpus_prefix": "vector-corpus/",
        }


class _ExplicitReplicasConfig(_EnabledConfig):
    """Enabled variant pinning replica_regions explicitly.

    The list deliberately includes the global region to prove the
    derivation strips it: a global table cannot replicate into its own
    (primary) region.
    """

    def get_vector_store_config(self):
        config = super().get_vector_store_config()
        return {**config, "replica_regions": ["eu-west-1", "us-east-2"]}


def _synth(cfg, *, env=_ENV):
    app = cdk.App()
    kwargs = {"env": env} if env is not None else {}
    stack = GCOGlobalStack(app, "id", config=cfg, **kwargs)
    return assertions.Template.from_stack(stack)


def _store_tables(template):
    out = {}
    for lid, res in template.find_resources("AWS::DynamoDB::GlobalTable").items():
        name = res.get("Properties", {}).get("TableName")
        if isinstance(name, str) and name.endswith("-vector-store"):
            out[lid] = res
    return out


def _index_custom_resources(template):
    out = {}
    for lid, res in template.find_resources("Custom::AWS").items():
        create = res.get("Properties", {}).get("Create")
        if create is not None and "corpus-embedding-index" in json.dumps(create):
            out[lid] = res
    return out


class TestVectorStoreEnabled:
    def test_global_table_present_with_pitr_sse_and_no_vector_property(self):
        template = _synth(_EnabledConfig())
        tables = _store_tables(template)
        assert len(tables) == 1
        props = next(iter(tables.values()))["Properties"]
        assert props["BillingMode"] == "PAY_PER_REQUEST"
        assert props["KeySchema"] == [{"AttributeName": "doc_id", "KeyType": "HASH"}]
        assert props["SSESpecification"]["SSEEnabled"] is True
        # PITR rides per replica on a GlobalTable resource.
        for replica in props["Replicas"]:
            assert replica["PointInTimeRecoverySpecification"] == {
                "PointInTimeRecoveryEnabled": True
            }
        # CloudFormation cannot express vector indexes; if this key ever
        # appears, the custom resource should be replaced with the native
        # property and this suite rewritten.
        assert "VectorIndexes" not in props

    def test_default_replicas_follow_regional_deployments(self):
        # Mock topology: regional=[us-east-1], global=us-east-2. The
        # GlobalTable resource lists the primary (stack) region as a replica
        # too, so the expected set is exactly {primary, derived replica}.
        template = _synth(_EnabledConfig())
        props = next(iter(_store_tables(template).values()))["Properties"]
        regions = sorted(replica["Region"] for replica in props["Replicas"])
        assert regions == ["us-east-1", "us-east-2"]

    def test_configured_replicas_win_and_the_global_region_is_stripped(self):
        # replica_regions=["eu-west-1", "us-east-2"]: the explicit list
        # replaces the regional-deployments derivation, and the global
        # region entry is dropped because the primary already lives there.
        template = _synth(_ExplicitReplicasConfig())
        props = next(iter(_store_tables(template).values()))["Properties"]
        regions = sorted(replica["Region"] for replica in props["Replicas"])
        assert regions == ["eu-west-1", "us-east-2"]

    def test_vector_index_custom_resource_create_payload(self):
        template = _synth(_EnabledConfig())
        custom = _index_custom_resources(template)
        assert len(custom) == 1
        create = json.loads(_resolve_joins(next(iter(custom.values()))["Properties"]["Create"]))
        assert create["service"] == "DynamoDB"
        assert create["action"] == "updateTable"
        # The INLINE_FILTER attribute must be declared in the same
        # UpdateTable call (live-verified on mission memory; re-verified
        # against a 2019.11.21 global table in the Phase 2 spike).
        assert create["parameters"]["AttributeDefinitions"] == [
            {"AttributeName": "source", "AttributeType": "S"}
        ]
        updates = create["parameters"]["VectorIndexUpdates"]
        assert len(updates) == 1
        spec = updates[0]["Create"]
        # CreateVectorIndexAction is FLAT (the nested draft shape deploys
        # as nulls and fails; see the mission-memory suite for history).
        assert spec["IndexName"] == "corpus-embedding-index"
        assert spec["VectorAttribute"] == {"AttributeName": "embedding"}
        assert spec["Dimensions"] == 1024
        assert spec["DistanceFunction"] == "COSINE"
        assert spec["SearchSchema"] == [
            {"AttributeName": "source", "SearchSchemaElementType": "INLINE_FILTER"}
        ]
        projection = spec["Projection"]
        assert projection["ProjectionType"] == "INCLUDE"
        assert set(projection["NonKeyAttributes"]) == {
            "text",
            "source",
            "chunk_index",
            "title",
            "embedding_model_id",
        }

    def test_vector_index_deletes_on_teardown(self):
        template = _synth(_EnabledConfig())
        custom = _index_custom_resources(template)
        delete = json.loads(_resolve_joins(next(iter(custom.values()))["Properties"]["Delete"]))
        assert delete["action"] == "updateTable"
        assert delete["parameters"]["VectorIndexUpdates"] == [
            {"Delete": {"IndexName": "corpus-embedding-index"}}
        ]

    def test_custom_resource_installs_a_current_sdk(self):
        """The index call must not run on the runtime's bundled SDK.

        Same live-deploy regression pinned for mission memory: a bundled
        SDK that predates vector indexes silently drops the unknown
        ``VectorIndexUpdates`` member at serialization and the create
        fails at deploy time.
        """
        template = _synth(_EnabledConfig())
        custom = _index_custom_resources(template)
        props = next(iter(custom.values()))["Properties"]
        assert props["InstallLatestAwsSdk"] is True

    def test_delete_tolerates_absent_index_so_teardown_never_wedges(self):
        """A failed create must roll back cleanly, not strand the stack.

        The global-table variant has one extra reason to swallow
        ValidationException: on stack teardown the index delete can race
        in-flight replica deletions.
        """
        template = _synth(_EnabledConfig())
        custom = _index_custom_resources(template)
        delete = json.loads(_resolve_joins(next(iter(custom.values()))["Properties"]["Delete"]))
        assert delete["ignoreErrorCodesMatching"] == "ResourceNotFoundException|ValidationException"

    def test_payloads_validate_against_the_botocore_api_model(self):
        """Every member the custom resource sends must exist in the API model.

        Unknown members are silently dropped at SDK serialization, so a
        wrong shape only surfaces at deploy time; walking the synthesized
        payloads against botocore's UpdateTable model catches reshaping at
        unit-test time (see the mission-memory suite for the incident
        history behind this).
        """
        import botocore.session

        model = botocore.session.get_session().get_service_model("dynamodb")
        update_table = model.operation_model("UpdateTable").input_shape

        def check(payload, shape, path):
            if shape.type_name == "structure":
                assert isinstance(payload, dict), f"{path}: expected object"
                unknown = set(payload) - set(shape.members)
                assert not unknown, (
                    f"{path}: members {sorted(unknown)} do not exist in the API model "
                    "and would be silently dropped at SDK serialization"
                )
                missing = set(shape.required_members) - set(payload)
                assert not missing, f"{path}: required members {sorted(missing)} absent"
                for key, value in payload.items():
                    check(value, shape.members[key], f"{path}.{key}")
            elif shape.type_name == "list":
                assert isinstance(payload, list), f"{path}: expected list"
                for index, entry in enumerate(payload):
                    check(entry, shape.member, f"{path}[{index}]")
            # Scalars: resolved refs and literals both acceptable here — the
            # shape walk is about member names, not value types.

        template = _synth(_EnabledConfig())
        custom = _index_custom_resources(template)
        props = next(iter(custom.values()))["Properties"]
        for call_name in ("Create", "Delete"):
            call = json.loads(_resolve_joins(props[call_name]))
            check(call["parameters"], update_table, f"{call_name}.parameters")

    def test_index_role_scoped_to_the_table_only(self):
        # Mission memory is disabled in this mock, so exactly one
        # UpdateTable-bearing policy statement exists: the vector store's.
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
        # The resource is a Fn::GetAtt on the vector-store table — never a
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
        assert any(name.endswith("/vector-store-table-name") for name in names)
        index_values = [
            value for name, value in names.items() if name.endswith("/vector-store-index-name")
        ]
        assert index_values == ["corpus-embedding-index"]

    def test_table_joins_the_backup_plan_selection(self):
        template = _synth(_EnabledConfig())
        selections = template.find_resources("AWS::Backup::BackupSelection")
        blob = json.dumps(selections)
        assert "VectorStoreTable" in blob


class TestVectorIngestWiring:
    def test_notification_watches_the_corpus_prefix_for_creates(self):
        template = _synth(_EnabledConfig())
        notifications = template.find_resources("Custom::S3BucketNotifications")
        assert len(notifications) == 1
        config = next(iter(notifications.values()))["Properties"]["NotificationConfiguration"]
        (lambda_config,) = config["LambdaFunctionConfigurations"]
        assert lambda_config["Events"] == ["s3:ObjectCreated:*"]
        assert lambda_config["Filter"]["Key"]["FilterRules"] == [
            {"Name": "prefix", "Value": "vector-corpus/"}
        ]

    def test_ingest_function_shape_env_and_dlq(self):
        template = _synth(_EnabledConfig())
        functions = {
            lid: res
            for lid, res in template.find_resources("AWS::Lambda::Function").items()
            if "Vector-store ingest" in str(res.get("Properties", {}).get("Description", ""))
        }
        assert len(functions) == 1
        props = next(iter(functions.values()))["Properties"]
        assert props["Timeout"] == 300
        assert props["MemorySize"] == 512
        env = props["Environment"]["Variables"]
        assert env["EMBEDDING_MODEL_ID"] == "amazon.titan-embed-text-v2:0"
        assert env["EMBEDDING_DIMENSIONS"] == "1024"
        assert env["CORPUS_PREFIX"] == "vector-corpus/"
        assert "Ref" in json.dumps(env["VECTOR_STORE_TABLE_NAME"])
        # Async failures land in the dedicated DLQ after Lambda's retries.
        assert "TargetArn" in props["DeadLetterConfig"]

    def test_ingest_role_carries_the_write_only_identity(self):
        # GetObject scoped to the corpus prefix, PutItem on the table,
        # InvokeModel on the one embedding model — and none of the read
        # path (SearchVectors/GetItem/Query belong to workloads and the
        # CLI, never to ingest).
        template = _synth(_EnabledConfig())
        statements = []
        for res in template.find_resources("AWS::IAM::Policy").values():
            statements.extend(res["Properties"]["PolicyDocument"]["Statement"])
        blob = json.dumps(statements)
        actions = {
            action
            for stmt in statements
            for action in (
                [stmt["Action"]] if isinstance(stmt.get("Action"), str) else stmt.get("Action", [])
            )
        }
        assert "dynamodb:PutItem" in actions
        assert "bedrock:InvokeModel" in actions
        assert "s3:GetObject" in actions
        assert "vector-corpus/*" in blob
        assert "foundation-model/amazon.titan-embed-text-v2:0" in blob
        assert "dynamodb:SearchVectors" not in actions
        assert "dynamodb:GetItem" not in actions
        assert "dynamodb:Query" not in actions


def _mock_helm_installer(stack):
    """Stand-in for the Docker-building helm-installer construct (synth only)."""
    stack.helm_installer_lambda = MagicMock()
    stack.helm_installer_provider = MagicMock()
    stack.helm_installer_provider.service_token = (
        "arn:aws:lambda:us-east-1:123456789012:function:mock"  # nosec B106 - test fixture ARN
    )


def _synth_regional(config):
    """Synthesize GCORegionalStack the way test_regional_stack.py does."""
    from gco.stacks.regional_stack import GCORegionalStack

    app = cdk.App()
    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(GCORegionalStack, "_create_helm_installer_lambda", _mock_helm_installer),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = "123456789012.dkr.ecr.us-east-1.amazonaws.com/test:latest"
        mock_docker.return_value = mock_image
        stack = GCORegionalStack(
            app,
            "vs-regional",
            config=config,
            region="us-east-1",
            auth_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-secret",  # nosec B106 - test fixture ARN
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )
        return assertions.Template.from_stack(stack)


def _convergence_replacements(template):
    """The ImageReplacements map on the convergence-trigger custom resource."""
    resources = template.to_json().get("Resources", {})
    trigger = resources.get("HelmInstallCharts")
    assert trigger is not None, "HelmInstallCharts CustomResource must be present"
    return trigger.get("Properties", {}).get("ImageReplacements", {})


class TestRegionalWiring:
    """Regional-stack wiring: ConfigMap replacements and workload read grants.

    Every replacement value is a synth-time literal (the global stack names
    the table and index from the same config), and the IAM grants point at
    the cluster's LOCAL global-table replica — the whole point of paying for
    replication.
    """

    def test_enabled_populates_the_vector_store_replacements(self):
        replacements = _convergence_replacements(_synth_regional(_EnabledConfig()))

        assert replacements["{{VECTOR_STORE_TABLE_NAME}}"] == "gco-test-vector-store"
        assert replacements["{{VECTOR_STORE_INDEX_NAME}}"] == "corpus-embedding-index"
        assert replacements["{{VECTOR_STORE_EMBEDDING_MODEL_ID}}"] == "amazon.titan-embed-text-v2:0"
        assert replacements["{{VECTOR_STORE_DIMENSIONS}}"] == "1024"

    def test_disabled_leaves_the_placeholders_unreplaced(self):
        # The applier skips 26-storage-vector-store.yaml when its
        # placeholders survive replacement — absence of the keys IS the
        # disable mechanism, so none may leak into a disabled synth.
        replacements = _convergence_replacements(_synth_regional(MockConfigLoader()))

        vector_keys = [key for key in replacements if "VECTOR_STORE" in key]
        assert vector_keys == []

    def test_enabled_grants_local_region_read_path_to_workloads(self):
        template = _synth_regional(_EnabledConfig())
        statements = []
        for res in template.find_resources("AWS::IAM::Policy").values():
            statements.extend(res["Properties"]["PolicyDocument"]["Statement"])

        search_statements = [
            stmt
            for stmt in statements
            if "dynamodb:SearchVectors"
            in ([stmt["Action"]] if isinstance(stmt.get("Action"), str) else stmt.get("Action", []))
        ]
        assert len(search_statements) == 1
        stmt = search_statements[0]
        assert sorted(stmt["Action"]) == [
            "dynamodb:GetItem",
            "dynamodb:Query",
            "dynamodb:SearchVectors",
        ]
        # LOCAL-region (us-east-1 cluster), exact table + exact index — the
        # store's primary lives in the global region, but reads stay local.
        # The partition rides as a CloudFormation token, so assert the
        # deterministic remainder of each ARN.
        assert len(stmt["Resource"]) == 2
        table_arn, index_arn = (json.dumps(entry) for entry in stmt["Resource"])
        assert ":dynamodb:us-east-1:123456789012:table/gco-test-vector-store" in table_arn
        assert table_arn.count("index") == 0
        assert (
            ":dynamodb:us-east-1:123456789012:table/gco-test-vector-store"
            "/index/corpus-embedding-index"
        ) in index_arn

        bedrock_statements = [
            stmt
            for stmt in statements
            if "bedrock:InvokeModel"
            in ([stmt["Action"]] if isinstance(stmt.get("Action"), str) else stmt.get("Action", []))
        ]
        assert len(bedrock_statements) == 1
        assert (":bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0") in json.dumps(
            bedrock_statements[0]["Resource"]
        )

    def test_disabled_grants_no_vector_store_access(self):
        template = _synth_regional(MockConfigLoader())
        blob = json.dumps(template.to_json())
        assert "dynamodb:SearchVectors" not in blob
        assert "bedrock:InvokeModel" not in blob
        assert "vector-store" not in blob


class TestVectorStoreDisabled:
    def test_no_store_table_or_custom_resource(self):
        # Region-agnostic synth on purpose: every pre-existing global-stack
        # test synthesizes without an env, and TableV2 replicas would throw
        # there — proving the disabled gate never constructs the table.
        template = _synth(MockConfigLoader(), env=None)
        assert template.find_resources("AWS::DynamoDB::GlobalTable") == {}
        assert _index_custom_resources(template) == {}

    def test_disabled_path_leaves_the_shared_bucket_unwatched(self):
        # The ingest notification is additive (a separate
        # Custom::S3BucketNotifications resource); disabled deployments
        # must carry neither it nor its singleton handler, leaving the
        # cluster-shared bucket's template exactly as before the feature.
        template = _synth(MockConfigLoader(), env=None)
        assert template.find_resources("Custom::S3BucketNotifications") == {}

    def test_disabled_template_carries_no_feature_traces(self):
        # With the feature shipped OFF by default, the disabled path is the
        # compatibility contract: no resource names, SSM paths, or exports
        # of the feature may appear anywhere in the template.
        template = _synth(MockConfigLoader(), env=None).to_json()
        blob = json.dumps(template)
        assert "vector-store" not in blob
        assert "VectorStore" not in blob
        assert "vector-corpus" not in blob
        assert "VectorIngest" not in blob
