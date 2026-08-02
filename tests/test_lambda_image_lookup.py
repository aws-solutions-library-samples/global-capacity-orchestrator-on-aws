"""
Tests for the ``lambda/image-lookup/handler.py`` ECR custom resource handler.

Covers every branch of the adopt-or-create flow: fresh creation,
adoption when the repo already exists, both translations of
``RepositoryNotFoundException`` (typed exception and generic
``ClientError`` shape), the lifecycle policy application
(including empty-string and JSON-validation paths), and every Delete
branch — idempotent absence, retain-tag short-circuit, removal-policy
retain, destroy with paginated empty-on-delete, destroy without
empty-on-delete, plus the unsupported-RequestType error.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests._lambda_imports import load_lambda_module

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def image_lookup_module() -> Any:
    """Load the image-lookup handler with ``boto3.client`` patched.

    See ``tests/_lambda_imports.py`` for why this uses
    :func:`load_lambda_module` rather than the
    ``sys.path.insert + import handler`` pattern.
    """
    with patch("boto3.client") as mock_client:
        handler = load_lambda_module("image-lookup")
        # Wire a typed ``RepositoryNotFoundException`` onto the
        # client mock so tests can raise it through the ECR-side
        # ``ecr.exceptions.RepositoryNotFoundException`` chain.
        mock_ecr = mock_client.return_value
        mock_ecr.exceptions.RepositoryNotFoundException = type(
            "RepositoryNotFoundException", (Exception,), {}
        )
        yield handler, mock_ecr


# ---------------------------------------------------------------------------
# _describe_repository
# ---------------------------------------------------------------------------


class TestDescribeRepository:
    def test_returns_repo_when_present(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.return_value = {
            "repositories": [
                {
                    "repositoryName": "gco/svc",
                    "repositoryArn": "arn:aws:ecr:us-east-1:1:repository/gco/svc",
                    "repositoryUri": "1.dkr.ecr.us-east-1.amazonaws.com/gco/svc",
                }
            ]
        }
        out = handler._describe_repository(ecr, "gco/svc")
        assert out is not None
        assert out["repositoryArn"].endswith("/gco/svc")

    def test_returns_none_on_typed_not_found(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.side_effect = ecr.exceptions.RepositoryNotFoundException(
            "missing"
        )
        out = handler._describe_repository(ecr, "gco/svc")
        assert out is None

    def test_returns_none_on_generic_client_error_translation(
        self, image_lookup_module: Any
    ) -> None:
        """Some boto3 stubs surface ``RepositoryNotFoundException``
        through the generic ``ClientError`` shape rather than the typed
        exception. The handler sniffs the error code and translates."""
        handler, ecr = image_lookup_module

        class _ClientErrorish(Exception):
            response = {"Error": {"Code": "RepositoryNotFoundException"}}

        ecr.describe_repositories.side_effect = _ClientErrorish("boom")
        out = handler._describe_repository(ecr, "gco/svc")
        assert out is None

    def test_propagates_other_errors(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module

        class _ClientErrorish(Exception):
            response = {"Error": {"Code": "AccessDeniedException"}}

        ecr.describe_repositories.side_effect = _ClientErrorish("denied")
        with pytest.raises(_ClientErrorish):
            handler._describe_repository(ecr, "gco/svc")

    def test_empty_repositories_list_returns_none(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.return_value = {"repositories": []}
        out = handler._describe_repository(ecr, "gco/svc")
        assert out is None


# ---------------------------------------------------------------------------
# _create_repository
# ---------------------------------------------------------------------------


class TestCreateRepository:
    def test_creates_with_project_standard_config(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.create_repository.return_value = {
            "repository": {
                "repositoryArn": "arn:aws:ecr:us-east-1:1:repository/gco/new",
                "repositoryUri": "1.dkr.ecr.us-east-1.amazonaws.com/gco/new",
            }
        }
        out = handler._create_repository(ecr, "gco/new")
        assert out["repositoryArn"].endswith("/gco/new")
        ecr.create_repository.assert_called_once_with(
            repositoryName="gco/new",
            imageTagMutability="MUTABLE",
            imageScanningConfiguration={"scanOnPush": True},
        )


# ---------------------------------------------------------------------------
# _apply_lifecycle_policy
# ---------------------------------------------------------------------------


class TestApplyLifecyclePolicy:
    def test_no_op_on_empty_string(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        handler._apply_lifecycle_policy(ecr, "gco/svc", "")
        ecr.put_lifecycle_policy.assert_not_called()

    def test_no_op_on_whitespace(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        handler._apply_lifecycle_policy(ecr, "gco/svc", "   \n  ")
        ecr.put_lifecycle_policy.assert_not_called()

    def test_no_op_on_none(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        handler._apply_lifecycle_policy(ecr, "gco/svc", None)
        ecr.put_lifecycle_policy.assert_not_called()

    def test_applies_valid_json(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        policy = json.dumps({"rules": [{"rulePriority": 1}]})
        handler._apply_lifecycle_policy(ecr, "gco/svc", policy)
        ecr.put_lifecycle_policy.assert_called_once_with(
            repositoryName="gco/svc",
            lifecyclePolicyText=policy,
        )

    def test_invalid_json_raises_before_calling_ecr(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        with pytest.raises(json.JSONDecodeError):
            handler._apply_lifecycle_policy(ecr, "gco/svc", "{ not json")
        ecr.put_lifecycle_policy.assert_not_called()


# ---------------------------------------------------------------------------
# _has_retain_tag
# ---------------------------------------------------------------------------


class TestHasRetainTag:
    def test_finds_retain_true(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.list_tags_for_resource.return_value = {
            "tags": [
                {"Key": "Project", "Value": "gco"},
                {"Key": "gco:retain", "Value": "true"},
            ]
        }
        assert handler._has_retain_tag(ecr, "arn:..") is True

    def test_finds_retain_true_case_insensitive(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.list_tags_for_resource.return_value = {"tags": [{"Key": "gco:retain", "Value": "True"}]}
        assert handler._has_retain_tag(ecr, "arn:..") is True

    def test_other_value_returns_false(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.list_tags_for_resource.return_value = {
            "tags": [{"Key": "gco:retain", "Value": "false"}]
        }
        assert handler._has_retain_tag(ecr, "arn:..") is False

    def test_no_tags_returns_false(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.list_tags_for_resource.return_value = {"tags": []}
        assert handler._has_retain_tag(ecr, "arn:..") is False

    def test_list_tags_failure_refuses_deletion(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.list_tags_for_resource.side_effect = RuntimeError("denied")

        with pytest.raises(RuntimeError, match="refusing deletion") as excinfo:
            handler._has_retain_tag(ecr, "arn:..")

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert str(excinfo.value.__cause__) == "denied"

    def test_handles_none_tags(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.list_tags_for_resource.return_value = {"tags": None}
        assert handler._has_retain_tag(ecr, "arn:..") is False


# ---------------------------------------------------------------------------
# _delete_all_images
# ---------------------------------------------------------------------------


class TestDeleteAllImages:
    def test_paginates_and_chunks(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module

        # Build 250 digests across 3 pages so we cover both pagination
        # (>1 page) and chunking (>100 IDs per batch_delete_image call).
        all_digests = [f"sha256:{i:064x}" for i in range(250)]
        page_one = [{"imageDigest": d} for d in all_digests[:100]]
        page_two = [{"imageDigest": d} for d in all_digests[100:200]]
        page_three = [{"imageDigest": d} for d in all_digests[200:]]

        paginator = MagicMock()
        paginator.paginate.return_value = iter(
            [
                {"imageDetails": page_one},
                {"imageDetails": page_two},
                {"imageDetails": page_three},
            ]
        )
        ecr.get_paginator.return_value = paginator

        # ECR returns one entry per deleted ID; mock matches input.
        def _delete(repositoryName: str, imageIds: list[dict[str, str]]) -> dict[str, Any]:
            return {"imageIds": list(imageIds)}

        ecr.batch_delete_image.side_effect = _delete

        deleted = handler._delete_all_images(ecr, "gco/svc")
        assert deleted == 250
        # Three batches: 100, 100, 50.
        assert ecr.batch_delete_image.call_count == 3

    def test_skips_entries_without_digest(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        paginator = MagicMock()
        paginator.paginate.return_value = iter(
            [
                {
                    "imageDetails": [
                        {"imageDigest": "sha256:aaa"},
                        {"imageDigest": ""},
                        {},  # no key at all
                    ]
                }
            ]
        )
        ecr.get_paginator.return_value = paginator
        ecr.batch_delete_image.return_value = {"imageIds": [{"imageDigest": "sha256:aaa"}]}
        deleted = handler._delete_all_images(ecr, "gco/svc")
        assert deleted == 1

    def test_empty_repo_returns_zero_no_calls(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"imageDetails": []}])
        ecr.get_paginator.return_value = paginator
        deleted = handler._delete_all_images(ecr, "gco/svc")
        assert deleted == 0
        ecr.batch_delete_image.assert_not_called()


# ---------------------------------------------------------------------------
# Create / Update flow via lambda_handler
# ---------------------------------------------------------------------------


class TestCreateOrUpdate:
    def test_creates_when_repository_missing(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.side_effect = ecr.exceptions.RepositoryNotFoundException(
            "missing"
        )
        ecr.create_repository.return_value = {
            "repository": {
                "repositoryArn": "arn:aws:ecr:us-east-1:1:repository/gco/svc",
                "repositoryUri": "1.dkr.ecr.us-east-1.amazonaws.com/gco/svc",
            }
        }

        result = handler.lambda_handler(
            {
                "RequestType": "Create",
                "ResourceProperties": {
                    "RepositoryName": "gco/svc",
                    "RemovalPolicy": "retain",
                    "EmptyOnDelete": False,
                },
            },
            None,
        )

        assert result["Data"]["Adopted"] == "false"
        assert result["Data"]["RepositoryArn"].endswith("/gco/svc")
        assert result["PhysicalResourceId"].endswith("/gco/svc")
        ecr.create_repository.assert_called_once()

    def test_adopts_when_repository_already_exists(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.return_value = {
            "repositories": [
                {
                    "repositoryArn": "arn:aws:ecr:us-east-1:1:repository/gco/svc",
                    "repositoryUri": "1.dkr.ecr.us-east-1.amazonaws.com/gco/svc",
                }
            ]
        }

        result = handler.lambda_handler(
            {
                "RequestType": "Update",
                "ResourceProperties": {
                    "RepositoryName": "gco/svc",
                    "RemovalPolicy": "retain",
                    "EmptyOnDelete": False,
                },
            },
            None,
        )

        assert result["Data"]["Adopted"] == "true"
        ecr.create_repository.assert_not_called()

    def test_applies_lifecycle_policy_after_create(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.side_effect = ecr.exceptions.RepositoryNotFoundException(
            "missing"
        )
        ecr.create_repository.return_value = {
            "repository": {
                "repositoryArn": "arn:aws:ecr:us-east-1:1:repository/gco/svc",
                "repositoryUri": "1.dkr.ecr.us-east-1.amazonaws.com/gco/svc",
            }
        }
        policy = json.dumps({"rules": [{"rulePriority": 1}]})

        handler.lambda_handler(
            {
                "RequestType": "Create",
                "ResourceProperties": {
                    "RepositoryName": "gco/svc",
                    "RemovalPolicy": "retain",
                    "EmptyOnDelete": False,
                    "LifecyclePolicy": policy,
                },
            },
            None,
        )

        ecr.put_lifecycle_policy.assert_called_once_with(
            repositoryName="gco/svc",
            lifecyclePolicyText=policy,
        )

    def test_falls_back_to_repo_name_when_arn_missing(self, image_lookup_module: Any) -> None:
        """Defensive: if create_repository returns no ARN (shouldn't
        happen in production), the handler falls back to the repo name
        as the PhysicalResourceId so CloudFormation has *some* stable
        identifier to track."""
        handler, ecr = image_lookup_module
        ecr.describe_repositories.side_effect = ecr.exceptions.RepositoryNotFoundException(
            "missing"
        )
        ecr.create_repository.return_value = {"repository": {}}

        result = handler.lambda_handler(
            {
                "RequestType": "Create",
                "ResourceProperties": {"RepositoryName": "gco/svc"},
            },
            None,
        )

        assert result["PhysicalResourceId"] == "gco/svc"
        assert result["Data"]["RepositoryArn"] == ""
        assert result["Data"]["RepositoryUri"] == ""


# ---------------------------------------------------------------------------
# Delete flow via lambda_handler
# ---------------------------------------------------------------------------


class TestDelete:
    def test_idempotent_when_repository_absent(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.side_effect = ecr.exceptions.RepositoryNotFoundException("gone")
        result = handler.lambda_handler(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": "arn:aws:ecr:us-east-1:1:repository/gco/svc",
                "ResourceProperties": {
                    "RepositoryName": "gco/svc",
                    "RemovalPolicy": "destroy",
                    "EmptyOnDelete": True,
                },
            },
            None,
        )
        assert result["Data"]["Deleted"] == "false"
        ecr.delete_repository.assert_not_called()

    def test_retain_tag_preserves_repo_even_with_destroy_policy(
        self, image_lookup_module: Any
    ) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.return_value = {
            "repositories": [{"repositoryArn": "arn:aws:ecr:us-east-1:1:repository/gco/svc"}]
        }
        ecr.list_tags_for_resource.return_value = {"tags": [{"Key": "gco:retain", "Value": "true"}]}

        result = handler.lambda_handler(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": "arn:aws:ecr:us-east-1:1:repository/gco/svc",
                "ResourceProperties": {
                    "RepositoryName": "gco/svc",
                    "RemovalPolicy": "destroy",
                    "EmptyOnDelete": True,
                },
            },
            None,
        )
        assert result["Data"]["Deleted"] == "false"
        assert result["Data"]["Reason"] == "retain-tag"
        ecr.delete_repository.assert_not_called()
        ecr.batch_delete_image.assert_not_called()

    def test_removal_policy_retain_skips_delete(self, image_lookup_module: Any) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.return_value = {
            "repositories": [{"repositoryArn": "arn:aws:ecr:us-east-1:1:repository/gco/svc"}]
        }
        ecr.list_tags_for_resource.return_value = {"tags": []}

        result = handler.lambda_handler(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": "arn:aws:ecr:us-east-1:1:repository/gco/svc",
                "ResourceProperties": {
                    "RepositoryName": "gco/svc",
                    "RemovalPolicy": "retain",
                    "EmptyOnDelete": False,
                },
            },
            None,
        )
        assert result["Data"]["Deleted"] == "false"
        assert result["Data"]["Reason"] == "removal-policy-retain"
        ecr.delete_repository.assert_not_called()

    def test_destroy_with_empty_on_delete_purges_then_deletes(
        self, image_lookup_module: Any
    ) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.return_value = {
            "repositories": [{"repositoryArn": "arn:aws:ecr:us-east-1:1:repository/gco/svc"}]
        }
        ecr.list_tags_for_resource.return_value = {"tags": []}
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"imageDetails": [{"imageDigest": "sha256:aaa"}]}])
        ecr.get_paginator.return_value = paginator
        ecr.batch_delete_image.return_value = {"imageIds": [{"imageDigest": "sha256:aaa"}]}

        result = handler.lambda_handler(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": "arn:aws:ecr:us-east-1:1:repository/gco/svc",
                "ResourceProperties": {
                    "RepositoryName": "gco/svc",
                    "RemovalPolicy": "destroy",
                    "EmptyOnDelete": True,
                },
            },
            None,
        )
        assert result["Data"]["Deleted"] == "true"
        ecr.batch_delete_image.assert_called_once()
        ecr.delete_repository.assert_called_once_with(
            repositoryName="gco/svc",
            force=False,
        )

    def test_destroy_without_empty_on_delete_skips_image_purge(
        self, image_lookup_module: Any
    ) -> None:
        handler, ecr = image_lookup_module
        ecr.describe_repositories.return_value = {
            "repositories": [{"repositoryArn": "arn:aws:ecr:us-east-1:1:repository/gco/svc"}]
        }
        ecr.list_tags_for_resource.return_value = {"tags": []}

        result = handler.lambda_handler(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": "arn:aws:ecr:us-east-1:1:repository/gco/svc",
                "ResourceProperties": {
                    "RepositoryName": "gco/svc",
                    "RemovalPolicy": "destroy",
                    "EmptyOnDelete": False,
                },
            },
            None,
        )
        assert result["Data"]["Deleted"] == "true"
        ecr.batch_delete_image.assert_not_called()
        ecr.delete_repository.assert_called_once_with(
            repositoryName="gco/svc",
            force=False,
        )


# ---------------------------------------------------------------------------
# Top-level lambda_handler dispatcher
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_unsupported_request_type_raises(self, image_lookup_module: Any) -> None:
        handler, _ = image_lookup_module
        with pytest.raises(ValueError, match="Unsupported RequestType"):
            handler.lambda_handler(
                {"RequestType": "Snapshot", "ResourceProperties": {}},
                None,
            )

    def test_missing_request_type_raises(self, image_lookup_module: Any) -> None:
        handler, _ = image_lookup_module
        with pytest.raises(ValueError, match="Unsupported RequestType"):
            handler.lambda_handler({"ResourceProperties": {}}, None)

    def test_handles_none_resource_properties(self, image_lookup_module: Any) -> None:
        """CFN sometimes sends ``None`` for ``ResourceProperties`` on
        the Delete event for a custom resource that never finished
        creating. The handler coerces ``None`` to an empty dict so it
        gets through the dispatcher rather than blowing up on the
        attribute access — the underlying property lookup will then
        raise its own ``KeyError`` for missing ``RepositoryName``."""
        handler, ecr = image_lookup_module
        # The dispatcher must coerce None → {} so it reaches the
        # delete handler. The downstream KeyError on RepositoryName is
        # the right surface — CFN always sends a RepositoryName, so a
        # missing key indicates a malformed event we want to fail loudly.
        with pytest.raises(KeyError, match="RepositoryName"):
            handler.lambda_handler(
                {
                    "RequestType": "Delete",
                    "PhysicalResourceId": "id",
                    "ResourceProperties": None,
                },
                None,
            )
        # describe_repositories must not have been called — we never
        # reached the AWS-side logic.
        ecr.describe_repositories.assert_not_called()
