"""Tests for the Slurm backend seam: ``slurm_backends``, backend resolution and ``/backends``.

The API can front two Slurm clusters, the Slinky one on the EKS cluster and an
AWS PCS one in the VPC. These tests pin the rules that decide which one a
request means (``GCO_SLURM_BACKENDS`` plus the ``backend`` query parameter),
that every route echoes the backend it served, that the per-backend client
cache behaves, that ``SlinkyBackend`` preserves the pre-PCS headers and
defaults, and that ``GET /api/v1/slurm/backends`` lists what is enabled with
one broken backend never hiding the other.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gco.services import slurm_backends, slurm_client
from gco.services.api_routes.slurm import router
from gco.services.slurm_backends import BackendDefaults, SlinkyBackend, build_backend
from gco.services.slurm_client import (
    SlurmBackendSelectionError,
    SlurmDisabledError,
    SlurmRestClient,
    SlurmUnavailableError,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "GCO_SLURM_BACKENDS",
        "GCO_SLURM_ENABLED",
        "SLURM_RESTAPI_URL",
        "GCO_PCS_CLUSTER_ID",
        "GCO_PCS_IDENTITY",
        "GCO_PCS_DEFAULT_PARTITION",
        "GCO_PCS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    slurm_backends.reset_client()
    yield
    slurm_backends.reset_client()


# ---------------------------------------------------------------------------
# Enablement and resolution
# ---------------------------------------------------------------------------


class TestEnabledBackends:
    def test_nothing_set_means_no_backend(self) -> None:
        assert slurm_client.enabled_backends() == []
        assert slurm_client.slurm_enabled() is False

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("slinky", ["slinky"]),
            ("pcs", ["pcs"]),
            ("slinky,pcs", ["slinky", "pcs"]),
            ("pcs, slinky", ["slinky", "pcs"]),
            (" PCS ", ["pcs"]),
            ("", []),
            (",", []),
        ],
    )
    def test_the_list_is_parsed_in_canonical_order(self, monkeypatch, raw, expected) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", raw)
        assert slurm_client.enabled_backends() == expected
        assert slurm_client.slurm_enabled() is bool(expected)

    def test_unknown_names_are_dropped_with_a_warning(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky,moab")
        with caplog.at_level("WARNING"):
            assert slurm_client.enabled_backends() == ["slinky"]
        assert "unknown Slurm backend(s): moab" in caplog.text

    def test_the_legacy_boolean_still_means_slinky(self, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_ENABLED", "true")
        assert slurm_client.enabled_backends() == ["slinky"]
        monkeypatch.setenv("GCO_SLURM_ENABLED", "false")
        assert slurm_client.enabled_backends() == []

    def test_the_list_wins_over_the_legacy_boolean(self, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_ENABLED", "true")
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "")
        assert slurm_client.enabled_backends() == []


class TestResolveBackend:
    def test_nothing_enabled_is_disabled_with_the_generic_remedy(self) -> None:
        with pytest.raises(SlurmDisabledError) as excinfo:
            slurm_client.resolve_backend(None)
        assert excinfo.value.detail is None
        http = slurm_client.http_exception_for(excinfo.value)
        assert (http.status_code, http.detail) == (404, slurm_client.DISABLED_DETAIL)
        assert "helm.slurm.enabled" in http.detail
        assert "pcs.enabled" in http.detail

    def test_one_enabled_is_the_default(self, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "pcs")
        assert slurm_client.resolve_backend(None) == "pcs"
        assert slurm_client.resolve_backend("pcs") == "pcs"
        assert slurm_client.resolve_backend(" PCS ") == "pcs"

    def test_both_enabled_requires_a_choice(self, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky,pcs")
        with pytest.raises(SlurmBackendSelectionError) as excinfo:
            slurm_client.resolve_backend(None)
        http = slurm_client.http_exception_for(excinfo.value)
        assert http.status_code == 400
        assert "backend=slinky or backend=pcs" in http.detail
        assert "/api/v1/slurm/backends" in http.detail
        assert slurm_client.resolve_backend("slinky") == "slinky"
        assert slurm_client.resolve_backend("pcs") == "pcs"

    def test_naming_a_disabled_backend_is_404_with_its_own_remedy(self, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky")
        with pytest.raises(SlurmDisabledError) as excinfo:
            slurm_client.resolve_backend("pcs")
        http = slurm_client.http_exception_for(excinfo.value)
        assert (http.status_code, http.detail) == (
            404,
            slurm_client.BACKEND_DISABLED_DETAILS["pcs"],
        )
        assert "pcs.enabled" in http.detail
        assert "helm.slurm" not in http.detail

    def test_naming_an_unknown_backend_is_400(self, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky")
        with pytest.raises(SlurmBackendSelectionError) as excinfo:
            slurm_client.resolve_backend("moab")
        http = slurm_client.http_exception_for(excinfo.value)
        assert http.status_code == 400
        assert "'moab'" in http.detail
        assert "slinky, pcs" in http.detail


class TestClientCache:
    def test_one_client_per_backend(self, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky,pcs")
        monkeypatch.setenv("GCO_PCS_CLUSTER_ID", "pcs_abc")
        monkeypatch.setenv("GCO_PCS_IDENTITY", '{"name": "ec2-user", "uid": 1000, "gid": 1000}')

        slinky = slurm_backends.get_client("slinky")
        pcs = slurm_backends.get_client("pcs")

        assert slinky is slurm_backends.get_client("slinky")
        assert pcs is slurm_backends.get_client("pcs")
        assert slinky is not pcs
        assert slinky.backend.name == "slinky"
        assert pcs.backend.name == "pcs"
        slurm_backends.reset_client()
        assert slurm_backends.get_client("slinky") is not slinky

    def test_get_client_without_a_name_follows_the_resolution_rules(self, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky,pcs")
        with pytest.raises(SlurmBackendSelectionError):
            slurm_backends.get_client()
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky")
        assert slurm_backends.get_client().backend.name == "slinky"

    def test_build_backend_rejects_unknown_names(self) -> None:
        with pytest.raises(ValueError, match="unknown Slurm backend 'moab'"):
            build_backend("moab")

    def test_legacy_disabled_flag_raises_before_building_anything(self, monkeypatch) -> None:
        monkeypatch.delenv("GCO_SLURM_BACKENDS", raising=False)
        monkeypatch.setenv("GCO_SLURM_ENABLED", "false")
        with pytest.raises(SlurmDisabledError):
            slurm_backends.get_client()

    def test_legacy_enabled_flag_builds_the_slinky_client_once(self, monkeypatch) -> None:
        """``GCO_SLURM_ENABLED=true`` (pre-PCS pods) still means one Slinky client."""
        monkeypatch.delenv("GCO_SLURM_BACKENDS", raising=False)
        monkeypatch.setenv("GCO_SLURM_ENABLED", "true")
        monkeypatch.setenv("SLURM_RESTAPI_URL", "http://restd:6820")

        first = slurm_backends.get_client()
        second = slurm_backends.get_client()

        assert first is second
        assert first.base_url == "http://restd:6820"
        assert isinstance(first.backend, SlinkyBackend)
        assert first.backend.user == "slurm"
        assert isinstance(first.backend.token_provider, slurm_client.SecretTokenProvider)

        slurm_backends.reset_client()
        assert slurm_backends.get_client() is not first


# ---------------------------------------------------------------------------
# SlinkyBackend
# ---------------------------------------------------------------------------


class _Tokens(slurm_client.SecretTokenProvider):
    def __init__(self) -> None:
        super().__init__()
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1
        super().invalidate()

    def _read_secret(self) -> str:
        return "tok"


class TestSlinkyBackend:
    def test_defaults_and_headers(self, monkeypatch) -> None:
        monkeypatch.setenv("SLURM_RESTAPI_URL", "http://restd:6820/")
        backend = SlinkyBackend(token_provider=_Tokens())

        assert backend.name == "slinky"
        assert _run(backend.base_url()) == "http://restd:6820"
        assert backend.endpoint_hint() == "http://restd:6820"
        assert _run(backend.auth_headers()) == {
            "X-SLURM-USER-NAME": "slurm",
            "X-SLURM-USER-TOKEN": "tok",
        }
        assert backend.defaults == BackendDefaults(partition=None, working_directory="/home/slurm")
        assert backend.unavailable_detail() == slurm_client.UNAVAILABLE_DETAIL
        assert backend.api_version_detail("v0.0.44") == slurm_client.API_VERSION_DETAIL.format(
            version="v0.0.44"
        )

    def test_invalidate_forwards_and_transport_errors_are_inert(self) -> None:
        tokens = _Tokens()
        backend = SlinkyBackend(base_url="http://x", token_provider=tokens)
        backend.invalidate()
        backend.on_transport_error()
        assert tokens.invalidations == 1

    def test_describe_names_the_chart_objects(self) -> None:
        facts = _run(SlinkyBackend(base_url="http://x", token_provider=_Tokens()).describe())
        assert facts["name"] == "slinky"
        assert facts["kind"] == "slinky"
        assert facts["endpoint"] == "http://x"
        assert facts["restapi_service"] == "slinky-slurm-restapi.gco-jobs"
        assert facts["login"] == {
            "kind": "pod",
            "namespace": "gco-jobs",
            "deployment": "slinky-slurm-login-gco",
        }
        assert facts["working_directory"] == "/home/slurm"

    def test_client_without_a_backend_builds_a_slinky_one(self) -> None:
        client = SlurmRestClient(base_url="http://x/", token_provider=_Tokens(), user="u")
        assert isinstance(client.backend, SlinkyBackend)
        assert client.backend.user == "u"
        assert client.base_url == "http://x"


# ---------------------------------------------------------------------------
# The /backends route and the backend echo
# ---------------------------------------------------------------------------


class _Backend:
    def __init__(self, name: str, *, error: Exception | None = None) -> None:
        self.name = name
        self.defaults = BackendDefaults(partition=None, working_directory=f"/home/{name}")
        self.error = error

    async def describe(self) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        return {"name": self.name, "kind": self.name, "endpoint": f"http://{self.name}"}


class _Client:
    version = "v0.0.44"

    def __init__(self, name: str, *, describe_error: Exception | None = None) -> None:
        self.backend = _Backend(name, error=describe_error)
        self.submitted: list[dict[str, Any]] = []

    async def ping(self) -> dict[str, Any]:
        return {"pings": [{"hostname": self.backend.name, "responding": True}]}

    async def partitions(self) -> dict[str, Any]:
        return {"partitions": [{"name": "p", "partition": {"state": ["UP"]}}]}

    async def nodes(self) -> dict[str, Any]:
        return {"nodes": []}

    async def jobs(self) -> dict[str, Any]:
        return {"jobs": []}

    async def job(self, job_id: int) -> dict[str, Any]:
        return {"jobs": [{"job_id": job_id, "name": "j"}]}

    async def submit(self, job: dict[str, Any], script: str) -> dict[str, Any]:
        self.submitted.append(dict(job))
        return {"job_id": 7}

    async def cancel(self, job_id: int, *, signal: str | None = None) -> dict[str, Any]:
        return {}

    async def accounting_jobs(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"jobs": []}

    async def accounting_job(self, job_id: int) -> dict[str, Any]:
        return {"jobs": []}


@pytest.fixture
def api() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def two_backends(monkeypatch: pytest.MonkeyPatch) -> dict[str, _Client]:
    monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky,pcs")
    clients = {"slinky": _Client("slinky"), "pcs": _Client("pcs")}

    def get_client(backend: str | None = None) -> _Client:
        return clients[slurm_client.resolve_backend(backend)]

    monkeypatch.setattr(slurm_backends, "get_client", get_client)
    return clients


SCRIPT = "#!/bin/bash\nhostname\n"
ROUTES = [
    ("GET", "/api/v1/slurm/status", None),
    ("GET", "/api/v1/slurm/partitions", None),
    ("GET", "/api/v1/slurm/nodes", None),
    ("GET", "/api/v1/slurm/jobs", None),
    ("POST", "/api/v1/slurm/jobs", {"script": SCRIPT}),
    ("GET", "/api/v1/slurm/jobs/11", None),
    ("DELETE", "/api/v1/slurm/jobs/11", None),
    ("GET", "/api/v1/slurm/accounting/jobs", None),
]


class TestBackendsRoute:
    def test_nothing_enabled_is_an_empty_list_not_a_404(self, api) -> None:
        result = api.get("/api/v1/slurm/backends")
        assert result.status_code == 200
        body = result.json()
        assert body["backends"] == []
        assert body["default"] is None
        assert body["selection_required"] is False

    def test_one_backend_is_the_default(self, api, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky")
        monkeypatch.setattr(slurm_backends, "get_client", lambda name=None: _Client("slinky"))
        body = api.get("/api/v1/slurm/backends").json()
        assert [b["name"] for b in body["backends"]] == ["slinky"]
        assert body["default"] == "slinky"
        assert body["selection_required"] is False

    def test_two_backends_require_selection(self, api, two_backends) -> None:
        body = api.get("/api/v1/slurm/backends").json()
        assert [b["name"] for b in body["backends"]] == ["slinky", "pcs"]
        assert body["backends"][1]["endpoint"] == "http://pcs"
        assert body["default"] is None
        assert body["selection_required"] is True

    def test_a_broken_backend_is_reported_beside_the_working_one(
        self, api, monkeypatch, caplog
    ) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky,pcs")
        clients = {
            "slinky": _Client("slinky"),
            "pcs": _Client("pcs", describe_error=SlurmUnavailableError("boom", detail="fix pcs")),
        }
        monkeypatch.setattr(slurm_backends, "get_client", lambda name=None: clients[name])
        with caplog.at_level("WARNING"):
            body = api.get("/api/v1/slurm/backends").json()
        assert body["backends"][0]["name"] == "slinky"
        assert body["backends"][1] == {"name": "pcs", "error": "fix pcs"}
        assert "describing backend pcs failed (HTTP 503)" in caplog.text

    def test_a_backend_whose_client_cannot_be_built_is_reported_too(self, api, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "pcs")

        def get_client(name: str | None = None) -> _Client:
            raise SlurmUnavailableError("GCO_PCS_CLUSTER_ID is not set", detail="set it")

        monkeypatch.setattr(slurm_backends, "get_client", get_client)
        body = api.get("/api/v1/slurm/backends").json()
        assert body["backends"] == [{"name": "pcs", "error": "set it"}]


class TestBackendSelectionOnEveryRoute:
    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_both_enabled_without_a_choice_is_400(self, api, two_backends, method, path, body):
        result = api.request(method, path, json=body)
        assert result.status_code == 400
        assert "backend=slinky or backend=pcs" in result.json()["detail"]

    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    @pytest.mark.parametrize("name", ["slinky", "pcs"])
    def test_the_chosen_backend_is_echoed(self, api, two_backends, method, path, body, name):
        result = api.request(method, path, params={"backend": name}, json=body)
        assert result.status_code in (200, 201), result.text
        assert result.json()["backend"] == name

    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_a_disabled_backend_is_404_with_its_remedy(self, api, monkeypatch, method, path, body):
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "slinky")
        result = api.request(method, path, params={"backend": "pcs"}, json=body)
        assert result.status_code == 404
        assert result.json()["detail"] == slurm_client.BACKEND_DISABLED_DETAILS["pcs"]

    @pytest.mark.parametrize(("method", "path", "body"), ROUTES)
    def test_an_unknown_backend_is_400(self, api, two_backends, method, path, body):
        result = api.request(method, path, params={"backend": "moab"}, json=body)
        assert result.status_code == 400
        assert "'moab'" in result.json()["detail"]

    def test_status_carries_the_backend_facts(self, api, two_backends) -> None:
        body = api.get("/api/v1/slurm/status", params={"backend": "pcs"}).json()
        assert body["backend"] == "pcs"
        assert body["cluster"] == {"name": "pcs", "kind": "pcs", "endpoint": "http://pcs"}

    def test_status_reports_a_describe_failure(self, api, monkeypatch) -> None:
        monkeypatch.setenv("GCO_SLURM_BACKENDS", "pcs")
        broken = _Client("pcs", describe_error=SlurmUnavailableError("x", detail="fix pcs"))
        monkeypatch.setattr(slurm_backends, "get_client", lambda name=None: broken)
        result = api.get("/api/v1/slurm/status")
        assert result.status_code == 503
        assert result.json()["detail"] == "fix pcs"


class TestSubmissionDefaultsFollowTheBackend:
    def test_working_directory_and_partition_default_per_backend(self, api, two_backends):
        two_backends["pcs"].backend.defaults = BackendDefaults(
            partition="gco", working_directory="/home/ec2-user"
        )

        result = api.post("/api/v1/slurm/jobs", params={"backend": "pcs"}, json={"script": SCRIPT})

        assert result.status_code == 201
        body = result.json()
        assert body["partition"] == "gco"
        assert body["working_directory"] == "/home/ec2-user"
        [job] = two_backends["pcs"].submitted
        assert job["partition"] == "gco"
        assert job["current_working_directory"] == "/home/ec2-user"

    def test_slinky_keeps_no_partition_and_the_slurm_home(self, api, two_backends):
        result = api.post(
            "/api/v1/slurm/jobs", params={"backend": "slinky"}, json={"script": SCRIPT}
        )
        body = result.json()
        assert body["partition"] is None
        assert body["working_directory"] == "/home/slinky"
        [job] = two_backends["slinky"].submitted
        assert "partition" not in job
        assert job["current_working_directory"] == "/home/slinky"

    def test_explicit_values_win(self, api, two_backends):
        two_backends["pcs"].backend.defaults = BackendDefaults(
            partition="gco", working_directory="/home/ec2-user"
        )
        result = api.post(
            "/api/v1/slurm/jobs",
            params={"backend": "pcs"},
            json={"script": SCRIPT, "partition": "gpu", "working_directory": "/scratch/run"},
        )
        body = result.json()
        assert (body["partition"], body["working_directory"]) == ("gpu", "/scratch/run")

    def test_a_relative_working_directory_is_rejected(self, api, two_backends):
        result = api.post(
            "/api/v1/slurm/jobs",
            params={"backend": "pcs"},
            json={"script": SCRIPT, "working_directory": "relative"},
        )
        assert result.status_code == 422
