"""Tests for the analytics pieces of the live release validation harness.

``checks/analytics.py``: the release override and the enablement with its
source, the ``gco`` runner (passwords only through the environment, the
record redacted), the deployment read from the stack, and every probe step
with its failure branches. ``actions/analytics.py``: validated, or skipped
with the source. ``cleanup/analytics.py``: the run's Studio lifecycle
configurations deleted once the stacks are gone, and the Studio domain's home
EFS proved gone. ``models.RunSettings``: the override in the CDK context.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError

from scripts.live_release_validation.actions import analytics as actions_analytics
from scripts.live_release_validation.checks import analytics as checks_analytics
from scripts.live_release_validation.cleanup import analytics as cleanup_analytics
from scripts.live_release_validation.models import RunSettings

_REGION = "us-east-2"
_ACCOUNT = "123456789012"
_PROJECT = "gco-live"
_DOMAIN = "d-abc123"
_POOL = "us-east-2_AbCdEf"
_EFS = "fs-0studio"
_HOME_EFS = "fs-0home"
_BUCKET = "gco-live-studio-only-bucket"
_ROLE = f"arn:aws:iam::{_ACCOUNT}:role/gco-live-studio-execution"
_DOMAIN_ARN = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:domain/{_DOMAIN}"
_PROFILE_ARN = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:user-profile/{_DOMAIN}/first"
_LCC_ARN = f"arn:aws:sagemaker:{_REGION}:{_ACCOUNT}:studio-lifecycle-config/probe"
_SUB = "11111111-2222-3333-4444-555555555555"
_OTHER_SUB = "66666666-7777-8888-9999-000000000000"
_OVERRIDES = checks_analytics.analytics_overrides_json()
_HOST = f"studio-{_DOMAIN}.studio.{_REGION}.sagemaker.aws"
_URL = f"https://{_HOST}/auth?token=presigned-token"
_ValidationError = checks_analytics.AnalyticsValidationError


def _ctx(
    *,
    overrides: str = _OVERRIDES,
    cdk_context: dict[str, Any] | None = None,
    regions: tuple[str, ...] = (_REGION,),
) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            run_id="run-123",
            analytics_overrides_json=overrides,
            repo_root=Path("/checkout"),
            command_timeout_seconds=300,
            expected_account=_ACCOUNT,
        ),
        cdk_context=cdk_context if cdk_context is not None else {},
        deployment_regions=regions,
        config=SimpleNamespace(project_name=_PROJECT, api_gateway_region=_REGION),
        session=MagicMock(),
        checkpoint=SimpleNamespace(state={}),
        state_lock=threading.RLock(),
        persist=MagicMock(),
    )


def _deployment(**overrides: str) -> dict[str, str]:
    deployment = {
        "region": _REGION,
        "stack_name": f"{_PROJECT}-analytics",
        "domain_id": _DOMAIN,
        "pool_id": _POOL,
        "efs_id": _EFS,
        "bucket": _BUCKET,
        "execution_role": _ROLE,
        "domain_arn": _DOMAIN_ARN,
        "home_efs_id": _HOME_EFS,
    }
    deployment.update(overrides)
    return deployment


def _client_error(code: str | None, operation: str = "Operation") -> ClientError:
    error = {"Code": code, "Message": "message"} if code else {}
    return ClientError({"Error": error}, operation)  # type: ignore[arg-type]


def _done(returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return subprocess.CompletedProcess(["gco"], returncode, stdout=stdout, stderr=stderr)


def _json(document: Any) -> Any:
    return _done(0, stdout=json.dumps(document))


class _Gco:
    """Stands in for the module's ``subprocess``: answers each ``gco`` call in turn."""

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def run(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append({"argv": argv, "args": argv[3:], **kwargs})
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _Clock:
    """Stands in for the module's ``time``: every sleep advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _patched(gco: _Gco | None = None, *passwords: str) -> Any:
    """Patch ``subprocess``, ``time`` and the password generator of the module."""
    stack = patch.multiple(
        checks_analytics,
        subprocess=gco or _Gco(),
        time=_Clock(),
        generate_strong_password=MagicMock(side_effect=list(passwords) or None),
    )
    return stack


def _cognito(*subs: str, missing_after: bool = False) -> MagicMock:
    """A Cognito client whose ``AdminGetUser`` answers each ``sub`` in turn."""
    cognito = MagicMock()
    answers: list[Any] = [{"UserAttributes": [{"Name": "sub", "Value": sub}]} for sub in subs]
    if missing_after:
        answers.append(_client_error("UserNotFoundException"))
    cognito.admin_get_user.side_effect = answers
    return cognito


def _profile(
    username: str = "first", *, status: str = "InService", path: str | None = None
) -> dict[str, Any]:
    return {
        "UserProfileArn": _PROFILE_ARN,
        "Status": status,
        "UserSettings": {
            "CustomFileSystemConfigs": [
                {
                    "EFSFileSystemConfig": {
                        "FileSystemId": _EFS,
                        "FileSystemPath": path or f"/home/{username}",
                    }
                }
            ]
        },
    }


def _sagemaker(*profiles: Any, tags: dict[str, str] | None = None) -> MagicMock:
    """A SageMaker client whose ``DescribeUserProfile`` answers each profile in turn."""
    sagemaker = MagicMock()
    sagemaker.describe_user_profile.side_effect = [
        profile if profile is not None else _client_error("ResourceNotFound")
        for profile in profiles
    ]
    owner = {checks_analytics.PROFILE_OWNER_TAG: _SUB} if tags is None else tags
    sagemaker.list_tags.return_value = {"Tags": [{"Key": k, "Value": v} for k, v in owner.items()]}
    return sagemaker


# ---------------------------------------------------------------------------
# The run's override and the enablement
# ---------------------------------------------------------------------------


class TestOverridesAndEnablement:
    def test_the_release_override_is_canonical_json(self) -> None:
        assert _OVERRIDES == '{"enabled":true}'
        assert json.loads(_OVERRIDES) == checks_analytics.RELEASE_ANALYTICS_OVERRIDES

    def test_the_run_override_turns_the_environment_on_and_is_named(self) -> None:
        assert checks_analytics.effective_analytics_enablement(_ctx()) == {
            "enabled": True,
            "source": "run-override (analytics_environment_overrides)",
            "region": _REGION,
        }

    def test_without_an_override_cdk_json_decides(self) -> None:
        on = _ctx(overrides="", cdk_context={"analytics_environment": {"enabled": True}})
        off = _ctx(overrides="")
        assert checks_analytics.effective_analytics_enablement(on)["enabled"] is True
        assert checks_analytics.effective_analytics_enablement(off) == {
            "enabled": False,
            "source": "cdk.json analytics_environment.enabled",
            "region": _REGION,
        }

    def test_an_override_that_leaves_enabled_alone_keeps_cdk_json_the_source(self) -> None:
        ctx = _ctx(
            overrides='{"efs":{"removal_policy":"retain"}}',
            cdk_context={"analytics_environment": {"enabled": True}},
        )
        assert checks_analytics.effective_analytics_enablement(ctx) == {
            "enabled": True,
            "source": "cdk.json analytics_environment.enabled",
            "region": _REGION,
        }

    def test_a_malformed_block_takes_no_override_as_the_loader_reads_it(self) -> None:
        ctx = _ctx(cdk_context={"analytics_environment": "on"})
        assert checks_analytics.effective_analytics_enablement(ctx)["enabled"] is False

    def test_the_override_rides_every_cdk_invocation_and_the_identity(self, tmp_path: Path) -> None:
        base: dict[str, Any] = {
            "run_id": "run-123",
            "repo_root": tmp_path,
            "report_dir": tmp_path / "report",
            "checkpoint_path": tmp_path / "report" / "checkpoint.json",
            "expected_account": _ACCOUNT,
            "expected_sha": "a" * 40,
            "expected_branch": "chore/test",
            "profile": "configured",
            "requested_actions": ("analytics",),
        }
        plain = RunSettings(**base)
        assert "analytics_environment_overrides" not in plain.extra_cdk_context()
        deployed = RunSettings(**base, analytics_overrides_json=_OVERRIDES)
        assert deployed.extra_cdk_context()["analytics_environment_overrides"] == _OVERRIDES
        assert deployed.identity()["extra_cdk_context"] == deployed.extra_cdk_context()
        assert deployed.identity() != plain.identity()


# ---------------------------------------------------------------------------
# Running gco
# ---------------------------------------------------------------------------


class TestRunGco:
    def test_passwords_reach_the_cli_through_its_environment_and_never_the_record(self) -> None:
        ctx = _ctx()
        commands: list[dict[str, Any]] = []
        stdout = json.dumps({"username": "u", "password": "Temporary-1!"}) + (
            f"\n{_URL}&state=1 Chosen-1!"
        )
        gco = _Gco(_done(0, stdout=stdout, stderr="Chosen-1! was refused"))
        operator = {
            "GCO_STUDIO_PASSWORD": "operator-password",
            "GCO_STUDIO_NEW_PASSWORD": "operator-new-password",
            "GCO_LIVE_TEST_KEEP": "kept",
        }
        with patch.dict(os.environ, operator), _patched(gco):
            result = checks_analytics._run_gco(
                ctx,
                commands,
                "first-login",
                ["analytics", "studio", "login"],
                secrets={"GCO_STUDIO_PASSWORD": "Chosen-1!", "GCO_STUDIO_NEW_PASSWORD": ""},
            )
        assert result.returncode == 0
        invocation = gco.calls[0]
        assert invocation["argv"] == [
            sys.executable,
            "-m",
            "cli.main",
            "analytics",
            "studio",
            "login",
        ]
        assert invocation["cwd"] == Path("/checkout")
        assert invocation["timeout"] == 300.0
        assert invocation["shell"] is False
        assert invocation["check"] is False
        assert invocation["capture_output"] is True
        environment = invocation["env"]
        assert environment["GCO_STUDIO_PASSWORD"] == "Chosen-1!"
        assert environment["GCO_STUDIO_NEW_PASSWORD"] == ""
        assert environment["GCO_LIVE_TEST_KEEP"] == "kept"
        record = commands[0]
        assert record["stage"] == "first-login"
        assert record["returncode"] == 0
        assert "Chosen-1!" not in json.dumps(record)
        assert "Temporary-1!" not in record["stdout"]
        assert "presigned-token" not in record["stdout"]
        assert '"password": "<redacted>"' in record["stdout"]
        assert "token=<redacted>&state=1 <redacted>" in record["stdout"]
        assert record["stderr"] == "<redacted> was refused"

    def test_the_operators_password_variables_never_reach_a_probe(self) -> None:
        gco = _Gco(_done(0))
        operator = {"GCO_STUDIO_PASSWORD": "operator", "GCO_STUDIO_NEW_PASSWORD": "operator-new"}
        with patch.dict(os.environ, operator), _patched(gco):
            checks_analytics._run_gco(_ctx(), [], "doctor", ["analytics", "doctor"])
        environment = gco.calls[0]["env"]
        assert "GCO_STUDIO_PASSWORD" not in environment
        assert "GCO_STUDIO_NEW_PASSWORD" not in environment

    def test_the_record_keeps_the_tail_of_long_output(self) -> None:
        commands: list[dict[str, Any]] = []
        gco = _Gco(_done(1, stdout="a" * 5000 + "end", stderr="b" * 5000))
        with _patched(gco):
            checks_analytics._run_gco(_ctx(), commands, "doctor", ["analytics", "doctor"])
        assert len(commands[0]["stdout"]) == 4000
        assert commands[0]["stdout"].endswith("end")
        assert len(commands[0]["stderr"]) == 4000

    def test_a_timeout_is_recorded_and_raised(self) -> None:
        commands: list[dict[str, Any]] = []
        gco = _Gco(subprocess.TimeoutExpired(cmd="gco", timeout=300))
        with (
            _patched(gco),
            pytest.raises(_ValidationError, match="doctor: gco timed out after 300s"),
        ):
            checks_analytics._run_gco(_ctx(), commands, "doctor", ["analytics", "doctor"])
        assert commands == [
            {
                "stage": "doctor",
                "argv": [sys.executable, "-m", "cli.main", "analytics", "doctor"],
                "timed_out": True,
            }
        ]


class TestSmallHelpers:
    def test_require_exit_names_the_stage_and_the_stderr_tail(self) -> None:
        checks_analytics._require_exit(_done(2), "login", 2)
        with pytest.raises(_ValidationError, match=r"add: gco exited 1, expected 0: boom$"):
            checks_analytics._require_exit(_done(1, stderr="  boom \n"), "add", 0)

    def test_checked_returns_a_successful_result(self) -> None:
        result = _done(0)
        assert checks_analytics._checked(result, "add") is result
        with pytest.raises(_ValidationError, match="add: gco exited 3"):
            checks_analytics._checked(_done(3), "add")

    @pytest.mark.parametrize(
        ("stdout", "match"),
        [("not json", "add: gco printed no JSON document"), ("[1]", "add: gco printed list")],
    )
    def test_json_output_requires_one_object(self, stdout: str, match: str) -> None:
        with pytest.raises(_ValidationError, match=match):
            checks_analytics._json_output(_done(0, stdout=stdout), "add")

    def test_json_output_returns_the_object(self) -> None:
        assert checks_analytics._json_output(_json({"a": 1}), "add") == {"a": 1}

    def test_wait_for_polls_until_done(self) -> None:
        statuses = iter(["Pending", None, "InService"])
        clock = _Clock()
        with patch.object(checks_analytics, "time", clock):
            status = checks_analytics._wait_for(
                lambda: next(statuses),
                done={"InService"},
                failed={"Failed"},
                what="Studio space s",
                timeout=600,
            )
        assert status == "InService"
        assert clock.sleeps == [15, 15]

    def test_wait_for_raises_on_a_failed_status(self) -> None:
        with (
            patch.object(checks_analytics, "time", _Clock()),
            pytest.raises(_ValidationError, match=r"^Studio space s is Failed$"),
        ):
            checks_analytics._wait_for(
                lambda: "Failed",
                done={"InService"},
                failed={"Failed"},
                what="Studio space s",
                timeout=600,
            )

    def test_wait_for_gives_up_at_the_deadline(self) -> None:
        clock = _Clock()
        with (
            patch.object(checks_analytics, "time", clock),
            pytest.raises(_ValidationError, match="Studio space s is still Pending after 30s"),
        ):
            checks_analytics._wait_for(
                lambda: "Pending",
                done={"InService"},
                failed={"Failed"},
                what="Studio space s",
                timeout=30,
            )
        assert clock.sleeps == [15, 15]

    @pytest.mark.parametrize(
        ("code", "missing"),
        [
            ("ResourceNotFound", True),
            ("ValidationException", True),
            ("UserNotFoundException", True),
            ("AccessDeniedException", False),
            (None, False),
        ],
    )
    def test_missing_names_the_absent_codes(self, code: str | None, missing: bool) -> None:
        assert checks_analytics._missing(_client_error(code)) is missing

    def test_names_are_run_scoped_under_the_project_prefix(self) -> None:
        prefix = f"{_PROJECT}-lv{hashlib.sha256(b'run-123').hexdigest()[:8]}2"
        assert checks_analytics._names(_ctx(), 2) == {
            "first": f"{prefix}-first",
            "second": f"{prefix}-second",
            "refused": f"{prefix}_refused",
            "unsupported": f"{prefix}_unsupported",
            "home_space": f"{prefix}-home",
            "second_space": f"{prefix}-second-space",
            "lifecycle_config": f"{prefix}-home-probe",
            "report_key": f"gco-live-validation/{prefix}/home-probe.json",
        }

    def test_the_cognito_sub_is_read_and_required(self) -> None:
        cognito = MagicMock()
        cognito.admin_get_user.side_effect = [
            {"UserAttributes": [{"Name": "email", "Value": "x"}, {"Name": "sub", "Value": _SUB}]},
            {},
        ]
        assert checks_analytics._cognito_sub(cognito, _POOL, "u") == _SUB
        with pytest.raises(_ValidationError, match="Cognito user u has no sub"):
            checks_analytics._cognito_sub(cognito, _POOL, "u")
        cognito.admin_get_user.assert_called_with(UserPoolId=_POOL, Username="u")

    def test_run_tags_name_the_project_and_the_run(self) -> None:
        assert checks_analytics._run_tags(_ctx()) == [
            {"Key": "gco:project", "Value": _PROJECT},
            {"Key": "gco:validation-run", "Value": "run-123"},
        ]

    def test_lifecycle_configs_are_recorded_once_per_region_and_name(self) -> None:
        ctx = _ctx()
        checks_analytics._record_lifecycle_config(ctx, _REGION, "probe")
        checks_analytics._record_lifecycle_config(ctx, _REGION, "probe")
        checks_analytics._record_lifecycle_config(ctx, "us-west-2", "probe")
        records = ctx.checkpoint.state[checks_analytics.LIFECYCLE_CONFIGS_STATE]
        assert [(r["region"], r["name"]) for r in records] == [
            (_REGION, "probe"),
            ("us-west-2", "probe"),
        ]
        assert all(r["recorded_at"] for r in records)
        assert ctx.persist.call_count == 3


# ---------------------------------------------------------------------------
# The deployment
# ---------------------------------------------------------------------------


def _deployment_clients(
    ctx: SimpleNamespace,
    *,
    outputs: dict[str, str] | None = None,
    resources: list[dict[str, str]] | None = None,
    domain: dict[str, Any] | None = None,
) -> dict[str, MagicMock]:
    cloudformation = MagicMock()
    if outputs is None:
        outputs = {"StudioDomainId": _DOMAIN, "CognitoUserPoolId": _POOL, "Other": "x"}
    cloudformation.describe_stacks.return_value = {
        "Stacks": [
            {"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()]},
            {},
        ]
    }
    if resources is None:
        resources = [
            {
                "LogicalResourceId": "StudioEfs1A2B3C4D",
                "ResourceType": "AWS::EFS::FileSystem",
                "PhysicalResourceId": _EFS,
            },
            {
                "LogicalResourceId": "StudioEfsMountTarget0",
                "ResourceType": "AWS::EFS::MountTarget",
                "PhysicalResourceId": "fsmt-0",
            },
            {
                "LogicalResourceId": "StudioOnlyBucket9F8E7D6C",
                "ResourceType": "AWS::S3::Bucket",
                "PhysicalResourceId": _BUCKET,
            },
            {
                "LogicalResourceId": "StudioDomain",
                "ResourceType": "AWS::SageMaker::Domain",
                "PhysicalResourceId": _DOMAIN_ARN,
            },
        ]
    cloudformation.get_paginator.return_value.paginate.return_value = [
        {"StackResourceSummaries": resources},
        {},
    ]
    sagemaker = MagicMock()
    sagemaker.describe_domain.return_value = (
        domain
        if domain is not None
        else {
            "DefaultUserSettings": {"ExecutionRole": _ROLE},
            "DomainArn": _DOMAIN_ARN,
            "HomeEfsFileSystemId": _HOME_EFS,
        }
    )
    clients = {"cloudformation": cloudformation, "sagemaker": sagemaker}
    ctx.session.client.side_effect = lambda service, **_kwargs: clients[service]
    return clients


class TestDeployment:
    def test_the_stack_names_its_domain_pool_efs_bucket_and_role(self) -> None:
        ctx = _ctx()
        clients = _deployment_clients(ctx)
        assert checks_analytics._deployment(ctx) == _deployment()
        clients["cloudformation"].describe_stacks.assert_called_once_with(
            StackName=f"{_PROJECT}-analytics"
        )
        clients["sagemaker"].describe_domain.assert_called_once_with(DomainId=_DOMAIN)
        ctx.session.client.assert_any_call("cloudformation", region_name=_REGION)

    def test_a_stack_without_its_outputs_and_resources_is_named(self) -> None:
        ctx = _ctx()
        _deployment_clients(ctx, outputs={}, resources=[])
        with pytest.raises(
            _ValidationError,
            match=(
                f"{_PROJECT}-analytics in {_REGION} does not name its "
                "bucket, domain_id, efs_id, pool_id"
            ),
        ):
            checks_analytics._deployment(ctx)

    def test_a_domain_without_an_execution_role_is_an_error(self) -> None:
        ctx = _ctx()
        _deployment_clients(ctx, domain={})
        with pytest.raises(_ValidationError, match=f"Studio domain {_DOMAIN} has no role"):
            checks_analytics._deployment(ctx)


# ---------------------------------------------------------------------------
# doctor and the username rule
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_a_passing_doctor_is_counted(self) -> None:
        document = {
            "passed": True,
            "checks": [{"name": "a", "ok": True}, {"name": "b", "ok": True}],
        }
        gco = _Gco(_json(document))
        commands: list[dict[str, Any]] = []
        with _patched(gco):
            assert checks_analytics._check_doctor(_ctx(), commands) == {"passed": True, "checks": 2}
        assert gco.calls[0]["args"] == ["--output", "json", "analytics", "doctor"]
        assert commands[0]["stage"] == "doctor"

    @pytest.mark.parametrize(
        ("document", "failed"),
        [
            ({"passed": False, "checks": []}, r"\[\]"),
            ({"passed": True, "checks": [{"name": "efs", "ok": False}]}, r"\['efs'\]"),
        ],
    )
    def test_a_failing_doctor_names_its_checks(self, document: dict[str, Any], failed: str) -> None:
        with (
            _patched(_Gco(_json(document))),
            pytest.raises(_ValidationError, match=f"gco analytics doctor failed: {failed}"),
        ):
            checks_analytics._check_doctor(_ctx(), [])

    def test_a_doctor_that_exits_nonzero_is_an_error(self) -> None:
        with (
            _patched(_Gco(_done(1, stderr="checks failed"))),
            pytest.raises(_ValidationError, match="doctor: gco exited 1, expected 0"),
        ):
            checks_analytics._check_doctor(_ctx(), [])


class TestUsernameRule:
    def _check(self, gco: _Gco, cognito: MagicMock) -> dict[str, Any]:
        with _patched(gco):
            return checks_analytics._check_username_rule(
                _ctx(), [], cognito, _deployment(), "gco-live_refused"
            )

    def test_users_add_refuses_the_name_before_cognito_sees_it(self) -> None:
        cognito = MagicMock()
        cognito.admin_get_user.side_effect = _client_error("UserNotFoundException")
        gco = _Gco(_done(1, stderr="Cannot create user 'gco-live_refused': rule"))
        assert self._check(gco, cognito) == {"username": "gco-live_refused", "refused": True}
        assert gco.calls[0]["args"] == [
            "--output",
            "json",
            "analytics",
            "users",
            "add",
            "--username",
            "gco-live_refused",
            "--no-email",
        ]
        cognito.admin_get_user.assert_called_once_with(
            UserPoolId=_POOL, Username="gco-live_refused"
        )

    def test_an_accepted_name_is_an_error(self) -> None:
        with pytest.raises(_ValidationError, match="username-rule: gco exited 0, expected 1"):
            self._check(_Gco(_done(0)), MagicMock())

    def test_a_refusal_without_the_profile_name_rule_is_an_error(self) -> None:
        with pytest.raises(_ValidationError, match="without the profile-name rule"):
            self._check(_Gco(_done(1, stderr="Cognito is down")), MagicMock())

    def test_a_name_cognito_has_after_the_refusal_is_an_error(self) -> None:
        cognito = MagicMock()
        cognito.admin_get_user.return_value = {"Username": "gco-live_refused"}
        with pytest.raises(_ValidationError, match="refused gco-live_refused but Cognito has it"):
            self._check(_Gco(_done(1, stderr="Cannot create user")), cognito)

    def test_an_unexpected_cognito_error_propagates(self) -> None:
        cognito = MagicMock()
        cognito.admin_get_user.side_effect = _client_error("AccessDeniedException")
        with pytest.raises(ClientError):
            self._check(_Gco(_done(1, stderr="Cannot create user")), cognito)


# ---------------------------------------------------------------------------
# studio login, the URL and the refusals
# ---------------------------------------------------------------------------


class TestLogin:
    def test_a_login_is_repeated_while_the_new_profile_is_not_ready(self) -> None:
        not_ready = _done(2, stderr="User profile did not become ready within 120s.")
        gco = _Gco(not_ready, _done(0, stdout=_URL))
        with _patched(gco):
            result = checks_analytics._login(_ctx(), [], "first-login", "u", {"A": "b"})
        assert result.stdout == _URL
        assert [c["args"] for c in gco.calls] == [
            ["analytics", "studio", "login", "--username", "u"]
        ] * 2
        assert gco.calls[0]["env"]["A"] == "b"

    def test_a_profile_that_never_becomes_ready_returns_the_last_answer(self) -> None:
        not_ready = _done(2, stderr="did not become ready")
        gco = _Gco(not_ready, not_ready, not_ready)
        with _patched(gco):
            result = checks_analytics._login(_ctx(), [], "first-login", "u", {})
        assert result is not_ready
        assert len(gco.calls) == 3

    def test_another_refusal_is_returned_at_once(self) -> None:
        refused = _done(2, stderr="login failed: HTTP 403 UserProfileOwnedByAnotherUser")
        gco = _Gco(refused)
        with _patched(gco):
            assert checks_analytics._login(_ctx(), [], "foreign-login", "u", {}) is refused
        assert len(gco.calls) == 1

    @pytest.mark.parametrize("host", [_HOST, f"{_DOMAIN}.studio.{_REGION}.sagemaker.aws"])
    def test_the_url_is_a_presigned_studio_url_for_the_domain(self, host: str) -> None:
        result = _done(0, stdout=f"  Waiting...\n\nhttps://{host}/auth?token=t\n\n")
        assert checks_analytics._studio_url(result, "login", _deployment()) == {
            "host": host,
            "path": "/auth",
        }

    @pytest.mark.parametrize(
        "stdout",
        [
            "",
            f"http://{_HOST}/auth?token=t",
            f"https://studio-d-other.studio.{_REGION}.sagemaker.aws/auth?token=t",
            f"https://{_HOST}/auth?state=t",
        ],
    )
    def test_anything_else_is_no_presigned_url(self, stdout: str) -> None:
        with pytest.raises(_ValidationError, match="login: studio login printed no presigned URL"):
            checks_analytics._studio_url(_done(0, stdout=stdout), "login", _deployment())

    def test_a_failed_login_prints_no_url(self) -> None:
        with pytest.raises(_ValidationError, match="login: gco exited 2, expected 0"):
            checks_analytics._studio_url(_done(2), "login", _deployment())

    def test_a_refusal_needs_exit_2_and_the_http_status(self) -> None:
        refused = _done(2, stderr="Error: login failed: HTTP 400 UnsupportedUsername, id=1")
        assert checks_analytics._require_refusal(refused, "x", "400 UnsupportedUsername") == {
            "refused": "HTTP 400 UnsupportedUsername"
        }
        with pytest.raises(_ValidationError, match="x: gco exited 0, expected 2"):
            checks_analytics._require_refusal(_done(0), "x", "400 UnsupportedUsername")
        other = _done(2, stderr="login failed: HTTP 500")
        with pytest.raises(_ValidationError, match=r"not refused with HTTP 400 .*HTTP 500"):
            checks_analytics._require_refusal(other, "x", "400 UnsupportedUsername")


# ---------------------------------------------------------------------------
# Profiles, spaces and apps
# ---------------------------------------------------------------------------


class TestDescribeHelpers:
    def test_profile_space_and_app_read_none_when_absent(self) -> None:
        sagemaker = MagicMock()
        sagemaker.describe_user_profile.side_effect = [
            {"Status": "InService"},
            _client_error("ResourceNotFound"),
        ]
        sagemaker.describe_space.side_effect = [
            {"Status": "Pending"},
            _client_error("ResourceNotFound"),
        ]
        sagemaker.describe_app.side_effect = [
            {"Status": "Pending"},
            _client_error("ResourceNotFound"),
        ]
        assert checks_analytics._profile(sagemaker, _DOMAIN, "u") == {"Status": "InService"}
        assert checks_analytics._profile(sagemaker, _DOMAIN, "u") is None
        assert checks_analytics._space_status(sagemaker, _DOMAIN, "s") == "Pending"
        assert checks_analytics._space_status(sagemaker, _DOMAIN, "s") is None
        assert checks_analytics._app(sagemaker, _DOMAIN, "s") == {"Status": "Pending"}
        assert checks_analytics._app(sagemaker, _DOMAIN, "s") is None
        sagemaker.describe_app.assert_called_with(
            DomainId=_DOMAIN, SpaceName="s", AppType="JupyterLab", AppName="default"
        )

    @pytest.mark.parametrize(
        ("method", "reader"),
        [
            ("describe_user_profile", checks_analytics._profile),
            ("describe_space", checks_analytics._space_status),
            ("describe_app", checks_analytics._app),
        ],
    )
    def test_other_errors_propagate(self, method: str, reader: Any) -> None:
        sagemaker = MagicMock()
        getattr(sagemaker, method).side_effect = _client_error("ThrottlingException")
        with pytest.raises(ClientError):
            reader(sagemaker, _DOMAIN, "x")

    def test_profile_tags_follow_every_page(self) -> None:
        sagemaker = MagicMock()
        sagemaker.list_tags.side_effect = [
            {"Tags": [{"Key": "a", "Value": "1"}], "NextToken": "next"},
            {"Tags": [{"Key": "b", "Value": "2"}]},
        ]
        assert checks_analytics._profile_tags(sagemaker, _PROFILE_ARN) == {"a": "1", "b": "2"}
        assert sagemaker.list_tags.call_args_list == [
            call(ResourceArn=_PROFILE_ARN),
            call(ResourceArn=_PROFILE_ARN, NextToken="next"),
        ]


class TestCheckProfile:
    def test_the_lambdas_profile_is_in_service_owned_and_on_the_users_directory(self) -> None:
        sagemaker = _sagemaker(_profile("u"))
        assert checks_analytics._check_profile(sagemaker, _deployment(), "u", _SUB) == {
            "status": "InService",
            "owner_tag": True,
            "efs_path": "/home/u",
        }
        sagemaker.list_tags.assert_called_once_with(ResourceArn=_PROFILE_ARN)

    @pytest.mark.parametrize(
        ("profile", "tags", "match"),
        [
            (None, None, "Studio profile u is absent"),
            (_profile("u", status="Pending"), None, "Studio profile u is Pending"),
            (_profile("u"), {"other": "x"}, "Studio profile u is not tagged with its owner"),
            (_profile("u", path="/"), None, "has custom file systems .*'/'.*expected"),
        ],
    )
    def test_anything_else_is_an_error(
        self, profile: dict[str, Any] | None, tags: dict[str, str] | None, match: str
    ) -> None:
        with pytest.raises(_ValidationError, match=match):
            checks_analytics._check_profile(
                _sagemaker(profile, tags=tags), _deployment(), "u", _SUB
            )

    def test_a_profile_without_custom_file_systems_is_an_error(self) -> None:
        profile = {"UserProfileArn": _PROFILE_ARN, "Status": "InService"}
        with pytest.raises(_ValidationError, match=r"has custom file systems \[\]"):
            checks_analytics._check_profile(_sagemaker(profile), _deployment(), "u", _SUB)


# ---------------------------------------------------------------------------
# The first sign-in and the unsupported username
# ---------------------------------------------------------------------------


class TestFirstSignIn:
    @staticmethod
    def _created(**overrides: Any) -> Any:
        document = {
            "username": "first",
            "password": "Temporary-1!",
            "password_state": "temporary",
            "invitation_emailed": False,
        }
        document.update(overrides)
        return _json(document)

    def test_the_temporary_password_is_replaced_at_the_first_login(self) -> None:
        gco = _Gco(self._created(), _done(0, stdout=_URL), _done(0, stdout=_URL))
        sagemaker = _sagemaker(_profile("first"))
        cognito = _cognito(_SUB)
        with _patched(gco, "Chosen-1!"):
            evidence, sub = checks_analytics._check_first_sign_in(
                _ctx(), [], sagemaker, cognito, _deployment(), "first"
            )
        assert sub == _SUB
        assert evidence == {
            "username": "first",
            "temporary_credential_replaced": True,
            "url": {"host": _HOST, "path": "/auth"},
            "profile": {"status": "InService", "owner_tag": True, "efs_path": "/home/first"},
        }
        add, first, again = gco.calls
        assert add["args"][-3:] == ["--username", "first", "--no-email"]
        assert first["env"]["GCO_STUDIO_PASSWORD"] == "Temporary-1!"
        assert first["env"]["GCO_STUDIO_NEW_PASSWORD"] == "Chosen-1!"
        assert again["env"]["GCO_STUDIO_PASSWORD"] == "Chosen-1!"
        assert "GCO_STUDIO_NEW_PASSWORD" not in again["env"]

    @pytest.mark.parametrize(
        ("created", "match"),
        [
            ({"password_state": "permanent"}, "printed no temporary password"),
            ({"password": ""}, "printed no temporary password"),
            ({"invitation_emailed": True}, "reported an emailed invitation"),
        ],
    )
    def test_users_add_must_print_a_temporary_password_and_send_nothing(
        self, created: dict[str, Any], match: str
    ) -> None:
        with _patched(_Gco(self._created(**created))), pytest.raises(_ValidationError, match=match):
            checks_analytics._check_first_sign_in(
                _ctx(), [], MagicMock(), MagicMock(), _deployment(), "first"
            )


class TestUnsupportedUsername:
    def test_a_name_sagemaker_cannot_use_is_refused_by_the_lambda(self) -> None:
        cognito = MagicMock()
        refused = _done(2, stderr="login failed: HTTP 400 UnsupportedUsername, correlation_id=1")
        gco = _Gco(refused)
        with _patched(gco, "Permanent-1!", "Temporary-2!"):
            evidence = checks_analytics._check_unsupported_username(
                _ctx(), [], cognito, _deployment(), "x_unsupported"
            )
        assert evidence == {"username": "x_unsupported", "refused": "HTTP 400 UnsupportedUsername"}
        cognito.admin_create_user.assert_called_once_with(
            UserPoolId=_POOL,
            Username="x_unsupported",
            TemporaryPassword="Temporary-2!",
            MessageAction="SUPPRESS",
        )
        cognito.admin_set_user_password.assert_called_once_with(
            UserPoolId=_POOL, Username="x_unsupported", Password="Permanent-1!", Permanent=True
        )
        assert gco.calls[0]["env"]["GCO_STUDIO_PASSWORD"] == "Permanent-1!"
        cognito.admin_delete_user.assert_called_once_with(
            UserPoolId=_POOL, Username="x_unsupported"
        )

    def test_the_cognito_user_is_deleted_when_the_lambda_lets_it_in(self) -> None:
        cognito = MagicMock()
        with (
            _patched(_Gco(_done(0, stdout=_URL)), "Permanent-1!", "Temporary-2!"),
            pytest.raises(_ValidationError, match="unsupported-login: gco exited 0, expected 2"),
        ):
            checks_analytics._check_unsupported_username(
                _ctx(), [], cognito, _deployment(), "x_unsupported"
            )
        cognito.admin_delete_user.assert_called_once_with(
            UserPoolId=_POOL, Username="x_unsupported"
        )


# ---------------------------------------------------------------------------
# The Studio EFS home probe
# ---------------------------------------------------------------------------


class TestCreateSpace:
    def test_a_private_jupyterlab_space_with_the_studio_efs_waits_for_in_service(self) -> None:
        sagemaker = MagicMock()
        sagemaker.describe_space.side_effect = [{"Status": "Pending"}, {"Status": "InService"}]
        with _patched():
            checks_analytics._create_space(
                _ctx(),
                sagemaker,
                _deployment(),
                owner="u",
                space="s",
                lifecycle_config_arn=_LCC_ARN,
            )
        sagemaker.create_space.assert_called_once_with(
            DomainId=_DOMAIN,
            SpaceName="s",
            OwnershipSettings={"OwnerUserProfileName": "u"},
            SpaceSharingSettings={"SharingType": "Private"},
            SpaceSettings={
                "AppType": "JupyterLab",
                "CustomFileSystems": [{"EFSFileSystem": {"FileSystemId": _EFS}}],
                "JupyterLabAppSettings": {
                    "DefaultResourceSpec": {
                        "InstanceType": "ml.t3.medium",
                        "LifecycleConfigArn": _LCC_ARN,
                    }
                },
            },
            Tags=checks_analytics._run_tags(_ctx()),
        )

    def test_a_space_without_a_lifecycle_config_names_none(self) -> None:
        sagemaker = MagicMock()
        sagemaker.describe_space.return_value = {"Status": "InService"}
        with _patched():
            checks_analytics._create_space(_ctx(), sagemaker, _deployment(), owner="u", space="s")
        settings = sagemaker.create_space.call_args.kwargs["SpaceSettings"]
        assert settings["JupyterLabAppSettings"] == {
            "DefaultResourceSpec": {"InstanceType": "ml.t3.medium"}
        }

    def test_a_failed_space_is_an_error(self) -> None:
        sagemaker = MagicMock()
        sagemaker.describe_space.return_value = {"Status": "Failed"}
        with _patched(), pytest.raises(_ValidationError, match="Studio space s is Failed"):
            checks_analytics._create_space(_ctx(), sagemaker, _deployment(), owner="u", space="s")


class TestStartHomeProbe:
    def test_the_probe_app_starts_with_the_lifecycle_config_recorded_first(self) -> None:
        ctx = _ctx()
        ctx.session.get_partition_for_region.return_value = "aws"
        names = checks_analytics._names(ctx, 1)
        sagemaker = MagicMock()

        def create_lifecycle_config(**kwargs: Any) -> dict[str, str]:
            # Recorded before it exists, so the teardown can delete it.
            records = ctx.checkpoint.state[checks_analytics.LIFECYCLE_CONFIGS_STATE]
            assert [r["name"] for r in records] == [names["lifecycle_config"]]
            return {"StudioLifecycleConfigArn": _LCC_ARN}

        sagemaker.create_studio_lifecycle_config.side_effect = create_lifecycle_config
        sagemaker.describe_user_profile.side_effect = [
            {"Status": "Updating"},
            {"Status": "InService"},
        ]
        sagemaker.describe_space.return_value = {"Status": "InService"}
        with _patched():
            started = checks_analytics._start_home_probe(ctx, sagemaker, _deployment(), names)
        image = "arn:aws:sagemaker:us-east-2:137914896644:image/sagemaker-distribution-cpu"
        assert started == {
            "lifecycle_config": names["lifecycle_config"],
            "space": names["home_space"],
            "image": image,
        }
        created = sagemaker.create_studio_lifecycle_config.call_args.kwargs
        assert created["StudioLifecycleConfigName"] == names["lifecycle_config"]
        assert created["StudioLifecycleConfigAppType"] == "JupyterLab"
        script = base64.b64decode(created["StudioLifecycleConfigContent"]).decode()
        assert f"custom-file-systems/efs/{_EFS}" in script
        assert f'Bucket="{_BUCKET}"' in script
        assert f'Key="{names["report_key"]}"' in script
        assert "__" not in script.replace("__name__", "")
        assert script.rstrip().endswith("exit 0")
        sagemaker.update_user_profile.assert_called_once_with(
            DomainId=_DOMAIN,
            UserProfileName=names["first"],
            UserSettings={"JupyterLabAppSettings": {"LifecycleConfigArns": [_LCC_ARN]}},
        )
        space = sagemaker.create_space.call_args.kwargs
        assert space["SpaceName"] == names["home_space"]
        assert space["OwnershipSettings"] == {"OwnerUserProfileName": names["first"]}
        sagemaker.create_app.assert_called_once_with(
            DomainId=_DOMAIN,
            SpaceName=names["home_space"],
            AppType="JupyterLab",
            AppName="default",
            ResourceSpec={
                "InstanceType": "ml.t3.medium",
                "SageMakerImageArn": image,
                "LifecycleConfigArn": _LCC_ARN,
            },
            Tags=checks_analytics._run_tags(ctx),
        )

    def test_a_region_without_a_distribution_image_account_is_refused(self) -> None:
        sagemaker = MagicMock()
        with pytest.raises(_ValidationError, match="No SageMaker Distribution image account"):
            checks_analytics._start_home_probe(
                _ctx(),
                sagemaker,
                _deployment(region="xx-east-9"),
                checks_analytics._names(_ctx(), 1),
            )
        sagemaker.create_studio_lifecycle_config.assert_not_called()

    def test_the_profile_must_return_to_in_service(self) -> None:
        ctx = _ctx()
        sagemaker = MagicMock()
        sagemaker.create_studio_lifecycle_config.return_value = {
            "StudioLifecycleConfigArn": _LCC_ARN
        }
        sagemaker.describe_user_profile.side_effect = _client_error("ResourceNotFound")
        with _patched(), pytest.raises(_ValidationError, match="is still None after 600s"):
            checks_analytics._start_home_probe(
                ctx, sagemaker, _deployment(), checks_analytics._names(ctx, 1)
            )
        sagemaker.create_space.assert_not_called()


class TestFinishHomeProbe:
    @staticmethod
    def _environment(
        apps: list[Any], reports: list[Any]
    ) -> tuple[SimpleNamespace, MagicMock, MagicMock]:
        ctx = _ctx()
        sagemaker = MagicMock()
        sagemaker.describe_app.side_effect = apps
        s3 = MagicMock()
        s3.get_object.side_effect = [
            {"Body": io.BytesIO(json.dumps(report).encode())}
            if isinstance(report, dict)
            else report
            for report in reports
        ]
        ctx.session.client.side_effect = lambda service, **_kwargs: {"s3": s3}[service]
        return ctx, sagemaker, s3

    def _finish(self, ctx: SimpleNamespace, sagemaker: MagicMock) -> dict[str, Any]:
        names = checks_analytics._names(ctx, 1)
        with _patched():
            return checks_analytics._finish_home_probe(
                ctx, sagemaker, _deployment(), names, {"lifecycle_config": "probe"}
            )

    def test_the_app_wrote_into_the_users_directory(self) -> None:
        report = {"uid": 1000, "gid": 100, "entries": ["notebook.ipynb"], "writable": True}
        ctx, sagemaker, s3 = self._environment(
            [_client_error("ResourceNotFound"), {"Status": "Pending"}, {"Status": "InService"}],
            [_client_error("NoSuchKey"), _client_error("404"), report],
        )
        assert self._finish(ctx, sagemaker) == {
            "lifecycle_config": "probe",
            "app": "InService",
            "writable": True,
            "uid": 1000,
            "gid": 100,
        }
        names = checks_analytics._names(ctx, 1)
        s3.get_object.assert_called_with(Bucket=_BUCKET, Key=names["report_key"])

    @pytest.mark.parametrize("status", ["Failed", "Deleted", "Deleting"])
    def test_an_app_that_stops_is_an_error_with_its_reason(self, status: str) -> None:
        app = {"Status": status, "FailureReason": "mount failed"}
        ctx, sagemaker, _s3 = self._environment([app], [])
        with pytest.raises(_ValidationError, match=f"is {status}: mount failed"):
            self._finish(ctx, sagemaker)

    def test_an_app_that_never_starts_gives_up_at_the_deadline(self) -> None:
        ctx, sagemaker, _s3 = self._environment([], [])
        sagemaker.describe_app.side_effect = None
        sagemaker.describe_app.return_value = {"Status": "Pending"}
        with pytest.raises(_ValidationError, match="is Pending: no reason"):
            self._finish(ctx, sagemaker)
        assert sagemaker.describe_app.call_count == 1500 // 15 + 1

    def test_a_report_that_never_arrives_gives_up_at_the_deadline(self) -> None:
        ctx, sagemaker, s3 = self._environment([{"Status": "InService"}], [])
        s3.get_object.side_effect = _client_error("NoSuchKey")
        with pytest.raises(_ValidationError, match="wrote no report in 600s"):
            self._finish(ctx, sagemaker)

    def test_another_s3_error_propagates(self) -> None:
        ctx, sagemaker, _s3 = self._environment(
            [{"Status": "InService"}], [_client_error("AccessDenied")]
        )
        with pytest.raises(ClientError):
            self._finish(ctx, sagemaker)

    @pytest.mark.parametrize(
        ("report", "match"),
        [
            (
                {"writable": False, "error": "PermissionError: denied"},
                "cannot write its Studio EFS directory: PermissionError: denied",
            ),
            ({"writable": True, "entries": ["home"]}, "mounted the Studio EFS root"),
        ],
    )
    def test_a_report_must_show_a_writable_user_directory(
        self, report: dict[str, Any], match: str
    ) -> None:
        ctx, sagemaker, _s3 = self._environment([{"Status": "InService"}], [report])
        with pytest.raises(_ValidationError, match=match):
            self._finish(ctx, sagemaker)


# ---------------------------------------------------------------------------
# Profile ownership and removal
# ---------------------------------------------------------------------------


_FOREIGN = _done(2, stderr="login failed: HTTP 403 UserProfileOwnedByAnotherUser, correlation_id=1")


class TestOwnerRefusal:
    @staticmethod
    def _results(**overrides: Any) -> list[Any]:
        kept = {"username": "first", "cognito_user_deleted": True, "studio_profile": None}
        created = {"username": "first", "password_state": "permanent"}
        removed = {"studio_profile": {"profile": "owned_by_another_user", "spaces": [], "apps": []}}
        return [
            _json(overrides.get("kept", kept)),
            _json(overrides.get("created", created)),
            overrides.get("refused", _FOREIGN),
            _json(overrides.get("removed", removed)),
        ]

    def _check(self, gco: _Gco, *, sub: str = _OTHER_SUB, profile: Any = None) -> dict[str, Any]:
        sagemaker = _sagemaker(profile if profile is not None else _profile("first"))
        with _patched(gco, "Permanent-1!"):
            return checks_analytics._check_owner_refusal(
                _ctx(), [], sagemaker, _cognito(sub), _deployment(), "first", _SUB
            )

    def test_a_new_user_with_the_first_ones_name_is_refused_and_cannot_remove_it(self) -> None:
        gco = _Gco(*self._results())
        assert self._check(gco) == {
            "username": "first",
            "refused": "HTTP 403 UserProfileOwnedByAnotherUser",
            "profile_left": True,
        }
        keep, readd, login, remove = gco.calls
        assert keep["args"][-4:] == ["--username", "first", "--keep-studio-profile", "--yes"]
        assert readd["args"][-2:] == ["--username", "first"]
        assert readd["env"]["GCO_STUDIO_PASSWORD"] == "Permanent-1!"
        assert login["env"]["GCO_STUDIO_PASSWORD"] == "Permanent-1!"
        assert remove["args"][-3:] == ["--username", "first", "--yes"]

    @pytest.mark.parametrize(
        "kept",
        [
            {"cognito_user_deleted": False, "studio_profile": None},
            {"cognito_user_deleted": True, "studio_profile": {"profile": "deleted"}},
        ],
    )
    def test_keep_studio_profile_must_delete_only_the_cognito_user(
        self, kept: dict[str, Any]
    ) -> None:
        with pytest.raises(_ValidationError, match="users remove --keep-studio-profile reported"):
            self._check(_Gco(*self._results(kept=kept)))

    def test_users_add_with_a_password_must_set_it_permanent(self) -> None:
        gco = _Gco(*self._results(created={"password_state": "temporary"}))
        with pytest.raises(_ValidationError, match="did not set a permanent password"):
            self._check(gco)

    def test_the_new_user_must_have_a_new_sub(self) -> None:
        with pytest.raises(_ValidationError, match="gave the new first the old sub"):
            self._check(_Gco(*self._results()), sub=_SUB)

    def test_the_login_must_be_refused_with_403(self) -> None:
        gco = _Gco(*self._results(refused=_done(0, stdout=_URL)))
        with pytest.raises(_ValidationError, match="foreign-login: gco exited 0, expected 2"):
            self._check(gco)

    def test_users_remove_must_leave_another_users_profile_alone(self) -> None:
        gco = _Gco(*self._results(removed={"studio_profile": {"profile": "deleted"}}))
        with pytest.raises(_ValidationError, match="acted on another user's profile"):
            self._check(gco)
        gco = _Gco(*self._results(removed={}))
        with pytest.raises(_ValidationError, match=r"acted on another user's profile: \{\}"):
            self._check(gco)

    def test_the_profile_must_survive(self) -> None:
        gco = _Gco(*self._results())
        sagemaker = _sagemaker(None)
        with (
            _patched(gco, "Permanent-1!"),
            pytest.raises(_ValidationError, match="users remove deleted the profile first"),
        ):
            checks_analytics._check_owner_refusal(
                _ctx(), [], sagemaker, _cognito(_OTHER_SUB), _deployment(), "first", _SUB
            )


class TestProfileRemoval:
    @staticmethod
    def _names() -> dict[str, str]:
        return {"second": "second", "second_space": "second-space"}

    @staticmethod
    def _results(studio: dict[str, Any] | None = None) -> list[Any]:
        removed = {
            "studio_profile": (
                studio
                if studio is not None
                else {"profile": "deleted", "spaces": ["second-space"], "apps": []}
            )
        }
        return [_json({"password_state": "permanent"}), _done(0, stdout=_URL), _json(removed)]

    @staticmethod
    def _sagemaker(*, profile_after: Any = None, space_after: Any = None) -> MagicMock:
        sagemaker = _sagemaker(_profile("second"), profile_after)
        sagemaker.describe_space.side_effect = [
            {"Status": "InService"},
            space_after if space_after is not None else _client_error("ResourceNotFound"),
        ]
        return sagemaker

    def _check(
        self, gco: _Gco, sagemaker: MagicMock, cognito: MagicMock | None = None
    ) -> dict[str, Any]:
        with _patched(gco, "Permanent-1!"):
            return checks_analytics._check_profile_removal(
                _ctx(),
                [],
                sagemaker,
                cognito or _cognito(_SUB, missing_after=True),
                _deployment(),
                self._names(),
            )

    def test_users_remove_deletes_the_profile_with_its_space(self) -> None:
        gco = _Gco(*self._results())
        sagemaker = self._sagemaker()
        assert self._check(gco, sagemaker) == {
            "username": "second",
            "profile": "deleted",
            "spaces": ["second-space"],
        }
        add, login, remove = gco.calls
        assert add["env"]["GCO_STUDIO_PASSWORD"] == "Permanent-1!"
        assert login["env"]["GCO_STUDIO_PASSWORD"] == "Permanent-1!"
        assert remove["args"][-3:] == ["--username", "second", "--yes"]
        assert sagemaker.create_space.call_args.kwargs["OwnershipSettings"] == {
            "OwnerUserProfileName": "second"
        }

    @pytest.mark.parametrize(
        "studio",
        [
            {"profile": "absent", "spaces": ["second-space"]},
            {"profile": "deleted", "spaces": []},
        ],
    )
    def test_the_report_must_name_the_deleted_profile_and_space(
        self, studio: dict[str, Any]
    ) -> None:
        with pytest.raises(_ValidationError, match="users remove reported"):
            self._check(_Gco(*self._results(studio)), self._sagemaker())

    def test_a_surviving_profile_is_an_error(self) -> None:
        sagemaker = self._sagemaker(profile_after=_profile("second"))
        with pytest.raises(_ValidationError, match="Studio profile second survived users remove"):
            self._check(_Gco(*self._results()), sagemaker)

    def test_a_surviving_space_is_an_error(self) -> None:
        sagemaker = self._sagemaker(space_after={"Status": "InService"})
        with pytest.raises(_ValidationError, match="Studio space second-space survived"):
            self._check(_Gco(*self._results()), sagemaker)

    def test_a_surviving_cognito_user_is_an_error(self) -> None:
        cognito = _cognito(_SUB, _SUB)
        with pytest.raises(_ValidationError, match="Cognito user second survived users remove"):
            self._check(_Gco(*self._results()), self._sagemaker(), cognito)

    def test_an_unexpected_cognito_error_propagates(self) -> None:
        cognito = _cognito(_SUB)
        cognito.admin_get_user.side_effect = [
            {"UserAttributes": [{"Name": "sub", "Value": _SUB}]},
            _client_error("AccessDeniedException"),
        ]
        with pytest.raises(ClientError):
            self._check(_Gco(*self._results()), self._sagemaker(), cognito)


# ---------------------------------------------------------------------------
# The Studio execution role
# ---------------------------------------------------------------------------


class TestStudioRole:
    @staticmethod
    def _iam(ctx: SimpleNamespace, decide: Any) -> MagicMock:
        iam = MagicMock()
        iam.simulate_principal_policy.side_effect = lambda **kwargs: decide(
            kwargs["ActionNames"][0], kwargs["ResourceArns"][0]
        )
        ctx.session.client.side_effect = lambda service, **_kwargs: {"iam": iam}[service]
        ctx.session.get_partition_for_region.return_value = "aws"
        return iam

    @staticmethod
    def _expected(action: str, resource: str) -> dict[str, Any]:
        allowed = not resource.endswith("-not-granted")
        return {"EvaluationResults": [{"EvalDecision": "allowed" if allowed else "implicitDeny"}]}

    def test_the_role_may_do_what_notebooks_rely_on_and_no_query_beyond_it(self) -> None:
        ctx = _ctx(regions=(_REGION, "us-west-2"))
        iam = self._iam(ctx, self._expected)
        evidence = checks_analytics._check_studio_role(ctx, _deployment())
        decisions = evidence["decisions"]
        assert len(decisions) == 4 + 2 * 2 + 1
        assert {d["action"] for d in decisions} == {
            "logs:GetQueryResults",
            "s3:PutObject",
            "s3:GetObject",
            "elasticfilesystem:ClientMount",
            "logs:StartQuery",
            "sqs:SendMessage",
        }
        assert decisions[-1] == {
            "action": "logs:StartQuery",
            "resource": (
                f"arn:aws:logs:{_REGION}:{_ACCOUNT}:log-group:/aws/lambda/{_PROJECT}-not-granted"
            ),
            "decision": "implicitDeny",
        }
        assert {
            "action": "sqs:SendMessage",
            "resource": f"arn:aws:sqs:us-west-2:{_ACCOUNT}:{_PROJECT}-jobs-us-west-2",
            "decision": "allowed",
        } in decisions
        assert {
            "action": "elasticfilesystem:ClientMount",
            "resource": f"arn:aws:elasticfilesystem:{_REGION}:{_ACCOUNT}:file-system/{_EFS}",
            "decision": "allowed",
        } in decisions
        for invocation in iam.simulate_principal_policy.call_args_list:
            assert invocation.kwargs["PolicySourceArn"] == _ROLE

    def test_a_missing_grant_and_an_extra_one_are_both_named(self) -> None:
        ctx = _ctx()

        def decide(action: str, resource: str) -> dict[str, Any]:
            if action == "sqs:SendMessage":
                return {"EvaluationResults": []}
            return {"EvaluationResults": [{"EvalDecision": "allowed"}]}

        self._iam(ctx, decide)
        with pytest.raises(_ValidationError) as raised:
            checks_analytics._check_studio_role(ctx, _deployment())
        message = str(raised.value)
        assert message.startswith("Studio execution role: sqs:SendMessage on ")
        assert ": None; logs:StartQuery on " in message
        assert message.endswith(f"/aws/lambda/{_PROJECT}-not-granted: allowed")


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


_STEPS = (
    "_check_doctor",
    "_check_username_rule",
    "_check_first_sign_in",
    "_start_home_probe",
    "_check_unsupported_username",
    "_check_profile_removal",
    "_check_owner_refusal",
    "_check_studio_role",
    "_finish_home_probe",
)


class TestProbe:
    @staticmethod
    def _steps(order: list[str], **overrides: Any) -> dict[str, MagicMock]:
        def step(name: str, result: Any) -> MagicMock:
            def run(*args: Any, **_kwargs: Any) -> Any:
                order.append(name)
                if isinstance(result, BaseException):
                    raise result
                return result

            return MagicMock(side_effect=run)

        results: dict[str, Any] = {
            "_check_doctor": {"passed": True},
            "_check_username_rule": {"refused": True},
            "_check_first_sign_in": ({"username": "first"}, _SUB),
            "_start_home_probe": {"lifecycle_config": "probe"},
            "_check_unsupported_username": {"refused": "HTTP 400 UnsupportedUsername"},
            "_check_profile_removal": {"profile": "deleted"},
            "_check_owner_refusal": {"profile_left": True},
            "_check_studio_role": {"decisions": []},
            "_finish_home_probe": {"app": "InService"},
        }
        results.update(overrides)
        return {name: step(name, results[name]) for name in _STEPS}

    def test_every_check_runs_in_order_and_the_first_user_is_left_for_the_teardown(
        self,
    ) -> None:
        ctx = _ctx()
        ctx.session.client.side_effect = lambda service, **_kwargs: MagicMock(name=service)
        order: list[str] = []
        steps = self._steps(order)
        with (
            patch.object(checks_analytics, "_deployment", return_value=_deployment()),
            patch.multiple(checks_analytics, **steps),
        ):
            evidence = checks_analytics.probe_analytics(ctx)
        assert order == list(_STEPS)
        names = checks_analytics._names(ctx, 1)
        assert evidence["attempt"] == 1
        assert evidence["doctor"] == {"passed": True}
        assert evidence["first_sign_in"] == {"username": "first"}
        assert evidence["efs_home"] == {"app": "InService"}
        assert evidence["left_for_teardown"] == {
            "profile": names["first"],
            "space": names["home_space"],
            "app": "JupyterLab/default",
            "lifecycle_config": names["lifecycle_config"],
        }
        assert ctx.checkpoint.state["analytics_attempts"] == 1
        assert ctx.checkpoint.state["analytics_domain"] == {
            "region": _REGION,
            "domain_id": _DOMAIN,
            "domain_arn": _DOMAIN_ARN,
            "home_efs_id": _HOME_EFS,
        }
        assert ctx.checkpoint.state["analytics_probe"] is evidence
        owner_args = steps["_check_owner_refusal"].call_args.args
        assert owner_args[-2:] == (names["first"], _SUB)
        assert steps["_check_unsupported_username"].call_args.args[-1] == names["unsupported"]
        assert steps["_check_username_rule"].call_args.args[-1] == names["refused"]
        assert steps["_finish_home_probe"].call_args.args[-1] == {"lifecycle_config": "probe"}

    def test_a_failed_step_leaves_its_commands_in_the_checkpoint_and_a_retry_new_names(
        self,
    ) -> None:
        ctx = _ctx()
        ctx.checkpoint.state["analytics_attempts"] = 1
        order: list[str] = []

        def doctor(_ctx: Any, commands: list[dict[str, Any]]) -> None:
            commands.append({"stage": "doctor", "returncode": 1})
            raise _ValidationError("gco analytics doctor failed: ['efs']")

        steps = self._steps(order)
        steps["_check_doctor"] = MagicMock(side_effect=doctor)
        with (
            patch.object(checks_analytics, "_deployment", return_value=_deployment()),
            patch.multiple(checks_analytics, **steps),
            pytest.raises(_ValidationError, match="doctor failed"),
        ):
            checks_analytics.probe_analytics(ctx)
        assert order == []
        probe = ctx.checkpoint.state["analytics_probe"]
        assert probe["attempt"] == 2
        assert probe["commands"] == [{"stage": "doctor", "returncode": 1}]
        assert ctx.checkpoint.state["analytics_attempts"] == 2


# ---------------------------------------------------------------------------
# The action
# ---------------------------------------------------------------------------


class TestAction:
    def test_with_the_environment_on_the_probe_is_the_evidence(self) -> None:
        ctx = _ctx()
        with patch.object(actions_analytics, "probe_analytics", return_value={"attempt": 1}):
            evidence = actions_analytics.action_analytics(ctx)
        assert evidence == {
            "enablement": {
                "enabled": True,
                "source": "run-override (analytics_environment_overrides)",
                "region": _REGION,
            },
            "analytics": {
                "status": "validated",
                "source": "run-override (analytics_environment_overrides)",
                "attempt": 1,
            },
        }
        assert ctx.checkpoint.state["analytics_validation"] == evidence
        ctx.persist.assert_called_once()

    def test_with_the_environment_off_the_action_skips_and_names_the_source(self) -> None:
        ctx = _ctx(overrides="")
        with patch.object(actions_analytics, "probe_analytics") as probe:
            evidence = actions_analytics.action_analytics(ctx)
        probe.assert_not_called()
        assert evidence["analytics"]["status"] == "skipped"
        assert evidence["analytics"]["reason"].startswith(
            "Disabled by cdk.json analytics_environment.enabled; a release run"
        )
        assert ctx.checkpoint.state["analytics_validation"] == evidence


# ---------------------------------------------------------------------------
# The teardown sweep
# ---------------------------------------------------------------------------


class TestCleanup:
    @staticmethod
    def _environment(
        records: list[dict[str, Any]] | None = None, domain: dict[str, Any] | None = None
    ) -> tuple[SimpleNamespace, MagicMock, MagicMock]:
        ctx = _ctx()
        if records is not None:
            ctx.checkpoint.state[checks_analytics.LIFECYCLE_CONFIGS_STATE] = records
        if domain is not None:
            ctx.checkpoint.state["analytics_domain"] = domain
        sagemaker = MagicMock()
        efs = MagicMock()
        clients = {"sagemaker": sagemaker, "efs": efs}
        ctx.session.client.side_effect = lambda service, **_kwargs: clients[service]
        return ctx, sagemaker, efs

    def test_a_run_without_the_probe_has_nothing_to_sweep(self) -> None:
        ctx, _sagemaker_client, _efs = self._environment()
        assert cleanup_analytics.cleanup_analytics_residue(ctx) == {
            "deleted_lifecycle_configs": [],
            "absent_lifecycle_configs": [],
            "errors": [],
        }
        ctx.session.client.assert_not_called()

    def test_recorded_lifecycle_configs_go_and_the_home_efs_is_proved_gone(self) -> None:
        ctx, sagemaker, efs = self._environment(
            records=[{"region": _REGION, "name": "a"}, {"region": _REGION, "name": "b"}],
            domain={"region": _REGION, "domain_id": _DOMAIN, "home_efs_id": _HOME_EFS},
        )
        sagemaker.delete_studio_lifecycle_config.side_effect = [
            None,
            _client_error("ResourceNotFound"),
        ]
        efs.describe_file_systems.side_effect = _client_error("FileSystemNotFound")
        assert cleanup_analytics.cleanup_analytics_residue(ctx) == {
            "deleted_lifecycle_configs": [f"{_REGION}:a"],
            "absent_lifecycle_configs": [f"{_REGION}:b"],
            "errors": [],
            "home_efs": "absent",
        }
        assert sagemaker.delete_studio_lifecycle_config.call_args_list == [
            call(StudioLifecycleConfigName="a"),
            call(StudioLifecycleConfigName="b"),
        ]
        ctx.session.client.assert_any_call("sagemaker", region_name=_REGION)
        ctx.session.client.assert_any_call("efs", region_name=_REGION)
        efs.describe_file_systems.assert_called_once_with(FileSystemId=_HOME_EFS)

    def test_anything_left_raises_with_the_partial_evidence(self) -> None:
        ctx, sagemaker, efs = self._environment(
            records=[
                {"region": _REGION, "name": "a"},
                {"region": "", "name": "b"},
                {"region": _REGION, "name": "c"},
            ],
            domain={"region": _REGION, "home_efs_id": _HOME_EFS},
        )
        sagemaker.delete_studio_lifecycle_config.side_effect = [
            _client_error("AccessDeniedException"),
            None,
        ]
        efs.describe_file_systems.return_value = {"FileSystems": [{"FileSystemId": _HOME_EFS}]}
        with pytest.raises(cleanup_analytics.AnalyticsResidueError) as raised:
            cleanup_analytics.cleanup_analytics_residue(ctx)
        details = raised.value.details
        assert details["deleted_lifecycle_configs"] == [f"{_REGION}:c"]
        assert details["errors"] == [
            f"{_REGION}:a: AccessDeniedException",
            "invalid lifecycle-config record {'region': '', 'name': 'b'}",
            f"The Studio domain's home EFS {_HOME_EFS} survived the analytics stack",
        ]
        assert str(raised.value) == "; ".join(details["errors"])

    def test_a_home_efs_that_cannot_be_read_is_not_proved_gone(self) -> None:
        ctx, _sagemaker_client, efs = self._environment(
            domain={"region": _REGION, "home_efs_id": _HOME_EFS}
        )
        efs.describe_file_systems.side_effect = _client_error("AccessDeniedException")
        with pytest.raises(
            cleanup_analytics.AnalyticsResidueError,
            match=f"home EFS {_HOME_EFS}: AccessDeniedException",
        ) as raised:
            cleanup_analytics.cleanup_analytics_residue(ctx)
        assert "home_efs" not in raised.value.details
