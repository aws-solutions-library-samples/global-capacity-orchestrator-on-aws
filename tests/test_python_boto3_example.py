"""Unit tests for ``docs/client-examples/python_boto3_example.py``.

The example is end-user documentation: it looks up the API Gateway endpoint in
CloudFormation, builds SigV4 auth from the boto3 credential chain, and POSTs
Kubernetes manifests to ``/api/v1/manifests``. It lives in a hyphenated,
package-less directory, so it is loaded by file path (the same convention the
``dockerfiles/`` and ``.github/scripts`` suites use).

Behaviours pinned here:

* ``get_api_endpoint`` reads ``ApiEndpoint`` from the ``<project>-api-gateway``
  stack outputs, skips unrelated outputs, strips a trailing slash, raises
  ``ValueError`` naming the stack when the output is absent, and lets
  CloudFormation's missing-stack error propagate.
* ``create_aws_auth`` freezes the boto3 session credentials (including any
  session token) into an ``AWSRequestsAuth`` signer for ``execute-api`` on the
  endpoint host, and refuses to run with an empty credential chain.
* ``submit_manifests`` / ``get_health`` hit the documented routes with the
  documented payload, headers and timeout, return the parsed JSON body, and
  surface HTTP failures as ``requests.exceptions.HTTPError``.
* ``get_deployment_config`` reads ``project_name`` and the API Gateway region
  from the repository ``cdk.json`` (two directories above the example) and
  falls back field by field to ``("gco", "us-east-2")`` when the file is
  missing, malformed, incomplete or well-formed JSON of the wrong shape.
* ``main`` runs the four documented examples end to end with one shared auth
  object, keeps going after an HTTP failure, and propagates the lookup and
  credential errors it does not catch.

Dependency stand-in: the example imports ``aws_requests_auth`` (``pip install
aws-requests-auth`` in its own docstring). That package is an end-user
requirement of the example, not a project dependency, so it is absent from the
venv and from CI. When it is not importable, this module installs a minimal
``aws_requests_auth.aws_auth.AWSRequestsAuth`` stand-in in ``sys.modules`` for
the duration of the example's import and removes it again afterwards. The
stand-in mirrors the real class's constructor signature and attribute names
(``aws_access_key``, ``aws_secret_access_key``, ``aws_host``, ``aws_region``,
``service``, ``aws_token``), so the same assertions hold against the real
package when a developer has it installed.

Everything is offline and deterministic: ``boto3`` and ``requests`` are
replaced on the loaded module with in-memory fakes that serve real
``requests.Response`` objects (so ``raise_for_status`` semantics are the real
ones), credentials are ``botocore`` ``Credentials`` built from placeholder
strings, and ``cdk.json`` lookups are redirected to ``tmp_path`` by relocating
the module's ``__file__``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import requests
from botocore.credentials import Credentials
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PATH = REPO_ROOT / "docs" / "client-examples" / "python_boto3_example.py"

STAND_IN_MODULES = ("aws_requests_auth", "aws_requests_auth.aws_auth")


class _StandInAWSRequestsAuth(requests.auth.AuthBase):
    """Minimal replacement for ``aws_requests_auth.aws_auth.AWSRequestsAuth``.

    Keeps the real class's constructor signature and attribute names so the
    assertions below hold whether or not the real package is installed. It
    never signs anything: every HTTP call in this suite is intercepted before
    ``requests`` would invoke the auth callable.
    """

    def __init__(
        self,
        aws_access_key: str,
        aws_secret_access_key: str,
        aws_host: str,
        aws_region: str,
        aws_service: str,
        aws_token: str | None = None,
    ) -> None:
        self.aws_access_key = aws_access_key
        self.aws_secret_access_key = aws_secret_access_key
        self.aws_host = aws_host
        self.aws_region = aws_region
        self.service = aws_service
        self.aws_token = aws_token

    def __call__(self, request: requests.PreparedRequest) -> requests.PreparedRequest:
        return request


def _load_example() -> ModuleType:
    """Load the example by path, standing in for ``aws_requests_auth`` when it is absent.

    The stand-in only lives in ``sys.modules`` while the example executes its
    imports; it is removed afterwards so nothing else in the session sees it.
    """
    stand_in_needed = importlib.util.find_spec("aws_requests_auth") is None
    if stand_in_needed:
        package = ModuleType("aws_requests_auth")
        package.__path__ = []
        auth_module = ModuleType("aws_requests_auth.aws_auth")
        auth_module.AWSRequestsAuth = _StandInAWSRequestsAuth
        package.aws_auth = auth_module
        sys.modules["aws_requests_auth"] = package
        sys.modules["aws_requests_auth.aws_auth"] = auth_module
    try:
        spec = importlib.util.spec_from_file_location(
            "_gco_docs_python_boto3_example", EXAMPLE_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        if stand_in_needed:
            for name in STAND_IN_MODULES:
                sys.modules.pop(name, None)
    return module


example = _load_example()


# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------

API_ENDPOINT = "https://abc123.execute-api.eu-west-1.amazonaws.com/prod"
API_HOST = "abc123.execute-api.eu-west-1.amazonaws.com"
API_ENDPOINT_OUTPUT = {
    "OutputKey": "ApiEndpoint",
    "OutputValue": API_ENDPOINT + "/",
    "Description": "API Gateway invoke URL",
}
UNRELATED_OUTPUT = {"OutputKey": "ApiId", "OutputValue": "abc123"}
ACME_CDK_JSON = {
    "context": {
        "project_name": "acme",
        "deployment_regions": {"global": "us-east-1", "api_gateway": "eu-west-1"},
    }
}
TEST_CREDENTIALS = Credentials("test-access-key", "test-secret-key", "test-session-token")


def _json_response(status_code: int, body: Any) -> requests.Response:
    """A real ``requests.Response`` carrying a JSON body, built without any network.

    ``url`` is filled in by the fake transport from the request, mirroring what
    ``requests`` does, so ``raise_for_status`` messages name the route.
    """
    response = requests.Response()
    response.status_code = status_code
    response.reason = HTTPStatus(status_code).phrase
    response.encoding = "utf-8"
    response._content = json.dumps(body).encode("utf-8")
    return response


def _missing_stack_error(stack_name: str) -> ClientError:
    """The ``ValidationError`` CloudFormation returns for ``DescribeStacks`` on an unknown stack."""
    message = f"Stack with id {stack_name} does not exist"
    return ClientError({"Error": {"Code": "ValidationError", "Message": message}}, "DescribeStacks")


class _FakeRequests:
    """Replacement for the example's ``requests`` module global.

    Records every ``post``/``get`` call and answers each from a queue of canned
    responses. ``exceptions`` is the real ``requests.exceptions`` so the
    example's ``except requests.exceptions.HTTPError`` matches what the real
    ``Response.raise_for_status`` raises.
    """

    exceptions = requests.exceptions

    def __init__(self, responses: list[requests.Response]) -> None:
        self._responses = list(responses)
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def _next(self, url: str) -> requests.Response:
        response = self._responses.pop(0)
        response.url = url
        return response

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        self.post_calls.append({"url": url, **kwargs})
        return self._next(url)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.get_calls.append({"url": url, **kwargs})
        return self._next(url)


class _FakeCloudFormation:
    """``boto3.client("cloudformation")`` stand-in serving one stack's outputs (or an error)."""

    def __init__(
        self, outputs: list[dict[str, str]] | None, error: ClientError | None = None
    ) -> None:
        self._outputs = outputs
        self._error = error
        self.describe_calls: list[str] = []

    def describe_stacks(self, StackName: str) -> dict[str, Any]:
        self.describe_calls.append(StackName)
        if self._error is not None:
            raise self._error
        return {
            "Stacks": [
                {
                    "StackName": StackName,
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": self._outputs,
                }
            ]
        }


class _FakeSession:
    """``boto3.Session()`` stand-in exposing only the credential lookup the example uses."""

    def __init__(self, credentials: Credentials | None) -> None:
        self._credentials = credentials

    def get_credentials(self) -> Credentials | None:
        return self._credentials


class _FakeBoto3:
    """Replacement for the example's ``boto3`` module global."""

    def __init__(
        self, cloudformation: _FakeCloudFormation, credentials: Credentials | None
    ) -> None:
        self.cloudformation = cloudformation
        self.credentials = credentials
        self.client_calls: list[tuple[str, str | None]] = []
        self.sessions_created = 0

    def client(self, service_name: str, region_name: str | None = None) -> _FakeCloudFormation:
        self.client_calls.append((service_name, region_name))
        return self.cloudformation

    def Session(self) -> _FakeSession:
        self.sessions_created += 1
        return _FakeSession(self.credentials)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outputs: list[dict[str, str]] | None = None,
    stack_error: ClientError | None = None,
    credentials: Credentials | None = TEST_CREDENTIALS,
    responses: list[requests.Response] | None = None,
) -> tuple[_FakeBoto3, _FakeRequests]:
    """Swap the example's ``boto3`` and ``requests`` globals for recording fakes."""
    fake_boto3 = _FakeBoto3(_FakeCloudFormation(outputs, stack_error), credentials)
    fake_requests = _FakeRequests(responses or [])
    monkeypatch.setattr(example, "boto3", fake_boto3)
    monkeypatch.setattr(example, "requests", fake_requests)
    return fake_boto3, fake_requests


def _write_cdk_json(root: Path, payload: Any) -> None:
    (root / "cdk.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repository layout; the example's ``cdk.json`` lookup resolves inside it."""
    example_dir = tmp_path / "docs" / "client-examples"
    example_dir.mkdir(parents=True)
    monkeypatch.setattr(example, "__file__", str(example_dir / "python_boto3_example.py"))
    return tmp_path


# ---------------------------------------------------------------------------
# get_api_endpoint
# ---------------------------------------------------------------------------


def test_get_api_endpoint_reads_output_from_project_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    """The endpoint is the ``ApiEndpoint`` output of ``<project>-api-gateway`` in the given
    region, with unrelated outputs skipped and the trailing slash removed."""
    fake_boto3, _ = _install_fakes(monkeypatch, outputs=[UNRELATED_OUTPUT, API_ENDPOINT_OUTPUT])

    endpoint = example.get_api_endpoint("eu-west-1", "acme")

    assert endpoint == API_ENDPOINT
    assert fake_boto3.client_calls == [("cloudformation", "eu-west-1")]
    assert fake_boto3.cloudformation.describe_calls == ["acme-api-gateway"]


def test_get_api_endpoint_defaults_to_the_gco_stack_and_keeps_clean_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a project name the stock ``gco-api-gateway`` stack is queried; a URL that has
    no trailing slash is returned unchanged."""
    fake_boto3, _ = _install_fakes(
        monkeypatch, outputs=[{"OutputKey": "ApiEndpoint", "OutputValue": API_ENDPOINT}]
    )

    assert example.get_api_endpoint("us-east-2") == API_ENDPOINT
    assert fake_boto3.cloudformation.describe_calls == ["gco-api-gateway"]


@pytest.mark.parametrize("outputs", [[], [UNRELATED_OUTPUT]], ids=["no-outputs", "unrelated-only"])
def test_get_api_endpoint_raises_when_output_is_missing(
    monkeypatch: pytest.MonkeyPatch, outputs: list[dict[str, str]]
) -> None:
    """A stack without an ``ApiEndpoint`` output raises ``ValueError`` naming the stack."""
    _install_fakes(monkeypatch, outputs=outputs)

    with pytest.raises(ValueError, match=r"ApiEndpoint not found in stack acme-api-gateway"):
        example.get_api_endpoint("eu-west-1", "acme")


def test_get_api_endpoint_propagates_missing_stack_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """CloudFormation's ``ValidationError`` for an absent stack is not swallowed."""
    _install_fakes(monkeypatch, stack_error=_missing_stack_error("acme-api-gateway"))

    with pytest.raises(ClientError, match=r"Stack with id acme-api-gateway does not exist"):
        example.get_api_endpoint("eu-west-1", "acme")


# ---------------------------------------------------------------------------
# create_aws_auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["test-session-token", None], ids=["temporary", "static"])
def test_create_aws_auth_freezes_session_credentials_for_execute_api(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    """The frozen boto3 credentials (session token included when present) become a SigV4
    signer for ``execute-api`` on the given host and region."""
    _install_fakes(
        monkeypatch, credentials=Credentials("test-access-key", "test-secret-key", token)
    )

    auth = example.create_aws_auth(API_HOST, "eu-west-1")

    assert isinstance(auth, example.AWSRequestsAuth)
    assert isinstance(auth, requests.auth.AuthBase)
    assert (auth.aws_access_key, auth.aws_secret_access_key, auth.aws_token) == (
        "test-access-key",
        "test-secret-key",
        token,
    )
    assert (auth.aws_host, auth.aws_region, auth.service) == (API_HOST, "eu-west-1", "execute-api")


def test_create_aws_auth_refuses_to_run_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty credential chain is a clear ``RuntimeError`` rather than a signer with ``None`` keys."""
    _install_fakes(monkeypatch, credentials=None)

    with pytest.raises(RuntimeError, match=r"No AWS credentials are available"):
        example.create_aws_auth(API_HOST, "eu-west-1")


# ---------------------------------------------------------------------------
# submit_manifests / get_health
# ---------------------------------------------------------------------------

CONFIG_MAP = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cfg"}, "data": {}}


def test_submit_manifests_posts_the_documented_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manifests go to ``POST /api/v1/manifests`` as JSON with ``dry_run`` false and no
    ``namespace`` key, the JSON content type, the caller's auth and a 30s timeout; the parsed
    body is returned."""
    _, fake_requests = _install_fakes(
        monkeypatch, responses=[_json_response(200, {"status": "applied", "applied": 1})]
    )
    auth = example.create_aws_auth(API_HOST, "eu-west-1")

    result = example.submit_manifests(API_ENDPOINT, auth, [CONFIG_MAP])

    assert result == {"status": "applied", "applied": 1}
    assert fake_requests.post_calls == [
        {
            "url": f"{API_ENDPOINT}/api/v1/manifests",
            "json": {"manifests": [CONFIG_MAP], "dry_run": False},
            "auth": auth,
            "headers": {"Content-Type": "application/json"},
            "timeout": 30,
        }
    ]
    assert fake_requests.get_calls == []


def test_submit_manifests_sends_namespace_and_dry_run_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``namespace`` is added to the payload only when given, and ``dry_run=True`` is forwarded."""
    _, fake_requests = _install_fakes(monkeypatch, responses=[_json_response(200, {"ok": True})])
    auth = example.create_aws_auth(API_HOST, "eu-west-1")

    result = example.submit_manifests(
        API_ENDPOINT, auth, [CONFIG_MAP], namespace="team-a", dry_run=True
    )

    assert result == {"ok": True}
    assert fake_requests.post_calls[0]["json"] == {
        "manifests": [CONFIG_MAP],
        "dry_run": True,
        "namespace": "team-a",
    }


def test_submit_manifests_raises_http_error_with_the_response_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 4xx answer surfaces as ``requests.exceptions.HTTPError`` carrying the response body."""
    _install_fakes(monkeypatch, responses=[_json_response(403, {"message": "Forbidden"})])
    auth = example.create_aws_auth(API_HOST, "eu-west-1")

    with pytest.raises(
        requests.exceptions.HTTPError,
        match=r"403 Client Error: Forbidden for url: .*/api/v1/manifests$",
    ) as exc_info:
        example.submit_manifests(API_ENDPOINT, auth, [CONFIG_MAP])

    assert exc_info.value.response.status_code == 403
    assert exc_info.value.response.json() == {"message": "Forbidden"}


def test_get_health_reads_the_health_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """``GET /api/v1/health`` is called with just the auth and a 30s timeout; its JSON body
    is returned."""
    _, fake_requests = _install_fakes(
        monkeypatch, responses=[_json_response(200, {"status": "healthy", "nodes": 3})]
    )
    auth = example.create_aws_auth(API_HOST, "eu-west-1")

    result = example.get_health(API_ENDPOINT, auth)

    assert result == {"status": "healthy", "nodes": 3}
    assert fake_requests.get_calls == [
        {"url": f"{API_ENDPOINT}/api/v1/health", "auth": auth, "timeout": 30}
    ]
    assert fake_requests.post_calls == []


def test_get_health_raises_http_error_on_server_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 5xx answer from the health route propagates as ``HTTPError``."""
    _install_fakes(monkeypatch, responses=[_json_response(502, {"message": "bad gateway"})])
    auth = example.create_aws_auth(API_HOST, "eu-west-1")

    with pytest.raises(requests.exceptions.HTTPError, match=r"502 Server Error: Bad Gateway"):
        example.get_health(API_ENDPOINT, auth)


# ---------------------------------------------------------------------------
# get_deployment_config
# ---------------------------------------------------------------------------


def test_get_deployment_config_reads_project_and_api_gateway_region(repo: Path) -> None:
    """``project_name`` and ``deployment_regions.api_gateway`` come from the ``cdk.json`` two
    directories above the example."""
    _write_cdk_json(repo, ACME_CDK_JSON)

    assert example.get_deployment_config() == ("acme", "eu-west-1")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, ("gco", "us-east-2")),
        ({"context": {}}, ("gco", "us-east-2")),
        ({"context": {"project_name": "acme"}}, ("acme", "us-east-2")),
        ({"context": {"deployment_regions": {"api_gateway": "ap-south-1"}}}, ("gco", "ap-south-1")),
        ({"context": {"deployment_regions": {"global": "us-east-1"}}}, ("gco", "us-east-2")),
        (
            {"context": {"project_name": None, "deployment_regions": {"api_gateway": ""}}},
            ("gco", "us-east-2"),
        ),
        (
            {"context": {"project_name": 7, "deployment_regions": {"api_gateway": "eu-central-1"}}},
            ("7", "eu-central-1"),
        ),
    ],
    ids=[
        "no-context",
        "empty-context",
        "project-only",
        "region-only",
        "other-regions-only",
        "null-and-empty",
        "stringified",
    ],
)
def test_get_deployment_config_falls_back_field_by_field(
    repo: Path, payload: dict[str, Any], expected: tuple[str, str]
) -> None:
    """Each missing, null or empty field independently falls back to the stock ``gco`` /
    ``us-east-2`` value; present values are stringified."""
    _write_cdk_json(repo, payload)

    assert example.get_deployment_config() == expected


def test_get_deployment_config_defaults_when_cdk_json_is_missing(repo: Path) -> None:
    """No ``cdk.json`` (an ``OSError`` on open) yields the stock values instead of failing."""
    assert not (repo / "cdk.json").exists()

    assert example.get_deployment_config() == ("gco", "us-east-2")


def test_get_deployment_config_defaults_when_cdk_json_is_malformed(repo: Path) -> None:
    """Unparseable JSON (a ``ValueError`` from ``json.load``) yields the stock values."""
    (repo / "cdk.json").write_text('{"context": {"project_name": ', encoding="utf-8")

    assert example.get_deployment_config() == ("gco", "us-east-2")


@pytest.mark.parametrize(
    "payload",
    [[], {"context": []}, {"context": None}, {"context": {"deployment_regions": []}}],
    ids=["top-level-list", "context-list", "context-null", "regions-list"],
)
def test_get_deployment_config_defaults_when_cdk_json_has_the_wrong_shape(
    repo: Path, payload: Any
) -> None:
    """Well-formed JSON of the wrong shape (a list or ``null`` where a mapping is expected)
    is "malformed" too: the stock values are used rather than an ``AttributeError`` escaping
    from ``.get``."""
    _write_cdk_json(repo, payload)

    assert example.get_deployment_config() == ("gco", "us-east-2")


def test_example_sits_two_directories_below_the_repository_cdk_json() -> None:
    """Un-relocated, the example's ``parents[2]`` arithmetic lands on the repository root's
    ``cdk.json`` and reads the real project name and API Gateway region from it."""
    cdk_json = Path(example.__file__).resolve().parents[2] / "cdk.json"

    assert cdk_json == (REPO_ROOT / "cdk.json").resolve()
    assert cdk_json.is_file()
    context = json.loads(cdk_json.read_text(encoding="utf-8"))["context"]
    assert example.get_deployment_config() == (
        context["project_name"],
        context["deployment_regions"]["api_gateway"],
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

MANIFESTS_URL = f"{API_ENDPOINT}/api/v1/manifests"
SECTION_HEADERS = [
    "=== Example 1: Submit a simple Job ===",
    "=== Example 2: Submit a GPU Job (on-demand) ===",
    "=== Example 3: Submit multiple manifests ===",
    "=== Example 4: Dry run validation ===",
    "=== Examples Complete ===",
]


def _acme_deployment(
    monkeypatch: pytest.MonkeyPatch, repo: Path, responses: list[requests.Response]
) -> tuple[_FakeBoto3, _FakeRequests]:
    """A deployed ``acme`` project in ``eu-west-1`` whose API stack exposes ``ApiEndpoint``."""
    _write_cdk_json(repo, ACME_CDK_JSON)
    return _install_fakes(
        monkeypatch, outputs=[UNRELATED_OUTPUT, API_ENDPOINT_OUTPUT], responses=responses
    )


def _outcome_lines(out: str) -> list[str]:
    """The per-example outcome markers ``main`` printed, in order."""
    prefixes = ("Success:", "Dry run result:", "Error:")
    return [line.split(":")[0] for line in out.splitlines() if line.startswith(prefixes)]


def test_main_runs_the_documented_examples_end_to_end(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main`` resolves the stack from ``cdk.json``, signs with the frozen credentials for
    the endpoint host, submits examples 1, 3 and 4 in order with one shared auth object
    (example 2 is print-only), and prints each parsed result."""
    fake_boto3, fake_requests = _acme_deployment(
        monkeypatch,
        repo,
        [
            _json_response(200, {"status": "applied", "applied": 1}),
            _json_response(200, {"status": "applied", "applied": 2}),
            _json_response(200, {"status": "validated", "dry_run": True}),
        ],
    )

    example.main()

    out = capsys.readouterr().out
    # Discovery: region and stack name come from cdk.json.
    assert "Using API Gateway region: eu-west-1" in out
    assert "Getting API Gateway endpoint from stack acme-api-gateway..." in out
    assert f"API Endpoint: {API_ENDPOINT}" in out
    assert fake_boto3.client_calls == [("cloudformation", "eu-west-1")]
    assert fake_boto3.cloudformation.describe_calls == ["acme-api-gateway"]

    # Auth: one signer for the endpoint host, shared by every request.
    assert fake_boto3.sessions_created == 1
    auth = fake_requests.post_calls[0]["auth"]
    assert isinstance(auth, example.AWSRequestsAuth)
    assert all(call["auth"] is auth for call in fake_requests.post_calls)
    assert (auth.aws_host, auth.aws_region, auth.service) == (API_HOST, "eu-west-1", "execute-api")
    assert (auth.aws_access_key, auth.aws_secret_access_key, auth.aws_token) == (
        "test-access-key",
        "test-secret-key",
        "test-session-token",
    )

    # Submissions: examples 1, 3 and 4; example 2 only prints its manifest.
    assert [call["url"] for call in fake_requests.post_calls] == [MANIFESTS_URL] * 3
    assert all(
        call["headers"] == {"Content-Type": "application/json"} and call["timeout"] == 30
        for call in fake_requests.post_calls
    )
    payloads = [call["json"] for call in fake_requests.post_calls]
    assert [[m["kind"] for m in p["manifests"]] for p in payloads] == [
        ["Job"],
        ["ConfigMap", "Job"],
        ["Job"],
    ]
    assert [p["dry_run"] for p in payloads] == [False, False, True]
    assert all("namespace" not in p for p in payloads)
    assert payloads[0]["manifests"][0]["metadata"] == {
        "name": "python-example-job",
        "namespace": "gco-jobs",
    }
    assert payloads[2]["manifests"] == payloads[0]["manifests"]
    config_map, reader_job = payloads[1]["manifests"]
    assert config_map["metadata"]["name"] == "python-example-config"
    reader_spec = reader_job["spec"]["template"]["spec"]
    assert reader_spec["volumes"] == [
        {"name": "config-volume", "configMap": {"name": "python-example-config"}}
    ]
    assert fake_requests.get_calls == []

    # Output: every section in order, the GPU preview, the parsed results, the closing notes.
    assert [line for line in out.splitlines() if line.startswith("=== ")] == SECTION_HEADERS
    assert _outcome_lines(out) == ["Success", "Success", "Dry run result"]
    assert '"applied": 1' in out
    assert '"applied": 2' in out
    assert '"status": "validated"' in out
    assert '"nvidia.com/gpu": "1"' in out
    assert '"karpenter.sh/capacity-type": "on-demand"' in out
    assert "(Not submitting - uncomment to test with GPU nodes)" in out
    assert "Error:" not in out
    assert out.rstrip().endswith(
        "4. Use nodeSelector 'karpenter.sh/capacity-type' to control spot vs on-demand"
    )


def test_main_reports_each_http_failure_and_keeps_going(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A rejected submission prints the HTTP error and the response body instead of aborting;
    the remaining examples and the closing notes still run."""
    _, fake_requests = _acme_deployment(
        monkeypatch, repo, [_json_response(403, {"message": "Forbidden"}) for _ in range(3)]
    )

    example.main()

    out = capsys.readouterr().out
    assert len(fake_requests.post_calls) == 3
    assert [line for line in out.splitlines() if line.startswith("Error: ")] == [
        f"Error: 403 Client Error: Forbidden for url: {MANIFESTS_URL}"
    ] * 3
    assert out.count('Response: {"message": "Forbidden"}') == 3
    assert _outcome_lines(out) == ["Error", "Error", "Error"]
    assert [line for line in out.splitlines() if line.startswith("=== ")] == SECTION_HEADERS


@pytest.mark.parametrize("failing_call", [0, 1, 2], ids=["example-1", "example-3", "example-4"])
def test_main_isolates_one_failed_submission(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing_call: int,
) -> None:
    """One failing example (a 500 here) is reported in place while the other submissions
    still succeed."""
    responses = [_json_response(200, {"status": "applied"}) for _ in range(3)]
    responses[failing_call] = _json_response(500, {"message": "upstream unavailable"})
    _acme_deployment(monkeypatch, repo, responses)

    example.main()

    out = capsys.readouterr().out
    expected = ["Success", "Success", "Dry run result"]
    expected[failing_call] = "Error"
    assert _outcome_lines(out) == expected
    assert out.count("Error: 500 Server Error: Internal Server Error") == 1
    assert out.count('Response: {"message": "upstream unavailable"}') == 1
    assert "=== Examples Complete ===" in out


def test_main_propagates_a_missing_stack_using_stock_config(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without a ``cdk.json`` the stock ``gco-api-gateway`` / ``us-east-2`` are used, and
    CloudFormation's missing-stack error propagates before any auth or submission happens."""
    fake_boto3, fake_requests = _install_fakes(
        monkeypatch, stack_error=_missing_stack_error("gco-api-gateway")
    )

    with pytest.raises(ClientError, match=r"Stack with id gco-api-gateway does not exist"):
        example.main()

    assert capsys.readouterr().out.splitlines() == [
        "Using API Gateway region: us-east-2",
        "Getting API Gateway endpoint from stack gco-api-gateway...",
    ]
    assert fake_boto3.client_calls == [("cloudformation", "us-east-2")]
    assert fake_boto3.sessions_created == 0
    assert fake_requests.post_calls == []


def test_main_propagates_a_missing_api_endpoint_output(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stack without ``ApiEndpoint`` stops ``main`` with the lookup's ``ValueError``; no
    auth is built and nothing is submitted."""
    _write_cdk_json(repo, ACME_CDK_JSON)
    fake_boto3, fake_requests = _install_fakes(monkeypatch, outputs=[UNRELATED_OUTPUT])

    with pytest.raises(ValueError, match=r"ApiEndpoint not found in stack acme-api-gateway"):
        example.main()

    out = capsys.readouterr().out
    assert out.rstrip().endswith("Getting API Gateway endpoint from stack acme-api-gateway...")
    assert "API Endpoint:" not in out
    assert fake_boto3.sessions_created == 0
    assert fake_requests.post_calls == []


def test_main_propagates_missing_credentials(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the endpoint resolved but no credentials, ``main`` stops at the auth step with
    ``RuntimeError``; nothing is submitted."""
    _write_cdk_json(repo, ACME_CDK_JSON)
    fake_boto3, fake_requests = _install_fakes(
        monkeypatch, outputs=[API_ENDPOINT_OUTPUT], credentials=None
    )

    with pytest.raises(RuntimeError, match=r"No AWS credentials are available"):
        example.main()

    out = capsys.readouterr().out
    assert f"API Endpoint: {API_ENDPOINT}" in out
    assert out.rstrip().endswith("Creating AWS SigV4 authentication...")
    assert fake_boto3.sessions_created == 1
    assert fake_requests.post_calls == []
