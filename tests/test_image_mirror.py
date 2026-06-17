"""
Tests for the general image-mirror core (cli/_image_mirror.py) and the
``gco images mirror`` CLI command.

The mirror copies third-party images into the project's ``gco/*`` ECR so the
cluster pulls them from same-account ECR instead of rate-limited upstreams
(chiefly docker.io / Volcano). These tests cover the pure logic (which images
are collected, source-ref parsing, the copy plan that must line up with the
consumer's ``image_registry`` override) and the side-effect helpers (multi-arch
copy strategy, repo creation, skip-if-already-mirrored), with subprocess and the
ECR client mocked — no Docker daemon or AWS calls.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli import _image_mirror as mirror


def _sample_charts(**basic_overrides):
    """A minimal charts.yaml-shaped config with a volcano chart block."""
    basic = {
        "controller_image_name": "volcanosh/vc-controller-manager",
        "scheduler_image_name": "volcanosh/vc-scheduler",
        "admission_image_name": "volcanosh/vc-webhook-manager",
        "image_tag_version": "v1.15.0",
    }
    basic.update(basic_overrides)
    return {"charts": {"volcano": {"values": {"basic": basic}}}}


class TestVolcanoSourceRefs:
    def test_returns_three_docker_io_refs(self):
        refs = mirror._volcano_source_refs(_sample_charts())
        assert refs == [
            "docker.io/volcanosh/vc-controller-manager:v1.15.0",
            "docker.io/volcanosh/vc-scheduler:v1.15.0",
            "docker.io/volcanosh/vc-webhook-manager:v1.15.0",
        ]

    def test_per_component_tag_override_wins(self):
        refs = mirror._volcano_source_refs(_sample_charts(scheduler_image_tag_version="v1.15.1"))
        assert "docker.io/volcanosh/vc-scheduler:v1.15.1" in refs
        assert "docker.io/volcanosh/vc-controller-manager:v1.15.0" in refs

    def test_missing_tag_raises(self):
        cfg = _sample_charts()
        del cfg["charts"]["volcano"]["values"]["basic"]["image_tag_version"]
        with pytest.raises(ValueError, match="image_tag_version"):
            mirror._volcano_source_refs(cfg)

    def test_no_image_names_raises(self):
        cfg = {"charts": {"volcano": {"values": {"basic": {"image_tag_version": "v1.15.0"}}}}}
        with pytest.raises(ValueError, match="image names"):
            mirror._volcano_source_refs(cfg)


class TestCollectSourceRefs:
    def test_includes_volcano_refs(self):
        refs = mirror.collect_source_refs(_sample_charts())
        assert "docker.io/volcanosh/vc-scheduler:v1.15.0" in refs
        # Currently Volcano is the only registered source (3 images).
        assert len(refs) == 3


class TestParseSourceRef:
    def test_splits_registry_repo_tag(self):
        assert mirror.parse_source_ref("docker.io/volcanosh/vc-scheduler:v1.15.0") == (
            "volcanosh/vc-scheduler",
            "v1.15.0",
        )

    def test_quay_ref(self):
        assert mirror.parse_source_ref("quay.io/org/img:1.2.3") == ("org/img", "1.2.3")

    def test_missing_tag_raises(self):
        with pytest.raises(ValueError, match="tag"):
            mirror.parse_source_ref("docker.io/org/img")

    def test_missing_registry_raises(self):
        with pytest.raises(ValueError, match="registry"):
            mirror.parse_source_ref("img:1.0")


class TestPlanFromSources:
    def test_plan_matches_image_registry_override_layout(self):
        registry_host = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
        refs = ["docker.io/volcanosh/vc-scheduler:v1.15.0"]
        plan = mirror.plan_from_sources(refs, registry_host, "gco/dockerhub")

        assert len(plan) == 1
        item = plan[0]
        assert item.source_ref == "docker.io/volcanosh/vc-scheduler:v1.15.0"
        # Dest repo lives under the gco/* namespace, preserving the upstream path.
        assert item.dest_repo == "gco/dockerhub/volcanosh/vc-scheduler"
        # Dest ref is exactly <image_registry-override>/<repo>:<tag>.
        assert item.dest_ref == (
            "123456789012.dkr.ecr.us-east-1.amazonaws.com"
            "/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0"
        )
        assert item.tag == "v1.15.0"

    def test_namespace_slashes_normalized(self):
        plan = mirror.plan_from_sources(
            ["docker.io/volcanosh/vc-scheduler:v1.15.0"],
            "reg",
            "/gco/dockerhub/",
        )
        assert plan[0].dest_repo == "gco/dockerhub/volcanosh/vc-scheduler"


class TestCopyStrategy:
    def test_prefers_buildx_when_available(self):
        with patch.object(mirror, "_runtime_has_buildx", return_value=True):
            assert mirror.resolve_copy_strategy("docker") == "buildx"

    def test_falls_back_to_all_platforms(self):
        with (
            patch.object(mirror, "_runtime_has_buildx", return_value=False),
            patch.object(mirror, "_runtime_supports_all_platforms", return_value=True),
        ):
            assert mirror.resolve_copy_strategy("finch") == "all-platforms"

    def test_falls_back_to_skopeo(self):
        with (
            patch.object(mirror, "_runtime_has_buildx", return_value=False),
            patch.object(mirror, "_runtime_supports_all_platforms", return_value=False),
            patch.object(mirror.shutil, "which", return_value="/usr/bin/skopeo"),
        ):
            assert mirror.resolve_copy_strategy("podman") == "skopeo"

    def test_raises_when_nothing_available(self):
        with (
            patch.object(mirror, "_runtime_has_buildx", return_value=False),
            patch.object(mirror, "_runtime_supports_all_platforms", return_value=False),
            patch.object(mirror.shutil, "which", return_value=None),
            pytest.raises(RuntimeError, match="multi-arch image-copy"),
        ):
            mirror.resolve_copy_strategy("docker")

    def _item(self):
        return mirror.MirrorItem(
            source_ref="docker.io/volcanosh/vc-scheduler:v1.15.0",
            dest_repo="gco/dockerhub/volcanosh/vc-scheduler",
            dest_ref="reg/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0",
        )

    def test_all_platforms_commands_preserve_arch(self):
        cmds = mirror._copy_commands(self._item(), "finch", "all-platforms", "")
        assert cmds == [
            ["finch", "pull", "--all-platforms", "docker.io/volcanosh/vc-scheduler:v1.15.0"],
            [
                "finch",
                "tag",
                "docker.io/volcanosh/vc-scheduler:v1.15.0",
                "reg/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0",
            ],
            [
                "finch",
                "push",
                "--all-platforms",
                "reg/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0",
            ],
        ]

    def test_skopeo_command_uses_copy_all(self):
        cmds = mirror._copy_commands(self._item(), "docker", "skopeo", "ECRTOKEN")
        argv = cmds[0]
        assert argv[:3] == ["skopeo", "copy", "--all"]
        assert argv[argv.index("--dest-creds") + 1] == "AWS:ECRTOKEN"
        assert argv[-2] == "docker://docker.io/volcanosh/vc-scheduler:v1.15.0"
        assert argv[-1] == "docker://reg/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0"

    def test_buildx_command_uses_imagetools_create(self):
        cmds = mirror._copy_commands(self._item(), "docker", "buildx", "")
        assert cmds[0][:4] == ["docker", "buildx", "imagetools", "create"]


class TestCopyImage:
    def _item(self):
        return mirror.MirrorItem(
            source_ref="docker.io/volcanosh/vc-scheduler:v1.15.0",
            dest_repo="gco/dockerhub/volcanosh/vc-scheduler",
            dest_ref="reg/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0",
        )

    def test_buildx_invocation(self):
        with patch.object(mirror.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mirror.copy_image(self._item(), runtime="docker", strategy="buildx")
        argv = mock_run.call_args[0][0]
        assert argv[:4] == ["docker", "buildx", "imagetools", "create"]
        assert "pull" not in argv and "push" not in argv

    def test_failure_raises(self):
        with patch.object(mirror.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="manifest unknown")
            with pytest.raises(RuntimeError, match="image copy failed"):
                mirror.copy_image(self._item(), runtime="docker", strategy="buildx")


class _FakeEcr:
    """MagicMock ECR client with the exception classes the code branches on."""

    def __init__(self):
        self.client = MagicMock()

        class RepositoryAlreadyExistsException(Exception):
            pass

        class ImageNotFoundException(Exception):
            pass

        class RepositoryNotFoundException(Exception):
            pass

        self.client.exceptions.RepositoryAlreadyExistsException = RepositoryAlreadyExistsException
        self.client.exceptions.ImageNotFoundException = ImageNotFoundException
        self.client.exceptions.RepositoryNotFoundException = RepositoryNotFoundException


class TestEnsureRepository:
    def test_creates_when_missing(self):
        ecr = _FakeEcr().client
        mirror.ensure_repository(ecr, "gco/dockerhub/volcanosh/vc-scheduler")
        ecr.create_repository.assert_called_once_with(
            repositoryName="gco/dockerhub/volcanosh/vc-scheduler"
        )

    def test_idempotent_when_exists(self):
        ecr = _FakeEcr().client
        ecr.create_repository.side_effect = ecr.exceptions.RepositoryAlreadyExistsException()
        mirror.ensure_repository(ecr, "gco/dockerhub/volcanosh/vc-scheduler")  # no raise


class TestTagExists:
    def test_true_when_image_present(self):
        ecr = _FakeEcr().client
        ecr.describe_images.return_value = {"imageDetails": [{"imageTag": "v1.15.0"}]}
        assert mirror.tag_exists(ecr, "gco/dockerhub/volcanosh/vc-scheduler", "v1.15.0") is True

    def test_false_when_image_missing(self):
        ecr = _FakeEcr().client
        ecr.describe_images.side_effect = ecr.exceptions.ImageNotFoundException()
        assert mirror.tag_exists(ecr, "gco/dockerhub/volcanosh/vc-scheduler", "v1.15.0") is False

    def test_false_when_repo_missing(self):
        ecr = _FakeEcr().client
        ecr.describe_images.side_effect = ecr.exceptions.RepositoryNotFoundException()
        assert mirror.tag_exists(ecr, "gco/dockerhub/volcanosh/vc-scheduler", "v1.15.0") is False


class TestMirrorCliDryRun:
    def test_dry_run_makes_no_aws_or_docker_calls(self):
        """`gco images mirror --dry-run` prints the plan with no side effects."""
        from click.testing import CliRunner

        from cli.commands.images_cmd import images

        with (
            patch.object(mirror, "_account_id") as mock_acct,
            patch.object(mirror.subprocess, "run") as mock_run,
            patch.object(mirror.boto3, "client") as mock_client,
        ):
            result = CliRunner().invoke(images, ["mirror", "--region", "us-east-1", "--dry-run"])
        assert result.exit_code == 0, result.output
        mock_acct.assert_not_called()
        mock_run.assert_not_called()
        mock_client.assert_not_called()


class TestPlanMirror:
    """plan_mirror resolves the destination registry/repos without side effects."""

    def test_resolves_registry_and_images(self):
        with (
            patch.object(mirror, "_account_id", return_value="123456789012"),
            patch.object(
                mirror,
                "collect_source_refs",
                return_value=["docker.io/volcanosh/vc-scheduler:v1.15.0"],
            ),
        ):
            plan = mirror.plan_mirror("us-east-1", "gco/dockerhub")
        assert plan["region"] == "us-east-1"
        assert plan["registry"] == "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/dockerhub"
        assert plan["ecr_namespace"] == "gco/dockerhub"
        assert plan["images"] == [
            {
                "source_ref": "docker.io/volcanosh/vc-scheduler:v1.15.0",
                "dest_repo": "gco/dockerhub/volcanosh/vc-scheduler",
                "dest_ref": (
                    "123456789012.dkr.ecr.us-east-1.amazonaws.com"
                    "/gco/dockerhub/volcanosh/vc-scheduler:v1.15.0"
                ),
                "tag": "v1.15.0",
            }
        ]

    def test_defaults_namespace_from_cdk(self):
        with (
            patch.object(mirror, "_account_id", return_value="123456789012"),
            patch.object(mirror, "cdk_default_namespace", return_value="gco/dockerhub"),
            patch.object(
                mirror,
                "collect_source_refs",
                return_value=["docker.io/volcanosh/vc-scheduler:v1.15.0"],
            ),
        ):
            plan = mirror.plan_mirror("us-east-1")
        assert plan["ecr_namespace"] == "gco/dockerhub"
        assert plan["images"][0]["dest_repo"] == "gco/dockerhub/volcanosh/vc-scheduler"

    def test_makes_no_ecr_or_copy_calls(self):
        """plan_mirror resolves the account (STS) but never touches ECR or a runtime."""
        with (
            patch.object(mirror, "_account_id", return_value="123456789012"),
            patch.object(
                mirror,
                "collect_source_refs",
                return_value=["docker.io/volcanosh/vc-scheduler:v1.15.0"],
            ),
            patch.object(mirror.boto3, "client") as mock_client,
            patch.object(mirror.subprocess, "run") as mock_run,
        ):
            mirror.plan_mirror("us-east-1", "gco/dockerhub")
        mock_client.assert_not_called()
        mock_run.assert_not_called()


class TestMirrorStatus:
    """mirror_status reports per-image presence via ECR describe (read-only)."""

    def test_reports_mirrored_and_missing(self):
        refs = [
            "docker.io/volcanosh/vc-scheduler:v1.15.0",
            "docker.io/volcanosh/vc-controller-manager:v1.15.0",
        ]
        fake = _FakeEcr()

        def _describe(repositoryName, imageIds):
            if "vc-scheduler" in repositoryName:
                return {"imageDetails": [{"imageTag": "v1.15.0"}]}
            raise fake.client.exceptions.ImageNotFoundException()

        fake.client.describe_images.side_effect = _describe
        with (
            patch.object(mirror, "_account_id", return_value="123456789012"),
            patch.object(mirror.boto3, "client", return_value=fake.client),
        ):
            status = mirror.mirror_status("us-east-1", "gco/dockerhub", source_refs=refs)

        by_repo = {img["dest_repo"]: img["mirrored"] for img in status["images"]}
        assert by_repo["gco/dockerhub/volcanosh/vc-scheduler"] is True
        assert by_repo["gco/dockerhub/volcanosh/vc-controller-manager"] is False
        assert status["all_mirrored"] is False
        assert status["missing"] == [
            "123456789012.dkr.ecr.us-east-1.amazonaws.com"
            "/gco/dockerhub/volcanosh/vc-controller-manager:v1.15.0"
        ]

    def test_all_mirrored_true_when_every_tag_present(self):
        refs = ["docker.io/volcanosh/vc-scheduler:v1.15.0"]
        fake = _FakeEcr()
        fake.client.describe_images.return_value = {"imageDetails": [{"imageTag": "v1.15.0"}]}
        with (
            patch.object(mirror, "_account_id", return_value="123456789012"),
            patch.object(mirror.boto3, "client", return_value=fake.client),
        ):
            status = mirror.mirror_status("us-east-1", "gco/dockerhub", source_refs=refs)
        assert status["all_mirrored"] is True
        assert status["missing"] == []
