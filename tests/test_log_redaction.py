"""Credentials never reach the CLI's log output, in any verbosity mode.

Pins the fix for the privately reported v8.15.2 exposure: ``gco -v`` raised
the AWS SDK loggers to DEBUG, and botocore's debug output carries the SigV4
``CanonicalRequest`` (session token), the ``Authorization`` signature and,
from ``botocore.parsers``, every response body, which for ``sts:AssumeRole``
is a complete set of temporary credentials.

Three layers are tested here:

* ``cli.log_redaction.redact`` and ``CredentialRedactionFilter`` against the
  exact shapes botocore, STS and the config file produce;
* ``cli.main._configure_cli_logging``'s level matrix (``-v`` opens the SDK
  loggers to INFO and no further; ``--debug-aws`` opens them to DEBUG except
  ``botocore.parsers``) and the ``--debug-aws`` notice;
* the reporter's own repro, end to end: the real CLI as a subprocess, with
  fake credentials, a canary session token and a local endpoint, must never
  print the canary in ``-v``, ``GCO_VERBOSE=true`` (the MCP server's path:
  its CLI runner inherits the host environment and returns stderr to the
  client) or ``--debug-aws`` mode; a local fake STS proves the assume-role
  response body never appears either.
"""

from __future__ import annotations

import http.server
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli import main as cli_main
from cli.log_redaction import REDACTED, CredentialRedactionFilter, redact

ROOT = Path(__file__).resolve().parent.parent

CANARY_TOKEN = "GCO-CANARY-TOKEN-35ea328a0c0a2eeb"
CANARY_SECRET = "CANARY-SECRET-KEY-7f3a9c2e1b"
CANARY_ASSUMED_TOKEN = "CANARY-ASSUMED-TOKEN-5d8e2f"
SIGNATURE = "0f" * 32
FAKE_KEY_ID = "AKIAIOSFODNN7EXAMPLE"
FAKE_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


# ---------------------------------------------------------------------------
# redact()
# ---------------------------------------------------------------------------


class TestRedact:
    @pytest.mark.parametrize(
        ("text", "kept"),
        [
            # botocore.auth: the canonical headers inside the CanonicalRequest.
            (
                f"CanonicalRequest:\nPOST\n/\n\ncontent-type:x\nhost:ec2.amazonaws.com\n"
                f"x-amz-date:20261030T144102Z\nx-amz-security-token:{CANARY_TOKEN}\n\n"
                "content-type;host;x-amz-date;x-amz-security-token\nabc",
                ("x-amz-security-token:", "content-type;host;x-amz-date;x-amz-security-token"),
            ),
            # botocore.endpoint: the prepared request's header dict.
            (
                "Sending http request: <AWSPreparedRequest method=POST, headers={'X-Amz-Date': "
                f"b'20261030T144102Z', 'X-Amz-Security-Token': b'{CANARY_TOKEN}', "
                f"'Authorization': b'AWS4-HMAC-SHA256 Credential={FAKE_KEY_ID}/20261030/us-east-1/"
                f"ec2/aws4_request, SignedHeaders=content-type;host, Signature={SIGNATURE}'}}>",
                (
                    f"Credential={FAKE_KEY_ID}/20261030",
                    "SignedHeaders=content-type;host",
                    "Signature=",
                ),
            ),
            # botocore.auth: the signature on its own line.
            (f"Signature:\n{SIGNATURE}", ("Signature:\n",)),
            # Presigned-URL style query parameters.
            (
                f"https://s3.amazonaws.com/b/k?X-Amz-Security-Token={CANARY_TOKEN}&X-Amz-Signature="
                f"{SIGNATURE}&X-Amz-Date=20261030T144102Z",
                ("X-Amz-Security-Token=", "X-Amz-Signature=", "&X-Amz-Date=20261030T144102Z"),
            ),
            # botocore.parsers: an STS response body (query/XML protocol).
            (
                f"Response body:\nb'<Credentials><AccessKeyId>ASIA123</AccessKeyId>"
                f"<SecretAccessKey>{CANARY_SECRET}</SecretAccessKey>"
                f"<SessionToken>{CANARY_ASSUMED_TOKEN}</SessionToken></Credentials>'",
                ("<AccessKeyId>ASIA123</AccessKeyId>", "<SecretAccessKey>", "<SessionToken>"),
            ),
            # JSON protocols, both spellings (STS vs. SSO GetRoleCredentials).
            (
                f'{{"roleCredentials": {{"accessKeyId": "ASIA123", "secretAccessKey": "{CANARY_SECRET}", '
                f'"sessionToken": "{CANARY_ASSUMED_TOKEN}"}}}}',
                ('"accessKeyId": "ASIA123"', '"secretAccessKey": "', '"sessionToken": "'),
            ),
            # Python repr of request parameters (a secret being written, an escaped quote inside).
            (
                f"with params: {{'SecretId': 'x', 'SecretString': 'pa\\'ss {CANARY_SECRET}'}}",
                ("'SecretId': 'x'", "'SecretString': '"),
            ),
            # Form-encoded bodies of the token-exchange operations.
            (
                f"body: RoleArn=arn%3Aaws%3Aiam%3A%3A1%3Arole%2Fx&WebIdentityToken={CANARY_TOKEN}&Version=2011-06-15",
                (
                    "RoleArn=arn%3Aaws%3Aiam%3A%3A1%3Arole%2Fx",
                    "WebIdentityToken=",
                    "&Version=2011-06-15",
                ),
            ),
            # Bearer and Slurm REST tokens.
            (
                f"headers={{'Authorization': 'Bearer {CANARY_TOKEN}', 'X-SLURM-USER-TOKEN': '{CANARY_TOKEN}'}}",
                ("'Authorization': 'Bearer ", "'X-SLURM-USER-TOKEN': '"),
            ),
            # The config-file and environment spellings.
            (
                f"aws_secret_access_key = {FAKE_SECRET}\nAWS_SESSION_TOKEN={CANARY_TOKEN}",
                ("aws_secret_access_key = ", "AWS_SESSION_TOKEN="),
            ),
        ],
    )
    def test_credential_material_is_replaced_and_the_structure_kept(
        self, text: str, kept: tuple[str, ...]
    ) -> None:
        redacted = redact(text)
        for secret in (CANARY_TOKEN, CANARY_SECRET, CANARY_ASSUMED_TOKEN, SIGNATURE, FAKE_SECRET):
            assert secret not in redacted
        assert REDACTED in redacted
        for fragment in kept:
            assert fragment in redacted

    def test_text_without_credentials_is_returned_unchanged(self) -> None:
        text = (
            "Event request-created.ec2.DescribeSpotPriceHistory: calling handler <bound method "
            "RequestSigner.handler>\nStringToSign:\nAWS4-HMAC-SHA256\n20261030T144102Z\n"
            f"20261030/us-east-1/ec2/aws4_request\n{'ab' * 32}\n"
            "Found credentials in environment variables.\n"
            "{'NextToken': 'eyJhbGciOi', 'MaxResults': 100, 'AccessKeyId': 'ASIA123'}"
        )
        assert redact(text) == text


# ---------------------------------------------------------------------------
# CredentialRedactionFilter
# ---------------------------------------------------------------------------


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("botocore.auth", logging.DEBUG, __file__, 1, msg, args or None, None)


class TestCredentialRedactionFilter:
    def test_a_record_with_credentials_is_rewritten_once_formatted(self) -> None:
        record = _record("Signature:\n%s", SIGNATURE)
        assert CredentialRedactionFilter().filter(record) is True
        assert record.msg == f"Signature:\n{REDACTED}"
        assert record.args is None
        assert record.getMessage() == f"Signature:\n{REDACTED}"

    def test_a_clean_record_keeps_its_lazy_formatting(self) -> None:
        record = _record("Found credentials in %s.", "environment variables")
        assert CredentialRedactionFilter().filter(record) is True
        assert record.msg == "Found credentials in %s."
        assert record.args == ("environment variables",)

    def test_a_record_that_cannot_be_formatted_is_passed_through(self) -> None:
        record = _record("%s and %s", "only one")
        with pytest.raises(TypeError):
            record.getMessage()
        assert CredentialRedactionFilter().filter(record) is True
        assert record.msg == "%s and %s"


# ---------------------------------------------------------------------------
# _configure_cli_logging and the --debug-aws flag
# ---------------------------------------------------------------------------


def _levels() -> dict[str, int]:
    return {
        name: logging.getLogger(name).level
        for name in ("botocore", "boto3", "urllib3", "s3transfer", "kubernetes", "botocore.parsers")
    }


class TestConfigureCliLogging:
    def test_default_pins_the_third_party_loggers_at_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GCO_LOG_LEVEL", raising=False)
        cli_main._configure_cli_logging(False)
        assert logging.getLogger().level == logging.WARNING
        assert _levels() == {
            "botocore": logging.WARNING,
            "boto3": logging.WARNING,
            "urllib3": logging.WARNING,
            "s3transfer": logging.WARNING,
            "kubernetes": logging.WARNING,
            "botocore.parsers": logging.NOTSET,
        }

    def test_verbose_opens_the_sdk_to_info_and_no_further(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GCO_LOG_LEVEL", raising=False)
        cli_main._configure_cli_logging(True)
        assert logging.getLogger().level == logging.DEBUG
        assert _levels() == {
            "botocore": logging.INFO,
            "boto3": logging.INFO,
            "urllib3": logging.INFO,
            "s3transfer": logging.INFO,
            "kubernetes": logging.INFO,
            "botocore.parsers": logging.NOTSET,
        }

    def test_debug_aws_opens_the_sdk_but_never_the_response_parser_or_kubernetes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GCO_LOG_LEVEL", raising=False)
        cli_main._configure_cli_logging(False, debug_aws=True)
        # GCO's own loggers are untouched by --debug-aws alone.
        assert logging.getLogger().level == logging.WARNING
        assert _levels() == {
            "botocore": logging.DEBUG,
            "boto3": logging.DEBUG,
            "urllib3": logging.DEBUG,
            "s3transfer": logging.DEBUG,
            "kubernetes": logging.WARNING,
            "botocore.parsers": logging.INFO,
        }
        # A later plain configuration releases the parser pin again.
        cli_main._configure_cli_logging(True)
        assert logging.getLogger("botocore.parsers").level == logging.NOTSET

    def test_the_redaction_filter_sits_on_the_root_handler_in_every_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GCO_LOG_LEVEL", raising=False)
        for verbose, debug_aws in ((False, False), (True, False), (False, True)):
            cli_main._configure_cli_logging(verbose, debug_aws=debug_aws)
            handlers = logging.getLogger().handlers
            assert handlers, (verbose, debug_aws)
            for handler in handlers:
                assert [type(f) for f in handler.filters] == [CredentialRedactionFilter]


class TestDebugAwsFlag:
    def _invoke(self, *args: str):
        with patch("cli.commands.jobs_cmd.get_job_manager") as manager:
            manager.return_value = MagicMock(list_jobs=MagicMock(return_value=[]))
            return CliRunner().invoke(
                cli_main.cli, [*args, "jobs", "list", "--region", "us-east-1"]
            )

    def test_the_flag_prints_the_notice_to_stderr(self) -> None:
        result = self._invoke("--debug-aws")
        assert result.exit_code == 0
        assert cli_main._DEBUG_AWS_NOTICE in result.stderr
        assert logging.getLogger("botocore").level == logging.DEBUG
        assert logging.getLogger("botocore.parsers").level == logging.INFO

    def test_verbose_alone_prints_no_notice_and_stops_at_info(self) -> None:
        result = self._invoke("-v")
        assert result.exit_code == 0
        assert cli_main._DEBUG_AWS_NOTICE not in result.output
        assert logging.getLogger("botocore").level == logging.INFO

    def test_the_flag_has_no_config_or_environment_spelling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SDK debug is a per-invocation decision: the MCP CLI runner inherits the environment."""
        monkeypatch.setenv("GCO_VERBOSE", "true")
        monkeypatch.setenv("GCO_DEBUG_AWS", "true")
        monkeypatch.setenv("GCO_LOG_LEVEL", "DEBUG")
        result = self._invoke()
        assert result.exit_code == 0
        assert cli_main._DEBUG_AWS_NOTICE not in result.output
        assert logging.getLogger("botocore").level == logging.INFO
        from cli.config import GCOConfig

        assert not hasattr(GCOConfig(), "debug_aws")


# ---------------------------------------------------------------------------
# The reporter's repro, end to end
# ---------------------------------------------------------------------------

_STS_BODY = (
    '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult>'
    "<Credentials><AccessKeyId>ASIACANARYKEYID00001</AccessKeyId>"
    f"<SecretAccessKey>{CANARY_SECRET}</SecretAccessKey>"
    f"<SessionToken>{CANARY_ASSUMED_TOKEN}</SessionToken>"
    "<Expiration>2099-01-01T00:00:00Z</Expiration></Credentials>"
    "<AssumedRoleUser><Arn>arn:aws:sts::123456789012:assumed-role/demo/s</Arn>"
    "<AssumedRoleId>AROA:s</AssumedRoleId></AssumedRoleUser></AssumeRoleResult>"
    "<ResponseMetadata><RequestId>x</RequestId></ResponseMetadata></AssumeRoleResponse>"
).encode()


class _FakeSts(http.server.BaseHTTPRequestHandler):
    """Answers every POST with a complete AssumeRole credential set."""

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(_STS_BODY)))
        self.end_headers()
        self.wfile.write(_STS_BODY)

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def fake_sts_port() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeSts)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _run_cli(home: Path, endpoint: str, *args: str, env: dict[str, str]) -> str:
    """The real CLI as a subprocess; nothing leaves the machine (local endpoint, fake credentials)."""
    base = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "AWS_CONFIG_FILE": str(home / "no-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(home / "no-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ENDPOINT_URL": endpoint,
        "AWS_MAX_ATTEMPTS": "1",
        "AWS_RETRY_MODE": "standard",
    }
    completed = (
        subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - static argv of our own CLI
            [
                sys.executable,
                "-m",
                "cli.main",
                *args,
                "capacity",
                "spot-prices",
                "-i",
                "p5.48xlarge",
                "-r",
                "us-east-1",
            ],
            cwd=ROOT,
            env={**base, **env},
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    )
    return completed.stdout + completed.stderr


_STATIC_CREDENTIALS = {
    "AWS_ACCESS_KEY_ID": FAKE_KEY_ID,
    "AWS_SECRET_ACCESS_KEY": FAKE_SECRET,
    "AWS_SESSION_TOKEN": CANARY_TOKEN,
}


class TestCanary:
    """The reporter's repro: a canary session token must never reach stdout or stderr."""

    def test_verbose_shows_sdk_info_but_no_signing_material(self, tmp_path: Path) -> None:
        output = _run_cli(tmp_path, "http://127.0.0.1:9", "-v", env=_STATIC_CREDENTIALS)
        assert CANARY_TOKEN not in output
        assert FAKE_SECRET not in output
        # INFO from the SDK is on: the user still learns which credentials were used.
        assert "Found credentials in environment variables" in output
        # DEBUG is not: no SigV4 material at all.
        assert "CanonicalRequest" not in output
        assert "Authorization" not in output

    def test_gco_verbose_in_the_environment_is_the_same_verbose(self, tmp_path: Path) -> None:
        """The MCP server's CLI runner inherits the host environment and returns stderr."""
        output = _run_cli(
            tmp_path, "http://127.0.0.1:9", env={**_STATIC_CREDENTIALS, "GCO_VERBOSE": "true"}
        )
        assert CANARY_TOKEN not in output
        assert "Found credentials in environment variables" in output
        assert "CanonicalRequest" not in output

    def test_debug_aws_prints_redacted_signing_details(self, tmp_path: Path) -> None:
        output = _run_cli(tmp_path, "http://127.0.0.1:9", "--debug-aws", env=_STATIC_CREDENTIALS)
        assert CANARY_TOKEN not in output
        assert FAKE_SECRET not in output
        assert cli_main._DEBUG_AWS_NOTICE in output
        assert "CanonicalRequest" in output
        assert f"x-amz-security-token:{REDACTED}" in output
        assert f"Signature={REDACTED}" in output
        assert f"Signature:\n{REDACTED}" in output

    def test_assume_role_credentials_never_appear(self, tmp_path: Path, fake_sts_port: int) -> None:
        """A role_arn profile: the AssumeRole response body is never logged, redacted or not."""
        (tmp_path / "awsconfig").write_text(
            "[profile base]\n"
            f"aws_access_key_id = {FAKE_KEY_ID}\n"
            f"aws_secret_access_key = {FAKE_SECRET}\n"
            "region = us-east-1\n"
            "[profile demo]\n"
            "role_arn = arn:aws:iam::123456789012:role/demo\n"
            "source_profile = base\n"
            "region = us-east-1\n",
            encoding="utf-8",
        )
        env = {"AWS_PROFILE": "demo", "AWS_CONFIG_FILE": str(tmp_path / "awsconfig")}
        endpoint = f"http://127.0.0.1:{fake_sts_port}"
        for args in (("-v",), ("--debug-aws",), ("-v", "--debug-aws")):
            output = _run_cli(tmp_path, endpoint, *args, env=env)
            assert CANARY_SECRET not in output, args
            assert CANARY_ASSUMED_TOKEN not in output, args
            assert FAKE_SECRET not in output, args
            assert "Response body" not in output, args
            if "--debug-aws" in args:
                # The request side is still fully debuggable, the sts:AssumeRole call included.
                assert "AssumeRole" in output and "CanonicalRequest" in output, args
