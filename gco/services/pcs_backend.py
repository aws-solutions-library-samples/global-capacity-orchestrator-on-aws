"""The AWS PCS Slurm backend: runtime discovery of slurmrestd and self-minted JWTs.

AWS PCS runs the Slurm controller for the operator and, when the cluster's
``slurmRest.mode`` is ``STANDARD``, exposes slurmrestd on the controller's
private address in the cluster's VPC (``http://<ip>:6820``, plain HTTP).
Authentication is Slurm's ``auth/jwt`` with a twist: PCS generates the HS256
signing key itself and keeps it in Secrets Manager; the caller mints tokens
from that key carrying the POSIX identity the job should run as. PCS does not
accept ``X-SLURM-USER-NAME`` impersonation, so the identity is whatever the
token says, which GCO fixes by configuration (``pcs.identity``) rather than by
caller. The submitting caller still travels in each job's ``comment``.

What this backend resolves at runtime, and why:

- ``pcs:GetCluster`` gives the SLURMRESTD endpoint (``endpoints[type=SLURMRESTD]``,
  a list the regional stack's flat-path custom resource cannot filter), the
  JWT key's secret ARN and version, the Slurm version and the REST mode. It
  is cached and re-read on a connection failure or a 401, so a controller
  replacement or a key rotation heals without a pod restart.
- ``secretsmanager:GetSecretValue`` on exactly that secret and version returns
  the base64-encoded signing key; it is cached with the cluster facts.
- Tokens are minted in-process with ``hmac``/``hashlib`` (no runtime JWT
  dependency), live fifteen minutes and are re-minted at 80% of that, so a
  request never carries a token about to expire mid-flight.

The IAM grants behind these two reads are the only PCS permissions the API
pod holds (``gco/stacks/pcs_cluster.py``). Nothing here logs a key, a token or
a header value.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from gco.pcs_config import (
    PCS_BACKEND,
    PCS_RESTAPI_PORT,
    PCS_SUPPORTED_SLURM_VERSIONS,
    identity_claims,
)
from gco.services import slurm_client
from gco.services.slurm_client import BackendDefaults

logger = logging.getLogger(__name__)

#: Environment the regional stack renders onto the API pod (31-manifest-processor.yaml).
CLUSTER_ID_ENV = "GCO_PCS_CLUSTER_ID"
IDENTITY_ENV = "GCO_PCS_IDENTITY"
DEFAULT_PARTITION_ENV = "GCO_PCS_DEFAULT_PARTITION"
#: Skip discovery and talk to this URL (tests, a static setup with a known
#: controller address). The signing key is still read from the cluster facts.
ENDPOINT_URL_ENV = "GCO_PCS_ENDPOINT_URL"

#: Token lifetime and the fraction of it after which a fresh one is minted.
TOKEN_TTL_SECONDS = 15 * 60
TOKEN_REFRESH_FRACTION = 0.8
#: Cluster facts (endpoint, secret ARN, version) are re-read this often even
#: without a failure, so a planned key rotation is picked up promptly.
FACTS_TTL_SECONDS = 15 * 60

#: ``Cluster.status`` values that mean the controller is serving.
ACTIVE_STATUSES = frozenset({"ACTIVE", "UPDATING"})

UNAVAILABLE_DETAIL = (
    "AWS PCS's Slurm REST API is unavailable in this region. Check that the PCS cluster is "
    "ACTIVE with slurmRest.mode STANDARD (aws pcs get-cluster), that its security group "
    "admits tcp/6820 from the EKS cluster security group, and that the cluster sits in the "
    "regional VPC or a peered range named in pcs.endpoint_cidr."
)
CREDENTIAL_UNAVAILABLE_DETAIL = (
    "The GCO API cannot mint a credential for AWS PCS: reading the cluster's JWT signing key "
    "from Secrets Manager failed. Check the manifest-processor role's pcs:GetCluster and "
    "secretsmanager:GetSecretValue grants (and kms:Decrypt if the secret uses a customer key)."
)
REST_DISABLED_DETAIL = (
    "The PCS cluster does not expose Slurm's REST API (slurmRest.mode is NONE). Enable it with "
    "`aws pcs update-cluster --slurm-configuration slurmRest={mode=STANDARD}` (Slurm 25.05+) "
    "or recreate the cluster with the REST API on."
)
VERSION_DETAIL = (
    "The PCS cluster runs Slurm {actual}, whose slurmrestd does not serve the OpenAPI plugin "
    "version GCO addresses ({plugin}); PCS clusters on Slurm {supported} do. Update the "
    "cluster's scheduler version (aws pcs update-cluster) or point pcs.cluster_id at one "
    "that qualifies."
)
API_VERSION_DETAIL = (
    "The PCS cluster's slurmrestd does not serve OpenAPI plugin version {version} (404 on "
    "/slurm/{version}/ping). GCO pins the plugin version to the Slurm releases PCS offers "
    "({supported}); check the cluster's scheduler version with `aws pcs get-cluster`."
)
NOT_ACTIVE_DETAIL = (
    "The PCS cluster is {status}, not ACTIVE; its controller is not serving. Wait for the "
    "cluster to become ACTIVE (aws pcs get-cluster) or check its errorInfo."
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_hs256_jwt(claims: Mapping[str, Any], key: bytes) -> str:
    """A compact JWS (``header.payload.signature``) signed with HMAC-SHA256.

    The header is the one Slurm's ``auth/jwt`` expects (``alg: HS256``,
    ``typ: JWT``); the payload is ``claims`` serialized compactly with sorted
    keys so a token's bytes are reproducible for a given claim set and time.
    """
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(
        json.dumps(dict(claims), separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(key, signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


def _mapping(value: Any) -> Mapping[str, Any]:
    """The value when it is a JSON object, else an empty one (sparse API payloads)."""
    return value if isinstance(value, Mapping) else {}


def _slurm_major_minor(version: str) -> str:
    """``"25.11.2"`` -> ``"25.11"`` (PCS reports the patch release)."""
    parts = version.strip().split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else version.strip()


class PcsClusterFacts:
    """What one ``GetCluster`` call told us, reduced to the fields the backend uses."""

    def __init__(self, cluster: Mapping[str, Any]) -> None:
        self.id = str(cluster.get("id") or "")
        self.name = str(cluster.get("name") or "")
        self.arn = str(cluster.get("arn") or "")
        self.status = str(cluster.get("status") or "UNKNOWN")
        self.scheduler_version = str(_mapping(cluster.get("scheduler")).get("version") or "")
        self.size = str(cluster.get("size") or "")
        slurm_configuration = _mapping(cluster.get("slurmConfiguration"))
        self.rest_mode = str(_mapping(slurm_configuration.get("slurmRest")).get("mode") or "NONE")
        self.accounting_mode = str(
            _mapping(slurm_configuration.get("accounting")).get("mode") or "NONE"
        )
        key = _mapping(_mapping(slurm_configuration.get("jwtAuth")).get("jwtKey"))
        self.jwt_secret_arn = str(key.get("secretArn") or "")
        self.jwt_secret_version = str(key.get("secretVersion") or "")
        self.endpoints: list[dict[str, Any]] = [
            dict(item) for item in (cluster.get("endpoints") or []) if isinstance(item, Mapping)
        ]

    def restapi_url(self) -> str | None:
        for endpoint in self.endpoints:
            if str(endpoint.get("type") or "").upper() != "SLURMRESTD":
                continue
            host = str(endpoint.get("privateIpAddress") or "").strip()
            if not host:
                ipv6 = str(endpoint.get("ipv6Address") or "").strip()
                host = f"[{ipv6}]" if ipv6 else ""
            if not host:
                continue
            port = str(endpoint.get("port") or PCS_RESTAPI_PORT).strip()
            return f"http://{host}:{port}"
        return None

    def controller_address(self) -> str | None:
        for endpoint in self.endpoints:
            if str(endpoint.get("type") or "").upper() == "SLURMCTLD":
                host = str(endpoint.get("privateIpAddress") or "").strip()
                if host:
                    return f"{host}:{endpoint.get('port') or ''}".rstrip(":")
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "cluster_id": self.id,
            "cluster_name": self.name,
            "cluster_arn": self.arn,
            "status": self.status,
            "size": self.size,
            "slurm_version": self.scheduler_version,
            "rest_mode": self.rest_mode,
            "accounting_mode": self.accounting_mode,
            "controller": self.controller_address(),
            "endpoint": self.restapi_url(),
        }


class PcsBackend:
    """The PCS cluster named by ``pcs.cluster_id`` (or the one the stack created)."""

    name = PCS_BACKEND

    def __init__(
        self,
        cluster_id: str,
        identity: Mapping[str, Any],
        *,
        region: str | None = None,
        default_partition: str | None = None,
        endpoint_url: str | None = None,
        pcs_client: Any | None = None,
        secrets_client: Any | None = None,
        token_ttl_seconds: float = TOKEN_TTL_SECONDS,
        facts_ttl_seconds: float = FACTS_TTL_SECONDS,
        clock: Any = time.monotonic,
        now: Any = time.time,
    ) -> None:
        if not cluster_id:
            raise ValueError("PcsBackend needs the PCS cluster id")
        self.cluster_id = cluster_id
        self.identity = dict(identity)
        self.region = region or os.getenv("AWS_REGION") or os.getenv("REGION")
        self.defaults = BackendDefaults(
            partition=default_partition or None,
            working_directory=str(self.identity.get("dir") or f"/home/{self.identity['name']}"),
        )
        self._endpoint_override = endpoint_url.rstrip("/") if endpoint_url else None
        self._pcs = pcs_client
        self._secrets = secrets_client
        self.token_ttl_seconds = float(token_ttl_seconds)
        self.facts_ttl_seconds = float(facts_ttl_seconds)
        self._clock = clock
        self._now = now
        self._facts: PcsClusterFacts | None = None
        self._facts_at = 0.0
        self._key: bytes | None = None
        self._key_version: str | None = None
        self._token: str | None = None
        self._token_at = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_environment(cls) -> PcsBackend:
        """Build from the env the regional stack renders onto the API pod."""
        cluster_id = os.getenv(CLUSTER_ID_ENV, "").strip()
        if not cluster_id:
            raise slurm_client.SlurmUnavailableError(
                f"{CLUSTER_ID_ENV} is not set on the API pod", detail=UNAVAILABLE_DETAIL
            )
        raw_identity = os.getenv(IDENTITY_ENV, "").strip()
        try:
            identity = json.loads(raw_identity) if raw_identity else {}
        except json.JSONDecodeError as exc:
            raise slurm_client.SlurmUnavailableError(
                f"{IDENTITY_ENV} is not valid JSON", detail=CREDENTIAL_UNAVAILABLE_DETAIL
            ) from exc
        if not isinstance(identity, dict) or not identity.get("name"):
            raise slurm_client.SlurmUnavailableError(
                f"{IDENTITY_ENV} must be a JSON object with at least a name",
                detail=CREDENTIAL_UNAVAILABLE_DETAIL,
            )
        return cls(
            cluster_id,
            identity,
            default_partition=os.getenv(DEFAULT_PARTITION_ENV, "").strip() or None,
            endpoint_url=os.getenv(ENDPOINT_URL_ENV, "").strip() or None,
        )

    # -- AWS clients -----------------------------------------------------------

    def _pcs_client(self) -> Any:
        if self._pcs is None:
            import boto3
            from botocore.config import Config

            self._pcs = boto3.client(
                "pcs",
                region_name=self.region,
                config=Config(
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
        return self._pcs

    def _secrets_client(self) -> Any:
        if self._secrets is None:
            import boto3
            from botocore.config import Config

            self._secrets = boto3.client(
                "secretsmanager",
                region_name=self.region,
                config=Config(
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
        return self._secrets

    # -- discovery ---------------------------------------------------------------

    def _fetch_facts(self) -> PcsClusterFacts:
        try:
            response = self._pcs_client().get_cluster(clusterIdentifier=self.cluster_id)
        except Exception as exc:
            logger.warning("pcs:GetCluster %s failed: %s", self.cluster_id, type(exc).__name__)
            raise slurm_client.SlurmUnavailableError(
                f"pcs:GetCluster {self.cluster_id} failed: {type(exc).__name__}",
                detail=UNAVAILABLE_DETAIL,
            ) from exc
        cluster = response.get("cluster") if isinstance(response, Mapping) else None
        if not isinstance(cluster, Mapping):
            raise slurm_client.SlurmUnavailableError(
                f"pcs:GetCluster {self.cluster_id} returned no cluster", detail=UNAVAILABLE_DETAIL
            )
        facts = PcsClusterFacts(cluster)
        if facts.status not in ACTIVE_STATUSES:
            raise slurm_client.SlurmUnavailableError(
                f"PCS cluster {self.cluster_id} is {facts.status}",
                detail=NOT_ACTIVE_DETAIL.format(status=facts.status),
            )
        if facts.rest_mode.upper() != "STANDARD":
            raise slurm_client.SlurmUnavailableError(
                f"PCS cluster {self.cluster_id} has slurmRest.mode {facts.rest_mode}",
                detail=REST_DISABLED_DETAIL,
            )
        major_minor = _slurm_major_minor(facts.scheduler_version)
        if major_minor and major_minor not in PCS_SUPPORTED_SLURM_VERSIONS:
            raise slurm_client.SlurmUnavailableError(
                f"PCS cluster {self.cluster_id} runs Slurm {facts.scheduler_version}",
                detail=VERSION_DETAIL.format(
                    actual=facts.scheduler_version,
                    plugin=slurm_client.api_version(),
                    supported=" or ".join(PCS_SUPPORTED_SLURM_VERSIONS),
                ),
            )
        if self._endpoint_override is None and facts.restapi_url() is None:
            raise slurm_client.SlurmUnavailableError(
                f"PCS cluster {self.cluster_id} reports no SLURMRESTD endpoint",
                detail=UNAVAILABLE_DETAIL,
            )
        if not facts.jwt_secret_arn:
            raise slurm_client.SlurmUnavailableError(
                f"PCS cluster {self.cluster_id} reports no JWT signing key",
                detail=CREDENTIAL_UNAVAILABLE_DETAIL,
            )
        return facts

    def _fetch_key(self, facts: PcsClusterFacts) -> bytes:
        kwargs: dict[str, Any] = {"SecretId": facts.jwt_secret_arn}
        if facts.jwt_secret_version:
            kwargs["VersionId"] = facts.jwt_secret_version
        try:
            response = self._secrets_client().get_secret_value(**kwargs)
        except Exception as exc:
            logger.warning(
                "secretsmanager:GetSecretValue for the PCS JWT key failed: %s", type(exc).__name__
            )
            raise slurm_client.SlurmUnavailableError(
                f"reading the PCS JWT signing key failed: {type(exc).__name__}",
                detail=CREDENTIAL_UNAVAILABLE_DETAIL,
            ) from exc
        secret = response.get("SecretString") if isinstance(response, Mapping) else None
        if isinstance(secret, str) and secret.strip():
            try:
                key = base64.b64decode(secret.strip(), validate=True)
            except ValueError, TypeError:
                # Not base64: PCS documents a base64 string, but a key pasted
                # raw still signs; use its bytes.
                key = secret.strip().encode("utf-8")
        else:
            binary = response.get("SecretBinary") if isinstance(response, Mapping) else None
            key = bytes(binary) if binary else b""
        if not key:
            raise slurm_client.SlurmUnavailableError(
                "the PCS JWT signing key secret is empty", detail=CREDENTIAL_UNAVAILABLE_DETAIL
            )
        return key

    async def _facts_and_key(self) -> tuple[PcsClusterFacts, bytes]:
        async with self._lock:
            now = self._clock()
            facts = self._facts
            if facts is None or now - self._facts_at >= self.facts_ttl_seconds:
                facts = await asyncio.to_thread(self._fetch_facts)
                self._facts = facts
                self._facts_at = self._clock()
                if self._key_version != facts.jwt_secret_version or self._key is None:
                    self._key = None
            if self._key is None:
                self._key = await asyncio.to_thread(self._fetch_key, facts)
                self._key_version = facts.jwt_secret_version
                self._token = None
            return facts, self._key

    # -- SlurmBackend protocol -------------------------------------------------

    async def base_url(self) -> str:
        if self._endpoint_override:
            return self._endpoint_override
        facts, _key = await self._facts_and_key()
        url = facts.restapi_url()
        if url is None:  # pragma: no cover - _fetch_facts rejects this already
            raise slurm_client.SlurmUnavailableError(
                "no SLURMRESTD endpoint", detail=UNAVAILABLE_DETAIL
            )
        return url

    async def auth_headers(self) -> dict[str, str]:
        _facts, key = await self._facts_and_key()
        now = self._clock()
        if self._token is None or now - self._token_at >= (
            self.token_ttl_seconds * TOKEN_REFRESH_FRACTION
        ):
            issued = int(self._now())
            claims = {
                "iat": issued,
                "exp": issued + int(self.token_ttl_seconds),
                **identity_claims(self.identity),
            }
            self._token = mint_hs256_jwt(claims, key)
            self._token_at = now
        return {"X-SLURM-USER-TOKEN": self._token}

    def invalidate(self) -> None:
        """A 401: the key may have rotated. Forget facts, key and token alike."""
        self._token = None
        self._key = None
        self._facts = None
        self._facts_at = 0.0

    def on_transport_error(self) -> None:
        """A connection failure: the controller may have moved. Re-discover next time."""
        self._facts = None
        self._facts_at = 0.0

    def endpoint_hint(self) -> str:
        if self._endpoint_override:
            return self._endpoint_override
        if self._facts is not None:
            return self._facts.restapi_url() or f"pcs://{self.cluster_id}"
        return f"pcs://{self.cluster_id}"

    def unavailable_detail(self) -> str:
        return UNAVAILABLE_DETAIL

    def api_version_detail(self, version: str) -> str:
        return API_VERSION_DETAIL.format(
            version=version, supported=" or ".join(PCS_SUPPORTED_SLURM_VERSIONS)
        )

    async def describe(self) -> dict[str, Any]:
        facts, _key = await self._facts_and_key()
        summary = facts.summary()
        if self._endpoint_override:
            summary["endpoint"] = self._endpoint_override
        return {
            "name": self.name,
            "kind": "pcs",
            "description": "AWS Parallel Computing Service cluster in the regional VPC",
            **summary,
            "identity": {
                "name": self.identity.get("name"),
                "uid": self.identity.get("uid"),
                "gid": self.identity.get("gid"),
            },
            "login": {"kind": "ssm", "cluster_id": self.cluster_id},
            "default_partition": self.defaults.partition,
            "working_directory": self.defaults.working_directory,
        }
