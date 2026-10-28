"""
Tests for gco/services/slurm_client.py — the slurmrestd client behind /api/v1/slurm.

The client reads the operator-minted JWT from a Kubernetes Secret and talks
to slurmrestd over plain HTTP inside gco-jobs. These tests answer its calls
from an in-memory ``httpx2.MockTransport`` swapped in at the tracing seam
(the same seam the cost proxy's tests use), with the Secret read answered by
a stub ``CoreV1Api``. They prove: the auth headers and versioned paths of
every endpoint, the Secret-backed token cache (re-read after the refresh
window, after ``invalidate()``, and once more on a 401), the Secret failure
modes (missing key, empty token, client error) surfacing as the token 503,
the error decoding (non-JSON and non-object bodies -> 502, Slurm's own
``errors`` list -> the status it implies: 404 for an unknown job, 400 for a
bad request, 502 otherwise), the environment knobs, and the process-wide
client gated on ``GCO_SLURM_ENABLED``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from fastapi import HTTPException

from gco.services import slurm_client, tracing
from gco.services.slurm_client import (
    SecretTokenProvider,
    SlurmApiError,
    SlurmDisabledError,
    SlurmRestClient,
    SlurmUnavailableError,
)

BASE = "http://slinky-slurm-restapi.gco-jobs.svc.cluster.local:6820"
TOKEN = "eyJ.header.payload"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class _Restd:
    """In-memory slurmrestd behind the client's real httpx2 client."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.handler: Callable[[httpx2.Request], httpx2.Response] = lambda _request: (
            httpx2.Response(200, json={"errors": []})
        )

    def wrap(self, transport: httpx2.AsyncBaseTransport) -> httpx2.AsyncBaseTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return self.handler(request)

        return httpx2.MockTransport(handle)

    def respond(self, status_code: int = 200, **kwargs: Any) -> None:
        self.handler = lambda _request: httpx2.Response(status_code, **kwargs)

    def sequence(self, *responses: httpx2.Response) -> None:
        queue = list(responses)

        def handle(_request: httpx2.Request) -> httpx2.Response:
            return queue.pop(0)

        self.handler = handle

    def fail(self, error_type: type[httpx2.TransportError], message: str) -> None:
        def raise_error(request: httpx2.Request) -> httpx2.Response:
            raise error_type(message, request=request)

        self.handler = raise_error


class _StaticTokens(SecretTokenProvider):
    """A provider that never touches Kubernetes; counts reads and invalidations."""

    def __init__(self, token: str = TOKEN) -> None:
        super().__init__()
        self.value = token
        self.reads = 0
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1
        super().invalidate()

    def _read_secret(self) -> str:
        self.reads += 1
        return self.value


@pytest.fixture
def restd(monkeypatch: pytest.MonkeyPatch) -> _Restd:
    double = _Restd()
    monkeypatch.setattr(tracing, "wrap_async_transport", double.wrap)
    for name in ("GCO_SLURM_ENABLED", "SLURM_RESTAPI_URL", "SLURM_API_VERSION"):
        monkeypatch.delenv(name, raising=False)
    return double


@pytest.fixture
def client(restd: _Restd) -> SlurmRestClient:
    return SlurmRestClient(token_provider=_StaticTokens())


# ---------------------------------------------------------------------------
# Environment knobs
# ---------------------------------------------------------------------------


class TestEnvironment:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [("true", True), (" TRUE ", True), ("false", False), ("", False), ("yes", False)],
    )
    def test_slurm_enabled_reads_the_api_pod_flag(self, monkeypatch, value, expected):
        monkeypatch.setenv("GCO_SLURM_ENABLED", value)
        assert slurm_client.slurm_enabled() is expected

    def test_slurm_disabled_when_unset(self, monkeypatch):
        monkeypatch.delenv("GCO_SLURM_ENABLED", raising=False)
        assert slurm_client.slurm_enabled() is False

    def test_defaults_name_the_chart_service_and_the_pinned_api_version(self, restd):
        assert slurm_client.restapi_base_url() == BASE
        assert slurm_client.api_version() == slurm_client.SLURM_API_VERSION

    def test_overrides_are_honoured_and_a_blank_version_falls_back(self, monkeypatch):
        monkeypatch.setenv("SLURM_RESTAPI_URL", "http://localhost:6820/")
        monkeypatch.setenv("SLURM_API_VERSION", "   ")
        assert slurm_client.restapi_base_url() == "http://localhost:6820"
        assert slurm_client.api_version() == slurm_client.SLURM_API_VERSION
        monkeypatch.setenv("SLURM_API_VERSION", " v0.0.45 ")
        assert slurm_client.api_version() == "v0.0.45"


# ---------------------------------------------------------------------------
# Token provider
# ---------------------------------------------------------------------------


def _secret(data: dict[str, str] | None) -> SimpleNamespace:
    return SimpleNamespace(data=data)


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


class _CoreV1Stub:
    calls: list[tuple[str, str]] = []
    secret: Any = None
    error: Exception | None = None

    def read_namespaced_secret(self, name: str, namespace: str) -> Any:
        type(self).calls.append((name, namespace))
        if type(self).error is not None:
            raise type(self).error
        return type(self).secret


@pytest.fixture
def core_v1(monkeypatch: pytest.MonkeyPatch) -> type[_CoreV1Stub]:
    import kubernetes.client

    _CoreV1Stub.calls = []
    _CoreV1Stub.secret = _secret({"auth-token": _encoded(TOKEN)})
    _CoreV1Stub.error = None
    monkeypatch.setattr(kubernetes.client, "CoreV1Api", _CoreV1Stub)
    return _CoreV1Stub


class TestSecretTokenProvider:
    def test_reads_the_named_key_from_the_named_secret_once(self, core_v1):
        provider = SecretTokenProvider()

        assert _run(provider.token()) == TOKEN
        assert _run(provider.token()) == TOKEN
        assert core_v1.calls == [("slinky-slurm-token-gco-api", "gco-jobs")]

    def test_rereads_after_the_refresh_window(self, core_v1, monkeypatch):
        provider = SecretTokenProvider(refresh_seconds=10.0)
        _run(provider.token())
        monkeypatch.setattr(slurm_client.time, "monotonic", lambda: provider._read_at + 11.0)

        assert _run(provider.token()) == TOKEN
        assert len(core_v1.calls) == 2

    def test_invalidate_forces_a_reread(self, core_v1):
        provider = SecretTokenProvider()
        _run(provider.token())
        provider.invalidate()

        _run(provider.token())
        assert len(core_v1.calls) == 2

    def test_strips_whitespace_around_the_token(self, core_v1):
        core_v1.secret = _secret({"auth-token": _encoded(f"  {TOKEN}\n")})
        assert _run(SecretTokenProvider().token()) == TOKEN

    @pytest.mark.parametrize(
        ("data", "fragment"),
        [
            (None, "has no 'auth-token' key yet"),
            ({"other": _encoded("x")}, "has no 'auth-token' key yet"),
            ({"auth-token": _encoded("   ")}, "holds an empty token"),
        ],
    )
    def test_missing_or_empty_token_is_unavailable(self, core_v1, data, fragment):
        core_v1.secret = _secret(data)

        with pytest.raises(SlurmUnavailableError, match=fragment):
            _run(SecretTokenProvider().token())

    def test_kubernetes_errors_are_wrapped_as_unavailable(self, core_v1):
        core_v1.error = RuntimeError("forbidden")

        with pytest.raises(SlurmUnavailableError, match="could not read Secret gco-jobs/slinky"):
            _run(SecretTokenProvider().token())

    def test_custom_coordinates(self, core_v1):
        provider = SecretTokenProvider(namespace="ns", name="s", key="k")
        core_v1.secret = _secret({"k": _encoded("t")})

        assert _run(provider.token()) == "t"
        assert core_v1.calls == [("s", "ns")]


# ---------------------------------------------------------------------------
# Transport, headers and endpoints
# ---------------------------------------------------------------------------


class TestRequests:
    def test_sends_slurm_auth_headers_without_env_trust(self, client, restd):
        restd.respond(200, json={"pings": [], "errors": []})

        payload = _run(client.ping())

        assert payload == {"pings": [], "errors": []}
        [request] = restd.requests
        assert request.method == "GET"
        assert str(request.url) == f"{BASE}/slurm/v0.0.44/ping"
        assert request.headers["X-SLURM-USER-NAME"] == "slurm"
        assert request.headers["X-SLURM-USER-TOKEN"] == TOKEN
        assert request.extensions["timeout"] == {
            "connect": 30.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }

    @pytest.mark.parametrize(
        ("call", "method", "path"),
        [
            (lambda c: c.ping(), "GET", "/slurm/v0.0.44/ping"),
            (lambda c: c.diag(), "GET", "/slurm/v0.0.44/diag"),
            (lambda c: c.partitions(), "GET", "/slurm/v0.0.44/partitions"),
            (lambda c: c.nodes(), "GET", "/slurm/v0.0.44/nodes"),
            (lambda c: c.jobs(), "GET", "/slurm/v0.0.44/jobs"),
            (lambda c: c.job(42), "GET", "/slurm/v0.0.44/job/42"),
            (lambda c: c.cancel(42), "DELETE", "/slurm/v0.0.44/job/42"),
            (
                lambda c: c.cancel(42, signal="SIGINT"),
                "DELETE",
                "/slurm/v0.0.44/job/42?signal=SIGINT",
            ),
            (
                lambda c: c.accounting_jobs({"users": "alice"}),
                "GET",
                "/slurmdb/v0.0.44/jobs?users=alice",
            ),
            (lambda c: c.accounting_job(42), "GET", "/slurmdb/v0.0.44/job/42"),
        ],
    )
    def test_endpoint_paths(self, client, restd, call, method, path):
        restd.respond(200, json={"errors": []})

        _run(call(client))

        [request] = restd.requests
        assert request.method == method
        assert str(request.url) == f"{BASE}{path}"

    def test_submit_posts_the_job_and_script(self, client, restd):
        restd.respond(200, json={"job_id": 7, "errors": []})

        payload = _run(client.submit({"name": "n", "tasks": 1}, "#!/bin/bash\nhostname\n"))

        assert payload["job_id"] == 7
        [request] = restd.requests
        assert request.method == "POST"
        assert str(request.url) == f"{BASE}/slurm/v0.0.44/job/submit"
        assert json.loads(request.content) == {
            "job": {"name": "n", "tasks": 1},
            "script": "#!/bin/bash\nhostname\n",
        }

    def test_version_and_base_url_overrides(self, restd):
        client = SlurmRestClient(
            base_url="http://restd:1/", version="v0.0.45", token_provider=_StaticTokens()
        )
        restd.respond(200, json={})

        _run(client.diag())

        assert str(restd.requests[0].url) == "http://restd:1/slurm/v0.0.45/diag"

    def test_transport_errors_are_unavailable(self, client, restd):
        restd.fail(httpx2.ConnectError, "refused")

        with pytest.raises(SlurmUnavailableError, match="slurmrestd unreachable"):
            _run(client.ping())

    def test_a_401_invalidates_the_token_and_retries_once(self, restd):
        tokens = _StaticTokens()
        client = SlurmRestClient(token_provider=tokens)
        restd.sequence(
            httpx2.Response(401, json={"errors": [{"description": "expired"}]}),
            httpx2.Response(200, json={"pings": [], "errors": []}),
        )

        assert _run(client.ping()) == {"pings": [], "errors": []}
        assert len(restd.requests) == 2
        assert tokens.invalidations == 1
        assert tokens.reads == 2

    def test_a_second_401_is_reported_not_looped(self, restd):
        client = SlurmRestClient(token_provider=_StaticTokens())
        restd.respond(401, json={"errors": [{"description": "Authentication failure"}]})

        with pytest.raises(SlurmApiError) as excinfo:
            _run(client.ping())

        assert len(restd.requests) == 2
        assert excinfo.value.status_code == 502
        assert excinfo.value.detail == "Authentication failure"

    def test_token_failures_surface_before_any_request(self, restd):
        class Broken(SecretTokenProvider):
            def _read_secret(self) -> str:
                raise SlurmUnavailableError("Secret gco-jobs/x has no 'auth-token' key yet")

        with pytest.raises(SlurmUnavailableError, match="Secret"):
            _run(SlurmRestClient(token_provider=Broken()).ping())
        assert restd.requests == []


class TestDecoding:
    def test_non_json_body_is_502(self, client, restd):
        restd.respond(200, content=b"<html>")

        with pytest.raises(SlurmApiError) as excinfo:
            _run(client.ping())

        assert excinfo.value.status_code == 502
        assert "non-JSON body (HTTP 200)" in excinfo.value.detail

    def test_non_object_body_is_502(self, client, restd):
        restd.respond(200, json=[1, 2])

        with pytest.raises(SlurmApiError, match="unexpected JSON shape") as excinfo:
            _run(client.ping())

        assert excinfo.value.status_code == 502

    def test_errors_in_a_200_body_are_still_errors(self, client, restd):
        restd.respond(
            200,
            json={
                "errors": [
                    {
                        "description": "Invalid job id specified",
                        "error_number": 2017,
                        "source": "slurm_load_job",
                    }
                ]
            },
        )

        with pytest.raises(SlurmApiError) as excinfo:
            _run(client.job(99))

        assert excinfo.value.status_code == 404
        assert excinfo.value.detail == "Invalid job id specified (slurm_load_job) [rc 2017]"
        assert excinfo.value.errors[0]["error_number"] == 2017

    def test_missing_errors_key_is_a_clean_payload(self, client, restd):
        restd.respond(200, json={"jobs": []})
        assert _run(client.jobs()) == {"jobs": []}


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("http_status", "errors", "expected"),
        [
            (500, [{"error_number": 2017}], 404),
            (404, [], 404),
            (400, [{"description": "bad"}], 400),
            (500, [{"description": "Invalid partition name specified"}], 400),
            (500, [{"description": "Requested partition is unavailable"}], 400),
            (500, [{"description": "Zero Bytes were transmitted or received"}], 502),
            (500, ["not-a-dict"], 502),
            (500, [{"error_number": "2017"}], 502),
        ],
    )
    def test_map_slurm_status(self, http_status, errors, expected):
        assert slurm_client._map_slurm_status(http_status, errors) == expected

    def test_describe_errors_joins_and_skips_junk(self):
        errors = [
            "junk",
            {"error": "fallback text", "error_number": 1},
            {"description": "  "},
            {"description": "with source", "source": "where"},
        ]
        assert slurm_client._describe_errors(errors) == (
            "fallback text [rc 1]; Slurm returned an error; with source (where)"
        )
        assert slurm_client._describe_errors([]) == "Slurm returned an error"

    def test_http_exception_for_each_error_class(self):
        disabled = slurm_client.http_exception_for(SlurmDisabledError("off"))
        assert (disabled.status_code, disabled.detail) == (404, slurm_client.DISABLED_DETAIL)

        unreachable = slurm_client.http_exception_for(
            SlurmUnavailableError("slurmrestd unreachable")
        )
        assert (unreachable.status_code, unreachable.detail) == (
            503,
            slurm_client.UNAVAILABLE_DETAIL,
        )

        token = slurm_client.http_exception_for(SlurmUnavailableError("Secret gco-jobs/x missing"))
        assert (token.status_code, token.detail) == (503, slurm_client.TOKEN_UNAVAILABLE_DETAIL)

        slurm = slurm_client.http_exception_for(SlurmApiError(400, [], "bad partition"))
        assert (slurm.status_code, slurm.detail) == (400, "bad partition")

        other = slurm_client.http_exception_for(ValueError("boom"), context="listing nodes")
        assert isinstance(other, HTTPException)
        assert (other.status_code, other.detail) == (500, "Internal error while listing nodes")
        assert "boom" not in other.detail


class TestProcessWideClient:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        slurm_client.reset_client()
        yield
        slurm_client.reset_client()

    def test_disabled_raises_before_building_anything(self, monkeypatch):
        monkeypatch.setenv("GCO_SLURM_ENABLED", "false")
        with pytest.raises(SlurmDisabledError):
            slurm_client.get_client()

    def test_enabled_builds_once_and_caches(self, monkeypatch):
        monkeypatch.setenv("GCO_SLURM_ENABLED", "true")
        monkeypatch.setenv("SLURM_RESTAPI_URL", "http://restd:6820")

        first = slurm_client.get_client()
        second = slurm_client.get_client()

        assert first is second
        assert first.base_url == "http://restd:6820"
        assert first.user == "slurm"
        assert isinstance(first.token_provider, SecretTokenProvider)

        slurm_client.reset_client()
        assert slurm_client.get_client() is not first
