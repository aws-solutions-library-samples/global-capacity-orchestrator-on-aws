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

import base64
import json
from pathlib import Path
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
    _REPOSITORY = "gco/dockerhub/volcanosh/vc-scheduler"
    _RUN_TAGS = {
        "GcoLiveValidationRun": "run-123",
        "gco:project": "gco-live",
    }

    def test_creates_and_tags_atomically_when_missing(self):
        ecr = _FakeEcr().client

        created = mirror.ensure_repository(
            ecr,
            self._REPOSITORY,
            repository_tags=self._RUN_TAGS,
        )

        assert created is True
        ecr.create_repository.assert_called_once_with(
            repositoryName=self._REPOSITORY,
            tags=[
                {"Key": "GcoLiveValidationRun", "Value": "run-123"},
                {"Key": "gco:project", "Value": "gco-live"},
            ],
        )
        ecr.tag_resource.assert_not_called()

    def test_creation_callback_receives_exact_acknowledgement_synchronously(self):
        ecr = _FakeEcr().client
        repository = {
            "repositoryName": self._REPOSITORY,
            "repositoryArn": ("arn:aws:ecr:us-east-1:123456789012:repository/" + self._REPOSITORY),
            "registryId": "123456789012",
            "createdAt": "2026-07-17T00:00:00+00:00",
        }
        ecr.create_repository.return_value = {"repository": repository}
        observed = []

        assert (
            mirror.ensure_repository(
                ecr,
                self._REPOSITORY,
                repository_tags=self._RUN_TAGS,
                on_created=lambda acknowledgement: observed.append(acknowledgement),
            )
            is True
        )

        assert observed == [repository]

    def test_creation_callback_fails_closed_without_acknowledgement(self):
        ecr = _FakeEcr().client
        ecr.create_repository.return_value = {}

        with pytest.raises(RuntimeError, match="omitted its acknowledgement"):
            mirror.ensure_repository(
                ecr,
                self._REPOSITORY,
                repository_tags=self._RUN_TAGS,
                on_created=MagicMock(),
            )

    def test_existing_repository_is_never_tagged_or_adopted(self):
        ecr = _FakeEcr().client
        ecr.create_repository.side_effect = ecr.exceptions.RepositoryAlreadyExistsException()

        created = mirror.ensure_repository(
            ecr,
            self._REPOSITORY,
            repository_tags=self._RUN_TAGS,
        )

        assert created is False
        ecr.create_repository.assert_called_once_with(
            repositoryName=self._REPOSITORY,
            tags=[
                {"Key": "GcoLiveValidationRun", "Value": "run-123"},
                {"Key": "gco:project", "Value": "gco-live"},
            ],
        )
        ecr.tag_resource.assert_not_called()


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


class TestConfigurationInputs:
    def test_collect_source_refs_loads_default_charts_config(self):
        charts = _sample_charts()
        with patch.object(mirror, "load_charts_config", return_value=charts) as load:
            refs = mirror.collect_source_refs()

        load.assert_called_once_with()
        assert refs == [
            "docker.io/volcanosh/vc-controller-manager:v1.15.0",
            "docker.io/volcanosh/vc-scheduler:v1.15.0",
            "docker.io/volcanosh/vc-webhook-manager:v1.15.0",
        ]

    def test_load_charts_config_treats_empty_document_as_empty_mapping(self, tmp_path: Path):
        charts_path = tmp_path / "charts.yaml"
        charts_path.write_text("", encoding="utf-8")

        assert mirror.load_charts_config(charts_path) == {}

    def test_load_charts_config_rejects_nonmapping_document(self, tmp_path: Path):
        charts_path = tmp_path / "charts.yaml"
        charts_path.write_text("- volcano\n", encoding="utf-8")

        with pytest.raises(ValueError, match="did not parse to a mapping"):
            mirror.load_charts_config(charts_path)

    @pytest.mark.parametrize(
        "source_ref",
        ["docker.io/:v1.0", "docker.io/example/image:"],
    )
    def test_parse_source_ref_rejects_empty_repository_or_tag(self, source_ref: str):
        with pytest.raises(ValueError, match="could not parse source ref"):
            mirror.parse_source_ref(source_ref)

    def test_read_mirror_config_fails_closed_when_file_is_missing(self, tmp_path: Path):
        config = mirror.read_mirror_config(tmp_path / "missing.json")

        assert config == {"enabled": False, "ecr_namespace": "gco/dockerhub"}

    def test_read_mirror_config_fails_closed_for_invalid_json(self, tmp_path: Path):
        config_path = tmp_path / "cdk.json"
        config_path.write_text("{not-json", encoding="utf-8")

        assert mirror.read_mirror_config(config_path) == {
            "enabled": False,
            "ecr_namespace": "gco/dockerhub",
        }

    def test_read_mirror_config_defaults_to_project_namespace(self, tmp_path: Path):
        config_path = tmp_path / "cdk.json"
        config_path.write_text(
            json.dumps({"context": {"project_name": "research"}}),
            encoding="utf-8",
        )

        assert mirror.read_mirror_config(config_path) == {
            "enabled": False,
            "ecr_namespace": "research/dockerhub",
        }
        assert mirror.cdk_default_namespace(config_path) == "research/dockerhub"

    def test_read_mirror_config_defaults_to_stock_namespace_without_context(self, tmp_path: Path):
        config_path = tmp_path / "cdk.json"
        config_path.write_text(json.dumps({"context": None}), encoding="utf-8")

        assert mirror.read_mirror_config(config_path) == {
            "enabled": False,
            "ecr_namespace": "gco/dockerhub",
        }

    def test_read_mirror_config_normalizes_configured_namespace(self, tmp_path: Path):
        config_path = tmp_path / "cdk.json"
        config_path.write_text(
            json.dumps(
                {
                    "context": {
                        "project_name": "research",
                        "volcano_image_mirror": {
                            "enabled": True,
                            "ecr_namespace": "/shared/upstream/",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        assert mirror.read_mirror_config(config_path) == {
            "enabled": True,
            "ecr_namespace": "shared/upstream",
        }

    def test_read_mirror_config_uses_project_namespace_when_override_is_empty(self, tmp_path: Path):
        config_path = tmp_path / "cdk.json"
        config_path.write_text(
            json.dumps(
                {
                    "context": {
                        "project_name": "research",
                        "volcano_image_mirror": {"ecr_namespace": "///"},
                    }
                }
            ),
            encoding="utf-8",
        )

        assert mirror.read_mirror_config(config_path) == {
            "enabled": False,
            "ecr_namespace": "research/dockerhub",
        }


class TestRuntimeCapabilities:
    @pytest.mark.parametrize(
        ("available", "expected"),
        [
            ({"docker": "/usr/bin/docker", "finch": "/usr/bin/finch"}, "docker"),
            ({"finch": "/usr/bin/finch", "podman": "/usr/bin/podman"}, "finch"),
            ({"podman": "/usr/bin/podman"}, "podman"),
        ],
    )
    def test_detect_runtime_uses_supported_preference_order(self, available, expected):
        with patch.object(mirror.shutil, "which", side_effect=available.get):
            assert mirror.detect_runtime() == expected

    def test_detect_runtime_errors_when_no_supported_cli_exists(self):
        with (
            patch.object(mirror.shutil, "which", return_value=None),
            pytest.raises(RuntimeError, match="No container CLI found"),
        ):
            mirror.detect_runtime()

    @pytest.mark.parametrize(("returncode", "expected"), [(0, True), (1, False)])
    def test_buildx_probe_reflects_command_result(self, returncode: int, expected: bool):
        result = MagicMock(returncode=returncode)
        with patch.object(mirror.subprocess, "run", return_value=result) as run:
            assert mirror._runtime_has_buildx("docker") is expected

        run.assert_called_once_with(
            ["docker", "buildx", "version"],
            capture_output=True,
            timeout=15,
        )

    @pytest.mark.parametrize(
        "error",
        [OSError("runtime unavailable"), mirror.subprocess.TimeoutExpired("docker", 15)],
    )
    def test_buildx_probe_treats_runtime_errors_as_unsupported(self, error: Exception):
        with patch.object(mirror.subprocess, "run", side_effect=error):
            assert mirror._runtime_has_buildx("docker") is False

    @pytest.mark.parametrize(
        ("stdout", "stderr", "expected"),
        [
            ("usage: pull --all-platforms", "", True),
            ("", "options: --all-platforms", True),
            ("usage: pull --platform", "", False),
        ],
    )
    def test_all_platforms_probe_reads_stdout_and_stderr(
        self, stdout: str, stderr: str, expected: bool
    ):
        result = MagicMock(stdout=stdout, stderr=stderr)
        with patch.object(mirror.subprocess, "run", return_value=result) as run:
            assert mirror._runtime_supports_all_platforms("finch") is expected

        run.assert_called_once_with(
            ["finch", "pull", "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )

    @pytest.mark.parametrize(
        "error",
        [OSError("runtime unavailable"), mirror.subprocess.TimeoutExpired("finch", 15)],
    )
    def test_all_platforms_probe_treats_runtime_errors_as_unsupported(self, error: Exception):
        with patch.object(mirror.subprocess, "run", side_effect=error):
            assert mirror._runtime_supports_all_platforms("finch") is False


class TestAuthentication:
    def test_ecr_auth_decodes_credentials_from_requested_region(self):
        ecr = MagicMock()
        token = base64.b64encode(b"AWS:secret-token").decode()
        ecr.get_authorization_token.return_value = {
            "authorizationData": [{"authorizationToken": token}]
        }

        with patch.object(mirror.boto3, "client", return_value=ecr) as client:
            assert mirror.ecr_auth("eu-west-1") == ("AWS", "secret-token")

        client.assert_called_once_with("ecr", region_name="eu-west-1")
        ecr.get_authorization_token.assert_called_once_with()

    def test_runtime_login_streams_password_and_logs_success(self):
        logs = []
        result = MagicMock(returncode=0, stderr=b"")
        with patch.object(mirror.subprocess, "run", return_value=result) as run:
            mirror.runtime_login(
                "docker",
                "registry.example",
                "AWS",
                "secret-token",
                log=logs.append,
            )

        run.assert_called_once_with(
            [
                "docker",
                "login",
                "--username",
                "AWS",
                "--password-stdin",
                "registry.example",
            ],
            input=b"secret-token",
            capture_output=True,
            check=False,
        )
        assert logs == ["  authenticated docker to registry.example"]

    def test_runtime_login_surfaces_runtime_error_without_logging_success(self):
        logs = []
        result = MagicMock(returncode=1, stderr=b"credentials rejected")
        with (
            patch.object(mirror.subprocess, "run", return_value=result),
            pytest.raises(RuntimeError, match="credentials rejected"),
        ):
            mirror.runtime_login(
                "podman",
                "registry.example",
                "AWS",
                "secret-token",
                log=logs.append,
            )

        assert logs == []


class TestCopyExecution:
    def _item(self):
        return mirror.MirrorItem(
            source_ref="docker.io/example/image:v1",
            dest_repo="gco/dockerhub/example/image",
            dest_ref="registry.example/gco/dockerhub/example/image:v1",
        )

    def test_unknown_copy_strategy_is_rejected(self):
        with pytest.raises(ValueError, match="unknown copy strategy"):
            mirror._copy_commands(self._item(), "docker", "single-platform", "")

    def test_all_platform_commands_run_in_pull_tag_push_order(self):
        item = self._item()
        expected = mirror._copy_commands(item, "finch", "all-platforms", "")
        successful = [MagicMock(returncode=0, stdout="", stderr="") for _ in expected]

        with patch.object(mirror.subprocess, "run", side_effect=successful) as run:
            mirror.copy_image(item, runtime="finch", strategy="all-platforms")

        assert [invocation.args[0] for invocation in run.call_args_list] == expected

    def test_copy_stops_at_first_failed_command_and_uses_stdout_detail(self):
        item = self._item()
        results = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="tag rejected", stderr=""),
        ]

        with (
            patch.object(mirror.subprocess, "run", side_effect=results) as run,
            pytest.raises(RuntimeError, match="tag rejected"),
        ):
            mirror.copy_image(item, runtime="finch", strategy="all-platforms")

        assert run.call_count == 2
        assert run.call_args_list[-1].args[0][:2] == ["finch", "tag"]


class TestMirrorImages:
    def test_full_flow_creates_copies_skips_and_binds_creation_callback(self):
        fake = _FakeEcr()
        ecr = fake.client
        created_acknowledgements = {}

        def _create_repository(repositoryName, **_kwargs):
            if repositoryName.endswith("existing-image"):
                raise ecr.exceptions.RepositoryAlreadyExistsException()
            acknowledgement = {
                "repositoryName": repositoryName,
                "repositoryArn": f"arn:aws:ecr:us-west-2:123456789012:repository/{repositoryName}",
            }
            created_acknowledgements[repositoryName] = acknowledgement
            return {"repository": acknowledgement}

        def _describe_images(repositoryName, imageIds):
            if repositoryName.endswith("existing-image"):
                return {"imageDetails": [{"imageTags": [imageIds[0]["imageTag"]]}]}
            raise ecr.exceptions.ImageNotFoundException()

        ecr.create_repository.side_effect = _create_repository
        ecr.describe_images.side_effect = _describe_images
        refs = [
            "docker.io/example/new-image:v1",
            "docker.io/example/existing-image:v2",
        ]
        repository_tags = {"gco:project": "research", "owner": "tests"}
        on_created = MagicMock()
        logs = []

        with (
            patch.object(mirror, "_account_id", return_value="123456789012"),
            patch.object(mirror, "detect_runtime", return_value="docker"),
            patch.object(mirror, "resolve_copy_strategy", return_value="buildx"),
            patch.object(mirror, "ecr_auth", return_value=("AWS", "secret-token")),
            patch.object(mirror, "runtime_login") as runtime_login,
            patch.object(mirror, "copy_image") as copy_image,
            patch.object(mirror.boto3, "client", return_value=ecr),
        ):
            result = mirror.mirror_images(
                "us-west-2",
                "research/dockerhub",
                source_refs=refs,
                log=logs.append,
                repository_tags=repository_tags,
                on_repository_created=on_created,
            )

        registry_host = "123456789012.dkr.ecr.us-west-2.amazonaws.com"
        new_repo = "research/dockerhub/example/new-image"
        new_ref = f"{registry_host}/{new_repo}:v1"
        existing_ref = f"{registry_host}/research/dockerhub/example/existing-image:v2"
        assert result == {
            "registry": f"{registry_host}/research/dockerhub",
            "strategy": "buildx",
            "mirrored": [new_ref],
            "skipped": [existing_ref],
            "created_repositories": [new_repo],
        }
        on_created.assert_called_once_with(
            "us-west-2",
            created_acknowledgements[new_repo],
        )
        assert ecr.create_repository.call_count == 2
        assert all(
            invocation.kwargs["tags"]
            == [
                {"Key": "gco:project", "Value": "research"},
                {"Key": "owner", "Value": "tests"},
            ]
            for invocation in ecr.create_repository.call_args_list
        )
        runtime_login.assert_called_once()
        assert runtime_login.call_args.args == (
            "docker",
            registry_host,
            "AWS",
            "secret-token",
        )
        copy_image.assert_called_once()
        copied_item = copy_image.call_args.args[0]
        assert copied_item.source_ref == refs[0]
        assert copied_item.dest_ref == new_ref
        assert copy_image.call_args.kwargs["strategy"] == "buildx"
        assert any("skip (already mirrored)" in message for message in logs)

    def test_skopeo_force_copy_does_not_use_runtime_login_or_presence_probe(self):
        source_ref = "docker.io/example/image:v1"
        with (
            patch.object(mirror, "_account_id", return_value="123456789012"),
            patch.object(mirror, "detect_runtime", return_value="docker"),
            patch.object(mirror, "resolve_copy_strategy", return_value="skopeo"),
            patch.object(mirror, "ecr_auth", return_value=("AWS", "secret-token")),
            patch.object(mirror, "runtime_login") as runtime_login,
            patch.object(mirror, "ensure_repository", return_value=False),
            patch.object(mirror, "tag_exists") as tag_exists,
            patch.object(mirror, "copy_image") as copy_image,
            patch.object(mirror.boto3, "client", return_value=MagicMock()),
        ):
            result = mirror.mirror_images(
                "us-east-1",
                "gco/dockerhub",
                source_refs=[source_ref],
                skip_existing=False,
            )

        runtime_login.assert_not_called()
        tag_exists.assert_not_called()
        copy_image.assert_called_once()
        assert copy_image.call_args.kwargs["runtime"] == "docker"
        assert copy_image.call_args.kwargs["strategy"] == "skopeo"
        assert copy_image.call_args.kwargs["password"] == "secret-token"
        assert result["mirrored"] == [
            "123456789012.dkr.ecr.us-east-1.amazonaws.com/gco/dockerhub/example/image:v1"
        ]
        assert result["skipped"] == []
        assert result["created_repositories"] == []

    def test_defaults_namespace_and_sources_from_project_configuration(self, tmp_path: Path):
        charts_path = tmp_path / "charts.yaml"
        charts = _sample_charts()
        source_ref = "docker.io/example/image:v3"

        with (
            patch.object(
                mirror, "cdk_default_namespace", return_value="/research/dockerhub/"
            ) as ns,
            patch.object(mirror, "load_charts_config", return_value=charts) as load,
            patch.object(mirror, "collect_source_refs", return_value=[source_ref]) as collect,
            patch.object(mirror, "_account_id", return_value="123456789012"),
            patch.object(mirror, "detect_runtime", return_value="docker"),
            patch.object(mirror, "resolve_copy_strategy", return_value="buildx"),
            patch.object(mirror, "ecr_auth", return_value=("AWS", "secret-token")),
            patch.object(mirror, "runtime_login"),
            patch.object(mirror, "ensure_repository", return_value=False),
            patch.object(mirror, "tag_exists", return_value=False),
            patch.object(mirror, "copy_image") as copy_image,
            patch.object(mirror.boto3, "client", return_value=MagicMock()),
        ):
            result = mirror.mirror_images("us-east-2", charts_path=charts_path)

        ns.assert_called_once_with()
        load.assert_called_once_with(charts_path)
        collect.assert_called_once_with(charts)
        assert result["registry"].endswith("/research/dockerhub")
        assert copy_image.call_args.args[0].dest_repo == "research/dockerhub/example/image"

    def test_default_source_collection_uses_canonical_charts(self):
        expected_sources = mirror.collect_source_refs()
        registry_host = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
        expected_destinations = [
            item.dest_ref
            for item in mirror.plan_from_sources(
                expected_sources,
                registry_host,
                "gco/dockerhub",
            )
        ]

        with (
            patch.object(mirror, "cdk_default_namespace", return_value="gco/dockerhub"),
            patch.object(
                mirror,
                "load_charts_config",
                wraps=mirror.load_charts_config,
            ) as load_charts,
            patch.object(mirror, "_account_id", return_value="123456789012"),
            patch.object(mirror, "detect_runtime", return_value="docker"),
            patch.object(mirror, "resolve_copy_strategy", return_value="buildx"),
            patch.object(mirror, "ecr_auth", return_value=("AWS", "secret-token")),
            patch.object(mirror, "runtime_login"),
            patch.object(mirror, "ensure_repository", return_value=False) as ensure_repository,
            patch.object(mirror, "tag_exists", return_value=True),
            patch.object(mirror, "copy_image") as copy_image,
            patch.object(mirror.boto3, "client", return_value=MagicMock()),
        ):
            result = mirror.mirror_images("us-east-1")

        load_charts.assert_called_once_with()
        assert ensure_repository.call_count == len(expected_sources)
        copy_image.assert_not_called()
        assert result["mirrored"] == []
        assert result["skipped"] == expected_destinations


class TestRepositoryAcknowledgement:
    def test_creation_callback_rejects_acknowledgement_for_different_repository(self):
        ecr = _FakeEcr().client
        ecr.create_repository.return_value = {
            "repository": {"repositoryName": "gco/dockerhub/example/other"}
        }
        callback = MagicMock()

        with pytest.raises(RuntimeError, match="acknowledged a different repository"):
            mirror.ensure_repository(
                ecr,
                "gco/dockerhub/example/image",
                on_created=callback,
            )

        callback.assert_not_called()
