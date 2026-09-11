"""Tests for the analytics-cleanup Lambda (lambda/analytics-cleanup/handler.py).

Covers:
- Create/Update events are no-ops (return SUCCESS immediately)
- Delete event deletes all user profiles from the domain
- Delete event deletes all EFS access points
- Errors during deletion are logged but don't fail the custom resource
  (always returns SUCCESS so stack destroy isn't blocked)
- Delete event drains apps and spaces (skipping already-terminal ones,
  tolerating "does not exist" races, reporting timeouts)
- Optional EFS-policy / security-group steps are skipped when EFS_ID /
  VPC_ID are not configured
- SageMaker NFS security groups have their cross-referencing rules
  revoked before deletion; revoke failures are logged, not fatal
- EFS resource-policy removal and HomeEfsFileSystemId lookup are
  best-effort (PolicyNotFound / ClientError are swallowed with a warning)
- List/describe failures in every helper are captured as error strings
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Import the handler module from lambda/analytics-cleanup/
# ---------------------------------------------------------------------------

_HANDLER_PATH = (
    Path(__file__).resolve().parent.parent / "lambda" / "analytics-cleanup" / "handler.py"
)
_SPEC = importlib.util.spec_from_file_location("analytics_cleanup_handler", _HANDLER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_module = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("analytics_cleanup_handler", _module)
_SPEC.loader.exec_module(_module)

handler = _module.handler
_delete_apps = _module._delete_apps
_delete_user_profiles = _module._delete_user_profiles
_delete_access_points = _module._delete_access_points


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ENV = {
    "DOMAIN_ID": "d-test123",
    "EFS_ID": "fs-abc123",
    "REGION": "us-east-2",
    "VPC_ID": "vpc-test123",
}


def _client_error(code: str, message: str, operation: str) -> ClientError:
    """Build a botocore ``ClientError`` shaped like a real AWS API failure."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    """Skip real waiting inside the handler so tests stay fast.

    ``_delete_user_profiles`` and ``_delete_spaces`` now poll and sleep
    while the SageMaker delete calls drain asynchronously. Tests use
    paginators that return empty on the first re-list, so the loop exits
    on the first iteration — but only if ``time.sleep`` is a no-op.
    """
    monkeypatch.setattr(_module.time, "sleep", lambda _: None)


# ---------------------------------------------------------------------------
# handler() top-level tests
# ---------------------------------------------------------------------------


class TestHandler:
    def test_create_event_is_noop(self):
        result = handler({"RequestType": "Create"}, None)
        assert result["Status"] == "SUCCESS"

    def test_update_event_is_noop(self):
        result = handler({"RequestType": "Update"}, None)
        assert result["Status"] == "SUCCESS"

    @patch("analytics_cleanup_handler._delete_sagemaker_security_groups", return_value=[])
    @patch("analytics_cleanup_handler._delete_sagemaker_managed_efs", return_value=[])
    @patch("analytics_cleanup_handler._get_sagemaker_home_efs_id", return_value="fs-sm-123")
    @patch("analytics_cleanup_handler._delete_efs_resource_policy")
    @patch("analytics_cleanup_handler._delete_user_profiles", return_value=[])
    @patch("analytics_cleanup_handler._delete_spaces", return_value=[])
    @patch("analytics_cleanup_handler._delete_apps", return_value=[])
    def test_delete_event_calls_cleanup(
        self,
        mock_apps,
        mock_spaces,
        mock_profiles,
        mock_efs_policy,
        mock_get_sm_efs,
        mock_efs,
        mock_sgs,
    ):
        result = handler({"RequestType": "Delete"}, None)
        assert result["Status"] == "SUCCESS"
        mock_apps.assert_called_once_with("us-east-2", "d-test123")
        mock_spaces.assert_called_once_with("us-east-2", "d-test123")
        mock_profiles.assert_called_once_with("us-east-2", "d-test123")
        mock_efs.assert_called_once_with("us-east-2", "d-test123")

    @patch("analytics_cleanup_handler._delete_sagemaker_security_groups", return_value=["err0"])
    @patch("analytics_cleanup_handler._delete_sagemaker_managed_efs", return_value=[])
    @patch("analytics_cleanup_handler._get_sagemaker_home_efs_id", return_value="")
    @patch("analytics_cleanup_handler._delete_efs_resource_policy")
    @patch("analytics_cleanup_handler._delete_user_profiles", return_value=["err2"])
    @patch("analytics_cleanup_handler._delete_spaces", return_value=[])
    @patch("analytics_cleanup_handler._delete_apps", return_value=[])
    def test_delete_raises_on_critical_errors(
        self,
        mock_apps,
        mock_spaces,
        mock_profiles,
        mock_efs_policy,
        mock_get_sm_efs,
        mock_efs,
        mock_sgs,
    ):
        """Errors draining apps/spaces/user-profiles must fail the custom
        resource so CloudFormation doesn't proceed to a guaranteed-fail
        domain delete.
        """
        with pytest.raises(RuntimeError, match="Analytics cleanup failed"):
            handler({"RequestType": "Delete"}, None)

    @patch("analytics_cleanup_handler._delete_sagemaker_security_groups", return_value=["sg-err"])
    @patch("analytics_cleanup_handler._delete_sagemaker_managed_efs", return_value=["efs-err"])
    @patch("analytics_cleanup_handler._get_sagemaker_home_efs_id", return_value="")
    @patch("analytics_cleanup_handler._delete_efs_resource_policy")
    @patch("analytics_cleanup_handler._delete_user_profiles", return_value=[])
    @patch("analytics_cleanup_handler._delete_spaces", return_value=[])
    @patch("analytics_cleanup_handler._delete_apps", return_value=[])
    def test_delete_tolerates_non_critical_errors(
        self,
        mock_apps,
        mock_spaces,
        mock_profiles,
        mock_efs_policy,
        mock_get_sm_efs,
        mock_efs,
        mock_sgs,
    ):
        """EFS and SG cleanup errors are best-effort and must not block the
        domain delete — they're logged but the handler still returns SUCCESS.
        """
        result = handler({"RequestType": "Delete"}, None)
        assert result["Status"] == "SUCCESS"

    @patch("analytics_cleanup_handler._delete_sagemaker_security_groups", return_value=[])
    @patch("analytics_cleanup_handler._delete_sagemaker_managed_efs", return_value=[])
    @patch("analytics_cleanup_handler._get_sagemaker_home_efs_id", return_value="")
    @patch("analytics_cleanup_handler._delete_efs_resource_policy")
    @patch("analytics_cleanup_handler._delete_user_profiles", return_value=[])
    @patch("analytics_cleanup_handler._delete_spaces", return_value=[])
    @patch("analytics_cleanup_handler._delete_apps", return_value=[])
    def test_delete_skips_optional_steps_without_efs_and_vpc_ids(
        self,
        mock_apps,
        mock_spaces,
        mock_profiles,
        mock_efs_policy,
        mock_get_sm_efs,
        mock_efs,
        mock_sgs,
        monkeypatch,
    ):
        """EFS_ID and VPC_ID are optional. When neither is configured (and
        the domain reports no home EFS) the resource-policy and security-
        group steps are skipped entirely while the mandatory SageMaker
        drain and managed-EFS cleanup still run.
        """
        monkeypatch.delenv("EFS_ID")
        monkeypatch.delenv("VPC_ID")

        result = handler({"RequestType": "Delete", "PhysicalResourceId": "phys-42"}, None)

        assert result == {"Status": "SUCCESS", "PhysicalResourceId": "phys-42"}
        mock_efs_policy.assert_not_called()
        mock_sgs.assert_not_called()
        mock_get_sm_efs.assert_called_once_with("us-east-2", "d-test123")
        mock_efs.assert_called_once_with("us-east-2", "d-test123")


# ---------------------------------------------------------------------------
# _delete_apps tests
# ---------------------------------------------------------------------------


class TestDeleteApps:
    def test_deletes_active_apps_and_waits_for_drain(self):
        """Apps already in ``Deleted``/``Failed`` are skipped, space-scoped
        apps are addressed by ``SpaceName`` and profile-scoped ones by
        ``UserProfileName``, and the wait loop exits as soon as every listed
        app is terminal.
        """
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = [
            # Initial enumeration for deletion.
            [
                {
                    "Apps": [
                        {
                            "AppName": "default",
                            "AppType": "JupyterLab",
                            "Status": "InService",
                            "SpaceName": "team-space",
                        },
                        {
                            "AppName": "old-kernel",
                            "AppType": "KernelGateway",
                            "Status": "Failed",
                            "UserProfileName": "alice",
                        },
                        {
                            "AppName": "default",
                            "AppType": "JupyterServer",
                            "Status": "InService",
                            "UserProfileName": "alice",
                        },
                    ]
                }
            ],
            # Wait loop: everything still listed has gone terminal.
            [
                {
                    "Apps": [
                        {
                            "AppName": "default",
                            "AppType": "JupyterLab",
                            "Status": "Deleted",
                            "SpaceName": "team-space",
                        },
                        {
                            "AppName": "old-kernel",
                            "AppType": "KernelGateway",
                            "Status": "Failed",
                            "UserProfileName": "alice",
                        },
                    ]
                }
            ],
        ]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_apps("us-east-2", "d-test123")

        assert errors == []
        assert mock_sm.delete_app.call_count == 2
        mock_sm.delete_app.assert_any_call(
            DomainId="d-test123",
            AppType="JupyterLab",
            AppName="default",
            SpaceName="team-space",
        )
        mock_sm.delete_app.assert_any_call(
            DomainId="d-test123",
            AppType="JupyterServer",
            AppName="default",
            UserProfileName="alice",
        )
        # One enumeration pass plus a single wait-loop poll.
        assert mock_paginator.paginate.call_count == 2

    def test_missing_app_is_not_an_error(self):
        """A ``does not exist`` failure means the app vanished between the
        list and the delete call; that race must not be reported."""
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = [
            [
                {
                    "Apps": [
                        {
                            "AppName": "default",
                            "AppType": "JupyterLab",
                            "Status": "InService",
                            "UserProfileName": "alice",
                        }
                    ]
                }
            ],
            [{"Apps": []}],
        ]
        mock_sm.get_paginator.return_value = mock_paginator
        mock_sm.delete_app.side_effect = _client_error(
            "ResourceNotFound", "App default does not exist", "DeleteApp"
        )

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_apps("us-east-2", "d-test123")

        assert errors == []
        mock_sm.delete_app.assert_called_once()

    def test_delete_failure_is_captured(self):
        """Any other ``delete_app`` failure is recorded so the handler can
        fail the custom resource instead of racing the domain delete."""
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = [
            [
                {
                    "Apps": [
                        {
                            "AppName": "default",
                            "AppType": "JupyterLab",
                            "Status": "InService",
                            "UserProfileName": "alice",
                        }
                    ]
                }
            ],
            [{"Apps": []}],
        ]
        mock_sm.get_paginator.return_value = mock_paginator
        mock_sm.delete_app.side_effect = _client_error(
            "ValidationException", "App is in Pending status", "DeleteApp"
        )

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_apps("us-east-2", "d-test123")

        assert len(errors) == 1
        assert errors[0].startswith("Failed to delete app default:")
        assert "ValidationException" in errors[0]

    def test_list_failure_is_captured(self):
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = _client_error(
            "AccessDeniedException", "denied", "ListApps"
        )
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_apps("us-east-2", "d-test123")

        assert len(errors) == 1
        assert errors[0].startswith("Failed to list apps:")
        mock_sm.delete_app.assert_not_called()

    def test_timeout_reports_error(self, monkeypatch):
        """A lingering app must fail cleanup instead of allowing a domain-delete race."""
        monkeypatch.setattr(_module, "APP_DELETE_WAIT_SECONDS", 0)
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "Apps": [
                    {
                        "AppName": "studio-app",
                        "AppType": "JupyterLab",
                        "Status": "Deleting",
                        "UserProfileName": "alice",
                    }
                ]
            }
        ]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_apps("us-east-2", "d-test123")

        assert len(errors) == 1
        assert "Timed out" in errors[0]
        assert "studio-app" in errors[0]


# ---------------------------------------------------------------------------
# _delete_spaces tests
# ---------------------------------------------------------------------------

_delete_spaces = _module._delete_spaces


class TestDeleteSpaces:
    def test_deletes_all_spaces_and_waits_for_drain(self):
        """Every non-Deleting space gets a ``delete_space`` call, then the
        wait loop keeps polling until ``list_spaces`` comes back empty."""
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = [
            # Initial enumeration for deletion.
            [
                {
                    "Spaces": [
                        {"SpaceName": "team-a", "Status": "InService"},
                        {"SpaceName": "team-b", "Status": "InService"},
                    ]
                }
            ],
            # Wait loop: one space still draining, then gone.
            [{"Spaces": [{"SpaceName": "team-b", "Status": "Deleting"}]}],
            [{"Spaces": []}],
        ]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_spaces("us-east-2", "d-test123")

        assert errors == []
        assert mock_sm.delete_space.call_count == 2
        mock_sm.delete_space.assert_any_call(DomainId="d-test123", SpaceName="team-a")
        mock_sm.delete_space.assert_any_call(DomainId="d-test123", SpaceName="team-b")
        assert mock_paginator.paginate.call_count == 3

    def test_skips_spaces_already_deleting(self):
        """Spaces already in ``Deleting`` are not re-deleted (that would
        raise) but still gate the wait loop until they disappear."""
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = [
            [{"Spaces": [{"SpaceName": "team-a", "Status": "Deleting"}]}],
            [{"Spaces": [{"SpaceName": "team-a", "Status": "Deleting"}]}],
            [{"Spaces": []}],
        ]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_spaces("us-east-2", "d-test123")

        assert errors == []
        mock_sm.delete_space.assert_not_called()
        assert mock_paginator.paginate.call_count == 3

    def test_empty_domain_returns_no_errors(self):
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"Spaces": []}]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_spaces("us-east-2", "d-test123")

        assert errors == []
        mock_sm.delete_space.assert_not_called()

    def test_missing_space_is_not_an_error(self):
        """A ``does not exist`` failure means the space vanished between the
        list and the delete call; that race must not be reported."""
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = [
            [{"Spaces": [{"SpaceName": "team-a", "Status": "InService"}]}],
            [{"Spaces": []}],
        ]
        mock_sm.get_paginator.return_value = mock_paginator
        mock_sm.delete_space.side_effect = _client_error(
            "ResourceNotFound", "Space team-a does not exist", "DeleteSpace"
        )

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_spaces("us-east-2", "d-test123")

        assert errors == []

    def test_delete_failure_is_captured(self):
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = [
            [{"Spaces": [{"SpaceName": "team-a", "Status": "InService"}]}],
            [{"Spaces": []}],
        ]
        mock_sm.get_paginator.return_value = mock_paginator
        mock_sm.delete_space.side_effect = _client_error(
            "ValidationException", "Space has running apps", "DeleteSpace"
        )

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_spaces("us-east-2", "d-test123")

        assert len(errors) == 1
        assert errors[0].startswith("Failed to delete space team-a:")
        assert "ValidationException" in errors[0]

    def test_timeout_reports_error(self):
        """If a space never leaves ``list_spaces`` the drain loop gives up
        after ``SPACE_DELETE_WAIT_SECONDS`` worth of polls and reports it,
        naming the lingering space so the operator can find it.
        """
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        # Always return a lingering space — the wait loop will time out.
        mock_paginator.paginate.return_value = [
            {"Spaces": [{"SpaceName": "team-a", "Status": "Deleting"}]}
        ]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_spaces("us-east-2", "d-test123")

        assert len(errors) == 1
        assert "Timed out waiting for spaces to delete in d-test123" in errors[0]
        assert "team-a" in errors[0]
        # One enumeration pass plus every poll the tunable allows.
        expected_polls = _module._poll_iterations(_module.SPACE_DELETE_WAIT_SECONDS)
        assert mock_paginator.paginate.call_count == 1 + expected_polls

    def test_list_failure_is_captured(self):
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = _client_error(
            "AccessDeniedException", "denied", "ListSpaces"
        )
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_spaces("us-east-2", "d-test123")

        assert len(errors) == 1
        assert errors[0].startswith("Failed to list spaces:")
        mock_sm.delete_space.assert_not_called()


# ---------------------------------------------------------------------------
# _delete_user_profiles tests
# ---------------------------------------------------------------------------


class TestDeleteUserProfiles:
    def test_deletes_all_profiles(self):
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        # First paginate() call: enumerate profiles for deletion.
        # Subsequent paginate() calls: poll loop — return empty to exit.
        mock_paginator.paginate.side_effect = [
            [
                {
                    "UserProfiles": [
                        {"UserProfileName": "alice"},
                        {"UserProfileName": "bob"},
                    ]
                }
            ],
            [{"UserProfiles": []}],
        ]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_user_profiles("us-east-2", "d-test123")

        assert errors == []
        assert mock_sm.delete_user_profile.call_count == 2
        mock_sm.delete_user_profile.assert_any_call(DomainId="d-test123", UserProfileName="alice")
        mock_sm.delete_user_profile.assert_any_call(DomainId="d-test123", UserProfileName="bob")

    def test_empty_domain_returns_no_errors(self):
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"UserProfiles": []}]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_user_profiles("us-east-2", "d-test123")

        assert errors == []
        mock_sm.delete_user_profile.assert_not_called()

    def test_waits_for_profiles_to_drain(self):
        """Profiles in Deleting state are skipped for the delete call but
        still gate the wait loop — we must not return until they're gone.
        """
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        # Initial list has 1 profile to delete.
        # Wait loop sees it still Deleting, then gone.
        mock_paginator.paginate.side_effect = [
            [{"UserProfiles": [{"UserProfileName": "alice", "Status": "InService"}]}],
            [{"UserProfiles": [{"UserProfileName": "alice", "Status": "Deleting"}]}],
            [{"UserProfiles": []}],
        ]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_user_profiles("us-east-2", "d-test123")

        assert errors == []
        mock_sm.delete_user_profile.assert_called_once_with(
            DomainId="d-test123", UserProfileName="alice"
        )

    def test_timeout_reports_error(self, monkeypatch):
        """If profiles never drain, the function must report an error so
        the top-level handler can raise.
        """
        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        # Always return a lingering profile — the wait loop will time out.
        mock_paginator.paginate.return_value = [
            {"UserProfiles": [{"UserProfileName": "alice", "Status": "Deleting"}]}
        ]
        mock_sm.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_user_profiles("us-east-2", "d-test123")

        assert len(errors) == 1
        assert "Timed out" in errors[0]
        assert "alice" in errors[0]

    def test_delete_failure_is_captured(self):
        from botocore.exceptions import ClientError

        mock_sm = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = [
            [{"UserProfiles": [{"UserProfileName": "alice"}]}],
            [{"UserProfiles": []}],
        ]
        mock_sm.get_paginator.return_value = mock_paginator
        mock_sm.delete_user_profile.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "in use"}},
            "DeleteUserProfile",
        )

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_user_profiles("us-east-2", "d-test123")

        assert len(errors) >= 1
        assert any("alice" in e for e in errors)

    def test_list_failure_is_captured(self):
        from botocore.exceptions import ClientError

        mock_sm = MagicMock()
        mock_sm.get_paginator.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "ListUserProfiles",
        )

        with patch("boto3.client", return_value=mock_sm):
            errors = _delete_user_profiles("us-east-2", "d-test123")

        assert len(errors) == 1
        assert "list" in errors[0].lower() or "List" in errors[0]


# ---------------------------------------------------------------------------
# _delete_access_points tests
# ---------------------------------------------------------------------------


class TestDeleteAccessPoints:
    def test_deletes_all_access_points(self):
        mock_efs = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {
                "AccessPoints": [
                    {"AccessPointId": "fsap-001"},
                    {"AccessPointId": "fsap-002"},
                ]
            }
        ]
        mock_efs.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_efs):
            errors = _delete_access_points("us-east-2", "fs-abc123")

        assert errors == []
        assert mock_efs.delete_access_point.call_count == 2

    def test_empty_filesystem_returns_no_errors(self):
        mock_efs = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"AccessPoints": []}]
        mock_efs.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_efs):
            errors = _delete_access_points("us-east-2", "fs-abc123")

        assert errors == []

    def test_delete_failure_is_captured(self):
        from botocore.exceptions import ClientError

        mock_efs = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"AccessPoints": [{"AccessPointId": "fsap-001"}]}]
        mock_efs.get_paginator.return_value = mock_paginator
        mock_efs.delete_access_point.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "oops"}},
            "DeleteAccessPoint",
        )

        with patch("boto3.client", return_value=mock_efs):
            errors = _delete_access_points("us-east-2", "fs-abc123")

        assert len(errors) == 1
        assert "fsap-001" in errors[0]

    def test_list_failure_is_captured(self):
        mock_efs = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.side_effect = _client_error(
            "AccessDeniedException", "denied", "DescribeAccessPoints"
        )
        mock_efs.get_paginator.return_value = mock_paginator

        with patch("boto3.client", return_value=mock_efs):
            errors = _delete_access_points("us-east-2", "fs-abc123")

        assert len(errors) == 1
        assert errors[0].startswith("Failed to list access points:")
        mock_efs.delete_access_point.assert_not_called()


# ---------------------------------------------------------------------------
# _delete_sagemaker_managed_efs tests
# ---------------------------------------------------------------------------

_delete_sagemaker_managed_efs = _module._delete_sagemaker_managed_efs


class TestDeleteSagemakerManagedEfs:
    def test_deletes_efs_matching_domain_id(self):
        mock_sm = MagicMock()
        mock_efs = MagicMock()
        # DescribeDomain returns the HomeEfsFileSystemId.
        mock_sm.describe_domain.return_value = {
            "HomeEfsFileSystemId": "fs-target",
        }
        # After deletion, no mount targets remain
        mock_efs.describe_mount_targets.side_effect = [
            {"MountTargets": [{"MountTargetId": "fsmt-001"}]},
            {"MountTargets": []},
        ]

        def client_factory(service, **kwargs):
            if service == "sagemaker":
                return mock_sm
            return mock_efs

        with patch("boto3.client", side_effect=client_factory):
            errors = _delete_sagemaker_managed_efs("us-east-2", "d-test123")

        assert errors == []
        mock_sm.describe_domain.assert_called_once_with(DomainId="d-test123")
        mock_efs.delete_mount_target.assert_called_once_with(MountTargetId="fsmt-001")
        mock_efs.delete_file_system.assert_called_once_with(FileSystemId="fs-target")

    def test_no_matching_efs_returns_empty(self):
        mock_sm = MagicMock()
        mock_efs = MagicMock()
        mock_sm.describe_domain.return_value = {}

        def client_factory(service, **kwargs):
            if service == "sagemaker":
                return mock_sm
            return mock_efs

        with patch("boto3.client", side_effect=client_factory):
            errors = _delete_sagemaker_managed_efs("us-east-2", "d-test123")

        assert errors == []
        mock_efs.delete_mount_target.assert_not_called()
        mock_efs.delete_file_system.assert_not_called()

    def test_mount_target_delete_failure_captured(self):
        from botocore.exceptions import ClientError

        mock_efs = MagicMock()
        mock_efs.describe_file_systems.return_value = {
            "FileSystems": [
                {"FileSystemId": "fs-target", "CreationToken": "d-test123"},
            ]
        }
        mock_efs.describe_mount_targets.return_value = {
            "MountTargets": [{"MountTargetId": "fsmt-001"}]
        }
        mock_efs.delete_mount_target.side_effect = ClientError(
            {"Error": {"Code": "MountTargetNotFound", "Message": "gone"}},
            "DeleteMountTarget",
        )

        with patch("boto3.client", return_value=mock_efs):
            errors = _delete_sagemaker_managed_efs("us-east-2", "d-test123")

        assert len(errors) == 1
        assert "fsmt-001" in errors[0]

    def test_file_system_delete_failure_captured(self):
        """A failing ``delete_file_system`` is reported as a (non-critical)
        error naming the file system; mount-target handling is unaffected."""
        mock_sm = MagicMock()
        mock_efs = MagicMock()
        mock_sm.describe_domain.return_value = {"HomeEfsFileSystemId": "fs-target"}
        # No mount targets at all — the wait loop exits on its first poll.
        mock_efs.describe_mount_targets.return_value = {"MountTargets": []}
        mock_efs.delete_file_system.side_effect = _client_error(
            "FileSystemInUse", "mount targets still attached", "DeleteFileSystem"
        )

        def client_factory(service, **kwargs):
            if service == "sagemaker":
                return mock_sm
            return mock_efs

        with patch("boto3.client", side_effect=client_factory):
            errors = _delete_sagemaker_managed_efs("us-east-2", "d-test123")

        assert len(errors) == 1
        assert errors[0].startswith("Failed to delete EFS fs-target:")
        assert "FileSystemInUse" in errors[0]
        mock_efs.delete_mount_target.assert_not_called()
        mock_efs.delete_file_system.assert_called_once_with(FileSystemId="fs-target")

    def test_describe_domain_failure_captured(self):
        """If the domain can't be described there is nothing to clean up;
        the failure is reported and no EFS API is touched."""
        mock_sm = MagicMock()
        mock_efs = MagicMock()
        mock_sm.describe_domain.side_effect = _client_error(
            "ResourceNotFound", "Domain d-test123 not found", "DescribeDomain"
        )

        def client_factory(service, **kwargs):
            if service == "sagemaker":
                return mock_sm
            return mock_efs

        with patch("boto3.client", side_effect=client_factory):
            errors = _delete_sagemaker_managed_efs("us-east-2", "d-test123")

        assert len(errors) == 1
        assert errors[0].startswith("Failed to find/delete SageMaker-managed EFS:")
        mock_efs.describe_mount_targets.assert_not_called()
        mock_efs.delete_file_system.assert_not_called()


# ---------------------------------------------------------------------------
# _delete_sagemaker_security_groups tests
# ---------------------------------------------------------------------------

_delete_sagemaker_security_groups = _module._delete_sagemaker_security_groups


class TestDeleteSagemakerSecurityGroups:
    """Cover the DependencyViolation retry behaviour for the NFS SGs."""

    def _sg(self, group_id, group_name, ingress=None, egress=None):
        return {
            "GroupId": group_id,
            "GroupName": group_name,
            "IpPermissions": ingress or [],
            "IpPermissionsEgress": egress or [],
        }

    def test_deletes_both_sgs_on_first_attempt(self):
        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                self._sg("sg-in", "security-group-for-inbound-nfs-d-test"),
                self._sg("sg-out", "security-group-for-outbound-nfs-d-test"),
            ]
        }

        with patch("boto3.client", return_value=mock_ec2):
            errors = _delete_sagemaker_security_groups("us-east-2", "d-test", "vpc-xyz")

        assert errors == []
        assert mock_ec2.delete_security_group.call_count == 2

    def test_retries_on_dependency_violation_then_succeeds(self, monkeypatch):
        """The outbound SG typically fails once with DependencyViolation
        and clears within one backoff interval."""
        from botocore.exceptions import ClientError

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                self._sg("sg-out", "security-group-for-outbound-nfs-d-test"),
            ]
        }
        # First call: DependencyViolation. Second call: succeeds.
        dep_violation = ClientError(
            {
                "Error": {
                    "Code": "DependencyViolation",
                    "Message": "has a dependent object",
                }
            },
            "DeleteSecurityGroup",
        )
        mock_ec2.delete_security_group.side_effect = [dep_violation, None]

        with patch("boto3.client", return_value=mock_ec2):
            errors = _delete_sagemaker_security_groups("us-east-2", "d-test", "vpc-xyz")

        assert errors == []
        assert mock_ec2.delete_security_group.call_count == 2

    def test_reports_error_after_exhausting_retries(self):
        """If DependencyViolation persists across every attempt, emit
        an actionable error so the caller can decide how to handle it."""
        from botocore.exceptions import ClientError

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                self._sg("sg-out", "security-group-for-outbound-nfs-d-test"),
            ]
        }
        mock_ec2.delete_security_group.side_effect = ClientError(
            {
                "Error": {
                    "Code": "DependencyViolation",
                    "Message": "has a dependent object",
                }
            },
            "DeleteSecurityGroup",
        )

        with patch("boto3.client", return_value=mock_ec2):
            errors = _delete_sagemaker_security_groups("us-east-2", "d-test", "vpc-xyz")

        assert len(errors) == 1
        assert "sg-out" in errors[0]
        assert "DependencyViolation did not clear" in errors[0]
        # Every attempt was used.
        assert mock_ec2.delete_security_group.call_count == _module.SG_DELETE_MAX_ATTEMPTS

    def test_treats_already_deleted_as_success(self):
        """An ``InvalidGroup.NotFound`` response means some other actor
        (e.g. an operator recovering a prior failed destroy) has already
        deleted the SG. Treat it as success, not an error."""
        from botocore.exceptions import ClientError

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                self._sg("sg-in", "security-group-for-inbound-nfs-d-test"),
            ]
        }
        mock_ec2.delete_security_group.side_effect = ClientError(
            {"Error": {"Code": "InvalidGroup.NotFound", "Message": "gone"}},
            "DeleteSecurityGroup",
        )

        with patch("boto3.client", return_value=mock_ec2):
            errors = _delete_sagemaker_security_groups("us-east-2", "d-test", "vpc-xyz")

        assert errors == []

    def test_other_client_errors_are_surfaced_immediately(self):
        """Errors other than ``DependencyViolation`` / ``InvalidGroup.NotFound``
        surface on the first attempt without retrying — these are not
        transient and retries would waste Lambda time."""
        from botocore.exceptions import ClientError

        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                self._sg("sg-in", "security-group-for-inbound-nfs-d-test"),
            ]
        }
        mock_ec2.delete_security_group.side_effect = ClientError(
            {"Error": {"Code": "UnauthorizedOperation", "Message": "denied"}},
            "DeleteSecurityGroup",
        )

        with patch("boto3.client", return_value=mock_ec2):
            errors = _delete_sagemaker_security_groups("us-east-2", "d-test", "vpc-xyz")

        assert len(errors) == 1
        assert "sg-in" in errors[0]
        # Only one attempt — no retry for non-DependencyViolation errors.
        assert mock_ec2.delete_security_group.call_count == 1

    def test_revokes_cross_referencing_rules_before_deleting(self):
        """The inbound SG allows NFS from the outbound SG and vice versa.
        Both rule sets must be revoked before any delete so neither SG
        trips over the circular reference."""
        inbound_rules = [
            {
                "IpProtocol": "tcp",
                "FromPort": 2049,
                "ToPort": 2049,
                "UserIdGroupPairs": [{"GroupId": "sg-out", "UserId": "123456789012"}],
            }
        ]
        outbound_rules = [
            {
                "IpProtocol": "tcp",
                "FromPort": 2049,
                "ToPort": 2049,
                "UserIdGroupPairs": [{"GroupId": "sg-in", "UserId": "123456789012"}],
            }
        ]
        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                self._sg("sg-in", "security-group-for-inbound-nfs-d-test", ingress=inbound_rules),
                self._sg("sg-out", "security-group-for-outbound-nfs-d-test", egress=outbound_rules),
            ]
        }

        with patch("boto3.client", return_value=mock_ec2):
            errors = _delete_sagemaker_security_groups("us-east-2", "d-test", "vpc-xyz")

        assert errors == []
        mock_ec2.revoke_security_group_ingress.assert_called_once_with(
            GroupId="sg-in", IpPermissions=inbound_rules
        )
        mock_ec2.revoke_security_group_egress.assert_called_once_with(
            GroupId="sg-out", IpPermissions=outbound_rules
        )
        assert mock_ec2.delete_security_group.call_count == 2
        # Every revoke happens before the first delete.
        call_names = [name for name, _args, _kwargs in mock_ec2.method_calls]
        first_delete = call_names.index("delete_security_group")
        assert call_names.index("revoke_security_group_ingress") < first_delete
        assert call_names.index("revoke_security_group_egress") < first_delete

    def test_revoke_failure_is_logged_and_delete_still_attempted(self, caplog):
        """A failed revoke (e.g. the rule was already removed) is only a
        warning: the SG delete is still attempted and no error is reported."""
        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.return_value = {
            "SecurityGroups": [
                self._sg(
                    "sg-in",
                    "security-group-for-inbound-nfs-d-test",
                    ingress=[{"IpProtocol": "-1", "UserIdGroupPairs": [{"GroupId": "sg-out"}]}],
                ),
            ]
        }
        mock_ec2.revoke_security_group_ingress.side_effect = _client_error(
            "InvalidPermission.NotFound",
            "The specified rule does not exist in this security group.",
            "RevokeSecurityGroupIngress",
        )

        with (
            caplog.at_level(logging.WARNING),
            patch("boto3.client", return_value=mock_ec2),
        ):
            errors = _delete_sagemaker_security_groups("us-east-2", "d-test", "vpc-xyz")

        assert errors == []
        assert "Failed to revoke rules on sg-in" in caplog.text
        mock_ec2.delete_security_group.assert_called_once_with(GroupId="sg-in")

    def test_list_failure_is_captured(self):
        mock_ec2 = MagicMock()
        mock_ec2.describe_security_groups.side_effect = _client_error(
            "UnauthorizedOperation", "denied", "DescribeSecurityGroups"
        )

        with patch("boto3.client", return_value=mock_ec2):
            errors = _delete_sagemaker_security_groups("us-east-2", "d-test", "vpc-xyz")

        assert len(errors) == 1
        assert errors[0].startswith("Failed to list SageMaker security groups:")
        mock_ec2.delete_security_group.assert_not_called()


# ---------------------------------------------------------------------------
# _get_sagemaker_home_efs_id tests
# ---------------------------------------------------------------------------

_get_sagemaker_home_efs_id = _module._get_sagemaker_home_efs_id


class TestGetSagemakerHomeEfsId:
    def test_returns_home_efs_id(self):
        mock_sm = MagicMock()
        mock_sm.describe_domain.return_value = {
            "DomainId": "d-test123",
            "HomeEfsFileSystemId": "fs-home",
        }

        with patch("boto3.client", return_value=mock_sm):
            efs_id = _get_sagemaker_home_efs_id("us-east-2", "d-test123")

        assert efs_id == "fs-home"
        mock_sm.describe_domain.assert_called_once_with(DomainId="d-test123")

    def test_returns_empty_when_domain_has_no_home_efs(self):
        mock_sm = MagicMock()
        mock_sm.describe_domain.return_value = {"DomainId": "d-test123"}

        with patch("boto3.client", return_value=mock_sm):
            efs_id = _get_sagemaker_home_efs_id("us-east-2", "d-test123")

        assert efs_id == ""

    def test_describe_failure_returns_empty_and_warns(self, caplog):
        """Lookup is best-effort: a failed DescribeDomain yields "" (so the
        caller skips the policy delete) and a warning, never an exception."""
        mock_sm = MagicMock()
        mock_sm.describe_domain.side_effect = _client_error(
            "ResourceNotFound", "Domain d-test123 not found", "DescribeDomain"
        )

        with (
            caplog.at_level(logging.WARNING),
            patch("boto3.client", return_value=mock_sm),
        ):
            efs_id = _get_sagemaker_home_efs_id("us-east-2", "d-test123")

        assert efs_id == ""
        assert "Failed to get HomeEfsFileSystemId for d-test123" in caplog.text


# ---------------------------------------------------------------------------
# _delete_efs_resource_policy tests
# ---------------------------------------------------------------------------

_delete_efs_resource_policy = _module._delete_efs_resource_policy


class TestDeleteEfsResourcePolicy:
    def test_deletes_policy(self):
        mock_efs = MagicMock()

        with patch("boto3.client", return_value=mock_efs):
            result = _delete_efs_resource_policy("us-east-2", "fs-abc123")

        assert result is None
        mock_efs.delete_file_system_policy.assert_called_once_with(FileSystemId="fs-abc123")

    def test_missing_policy_is_ignored(self, caplog):
        """``PolicyNotFound`` means there was nothing to remove — that is
        the desired end state, so nothing is logged."""
        mock_efs = MagicMock()
        mock_efs.delete_file_system_policy.side_effect = _client_error(
            "PolicyNotFound", "No policy on fs-abc123", "DeleteFileSystemPolicy"
        )

        with (
            caplog.at_level(logging.WARNING),
            patch("boto3.client", return_value=mock_efs),
        ):
            _delete_efs_resource_policy("us-east-2", "fs-abc123")

        assert "Failed to delete EFS resource policy" not in caplog.text

    def test_other_failure_is_logged_not_raised(self, caplog):
        mock_efs = MagicMock()
        mock_efs.delete_file_system_policy.side_effect = _client_error(
            "AccessDeniedException", "denied", "DeleteFileSystemPolicy"
        )

        with (
            caplog.at_level(logging.WARNING),
            patch("boto3.client", return_value=mock_efs),
        ):
            _delete_efs_resource_policy("us-east-2", "fs-abc123")

        assert "Failed to delete EFS resource policy on fs-abc123" in caplog.text
        assert "AccessDeniedException" in caplog.text
