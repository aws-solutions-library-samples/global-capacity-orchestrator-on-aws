"""Tests for gco/services/pcs_backend.py — the AWS PCS Slurm backend.

The backend discovers slurmrestd through ``pcs:GetCluster``, reads the JWT
signing key PCS keeps in Secrets Manager and mints HS256 tokens for the
configured POSIX identity. These tests use hand-written client doubles (the
convention in ``test_spot_price_gate.py``) and a fake clock. They pin: the
token shape against PyJWT's decoder and PCS's documented claims; the refresh
at 80% of the lifetime; invalidation after a 401 and re-discovery after a
transport error; key-version rotation; every error mapping (cluster not
ACTIVE, REST mode NONE, Slurm too old, no endpoint, no key, IAM denied) with
its remedy; the environment constructor; and that no key or token reaches the
log.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import jwt
import pytest

from gco.services import pcs_backend as pb
from gco.services import slurm_client
from gco.services.pcs_backend import PcsBackend, PcsClusterFacts, mint_hs256_jwt
from gco.services.slurm_client import SlurmRestClient, SlurmUnavailableError

KEY = b"0123456789abcdef0123456789abcdef"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:pcs-jwt-AbCdEf"
IDENTITY = {
    "name": "ec2-user",
    "uid": 1000,
    "gid": 1000,
    "gids": [1000, 2000],
    "dir": "/home/ec2-user",
    "shell": "/bin/bash",
    "gecos": None,
}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _decode(token: str, key: bytes = KEY) -> dict[str, Any]:
    """PyJWT verifies the signature and the claim shape; ``exp`` is checked
    against the fake clock's epoch by the tests themselves, not the real one."""
    return jwt.decode(token, key, algorithms=["HS256"], options={"verify_exp": False})


def _cluster(**overrides: Any) -> dict[str, Any]:
    cluster: dict[str, Any] = {
        "id": "pcs_abc123",
        "name": "gco-pcs",
        "arn": "arn:aws:pcs:us-east-1:123456789012:cluster/pcs_abc123",
        "status": "ACTIVE",
        "size": "SMALL",
        "scheduler": {"type": "SLURM", "version": "25.11"},
        "slurmConfiguration": {
            "slurmRest": {"mode": "STANDARD"},
            "accounting": {"mode": "STANDARD", "defaultPurgeTimeInDays": 30},
            "jwtAuth": {"jwtKey": {"secretArn": SECRET_ARN, "secretVersion": "v1"}},
        },
        "endpoints": [
            {"type": "SLURMCTLD", "privateIpAddress": "10.0.1.5", "port": "6817"},
            {"type": "SLURMDBD", "privateIpAddress": "10.0.1.5", "port": "6819"},
            {"type": "SLURMRESTD", "privateIpAddress": "10.0.1.5", "port": "6820"},
        ],
    }
    cluster.update(overrides)
    return cluster


class _Pcs:
    def __init__(self, cluster: dict[str, Any] | None = None) -> None:
        self.cluster = cluster if cluster is not None else _cluster()
        self.calls = 0
        self.error: Exception | None = None

    def get_cluster(self, *, clusterIdentifier: str) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert clusterIdentifier == "pcs_abc123"
        return {"cluster": self.cluster}


class _Secrets:
    def __init__(self, key: bytes = KEY) -> None:
        self.response: dict[str, Any] = {"SecretString": base64.b64encode(key).decode()}
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class _Clock:
    def __init__(self) -> None:
        self.monotonic = 1000.0
        self.wall = 1_700_000_000.0

    def tick(self, seconds: float) -> None:
        self.monotonic += seconds
        self.wall += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _backend(
    clock: _Clock,
    *,
    pcs: _Pcs | None = None,
    secrets: _Secrets | None = None,
    **kwargs: Any,
) -> tuple[PcsBackend, _Pcs, _Secrets]:
    pcs = pcs if pcs is not None else _Pcs()
    secrets = secrets if secrets is not None else _Secrets()
    backend = PcsBackend(
        "pcs_abc123",
        IDENTITY,
        region="us-east-1",
        pcs_client=pcs,
        secrets_client=secrets,
        clock=lambda: clock.monotonic,
        now=lambda: clock.wall,
        **kwargs,
    )
    return backend, pcs, secrets


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


class TestMintHs256Jwt:
    def test_pyjwt_decodes_what_we_mint(self) -> None:
        claims = {"exp": 2_000_000_000, "iat": 1_700_000_000, "uid": 1000, "gid": 1000}
        token = mint_hs256_jwt(claims, KEY)
        header, _payload, _signature = token.split(".")
        assert json.loads(base64.urlsafe_b64decode(header + "==")) == {
            "alg": "HS256",
            "typ": "JWT",
        }
        assert jwt.decode(token, KEY, algorithms=["HS256"]) == claims

    def test_the_signature_binds_the_key(self) -> None:
        token = mint_hs256_jwt({"exp": 2_000_000_000, "uid": 1}, KEY)
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(token, b"another-key-another-key-another-k", algorithms=["HS256"])

    def test_no_padding_and_sorted_keys(self) -> None:
        token = mint_hs256_jwt({"b": 1, "a": 2}, KEY)
        assert "=" not in token
        _header, payload, _signature = token.split(".")
        assert base64.urlsafe_b64decode(payload + "==") == b'{"a":2,"b":1}'


# ---------------------------------------------------------------------------
# Cluster facts
# ---------------------------------------------------------------------------


class TestPcsClusterFacts:
    def test_reads_every_field_the_backend_uses(self) -> None:
        facts = PcsClusterFacts(_cluster())
        assert facts.id == "pcs_abc123"
        assert facts.name == "gco-pcs"
        assert facts.status == "ACTIVE"
        assert facts.scheduler_version == "25.11"
        assert facts.size == "SMALL"
        assert facts.rest_mode == "STANDARD"
        assert facts.accounting_mode == "STANDARD"
        assert facts.jwt_secret_arn == SECRET_ARN
        assert facts.jwt_secret_version == "v1"
        assert facts.restapi_url() == "http://10.0.1.5:6820"
        assert facts.controller_address() == "10.0.1.5:6817"
        assert facts.summary()["endpoint"] == "http://10.0.1.5:6820"

    def test_sparse_payload_reads_as_unknown(self) -> None:
        facts = PcsClusterFacts({})
        assert facts.status == "UNKNOWN"
        assert facts.rest_mode == "NONE"
        assert facts.accounting_mode == "NONE"
        assert facts.jwt_secret_arn == ""
        assert facts.restapi_url() is None
        assert facts.controller_address() is None
        assert facts.summary()["controller"] is None

    def test_ipv6_endpoint_and_default_port(self) -> None:
        facts = PcsClusterFacts(
            _cluster(
                endpoints=[{"type": "SLURMRESTD", "privateIpAddress": "", "ipv6Address": "fd00::5"}]
            )
        )
        assert facts.restapi_url() == "http://[fd00::5]:6820"

    def test_endpoints_without_an_address_are_skipped(self) -> None:
        facts = PcsClusterFacts(
            _cluster(
                endpoints=[
                    {"type": "SLURMRESTD", "privateIpAddress": ""},
                    {"type": "SLURMCTLD", "privateIpAddress": ""},
                    "junk",
                ]
            )
        )
        assert facts.restapi_url() is None
        assert facts.controller_address() is None


# ---------------------------------------------------------------------------
# Discovery, minting and caching
# ---------------------------------------------------------------------------


class TestPcsBackendHappyPath:
    def test_discovers_the_endpoint_and_mints_a_token_for_the_identity(self, clock) -> None:
        backend, pcs, secrets = _backend(clock)

        assert _run(backend.base_url()) == "http://10.0.1.5:6820"
        headers = _run(backend.auth_headers())

        assert set(headers) == {"X-SLURM-USER-TOKEN"}, "PCS rejects X-SLURM-USER-NAME"
        claims = _decode(headers["X-SLURM-USER-TOKEN"])
        assert claims == {
            "iat": 1_700_000_000,
            "exp": 1_700_000_000 + 15 * 60,
            "uid": 1000,
            "gid": 1000,
            "id": {
                "name": "ec2-user",
                "gecos": "ec2-user",
                "dir": "/home/ec2-user",
                "shell": "/bin/bash",
                "gids": [1000, 2000],
            },
        }
        assert pcs.calls == 1
        assert secrets.calls == [{"SecretId": SECRET_ARN, "VersionId": "v1"}]

    def test_defaults_and_hints(self, clock) -> None:
        backend, _pcs, _secrets = _backend(clock, default_partition="gco")
        assert backend.name == "pcs"
        assert backend.defaults.partition == "gco"
        assert backend.defaults.working_directory == "/home/ec2-user"
        assert backend.endpoint_hint() == "pcs://pcs_abc123"
        _run(backend.base_url())
        assert backend.endpoint_hint() == "http://10.0.1.5:6820"
        assert backend.unavailable_detail() == pb.UNAVAILABLE_DETAIL
        assert "25.11 or 26.05" in backend.api_version_detail("v0.0.44")
        assert "/slurm/v0.0.44/ping" in backend.api_version_detail("v0.0.44")

    def test_working_directory_falls_back_to_home_of_name(self, clock) -> None:
        identity = {k: v for k, v in IDENTITY.items() if k != "dir"}
        backend = PcsBackend("pcs_abc123", identity, clock=lambda: clock.monotonic)
        assert backend.defaults.working_directory == "/home/ec2-user"

    def test_the_token_is_reused_until_eighty_percent_of_its_life(self, clock) -> None:
        backend, pcs, secrets = _backend(clock)
        first = _run(backend.auth_headers())["X-SLURM-USER-TOKEN"]
        clock.tick(15 * 60 * 0.8 - 1)
        assert _run(backend.auth_headers())["X-SLURM-USER-TOKEN"] == first
        clock.tick(2)
        second = _run(backend.auth_headers())["X-SLURM-USER-TOKEN"]
        assert second != first
        assert _decode(second)["iat"] == int(clock.wall)
        # Facts and key were read once; only the token was re-minted.
        assert pcs.calls == 1
        assert len(secrets.calls) == 1

    def test_facts_are_refreshed_after_their_ttl_and_the_key_is_kept_for_the_same_version(
        self, clock
    ) -> None:
        backend, pcs, secrets = _backend(clock, facts_ttl_seconds=60)
        _run(backend.auth_headers())
        clock.tick(61)
        _run(backend.base_url())
        assert pcs.calls == 2
        assert len(secrets.calls) == 1

    def test_a_rotated_key_version_is_re_read_and_the_token_re_minted(self, clock) -> None:
        backend, pcs, secrets = _backend(clock, facts_ttl_seconds=60)
        first = _run(backend.auth_headers())["X-SLURM-USER-TOKEN"]
        pcs.cluster["slurmConfiguration"]["jwtAuth"]["jwtKey"]["secretVersion"] = "v2"
        secrets.response = {
            "SecretString": base64.b64encode(b"second-key-second-key-second-key").decode()
        }
        clock.tick(61)
        second = _run(backend.auth_headers())["X-SLURM-USER-TOKEN"]
        assert second != first
        assert secrets.calls[-1] == {"SecretId": SECRET_ARN, "VersionId": "v2"}
        _decode(second, b"second-key-second-key-second-key")

    def test_invalidate_forgets_facts_key_and_token(self, clock) -> None:
        backend, pcs, secrets = _backend(clock)
        _run(backend.auth_headers())
        backend.invalidate()
        assert backend.endpoint_hint() == "pcs://pcs_abc123"
        _run(backend.auth_headers())
        assert pcs.calls == 2
        assert len(secrets.calls) == 2

    def test_a_transport_error_forgets_only_the_facts(self, clock) -> None:
        backend, pcs, secrets = _backend(clock)
        _run(backend.auth_headers())
        backend.on_transport_error()
        _run(backend.base_url())
        assert pcs.calls == 2
        assert len(secrets.calls) == 1

    def test_an_endpoint_override_skips_discovery_for_the_url_only(self, clock) -> None:
        backend, pcs, _secrets = _backend(clock, endpoint_url="http://localhost:6820/")
        assert _run(backend.base_url()) == "http://localhost:6820"
        assert backend.endpoint_hint() == "http://localhost:6820"
        assert pcs.calls == 0
        _run(backend.auth_headers())
        assert pcs.calls == 1
        facts = _run(backend.describe())
        assert facts["endpoint"] == "http://localhost:6820"

    def test_an_override_tolerates_a_cluster_without_a_rest_endpoint(self, clock) -> None:
        pcs = _Pcs(_cluster(endpoints=[]))
        backend, _pcs, _secrets = _backend(clock, pcs=pcs, endpoint_url="http://localhost:6820")
        assert "X-SLURM-USER-TOKEN" in _run(backend.auth_headers())

    def test_describe_lists_the_cluster_facts_and_the_identity(self, clock) -> None:
        backend, _pcs, _secrets = _backend(clock, default_partition="gco")
        facts = _run(backend.describe())
        assert facts["name"] == "pcs"
        assert facts["kind"] == "pcs"
        assert facts["cluster_id"] == "pcs_abc123"
        assert facts["cluster_name"] == "gco-pcs"
        assert facts["status"] == "ACTIVE"
        assert facts["slurm_version"] == "25.11"
        assert facts["rest_mode"] == "STANDARD"
        assert facts["accounting_mode"] == "STANDARD"
        assert facts["endpoint"] == "http://10.0.1.5:6820"
        assert facts["controller"] == "10.0.1.5:6817"
        assert facts["identity"] == {"name": "ec2-user", "uid": 1000, "gid": 1000}
        assert facts["login"] == {"kind": "ssm", "cluster_id": "pcs_abc123"}
        assert facts["default_partition"] == "gco"
        assert facts["working_directory"] == "/home/ec2-user"
        assert "X-SLURM" not in json.dumps(facts)

    def test_secret_shapes(self, clock) -> None:
        # A raw (non-base64) string signs with its own bytes.
        secrets = _Secrets()
        secrets.response = {"SecretString": "not base64! not base64! not base64!"}
        backend, _pcs, _secrets = _backend(clock, secrets=secrets)
        token = _run(backend.auth_headers())["X-SLURM-USER-TOKEN"]
        _decode(token, b"not base64! not base64! not base64!")
        # SecretBinary is accepted too.
        secrets = _Secrets()
        secrets.response = {"SecretBinary": KEY}
        backend, _pcs, _secrets = _backend(clock, secrets=secrets)
        token = _run(backend.auth_headers())["X-SLURM-USER-TOKEN"]
        _decode(token)

    def test_nothing_sensitive_is_logged(self, clock, caplog) -> None:
        backend, _pcs, _secrets = _backend(clock)
        with caplog.at_level(logging.DEBUG):
            token = _run(backend.auth_headers())["X-SLURM-USER-TOKEN"]
            _run(backend.describe())
        assert token not in caplog.text
        assert base64.b64encode(KEY).decode() not in caplog.text
        assert KEY.decode() not in caplog.text

    def test_concurrent_callers_share_one_discovery(self, clock) -> None:
        backend, pcs, secrets = _backend(clock)

        async def fan_out() -> None:
            await asyncio.gather(*(backend.auth_headers() for _ in range(5)))

        _run(fan_out())
        assert pcs.calls == 1
        assert len(secrets.calls) == 1


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestPcsBackendErrors:
    def _expect(self, clock, pcs: _Pcs, message: str, detail: str, **kwargs: Any) -> None:
        backend, _pcs, _secrets = _backend(clock, pcs=pcs, **kwargs)
        with pytest.raises(SlurmUnavailableError) as excinfo:
            _run(backend.auth_headers())
        assert message in str(excinfo.value)
        assert excinfo.value.detail == detail
        http = slurm_client.http_exception_for(excinfo.value)
        assert (http.status_code, http.detail) == (503, detail)

    def test_get_cluster_denied(self, clock, caplog) -> None:
        pcs = _Pcs()
        pcs.error = PermissionError("AccessDeniedException")
        with caplog.at_level("WARNING"):
            self._expect(
                clock,
                pcs,
                "pcs:GetCluster pcs_abc123 failed: PermissionError",
                pb.UNAVAILABLE_DETAIL,
            )
        assert "pcs:GetCluster pcs_abc123 failed: PermissionError" in caplog.text

    def test_get_cluster_without_a_cluster(self, clock) -> None:
        class Empty(_Pcs):
            def get_cluster(self, **kwargs: Any) -> dict[str, Any]:
                return {}

        self._expect(clock, Empty(), "returned no cluster", pb.UNAVAILABLE_DETAIL)

    @pytest.mark.parametrize("status", ["CREATING", "DELETING", "SUSPENDED", "CREATE_FAILED"])
    def test_cluster_not_active(self, clock, status) -> None:
        self._expect(
            clock,
            _Pcs(_cluster(status=status)),
            f"is {status}",
            pb.NOT_ACTIVE_DETAIL.format(status=status),
        )

    def test_updating_counts_as_serving(self, clock) -> None:
        backend, _pcs, _secrets = _backend(clock, pcs=_Pcs(_cluster(status="UPDATING")))
        assert "X-SLURM-USER-TOKEN" in _run(backend.auth_headers())

    def test_rest_mode_none(self, clock) -> None:
        cluster = _cluster()
        cluster["slurmConfiguration"]["slurmRest"] = {"mode": "NONE"}
        self._expect(clock, _Pcs(cluster), "slurmRest.mode NONE", pb.REST_DISABLED_DETAIL)

    def test_rest_mode_missing(self, clock) -> None:
        cluster = _cluster()
        del cluster["slurmConfiguration"]["slurmRest"]
        self._expect(clock, _Pcs(cluster), "slurmRest.mode NONE", pb.REST_DISABLED_DETAIL)

    def test_slurm_too_old(self, clock, monkeypatch) -> None:
        monkeypatch.delenv("SLURM_API_VERSION", raising=False)
        cluster = _cluster(scheduler={"type": "SLURM", "version": "25.05.7"})
        detail = pb.VERSION_DETAIL.format(
            actual="25.05.7", plugin="v0.0.44", supported="25.11 or 26.05"
        )
        self._expect(clock, _Pcs(cluster), "runs Slurm 25.05.7", detail)

    def test_a_patch_release_of_a_supported_version_is_fine(self, clock) -> None:
        backend, _pcs, _secrets = _backend(
            clock, pcs=_Pcs(_cluster(scheduler={"type": "SLURM", "version": "26.05.5"}))
        )
        assert _run(backend.describe())["slurm_version"] == "26.05.5"

    def test_an_unknown_version_string_is_not_rejected(self, clock) -> None:
        backend, _pcs, _secrets = _backend(clock, pcs=_Pcs(_cluster(scheduler={"type": "SLURM"})))
        assert "X-SLURM-USER-TOKEN" in _run(backend.auth_headers())

    def test_no_rest_endpoint(self, clock) -> None:
        cluster = _cluster(endpoints=[{"type": "SLURMCTLD", "privateIpAddress": "10.0.1.5"}])
        self._expect(clock, _Pcs(cluster), "reports no SLURMRESTD endpoint", pb.UNAVAILABLE_DETAIL)

    def test_no_jwt_key(self, clock) -> None:
        cluster = _cluster()
        del cluster["slurmConfiguration"]["jwtAuth"]
        self._expect(
            clock, _Pcs(cluster), "reports no JWT signing key", pb.CREDENTIAL_UNAVAILABLE_DETAIL
        )

    def test_secret_read_denied(self, clock, caplog) -> None:
        secrets = _Secrets()
        secrets.error = PermissionError("AccessDeniedException")
        backend, _pcs, _secrets = _backend(clock, secrets=secrets)
        with caplog.at_level("WARNING"), pytest.raises(SlurmUnavailableError) as excinfo:
            _run(backend.auth_headers())
        assert excinfo.value.detail == pb.CREDENTIAL_UNAVAILABLE_DETAIL
        assert "PermissionError" in caplog.text
        assert "AccessDeniedException" not in str(excinfo.value)

    def test_empty_secret(self, clock) -> None:
        secrets = _Secrets()
        secrets.response = {"SecretString": "   "}
        backend, _pcs, _secrets = _backend(clock, secrets=secrets)
        with pytest.raises(SlurmUnavailableError, match="secret is empty") as excinfo:
            _run(backend.auth_headers())
        assert excinfo.value.detail == pb.CREDENTIAL_UNAVAILABLE_DETAIL

    def test_secret_without_a_version_is_read_by_arn_alone(self, clock) -> None:
        cluster = _cluster()
        cluster["slurmConfiguration"]["jwtAuth"]["jwtKey"] = {"secretArn": SECRET_ARN}
        backend, _pcs, secrets = _backend(clock, pcs=_Pcs(cluster))
        _run(backend.auth_headers())
        assert secrets.calls == [{"SecretId": SECRET_ARN}]

    def test_the_constructor_needs_a_cluster_id(self) -> None:
        with pytest.raises(ValueError, match="needs the PCS cluster id"):
            PcsBackend("", IDENTITY)


# ---------------------------------------------------------------------------
# Environment constructor and the real client
# ---------------------------------------------------------------------------


class TestFromEnvironment:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch):
        for name in (
            pb.CLUSTER_ID_ENV,
            pb.IDENTITY_ENV,
            pb.DEFAULT_PARTITION_ENV,
            pb.ENDPOINT_URL_ENV,
        ):
            monkeypatch.delenv(name, raising=False)

    def test_reads_every_knob(self, monkeypatch) -> None:
        monkeypatch.setenv(pb.CLUSTER_ID_ENV, " pcs_abc123 ")
        monkeypatch.setenv(pb.IDENTITY_ENV, json.dumps(IDENTITY))
        monkeypatch.setenv(pb.DEFAULT_PARTITION_ENV, "gco")
        monkeypatch.setenv(pb.ENDPOINT_URL_ENV, "http://10.0.0.9:6820/")
        monkeypatch.setenv("AWS_REGION", "eu-west-1")

        backend = PcsBackend.from_environment()

        assert backend.cluster_id == "pcs_abc123"
        assert backend.identity == IDENTITY
        assert backend.region == "eu-west-1"
        assert backend.defaults.partition == "gco"
        assert backend.endpoint_hint() == "http://10.0.0.9:6820"

    def test_region_falls_back_to_the_pod_region_variable(self, monkeypatch) -> None:
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("REGION", "ap-south-1")
        monkeypatch.setenv(pb.CLUSTER_ID_ENV, "pcs_abc123")
        monkeypatch.setenv(pb.IDENTITY_ENV, json.dumps(IDENTITY))
        assert PcsBackend.from_environment().region == "ap-south-1"

    def test_blank_partition_means_the_cluster_default(self, monkeypatch) -> None:
        monkeypatch.setenv(pb.CLUSTER_ID_ENV, "pcs_abc123")
        monkeypatch.setenv(pb.IDENTITY_ENV, json.dumps(IDENTITY))
        monkeypatch.setenv(pb.DEFAULT_PARTITION_ENV, "  ")
        assert PcsBackend.from_environment().defaults.partition is None

    def test_missing_cluster_id(self) -> None:
        with pytest.raises(SlurmUnavailableError, match="GCO_PCS_CLUSTER_ID is not set") as excinfo:
            PcsBackend.from_environment()
        assert excinfo.value.detail == pb.UNAVAILABLE_DETAIL

    @pytest.mark.parametrize("raw", ["not json", "[]", '{"uid": 1}', ""])
    def test_bad_identity(self, monkeypatch, raw) -> None:
        monkeypatch.setenv(pb.CLUSTER_ID_ENV, "pcs_abc123")
        monkeypatch.setenv(pb.IDENTITY_ENV, raw)
        with pytest.raises(SlurmUnavailableError, match="GCO_PCS_IDENTITY") as excinfo:
            PcsBackend.from_environment()
        assert excinfo.value.detail == pb.CREDENTIAL_UNAVAILABLE_DETAIL

    def test_lazy_boto_clients_are_built_for_the_region(self, monkeypatch) -> None:
        created: list[tuple[str, str | None]] = []

        class Boto:
            @staticmethod
            def client(service: str, *, region_name: str | None = None, config: Any = None) -> str:
                created.append((service, region_name))
                return service

        monkeypatch.setattr("boto3.client", Boto.client)
        backend = PcsBackend("pcs_abc123", IDENTITY, region="us-east-2")
        assert backend._pcs_client() == "pcs"
        assert backend._secrets_client() == "secretsmanager"
        assert backend._pcs_client() == "pcs"
        assert created == [("pcs", "us-east-2"), ("secretsmanager", "us-east-2")]


class TestThroughTheRestClient:
    """The real ``SlurmRestClient`` with a PCS backend: headers, 401 and transport paths."""

    @pytest.fixture
    def transport(self, monkeypatch: pytest.MonkeyPatch):
        import httpx2

        from gco.services import tracing

        state: dict[str, Any] = {"requests": [], "responses": []}

        def handler(request: httpx2.Request) -> httpx2.Response:
            state["requests"].append(request)
            response = state["responses"].pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        def wrap(_transport: Any) -> httpx2.MockTransport:
            return httpx2.MockTransport(handler)

        monkeypatch.setattr(tracing, "wrap_async_transport", wrap)
        return state

    def test_requests_carry_only_the_token_and_go_to_the_discovered_endpoint(
        self, clock, transport
    ) -> None:
        import httpx2

        backend, _pcs, _secrets = _backend(clock)
        client = SlurmRestClient(backend=backend, version="v0.0.44")
        transport["responses"] = [httpx2.Response(200, json={"pings": [], "errors": []})]

        _run(client.ping())

        [request] = transport["requests"]
        assert str(request.url) == "http://10.0.1.5:6820/slurm/v0.0.44/ping"
        assert "X-SLURM-USER-NAME" not in request.headers
        _decode(request.headers["X-SLURM-USER-TOKEN"])

    def test_a_401_re_discovers_and_re_mints_before_retrying(self, clock, transport) -> None:
        import httpx2

        backend, pcs, secrets = _backend(clock)
        client = SlurmRestClient(backend=backend, version="v0.0.44")
        transport["responses"] = [
            httpx2.Response(401, json={"errors": [{"description": "expired"}]}),
            httpx2.Response(200, json={"pings": [], "errors": []}),
        ]

        _run(client.ping())

        assert len(transport["requests"]) == 2
        assert pcs.calls == 2
        assert len(secrets.calls) == 2

    def test_a_transport_failure_is_503_with_the_pcs_remedy_and_re_discovers(
        self, clock, transport
    ) -> None:
        import httpx2

        backend, pcs, _secrets = _backend(clock)
        client = SlurmRestClient(backend=backend, version="v0.0.44")
        transport["responses"] = [httpx2.ConnectError("refused")]

        with pytest.raises(SlurmUnavailableError) as excinfo:
            _run(client.ping())

        assert excinfo.value.detail == pb.UNAVAILABLE_DETAIL
        assert "http://10.0.1.5:6820" in str(excinfo.value)
        assert backend.endpoint_hint() == "pcs://pcs_abc123"
        _run(backend.base_url())
        assert pcs.calls == 2

    def test_a_404_on_ping_names_the_pcs_version_remedy(self, clock, transport) -> None:
        import httpx2

        backend, _pcs, _secrets = _backend(clock)
        client = SlurmRestClient(backend=backend, version="v0.0.44")
        transport["responses"] = [httpx2.Response(404, content=b"Not Found")]

        with pytest.raises(SlurmUnavailableError) as excinfo:
            _run(client.ping())

        assert excinfo.value.detail == backend.api_version_detail("v0.0.44")
        assert "aws pcs get-cluster" in excinfo.value.detail
