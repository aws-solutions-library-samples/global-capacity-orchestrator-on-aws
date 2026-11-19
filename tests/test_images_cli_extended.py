"""
Extended unit coverage for ``cli/images.py``.

The base ``tests/test_images_cli.py`` covers the property-based
validation surface plus build/push happy paths. This module fills in
the read-only methods (``list_repos``, ``list_tags``, ``describe``,
``get_uri``, ``replication_get``, ``replication_status``),
administrative methods (``init`` retry paths, ``lifecycle_get/set``,
``replication_sync``), and the destructive methods (``delete_tag``,
``delete_repo``, ``cleanup``, ``prune``, ``orphans``). Each test
mocks the ECR boto3 client and asserts the request shape and the
manager-side aggregation logic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from cli.images import ImageManager, _isoformat, get_image_manager

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config() -> Any:
    config = MagicMock()
    config.global_region = "us-east-2"
    config.default_region = "us-west-2"
    config.project_name = "gco"
    return config


@pytest.fixture
def manager(mock_config: Any) -> ImageManager:
    with patch("cli.images.get_config", return_value=mock_config):
        mgr = ImageManager(config=mock_config, region="us-east-2")
    mgr._account_id_cache = "123456789012"
    return mgr


def _ecr_mock() -> Any:
    """Build a mock ECR client whose exception types are real subclasses of ClientError."""
    mock_ecr = MagicMock()

    class _RepoExists(ClientError):
        pass

    class _LifecycleNotFound(ClientError):
        pass

    class _RepoNotFound(ClientError):
        pass

    class _ImageNotFound(ClientError):
        pass

    mock_ecr.exceptions.RepositoryAlreadyExistsException = _RepoExists
    mock_ecr.exceptions.LifecyclePolicyNotFoundException = _LifecycleNotFound
    mock_ecr.exceptions.RepositoryNotFoundException = _RepoNotFound
    mock_ecr.exceptions.ImageNotFoundException = _ImageNotFound
    return mock_ecr


def _make_paginator(pages: list[dict[str, Any]]) -> Any:
    """Return a paginator stand-in whose ``paginate`` yields ``pages``."""
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    return paginator


# ---------------------------------------------------------------------------
# Region resolution
# ---------------------------------------------------------------------------


class TestRegionResolution:
    def test_explicit_region_argument_wins(self, mock_config: Any) -> None:
        with patch("cli.images.get_config", return_value=mock_config):
            mgr = ImageManager(config=mock_config, region="ap-south-1")
        assert mgr.region == "ap-south-1"

    def test_falls_through_to_global_region(self, mock_config: Any, monkeypatch: Any) -> None:
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        with patch("cli.images.get_config", return_value=mock_config):
            mgr = ImageManager(config=mock_config)
        assert mock_config.default_region == "us-west-2"
        assert mgr.region == "us-east-2"


# ---------------------------------------------------------------------------
# Account / registry / ARN helpers
# ---------------------------------------------------------------------------


class TestAccountAndRegistry:
    def test_account_id_caches_result(self, mock_config: Any) -> None:
        sts_client = MagicMock()
        sts_client.get_caller_identity.return_value = {"Account": "999999999999"}
        with (
            patch("cli.images.get_config", return_value=mock_config),
            patch("cli.images.boto3.client", return_value=sts_client) as mock_boto,
        ):
            mgr = ImageManager(config=mock_config, region="us-east-2")
            first = mgr._account_id()
            second = mgr._account_id()
        assert first == second == "999999999999"
        # Second call must hit the cache, not boto3.
        assert mock_boto.call_count == 1

    def test_registry_host_combines_account_and_region(self, manager: ImageManager) -> None:
        assert manager._registry_host() == "123456789012.dkr.ecr.us-east-2.amazonaws.com"

    def test_repo_arn_includes_prefix(self, manager: ImageManager) -> None:
        assert manager._repo_arn("svc") == "arn:aws:ecr:us-east-2:123456789012:repository/gco/svc"


# ---------------------------------------------------------------------------
# Validation negative cases
# ---------------------------------------------------------------------------


class TestValidation:
    def test_validate_name_accepts_single_char(self, manager: ImageManager) -> None:
        assert manager._validate_name("a") == "a"

    def test_validate_tag_accepts_underscore_prefix(self, manager: ImageManager) -> None:
        assert manager._validate_tag("_init") == "_init"

    def test_validate_context_rejects_path_traversal(self, manager: ImageManager) -> None:
        with pytest.raises(ValueError, match="path traversal"):
            manager._validate_context("foo/../bar")

    def test_validate_context_missing_dir_raises(
        self, manager: ImageManager, tmp_path: Any
    ) -> None:
        with pytest.raises(FileNotFoundError):
            manager._validate_context(str(tmp_path / "missing"))

    def test_validate_context_file_path_rejected(
        self, manager: ImageManager, tmp_path: Any
    ) -> None:
        f = tmp_path / "Dockerfile"
        f.write_text("FROM scratch\n")
        with pytest.raises(ValueError, match="not a directory"):
            manager._validate_context(str(f))


# ---------------------------------------------------------------------------
# Default tag (git short SHA fallback)
# ---------------------------------------------------------------------------


class TestDefaultTag:
    def test_default_tag_uses_git_sha(self, manager: ImageManager) -> None:
        result = MagicMock()
        result.returncode = 0
        result.stdout = "abc1234\n"
        with patch("cli.images.subprocess.run", return_value=result):
            assert manager._default_tag() == "abc1234"

    def test_default_tag_falls_back_to_latest_when_git_missing(self, manager: ImageManager) -> None:
        with patch("cli.images.subprocess.run", side_effect=FileNotFoundError("git")):
            assert manager._default_tag() == "latest"

    def test_default_tag_falls_back_when_git_returns_non_zero(self, manager: ImageManager) -> None:
        result = MagicMock()
        result.returncode = 128
        result.stdout = ""
        with patch("cli.images.subprocess.run", return_value=result):
            assert manager._default_tag() == "latest"


# ---------------------------------------------------------------------------
# init() — exception paths
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_re_raises_unrelated_create_client_error(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        err = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "CreateRepository",
        )
        ecr.create_repository.side_effect = err
        with patch.object(manager, "_ecr_client", return_value=ecr):
            with pytest.raises(ClientError) as excinfo:
                manager.init("svc")
            assert "AccessDeniedException" in str(excinfo.value)

    def test_init_lifecycle_error_is_swallowed(self, manager: ImageManager) -> None:
        """A failing lifecycle policy doesn't surface to callers."""
        ecr = _ecr_mock()
        ecr.create_repository.return_value = {}
        ecr.put_lifecycle_policy.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad policy"}},
            "PutLifecyclePolicy",
        )
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.init("svc")
        assert result["created"] is True

    def test_init_with_retain_applies_resource_tag(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.create_repository.return_value = {}
        ecr.put_lifecycle_policy.return_value = {}
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.init("svc", retain=True)
        assert result["retain"] is True
        ecr.tag_resource.assert_called_once()
        kwargs = ecr.tag_resource.call_args.kwargs
        assert kwargs["tags"] == [{"Key": "gco:retain", "Value": "true"}]

    def test_init_with_retain_swallows_tag_failure(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.create_repository.return_value = {}
        ecr.put_lifecycle_policy.return_value = {}
        ecr.tag_resource.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no tag perms"}},
            "TagResource",
        )
        with patch.object(manager, "_ecr_client", return_value=ecr):
            # Should not raise.
            result = manager.init("svc", retain=True)
        assert result["retain"] is True


# ---------------------------------------------------------------------------
# Read-only surface
# ---------------------------------------------------------------------------


class TestListRepos:
    def test_filters_to_project_prefix(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        pages = [
            {
                "repositories": [
                    {
                        "repositoryName": "gco/my-app",
                        "repositoryArn": "arn:aws:ecr:us-east-2:123456789012:repository/gco/my-app",
                        "repositoryUri": "123456789012.dkr.ecr.us-east-2.amazonaws.com/gco/my-app",
                        "createdAt": datetime(2026, 1, 1, tzinfo=UTC),
                        "imageTagMutability": "MUTABLE",
                    },
                    # Out of scope — must be skipped.
                    {"repositoryName": "other/svc"},
                ]
            }
        ]
        ecr.get_paginator.side_effect = lambda op: _make_paginator(
            pages if op == "describe_repositories" else [{"imageDetails": []}]
        )
        with patch.object(manager, "_ecr_client", return_value=ecr):
            repos = manager.list_repos()
        assert len(repos) == 1
        repo = repos[0]
        assert repo["name"] == "gco/my-app"
        assert repo["tag_mutability"] == "MUTABLE"
        assert repo["image_count"] == 0
        assert repo["created_at"].startswith("2026-01-01")

    def test_image_count_swallows_describe_errors(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.get_paginator.return_value = _make_paginator([])
        # Force describe_images to blow up so we exercise the except path.

        def paginator_side(op: str) -> Any:
            if op == "describe_images":
                raise ClientError(
                    {"Error": {"Code": "InternalFailure", "Message": "x"}},
                    "GetPaginator",
                )
            return _make_paginator(
                [
                    {
                        "repositories": [
                            {"repositoryName": "gco/svc"},
                        ]
                    }
                ]
            )

        ecr.get_paginator.side_effect = paginator_side
        with patch.object(manager, "_ecr_client", return_value=ecr):
            repos = manager.list_repos()
        assert repos[0]["image_count"] == 0


class TestListTags:
    def test_emits_one_row_per_tag_and_handles_untagged(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        pages = [
            {
                "imageDetails": [
                    {
                        "imageTags": ["v1", "latest"],
                        "imageDigest": "sha256:" + "a" * 64,
                        "imagePushedAt": datetime(2026, 5, 1, tzinfo=UTC),
                        "imageSizeInBytes": 1234,
                    },
                    {
                        # Untagged image: imageTags missing → list_tags emits a row with tag=None.
                        "imageDigest": "sha256:" + "b" * 64,
                        "imagePushedAt": datetime(2026, 5, 2, tzinfo=UTC),
                        "imageSizeInBytes": 4321,
                    },
                ]
            }
        ]
        ecr.get_paginator.return_value = _make_paginator(pages)
        with patch.object(manager, "_ecr_client", return_value=ecr):
            rows = manager.list_tags("svc")
        # 2 tags from the first detail + 1 untagged row from the second.
        assert len(rows) == 3
        tags = [r["tag"] for r in rows]
        assert "v1" in tags
        assert "latest" in tags
        assert None in tags


class TestDescribe:
    def test_returns_empty_when_no_details(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.describe_images.return_value = {"imageDetails": []}
        with patch.object(manager, "_ecr_client", return_value=ecr):
            assert manager.describe("svc", "v1") == {}

    def test_unpacks_detail_into_documented_shape(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.describe_images.return_value = {
            "imageDetails": [
                {
                    "imageTags": ["v1"],
                    "imageDigest": "sha256:" + "a" * 64,
                    "imagePushedAt": datetime(2026, 5, 1, tzinfo=UTC),
                    "imageSizeInBytes": 1234,
                    "imageScanFindingsSummary": {"findingSeverityCounts": {}},
                }
            ]
        }
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.describe("svc", "v1")
        assert result["name"] == "gco/svc"
        assert result["tag"] == "v1"
        assert result["digest"].startswith("sha256:")
        assert result["pushed_at"].startswith("2026-05-01")
        assert result["scan_findings_summary"] == {"findingSeverityCounts": {}}


def _stubbed_ecr(region: str = "us-east-2") -> tuple[Any, Stubber]:
    """A real ECR client, built from botocore's service model, with stubbed responses.

    Unlike a MagicMock it has only the operations ECR has, and the Stubber
    checks every request and response against the model, so a call to an
    operation that does not exist fails the test. Nothing is sent.
    """
    client = boto3.client(
        "ecr",
        region_name=region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # nosec B106 - fake credentials for a stubbed client
    )
    return client, Stubber(client)


_RULE = {
    "destinations": [{"region": "us-west-2", "registryId": "123456789012"}],
    "repositoryFilters": [{"filter": "gco/", "filterType": "PREFIX_MATCH"}],
}


class TestDescribeAgainstTheEcrModel:
    def test_a_tag_the_repository_lacks_is_empty(self, manager: ImageManager) -> None:
        """N43: DescribeImages answers a missing tag with ImageNotFoundException,
        not an empty list; describe reports both as {}."""
        ecr, stubber = _stubbed_ecr()
        stubber.add_client_error(
            "describe_images",
            service_error_code="ImageNotFoundException",
            http_status_code=400,
            expected_params={"repositoryName": "gco/svc", "imageIds": [{"imageTag": "v9"}]},
        )
        with stubber, patch.object(manager, "_ecr_client", return_value=ecr):
            assert manager.describe("svc", "v9") == {}
        stubber.assert_no_pending_responses()

    def test_a_missing_repository_still_raises(self, manager: ImageManager) -> None:
        ecr, stubber = _stubbed_ecr()
        stubber.add_client_error(
            "describe_images", service_error_code="RepositoryNotFoundException"
        )
        with (
            stubber,
            patch.object(manager, "_ecr_client", return_value=ecr),
            pytest.raises(ClientError, match="RepositoryNotFoundException"),
        ):
            manager.describe("svc", "v1")


class TestReplicationGet:
    """IMAGES-REPLICATION: ECR has no GetReplicationConfiguration; the rules come
    from DescribeRegistry, so `gco images replication get` used to fail."""

    def test_reads_the_rules_from_describe_registry(self, manager: ImageManager) -> None:
        ecr, stubber = _stubbed_ecr()
        stubber.add_response(
            "describe_registry",
            {"registryId": "123456789012", "replicationConfiguration": {"rules": [_RULE]}},
            {},
        )
        with stubber, patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.replication_get()
        stubber.assert_no_pending_responses()
        assert result == {"registryId": "123456789012", "policy": {"rules": [_RULE]}}

    @pytest.mark.parametrize(
        "response",
        [
            {"registryId": "123456789012", "replicationConfiguration": {"rules": []}},
            {"registryId": "123456789012"},
        ],
    )
    def test_returns_empty_without_rules(
        self, manager: ImageManager, response: dict[str, Any]
    ) -> None:
        ecr, stubber = _stubbed_ecr()
        stubber.add_response("describe_registry", response, {})
        with stubber, patch.object(manager, "_ecr_client", return_value=ecr):
            assert manager.replication_get() == {}

    def test_re_raises_a_describe_registry_error(self, manager: ImageManager) -> None:
        ecr, stubber = _stubbed_ecr()
        stubber.add_client_error(
            "describe_registry", service_error_code="ValidationException", http_status_code=400
        )
        with (
            stubber,
            patch.object(manager, "_ecr_client", return_value=ecr),
            pytest.raises(ClientError, match="ValidationException"),
        ):
            manager.replication_get()

    def test_the_cli_commands_run_against_the_real_ecr_model(self, manager: ImageManager) -> None:
        """`gco images replication get` and `sync` end to end on the stubbed client."""
        from click.testing import CliRunner

        from cli.main import cli

        ecr, stubber = _stubbed_ecr()
        stubber.add_response(
            "describe_registry",
            {"registryId": "123456789012", "replicationConfiguration": {"rules": [_RULE]}},
            {},
        )
        stubber.add_response(
            "describe_registry",
            {"registryId": "123456789012", "replicationConfiguration": {"rules": []}},
            {},
        )
        written = {
            "rules": [
                {
                    "destinations": [{"region": "eu-west-1", "registryId": "123456789012"}],
                    "repositoryFilters": [{"filter": "gco/", "filterType": "PREFIX_MATCH"}],
                }
            ]
        }
        stubber.add_response(
            "put_replication_configuration",
            {"replicationConfiguration": written},
            {"replicationConfiguration": written},
        )
        runner = CliRunner()
        with (
            stubber,
            patch("cli.images.get_image_manager", return_value=manager),
            patch.object(manager, "_ecr_client", return_value=ecr),
            patch("cli.images._load_cdk_json", return_value={"regional": ["eu-west-1"]}),
        ):
            got = runner.invoke(cli, ["--output", "json", "images", "replication", "get"])
            synced = runner.invoke(cli, ["--output", "json", "images", "replication", "sync"])
        assert got.exit_code == 0, got.output
        assert json.loads(got.output) == {
            "registryId": "123456789012",
            "policy": {"rules": [_RULE]},
        }
        assert synced.exit_code == 0, synced.output
        assert '"updated": true' in synced.output
        stubber.assert_no_pending_responses()


class TestReplicationStatus:
    def test_aggregates_status_per_image_per_region(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()

        def paginator_side(op: str) -> Any:
            if op == "describe_repositories":
                return _make_paginator(
                    [
                        {
                            "repositories": [
                                {"repositoryName": "gco/svc"},
                            ]
                        }
                    ]
                )
            if op == "describe_images":
                return _make_paginator(
                    [
                        {
                            "imageDetails": [
                                {
                                    "imageDigest": "sha256:" + "a" * 64,
                                    "imageTags": ["v1"],
                                }
                            ]
                        }
                    ]
                )
            raise AssertionError(f"unexpected paginator: {op}")

        ecr.get_paginator.side_effect = paginator_side
        ecr.describe_image_replication_status.return_value = {
            "replicationStatuses": [
                {"region": "us-west-2", "status": "COMPLETE", "registryId": "123456789012"},
                {"region": "eu-west-1", "status": "IN_PROGRESS", "registryId": "123456789012"},
            ]
        }
        with patch.object(manager, "_ecr_client", return_value=ecr):
            rows = manager.replication_status()
        assert {r["region"] for r in rows} == {"us-west-2", "eu-west-1"}
        assert all(r["repository"] == "gco/svc" for r in rows)


# ---------------------------------------------------------------------------
# Lifecycle policy
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_get_returns_empty_when_not_found(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        err = ecr.exceptions.LifecyclePolicyNotFoundException(
            {"Error": {"Code": "LifecyclePolicyNotFoundException", "Message": ""}},
            "GetLifecyclePolicy",
        )
        ecr.get_lifecycle_policy.side_effect = err
        with patch.object(manager, "_ecr_client", return_value=ecr):
            assert manager.lifecycle_get("svc") == {}

    def test_get_re_raises_other_errors(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.get_lifecycle_policy.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": ""}},
            "GetLifecyclePolicy",
        )
        with (
            patch.object(manager, "_ecr_client", return_value=ecr),
            pytest.raises(ClientError),
        ):
            manager.lifecycle_get("svc")

    def test_get_decodes_policy_text(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.get_lifecycle_policy.return_value = {
            "lifecyclePolicyText": json.dumps({"rules": [{"rulePriority": 1}]})
        }
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.lifecycle_get("svc")
        assert result["name"] == "gco/svc"
        assert result["policy"] == {"rules": [{"rulePriority": 1}]}

    def test_set_round_trips_policy(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.put_lifecycle_policy.return_value = {"registryId": "123456789012"}
        policy = {"rules": [{"rulePriority": 1}]}
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.lifecycle_set("svc", policy)
        ecr.put_lifecycle_policy.assert_called_once()
        kwargs = ecr.put_lifecycle_policy.call_args.kwargs
        assert kwargs["repositoryName"] == "gco/svc"
        assert json.loads(kwargs["lifecyclePolicyText"]) == policy
        assert result["registry_id"] == "123456789012"


# ---------------------------------------------------------------------------
# Replication sync
# ---------------------------------------------------------------------------


class TestReplicationSync:
    """replication_sync reads the rules with DescribeRegistry and writes them
    with PutReplicationConfiguration, both checked against botocore's model."""

    def test_writes_rule_to_every_other_region(self, manager: ImageManager) -> None:
        ecr, stubber = _stubbed_ecr()
        stubber.add_response(
            "describe_registry",
            {"registryId": "123456789012", "replicationConfiguration": {"rules": []}},
            {},
        )
        written = {
            "rules": [
                {
                    "destinations": [
                        {"region": "us-west-2", "registryId": "123456789012"},
                        {"region": "eu-west-1", "registryId": "123456789012"},
                    ],
                    "repositoryFilters": [{"filter": "gco/", "filterType": "PREFIX_MATCH"}],
                }
            ]
        }
        stubber.add_response(
            "put_replication_configuration",
            {"replicationConfiguration": written},
            {"replicationConfiguration": written},
        )
        with (
            stubber,
            patch.object(
                manager,
                "_replication_regions",
                return_value=["us-east-2", "us-west-2", "eu-west-1"],
            ),
            patch.object(manager, "_ecr_client", return_value=ecr),
        ):
            result = manager.replication_sync()
        stubber.assert_no_pending_responses()
        assert result == {
            "configuration": written,
            "destinations": ["us-west-2", "eu-west-1"],  # source region elided
            "registry_id": "123456789012",
            "updated": True,
        }

    def test_preserves_unrelated_rules(self, manager: ImageManager) -> None:
        unrelated_rule = {
            "destinations": [{"region": "ap-south-1", "registryId": "123456789012"}],
            "repositoryFilters": [{"filter": "other/", "filterType": "PREFIX_MATCH"}],
        }
        stale_rule = {
            "destinations": [{"region": "eu-west-1", "registryId": "123456789012"}],
            "repositoryFilters": [{"filter": "gco/", "filterType": "PREFIX_MATCH"}],
        }
        ecr, stubber = _stubbed_ecr()
        stubber.add_response(
            "describe_registry",
            {
                "registryId": "123456789012",
                "replicationConfiguration": {"rules": [unrelated_rule, stale_rule]},
            },
            {},
        )
        written = {
            "rules": [
                unrelated_rule,
                {
                    "destinations": [{"region": "us-west-2", "registryId": "123456789012"}],
                    "repositoryFilters": [{"filter": "gco/", "filterType": "PREFIX_MATCH"}],
                },
            ]
        }
        stubber.add_response(
            "put_replication_configuration",
            {"replicationConfiguration": written},
            {"replicationConfiguration": written},
        )
        with (
            stubber,
            patch.object(manager, "_replication_regions", return_value=["us-west-2"]),
            patch.object(manager, "_ecr_client", return_value=ecr),
        ):
            result = manager.replication_sync()
        stubber.assert_no_pending_responses()
        assert result["configuration"] == written

    def test_no_destinations_preserves_current_configuration(self, mock_config: Any) -> None:
        single_region = MagicMock()
        single_region.global_region = "us-east-2"
        single_region.default_region = "us-east-2"
        single_region.project_name = "gco"
        with patch("cli.images.get_config", return_value=single_region):
            mgr = ImageManager(config=single_region, region="us-east-2")
        mgr._account_id_cache = "123456789012"

        current = {
            "rules": [
                {
                    "destinations": [{"region": "eu-west-1", "registryId": "123456789012"}],
                    "repositoryFilters": [{"filter": "other/", "filterType": "PREFIX_MATCH"}],
                }
            ]
        }
        ecr, stubber = _stubbed_ecr()
        stubber.add_response(
            "describe_registry",
            {"registryId": "123456789012", "replicationConfiguration": current},
            {},
        )
        # No put_replication_configuration response is queued: a write fails the test.
        with (
            stubber,
            patch.object(mgr, "_replication_regions", return_value=["us-east-2"]),
            patch.object(mgr, "_ecr_client", return_value=ecr),
        ):
            result = mgr.replication_sync()
        stubber.assert_no_pending_responses()
        assert result == {
            "configuration": current,
            "destinations": [],
            "registry_id": "123456789012",
            "updated": False,
        }


# ---------------------------------------------------------------------------
# Destructive — single-tag and full-repo deletes
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_tag_returns_summary(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.batch_delete_image.return_value = {
            "imageIds": [{"imageDigest": "sha256:abc", "imageTag": "v1"}],
            "failures": [],
        }
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.delete_tag("svc", "v1")
        assert result["tag"] == "v1"
        assert result["deleted"] == [{"digest": "sha256:abc", "tag": "v1"}]

    def test_delete_repo_force_passes_through(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.delete_repository.return_value = {
            "repository": {"registryId": "123456789012"},
        }
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.delete_repo("svc", force=True)
        kwargs = ecr.delete_repository.call_args.kwargs
        assert kwargs["force"] is True
        assert result["registry_id"] == "123456789012"


# ---------------------------------------------------------------------------
# cleanup() / prune()
# ---------------------------------------------------------------------------


class TestCleanup:
    def test_cleanup_requires_name_or_all(self, manager: ImageManager) -> None:
        with pytest.raises(ValueError, match="cleanup\\(\\) requires"):
            manager.cleanup()

    def test_cleanup_one_repo_aggregates_untagged(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.get_paginator.return_value = _make_paginator(
            [
                {
                    "imageDetails": [
                        {"imageDigest": "sha256:a", "imageSizeInBytes": 100},
                        {"imageDigest": "sha256:b", "imageSizeInBytes": 200},
                        {"imageSizeInBytes": 50},  # missing digest — skipped
                    ]
                }
            ]
        )
        ecr.batch_delete_image.return_value = {
            "imageIds": [{"imageDigest": "sha256:a"}, {"imageDigest": "sha256:b"}]
        }
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.cleanup(name="svc")
        assert result["repos_touched"] == 1
        assert result["tags_deleted"] == 2
        assert result["bytes_freed"] == 300

    def test_cleanup_all_iterates_every_repo(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        # describe_repositories returns 2 repos; describe_images returns
        # untagged for each.
        repos_pages = [
            {
                "repositories": [
                    {"repositoryName": "gco/a"},
                    {"repositoryName": "gco/b"},
                ]
            }
        ]
        untagged_pages = [
            {"imageDetails": [{"imageDigest": "sha256:" + "a" * 64, "imageSizeInBytes": 10}]}
        ]

        def paginator_side(op: str) -> Any:
            if op == "describe_repositories":
                return _make_paginator(list(repos_pages))
            return _make_paginator(list(untagged_pages))

        ecr.get_paginator.side_effect = paginator_side
        ecr.batch_delete_image.return_value = {"imageIds": [{"imageDigest": "x"}]}
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.cleanup(all=True)
        assert result["repos_touched"] == 2

    def test_cleanup_skips_repo_on_describe_error(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.get_paginator.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": ""}},
            "GetPaginator",
        )
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.cleanup(name="svc")
        assert result["repos_touched"] == 0

    def test_cleanup_handles_chunks_over_100(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        details = [{"imageDigest": f"sha256:{i:064x}", "imageSizeInBytes": 1} for i in range(150)]
        ecr.get_paginator.return_value = _make_paginator([{"imageDetails": details}])
        # Each batch_delete_image returns whatever was passed in.
        ecr.batch_delete_image.side_effect = lambda **kwargs: {"imageIds": kwargs["imageIds"]}
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.cleanup(name="svc")
        # 150 ids → 100 + 50 → 2 calls.
        assert ecr.batch_delete_image.call_count == 2
        assert result["tags_deleted"] == 150


class TestPrune:
    def _untagged(self, age_days: int, digest: str = "sha256:" + "a" * 64) -> dict[str, Any]:
        return {
            "imageDigest": digest,
            "imagePushedAt": datetime.now(UTC) - timedelta(days=age_days),
            "imageSizeInBytes": 1000,
        }

    def test_prune_dry_run_does_not_delete(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()

        def paginator_side(op: str) -> Any:
            if op == "describe_repositories":
                return _make_paginator([{"repositories": [{"repositoryName": "gco/svc"}]}])
            return _make_paginator([{"imageDetails": [self._untagged(60)]}])

        ecr.get_paginator.side_effect = paginator_side
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.prune(dry_run=True)
        ecr.batch_delete_image.assert_not_called()
        assert result["dry_run"] is True
        assert result["tags_deleted"] == 1
        assert result["bytes_freed"] == 1000

    def test_prune_skips_recent_images(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()

        def paginator_side(op: str) -> Any:
            if op == "describe_repositories":
                return _make_paginator([{"repositories": [{"repositoryName": "gco/svc"}]}])
            return _make_paginator(
                [{"imageDetails": [self._untagged(2)]}]
            )  # 2 days old, well within 30d cutoff

        ecr.get_paginator.side_effect = paginator_side
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.prune(dry_run=False)
        assert result["repos_touched"] == 0
        assert result["tags_deleted"] == 0

    def test_prune_actually_deletes_when_not_dry_run(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()

        def paginator_side(op: str) -> Any:
            if op == "describe_repositories":
                return _make_paginator([{"repositories": [{"repositoryName": "gco/svc"}]}])
            return _make_paginator([{"imageDetails": [self._untagged(60)]}])

        ecr.get_paginator.side_effect = paginator_side
        with patch.object(manager, "_ecr_client", return_value=ecr):
            result = manager.prune(dry_run=False)
        ecr.batch_delete_image.assert_called_once()
        assert result["repos_touched"] == 1
        assert result["tags_deleted"] == 1


# ---------------------------------------------------------------------------
# orphans()
# ---------------------------------------------------------------------------


class TestOrphans:
    _OLD = (datetime.now(UTC) - timedelta(days=60)).isoformat()

    def _orphans(
        self,
        manager: ImageManager,
        tags: list[dict[str, Any]],
        *,
        endpoint_refs: set[str] | None = None,
        job_refs: set[str] | None = None,
    ) -> list[str]:
        with (
            patch.object(manager, "list_repos", return_value=[{"name": "gco/svc"}]),
            patch.object(manager, "list_tags", return_value=tags),
            patch.object(
                manager, "_collect_inference_image_refs", return_value=set(endpoint_refs or ())
            ),
            patch.object(
                manager, "_collect_recent_job_image_refs", return_value=set(job_refs or ())
            ),
        ):
            return [row["tag"] for row in manager.orphans(threshold_days=30)]

    def test_orphans_excludes_referenced(self, manager: ImageManager) -> None:
        tags = [
            {"tag": "in-use", "digest": "sha256:1", "pushed_at": self._OLD},
            {"tag": "stale", "digest": "sha256:2", "pushed_at": self._OLD},
        ]
        in_use = f"{manager._registry_host()}/gco/svc:in-use"
        assert self._orphans(manager, tags, endpoint_refs={in_use}) == ["stale"]
        assert self._orphans(manager, tags, job_refs={in_use}) == ["stale"]

    def test_orphans_excludes_recent(self, manager: ImageManager) -> None:
        recent = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        tags = [{"tag": "fresh", "digest": "sha256:1", "pushed_at": recent}]
        assert self._orphans(manager, tags) == []

    def test_a_tag_named_through_another_regions_replica_is_in_use(
        self, manager: ImageManager
    ) -> None:
        """CC19: endpoints and jobs pull from their own Region's replica, so
        the scan's us-east-2 URI never matched a us-west-2 reference."""
        tags = [
            {"tag": "v1", "digest": "sha256:1", "pushed_at": self._OLD},
            {"tag": "v2", "digest": "sha256:2", "pushed_at": self._OLD},
        ]
        replica = "123456789012.dkr.ecr.us-west-2.amazonaws.com/gco/svc:v1"
        other_account = "210987654321.dkr.ecr.us-east-2.amazonaws.com/gco/svc:v2"
        assert self._orphans(manager, tags, job_refs={replica, other_account}) == ["v2"]

    def test_a_tag_whose_image_is_named_by_digest_is_in_use(self, manager: ImageManager) -> None:
        host = manager._registry_host()
        tags = [
            {"tag": "pinned", "digest": "sha256:1", "pushed_at": self._OLD},
            {"tag": "also-pinned", "digest": "sha256:1", "pushed_at": self._OLD},
            {"tag": "moved", "digest": "sha256:2", "pushed_at": self._OLD},
            {"tag": "named", "digest": "sha256:3", "pushed_at": self._OLD},
            {"tag": "stale", "digest": "sha256:4", "pushed_at": self._OLD},
        ]
        refs = {
            f"{host}/gco/svc@sha256:1",
            # A tag and a digest: both are in use.
            f"{host}/gco/svc:named@sha256:2",
        }
        assert self._orphans(manager, tags, endpoint_refs=refs) == ["stale"]

    def test_a_reference_without_a_tag_names_latest(self, manager: ImageManager) -> None:
        tags = [
            {"tag": "latest", "digest": "sha256:1", "pushed_at": self._OLD},
            {"tag": "v1", "digest": "sha256:2", "pushed_at": self._OLD},
        ]
        refs = {f"{manager._registry_host()}/gco/svc"}
        assert self._orphans(manager, tags, job_refs=refs) == ["v1"]

    def test_an_unreadable_source_fails_the_scan(self, manager: ImageManager) -> None:
        with (
            patch.object(manager, "list_repos") as list_repos,
            patch.object(
                manager,
                "_collect_inference_image_refs",
                side_effect=RuntimeError("could not read the inference endpoints: denied"),
            ),
            patch.object(manager, "_collect_recent_job_image_refs", return_value=set()),
            pytest.raises(RuntimeError, match="could not read the inference endpoints"),
        ):
            manager.orphans()
        list_repos.assert_not_called()


# ---------------------------------------------------------------------------
# Inference cross-reference lookup
# ---------------------------------------------------------------------------


class TestInferenceImageRefs:
    def test_collects_every_image_in_the_record(self, manager: ImageManager) -> None:
        """The flat image, the canary's, the per-Region replica URIs and any
        nested image (the Mooncake proxy, a status field) all count."""
        fake_inference = MagicMock()
        fake_inference.list_endpoints.return_value = [
            {
                "spec": {
                    "image": "registry/img:v1",
                    "region_image_uris": {"us-west-2": "replica/img:v1", "bad": 7},
                    "canary": {"image": "registry/img:v2"},
                    "mooncake": {"proxy": {"image": "registry/proxy:v1"}},
                    "env": [{"name": "X", "value": "registry/not-an-image:v1"}],
                },
                "status": {"rollout": [{"image": "registry/img:v0"}]},
            },
            {"spec": {"image": "registry/other:v1"}},
            # Endpoint with no image is skipped.
            {"spec": {"image": ""}},
        ]
        with patch("cli.inference.InferenceManager", return_value=fake_inference):
            refs = manager._collect_inference_image_refs()
        assert refs == {
            "registry/img:v1",
            "replica/img:v1",
            "registry/img:v2",
            "registry/proxy:v1",
            "registry/img:v0",
            "registry/other:v1",
        }

    def test_raises_when_the_inference_manager_cannot_start(self, manager: ImageManager) -> None:
        """CC19: an unreadable endpoint table used to mean "nothing is
        referenced", so every old tag was listed, in-use ones included."""
        with (
            patch("cli.inference.InferenceManager", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="could not read the inference endpoints: boom"),
        ):
            manager._collect_inference_image_refs()

    def test_raises_when_list_endpoints_fails(self, manager: ImageManager) -> None:
        fake_inference = MagicMock()
        fake_inference.list_endpoints.side_effect = RuntimeError("ddb down")
        with (
            patch("cli.inference.InferenceManager", return_value=fake_inference),
            pytest.raises(RuntimeError, match="could not read the inference endpoints: ddb down"),
        ):
            manager._collect_inference_image_refs()


# ---------------------------------------------------------------------------
# Job cross-reference lookup
# ---------------------------------------------------------------------------


def _job(name: str, status: str, created: Any, refs: list[Any]) -> Any:
    from cli.jobs import JobInfo

    return JobInfo(
        name=name,
        namespace="gco-jobs",
        region="us-east-1",
        status=status,
        created_time=created,
        image_refs=refs,
    )


class TestJobImageRefs:
    def _refs(
        self,
        manager: ImageManager,
        jobs_by_region: dict[str, Any],
        threshold_days: int = 30,
    ) -> tuple[set[str], Any]:
        fake_jm = MagicMock()

        def list_jobs(*, region: str) -> list[Any]:
            jobs = jobs_by_region[region]
            if isinstance(jobs, Exception):
                raise jobs
            return list(jobs)

        fake_jm.list_jobs.side_effect = list_jobs
        aws = MagicMock()
        aws.discover_regional_stacks.return_value = dict.fromkeys(jobs_by_region)
        with (
            patch("cli.jobs.JobManager", return_value=fake_jm),
            patch("cli.aws_client.get_aws_client", return_value=aws),
        ):
            refs = manager._collect_recent_job_image_refs(threshold_days=threshold_days)
        return refs, fake_jm

    def test_unions_every_regions_jobs(self, manager: ImageManager) -> None:
        recent = datetime.now(UTC)
        refs, fake_jm = self._refs(
            manager,
            {
                "us-west-2": [_job("j2", "running", recent, ["registry/a:v1", "registry/c:v3"])],
                "us-east-1": [_job("j1", "succeeded", recent, ["registry/a:v1", "registry/b:v2"])],
            },
        )
        assert refs == {"registry/a:v1", "registry/b:v2", "registry/c:v3"}
        assert [c.kwargs for c in fake_jm.list_jobs.call_args_list] == [
            {"region": "us-east-1"},
            {"region": "us-west-2"},
        ]

    def test_an_unfinished_job_counts_however_old(self, manager: ImageManager) -> None:
        """CC19: a job running for longer than the threshold uses its image;
        only finished jobs older than the threshold stop counting."""
        old = datetime.now(UTC) - timedelta(days=120)
        recent = datetime.now(UTC) - timedelta(days=1)
        refs, _ = self._refs(
            manager,
            {
                "us-east-1": [
                    _job("ancient-done", "succeeded", old, ["should/not:appear"]),
                    _job("ancient-failed", "failed", old, ["also/not:appear"]),
                    _job("ancient-running", "running", old, ["running/img:v1"]),
                    _job("ancient-pending", "pending", old, ["pending/img:v1"]),
                    _job("recent-done", "succeeded", recent, ["recent/img:v1"]),
                ]
            },
        )
        assert refs == {"running/img:v1", "pending/img:v1", "recent/img:v1"}

    def test_treats_missing_created_time_as_in_window(self, manager: ImageManager) -> None:
        """A freshly-submitted job that hasn't been picked up by the
        cluster's status loop yet has ``created_time=None``."""
        refs, _ = self._refs(
            manager, {"us-east-1": [_job("just-submitted", "succeeded", None, ["fresh/img:v1"])]}
        )
        assert refs == {"fresh/img:v1"}

    def test_handles_naive_datetime(self, manager: ImageManager) -> None:
        """A naive ``created_time`` is read as UTC, not a TypeError."""
        # Deliberately naive — the branch under test is a caller that did not
        # attach tzinfo, which the collector must still compare against now().
        naive_old = datetime.now() - timedelta(days=90)  # noqa: DTZ005
        refs, _ = self._refs(
            manager, {"us-east-1": [_job("j", "succeeded", naive_old, ["img:v1"])]}
        )
        assert refs == set()

    def test_raises_when_a_region_cannot_be_listed(self, manager: ImageManager) -> None:
        """CC19: a skipped Region used to make its jobs' images look unused."""
        with pytest.raises(RuntimeError, match="could not list the jobs in us-west-2: api down"):
            self._refs(
                manager,
                {
                    "us-east-1": [_job("j", "running", None, ["img:v1"])],
                    "us-west-2": RuntimeError("api down"),
                },
            )

    def test_raises_when_jobs_cannot_be_read_at_all(self, manager: ImageManager) -> None:
        with (
            patch("cli.jobs.JobManager", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="boom"),
        ):
            manager._collect_recent_job_image_refs()

    def test_no_regional_stack_means_no_jobs(self, manager: ImageManager) -> None:
        refs, fake_jm = self._refs(manager, {})
        assert refs == set()
        fake_jm.list_jobs.assert_not_called()

    def test_skips_invalid_entries(self, manager: ImageManager) -> None:
        """Non-string and empty ``image_refs`` items are ignored."""
        refs, _ = self._refs(
            manager,
            {
                "us-east-1": [
                    _job("j", "running", None, ["good/img:v1", "", None, 7, "another/img:v2"])
                ]
            },
        )
        assert refs == {"good/img:v1", "another/img:v2"}


# ---------------------------------------------------------------------------
# ECR auth + tag-collision wiring (build/push pre-flight)
# ---------------------------------------------------------------------------


class TestEcrLoginAndCollision:
    def test_ecr_login_command_shape(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        # base64("AWS:abcdef")
        ecr.get_authorization_token.return_value = {
            "authorizationData": [{"authorizationToken": "QVdTOmFiY2RlZg=="}]
        }
        captured: dict[str, Any] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        with (
            patch.object(manager, "_ecr_client", return_value=ecr),
            patch("cli.images.subprocess.run", side_effect=fake_run),
        ):
            manager._ecr_login("docker")
        assert captured["cmd"][0] == "docker"
        assert captured["cmd"][1] == "login"
        assert "--password-stdin" in captured["cmd"]
        assert captured["input"] == b"abcdef"

    def test_ecr_login_failure_raises_runtime_error(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.get_authorization_token.return_value = {
            "authorizationData": [{"authorizationToken": "QVdTOmFiY2RlZg=="}]
        }
        with (
            patch.object(manager, "_ecr_client", return_value=ecr),
            patch(
                "cli.images.subprocess.run",
                return_value=MagicMock(returncode=1, stderr=b"denied"),
            ),
            pytest.raises(RuntimeError, match=r"login.*failed"),
        ):
            manager._ecr_login("docker")

    def test_collision_check_skipped_for_mutable_repo(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.describe_repositories.return_value = {
            "repositories": [{"imageTagMutability": "MUTABLE"}]
        }
        with patch.object(manager, "_ecr_client", return_value=ecr):
            # Should not raise; describe_images shouldn't even be consulted.
            manager._check_tag_immutable_collision("svc", "v1")
        ecr.describe_images.assert_not_called()

    def test_collision_check_passes_when_repo_missing(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        err = ecr.exceptions.RepositoryNotFoundException(
            {"Error": {"Code": "RepositoryNotFoundException", "Message": ""}},
            "DescribeRepositories",
        )
        ecr.describe_repositories.side_effect = err
        with patch.object(manager, "_ecr_client", return_value=ecr):
            manager._check_tag_immutable_collision("svc", "v1")  # no raise

    def test_collision_check_passes_when_tag_missing(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.describe_repositories.return_value = {
            "repositories": [{"imageTagMutability": "IMMUTABLE"}]
        }
        err = ecr.exceptions.ImageNotFoundException(
            {"Error": {"Code": "ImageNotFoundException", "Message": ""}},
            "DescribeImages",
        )
        ecr.describe_images.side_effect = err
        with patch.object(manager, "_ecr_client", return_value=ecr):
            manager._check_tag_immutable_collision("svc", "v1")  # no raise


# ---------------------------------------------------------------------------
# Push-only (no build) happy path
# ---------------------------------------------------------------------------


class TestPushOnly:
    def test_push_invokes_tag_then_push(self, manager: ImageManager) -> None:
        ecr = _ecr_mock()
        ecr.create_repository.return_value = {}
        ecr.put_lifecycle_policy.return_value = {}
        ecr.describe_repositories.return_value = {"repositories": []}
        ecr.describe_images.return_value = {"imageDetails": []}
        ecr.get_authorization_token.return_value = {
            "authorizationData": [{"authorizationToken": "QVdTOnRva2Vu"}]
        }

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: Any) -> Any:
            captured.append(list(cmd))
            return MagicMock(
                returncode=0,
                stdout="digest: sha256:" + "0" * 64,
                stderr="",
            )

        with (
            patch("cli.images.detect_container_runtime", return_value="docker"),
            patch.object(manager, "_ecr_client", return_value=ecr),
            patch("cli.images.subprocess.run", side_effect=fake_run),
        ):
            result = manager.push(name="my-app", tag="v1", local_image="local/my-app:scratch")

        assert any("tag" in cmd and "local/my-app:scratch" in cmd for cmd in captured)
        assert any("push" in cmd for cmd in captured)
        assert result["digest"].startswith("sha256:")
        assert result["repository"] == "gco/my-app"

    def test_push_rejects_empty_local_image(self, manager: ImageManager) -> None:
        with pytest.raises(ValueError, match="non-empty image reference"):
            manager.push(name="my-app", tag="v1", local_image="")


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestIsoformat:
    def test_passes_through_none(self) -> None:
        assert _isoformat(None) is None

    def test_emits_iso_for_datetime(self) -> None:
        d = datetime(2026, 5, 1, tzinfo=UTC)
        assert _isoformat(d).startswith("2026-05-01")

    def test_passes_string_through(self) -> None:
        assert _isoformat("not a date") == "not a date"


class TestParseIso:
    def test_returns_aware_datetime(self) -> None:
        d = ImageManager._parse_iso("2026-05-01T00:00:00+00:00")
        assert d is not None and d.tzinfo is not None

    def test_naive_string_gets_utc(self) -> None:
        d = ImageManager._parse_iso("2026-05-01T00:00:00")
        assert d is not None and d.tzinfo is UTC

    def test_returns_none_for_garbage(self) -> None:
        assert ImageManager._parse_iso("not a date") is None

    def test_returns_none_for_other_types(self) -> None:
        assert ImageManager._parse_iso(123) is None

    def test_passes_through_aware_datetime(self) -> None:
        d = datetime(2026, 5, 1, tzinfo=UTC)
        assert ImageManager._parse_iso(d) is d


class TestFactory:
    def test_get_image_manager_constructs(self, mock_config: Any) -> None:
        with patch("cli.images.get_config", return_value=mock_config):
            mgr = get_image_manager(config=mock_config, region="us-east-2")
        assert isinstance(mgr, ImageManager)
        assert mgr.region == "us-east-2"


class TestRewriteImageUriEdgeCases:
    """Branch coverage for ``cli._image_uri.rewrite_image_uri_for_region``.

    Two unhit branches in the previous suite:
      1. URI containing ``://`` (looks like a URL with a scheme) — should
         pass through unchanged.
      2. ECR URI with no path/tag (just ``<account>.dkr.ecr.<region>.amazonaws.com``,
         no trailing slash) — the rewrite still has to swap the region
         segment but there's no ``parts[1]`` to append.
    """

    def test_uri_with_scheme_passes_through(self) -> None:
        from cli._image_uri import rewrite_image_uri_for_region

        # Anything containing ``://`` is treated as a non-bare ref and left
        # alone, even when it would otherwise look like an ECR host.
        for uri in (
            "https://123456789012.dkr.ecr.us-east-2.amazonaws.com/gco/svc:v1",
            "docker://123456789012.dkr.ecr.us-east-1.amazonaws.com/foo",
        ):
            assert rewrite_image_uri_for_region(uri, "eu-west-1") == uri

    def test_bare_ecr_host_with_no_path(self) -> None:
        from cli._image_uri import rewrite_image_uri_for_region

        # Just the host — no ``/path`` or ``:tag``. The rewrite still
        # swaps the region segment so the caller gets back a host with
        # the destination region applied.
        src = "123456789012.dkr.ecr.us-east-2.amazonaws.com"
        out = rewrite_image_uri_for_region(src, "eu-west-1")
        assert out == "123456789012.dkr.ecr.eu-west-1.amazonaws.com"
