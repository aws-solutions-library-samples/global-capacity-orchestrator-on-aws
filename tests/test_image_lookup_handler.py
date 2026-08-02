"""
Tests for ``lambda/image-lookup/handler.py``.

Mocks the boto3 ECR client to validate the adopt-or-create behaviour on
``Create``/``Update`` events and the retain-tag-aware behaviour on
``Delete`` events.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The Lambda code lives outside the importable package tree (it ships as
# a CFN asset, not as part of the Python package). Load it via spec so
# the module is importable in tests without polluting the package path.
_HANDLER_PATH = Path(__file__).resolve().parent.parent / "lambda" / "image-lookup" / "handler.py"


@pytest.fixture
def handler_module():
    spec = importlib.util.spec_from_file_location("image_lookup_handler", str(_HANDLER_PATH))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["image_lookup_handler"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mock_ecr() -> MagicMock:
    """A fresh MagicMock ECR client with the shape the handler expects.

    ``boto3``'s typed exception attributes are not present on plain
    MagicMocks; we attach a stand-in ``RepositoryNotFoundException``
    type so the ``except`` branch in ``_describe_repository`` can match
    on it.
    """
    mock = MagicMock()

    class _RepoNotFound(Exception):
        pass

    mock.exceptions.RepositoryNotFoundException = _RepoNotFound
    return mock


# ---------------------------------------------------------------------------
# Create / Update
# ---------------------------------------------------------------------------


def test_lookup_or_create_adopts_existing_repo(
    handler_module, mock_ecr: MagicMock, monkeypatch
) -> None:
    """``DescribeRepositories`` returns an existing repo → handler adopts."""
    existing_arn = "arn:aws:ecr:us-east-2:123456789012:repository/gco/my-app"
    existing_uri = "123456789012.dkr.ecr.us-east-2.amazonaws.com/gco/my-app"
    mock_ecr.describe_repositories.return_value = {
        "repositories": [
            {
                "repositoryArn": existing_arn,
                "repositoryUri": existing_uri,
                "repositoryName": "gco/my-app",
            }
        ]
    }
    monkeypatch.setattr(handler_module, "_ecr_client", lambda: mock_ecr)

    event = {
        "RequestType": "Create",
        "ResourceProperties": {
            "RepositoryName": "gco/my-app",
            "RemovalPolicy": "retain",
            "EmptyOnDelete": False,
        },
    }
    result = handler_module.lambda_handler(event, None)

    assert result["PhysicalResourceId"] == existing_arn
    assert result["Data"]["RepositoryArn"] == existing_arn
    assert result["Data"]["RepositoryUri"] == existing_uri
    assert result["Data"]["Adopted"] == "true"
    # The handler must NOT have called CreateRepository when the repo
    # was already present.
    mock_ecr.create_repository.assert_not_called()


def test_lookup_or_create_creates_when_missing(
    handler_module, mock_ecr: MagicMock, monkeypatch
) -> None:
    """``RepositoryNotFoundException`` from describe → CreateRepository fires."""
    mock_ecr.describe_repositories.side_effect = mock_ecr.exceptions.RepositoryNotFoundException(
        "missing"
    )
    new_arn = "arn:aws:ecr:us-east-2:123456789012:repository/gco/new-app"
    new_uri = "123456789012.dkr.ecr.us-east-2.amazonaws.com/gco/new-app"
    mock_ecr.create_repository.return_value = {
        "repository": {
            "repositoryArn": new_arn,
            "repositoryUri": new_uri,
            "repositoryName": "gco/new-app",
        }
    }
    monkeypatch.setattr(handler_module, "_ecr_client", lambda: mock_ecr)

    event = {
        "RequestType": "Create",
        "ResourceProperties": {
            "RepositoryName": "gco/new-app",
            "RemovalPolicy": "retain",
            "EmptyOnDelete": False,
        },
    }
    result = handler_module.lambda_handler(event, None)

    assert result["PhysicalResourceId"] == new_arn
    assert result["Data"]["Adopted"] == "false"
    mock_ecr.create_repository.assert_called_once()
    create_kwargs = mock_ecr.create_repository.call_args.kwargs
    assert create_kwargs["repositoryName"] == "gco/new-app"
    assert create_kwargs["imageTagMutability"] == "MUTABLE"
    assert create_kwargs["imageScanningConfiguration"] == {"scanOnPush": True}


def test_lookup_or_create_applies_lifecycle_policy(
    handler_module, mock_ecr: MagicMock, monkeypatch
) -> None:
    """When ``LifecyclePolicy`` is provided, ``put_lifecycle_policy`` fires."""
    mock_ecr.describe_repositories.return_value = {
        "repositories": [
            {
                "repositoryArn": "arn:aws:ecr:us-east-2:111:repository/gco/x",
                "repositoryUri": "111.dkr.ecr.us-east-2.amazonaws.com/gco/x",
            }
        ]
    }
    monkeypatch.setattr(handler_module, "_ecr_client", lambda: mock_ecr)

    policy = '{"rules": [{"rulePriority": 1, "selection": {}, "action": {}}]}'
    event = {
        "RequestType": "Update",
        "ResourceProperties": {
            "RepositoryName": "gco/x",
            "RemovalPolicy": "retain",
            "EmptyOnDelete": False,
            "LifecyclePolicy": policy,
        },
    }
    handler_module.lambda_handler(event, None)
    mock_ecr.put_lifecycle_policy.assert_called_once_with(
        repositoryName="gco/x",
        lifecyclePolicyText=policy,
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_destroy_respects_retain_tag(handler_module, mock_ecr: MagicMock, monkeypatch) -> None:
    """``gco:retain=true`` on the repo → delete is skipped even with destroy."""
    arn = "arn:aws:ecr:us-east-2:123456789012:repository/gco/keep-me"
    mock_ecr.describe_repositories.return_value = {
        "repositories": [
            {"repositoryArn": arn, "repositoryUri": "u", "repositoryName": "gco/keep-me"}
        ]
    }
    mock_ecr.list_tags_for_resource.return_value = {
        "tags": [{"Key": "gco:retain", "Value": "true"}]
    }
    monkeypatch.setattr(handler_module, "_ecr_client", lambda: mock_ecr)

    event = {
        "RequestType": "Delete",
        "PhysicalResourceId": arn,
        "ResourceProperties": {
            "RepositoryName": "gco/keep-me",
            "RemovalPolicy": "destroy",
            "EmptyOnDelete": True,
        },
    }
    result = handler_module.lambda_handler(event, None)

    assert result["PhysicalResourceId"] == arn
    assert result["Data"]["Deleted"] == "false"
    assert result["Data"]["Reason"] == "retain-tag"
    mock_ecr.delete_repository.assert_not_called()
    mock_ecr.batch_delete_image.assert_not_called()


def test_destroy_with_retain_policy_skips_delete(
    handler_module, mock_ecr: MagicMock, monkeypatch
) -> None:
    """``RemovalPolicy=retain`` skips the delete regardless of empty_on_delete."""
    arn = "arn:aws:ecr:us-east-2:111:repository/gco/x"
    mock_ecr.describe_repositories.return_value = {
        "repositories": [{"repositoryArn": arn, "repositoryUri": "u", "repositoryName": "gco/x"}]
    }
    mock_ecr.list_tags_for_resource.return_value = {"tags": []}
    monkeypatch.setattr(handler_module, "_ecr_client", lambda: mock_ecr)

    event = {
        "RequestType": "Delete",
        "PhysicalResourceId": arn,
        "ResourceProperties": {
            "RepositoryName": "gco/x",
            "RemovalPolicy": "retain",
            "EmptyOnDelete": True,
        },
    }
    result = handler_module.lambda_handler(event, None)
    assert result["Data"]["Deleted"] == "false"
    assert result["Data"]["Reason"] == "removal-policy-retain"
    mock_ecr.delete_repository.assert_not_called()


def test_destroy_with_empty_on_delete_purges_then_deletes(
    handler_module, mock_ecr: MagicMock, monkeypatch
) -> None:
    """destroy + empty_on_delete=True → batch_delete_image, then delete_repository."""
    arn = "arn:aws:ecr:us-east-2:111:repository/gco/x"
    mock_ecr.describe_repositories.return_value = {
        "repositories": [{"repositoryArn": arn, "repositoryUri": "u", "repositoryName": "gco/x"}]
    }
    mock_ecr.list_tags_for_resource.return_value = {"tags": []}
    paginator = MagicMock()
    paginator.paginate.return_value = [
        {
            "imageDetails": [
                {"imageDigest": "sha256:" + "a" * 64},
                {"imageDigest": "sha256:" + "b" * 64},
            ]
        }
    ]
    mock_ecr.get_paginator.return_value = paginator
    mock_ecr.batch_delete_image.return_value = {
        "imageIds": [{"imageDigest": "sha256:" + "a" * 64}, {"imageDigest": "sha256:" + "b" * 64}]
    }
    monkeypatch.setattr(handler_module, "_ecr_client", lambda: mock_ecr)

    event = {
        "RequestType": "Delete",
        "PhysicalResourceId": arn,
        "ResourceProperties": {
            "RepositoryName": "gco/x",
            "RemovalPolicy": "destroy",
            "EmptyOnDelete": True,
        },
    }
    result = handler_module.lambda_handler(event, None)
    assert result["Data"]["Deleted"] == "true"
    mock_ecr.batch_delete_image.assert_called_once()
    mock_ecr.delete_repository.assert_called_once_with(repositoryName="gco/x", force=False)


def test_destroy_refuses_delete_when_retain_tags_cannot_be_read(
    handler_module, mock_ecr: MagicMock, monkeypatch
) -> None:
    """A tag authorization/read failure must never authorize repository deletion."""
    arn = "arn:aws:ecr:us-east-2:111:repository/gco/x"
    mock_ecr.describe_repositories.return_value = {
        "repositories": [{"repositoryArn": arn, "repositoryUri": "u", "repositoryName": "gco/x"}]
    }
    mock_ecr.list_tags_for_resource.side_effect = PermissionError("denied")
    monkeypatch.setattr(handler_module, "_ecr_client", lambda: mock_ecr)
    event = {
        "RequestType": "Delete",
        "PhysicalResourceId": arn,
        "ResourceProperties": {
            "RepositoryName": "gco/x",
            "RemovalPolicy": "destroy",
            "EmptyOnDelete": True,
        },
    }

    with pytest.raises(RuntimeError, match="refusing deletion"):
        handler_module.lambda_handler(event, None)

    mock_ecr.batch_delete_image.assert_not_called()
    mock_ecr.delete_repository.assert_not_called()


def test_destroy_when_repo_already_absent(handler_module, mock_ecr: MagicMock, monkeypatch) -> None:
    """A Delete event for a missing repo is a no-op success."""
    mock_ecr.describe_repositories.side_effect = mock_ecr.exceptions.RepositoryNotFoundException(
        "missing"
    )
    monkeypatch.setattr(handler_module, "_ecr_client", lambda: mock_ecr)
    event = {
        "RequestType": "Delete",
        "PhysicalResourceId": "arn:gone",
        "ResourceProperties": {
            "RepositoryName": "gco/gone",
            "RemovalPolicy": "destroy",
            "EmptyOnDelete": True,
        },
    }
    result = handler_module.lambda_handler(event, None)
    assert result["PhysicalResourceId"] == "arn:gone"
    mock_ecr.delete_repository.assert_not_called()


def test_unsupported_request_type_raises(handler_module, mock_ecr: MagicMock, monkeypatch) -> None:
    monkeypatch.setattr(handler_module, "_ecr_client", lambda: mock_ecr)
    with pytest.raises(ValueError, match="Unsupported RequestType"):
        handler_module.lambda_handler(
            {"RequestType": "Banana", "ResourceProperties": {"RepositoryName": "gco/x"}}, None
        )
