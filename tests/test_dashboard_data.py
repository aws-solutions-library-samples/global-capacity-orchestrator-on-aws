"""Behaviour of ``cli.dashboard.data``'s shared machinery: errors, discovery, fan-out, parsers.

``FleetData`` is the dashboard's only path to AWS and the GCO API. This file
covers what every read and action relies on: ``describe_error`` (any failure
from the CLI layers turned into an operator message and hint), the endpoint
discovery memo the signed clients share across threads, the per-Region
fan-out, the manifest and example-header parsers, example discovery (on a
``tmp_path`` tree and, read-only, on the checkout's own ``examples/``), and
the client, session, topology and identity plumbing. The reads and the writes
have their own files (``test_dashboard_data_reads.py`` and
``test_dashboard_data_writes.py``), which import the doubles defined here.

Every boundary is mocked: CloudFormation discovery by patching the CLI
client's own lookup methods (or, where the exact exception the client raises
matters, a real CloudFormation client behind ``botocore.stub.Stubber``), STS
the same way, and HTTP answers as real ``requests.Response`` objects handed
to the CLI client's own HTTP seam. Nothing reaches the network.
"""

from __future__ import annotations

import copy
import io
import json
import pickle
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, create_autospec

import boto3
import pytest
import requests
from botocore.credentials import Credentials
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from botocore.stub import Stubber

from cli.aws_client import (
    ApiEndpoint,
    APIRequestError,
    GCOAWSClient,
    ManifestSubmissionError,
    RegionalApiDiscoveryError,
)
from cli.config import GCOConfig
from cli.dashboard import data
from cli.dashboard.data import (
    CONSOLE_ROUTES,
    GLOBAL_ONLY_ROUTES,
    DashboardError,
    ExampleManifest,
    FleetData,
    RegionRead,
    SharedDiscoveryClient,
    Topology,
    UtilizationRows,
    _fan_out,
    _job_errors,
    _object,
    _objects,
    _response_detail,
    clear_discovery_cache,
    describe_error,
    example_header,
    load_manifest_text,
    manifest_requests_gpu,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)
ACCOUNT = "123456789012"


# ---------------------------------------------------------------------------
# Doubles shared with test_dashboard_data_reads.py and _writes.py
# ---------------------------------------------------------------------------


class RecordingResponse(requests.Response):
    """A real ``requests.Response`` that counts how often it was closed."""

    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


def http_response(
    status: int, body: object = b"", *, reason: str | None = None
) -> RecordingResponse:
    """A response as ``requests`` builds one: the body is a stream, readable once."""
    if isinstance(body, bytes):
        content = body
    elif isinstance(body, str):
        content = body.encode("utf-8")
    else:
        content = json.dumps(body).encode("utf-8")
    response = RecordingResponse()
    response.status_code = status
    response.reason = HTTPStatus(status).phrase if reason is None else reason
    response.url = "https://api.example.com/prod"
    response.encoding = "utf-8"
    response.raw = io.BytesIO(content)
    return response


def client_error(code: str, message: str = "", operation: str = "DescribeStacks") -> ClientError:
    """A botocore ``ClientError`` as a service raises it."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def caused_by(exc: BaseException, cause: BaseException) -> BaseException:
    """``exc`` as ``raise exc from cause`` leaves it."""
    exc.__cause__ = cause
    return exc


def api_client() -> Any:
    """An autospec of the dashboard's signed client: a call the real one would refuse fails."""
    return create_autospec(SharedDiscoveryClient, instance=True)


def patch_client(monkeypatch: pytest.MonkeyPatch, fleet: FleetData) -> Any:
    """Make every ``fleet.client()`` return one autospec client, and return it."""
    client = api_client()
    monkeypatch.setattr(fleet, "client", lambda: client)
    return client


def k8s_job(
    name: str,
    *,
    region: str | None = None,
    active: int = 0,
    succeeded: int = 0,
    failed: int = 0,
    complete: bool = False,
) -> dict[str, Any]:
    """A batch/v1 Job as the jobs API returns it (``_source_region`` from the aggregator)."""
    conditions = [{"type": "Complete", "status": "True"}] if complete else []
    job: dict[str, Any] = {
        "metadata": {
            "name": name,
            "namespace": "gco-jobs",
            "creationTimestamp": "2026-11-25T16:00:00Z",
            "labels": {"team": "ml"},
            "uid": f"uid-{name}",
        },
        "spec": {
            "parallelism": 1,
            "completions": 1,
            "backoffLimit": 0,
            "template": {"spec": {"containers": [{"name": "main", "image": "busybox:1.38.0"}]}},
        },
        "status": {
            "active": active,
            "succeeded": succeeded,
            "failed": failed,
            "startTime": "2026-11-25T16:00:05Z",
            "conditions": conditions,
        },
    }
    if region is not None:
        job["_source_region"] = region
    return job


def patch_factory(
    monkeypatch: pytest.MonkeyPatch, target: str, spec: type
) -> tuple[Any, MagicMock]:
    """Patch the factory at ``target`` to return an autospec of ``spec``; return both."""
    instance = create_autospec(spec, instance=True)
    factory = MagicMock(return_value=instance)
    monkeypatch.setattr(target, factory)
    return instance, factory


def by_region(mock: Any, key: str = "region") -> list[Any]:
    """A fanned-out mock's calls in Region order (threads finish in any order)."""
    return sorted(mock.call_args_list, key=lambda made: made.kwargs[key] or "")


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now`` is always ``NOW``."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return NOW if tz is None else NOW.astimezone(tz)


def freeze_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``datetime.now`` as ``cli.dashboard.data`` sees it to ``NOW``."""
    monkeypatch.setattr(data, "datetime", _FrozenDatetime)


class StubbedSession:
    """Stands in for ``boto3.Session()``: real clients, each answering from a Stubber.

    ``stub(service, region)`` creates the client ``FleetData`` gets for that
    service and Region and returns its active Stubber, so requests are checked
    against the real service model. A client nobody stubbed is a KeyError.
    """

    def __init__(self) -> None:
        self._session = boto3.Session(region_name="us-east-1")
        self._clients: dict[tuple[str, str | None], Any] = {}
        self._stubbers: list[Stubber] = []

    def stub(self, service: str, region: str | None = None) -> Stubber:
        client = self._session.client(service, region_name=region)
        stubber = Stubber(client)
        stubber.activate()
        self._clients[(service, region)] = client
        self._stubbers.append(stubber)
        return stubber

    def client(self, service: str, region_name: str | None = None) -> Any:
        return self._clients[(service, region_name)]

    def assert_no_pending_responses(self) -> None:
        for stubber in self._stubbers:
            stubber.assert_no_pending_responses()


def patch_session(monkeypatch: pytest.MonkeyPatch, fleet: FleetData) -> StubbedSession:
    """Make every ``fleet.session()`` return one StubbedSession, and return it."""
    session = StubbedSession()
    monkeypatch.setattr(fleet, "session", lambda: session)
    return session


@pytest.fixture(name="fleet")
def fleet_fixture() -> FleetData:
    """The CLI's defaults: project ``gco``, global API mode, default Region us-east-1."""
    return FleetData(GCOConfig())


@pytest.fixture(name="api")
def api_fixture(monkeypatch: pytest.MonkeyPatch, fleet: FleetData) -> Any:
    return patch_client(monkeypatch, fleet)


@pytest.fixture(name="regional_fleet")
def regional_fleet_fixture() -> FleetData:
    """Regional API mode: every request rides a Region's bridge (us-east-1 by default)."""
    return FleetData(GCOConfig(use_regional_api=True))


@pytest.fixture(name="regional_api")
def regional_api_fixture(monkeypatch: pytest.MonkeyPatch, regional_fleet: FleetData) -> Any:
    return patch_client(monkeypatch, regional_fleet)


@pytest.fixture(autouse=True)
def _empty_discovery_memo() -> Iterator[None]:
    clear_discovery_cache()
    yield
    clear_discovery_cache()


# ---------------------------------------------------------------------------
# describe_error and _response_detail
# ---------------------------------------------------------------------------

_SIGN_IN = "Sign in (for example `aws sso login`) and refresh the page."
_EXPIRED = "Your session has probably expired: sign in again and refresh."
_DENIED = "The signed-in principal lacks a permission this view needs."
_DEPLOY_BRIDGE = "Deploy the regional API bridge with `gco stacks deploy-all`."


class TestDescribeError:
    def test_a_dashboard_error_is_returned_unchanged(self) -> None:
        error = DashboardError("Pick at least one event.", hint="Tick a box.")
        assert describe_error(error) is error

    @pytest.mark.parametrize(
        "exc",
        [
            NoCredentialsError(),
            RuntimeError(
                "No AWS credentials found. Configure credentials via environment variables, "
                "~/.aws/credentials, IAM role, or SSO (aws sso login)."
            ),
        ],
        ids=["botocore", "signed-client"],
    )
    def test_missing_credentials_ask_the_operator_to_sign_in(self, exc: BaseException) -> None:
        error = describe_error(exc)
        assert str(error) == "No AWS credentials were found."
        assert error.hint == _SIGN_IN

    @pytest.mark.parametrize(
        "code",
        [
            "ExpiredToken",
            "ExpiredTokenException",
            "InvalidClientTokenId",
            "UnrecognizedClientException",
            "UnauthorizedSSOToken",
            "TokenRefreshRequired",
        ],
    )
    def test_rejected_credentials_say_the_session_expired(self, code: str) -> None:
        error = describe_error(client_error(code, "The security token included is expired"))
        assert str(error) == f"AWS rejected the credentials ({code})."
        assert error.hint == _EXPIRED

    @pytest.mark.parametrize(
        "code", ["AccessDenied", "AccessDeniedException", "UnauthorizedOperation"]
    )
    def test_denials_quote_aws_and_blame_a_missing_permission(self, code: str) -> None:
        exc = client_error(code, "  User is not authorized to perform: ce:GetCostAndUsage  ")
        error = describe_error(exc)
        assert str(error) == (
            "AWS denied the request: User is not authorized to perform: ce:GetCostAndUsage"
        )
        assert error.hint == _DENIED

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            (
                {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
                "ThrottlingException: Rate exceeded",
            ),
            (
                {"Error": {"Code": "ValidationError", "Message": "   "}},
                "ValidationError: ValidationError",
            ),
            ({}, "ClientError: ClientError"),
        ],
        ids=["code-and-message", "blank-message", "no-error-block"],
    )
    def test_other_client_errors_show_code_and_message(
        self, response: dict[str, Any], message: str
    ) -> None:
        error = describe_error(ClientError(response, "GetMetricData"))
        assert str(error) == message
        assert error.hint is None

    def test_a_botocore_error_names_its_type(self) -> None:
        error = describe_error(EndpointConnectionError(endpoint_url="https://example.com"))
        assert str(error) == (
            'EndpointConnectionError: Could not connect to the endpoint URL: "https://example.com"'
        )
        assert error.hint is None

    @pytest.mark.parametrize(
        "cause",
        [None, ValueError("regional endpoint output is not an execute-api prod URL")],
        ids=["no-cause", "invalid-output"],
    )
    def test_a_missing_or_broken_bridge_points_at_the_deploy(
        self, cause: BaseException | None
    ) -> None:
        exc = RegionalApiDiscoveryError(
            "Regional API stack 'gco-regional-api-us-west-2' exists in us-west-2 but does not "
            "publish a RegionalApiEndpoint output; the bridge deployment may be incomplete"
        )
        if cause is not None:
            caused_by(exc, cause)
        error = describe_error(exc)
        assert str(error) == str(exc)
        assert error.hint == _DEPLOY_BRIDGE

    @pytest.mark.parametrize(
        ("cause", "hint"),
        [
            (client_error("ExpiredToken", "The security token included is expired"), _EXPIRED),
            (
                client_error("AccessDenied", "not authorized: cloudformation:DescribeStacks"),
                _DENIED,
            ),
            (NoCredentialsError(), _SIGN_IN),
            (client_error("Throttling", "Rate exceeded"), None),
            (EndpointConnectionError(endpoint_url="https://example.com"), None),
        ],
        ids=["expired", "denied", "no-credentials", "throttled", "unreachable"],
    )
    def test_discovery_that_could_not_ask_cloudformation_never_blames_the_bridge(
        self, cause: BaseException, hint: str | None
    ) -> None:
        exc = caused_by(
            RegionalApiDiscoveryError(
                "Regional API endpoint discovery could not run for stack "
                "'gco-regional-api-us-west-2' in us-west-2."
            ),
            cause,
        )
        error = describe_error(exc)
        assert str(error) == str(exc)
        assert error.hint == hint

    def test_refused_manifests_carry_the_status(self) -> None:
        exc = ManifestSubmissionError(
            422, "trainer: image registry is not allowed", [{"name": "trainer", "status": "failed"}]
        )
        error = describe_error(exc)
        assert str(error) == (
            "The API refused the manifests (422): trainer: image registry is not allowed"
        )
        assert error.hint is None

    @pytest.mark.parametrize(
        ("status", "hint"),
        [(504, "The API Gateway front door timed out; try again."), (404, None)],
    )
    def test_api_request_errors_carry_the_status(self, status: int, hint: str | None) -> None:
        error = describe_error(APIRequestError(status, "Endpoint request timed out"))
        assert str(error) == f"API request failed: Endpoint request timed out (HTTP {status})"
        assert error.hint == hint

    def test_an_http_error_quotes_the_api_detail(self) -> None:
        response = http_response(409, {"detail": "Job trainer already exists"})
        error = describe_error(requests.HTTPError("409 Client Error", response=response))
        assert str(error) == "The API answered HTTP 409: Job trainer already exists"

    def test_an_http_error_without_a_response_falls_back_to_its_text(self) -> None:
        error = describe_error(requests.HTTPError("500 Server Error: for url"))
        assert str(error) == "The API answered HTTP ?: 500 Server Error: for url"

    @pytest.mark.parametrize(
        ("exc", "name"),
        [
            (requests.ConnectionError("Max retries exceeded"), "ConnectionError"),
            (requests.ReadTimeout("read timed out"), "ReadTimeout"),
        ],
    )
    def test_transport_failures_say_the_api_was_unreachable(
        self, exc: BaseException, name: str
    ) -> None:
        error = describe_error(exc)
        assert str(error) == f"Could not reach the GCO API: {name}"
        assert error.hint is None

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("max_attempts must be a positive integer"),
            RuntimeError("Regional API endpoint is not deployed in us-west-2"),
        ],
    )
    def test_anything_else_names_its_type(self, exc: BaseException) -> None:
        error = describe_error(exc)
        assert str(error) == f"{type(exc).__name__}: {exc}"
        assert error.hint is None


class _BrokenCloudFormation:
    """A session whose CloudFormation client raises ``exc`` from every call."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def client(self, service: str, region_name: str | None = None) -> Any:
        def fail(**kwargs: Any) -> Any:
            raise self._exc

        return SimpleNamespace(describe_stacks=fail)


#: API Gateway's own answers to a request it will not pass on (no custom gateway responses).
_GATEWAY_EXPIRED = "The security token included in the request is expired"
_GATEWAY_DENIED = (
    "User: arn:aws:sts::123456789012:assumed-role/Operator/tester is not authorized to "
    "perform: execute-api:Invoke on resource: arn:aws:execute-api:us-east-2:123456789012:"
    "abc123/prod/GET/api/v1/templates"
)


class TestWrappedFailures:
    """A failure wrapped in another keeps the wrapper's message and the wrapped one's hint."""

    @pytest.mark.parametrize(
        ("cause", "hint"),
        [
            (client_error("ExpiredToken", _GATEWAY_EXPIRED), _EXPIRED),
            (
                client_error("AccessDenied", "not authorized: cloudformation:DescribeStacks"),
                _DENIED,
            ),
            (NoCredentialsError(), _SIGN_IN),
        ],
        ids=["expired", "denied", "no-credentials"],
    )
    def test_the_clients_failed_global_api_discovery_asks_what_its_cause_asks(
        self, monkeypatch: pytest.MonkeyPatch, cause: BaseException, hint: str
    ) -> None:
        # GCOAWSClient.get_api_endpoint wraps any discovery failure in
        # RuntimeError("Failed to get API endpoint: ...") raised from it.
        client = GCOAWSClient(GCOConfig())
        monkeypatch.setattr(client, "_session", _BrokenCloudFormation(cause))
        with pytest.raises(RuntimeError) as raised:
            client.get_api_endpoint()
        error = describe_error(raised.value)
        assert str(error) == f"Failed to get API endpoint: {cause}"
        assert error.hint == hint

    def test_a_read_the_gateway_refuses_for_an_expired_session_asks_to_sign_in_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A read-only 403 is retried once with fresh credentials; with none to
        # be found the client hands back the 403, and call_api raises it.
        client = GCOAWSClient(GCOConfig())
        endpoint = ApiEndpoint(url="https://api.example.com/prod", region="us-east-2", api_id="a1")
        monkeypatch.setattr(client, "get_api_endpoint", lambda force_refresh=False: endpoint)
        signer = SimpleNamespace(get_credentials=lambda: Credentials("test-key", "test-secret"))
        monkeypatch.setattr(client, "_session", signer)
        sent: list[str] = []

        def answer(**request: Any) -> requests.Response:
            sent.append(request["url"])
            return http_response(403, {"message": _GATEWAY_EXPIRED})

        monkeypatch.setattr("cli.aws_client._http_request", answer)
        with pytest.raises(APIRequestError) as raised:
            client.call_api("GET", "/api/v1/templates")
        error = describe_error(raised.value)
        assert str(error) == f"API request failed: {_GATEWAY_EXPIRED} (HTTP 403)"
        assert error.hint == _EXPIRED
        assert sent == ["https://api.example.com/prod/api/v1/templates"]

    @pytest.mark.parametrize(
        ("make", "message", "hint"),
        [
            (
                lambda: APIRequestError(403, _GATEWAY_EXPIRED),
                f"API request failed: {_GATEWAY_EXPIRED} (HTTP 403)",
                _EXPIRED,
            ),
            (
                lambda: APIRequestError(
                    403, "The security token included in the request is invalid."
                ),
                "API request failed: The security token included in the request is invalid. "
                "(HTTP 403)",
                _EXPIRED,
            ),
            (
                lambda: APIRequestError(403, _GATEWAY_DENIED),
                f"API request failed: {_GATEWAY_DENIED} (HTTP 403)",
                _DENIED,
            ),
            (
                lambda: requests.HTTPError(
                    "403 Client Error",
                    response=http_response(403, {"message": _GATEWAY_EXPIRED}),
                ),
                f"The API answered HTTP 403: {_GATEWAY_EXPIRED}",
                _EXPIRED,
            ),
            (
                # API Gateway capitalises the key of an IAM denial.
                lambda: requests.HTTPError(
                    "403 Client Error",
                    response=http_response(403, {"Message": _GATEWAY_DENIED}),
                ),
                f"The API answered HTTP 403: {_GATEWAY_DENIED}",
                _DENIED,
            ),
            (
                lambda: ManifestSubmissionError(403, _GATEWAY_EXPIRED),
                f"The API refused the manifests (403): {_GATEWAY_EXPIRED}",
                _EXPIRED,
            ),
            (
                # The GCO API's own 403 is about the request, not the session.
                lambda: APIRequestError(
                    403, "Namespace 'kube-system' not allowed. Allowed: ['gco-jobs']"
                ),
                "API request failed: Namespace 'kube-system' not allowed. Allowed: ['gco-jobs'] "
                "(HTTP 403)",
                None,
            ),
            (
                lambda: requests.HTTPError(
                    "401 Client Error",
                    response=http_response(401, {"message": _GATEWAY_EXPIRED}),
                ),
                f"The API answered HTTP 401: {_GATEWAY_EXPIRED}",
                None,
            ),
        ],
        ids=[
            "expired",
            "invalid",
            "denied",
            "http-error-expired",
            "http-error-denied",
            "manifests-expired",
            "gco-api-403",
            "not-a-403",
        ],
    )
    def test_api_gateways_own_403s_say_what_to_do(
        self, make: Any, message: str, hint: str | None
    ) -> None:
        error = describe_error(make())
        assert str(error) == message
        assert error.hint == hint

    @pytest.mark.parametrize(
        ("status", "body", "message", "hint"),
        [
            (403, {"message": _GATEWAY_EXPIRED}, _GATEWAY_EXPIRED, _EXPIRED),
            (403, {"Message": _GATEWAY_DENIED}, _GATEWAY_DENIED, _DENIED),
            (404, {"detail": "Job 'trainer' not found"}, "Job 'trainer' not found", None),
        ],
        ids=["expired", "denied", "gco-api-404"],
    )
    def test_an_api_request_error_carrying_its_response_reads_as_the_apis_message(
        self, status: int, body: dict[str, str], message: str, hint: str | None
    ) -> None:
        # The client may raise APIRequestError as a requests.HTTPError with the
        # response attached (as raise_for_status did); it is still described
        # by the API's own message, not as a bare HTTP error.
        exc = APIRequestError(status, message)
        exc.response = http_response(status, body)
        error = describe_error(exc)
        assert str(error) == f"API request failed: {message} (HTTP {status})"
        assert error.hint == hint

    def test_a_failure_raised_while_handling_another_takes_its_hint(self) -> None:
        exc = RuntimeError("Stack outputs could not be read")
        exc.__context__ = client_error("ExpiredToken", _GATEWAY_EXPIRED)
        error = describe_error(exc)
        assert (str(error), error.hint) == ("Stack outputs could not be read", _EXPIRED)

    def test_a_context_raised_from_none_is_not_followed(self) -> None:
        exc = RuntimeError("Stack outputs could not be read")
        exc.__context__ = client_error("ExpiredToken", _GATEWAY_EXPIRED)
        exc.__suppress_context__ = True
        error = describe_error(exc)
        assert (str(error), error.hint) == ("RuntimeError: Stack outputs could not be read", None)

    def test_the_hint_may_come_from_further_down_the_chain(self) -> None:
        inner = caused_by(ValueError("no usable stack record"), NoCredentialsError())
        exc = caused_by(RuntimeError("Failed to get API endpoint: no usable stack record"), inner)
        error = describe_error(exc)
        assert str(error) == "Failed to get API endpoint: no usable stack record"
        assert error.hint == _SIGN_IN

    def test_a_cause_without_a_hint_leaves_the_plain_description(self) -> None:
        exc = caused_by(
            RuntimeError("Failed to get API endpoint: ApiEndpoint not found in stack gco-api"),
            ValueError("ApiEndpoint not found in stack gco-api"),
        )
        error = describe_error(exc)
        assert str(error) == (
            "RuntimeError: Failed to get API endpoint: ApiEndpoint not found in stack gco-api"
        )
        assert error.hint is None

    def test_a_chain_that_loops_back_on_itself_ends(self) -> None:
        first, second = RuntimeError("first"), RuntimeError("second")
        caused_by(first, second)
        caused_by(second, first)
        error = describe_error(first)
        assert (str(error), error.hint) == ("RuntimeError: first", None)

    def test_a_wrapper_without_a_message_reads_as_what_it_wraps(self) -> None:
        error = describe_error(caused_by(RuntimeError(), NoCredentialsError()))
        assert (str(error), error.hint) == ("No AWS credentials were found.", _SIGN_IN)


class TestResponseDetail:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ({"detail": "Job not found", "error": "ignored"}, "Job not found"),
            ({"error": "Forbidden"}, "Forbidden"),
            ({"Message": "User: tester is not authorized"}, "User: tester is not authorized"),
            (
                {"detail": "", "error": None, "message": "Internal server error"},
                "Internal server error",
            ),
            ({"status": "failed"}, '{"status": "failed"}'),
            (["first", "second"], '["first", "second"]'),
            ({"detail": "x" * 500}, "x" * 400),
        ],
        ids=[
            "detail-first",
            "error",
            "iam-denial",
            "falsy-keys-skipped",
            "other-object",
            "array",
            "bounded",
        ],
    )
    def test_a_json_body_gives_its_own_account(self, body: object, expected: str) -> None:
        assert _response_detail(http_response(400, body)) == expected

    @pytest.mark.parametrize(
        ("body", "reason", "expected"),
        [
            (b"<html>502 Bad Gateway</html>", "Bad Gateway", "<html>502 Bad Gateway</html>"),
            (b"", "Bad Gateway", "Bad Gateway"),
            (b"", "", "no detail"),
            (b"y" * 500, "", "y" * 400),
        ],
        ids=["text", "reason", "nothing", "bounded"],
    )
    def test_a_non_json_body_falls_back_to_text_then_reason(
        self, body: bytes, reason: str, expected: str
    ) -> None:
        assert _response_detail(http_response(502, body, reason=reason)) == expected


# ---------------------------------------------------------------------------
# SharedDiscoveryClient and clear_discovery_cache
# ---------------------------------------------------------------------------


@pytest.fixture(name="lookups")
def lookups_fixture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, bool]]:
    """Record every CloudFormation lookup behind the memo (the CLI client's own methods).

    The regional bridge is absent in eu-west-1 (``None``, CloudFormation's
    confirmation) and unreadable in ap-south-1.
    """
    lookups: list[tuple[str, str, bool]] = []

    def global_lookup(self: GCOAWSClient, force_refresh: bool = False) -> ApiEndpoint:
        lookups.append(("global", self.config.api_gateway_stack_name, force_refresh))
        return ApiEndpoint(
            url=f"https://{self.config.project_name}.example.com/prod",
            region=self.config.api_gateway_region,
            api_id=f"{self.config.project_name}{len(lookups)}",
        )

    def regional_lookup(
        self: GCOAWSClient, region: str, force_refresh: bool = False
    ) -> ApiEndpoint | None:
        lookups.append((region, self.config.project_name, force_refresh))
        if region == "eu-west-1":
            return None
        if region == "ap-south-1":
            raise RegionalApiDiscoveryError("discovery could not run in ap-south-1")
        return ApiEndpoint(
            url=f"https://{region}.example.com/prod",
            region=region,
            api_id=f"r{len(lookups)}",
            is_regional=True,
        )

    monkeypatch.setattr(GCOAWSClient, "get_api_endpoint", global_lookup)
    monkeypatch.setattr(GCOAWSClient, "get_regional_api_endpoint", regional_lookup)
    return lookups


@pytest.fixture(name="clock")
def clock_fixture(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The memo's monotonic clock, as a value the test moves forward."""
    clock = [1000.0]
    monkeypatch.setattr(
        data, "time", SimpleNamespace(monotonic=lambda: clock[0], perf_counter=time.perf_counter)
    )
    return clock


class TestSharedDiscovery:
    def test_the_global_endpoint_is_discovered_once_for_every_client(
        self, lookups: list[Any], clock: list[float]
    ) -> None:
        config = GCOConfig()
        first = SharedDiscoveryClient(config).get_api_endpoint()
        second = SharedDiscoveryClient(config).get_api_endpoint()
        assert second is first
        assert lookups == [("global", "gco-api-gateway", False)]

    def test_force_refresh_rediscovers_and_replaces_the_memo(
        self, lookups: list[Any], clock: list[float]
    ) -> None:
        config = GCOConfig()
        stale = SharedDiscoveryClient(config).get_api_endpoint()
        fresh = SharedDiscoveryClient(config).get_api_endpoint(force_refresh=True)
        assert fresh != stale
        assert SharedDiscoveryClient(config).get_api_endpoint() is fresh
        assert lookups == [
            ("global", "gco-api-gateway", False),
            ("global", "gco-api-gateway", True),
        ]

    def test_an_answer_older_than_the_cli_ttl_is_rediscovered(
        self, lookups: list[Any], clock: list[float]
    ) -> None:
        config = GCOConfig()
        SharedDiscoveryClient(config).get_api_endpoint()
        clock[0] += config.cache_ttl_seconds - 1
        SharedDiscoveryClient(config).get_api_endpoint()
        assert len(lookups) == 1
        clock[0] += 1
        SharedDiscoveryClient(config).get_api_endpoint()
        assert len(lookups) == 2

    def test_each_deployment_has_its_own_entry(
        self, lookups: list[Any], clock: list[float]
    ) -> None:
        gco = SharedDiscoveryClient(GCOConfig()).get_api_endpoint()
        other = SharedDiscoveryClient(GCOConfig(project_name="other")).get_api_endpoint()
        assert gco.url == "https://gco.example.com/prod"
        assert other.url == "https://other.example.com/prod"
        assert [entry[1] for entry in lookups] == ["gco-api-gateway", "other-api-gateway"]

    def test_regional_answers_are_memoized_per_region_absence_included(
        self, lookups: list[Any], clock: list[float]
    ) -> None:
        config = GCOConfig()
        east = SharedDiscoveryClient(config).get_regional_api_endpoint("us-east-1")
        assert SharedDiscoveryClient(config).get_regional_api_endpoint("us-east-1") is east
        assert SharedDiscoveryClient(config).get_regional_api_endpoint("eu-west-1") is None
        assert SharedDiscoveryClient(config).get_regional_api_endpoint("eu-west-1") is None
        assert lookups == [("us-east-1", "gco", False), ("eu-west-1", "gco", False)]

    def test_a_failed_regional_discovery_is_retried_next_time(
        self, lookups: list[Any], clock: list[float]
    ) -> None:
        client = SharedDiscoveryClient(GCOConfig())
        for _ in range(2):
            with pytest.raises(RegionalApiDiscoveryError, match="could not run in ap-south-1"):
                client.get_regional_api_endpoint("ap-south-1")
        assert lookups == [("ap-south-1", "gco", False)] * 2

    def test_regional_force_refresh_and_expiry_rediscover(
        self, lookups: list[Any], clock: list[float]
    ) -> None:
        config = GCOConfig()
        SharedDiscoveryClient(config).get_regional_api_endpoint("us-west-2")
        refreshed = SharedDiscoveryClient(config).get_regional_api_endpoint(
            "us-west-2", force_refresh=True
        )
        assert SharedDiscoveryClient(config).get_regional_api_endpoint("us-west-2") is refreshed
        clock[0] += config.cache_ttl_seconds
        SharedDiscoveryClient(config).get_regional_api_endpoint("us-west-2")
        assert lookups == [
            ("us-west-2", "gco", False),
            ("us-west-2", "gco", True),
            ("us-west-2", "gco", False),
        ]

    def test_clear_discovery_cache_forgets_every_endpoint(
        self, lookups: list[Any], clock: list[float]
    ) -> None:
        client = SharedDiscoveryClient(GCOConfig())
        client.get_api_endpoint()
        client.get_regional_api_endpoint("us-east-1")
        clear_discovery_cache()
        SharedDiscoveryClient(GCOConfig()).get_api_endpoint()
        SharedDiscoveryClient(GCOConfig()).get_regional_api_endpoint("us-east-1")
        assert [entry[0] for entry in lookups] == ["global", "us-east-1", "global", "us-east-1"]


# ---------------------------------------------------------------------------
# Small helpers: RegionRead, _object, _job_errors, _fan_out
# ---------------------------------------------------------------------------


def test_region_read_is_ok_only_without_an_error() -> None:
    assert RegionRead("us-east-1", {"status": "healthy"}).ok
    assert not RegionRead("us-west-2", error="API request failed: 503").ok


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"MODEL": "example/model"}, {"MODEL": "example/model"}),
        (SimpleNamespace(), {}),
        (None, {}),
        (["MODEL"], {}),
    ],
)
def test_object_keeps_only_json_objects(value: object, expected: dict[str, Any]) -> None:
    result = _object(value)
    assert result == expected
    assert result is not value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([{"name": "a"}, "junk", None, ["nested"], {"name": "b"}], [{"name": "a"}, {"name": "b"}]),
        ([], []),
        (None, []),
        ({"name": "a"}, []),
        ("[]", []),
    ],
    ids=["mixed", "empty", "null", "an-object", "a-string"],
)
def test_objects_keeps_only_the_json_objects_of_a_list(
    value: object, expected: list[dict[str, Any]]
) -> None:
    assert _objects(value) == expected


def test_utilization_rows_are_rows_whose_errors_survive_a_copy_and_a_pickle() -> None:
    sample = {"region": "us-east-1", "metric": "cpu", "timestamp": NOW, "value": 3.0}
    rows = UtilizationRows([sample], {"us-west-2": "Throttling: Rate exceeded"})
    assert rows == [sample]
    # The dashboard's cache pickles what a loader returns; the fake fleet deep-copies it.
    pickled = pickle.dumps(rows)  # nosemgrep: avoid-pickle - an in-process round trip
    unpickled = pickle.loads(pickled)  # nosemgrep: avoid-pickle - an in-process round trip
    for copied in (copy.deepcopy(rows), unpickled):
        assert isinstance(copied, UtilizationRows)
        assert copied == [sample]
        assert copied.errors == {"us-west-2": "Throttling: Rate exceeded"}
    assert UtilizationRows().errors == {}
    assert UtilizationRows() == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, {}),
        ({"region": "us-west-2", "error": "timeout"}, {}),
        (
            [
                {"region": "us-west-2", "error": "Regional request failed"},
                {"region": "eu-west-1", "error": None},
                {"region": "", "error": "unnamed"},
                {"error": "no region"},
                "junk",
            ],
            {"us-west-2": "Regional request failed", "eu-west-1": "unreadable"},
        ),
    ],
    ids=["null", "not-a-list", "mixed"],
)
def test_job_errors_maps_the_aggregator_errors_by_region(
    raw: object, expected: dict[str, str]
) -> None:
    assert _job_errors(raw) == expected


class TestFanOut:
    def test_no_regions_reads_nothing(self) -> None:
        def read(region: str) -> dict[str, Any]:
            raise AssertionError("no Region to read")

        assert _fan_out([], read) == []

    def test_each_region_keeps_its_own_answer_or_error_in_order(self) -> None:
        def read(region: str) -> dict[str, Any]:
            if region == "us-west-2":
                raise APIRequestError(503, "Service Unavailable")
            return {"region": region}

        reads = _fan_out(["us-east-1", "us-west-2", "eu-west-1"], read)
        assert reads == [
            RegionRead("us-east-1", {"region": "us-east-1"}),
            RegionRead("us-west-2", error="API request failed: Service Unavailable (HTTP 503)"),
            RegionRead("eu-west-1", {"region": "eu-west-1"}),
        ]

    def test_a_regions_error_ends_with_what_to_do_about_it(self) -> None:
        # Pages print a RegionRead's error as one line, so the hint is part of it.
        def read(region: str) -> dict[str, Any]:
            if region == "us-east-1":
                raise client_error("ExpiredToken", _GATEWAY_EXPIRED)
            raise APIRequestError(504, "Endpoint request timed out")

        reads = _fan_out(["us-east-1", "us-west-2"], read)
        assert [read.error for read in reads] == [
            f"AWS rejected the credentials (ExpiredToken). {_EXPIRED}",
            "API request failed: Endpoint request timed out (HTTP 504). "
            "The API Gateway front door timed out; try again.",
        ]

    def test_regions_are_read_at_the_same_time(self) -> None:
        # Serial reads would leave the first one waiting at the barrier until it broke.
        barrier = threading.Barrier(3, timeout=10)

        def read(region: str) -> dict[str, Any]:
            barrier.wait()
            return {}

        reads = _fan_out(["us-east-1", "us-west-2", "eu-west-1"], read)
        assert [r.ok for r in reads] == [True, True, True]

    @pytest.mark.parametrize(("count", "workers"), [(2, 2), (8, 8), (12, 8)])
    def test_the_pool_never_exceeds_max_fanout(
        self, monkeypatch: pytest.MonkeyPatch, count: int, workers: int
    ) -> None:
        sizes: list[int | None] = []

        class RecordingPool(ThreadPoolExecutor):
            def __init__(self, max_workers: int | None = None) -> None:
                sizes.append(max_workers)
                super().__init__(max_workers=max_workers)

        monkeypatch.setattr(data, "ThreadPoolExecutor", RecordingPool)
        reads = _fan_out([f"region-{i}" for i in range(count)], lambda region: {"r": region})
        assert sizes == [workers]
        assert [r.data["r"] for r in reads] == [f"region-{i}" for i in range(count)]


# ---------------------------------------------------------------------------
# Manifests: manifest_requests_gpu, load_manifest_text
# ---------------------------------------------------------------------------

_GPU_JOB = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "metadata": {"name": "gpu-test-job"},
    "spec": {
        "template": {
            "spec": {
                "containers": [
                    {"name": "main", "resources": {"limits": {"nvidia.com/gpu": 1}}},
                ]
            }
        }
    },
}


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (_GPU_JOB, True),
        ([{"kind": "ConfigMap", "data": {"a": "b"}}, _GPU_JOB], True),
        ({"spec": {"containers": [{"resources": {"requests": {"nvidia.com/gpu": "2"}}}]}}, True),
        ({"kind": "Job", "spec": {"containers": [{"resources": {"limits": {"cpu": "1"}}}]}}, False),
        ([], False),
        ("nvidia.com/gpu", False),
        (None, False),
    ],
    ids=["limits", "in-a-list", "requests", "cpu-only", "empty-list", "scalar", "null"],
)
def test_manifest_requests_gpu_finds_the_resource_at_any_depth(
    document: object, expected: bool
) -> None:
    assert manifest_requests_gpu(document) is expected


class TestLoadManifestText:
    def test_every_document_is_returned_and_empty_ones_are_dropped(self) -> None:
        text = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings}\n"
            "---\n---\n"
            "apiVersion: batch/v1\nkind: Job\nmetadata: {name: trainer}\n"
        )
        manifests = load_manifest_text(text)
        assert [(m["kind"], m["metadata"]["name"]) for m in manifests] == [
            ("ConfigMap", "settings"),
            ("Job", "trainer"),
        ]

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("kind: Job\nmetadata: [unclosed", "The YAML does not parse: "),
            ("base: &shared {kind: Job}\nother: *shared\n", "The YAML does not parse: "),
            ("", "The YAML holds no manifests."),
            ("# only a comment\n---\n", "The YAML holds no manifests."),
            ("- kind: Job\n", "Every YAML document must be a Kubernetes object with a kind."),
            (
                "metadata: {name: x}\n",
                "Every YAML document must be a Kubernetes object with a kind.",
            ),
        ],
        ids=["malformed", "alias", "empty", "comment-only", "list", "no-kind"],
    )
    def test_anything_but_kubernetes_objects_is_refused(self, text: str, message: str) -> None:
        with pytest.raises(DashboardError) as raised:
            load_manifest_text(text)
        assert str(raised.value).startswith(message)

    def test_a_parse_failure_keeps_the_yaml_error_as_its_cause(self) -> None:
        with pytest.raises(DashboardError, match="does not parse") as raised:
            load_manifest_text("kind: [")
        assert raised.value.__cause__ is not None


# ---------------------------------------------------------------------------
# example_header
# ---------------------------------------------------------------------------

_SIMPLE_HEADER = """\
# Simple Kubernetes Job Example
#
# A minimal job that prints a message, sleeps briefly, and exits. Useful for
# verifying cluster connectivity and the submission path end-to-end.
#
# Prerequisites:
#   - None
#
# Usage:
#   gco jobs submit-sqs examples/simple-job.yaml --region us-east-1
apiVersion: batch/v1
kind: Job
"""


class TestExampleHeader:
    def test_title_paragraph_and_prerequisites_come_from_the_header(self) -> None:
        assert example_header(_SIMPLE_HEADER) == (
            "Simple Kubernetes Job Example",
            "A minimal job that prints a message, sleeps briefly, and exits. Useful for "
            "verifying cluster connectivity and the submission path end-to-end.",
            "None",
        )

    @pytest.mark.parametrize(
        "text",
        ["apiVersion: batch/v1\nkind: Job\n", "", "\n\n"],
        ids=["no-header", "empty", "blank"],
    )
    def test_no_comment_header_gives_empty_fields(self, text: str) -> None:
        assert example_header(text) == ("", "", "")

    def test_leading_blank_lines_are_skipped_and_a_title_alone_is_enough(self) -> None:
        assert example_header("\n\n# Only a title\nkind: Job\n") == ("Only a title", "", "")

    def test_a_header_that_runs_to_the_end_of_the_file_is_read_whole(self) -> None:
        text = "# Title\n# Does one thing.\n# Prerequisites:\n#   - FSx for Lustre"
        assert example_header(text) == ("Title", "Does one thing.", "FSx for Lustre")

    def test_a_label_right_after_the_paragraph_ends_it(self) -> None:
        text = "# Title\n# Does one thing.\n# Usage:\n#   gco jobs submit x.yaml\n"
        assert example_header(text) == ("Title", "Does one thing.", "")

    def test_prerequisites_stop_at_the_next_label_and_skip_empty_bullets(self) -> None:
        text = (
            "# Title\n#\n# Summary.\n#\n# Prerequisites:\n"
            "#   - Shared EFS PVC\n#   -\n#   - One of:\n#   - IAM role\n"
            "# Usage:\n#   gco jobs submit x.yaml\n"
        )
        assert example_header(text) == ("Title", "Summary.", "Shared EFS PVC; One of:; IAM role")

    def test_a_wrapped_line_continues_its_item(self) -> None:
        text = (
            "# GPU Job Example\n#\n# Runs nvidia-smi.\n#\n# Prerequisites:\n"
            "#   - GPU nodepool (enabled by default)\n"
            "#   - Nothing to install for the GPU itself: EKS Auto Mode nodes ship the\n"
            "#     NVIDIA driver and device plugin.\n"
            "#\n# Usage:\n"
        )
        assert example_header(text)[2] == (
            "GPU nodepool (enabled by default); Nothing to install for the GPU itself: "
            "EKS Auto Mode nodes ship the NVIDIA driver and device plugin."
        )

    def test_numbered_items_are_items_and_an_unbulleted_first_line_starts_one(self) -> None:
        text = (
            "# Title\n# Summary.\n# Prerequisites:\n"
            "#   1. gco analytics enable\n"
            "#      Sets analytics_environment.enabled in cdk.json.\n"
            "#   2) gco stacks deploy gco-analytics\n"
            "#\n"
        )
        assert example_header(text)[2] == (
            "1. gco analytics enable Sets analytics_environment.enabled in cdk.json.; "
            "2) gco stacks deploy gco-analytics"
        )
        assert example_header("# T\n# Prerequisites:\n#   Nothing at all\n#   really\n")[2] == (
            "Nothing at all really"
        )


# ---------------------------------------------------------------------------
# FleetData plumbing: client, session, topology, identity
# ---------------------------------------------------------------------------


class TestClients:
    def test_client_is_a_fresh_shared_discovery_client_on_the_fleet_config(
        self, fleet: FleetData
    ) -> None:
        first, second = fleet.client(), fleet.client()
        assert isinstance(first, SharedDiscoveryClient)
        assert first is not second
        assert first.config is fleet.config

    def test_session_is_a_fresh_boto3_session(self, fleet: FleetData) -> None:
        first, second = fleet.session(), fleet.session()
        assert isinstance(first, boto3.Session)
        assert first is not second


class TestTopology:
    def test_cdk_json_regions_win_and_non_strings_are_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            data,
            "_load_cdk_json",
            lambda: {
                "global": "us-east-2",
                "api_gateway": "us-west-2",
                "monitoring": "eu-west-1",
                "regional": ["us-east-1", 7, None, "eu-west-1"],
            },
        )
        assert FleetData(GCOConfig(project_name="research")).topology() == Topology(
            project="research",
            global_region="us-east-2",
            api_gateway_region="us-west-2",
            monitoring_region="eu-west-1",
            workload_regions=("us-east-1", "eu-west-1"),
            configured=True,
            regional_api=False,
        )

    def test_without_cdk_json_the_cli_defaults_apply_and_nothing_is_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(data, "_load_cdk_json", dict)
        config = GCOConfig(use_regional_api=True, monitoring_region="ap-south-1")
        topology = FleetData(config).topology()
        assert topology == Topology(
            project="gco",
            global_region="us-east-2",
            api_gateway_region="us-east-2",
            monitoring_region="ap-south-1",
            workload_regions=(),
            configured=False,
            regional_api=True,
        )


class TestIdentity:
    @pytest.mark.parametrize(
        ("arn", "principal"),
        [
            (f"arn:aws:sts::{ACCOUNT}:assumed-role/Admin/tester", "tester"),
            (f"arn:aws:iam::{ACCOUNT}:user/tester", "tester"),
            (f"arn:aws:iam::{ACCOUNT}:root", f"arn:aws:iam::{ACCOUNT}:root"),
        ],
        ids=["assumed-role", "user", "root"],
    )
    def test_identity_names_the_signed_in_principal(
        self, monkeypatch: pytest.MonkeyPatch, fleet: FleetData, arn: str, principal: str
    ) -> None:
        session = patch_session(monkeypatch, fleet)
        session.stub("sts").add_response(
            "get_caller_identity",
            {"UserId": "AIDAEXAMPLE", "Account": ACCOUNT, "Arn": arn},
            {},
        )
        assert fleet.identity() == {"arn": arn, "account": ACCOUNT, "principal": principal}
        session.assert_no_pending_responses()

    def test_identity_failures_propagate_for_the_caller_to_describe(
        self, monkeypatch: pytest.MonkeyPatch, fleet: FleetData
    ) -> None:
        session = patch_session(monkeypatch, fleet)
        session.stub("sts").add_client_error(
            "get_caller_identity",
            service_error_code="ExpiredToken",
            service_message="The security token included in the request is expired",
            http_status_code=403,
        )
        with pytest.raises(ClientError) as raised:
            fleet.identity()
        assert str(describe_error(raised.value)) == "AWS rejected the credentials (ExpiredToken)."


# ---------------------------------------------------------------------------
# FleetData.examples
# ---------------------------------------------------------------------------

_HEADER = "# {title}\n#\n# {summary}\n#\n# Prerequisites:\n#   - {prerequisite}\n#\n"


def _write(directory: Path, name: str, text: str | bytes) -> None:
    path = directory / name
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


@pytest.fixture(name="example_root")
def example_root_fixture(tmp_path: Path) -> Path:
    """A checkout whose examples/ mixes Job manifests with files discovery must skip."""
    examples = tmp_path / "examples"
    examples.mkdir()
    _write(
        examples,
        "b-gpu-job.yaml",
        _HEADER.format(title="GPU Job", summary="Runs nvidia-smi.", prerequisite="GPU nodepool")
        + json.dumps(_GPU_JOB),
    )
    _write(
        examples,
        "a-queue-and-job.yaml",
        "# Queue and Job\n# Two objects and a stray scalar.\n"
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: settings}\n"
        "---\njust a scalar\n---\n"
        "apiVersion: batch/v1\nkind: Job\nmetadata: {name: worker}\n",
    )
    _write(examples, "c-deployment.yaml", "apiVersion: apps/v1\nkind: Deployment\n")
    _write(examples, "d-broken.yaml", "kind: Job\nmetadata: [unclosed\n")
    _write(examples, "e-binary.yaml", b"kind: Job\n\xff\xfe\n")
    _write(
        examples,
        "f-alias.yaml",
        "apiVersion: batch/v1\nkind: Job\nmetadata: {name: &n worker, labels: {app: *n}}\n",
    )
    _write(examples, "g-list.yaml", "- kind: Job\n")
    _write(examples, "notes.yml", "apiVersion: batch/v1\nkind: Job\n")
    _write(examples, "README.md", "# Examples\n")
    return tmp_path


class TestExamples:
    def test_only_parseable_job_manifests_are_listed_in_name_order(
        self, fleet: FleetData, example_root: Path
    ) -> None:
        examples = fleet.examples(root=example_root)
        directory = example_root / "examples"
        assert examples == [
            ExampleManifest(
                name="a-queue-and-job",
                path=str(directory / "a-queue-and-job.yaml"),
                kinds=("ConfigMap", "Job"),
                gpu=False,
                title="Queue and Job",
                summary="Two objects and a stray scalar.",
                prerequisites="",
            ),
            ExampleManifest(
                name="b-gpu-job",
                path=str(directory / "b-gpu-job.yaml"),
                kinds=("Job",),
                gpu=True,
                title="GPU Job",
                summary="Runs nvidia-smi.",
                prerequisites="GPU nodepool",
            ),
        ]

    def test_the_default_root_is_the_cli_project_directory(
        self, monkeypatch: pytest.MonkeyPatch, fleet: FleetData, example_root: Path
    ) -> None:
        monkeypatch.setattr(data, "_project_dir", lambda: example_root)
        assert [e.name for e in fleet.examples()] == ["a-queue-and-job", "b-gpu-job"]

    def test_a_checkout_without_examples_lists_nothing(
        self, fleet: FleetData, tmp_path: Path
    ) -> None:
        assert fleet.examples(root=tmp_path) == []

    def test_the_checkouts_own_examples_are_discovered(self, fleet: FleetData) -> None:
        found = fleet.examples(root=REPO_ROOT)
        assert [e.path for e in found] == sorted(e.path for e in found)
        examples = {example.name: example for example in found}
        simple, gpu = examples["simple-job"], examples["gpu-job"]
        assert (simple.title, simple.gpu, simple.prerequisites) == (
            "Simple Kubernetes Job Example",
            False,
            "None",
        )
        assert (gpu.title, gpu.gpu) == ("GPU Job Example", True)
        assert gpu.prerequisites.startswith("GPU nodepool (enabled by default); ")
        assert "inference-vllm" not in examples
        for example in examples.values():
            assert "Job" in example.kinds
            assert example.title
            assert Path(example.path).parent == REPO_ROOT / "examples"


# ---------------------------------------------------------------------------
# Console routes
# ---------------------------------------------------------------------------


def test_console_routes_are_described_api_paths_and_global_only_is_a_subset() -> None:
    assert all(path.startswith("/api/v1/") and text for path, text in CONSOLE_ROUTES.items())
    assert CONSOLE_ROUTES.keys() >= GLOBAL_ONLY_ROUTES
    assert all(path.startswith("/api/v1/global/") for path in GLOBAL_ONLY_ROUTES)
