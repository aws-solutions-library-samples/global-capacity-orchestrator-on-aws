"""A fake NVIDIA Run:ai control plane over real HTTPS, for the reader's tests.

The reader's transport (TLS verification against a private CA, form-encoded
token exchange, bearer GETs, ``limit``/``offset`` paging, status handling) is
only proven against a real socket, so ``tests/test_capacity_runai.py`` runs it
against this server instead of a mocked session. The documents it serves are
shaped like the public API reference (``gco/config/runai_api_contract.json``
names every field the reader reads, and the tests assert each one is present
here).

Each server mints its own CA and leaf certificate (SAN ``localhost`` and
``127.0.0.1``) under a temporary directory; ``ca_file`` is the bundle a client
trusts, and a second CA (``other_ca_file``) proves verification is not
accidental.
"""

from __future__ import annotations

import datetime
import json
import ssl
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CLIENT_ID = "gco-reader"
CLIENT_SECRET = "s3cret-value"  # nosec B105 - a fake credential for the fake server
TOKEN = "token-abc123"  # nosec B105 - a fake token for the fake server
CLUSTER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_CLUSTER_ID = "22222222-2222-2222-2222-222222222222"
TIMESTAMP = "2026-11-09T12:00:00Z"


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def mint_ca(
    directory: Path, name: str
) -> tuple[Path, x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = _key()
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(name))
        .issuer_name(_name(name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    path = directory / f"{name}.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path, cert, key


def mint_leaf(
    directory: Path, ca_cert: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey
) -> tuple[Path, Path]:
    key = _key()
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = directory / "server.pem"
    key_path = directory / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def default_state() -> dict[str, Any]:
    """Two clusters, three pools, a T4 pool with a 2-GPU job running."""
    return {
        "clusters": [
            {"uuid": CLUSTER_ID, "name": "prod", "version": "2.26.3", "tenantId": 1},
            {"uuid": OTHER_CLUSTER_ID, "name": "lab", "version": "2.24.10", "tenantId": 1},
        ],
        "node_pools": [
            {
                "id": "np-1",
                "name": "t4",
                "clusterId": CLUSTER_ID,
                "clusterName": "prod",
                "phase": "Ready",
                "isDefault": False,
            },
            {
                "id": "np-2",
                "name": "default",
                "clusterId": CLUSTER_ID,
                "clusterName": "prod",
                "phase": "Ready",
                "isDefault": True,
            },
            {
                "id": "np-3",
                "name": "h100",
                "clusterId": OTHER_CLUSTER_ID,
                "clusterName": "lab",
                "phase": "Ready",
                "isDefault": False,
            },
        ],
        "nodes": [
            {
                "name": "node-a",
                "clusterUuid": CLUSTER_ID,
                "nodePool": "t4",
                "status": "Ready",
                "gpuInfo": {"gpuType": "Tesla-T4", "gpuCount": 4},
            },
            {
                "name": "node-b",
                "clusterUuid": CLUSTER_ID,
                "nodePool": "default",
                "status": "Ready",
                "gpuInfo": None,
            },
            {
                "name": "node-c",
                "clusterUuid": OTHER_CLUSTER_ID,
                "nodePool": "h100",
                "status": "Ready",
                "gpuInfo": {"gpuType": "NVIDIA-H100-80GB-HBM3", "gpuCount": 8},
            },
            {
                "name": "node-d",
                "clusterUuid": OTHER_CLUSTER_ID,
                "nodePool": "h100",
                "status": "NotReady",
                "gpuInfo": {"gpuType": "NVIDIA-H100-80GB-HBM3", "gpuCount": 8},
            },
        ],
        "quotas": [
            {
                "id": "q-1",
                "orgUnitId": "p-1",
                "orgUnitName": "team-a",
                "parentId": "d-1",
                "clusterId": CLUSTER_ID,
                "nodePoolId": "np-1",
                "nodePoolName": "t4",
                "rank": "1",
                "resources": {"gpu": {"deserved": 2, "limit": 4, "overQuotaWeight": 1}},
                "quotaStatus": {"allocated": {"gpu": 2}, "requested": {"gpu": 2}},
            },
            {
                "id": "q-2",
                "orgUnitId": "p-2",
                "orgUnitName": "team-b",
                "parentId": "d-1",
                "clusterId": OTHER_CLUSTER_ID,
                "nodePoolId": "np-3",
                "nodePoolName": "h100",
                "rank": "1",
                "resources": {"gpu": {"deserved": 8, "limit": None, "overQuotaWeight": 1}},
                "quotaStatus": {"allocated": {}, "requested": {}},
            },
        ],
        "telemetry": {
            "TOTAL_GPUS": _telemetry(
                "TOTAL_GPUS", {(CLUSTER_ID, "t4"): "4", (OTHER_CLUSTER_ID, "h100"): "16"}
            ),
            "ALLOCATED_GPUS": _telemetry(
                "ALLOCATED_GPUS", {(CLUSTER_ID, "t4"): "2.5", (OTHER_CLUSTER_ID, "h100"): "0"}
            ),
            "FREE_GPUS": _telemetry(
                "FREE_GPUS", {(CLUSTER_ID, "t4"): "2", (OTHER_CLUSTER_ID, "h100"): "8"}
            ),
        },
    }


def _telemetry(
    kind: str, values: dict[tuple[str, str], str], timestamp: str = TIMESTAMP
) -> dict[str, Any]:
    return {
        "type": kind,
        "timestamp": timestamp,
        "values": [
            {
                "value": value,
                "groups": [
                    {"key": "ClusterId", "value": cluster, "name": cluster},
                    {"key": "Nodepool", "value": pool, "name": pool},
                ],
            }
            for (cluster, pool), value in values.items()
        ],
    }


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: str


@dataclass
class FakeControlPlane:
    """State and behaviour knobs the handler reads on every request."""

    state: dict[str, Any] = field(default_factory=default_state)
    #: "METHOD /path" -> HTTP status to answer instead of the document.
    status: dict[str, int] = field(default_factory=dict)
    #: "METHOD /path" -> (status, headers, body) to answer verbatim.
    raw: dict[str, tuple[int, dict[str, str], bytes]] = field(default_factory=dict)
    #: Ignore ``offset`` on the paged routes (a server that never advances).
    ignore_offset: bool = False
    recorded: list[Recorded] = field(default_factory=list)
    url: str = ""
    ca_file: Path = Path()
    other_ca_file: Path = Path()


def _handler(plane: FakeControlPlane) -> type[BaseHTTPRequestHandler]:
    paged = {
        "/api/v1/node-pools": ("nodepools", "node_pools", False),
        "/api/v1/nodes": ("nodes", "nodes", False),
        "/api/v1/org-unit/projects/node-pool-resources": ("nodePoolResources", "quotas", True),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence the stderr access log
            pass

        def _answer(
            self,
            status: int,
            document: Any = None,
            headers: dict[str, str] | None = None,
            raw: bytes | None = None,
        ) -> None:
            body = raw if raw is not None else json.dumps(document).encode()
            self.send_response(status)
            for key, value in (headers or {"Content-Type": "application/json"}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve(self, method: str) -> None:
            parts = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            plane.recorded.append(
                Recorded(
                    method, parts.path, parse_qs(parts.query), dict(self.headers.items()), body
                )
            )
            label = f"{method} {parts.path}"
            if label in plane.raw:
                status, headers, raw = plane.raw[label]
                self._answer(status, headers=headers, raw=raw)
                return
            if label in plane.status:
                self._answer(plane.status[label], {"code": plane.status[label], "message": "fake"})
                return
            if label == "POST /api/v2/token":
                form = parse_qs(body)
                ok = (
                    form.get("grant_type") == ["client_credentials"]
                    and form.get("client_id") == [CLIENT_ID]
                    and form.get("client_secret") == [CLIENT_SECRET]
                )
                if ok:
                    self._answer(
                        200, {"access_token": TOKEN, "token_type": "Bearer", "expires_in": 3600}
                    )
                else:
                    self._answer(401, {"code": 401, "message": "bad credentials"})
                return
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._answer(401, {"code": 401, "message": "no token"})
                return
            query = parse_qs(parts.query)
            if parts.path == "/api/v1/clusters":
                self._answer(200, plane.state["clusters"])
            elif parts.path in paged:
                collection, key, has_next = paged[parts.path]
                items = plane.state[key]
                offset = 0 if plane.ignore_offset else int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                page = items[offset : offset + limit]
                document: dict[str, Any] = {collection: page}
                if has_next:
                    document["next"] = (
                        offset + len(page) if offset + len(page) < len(items) else None
                    )
                self._answer(200, document)
            elif parts.path == "/api/v1/nodes/telemetry":
                kind = query.get("telemetryType", [""])[0]
                self._answer(200, plane.state["telemetry"][kind])
            else:
                self._answer(404, {"code": 404, "message": "no route"})

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

    return Handler


class RunningControlPlane:
    """Start a FakeControlPlane on 127.0.0.1 with its own CA; stop() shuts it down."""

    def __init__(self, directory: Path) -> None:
        self.plane = FakeControlPlane()
        ca_file, ca_cert, ca_key = mint_ca(directory, "runai-test-ca")
        other_ca, _, _ = mint_ca(directory, "unrelated-ca")
        cert, key = mint_leaf(directory, ca_cert, ca_key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.plane))
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.plane.url = f"https://127.0.0.1:{self.server.server_address[1]}"
        self.plane.ca_file = ca_file
        self.plane.other_ca_file = other_ca

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
