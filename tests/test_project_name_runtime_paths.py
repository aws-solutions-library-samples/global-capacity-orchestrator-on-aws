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
            patch.dict(os.environ, {"TARGET_REGIONS": '["us-west-2"]'}, clear=True),
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
