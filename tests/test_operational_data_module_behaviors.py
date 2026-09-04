"""Behavior tests for operational and data-oriented production helpers."""

from __future__ import annotations

import builtins
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest
from botocore.exceptions import ClientError

from cli import (
    _container_runtime,
    _image_mirror,
    _image_uri,
    analytics_user_mgmt,
    autopilot,
    cost_analytics,
    costs,
    ephemeral_bastion,
    models,
    monitoring_user_mgmt,
)
from cli._image_reference import immutable_sha256_digest
from cli.cost_analytics import AthenaQueryError, CostAnalytics
from cli.dag import DagDefinition, DagRunner, DagStep, get_dag_runner
from cli.images import ImageManager
from cli.models import ModelManager

_DIGEST = "a" * 64


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


def test_acyclic_dag_recursion_resumes_parent_dependency_loop(tmp_path: Path) -> None:
    manifest = tmp_path / "job.yaml"
    manifest.write_text("kind: Job\n", encoding="utf-8")
    dag = DagDefinition(
        name="acyclic",
        steps=[
            DagStep(name="final", manifest=str(manifest), depends_on=["left", "right"]),
            DagStep(name="left", manifest=str(manifest), depends_on=["root"]),
            DagStep(name="right", manifest=str(manifest), depends_on=["root"]),
            DagStep(name="root", manifest=str(manifest)),
        ],
    )

    assert dag.validate() == []


def test_dag_runner_discovers_region_and_records_failed_terminal_status(tmp_path: Path) -> None:
    manifest = tmp_path / "job.yaml"
    manifest.write_text("kind: Job\nmetadata:\n  name: actual\n", encoding="utf-8")
    manager = MagicMock()
    manager._aws_client.discover_regional_stacks.return_value = {"us-west-2": object()}
    manager.load_manifests.return_value = [{"kind": "Job", "metadata": {"name": "actual"}}]
    manager.submit_job.return_value = {"job_name": "submitted", "namespace": "gco-jobs"}
    manager.wait_for_job.return_value = SimpleNamespace(status="Failed")
    events: list[tuple[str, str, str]] = []
    dag = DagDefinition(name="failed", steps=[DagStep(name="step", manifest=str(manifest))])

    result = DagRunner(job_manager=manager).run(
        dag,
        progress_callback=lambda *event: events.append(event),
    )

    assert result.steps[0].status == "failed"
    assert result.steps[0].error == "Job ended with status: Failed"
    manager.wait_for_job.assert_called_once()
    assert manager.wait_for_job.call_args.kwargs["region"] == "us-west-2"
    assert any(status == "failed" and "Failed" in message for _, status, message in events)


def test_dag_runner_conservatively_skips_unresolved_pending_state() -> None:
    manager = MagicMock()
    events: list[tuple[str, str, str]] = []
    dag = DagDefinition(
        name="inconsistent",
        steps=[
            DagStep(name="running", manifest="ignored", status="running"),
            DagStep(
                name="pending",
                manifest="ignored",
                depends_on=["running"],
                status="pending",
            ),
        ],
        region="us-east-1",
    )

    result = DagRunner(job_manager=manager).run(
        dag,
        progress_callback=lambda *event: events.append(event),
    )

    assert result.steps[1].status == "skipped"
    assert result.steps[1].error == "Dependencies could not be satisfied"
    assert ("pending", "skipped", "Skipped (dependencies unresolved)") in events
    manager.submit_job.assert_not_called()


def test_dag_runner_breaks_when_only_nonterminal_nonpending_state_remains() -> None:
    manager = MagicMock()
    dag = DagDefinition(
        name="running-only",
        steps=[DagStep(name="running", manifest="ignored", status="running")],
        region="us-east-1",
    )

    result = DagRunner(job_manager=manager).run(dag)

    assert result.steps[0].status == "running"
    manager.submit_job.assert_not_called()


def test_dag_runner_factory_preserves_config() -> None:
    config = SimpleNamespace(project_name="test-gco")

    with patch("cli.dag.get_job_manager", return_value=MagicMock()):
        runner = get_dag_runner(config)

    assert runner.config is config


def _image_manager() -> ImageManager:
    manager = object.__new__(ImageManager)
    manager.config = SimpleNamespace(project_name="gco")
    manager._repo_prefix = "gco"
    manager.region = "us-east-1"
    manager._account_id_cache = None
    return manager


def test_image_manager_creates_regional_ecr_client() -> None:
    manager = _image_manager()
    with patch("cli.images.boto3.client", return_value="ecr-client") as client:
        assert manager._ecr_client() == "ecr-client"
    client.assert_called_once_with("ecr", region_name="us-east-1")


def test_immutable_repository_allows_absent_tag() -> None:
    manager = _image_manager()
    ecr = MagicMock()
    ecr.describe_repositories.return_value = {"repositories": [{"imageTagMutability": "IMMUTABLE"}]}
    ecr.describe_images.return_value = {"imageDetails": []}
    manager._ecr_client = lambda: ecr

    manager._check_tag_immutable_collision("service", "v1")

    ecr.describe_images.assert_called_once()


def test_image_build_rejects_missing_dockerfile_before_runtime_use(tmp_path: Path) -> None:
    manager = _image_manager()
    manager._runtime_or_error = Mock()
    manager.init = Mock()

    with pytest.raises(FileNotFoundError, match="Dockerfile not found"):
        manager.build(str(tmp_path), "service")

    manager._runtime_or_error.assert_not_called()
    manager.init.assert_not_called()


def test_image_init_suppresses_generic_already_exists_client_error() -> None:
    manager = _image_manager()
    ecr = MagicMock()
    ecr.exceptions.RepositoryAlreadyExistsException = type(
        "RepositoryAlreadyExistsException",
        (Exception,),
        {},
    )
    ecr.create_repository.side_effect = _client_error("RepositoryAlreadyExistsException")
    manager._ecr_client = lambda: ecr

    result = manager.init("service")

    assert result["created"] is False
    ecr.put_lifecycle_policy.assert_called_once()


@pytest.mark.parametrize(
    "response_or_error",
    [
        {"lifecyclePolicyText": ""},
        _client_error("LifecyclePolicyNotFoundException"),
    ],
)
def test_image_lifecycle_get_returns_empty_for_absent_policy(response_or_error: object) -> None:
    manager = _image_manager()
    ecr = MagicMock()
    ecr.exceptions.LifecyclePolicyNotFoundException = type(
        "LifecyclePolicyNotFoundException",
        (Exception,),
        {},
    )
    if isinstance(response_or_error, Exception):
        ecr.get_lifecycle_policy.side_effect = response_or_error
    else:
        ecr.get_lifecycle_policy.return_value = response_or_error
    manager._ecr_client = lambda: ecr

    assert manager.lifecycle_get("service") == {}


def test_image_cleanup_skips_empty_repo_and_ignores_nonnumeric_size() -> None:
    manager = _image_manager()
    manager.list_repos = Mock(return_value=[{"name": "gco/empty"}, {"name": "gco/service"}])
    ecr = MagicMock()
    paginator = MagicMock()
    paginator.paginate.side_effect = [
        [{"imageDetails": []}],
        [{"imageDetails": [{"imageDigest": "sha256:one", "imageSizeInBytes": "unknown"}]}],
    ]
    ecr.get_paginator.return_value = paginator
    ecr.batch_delete_image.return_value = {"imageIds": [{"imageDigest": "sha256:one"}]}
    manager._ecr_client = lambda: ecr

    result = manager.cleanup(all=True)

    assert result == {"repos_touched": 1, "tags_deleted": 1, "bytes_freed": 0}
    ecr.batch_delete_image.assert_called_once_with(
        repositoryName="gco/service",
        imageIds=[{"imageDigest": "sha256:one"}],
    )


def test_image_orphans_ignores_tag_rows_without_a_tag() -> None:
    manager = _image_manager()
    manager._collect_inference_image_refs = Mock(return_value=set())
    manager._collect_recent_job_image_refs = Mock(return_value=set())
    manager.list_repos = Mock(return_value=[{"name": "gco/service"}])
    manager.list_tags = Mock(return_value=[{"tag": None}])

    assert manager.orphans() == []


@pytest.mark.parametrize(
    ("module_name", "method_name", "args"),
    [
        ("inference", "_collect_inference_image_refs", ()),
        ("jobs", "_collect_recent_job_image_refs", (30,)),
    ],
)
def test_image_reference_collectors_degrade_when_optional_manager_import_fails(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    method_name: str,
    args: tuple[object, ...],
) -> None:
    manager = _image_manager()
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == module_name and level == 1:
            raise ImportError(f"no {module_name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert getattr(manager, method_name)(*args) == set()


@pytest.mark.parametrize(
    ("mcp_config", "message"),
    [
        ({"mcpServers": {"bad": {"args": []}}}, "has no command"),
        ({"mcpServers": {"bad": {"command": "tool", "args": "bad"}}}, "args must be strings"),
        (
            {"mcpServers": {"bad": {"command": "tool", "args": [], "env": {"COUNT": 1}}}},
            "env must contain strings",
        ),
    ],
)
def test_codex_config_rejects_malformed_mcp_server_entries(
    mcp_config: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        autopilot.build_codex_config_toml(
            mcp_config,
            model="global.openai.gpt-test",
            region="us-east-1",
            reasoning_effort=None,
        )


def test_codex_model_warns_for_non_openai_override() -> None:
    model, warnings = autopilot.resolve_codex_model("global.anthropic.other")

    assert model == "global.anthropic.other"
    assert len(warnings) == 1
    assert "does not look like an OpenAI model" in warnings[0]


def test_small_fast_model_rejects_blank_explicit_value() -> None:
    with pytest.raises(ValueError, match="--small-fast-model must be a non-empty"):
        autopilot.resolve_small_fast_model("   ")


def test_plugin_paths_deduplicate_same_resolved_directory(tmp_path: Path) -> None:
    assert autopilot.resolve_plugin_paths((str(tmp_path), str(tmp_path))) == [tmp_path.resolve()]


def test_codex_project_root_degrades_when_git_cannot_launch(tmp_path: Path) -> None:
    with patch("cli.autopilot.subprocess.run", side_effect=OSError("git missing")):
        assert autopilot.codex_project_root(tmp_path) == tmp_path.resolve()


def test_exec_codex_delegates_to_shared_exec() -> None:
    with patch("cli.autopilot.exec_claude", return_value=23) as execute:
        assert autopilot.exec_codex(["codex"], {"A": "B"}) == 23
    execute.assert_called_once_with(["codex"], {"A": "B"})


def test_describe_stack_outputs_returns_none_for_empty_stack_list() -> None:
    cfn = MagicMock()
    cfn.describe_stacks.return_value = {"Stacks": []}
    with patch("boto3.client", return_value=cfn):
        assert analytics_user_mgmt._describe_stack_outputs("us-east-1", "stack") is None


def test_srp_authenticate_calls_admin_password_flow_and_normalizes_tokens() -> None:
    cognito = MagicMock()
    cognito.admin_initiate_auth.return_value = {
        "AuthenticationResult": {
            "IdToken": "id",
            "AccessToken": "access",
            "RefreshToken": "refresh",
        }
    }
    with patch("boto3.client", return_value=cognito) as client:
        result = analytics_user_mgmt.srp_authenticate(
            "pool",
            "client",
            "alice",
            "Secret!1",
            "us-east-1",
        )

    assert result == {"IdToken": "id", "AccessToken": "access", "RefreshToken": "refresh"}
    client.assert_called_once_with("cognito-idp", region_name="us-east-1")
    cognito.admin_initiate_auth.assert_called_once_with(
        UserPoolId="pool",
        ClientId="client",
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": "alice", "PASSWORD": "Secret!1"},
    )


@pytest.mark.parametrize("api_base", ["http://api.example", "https:///missing-host"])
def test_fetch_studio_url_rejects_non_https_or_hostless_base(api_base: str) -> None:
    with pytest.raises(ValueError):
        analytics_user_mgmt.fetch_studio_url(api_base, "token")


def test_fetch_studio_url_returns_pending_provisioning_state() -> None:
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 202
    response.read.return_value = b'{"status":"provisioning"}'
    response.headers = {"x-amzn-RequestId": "request-1"}

    with patch("urllib.request.urlopen", return_value=response):
        assert analytics_user_mgmt.fetch_studio_url("https://api.example", "token") == (
            "",
            0,
            "request-1",
        )


def test_orphan_scan_skips_resources_without_identifiers() -> None:
    efs = MagicMock()
    efs.describe_file_systems.return_value = {"FileSystems": [{}]}
    cognito = MagicMock()
    cognito.list_user_pools.return_value = {"UserPools": [{}]}

    with patch("boto3.client", side_effect=[efs, cognito]):
        assert analytics_user_mgmt.scan_orphan_analytics_resources("us-east-1") == []

    efs.list_tags_for_resource.assert_not_called()
    cognito.describe_user_pool.assert_not_called()


@pytest.mark.parametrize(
    ("side_effect", "message"),
    [
        (FileNotFoundError("kubectl"), "kubectl not found"),
        (
            SimpleNamespace(returncode=1, stdout="", stderr="forbidden"),
            "Failed to read Secret monitoring/grafana: forbidden",
        ),
    ],
)
def test_grafana_credentials_translate_kubectl_failures(
    side_effect: object,
    message: str,
) -> None:
    if isinstance(side_effect, Exception):
        run = Mock(side_effect=side_effect)
    else:
        run = Mock(return_value=side_effect)
    with (
        patch("cli.monitoring_user_mgmt.subprocess.run", run),
        pytest.raises(RuntimeError, match=message),
    ):
        monitoring_user_mgmt.read_grafana_admin_credentials(secret_name="grafana")


def test_grafana_create_user_omits_absent_email() -> None:
    response = MagicMock()
    response.json.return_value = {"id": 7}
    with patch("cli.monitoring_user_mgmt.requests.post", return_value=response) as post:
        assert (
            monitoring_user_mgmt.create_user(
                "http://grafana",
                ("admin", "secret"),
                login="alice",
                password="Password!1",
            )
            == 7
        )

    assert "email" not in post.call_args.kwargs["json"]


def test_cost_workload_estimation_degrades_when_capacity_package_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = object.__new__(costs.CostTracker)
    tracker._config = SimpleNamespace(project_name="gco")
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "capacity" and level == 1:
            raise ImportError("capacity unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    assert tracker.estimate_running_workloads("us-east-1") == []


def _cost_analytics_with_paginator(pages: list[dict[str, object]]) -> CostAnalytics:
    analytics = object.__new__(CostAnalytics)
    analytics._athena = MagicMock()
    analytics._athena.get_paginator.return_value.paginate.return_value = pages
    return analytics


def test_cost_result_collection_keeps_columns_across_pages_and_data_first_row() -> None:
    analytics = _cost_analytics_with_paginator(
        [
            {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "name"}]},
                    "Rows": [{"Data": [{"VarCharValue": "first"}]}],
                }
            },
            {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "ignored"}]},
                    "Rows": [{"Data": [{"VarCharValue": "second"}]}],
                }
            },
        ]
    )

    result = analytics._collect_results("query")

    assert result.columns == ["name"]
    assert result.rows == [{"name": "first"}, {"name": "second"}]


def test_cost_result_collection_enforces_row_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    analytics = _cost_analytics_with_paginator(
        [
            {
                "ResultSet": {
                    "ResultSetMetadata": {"ColumnInfo": [{"Name": "name"}]},
                    "Rows": [{"Data": [{"VarCharValue": "first"}]}],
                }
            }
        ]
    )
    monkeypatch.setattr(cost_analytics, "_MAX_RESULT_ROWS", 1)

    result = analytics._collect_results("query")

    assert result.rows == [{"name": "first"}]


@pytest.mark.parametrize(("database", "table"), [("bad-name", "costs"), ("gco", "bad table")])
def test_cost_analytics_rejects_unsafe_identifiers(database: str, table: str) -> None:
    analytics = object.__new__(CostAnalytics)
    analytics.database = database
    analytics.table = table

    with pytest.raises(AthenaQueryError, match="Invalid Athena identifier"):
        analytics._qualified_table()


def test_cost_analytics_factory_preserves_config() -> None:
    config = SimpleNamespace(project_name="gco")
    with patch("cli.cost_analytics.CostAnalytics", return_value="analytics") as constructor:
        assert cost_analytics.get_cost_analytics(config) == "analytics"
    constructor.assert_called_once_with(config=config)


def test_model_delete_skips_missing_key_and_deletes_key_without_version() -> None:
    manager = object.__new__(ModelManager)
    manager._get_bucket_name = Mock(return_value="models")
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {
            "Versions": [{"VersionId": "ignored"}, {"Key": "models/llama/file"}],
            "DeleteMarkers": [],
        }
    ]
    s3.delete_objects.return_value = {"Deleted": [{"Key": "models/llama/file"}]}
    manager._get_s3_client = Mock(return_value=s3)

    assert manager.delete_model("llama") == 1
    assert s3.delete_objects.call_args.kwargs["Delete"]["Objects"] == [{"Key": "models/llama/file"}]


def test_regional_bucket_manager_factory_preserves_config() -> None:
    config = SimpleNamespace(project_name="gco")
    with patch("cli.models.RegionalBucketManager", return_value="regional") as constructor:
        assert models.get_regional_bucket_manager(config) == "regional"
    constructor.assert_called_once_with(config)


def test_private_subnet_command_requires_at_least_one_subnet() -> None:
    with pytest.raises(ValueError, match="At least one cluster subnet"):
        ephemeral_bastion.build_describe_private_cluster_subnet_command([], "us-east-1")


def test_bastion_launch_exhausts_profile_propagation_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ephemeral_bastion, "_PROFILE_PROPAGATION_RETRIES", 2)
    monkeypatch.setattr(ephemeral_bastion, "_PROFILE_PROPAGATION_WAIT_SECONDS", 0)
    run = Mock(side_effect=RuntimeError("Invalid IAM Instance Profile"))
    monkeypatch.setattr(ephemeral_bastion, "_run_aws", run)
    monkeypatch.setattr(ephemeral_bastion.time, "sleep", lambda _seconds: None)
    network = SimpleNamespace(subnet_id="subnet-01234567", security_group_id="sg-01234567")

    with pytest.raises(RuntimeError, match="failed after 2 attempts"):
        ephemeral_bastion.launch_bastion(
            network=network,
            ami_id="ami-01234567",
            region="us-east-1",
            ttl_minutes=60,
        )

    assert run.call_count == 2


def test_wait_for_ssm_online_times_out_without_polling() -> None:
    with (
        patch.object(ephemeral_bastion, "_run_aws") as run,
        pytest.raises(RuntimeError, match="did not come Online in SSM within 0s"),
    ):
        ephemeral_bastion.wait_until_ssm_online(
            "i-0123456789abcdef0",
            "us-east-1",
            timeout_seconds=0,
        )
    run.assert_not_called()


def test_podman_nonzero_falls_through_to_no_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _container_runtime.shutil,
        "which",
        lambda name: "/usr/bin/podman" if name == "podman" else None,
    )
    monkeypatch.setattr(
        _container_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )

    assert _container_runtime._detect_container_runtime_uncached() is None


def test_image_mirror_account_id_uses_sts() -> None:
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "123456789012"}
    with patch("cli._image_mirror.boto3.client", return_value=sts) as client:
        assert _image_mirror._account_id() == "123456789012"
    client.assert_called_once_with("sts")


@pytest.mark.parametrize("component", ["a_b", "a__b", "a--b"])
def test_image_reference_accepts_distribution_repository_separators(component: str) -> None:
    reference = f"registry.example/team/{component}@sha256:{_DIGEST}"
    assert immutable_sha256_digest(reference) == _DIGEST


@pytest.mark.parametrize(
    "reference",
    [
        f"registry.example:443/team/image@sha256:{_DIGEST}",
        f"registry.example:1/team/image@sha256:{_DIGEST}",
    ],
)
def test_image_reference_accepts_valid_registry_ports(reference: str) -> None:
    assert immutable_sha256_digest(reference) == _DIGEST


@pytest.mark.parametrize(
    "reference",
    [
        f"registry:443:extra/team/image@sha256:{_DIGEST}",
        f"-bad.example/team/image@sha256:{_DIGEST}",
        f"registry.example/team/Bad@sha256:{_DIGEST}",
    ],
)
def test_image_reference_rejects_malformed_registry_or_repository(reference: str) -> None:
    assert immutable_sha256_digest(reference) is None


@pytest.mark.parametrize(
    ("region", "partition", "suffix", "message"),
    [
        ("", "aws", "amazonaws.com", "must not be empty"),
        ("unknown-1", None, "amazonaws.com", "Could not resolve an AWS partition"),
        ("us-test-1", "aws-test", None, "Could not resolve the URL suffix"),
    ],
)
def test_partition_metadata_rejects_incomplete_resolution(
    region: str,
    partition: str | None,
    suffix: str | None,
    message: str,
) -> None:
    resolver = MagicMock()
    resolver.get_partition_for_region.return_value = partition
    resolver.get_partition_dns_suffix.return_value = suffix
    session = MagicMock()
    session.get_component.return_value = resolver
    _image_uri._partition_metadata.cache_clear()

    with (
        patch("cli._image_uri.botocore.session.get_session", return_value=session),
        pytest.raises(ValueError, match=message),
    ):
        _image_uri._partition_metadata(region)


def test_grafana_credentials_reject_invalid_secret_name() -> None:
    with pytest.raises(ValueError, match="Invalid secret name"):
        monitoring_user_mgmt.read_grafana_admin_credentials(secret_name="Bad Secret")


def test_wait_for_ssm_online_sleeps_once_then_times_out() -> None:
    clock = iter((0.0, 0.0, 1.0))
    with (
        patch.object(ephemeral_bastion.time, "monotonic", side_effect=lambda: next(clock)),
        patch.object(ephemeral_bastion.time, "sleep") as sleep,
        patch.object(ephemeral_bastion, "_run_aws", return_value="Pending") as run,
        pytest.raises(RuntimeError, match="did not come Online in SSM"),
    ):
        ephemeral_bastion.wait_until_ssm_online(
            "i-0123456789abcdef0",
            "us-east-1",
            timeout_seconds=0.5,
            poll_interval_seconds=0.1,
        )

    run.assert_called_once()
    sleep.assert_called_once_with(0.1)


@pytest.mark.parametrize(
    "reference",
    [
        f"registry.example/team/image:@sha256:{_DIGEST}",
        f"registry.example//image@sha256:{_DIGEST}",
    ],
)
def test_image_reference_rejects_invalid_tag_or_empty_path_segment(reference: str) -> None:
    assert immutable_sha256_digest(reference) is None
