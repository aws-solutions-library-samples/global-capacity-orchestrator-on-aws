"""Runtime SSM path scoping (issue #139).

The CDK stacks scope every resource name to ``project_name``. The runtime
services and Lambdas that read/write SSM Parameter Store at run time must build
the SAME project-scoped paths — otherwise a second deployment in the same
account+region would read or overwrite the first deployment's parameters even
though the stacks themselves never collide.

These are plain unit tests (no CDK synth) that lock in the project-scoping of
every runtime SSM path builder, and — for the regional-shared-bucket namespace,
which is intentionally duplicated between the CDK ``constants`` module and the
inference monitor — that the two stay in lockstep. They run in the normal
``unit:pytest:core`` job.

Covered runtime writers/readers:
  * ``gco.services.inference_monitor`` — regional-shared-bucket discovery prefix.
  * ``lambda/ga-registration`` — writes/deletes ``/<project>/alb-hostname-<region>``.
  * ``lambda/cross-region-aggregator`` — discovers deterministic
    ``<project>-regional-api-<region>`` stacks and their
    ``RegionalApiEndpoint`` CloudFormation outputs.
  * ``lambda/kubectl-applier-simple`` + ``lambda/helm-installer`` — write
    ``/<project>/addons/<region>/<phase|chart>`` add-on status.

Also covers the CLI-side ECR image namespace, which is account+region-unique and
must be project-scoped so two deployments don't share an image registry (#139):
  * ``cli.images.ImageManager`` — repos live under ``<project>/``.
  * ``cli._image_mirror.read_mirror_config`` — mirror namespace defaults to
    ``<project>/dockerhub``.

And the inference endpoints table the CLI reads (moto): ``gco inference`` and
``gco images orphans`` use ``<project>-inference-endpoints`` in the global
Region of cdk.json (or ``GCO_PROJECT_NAME`` / ``GCO_GLOBAL_REGION``), not the
``gco`` deployment's table.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from tests._lambda_imports import load_lambda_module

# project_name permutations: default, plain, and hyphenated.
_PROJECTS = ["gco", "acme", "gco-staging"]


class TestInferenceMonitorSsmPrefix:
    """The inference monitor resolves the regional-shared-bucket SSM namespace
    from ``PROJECT_NAME`` and must agree with the CDK-side constant."""

    @pytest.mark.parametrize("project", _PROJECTS)
    def test_prefix_is_project_scoped_and_matches_constants(self, project: str) -> None:
        from gco.services import inference_monitor
        from gco.stacks.constants import regional_shared_ssm_parameter_prefix

        with patch.dict(os.environ, {"PROJECT_NAME": project}):
            resolved = inference_monitor._regional_shared_ssm_parameter_prefix()

        assert resolved == f"/{project}/regional-shared-bucket"
        # Drift guard: the monitor keeps a local copy of this namespace (no CDK
        # imports at runtime), so it must render identically to the CDK constant
        # the regional stack writes with.
        assert resolved == regional_shared_ssm_parameter_prefix(project)

    def test_prefix_defaults_to_gco(self) -> None:
        from gco.services import inference_monitor

        with patch.dict(os.environ):
            os.environ.pop("PROJECT_NAME", None)
            assert (
                inference_monitor._regional_shared_ssm_parameter_prefix()
                == "/gco/regional-shared-bucket"
            )


class TestGaRegistrationAlbHostnamePath:
    """The GA-registration Lambda stores/deletes the ALB hostname under a
    project-scoped SSM path in the global region."""

    @pytest.mark.parametrize("project", _PROJECTS)
    def test_store_uses_project_scoped_path(self, project: str) -> None:
        handler = load_lambda_module("ga-registration")
        mock_ssm = MagicMock()
        with patch("boto3.client", return_value=mock_ssm):
            handler.store_alb_hostname_in_ssm("us-east-1", "alb.example.com", "us-east-2", project)
        assert (
            mock_ssm.put_parameter.call_args.kwargs["Name"] == f"/{project}/alb-hostname-us-east-1"
        )

    @pytest.mark.parametrize("project", _PROJECTS)
    def test_delete_uses_project_scoped_path(self, project: str) -> None:
        handler = load_lambda_module("ga-registration")
        mock_ssm = MagicMock()
        with patch("boto3.client", return_value=mock_ssm):
            handler.delete_alb_hostname_from_ssm("us-west-2", "us-east-2", project)
        assert (
            mock_ssm.delete_parameter.call_args.kwargs["Name"]
            == f"/{project}/alb-hostname-us-west-2"
        )


class TestAggregatorDiscoveryStackName:
    """The aggregator scopes deterministic regional API stack discovery to
    ``PROJECT_NAME`` and consumes only the expected CloudFormation output."""

    @pytest.mark.parametrize("project", _PROJECTS)
    def test_discovery_uses_project_scoped_stack_name(self, project: str) -> None:
        handler = load_lambda_module("cross-region-aggregator")
        handler._cached_endpoints = None
        handler._endpoints_cache_time = 0
        mock_cloudformation = MagicMock()
        mock_cloudformation.describe_stacks.return_value = {
            "Stacks": [
                {
                    "Outputs": [
                        {
                            "OutputKey": "RegionalApiEndpoint",
                            "OutputValue": (
                                "https://abc123.execute-api.us-east-1.amazonaws.com/prod/"
                            ),
                        }
                    ]
                }
            ]
        }
        with (
            patch.dict(
                os.environ,
                {
                    "PROJECT_NAME": project,
                    "TARGET_REGIONS": '["us-east-1"]',
                    "AWS_URL_SUFFIX": "amazonaws.com",
                },
            ),
            patch("boto3.client", return_value=mock_cloudformation) as mock_client,
        ):
            endpoints = handler.get_regional_endpoints()

        mock_client.assert_called_once_with("cloudformation", region_name="us-east-1")
        mock_cloudformation.describe_stacks.assert_called_once_with(
            StackName=f"{project}-regional-api-us-east-1"
        )
        assert endpoints == {"us-east-1": "https://abc123.execute-api.us-east-1.amazonaws.com/prod"}

    def test_discovery_defaults_to_gco(self) -> None:
        handler = load_lambda_module("cross-region-aggregator")
        handler._cached_endpoints = None
        handler._endpoints_cache_time = 0
        mock_cloudformation = MagicMock()
        mock_cloudformation.describe_stacks.return_value = {
            "Stacks": [
                {
                    "Outputs": [
                        {
                            "OutputKey": "RegionalApiEndpoint",
                            "OutputValue": (
                                "https://abc123.execute-api.us-west-2.amazonaws.com/prod"
                            ),
                        }
                    ]
                }
            ]
        }
        with (
            patch.dict(
                os.environ,
                {
                    "TARGET_REGIONS": '["us-west-2"]',
                    "AWS_URL_SUFFIX": "amazonaws.com",
                },
                clear=True,
            ),
            patch("boto3.client", return_value=mock_cloudformation),
        ):
            endpoints = handler.get_regional_endpoints()

        mock_cloudformation.describe_stacks.assert_called_once_with(
            StackName="gco-regional-api-us-west-2"
        )
        assert endpoints == {"us-west-2": "https://abc123.execute-api.us-west-2.amazonaws.com/prod"}


class TestAddonStatusPaths:
    """The kubectl-applier and helm-installer Lambdas record add-on status under
    a project-scoped ``/<project>/addons/<region>/...`` path (from env vars)."""

    @pytest.mark.parametrize("project", _PROJECTS)
    def test_kubectl_applier_phase_status_path(self, project: str) -> None:
        handler = load_lambda_module("kubectl-applier-simple")
        mock_ssm = MagicMock()
        with (
            patch.dict(os.environ, {"PROJECT_NAME": project, "REGION": "us-east-1"}),
            patch("boto3.client", return_value=mock_ssm),
        ):
            handler._record_phase_status("base-manifests", "success", "ok")
        assert (
            mock_ssm.put_parameter.call_args.kwargs["Name"]
            == f"/{project}/addons/us-east-1/base-manifests"
        )

    @pytest.mark.parametrize("project", _PROJECTS)
    def test_helm_installer_addon_status_path(self, project: str) -> None:
        handler = load_lambda_module("helm-installer")
        mock_ssm = MagicMock()
        with (
            patch.dict(os.environ, {"PROJECT_NAME": project, "REGION": "eu-west-1"}),
            patch("boto3.client", return_value=mock_ssm),
        ):
            handler._record_addon_status("aws-load-balancer-controller", "success", "ok")
        assert (
            mock_ssm.put_parameter.call_args.kwargs["Name"]
            == f"/{project}/addons/eu-west-1/aws-load-balancer-controller"
        )

    def test_addon_status_noop_without_project_name(self) -> None:
        # With PROJECT_NAME unset the writers must be a no-op, never falling back
        # to an unscoped path that could clash with another deployment.
        handler = load_lambda_module("kubectl-applier-simple")
        mock_ssm = MagicMock()
        with (
            patch.dict(os.environ, {"REGION": "us-east-1"}),
            patch("boto3.client", return_value=mock_ssm),
        ):
            os.environ.pop("PROJECT_NAME", None)
            handler._record_phase_status("base-manifests", "success", "ok")
        mock_ssm.put_parameter.assert_not_called()


class TestImageRepoNamespace:
    """ECR repos and the Volcano mirror namespace live under the project prefix."""

    @pytest.mark.parametrize("project", _PROJECTS)
    def test_image_manager_repo_prefix_is_project(self, project: str) -> None:
        from cli.images import ImageManager

        config = MagicMock()
        config.project_name = project
        # region passed explicitly so construction makes no AWS/STS calls.
        manager = ImageManager(config=config, region="us-east-2")
        assert manager._repo_prefix == project

    def test_image_manager_repo_prefix_defaults_to_gco(self) -> None:
        from cli.config import GCOConfig
        from cli.images import ImageManager

        config = GCOConfig()  # stock defaults -> project_name == "gco"
        assert ImageManager(config=config, region="us-east-2")._repo_prefix == "gco"

    @pytest.mark.parametrize("project", _PROJECTS)
    def test_mirror_default_namespace_is_project_scoped(self, project: str, tmp_path) -> None:
        import json

        from cli import _image_mirror

        cdk_json = tmp_path / "cdk.json"
        cdk_json.write_text(json.dumps({"context": {"project_name": project}}))
        config = _image_mirror.read_mirror_config(cdk_json)
        assert config["ecr_namespace"] == f"{project}/dockerhub"


_ACME_CDK = {
    "app": "python3 app.py",
    "context": {
        "project_name": "acme",
        "deployment_regions": {
            "global": "us-east-2",
            "api_gateway": "us-east-2",
            "monitoring": "us-east-2",
            "regional": ["us-west-2"],
        },
    },
}


def _inference_table(project: str, region: str, *endpoints: str) -> None:
    """Create ``<project>-inference-endpoints`` the way the global stack does."""
    import boto3

    from gco.services.inference_store import InferenceEndpointStore

    boto3.client("dynamodb", region_name=region).create_table(
        TableName=f"{project}-inference-endpoints",
        KeySchema=[{"AttributeName": "endpoint_name", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "endpoint_name", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    store = InferenceEndpointStore(table_name=f"{project}-inference-endpoints", region=region)
    for name in endpoints:
        store.create_endpoint(name, {"image": f"{name}:v1", "replicas": 1}, ["us-west-2"])


class TestInferenceEndpointsTable:
    """``gco inference`` and ``gco images orphans`` read the configured project's table.

    The global stack creates ``<project_name>-inference-endpoints`` in
    ``deployment_regions.global``. moto stands in for an ``acme`` deployment
    (global stack in us-east-2, operator default Region us-west-2) that shares
    its account with a ``gco`` deployment. The CLI used to build the store
    with no table name, so it read ``gco-inference-endpoints`` (the other
    deployment's endpoints) whatever cdk.json named the project.
    """

    @pytest.fixture
    def acme_root(self, tmp_path, monkeypatch):
        import json

        from moto import mock_aws

        root = tmp_path / "acme"
        root.mkdir()
        (root / "cdk.json").write_text(json.dumps(_ACME_CDK), encoding="utf-8")
        for name, value in {
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_SESSION_TOKEN": "testing",
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_REGION": "us-west-2",
        }.items():
            monkeypatch.setenv(name, value)
        for name in (
            "GCO_PROJECT_NAME",
            "GCO_GLOBAL_REGION",
            "DYNAMODB_REGION",
            "GLOBAL_REGION",
            "REGION",
            "INFERENCE_ENDPOINTS_TABLE_NAME",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.chdir(root)
        monkeypatch.setenv("HOME", str(root))  # no ~/.gco/config.yaml of the host
        with mock_aws():
            _inference_table("gco", "us-east-2", "gco-llm")
            _inference_table("acme", "us-east-2", "acme-llm")
            yield root

    @staticmethod
    def _gco(*args: str):
        from click.testing import CliRunner

        from cli.main import cli

        return CliRunner().invoke(cli, ["--output", "json", *args])

    def test_list_reads_the_cdk_json_projects_table(self, acme_root) -> None:
        import json

        result = self._gco("inference", "list")

        assert result.exit_code == 0, result.output
        assert [ep["endpoint_name"] for ep in json.loads(result.stdout)] == ["acme-llm"]

    def test_status_and_writes_use_the_projects_table(self, acme_root) -> None:
        import json

        import boto3

        assert self._gco("inference", "status", "gco-llm").exit_code == 1

        status = self._gco("inference", "status", "acme-llm")
        assert status.exit_code == 0, status.output
        assert json.loads(status.stdout)["endpoint_name"] == "acme-llm"

        scaled = self._gco("inference", "scale", "acme-llm", "--replicas", "3")
        assert scaled.exit_code == 0, scaled.output
        table = boto3.resource("dynamodb", region_name="us-east-2").Table(
            "acme-inference-endpoints"
        )
        assert table.get_item(Key={"endpoint_name": "acme-llm"})["Item"]["spec"]["replicas"] == 3

    def test_a_project_without_the_gco_table_still_reads_its_own(self, acme_root) -> None:
        import json

        import boto3

        boto3.client("dynamodb", region_name="us-east-2").delete_table(
            TableName="gco-inference-endpoints"
        )

        result = self._gco("inference", "list")

        assert result.exit_code == 0, result.output
        assert [ep["endpoint_name"] for ep in json.loads(result.stdout)] == ["acme-llm"]

    def test_images_orphans_collects_the_projects_endpoint_images(self, acme_root) -> None:
        from cli.config import get_config
        from cli.images import ImageManager

        manager = ImageManager(config=get_config(), region="us-east-2")

        assert manager._collect_inference_image_refs() == {"acme-llm:v1"}

    def test_cli_environment_overrides_reach_the_store(self, acme_root, monkeypatch) -> None:
        import json

        _inference_table("beta", "eu-west-1", "beta-llm")
        monkeypatch.setenv("GCO_PROJECT_NAME", "beta")
        monkeypatch.setenv("GCO_GLOBAL_REGION", "eu-west-1")

        result = self._gco("inference", "list")

        assert result.exit_code == 0, result.output
        assert [ep["endpoint_name"] for ep in json.loads(result.stdout)] == ["beta-llm"]

    def test_the_services_table_variable_still_names_the_table(self, acme_root, monkeypatch):
        import json

        # INFERENCE_ENDPOINTS_TABLE_NAME is what the in-cluster services are
        # given; a shell that sets it keeps reading the table it names.
        monkeypatch.setenv("INFERENCE_ENDPOINTS_TABLE_NAME", "gco-inference-endpoints")

        result = self._gco("inference", "list")

        assert result.exit_code == 0, result.output
        assert [ep["endpoint_name"] for ep in json.loads(result.stdout)] == ["gco-llm"]
