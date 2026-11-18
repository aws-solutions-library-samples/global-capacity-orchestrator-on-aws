"""The MCP role reaches every process the server starts, and keeps refreshing.

``GCO_MCP_ROLE_ARN`` used to replace only boto3's in-process default session,
with static credentials that stopped working after
``GCO_MCP_ROLE_DURATION_SECONDS``, while every ``gco`` and kubectl child
resolved the operator's ambient (often administrator) credentials.

``iam.assume_mcp_role`` now gives the default session refreshable role
credentials and serves the role to children through the AWS SDKs'
container credentials provider: a loopback endpoint guarded by a random
token, plus a child environment without the server's credential variables
and with private AWS config files whose profiles carry no credentials. These
tests use real boto3/botocore with a fake STS client; children are real
Python processes that resolve credentials the way ``gco`` does.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import boto3
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gco_mcp"))

import cli_runner
import iam
from tools._long_task import _run_long_task

ROLE_ARN = "arn:aws:iam::123456789012:role/gco-mcp"
AMBIENT_KEY = "AKIAAMBIENTADMIN0000"

#: The operator's AWS files: every profile here would hand a child other
#: credentials than the role's if the child could read it.
_CONFIG = """\
[default]
region = us-west-2
credential_process = /bin/false
s3 =
    addressing_style = path
services = local

[profile admin]
region = eu-west-1
role_arn = arn:aws:iam::123456789012:role/admin
source_profile = default

[profile sso]
sso_session = corp
sso_account_id = 123456789012
sso_role_name = Admin

[sso-session corp]
sso_start_url = https://example.com/start
sso_region = us-east-1

[services local]
s3 =
    endpoint_url = http://localhost:4566
"""
_CREDENTIALS = """\
[default]
aws_access_key_id = AKIADEFAULTPROFILE00
aws_secret_access_key = default-secret

[admin]
aws_access_key_id = AKIAADMINPROFILE0000
aws_secret_access_key = admin-secret
"""
_BOTO = """\
[Credentials]
aws_access_key_id = AKIABOTOCONFIG000000
aws_secret_access_key = boto-secret
"""

#: Prints how boto3 resolves credentials in a child, the way ``gco`` would.
_RESOLVE = (
    "import json, boto3; c = boto3.Session().get_credentials(); "
    "print(json.dumps({'method': c.method, 'access_key': c.access_key}))"
)


class _FakeSts:
    """STS stand-in: each AssumeRole returns a new key with ``lifetime`` left."""

    def __init__(self, lifetime: timedelta = timedelta(hours=1)) -> None:
        self.lifetime = lifetime
        self.calls: list[dict[str, Any]] = []
        self.regions: list[str | None] = []
        self.source_keys: list[str | None] = []

    def client(self, session: Any, service_name: str, **kwargs: Any) -> _FakeSts:
        assert service_name == "sts"
        self.regions.append(kwargs.get("region_name"))
        self.source_keys.append(kwargs.get("aws_access_key_id"))
        return self

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": f"ASIAROLE{len(self.calls):012d}",
                "SecretAccessKey": "role-secret",
                "SessionToken": "role-token",
                "Expiration": datetime.now(UTC) + self.lifetime,
            },
            "AssumedRoleUser": {
                "AssumedRoleId": "AROAEXAMPLE:gco-mcp-server",
                "Arn": "arn:aws:sts::123456789012:assumed-role/gco-mcp/gco-mcp-server",
            },
        }


@pytest.fixture(autouse=True)
def _restore_role_state() -> Iterator[None]:
    saved = boto3.DEFAULT_SESSION
    yield
    iam._install_child_credentials(None)
    boto3.DEFAULT_SESSION = saved


@pytest.fixture
def aws_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Ambient env credentials plus AWS files full of other credentials."""
    home = tmp_path / "home"
    (home / ".aws").mkdir(parents=True)
    (home / ".aws" / "config").write_text(_CONFIG)
    (home / ".aws" / "credentials").write_text(_CREDENTIALS)
    (home / ".boto").write_text(_BOTO)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / ".aws" / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(home / ".aws" / "credentials"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", AMBIENT_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "ambient-token")
    monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", str(home / "pod-token"))
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "BOTO_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GCO_MCP_ROLE_ARN", ROLE_ARN)
    monkeypatch.setenv("GCO_MCP_ROLE_SESSION_NAME", "gco-mcp-test")
    monkeypatch.setenv("GCO_MCP_ROLE_DURATION_SECONDS", "900")
    monkeypatch.setenv("GCO_DISABLE_TASK_STATUS", "1")
    return home


@pytest.fixture
def sts() -> Iterator[_FakeSts]:
    fake = _FakeSts()
    with patch.object(
        boto3.Session, "client", lambda session, name, **kw: fake.client(session, name, **kw)
    ):
        yield fake


def _resolve_in_child(env: dict[str, str] | None) -> dict[str, str]:
    child = subprocess.run(
        [sys.executable, "-c", _RESOLVE],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    return dict(json.loads(child.stdout))


# ---------------------------------------------------------------------------
# Children get the role, never the ambient credentials
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", [None, "default", "admin", "sso"])
def test_children_resolve_the_role_whatever_profile_they_select(
    aws_home: Path, sts: _FakeSts, profile: str | None
) -> None:
    iam.assume_mcp_role()
    env = iam.child_environment()
    assert env is not None
    if profile:
        env["AWS_PROFILE"] = profile
    assert _resolve_in_child(env) == {
        "method": "container-role",
        "access_key": "ASIAROLE000000000001",
    }
    # The ambient credentials were the source for AssumeRole, and only that.
    assert sts.source_keys == [AMBIENT_KEY]
    assert sts.calls == [
        {"RoleArn": ROLE_ARN, "RoleSessionName": "gco-mcp-test", "DurationSeconds": 900}
    ]


def test_without_the_fix_a_child_would_use_the_ambient_key(aws_home: Path, sts: _FakeSts) -> None:
    """Control: the inherited environment resolves the ambient credentials."""
    iam.assume_mcp_role()
    assert _resolve_in_child(dict(os.environ))["access_key"] == AMBIENT_KEY


def test_child_environment_drops_credential_variables_and_keeps_the_rest(
    aws_home: Path, sts: _FakeSts, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "admin")
    iam.assume_mcp_role()
    env = iam.child_environment({"GCO_STUDIO_PASSWORD": "pw", "AWS_REGION": "eu-north-1"})
    assert env is not None
    for name in iam.CHILD_CREDENTIAL_VARIABLES:
        assert name not in env
    assert env["AWS_PROFILE"] == "admin"  # selects a profile without credentials
    assert env["AWS_CONTAINER_CREDENTIALS_FULL_URI"].startswith("http://127.0.0.1:")
    assert env["AWS_CONTAINER_AUTHORIZATION_TOKEN"]
    assert env["GCO_STUDIO_PASSWORD"] == "pw"
    assert env["AWS_REGION"] == "eu-north-1"  # a tool's own variables come last
    private = Path(env["AWS_CONFIG_FILE"]).parent
    assert oct(private.stat().st_mode & 0o777) == "0o700"
    for name in ("config", "credentials", "boto"):
        assert oct((private / name).stat().st_mode & 0o777) == "0o600"
    assert Path(env["BOTO_CONFIG"]).read_text() == ""
    assert Path(env["AWS_SHARED_CREDENTIALS_FILE"]).read_text() == ""


def test_child_environment_without_a_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", AMBIENT_KEY)
    assert iam.child_environment() is None
    env = iam.child_environment({"GCO_WEBHOOK_SECRET": "s"})
    assert env is not None
    assert env["AWS_ACCESS_KEY_ID"] == AMBIENT_KEY
    assert env["GCO_WEBHOOK_SECRET"] == "s"


# ---------------------------------------------------------------------------
# Every spawn site uses the child environment
# ---------------------------------------------------------------------------

_FAKE_GCO = """
import json, os
import boto3
credentials = boto3.Session().get_credentials()
report = {"method": credentials.method, "access_key": credentials.access_key}
with open(os.environ["PROBE_OUT"], "w") as fh:
    json.dump(report, fh)
print(json.dumps(report))
"""


@pytest.mark.parametrize("runner", ["sync", "async", "long-task"])
def test_gco_children_of_every_runner_get_the_role(
    aws_home: Path, sts: _FakeSts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: str
) -> None:
    fake = tmp_path / "gco"
    fake.write_text(f"#!{sys.executable}\n{_FAKE_GCO}")
    fake.chmod(0o755)
    probe = tmp_path / "probe.json"
    monkeypatch.setenv("PROBE_OUT", str(probe))
    monkeypatch.setattr(cli_runner, "_gco_executable", lambda: str(fake))
    iam.assume_mcp_role()
    if runner == "sync":
        cli_runner._run_cli("probe", env={"GCO_STUDIO_PASSWORD": "pw"})
    elif runner == "async":
        asyncio.run(cli_runner._run_cli_async("probe"))
    else:
        asyncio.run(_run_long_task(["gco", "probe"], ctx=None, progress=None, is_stack_op=False))
    assert json.loads(probe.read_text()) == {
        "method": "container-role",
        "access_key": "ASIAROLE000000000001",
    }


@pytest.mark.parametrize(
    ("module", "call"),
    [
        ("resources.jobs", lambda m: m._job_resource_for_region("us-east-1", "train")),
        ("resources.cluster", lambda m: m._pending_pods("us-east-1")),
        (
            "resources.k8s",
            lambda m: m._k8s_live_resource_for_region("us-east-1", "gco-jobs", "job", "train"),
        ),
    ],
    ids=["jobs", "cluster", "k8s"],
)
def test_kubectl_resources_run_kubectl_with_the_role(
    aws_home: Path, sts: _FakeSts, module: str, call: Any
) -> None:
    import run_mcp  # noqa: F401  # registers the resource modules

    iam.assume_mcp_role()
    loaded = sys.modules[module]
    completed = subprocess.CompletedProcess([], 0, stdout='{"items": []}', stderr="")
    with (
        patch.object(loaded, "eks_context_for_region", return_value="arn:aws:eks:x"),
        patch.object(cli_runner.subprocess, "run", return_value=completed) as run,
    ):
        call(loaded)
    env = run.call_args.kwargs["env"]
    assert env["AWS_CONTAINER_CREDENTIALS_FULL_URI"].startswith("http://127.0.0.1:")
    assert "AWS_ACCESS_KEY_ID" not in env


# ---------------------------------------------------------------------------
# The role keeps refreshing, in process and for children
# ---------------------------------------------------------------------------


def test_the_default_session_refreshes_the_role(aws_home: Path) -> None:
    fake = _FakeSts(lifetime=timedelta(minutes=5))  # inside botocore's refresh window
    with patch.object(boto3.Session, "client", lambda s, n, **kw: fake.client(s, n, **kw)):
        iam.assume_mcp_role()
        first = boto3.DEFAULT_SESSION.get_credentials().get_frozen_credentials()
        second = boto3.DEFAULT_SESSION.get_credentials().get_frozen_credentials()
    assert boto3.DEFAULT_SESSION.get_credentials().method == "assume-role"
    assert len(fake.calls) >= 3  # startup, then one AssumeRole per expiring read
    assert first.access_key != second.access_key


def test_the_endpoint_serves_each_refresh_with_its_own_expiry() -> None:
    expiries = iter(
        [
            datetime.now(UTC) + timedelta(minutes=5),  # due for refresh at once
            datetime(2031, 1, 1, tzinfo=UTC),
        ]
    )
    keys = iter(["ASIAFIRST", "ASIASECOND"])

    def fetch() -> dict[str, Any]:
        return {
            "access_key": next(keys),
            "secret_key": "s",
            "token": "t",
            "expiry_time": next(expiries).isoformat(),
        }

    role = iam.RoleCredentials(fetch)
    assert role.container_document()["AccessKeyId"] == "ASIAFIRST"
    assert role.container_document() == {
        "AccessKeyId": "ASIASECOND",
        "SecretAccessKey": "s",
        "Token": "t",
        "Expiration": "2031-01-01T00:00:00Z",
    }


def test_the_endpoint_document_names_the_account(aws_home: Path, sts: _FakeSts) -> None:
    iam.assume_mcp_role()
    env = iam.child_environment()
    assert env is not None
    request = urllib.request.Request(
        env["AWS_CONTAINER_CREDENTIALS_FULL_URI"],
        headers={"Authorization": env["AWS_CONTAINER_AUTHORIZATION_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        document = json.loads(response.read())
        assert response.headers["Cache-Control"] == "no-store"
    assert document["AccessKeyId"] == "ASIAROLE000000000001"
    assert document["AccountId"] == "123456789012"
    assert document["Expiration"].endswith("Z")


# ---------------------------------------------------------------------------
# The endpoint answers only the token holder
# ---------------------------------------------------------------------------


def _get(url: str, token: str | None) -> tuple[int, dict[str, Any]]:
    headers = {} if token is None else {"Authorization": token}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=10
        ) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture
def endpoint() -> Iterator[iam.CredentialEndpoint]:
    served = iam.CredentialEndpoint(lambda: {"AccessKeyId": "ASIAOK"})
    yield served
    served.close()


def test_the_endpoint_serves_the_token_holder(endpoint: iam.CredentialEndpoint) -> None:
    assert _get(endpoint.url, endpoint.token) == (200, {"AccessKeyId": "ASIAOK"})


@pytest.mark.parametrize("token", [None, "wrong", "Bearer x", "é"])
def test_the_endpoint_refuses_a_missing_or_wrong_token(
    endpoint: iam.CredentialEndpoint, token: str | None
) -> None:
    status, body = _get(endpoint.url, token)
    assert (status, body) == (403, {"message": "forbidden"})


def test_the_endpoint_refuses_another_path(endpoint: iam.CredentialEndpoint) -> None:
    other = endpoint.url.rsplit("/", 1)[0] + "/latest"
    assert _get(other, endpoint.token)[0] == 403


def test_a_failed_refresh_is_a_500_and_an_audit_error(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> dict[str, str]:
        raise RuntimeError("ExpiredToken: the source session expired")

    served = iam.CredentialEndpoint(broken)
    caplog.set_level(logging.ERROR, logger="gco.mcp.audit")
    try:
        status, body = _get(served.url, served.token)
    finally:
        served.close()
    assert status == 500
    assert body == {"message": "the MCP server could not refresh its role"}
    record = json.loads(caplog.records[-1].message)
    assert record["event"] == "mcp.server.role_assumption.error"
    assert "ExpiredToken" in record["error"]
    assert served.token not in caplog.text


# ---------------------------------------------------------------------------
# The children's private AWS config
# ---------------------------------------------------------------------------


def test_child_config_keeps_every_profile_without_credentials(aws_home: Path) -> None:
    import botocore.session

    text = iam.child_aws_config(botocore.session.Session().full_config)
    parser = configparser.ConfigParser()
    parser.read_string(text)
    assert parser.sections() == ["default", "profile admin", "profile sso", "services local"]
    assert dict(parser["default"]) == {
        "region": "us-west-2",
        "s3": "\naddressing_style = path",
        "services": "local",
    }
    assert dict(parser["profile admin"]) == {"region": "eu-west-1"}
    assert dict(parser["profile sso"]) == {}
    assert dict(parser["services local"]) == {"s3": "\nendpoint_url = http://localhost:4566"}
    for secret in ("secret", "role_arn", "credential_process", "sso_", "AKIA"):
        assert secret not in text


# ---------------------------------------------------------------------------
# Startup: STS Region, configuration errors, and what is logged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role_arn", "region", "expected"),
    [
        (ROLE_ARN, "eu-west-1", "eu-west-1"),
        ("arn:aws-cn:iam::123456789012:role/gco-mcp", None, "cn-north-1"),
        ("arn:aws-us-gov:iam::123456789012:role/gco-mcp", None, "us-gov-west-1"),
        ("gco-mcp", None, None),
    ],
    ids=["configured", "aws-cn", "aws-us-gov", "not-an-arn"],
)
def test_assume_role_uses_a_regional_sts_endpoint(
    aws_home: Path,
    sts: _FakeSts,
    monkeypatch: pytest.MonkeyPatch,
    role_arn: str,
    region: str | None,
    expected: str | None,
) -> None:
    monkeypatch.setenv("GCO_MCP_ROLE_ARN", role_arn)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_home / "no-config"))
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    if region:
        monkeypatch.setenv("AWS_DEFAULT_REGION", region)  # botocore's Region variable
    iam.assume_mcp_role()
    assert sts.regions == [expected]


def test_a_non_numeric_duration_stops_startup(
    aws_home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GCO_MCP_ROLE_DURATION_SECONDS", "1h")
    caplog.set_level(logging.ERROR, logger="gco.mcp.audit")
    with pytest.raises(ValueError, match="whole number of seconds, not '1h'"):
        iam.assume_mcp_role()
    assert json.loads(caplog.records[-1].message)["event"] == "mcp.server.role_assumption.error"


def test_no_ambient_credentials_stops_startup(
    aws_home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_home / "no-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(aws_home / "no-credentials"))
    monkeypatch.setenv("BOTO_CONFIG", str(aws_home / "no-boto"))
    caplog.set_level(logging.ERROR, logger="gco.mcp.audit")
    with pytest.raises(RuntimeError, match="no ambient AWS credentials"):
        iam.assume_mcp_role()
    assert "no ambient AWS credentials" in json.loads(caplog.records[-1].message)["error"]
    assert iam.child_environment() is None


def test_an_endpoint_that_cannot_start_stops_startup_and_leaves_no_files(
    aws_home: Path, sts: _FakeSts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(iam.tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    with (
        patch.object(iam, "ThreadingHTTPServer", side_effect=OSError("no loopback")),
        pytest.raises(OSError, match="no loopback"),
    ):
        iam.assume_mcp_role()
    assert list((tmp_path / "tmp").iterdir()) == []
    assert iam.child_environment() is None


def test_success_is_audited_without_secrets(
    aws_home: Path, sts: _FakeSts, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="gco.mcp.audit")
    iam.assume_mcp_role()
    record = json.loads(caplog.records[-1].message)
    assert record["event"] == "mcp.server.role_assumption.success"
    assert record["role_arn"] == ROLE_ARN
    assert record["expiration"].endswith("Z")
    env = iam.child_environment()
    assert env is not None
    for secret in ("role-secret", "role-token", env["AWS_CONTAINER_AUTHORIZATION_TOKEN"]):
        assert secret not in caplog.text


def test_assuming_again_replaces_the_previous_endpoint(aws_home: Path, sts: _FakeSts) -> None:
    iam.assume_mcp_role()
    first = iam.child_environment()
    iam.assume_mcp_role()
    second = iam.child_environment()
    assert first is not None and second is not None
    assert (
        first["AWS_CONTAINER_CREDENTIALS_FULL_URI"] != second["AWS_CONTAINER_CREDENTIALS_FULL_URI"]
    )
    assert not Path(first["AWS_CONFIG_FILE"]).exists()
    with pytest.raises(urllib.error.URLError):
        _get(
            first["AWS_CONTAINER_CREDENTIALS_FULL_URI"], first["AWS_CONTAINER_AUTHORIZATION_TOKEN"]
        )
