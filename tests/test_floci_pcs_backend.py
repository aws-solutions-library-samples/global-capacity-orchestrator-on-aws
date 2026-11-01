"""Floci emulator layer: the PCS Slurm backend's JWT signing key over the wire.

``gco.services.pcs_backend.PcsBackend`` signs every slurmrestd request with
an HS256 token minted from the key AWS PCS keeps in Secrets Manager
(``slurmConfiguration.jwtAuth.jwtKey``: the secret's ARN and the version the
cluster is using). The PCS API itself is absent from Floci
(docs/FLOCI_TESTING.md, gap 9), so ``GetCluster`` is answered by a stub
carrying the facts a real cluster reports; everything from there on runs for
real against the emulator's Secrets Manager: the version-pinned read, the two
shapes the key arrives in (a base64 ``SecretString``, the documented form, and
raw ``SecretBinary``), a rotation that moves the pin to a new version, and a
pin to a version the secret no longer has.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from typing import Any

import boto3
import pytest

from tests._floci import floci_test_markers, unique_name

pytestmark = floci_test_markers()

REGION = "us-east-1"
#: The API's Slurm identity as the regional stack renders it onto the pod.
IDENTITY = {
    "name": "gco-slurm",
    "uid": 1500,
    "gid": 1500,
    "gids": [1500],
    "dir": "/home/gco-slurm",
    "shell": "/bin/bash",
}


class _ClusterFactsStub:
    """``GetCluster`` as PCS answers it for GCO's managed cluster, minus the API."""

    def __init__(self, secret_arn: str, secret_version: str) -> None:
        self.secret_arn = secret_arn
        self.secret_version = secret_version
        self.calls = 0

    def get_cluster(self, *, clusterIdentifier: str) -> dict[str, Any]:
        self.calls += 1
        return {
            "cluster": {
                "id": clusterIdentifier,
                "name": "gco-pcs",
                "arn": f"arn:aws:pcs:{REGION}:{os.environ['AWS_ACCESS_KEY_ID']}:cluster/{clusterIdentifier}",
                "status": "ACTIVE",
                "size": "SMALL",
                "scheduler": {"type": "SLURM", "version": "25.11"},
                "slurmConfiguration": {
                    "slurmRest": {"mode": "STANDARD"},
                    "accounting": {"mode": "STANDARD", "defaultPurgeTimeInDays": 30},
                    "jwtAuth": {
                        "jwtKey": {
                            "secretArn": self.secret_arn,
                            "secretVersion": self.secret_version,
                        }
                    },
                },
                "endpoints": [
                    {"type": "SLURMCTLD", "privateIpAddress": "10.0.1.5", "port": "6817"},
                    {"type": "SLURMRESTD", "privateIpAddress": "10.0.1.5", "port": "6820"},
                ],
            }
        }


def _verify(token: str, key: bytes) -> dict[str, Any]:
    """The claims of ``token`` once its HS256 signature checks against ``key``."""
    header, payload, signature = token.split(".")
    signing_input = f"{header}.{payload}".encode("ascii")
    expected = base64.urlsafe_b64encode(
        hmac.new(key, signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    assert signature.encode("ascii") == expected, "the token was not signed with the key read"
    padded = payload + "=" * (-len(payload) % 4)
    claims: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    return claims


@pytest.fixture
def secrets(verified_floci_endpoint):
    return boto3.client("secretsmanager", region_name=REGION)


def _backend(stub: _ClusterFactsStub, cluster_id: str) -> Any:
    from gco.services.pcs_backend import PcsBackend

    # The Secrets Manager client is the production one: boto3 against the
    # emulator through AWS_ENDPOINT_URL, no injection.
    return PcsBackend(cluster_id, IDENTITY, region=REGION, pcs_client=stub)


class TestJwtKeyRead:
    def test_the_pinned_version_of_a_base64_key_signs_the_token(self, secrets):
        key = os.urandom(32)
        name = unique_name("gco/pcs/jwt")
        created = secrets.create_secret(Name=name, SecretString=base64.b64encode(key).decode())
        stub = _ClusterFactsStub(created["ARN"], created["VersionId"])
        try:
            backend = _backend(stub, "pcs_abc123def456")
            headers = asyncio.run(backend.auth_headers())
            claims = _verify(headers["X-SLURM-USER-TOKEN"], key)
            assert claims["id"]["name"] == "gco-slurm"
            assert claims["uid"] == 1500
            assert claims["exp"] - claims["iat"] == int(backend.token_ttl_seconds)
            assert stub.calls == 1
            # The backend's description carries the cluster facts, never the key.
            described = asyncio.run(backend.describe())
            assert described["endpoint"] == "http://10.0.1.5:6820"
            assert described["slurm_version"] == "25.11"
            assert base64.b64encode(key).decode() not in json.dumps(described)
        finally:
            secrets.delete_secret(SecretId=created["ARN"], ForceDeleteWithoutRecovery=True)

    def test_a_raw_binary_key_signs_too(self, secrets):
        key = os.urandom(32)
        name = unique_name("gco/pcs/jwt-binary")
        created = secrets.create_secret(Name=name, SecretBinary=key)
        stub = _ClusterFactsStub(created["ARN"], created["VersionId"])
        try:
            headers = asyncio.run(_backend(stub, "pcs_bin0000000001").auth_headers())
            _verify(headers["X-SLURM-USER-TOKEN"], key)
        finally:
            secrets.delete_secret(SecretId=created["ARN"], ForceDeleteWithoutRecovery=True)

    def test_a_rotated_key_is_read_once_the_cluster_pins_the_new_version(self, secrets):
        """PCS rotates by writing a new version and moving the cluster's pin.

        Until the pin moves the backend must keep signing with the version it
        was told, not with whatever is AWSCURRENT; once the pin moves, the
        next token has to come from the new key.
        """
        from gco.services import slurm_client

        old_key, new_key = os.urandom(32), os.urandom(32)
        name = unique_name("gco/pcs/jwt-rotate")
        created = secrets.create_secret(Name=name, SecretString=base64.b64encode(old_key).decode())
        stub = _ClusterFactsStub(created["ARN"], created["VersionId"])
        try:
            backend = _backend(stub, "pcs_rot0000000001")
            first = asyncio.run(backend.auth_headers())["X-SLURM-USER-TOKEN"]
            _verify(first, old_key)

            rotated = secrets.put_secret_value(
                SecretId=created["ARN"], SecretString=base64.b64encode(new_key).decode()
            )
            assert rotated["VersionId"] != created["VersionId"]
            # A 401 from slurmrestd makes the backend forget facts, key and
            # token; the pin still names the old version, so the old key is
            # what the pinned read returns even though AWSCURRENT moved on.
            backend.invalidate()
            _verify(asyncio.run(backend.auth_headers())["X-SLURM-USER-TOKEN"], old_key)

            stub.secret_version = rotated["VersionId"]
            backend.invalidate()
            _verify(asyncio.run(backend.auth_headers())["X-SLURM-USER-TOKEN"], new_key)

            # A pin to a version the secret does not have is unavailability
            # naming the failed operation's error type, never the key or the
            # secret's contents.
            stub.secret_version = "00000000-0000-0000-0000-000000000000"
            backend.invalidate()
            with pytest.raises(slurm_client.SlurmUnavailableError) as excinfo:
                asyncio.run(backend.auth_headers())
            assert str(excinfo.value) == (
                "reading the PCS JWT signing key failed: ResourceNotFoundException"
            )
        finally:
            secrets.delete_secret(SecretId=created["ARN"], ForceDeleteWithoutRecovery=True)

    def test_a_deleted_secret_is_unavailability_not_a_crash(self, secrets):
        from gco.services import slurm_client

        name = unique_name("gco/pcs/jwt-gone")
        created = secrets.create_secret(
            Name=name, SecretString=base64.b64encode(b"k" * 32).decode()
        )
        secrets.delete_secret(SecretId=created["ARN"], ForceDeleteWithoutRecovery=True)
        stub = _ClusterFactsStub(created["ARN"], created["VersionId"])
        with pytest.raises(slurm_client.SlurmUnavailableError, match="ResourceNotFoundException"):
            asyncio.run(_backend(stub, "pcs_gone000000001").auth_headers())
