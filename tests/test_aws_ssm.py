"""Tests for ``gco/services/aws_ssm.py``.

The SSM helpers are the single source of truth for every callsite
in the tree (``cli/models.py``, ``cli/storage.py``, ``cli/vector_store.py``,
``cli/analytics_user_mgmt.py``, ``gco/services/inference_monitor.py``,
``gco_mcp/mission/state.py``). This file pins their contract so a
regression in any of those callsites fails here first.

Three concerns under test:

* **Happy path** for each function — ``moto``-backed SSM in the same
  ``us-east-2`` region the rest of the analytics test suite uses, so
  the helper integrates with a real boto3 client.
* **Missing-parameter handling** — ``get_ssm_parameter`` raises
  verbatim, ``get_ssm_parameter_optional`` returns ``None``,
  ``check_ssm_parameter`` returns ``(False, str(exc))``.
* **Other-error handling** — a non-``ParameterNotFound`` ``ClientError``
  propagates from ``get_ssm_parameter_optional`` (it does *not* swallow
  permission-denied or throttle errors), and surfaces flattened from
  ``check_ssm_parameter``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from gco.services.aws_ssm import (
    check_ssm_parameter,
    get_ssm_parameter,
    get_ssm_parameter_optional,
)

# ---------------------------------------------------------------------------
# get_ssm_parameter
# ---------------------------------------------------------------------------


class TestGetSsmParameter:
    @mock_aws
    def test_returns_value_for_existing_parameter(self) -> None:
        ssm = boto3.client("ssm", region_name="us-east-2")
        ssm.put_parameter(Name="/gco/foo", Value="bar", Type="String")
        assert get_ssm_parameter("/gco/foo", region="us-east-2") == "bar"

    @mock_aws
    def test_propagates_parameter_not_found(self) -> None:
        with pytest.raises(ClientError) as excinfo:
            get_ssm_parameter("/gco/nope", region="us-east-2")
        assert excinfo.value.response["Error"]["Code"] == "ParameterNotFound"

    def test_propagates_arbitrary_error(self) -> None:
        # Force an unexpected error code so the test pins the
        # "errors propagate verbatim" contract regardless of which
        # specific code came back. ``AccessDeniedException`` is the
        # most common real-world non-not-found case.
        fake_client = MagicMock()
        fake_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "GetParameter",
        )
        with patch("boto3.client", return_value=fake_client):
            with pytest.raises(ClientError) as excinfo:
                get_ssm_parameter("/gco/restricted", region="us-east-2")
            assert excinfo.value.response["Error"]["Code"] == "AccessDeniedException"

    @mock_aws
    def test_region_omitted_uses_boto3_default_chain(self) -> None:
        # When ``region`` is ``None`` the helper uses ``boto3.client("ssm")``
        # which falls back to the default region resolution chain.
        # ``moto`` honours ``AWS_DEFAULT_REGION`` for that path so the
        # test patches it explicitly to keep the test hermetic.
        with patch.dict("os.environ", {"AWS_DEFAULT_REGION": "us-east-2"}):
            ssm = boto3.client("ssm", region_name="us-east-2")
            ssm.put_parameter(Name="/gco/default", Value="ok", Type="String")
            assert get_ssm_parameter("/gco/default") == "ok"


# ---------------------------------------------------------------------------
# get_ssm_parameter_optional
# ---------------------------------------------------------------------------


class TestGetSsmParameterOptional:
    @mock_aws
    def test_returns_value_for_existing_parameter(self) -> None:
        ssm = boto3.client("ssm", region_name="us-east-2")
        ssm.put_parameter(Name="/gco/foo", Value="bar", Type="String")
        assert get_ssm_parameter_optional("/gco/foo", region="us-east-2") == "bar"

    @mock_aws
    def test_returns_none_for_missing_parameter(self) -> None:
        assert get_ssm_parameter_optional("/gco/nope", region="us-east-2") is None

    def test_propagates_non_not_found_errors(self) -> None:
        # The "absent vs unreachable" distinction is the entire point
        # of this helper — a permission error must NOT degrade to None.
        fake_client = MagicMock()
        fake_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "GetParameter",
        )
        with patch("boto3.client", return_value=fake_client), pytest.raises(ClientError):
            get_ssm_parameter_optional("/gco/restricted", region="us-east-2")


# ---------------------------------------------------------------------------
# check_ssm_parameter
# ---------------------------------------------------------------------------


class TestCheckSsmParameter:
    @mock_aws
    def test_returns_true_for_existing_parameter(self) -> None:
        ssm = boto3.client("ssm", region_name="us-east-2")
        ssm.put_parameter(Name="/gco/foo", Value="bar", Type="String")
        ok, msg = check_ssm_parameter("/gco/foo", region="us-east-2")
        assert ok is True
        assert msg == ""

    @mock_aws
    def test_returns_false_for_missing_parameter(self) -> None:
        ok, msg = check_ssm_parameter("/gco/nope", region="us-east-2")
        assert ok is False
        # The error string carries the underlying boto3 message
        # so a caller logging the result has a useful breadcrumb.
        assert "ParameterNotFound" in msg

    def test_returns_false_with_message_on_arbitrary_error(self) -> None:
        # Diagnostic-style helper flattens every kind of failure to
        # ``(False, str(exc))`` — including non-not-found errors.
        fake_client = MagicMock()
        fake_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "GetParameter",
        )
        with patch("boto3.client", return_value=fake_client):
            ok, msg = check_ssm_parameter("/gco/foo", region="us-east-2")
            assert ok is False
            assert "ThrottlingException" in msg or "slow down" in msg


# ---------------------------------------------------------------------------
# Compatibility alias on cli/analytics_user_mgmt
# ---------------------------------------------------------------------------


class TestCheckSsmParameterAliasContract:
    """``cli.analytics_user_mgmt.check_ssm_parameter`` is now a thin alias.

    Pin the alias contract so callers that imported
    ``aum.check_ssm_parameter("us-east-2", "/foo")`` keep working —
    positional ``(region, name)`` order, ``(bool, str)`` return shape.
    A future delegation refactor that flips the argument order would
    silently break callers; this test fails that scenario fast.
    """

    @mock_aws
    def test_positional_region_first_then_name_still_works(self) -> None:
        from cli import analytics_user_mgmt as aum

        ssm = boto3.client("ssm", region_name="us-east-2")
        ssm.put_parameter(Name="/gco/alias", Value="ok", Type="String")
        ok, msg = aum.check_ssm_parameter("us-east-2", "/gco/alias")
        assert ok is True
        assert msg == ""

    @mock_aws
    def test_alias_returns_false_for_missing(self) -> None:
        from cli import analytics_user_mgmt as aum

        ok, msg = aum.check_ssm_parameter("us-east-2", "/gco/nope")
        assert ok is False
        assert "ParameterNotFound" in msg
