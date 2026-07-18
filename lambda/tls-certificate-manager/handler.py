"""Manage GCO's deployment-local root CA and regional ACM leaf certificates.

The function has two entry points:

* a CDK ``cr.Provider`` custom resource bootstraps the root and one imported
  certificate per workload region; and
* an EventBridge schedule renews leaves before expiry and performs staged,
  overlap-safe root rollover.

The root private key is stored only in the KMS-encrypted Secrets Manager secret
named by ``ROOT_SECRET_ARN``. Leaf private keys are generated in memory and sent
directly to the regional ACM ``ImportCertificate`` API; they are never written
to logs, SSM, or durable Lambda storage. SSM contains only public trust material
and ACM certificate ARNs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/tls-certificate-manager/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/tls-certificate-manager/handler.lambda_handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

_SCHEMA_VERSION = 1
_REGION_RE = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+$")
_MANAGED_BY_TAG_VALUE = "gco-backend-tls-manager"
_CERTIFICATE_STATUSES = (
    "PENDING_VALIDATION",
    "ISSUED",
    "INACTIVE",
    "EXPIRED",
    "VALIDATION_TIMED_OUT",
    "REVOKED",
    "FAILED",
)
_DNS_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ManagerConfig:
    """Validated certificate policy shared by custom-resource and scheduled calls."""

    regions: tuple[str, ...]
    server_name: str
    project_name: str
    registry_region: str
    root_ca_parameter_name: str
    certificate_parameter_prefix: str
    root_generation: int
    root_validity_days: int
    root_rotate_before_days: int
    root_activation_delay_hours: int
    root_overlap_days: int
    leaf_validity_days: int
    leaf_rotate_before_days: int

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> ManagerConfig:
        properties = event.get("ResourceProperties") or event
        raw_regions = properties.get("Regions")
        if raw_regions is None:
            raw_regions = json.loads(os.environ.get("CERTIFICATE_REGIONS", "[]"))
        if not isinstance(raw_regions, list):
            raise ValueError("Regions must be a list")
        regions = tuple(dict.fromkeys(str(region).strip() for region in raw_regions))
        if not regions or any(_REGION_RE.fullmatch(region) is None for region in regions):
            raise ValueError("At least one valid AWS workload region is required")

        config = cls(
            regions=regions,
            server_name=str(
                properties.get("ServerName") or os.environ.get("BACKEND_TLS_SERVER_NAME", "")
            ).strip(),
            project_name=str(
                properties.get("ProjectName") or os.environ.get("PROJECT_NAME", "")
            ).strip(),
            registry_region=str(
                properties.get("RegistryRegion") or os.environ.get("REGISTRY_REGION", "")
            ).strip(),
            root_ca_parameter_name=str(
                properties.get("RootCaParameterName")
                or os.environ.get("ROOT_CA_PARAMETER_NAME", "")
            ).strip(),
            certificate_parameter_prefix=str(
                properties.get("CertificateParameterPrefix")
                or os.environ.get("CERTIFICATE_PARAMETER_PREFIX", "")
            ).strip(),
            root_generation=_positive_int(
                properties.get("RootGeneration", os.environ.get("ROOT_GENERATION", "1")),
                "RootGeneration",
            ),
            root_validity_days=_positive_int(
                properties.get("RootValidityDays", os.environ.get("ROOT_VALIDITY_DAYS", "3650")),
                "RootValidityDays",
            ),
            root_rotate_before_days=_positive_int(
                properties.get(
                    "RootRotateBeforeDays",
                    os.environ.get("ROOT_ROTATE_BEFORE_DAYS", "180"),
                ),
                "RootRotateBeforeDays",
            ),
            root_activation_delay_hours=_positive_int(
                properties.get(
                    "RootActivationDelayHours",
                    os.environ.get("ROOT_ACTIVATION_DELAY_HOURS", "24"),
                ),
                "RootActivationDelayHours",
            ),
            root_overlap_days=_positive_int(
                properties.get("RootOverlapDays", os.environ.get("ROOT_OVERLAP_DAYS", "45")),
                "RootOverlapDays",
            ),
            leaf_validity_days=_positive_int(
                properties.get("LeafValidityDays", os.environ.get("LEAF_VALIDITY_DAYS", "30")),
                "LeafValidityDays",
            ),
            leaf_rotate_before_days=_positive_int(
                properties.get(
                    "LeafRotateBeforeDays",
                    os.environ.get("LEAF_ROTATE_BEFORE_DAYS", "10"),
                ),
                "LeafRotateBeforeDays",
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if _DNS_RE.fullmatch(self.server_name) is None:
            raise ValueError("ServerName must be a valid private DNS name")
        if _REGION_RE.fullmatch(self.registry_region) is None:
            raise ValueError("RegistryRegion must be a valid AWS region")
        if not self.project_name:
            raise ValueError("ProjectName is required")
        if not self.root_ca_parameter_name.startswith(f"/{self.project_name}/backend-tls/"):
            raise ValueError("RootCaParameterName must stay inside the project TLS namespace")
        if not self.certificate_parameter_prefix.startswith(f"/{self.project_name}/backend-tls/"):
            raise ValueError(
                "CertificateParameterPrefix must stay inside the project TLS namespace"
            )
        if self.root_rotate_before_days >= self.root_validity_days:
            raise ValueError("RootRotateBeforeDays must be less than RootValidityDays")
        if self.leaf_rotate_before_days >= self.leaf_validity_days:
            raise ValueError("LeafRotateBeforeDays must be less than LeafValidityDays")
        if self.root_validity_days <= self.leaf_validity_days:
            raise ValueError("RootValidityDays must exceed LeafValidityDays")
        if self.root_overlap_days <= self.leaf_validity_days:
            raise ValueError("RootOverlapDays must exceed LeafValidityDays")

    def certificate_parameter_name(self, region: str) -> str:
        return f"{self.certificate_parameter_prefix}{region}"


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Invalid root state: {field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid root state: {field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _certificate_not_after(certificate: x509.Certificate) -> datetime:
    value = getattr(certificate, "not_valid_after_utc", None)
    if isinstance(value, datetime):
        return value
    return certificate.not_valid_after.replace(tzinfo=UTC)


def _serialize_private_key(private_key: ec.EllipticCurvePrivateKey) -> str:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _serialize_certificate(certificate: x509.Certificate) -> str:
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _root_subject(project_name: str, generation: int) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "GCO deployment-local PKI"),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"{project_name} backend root generation {generation}",
            ),
        ]
    )


def _generate_root(config: ManagerConfig, generation: int) -> dict[str, Any]:
    now = _now()
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = _root_subject(config.project_name, generation)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=config.root_validity_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    return {
        "generation": generation,
        "private_key_pem": _serialize_private_key(private_key),
        "certificate_pem": _serialize_certificate(certificate),
        "created_at": _iso(now),
        "not_after": _iso(_certificate_not_after(certificate)),
    }


def _validate_root_record(record: Any, field: str) -> tuple[Any, x509.Certificate]:
    if not isinstance(record, dict):
        raise ValueError(f"Invalid root state: {field} is missing")
    generation = record.get("generation")
    if type(generation) is not int or generation <= 0:
        raise ValueError(f"Invalid root state: {field}.generation")
    private_pem = record.get("private_key_pem")
    certificate_pem = record.get("certificate_pem")
    if not isinstance(private_pem, str) or not isinstance(certificate_pem, str):
        raise ValueError(f"Invalid root state: {field} key or certificate is missing")
    try:
        private_key = serialization.load_pem_private_key(private_pem.encode("ascii"), None)
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid root state: {field} contains malformed PEM") from exc
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise ValueError(f"Invalid root state: {field} key must be ECDSA")
    key_public = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    cert_public = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if key_public != cert_public:
        raise ValueError(f"Invalid root state: {field} key does not match its certificate")
    try:
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound as exc:
        raise ValueError(f"Invalid root state: {field} is not a CA") from exc
    if not constraints.ca:
        raise ValueError(f"Invalid root state: {field} is not a CA")
    _parse_iso(record.get("not_after"), f"{field}.not_after")
    return private_key, certificate


def _load_root_state() -> dict[str, Any] | None:
    client = boto3.client("secretsmanager")
    try:
        response = client.get_secret_value(SecretId=os.environ["ROOT_SECRET_ARN"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    secret_string = response.get("SecretString")
    if not secret_string:
        return None
    try:
        state = json.loads(secret_string)
    except json.JSONDecodeError as exc:
        raise ValueError("Root CA secret contains invalid JSON") from exc
    if isinstance(state, dict) and state.get("state") == "UNINITIALIZED":
        return None
    if not isinstance(state, dict) or state.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("Root CA secret has an unsupported schema")
    _validate_root_record(state.get("current"), "current")
    pending = state.get("pending")
    if pending is not None:
        _validate_root_record(pending, "pending")
        _parse_iso(pending.get("activate_after"), "pending.activate_after")
        published_at = pending.get("trust_bundle_published_at")
        if published_at is not None:
            _parse_iso(published_at, "pending.trust_bundle_published_at")
    previous = state.get("previous", [])
    if not isinstance(previous, list):
        raise ValueError("Invalid root state: previous must be a list")
    for index, item in enumerate(previous):
        if not isinstance(item, dict) or not isinstance(item.get("certificate_pem"), str):
            raise ValueError(f"Invalid root state: previous[{index}]")
        x509.load_pem_x509_certificate(item["certificate_pem"].encode("ascii"))
        _parse_iso(item.get("retire_after"), f"previous[{index}].retire_after")
    retired_regions = state.get("retired_regions", [])
    if (
        not isinstance(retired_regions, list)
        or any(
            not isinstance(region, str) or _REGION_RE.fullmatch(region) is None
            for region in retired_regions
        )
        or len(retired_regions) != len(set(retired_regions))
    ):
        raise ValueError("Invalid root state: retired_regions")
    return state


def _save_root_state(state: dict[str, Any]) -> None:
    boto3.client("secretsmanager").put_secret_value(
        SecretId=os.environ["ROOT_SECRET_ARN"],
        SecretString=json.dumps(state, separators=(",", ":")),
    )


def _publish_trust_bundle(config: ManagerConfig, state: dict[str, Any]) -> None:
    certificates = [state["current"]["certificate_pem"]]
    pending = state.get("pending")
    if pending is not None:
        certificates.append(pending["certificate_pem"])
    certificates.extend(item["certificate_pem"] for item in state.get("previous", []))
    bundle = "".join(cert.rstrip() + "\n" for cert in certificates)
    boto3.client("ssm", region_name=config.registry_region).put_parameter(
        Name=config.root_ca_parameter_name,
        Value=bundle,
        Type="String",
        Overwrite=True,
        Description="Public GCO backend TLS root trust bundle; contains no private key",
    )


def _ensure_root(config: ManagerConfig) -> tuple[dict[str, Any], bool]:
    """Ensure root state, stage/promote rollover, and publish the public bundle."""
    now = _now()
    state = _load_root_state()
    changed = False
    if state is None:
        state = {
            "schema_version": _SCHEMA_VERSION,
            "current": _generate_root(config, config.root_generation),
            "pending": None,
            "previous": [],
            "retired_regions": [],
        }
        changed = True
    elif "retired_regions" not in state:
        # Additive migration for root state written before regional retirement
        # tracking existed. The schema remains compatible with old secrets.
        state["retired_regions"] = []
        changed = True

    _, current_certificate = _validate_root_record(state["current"], "current")
    current_generation = int(state["current"]["generation"])
    pending = state.get("pending")
    pending_generation = int(pending["generation"]) if pending is not None else 0
    current_expiry = _certificate_not_after(current_certificate)
    should_rotate_for_expiry = current_expiry <= now + timedelta(
        days=config.root_rotate_before_days
    )
    desired_generation = max(
        config.root_generation,
        current_generation + 1 if should_rotate_for_expiry else current_generation,
    )

    if desired_generation > max(current_generation, pending_generation):
        pending = _generate_root(config, desired_generation)
        pending["activate_after"] = _iso(now + timedelta(hours=config.root_activation_delay_hours))
        state["pending"] = pending
        changed = True
        LOGGER.info(
            "Staged root generation %d; activation begins after the trust propagation delay",
            desired_generation,
        )

    pending = state.get("pending")
    if (
        pending is not None
        and pending.get("trust_bundle_published_at") is not None
        and _parse_iso(pending["activate_after"], "pending.activate_after") <= now
    ):
        previous = list(state.get("previous", []))
        previous.insert(
            0,
            {
                "generation": state["current"]["generation"],
                "certificate_pem": state["current"]["certificate_pem"],
                "retire_after": _iso(now + timedelta(days=config.root_overlap_days)),
            },
        )
        promoted = dict(pending)
        promoted.pop("activate_after", None)
        promoted.pop("trust_bundle_published_at", None)
        state["current"] = promoted
        state["pending"] = None
        state["previous"] = previous
        changed = True
        LOGGER.info("Promoted root generation %d", promoted["generation"])

    active_previous = [
        item
        for item in state.get("previous", [])
        if _parse_iso(item["retire_after"], "previous.retire_after") > now
    ]
    if len(active_previous) != len(state.get("previous", [])):
        state["previous"] = active_previous
        changed = True
        LOGGER.info("Removed expired previous root certificates from the trust bundle")

    if changed:
        _save_root_state(state)
    _publish_trust_bundle(config, state)

    # Activation delay starts only after SSM accepted a bundle containing the
    # pending root. A prolonged SSM outage therefore cannot consume the safety
    # window and promote a root that warm proxy caches never had a chance to
    # observe. Missing markers on legacy pending records are repaired safely.
    pending = state.get("pending")
    if pending is not None and pending.get("trust_bundle_published_at") is None:
        published_at = _now()
        pending["trust_bundle_published_at"] = _iso(published_at)
        pending["activate_after"] = _iso(
            published_at + timedelta(hours=config.root_activation_delay_hours)
        )
        _save_root_state(state)
        changed = True
        LOGGER.info(
            "Confirmed trust publication for pending root generation %d; activation begins %s",
            pending["generation"],
            pending["activate_after"],
        )
    return state, changed


def _generate_leaf(
    config: ManagerConfig,
    root_record: dict[str, Any],
) -> tuple[bytes, bytes, datetime]:
    root_key, root_certificate = _validate_root_record(root_record, "current")
    now = _now()
    root_expiry = _certificate_not_after(root_certificate)
    requested_expiry = now + timedelta(days=config.leaf_validity_days)
    leaf_expiry = min(requested_expiry, root_expiry - timedelta(days=1))
    if leaf_expiry <= now + timedelta(days=config.leaf_rotate_before_days):
        raise RuntimeError("Current root expires too soon to issue a safe leaf certificate")

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, config.server_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(root_certificate.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(leaf_expiry)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(config.server_name)]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    private_key_pem = leaf_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    return certificate_pem, private_key_pem, leaf_expiry


def _validated_certificate_arn(region: str, value: Any) -> str:
    """Validate one project-registry value before it authorizes mutation."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid ACM certificate ARN stored for {region}")
    certificate_arn = value.strip()
    expected_prefix = (
        f"arn:{os.environ.get('AWS_PARTITION', 'aws')}:acm:{region}:"
        f"{os.environ.get('AWS_ACCOUNT_ID', '')}:certificate/"
    )
    certificate_id = certificate_arn.removeprefix(expected_prefix)
    if (
        not certificate_arn.startswith(expected_prefix)
        or re.fullmatch(r"[A-Za-z0-9-]+", certificate_id) is None
    ):
        raise ValueError(f"Invalid ACM certificate ARN stored for {region}")
    return certificate_arn


def _write_certificate_registry(
    config: ManagerConfig,
    region: str,
    certificate_arn: str,
) -> None:
    """Persist the canonical regional certificate association in SSM."""
    boto3.client("ssm", region_name=config.registry_region).put_parameter(
        Name=config.certificate_parameter_name(region),
        Value=certificate_arn,
        Type="String",
        Overwrite=True,
        Description=f"Regional ACM certificate ARN for GCO backend TLS in {region}",
    )


def _certificate_tags(acm_client: Any, certificate_arn: str) -> dict[str, str]:
    """Return a strict tag map for one ACM certificate."""
    response = acm_client.list_tags_for_certificate(CertificateArn=certificate_arn)
    raw_tags = response.get("Tags", [])
    if not isinstance(raw_tags, list):
        raise ValueError("ACM returned malformed certificate tags")

    tags: dict[str, str] = {}
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, dict):
            raise ValueError("ACM returned a malformed certificate tag")
        key = raw_tag.get("Key")
        value = raw_tag.get("Value")
        if not isinstance(key, str) or not isinstance(value, str) or key in tags:
            raise ValueError("ACM returned a malformed certificate tag")
        tags[key] = value
    return tags


def _certificate_ownership_status(config: ManagerConfig, tags: dict[str, str]) -> str:
    """Classify ownership tags as ``owned``, ``legacy``, or ``conflicting``."""
    expected = {
        "Project": config.project_name,
        "ManagedBy": _MANAGED_BY_TAG_VALUE,
    }
    if any(key in tags and tags[key] != value for key, value in expected.items()):
        return "conflicting"
    if all(tags.get(key) == value for key, value in expected.items()):
        return "owned"
    return "legacy"


def _require_certificate_ownership(
    config: ManagerConfig,
    region: str,
    certificate_arn: str,
    acm_client: Any,
) -> None:
    """Fail closed unless an ACM certificate has both GCO ownership tags."""
    status = _certificate_ownership_status(
        config,
        _certificate_tags(acm_client, certificate_arn),
    )
    if status != "owned":
        raise PermissionError(
            f"Registered ACM certificate for {region} has {status} ownership tags"
        )


def _managed_root_certificates(state: dict[str, Any]) -> tuple[x509.Certificate, ...]:
    """Load every current, pending, and previous root authorized by state."""
    roots: list[x509.Certificate] = []
    _, current = _validate_root_record(state.get("current"), "current")
    roots.append(current)

    pending = state.get("pending")
    if pending is not None:
        _, pending_certificate = _validate_root_record(pending, "pending")
        roots.append(pending_certificate)

    for index, previous in enumerate(state.get("previous", [])):
        if not isinstance(previous, dict) or not isinstance(previous.get("certificate_pem"), str):
            raise ValueError(f"Invalid root state: previous[{index}]")
        try:
            roots.append(
                x509.load_pem_x509_certificate(previous["certificate_pem"].encode("ascii"))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid root state: previous[{index}] certificate") from exc
    return tuple(roots)


def _certificate_has_server_name(
    certificate: x509.Certificate,
    server_name: str,
) -> bool:
    try:
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        return False
    expected = server_name.casefold()
    return any(name.casefold() == expected for name in names)


def _certificate_signed_by_root(
    certificate: x509.Certificate,
    root_certificate: x509.Certificate,
) -> bool:
    if certificate.issuer != root_certificate.subject:
        return False
    root_public_key = root_certificate.public_key()
    signature_algorithm = certificate.signature_hash_algorithm
    if not isinstance(root_public_key, ec.EllipticCurvePublicKey) or signature_algorithm is None:
        return False
    try:
        root_public_key.verify(
            certificate.signature,
            certificate.tbs_certificate_bytes,
            ec.ECDSA(signature_algorithm),
        )
    except InvalidSignature, UnsupportedAlgorithm, TypeError, ValueError:
        return False
    return True


def _recover_unregistered_certificate(
    config: ManagerConfig,
    region: str,
) -> tuple[str | None, x509.Certificate | None]:
    """Adopt the unique tagged ACM leaf left by a failed SSM registration.

    SSM remains the canonical registry. Tagged ACM inventory is the durable
    recovery channel for the narrow case where both that write and the
    compensating delete fail. Ambiguous inventory fails closed rather than
    importing another certificate.
    """
    acm_client = boto3.client("acm", region_name=region)
    managed: list[tuple[str, x509.Certificate]] = []
    paginator = acm_client.get_paginator("list_certificates")
    for page in paginator.paginate(
        CertificateStatuses=list(_CERTIFICATE_STATUSES),
        Includes={"keyTypes": ["EC_prime256v1"]},
    ):
        for summary in page.get("CertificateSummaryList", []):
            if summary.get("Type") not in (None, "IMPORTED"):
                continue
            certificate_arn = _validated_certificate_arn(
                region,
                summary.get("CertificateArn"),
            )
            tags = _certificate_tags(acm_client, certificate_arn)
            if _certificate_ownership_status(config, tags) != "owned":
                continue
            detail = acm_client.describe_certificate(CertificateArn=certificate_arn)
            if detail.get("Certificate", {}).get("Type") != "IMPORTED":
                raise RuntimeError(
                    f"Managed ACM certificate inventory for {region} contains a non-imported leaf"
                )
            certificate_response = acm_client.get_certificate(CertificateArn=certificate_arn)
            certificate_pem = certificate_response.get("Certificate")
            if not isinstance(certificate_pem, str):
                raise ValueError(f"ACM did not return a managed imported certificate for {region}")
            managed.append(
                (
                    certificate_arn,
                    x509.load_pem_x509_certificate(certificate_pem.encode("ascii")),
                )
            )

    if len(managed) > 1:
        raise RuntimeError(
            f"Multiple unregistered managed ACM certificates were found for {region}"
        )
    if not managed:
        return None, None

    certificate_arn, certificate = managed[0]
    _write_certificate_registry(config, region, certificate_arn)
    LOGGER.warning(
        "Recovered the tagged backend leaf certificate registry association in %s",
        region,
    )
    return certificate_arn, certificate


def _registered_certificate(
    config: ManagerConfig,
    region: str,
    *,
    migration_roots: tuple[x509.Certificate, ...] | None = None,
) -> tuple[str | None, x509.Certificate | None]:
    """Load a registered leaf, optionally adopting a proven legacy leaf.

    Strict callers, including every cleanup path, omit ``migration_roots`` and
    therefore reject missing or conflicting ownership tags. Reconciliation may
    pass the roots from its authenticated secret state; only then can an
    untagged legacy imported leaf be tagged after its SAN and signature prove
    that it belongs to this deployment.
    """
    parameter_name = config.certificate_parameter_name(region)
    ssm_client = boto3.client("ssm", region_name=config.registry_region)
    try:
        response = ssm_client.get_parameter(Name=parameter_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None, None
        raise
    certificate_arn = _validated_certificate_arn(
        region,
        response.get("Parameter", {}).get("Value"),
    )

    acm_client = boto3.client("acm", region_name=region)
    ownership = _certificate_ownership_status(
        config,
        _certificate_tags(acm_client, certificate_arn),
    )
    if ownership == "conflicting":
        raise PermissionError(
            f"Registered ACM certificate for {region} has conflicting ownership tags"
        )
    if ownership == "legacy" and migration_roots is None:
        raise PermissionError(f"Registered ACM certificate for {region} is missing ownership tags")

    if ownership == "legacy":
        detail = acm_client.describe_certificate(CertificateArn=certificate_arn)
        certificate_detail = detail.get("Certificate")
        if not isinstance(certificate_detail, dict) or certificate_detail.get("Type") != "IMPORTED":
            raise PermissionError(
                f"Legacy ACM certificate for {region} is not an imported certificate"
            )

    response = acm_client.get_certificate(CertificateArn=certificate_arn)
    certificate_pem = response.get("Certificate")
    if not isinstance(certificate_pem, str):
        raise ValueError(f"ACM did not return the imported certificate for {region}")
    certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))

    if ownership == "legacy":
        assert migration_roots is not None
        if not _certificate_has_server_name(certificate, config.server_name) or not any(
            _certificate_signed_by_root(certificate, root) for root in migration_roots
        ):
            raise PermissionError(
                f"Legacy ACM certificate for {region} is not a managed backend leaf"
            )
        acm_client.add_tags_to_certificate(
            CertificateArn=certificate_arn,
            Tags=[
                {"Key": "Project", "Value": config.project_name},
                {"Key": "ManagedBy", "Value": _MANAGED_BY_TAG_VALUE},
            ],
        )
        LOGGER.warning(
            "Migrated cryptographically verified legacy backend leaf ownership in %s",
            region,
        )

    return certificate_arn, certificate


def _leaf_needs_rotation(
    config: ManagerConfig,
    certificate: x509.Certificate | None,
    root_certificate: x509.Certificate,
) -> bool:
    if certificate is None:
        return True
    if _certificate_not_after(certificate) <= _now() + timedelta(
        days=config.leaf_rotate_before_days
    ):
        return True
    return not _certificate_signed_by_root(
        certificate,
        root_certificate,
    ) or not _certificate_has_server_name(certificate, config.server_name)


def _ensure_certificate(
    config: ManagerConfig,
    state: dict[str, Any],
    region: str,
) -> tuple[str, datetime, bool]:
    _, root_certificate = _validate_root_record(state["current"], "current")
    certificate_arn, existing_certificate = _registered_certificate(
        config,
        region,
        migration_roots=_managed_root_certificates(state),
    )
    if certificate_arn is None:
        certificate_arn, existing_certificate = _recover_unregistered_certificate(
            config,
            region,
        )
    if not _leaf_needs_rotation(config, existing_certificate, root_certificate):
        assert certificate_arn is not None
        assert existing_certificate is not None
        return certificate_arn, _certificate_not_after(existing_certificate), False

    certificate_pem, private_key_pem, leaf_expiry = _generate_leaf(config, state["current"])
    acm_client = boto3.client("acm", region_name=region)
    import_args: dict[str, Any] = {
        "Certificate": certificate_pem,
        "PrivateKey": private_key_pem,
    }
    if certificate_arn is not None:
        import_args["CertificateArn"] = certificate_arn
    else:
        import_args["Tags"] = [
            {"Key": "Project", "Value": config.project_name},
            {"Key": "ManagedBy", "Value": _MANAGED_BY_TAG_VALUE},
        ]
    response = acm_client.import_certificate(**import_args)
    imported_arn = str(response["CertificateArn"])
    try:
        _write_certificate_registry(config, region, imported_arn)
    except Exception:  # noqa: BLE001 - compensate every failed registry write
        # A first import has no stable ARN until the registry write succeeds.
        # Delete only that newly-created certificate so a retry cannot leak an
        # undiscoverable managed certificate or create another orphan. A
        # reimport of an existing ARN must never be deleted on registry failure.
        if certificate_arn is None:
            try:
                acm_client.delete_certificate(CertificateArn=imported_arn)
            except Exception:  # noqa: BLE001 - retain the original SSM failure
                LOGGER.exception(
                    "Could not remove unregistered backend leaf certificate in %s",
                    region,
                )
        raise
    LOGGER.info(
        "%s backend leaf certificate in %s; ACM ARN association remains stable",
        "Reimported" if certificate_arn else "Imported",
        region,
    )
    return imported_arn, leaf_expiry, True


def _publish_expiry_metrics(
    config: ManagerConfig,
    certificate_expiries: dict[str, datetime],
    root_expiry: datetime,
) -> None:
    now = _now()
    metric_data = [
        {
            "MetricName": "ReconciliationSuccess",
            "Dimensions": [{"Name": "Project", "Value": config.project_name}],
            "Value": 1.0,
            "Unit": "Count",
        },
        {
            "MetricName": "RootCertificateDaysToExpiry",
            "Dimensions": [{"Name": "Project", "Value": config.project_name}],
            "Value": max(0.0, (root_expiry - now).total_seconds() / 86400),
            "Unit": "Count",
        },
    ]
    metric_data.extend(
        {
            "MetricName": "LeafCertificateDaysToExpiry",
            "Dimensions": [
                {"Name": "Project", "Value": config.project_name},
                {"Name": "Region", "Value": region},
            ],
            "Value": max(0.0, (expiry - now).total_seconds() / 86400),
            "Unit": "Count",
        }
        for region, expiry in certificate_expiries.items()
    )
    try:
        boto3.client("cloudwatch").put_metric_data(
            Namespace="GCO/BackendTLS",
            MetricData=metric_data,
        )
    except (BotoCoreError, ClientError) as exc:
        LOGGER.warning("Could not publish backend TLS expiry metrics: %s", exc)


def _reconcile(
    config: ManagerConfig,
    newly_retired: tuple[str, ...] = (),
) -> dict[str, Any]:
    state, root_changed = _ensure_root(config)
    expiries: dict[str, datetime] = {}
    rotated_regions: list[str] = []
    for region in config.regions:
        _, expiry, rotated = _ensure_certificate(config, state, region)
        expiries[region] = expiry
        if rotated:
            rotated_regions.append(region)

    cleaned_retired, pending_retired = _retry_retired_region_cleanup(
        config,
        state,
        newly_retired,
    )
    _, root_certificate = _validate_root_record(state["current"], "current")
    _publish_expiry_metrics(config, expiries, _certificate_not_after(root_certificate))
    return {
        "RootChanged": root_changed,
        "RotatedRegions": rotated_regions,
        "ManagedRegionCount": len(config.regions),
        "CleanedRetiredRegions": cleaned_retired,
        "PendingRetiredRegions": pending_retired,
    }


def _event_regions(properties: Any, field: str) -> tuple[str, ...]:
    """Return a strict custom-resource region list without silent coercion."""
    if not isinstance(properties, dict):
        raise ValueError(f"{field} must be an object")
    raw_regions = properties.get("Regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise ValueError(f"{field}.Regions must be a non-empty list")
    regions: list[str] = []
    for value in raw_regions:
        if not isinstance(value, str):
            raise ValueError(f"{field}.Regions contains an invalid region")
        region = value.strip()
        if _REGION_RE.fullmatch(region) is None:
            raise ValueError(f"{field}.Regions contains an invalid region")
        if region in regions:
            raise ValueError(f"{field}.Regions contains a duplicate region")
        regions.append(region)
    return tuple(regions)


def _retired_regions_from_update(
    event: dict[str, Any],
    config: ManagerConfig,
) -> tuple[str, ...]:
    old_regions = _event_regions(event.get("OldResourceProperties"), "OldResourceProperties")
    current_regions = set(config.regions)
    return tuple(region for region in old_regions if region not in current_regions)


def _delete_parameter(client: Any, name: str) -> None:
    try:
        client.delete_parameter(Name=name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise


def _certificate_registry_regions(config: ManagerConfig) -> frozenset[str]:
    """Discover and validate every managed regional certificate parameter.

    Inventory and ownership validation finish before cleanup mutates anything.
    Names must be exact direct children of the project prefix, values must be
    account/partition/Region-scoped ACM ARNs, and every referenced certificate
    must already carry both expected ownership tags. Cleanup never performs
    legacy tag migration.
    """
    client = boto3.client("ssm", region_name=config.registry_region)
    certificates: dict[str, str] = {}
    seen_tokens: set[str] = set()
    next_token: str | None = None

    while True:
        request: dict[str, Any] = {
            "Path": config.certificate_parameter_prefix,
            "Recursive": True,
            "WithDecryption": False,
        }
        if next_token is not None:
            request["NextToken"] = next_token
        response = client.get_parameters_by_path(**request)
        parameters = response.get("Parameters")
        if not isinstance(parameters, list):
            raise ValueError("Certificate registry inventory returned malformed parameters")

        for parameter in parameters:
            if not isinstance(parameter, dict):
                raise ValueError("Certificate registry inventory contains a malformed entry")
            name = parameter.get("Name")
            if not isinstance(name, str) or not name.startswith(
                config.certificate_parameter_prefix
            ):
                raise ValueError("Certificate registry parameter is outside the project prefix")
            region = name.removeprefix(config.certificate_parameter_prefix)
            if _REGION_RE.fullmatch(region) is None or name != config.certificate_parameter_name(
                region
            ):
                raise ValueError(f"Malformed certificate registry parameter name: {name!r}")
            if region in certificates:
                raise ValueError(f"Duplicate certificate registry parameter for {region}")
            certificates[region] = _validated_certificate_arn(
                region,
                parameter.get("Value"),
            )

        token = response.get("NextToken")
        if token is None:
            break
        if not isinstance(token, str) or not token or token in seen_tokens:
            raise ValueError("Certificate registry inventory returned an invalid pagination token")
        seen_tokens.add(token)
        next_token = token

    for region, certificate_arn in certificates.items():
        _require_certificate_ownership(
            config,
            region,
            certificate_arn,
            boto3.client("acm", region_name=region),
        )
    return frozenset(certificates)


def _delete_regional_certificate(
    config: ManagerConfig,
    region: str,
    *,
    defer_in_use: bool,
) -> bool:
    """Delete one managed certificate and its ARN parameter when safe.

    ACM refuses deletion while an ALB listener still uses the certificate. An
    Update or scheduled reconciliation preserves the parameter and returns
    ``False`` so durable retired-region state can retry later. Delete events
    fail instead: once the custom resource disappears no scheduler remains to
    finish cleanup.
    """
    certificate_arn, _ = _registered_certificate(config, region)
    if certificate_arn is None:
        # A failed first import can leave a tagged ACM certificate without its
        # canonical SSM association when both registration and compensation
        # fail. Recover that association before deletion so CloudFormation
        # cleanup cannot strand the durable orphan that reconciliation knows
        # how to adopt.
        certificate_arn, _ = _recover_unregistered_certificate(config, region)
    if certificate_arn is not None:
        try:
            boto3.client("acm", region_name=region).delete_certificate(
                CertificateArn=certificate_arn
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ResourceInUseException" and defer_in_use:
                LOGGER.info(
                    "Backend certificate in retired region %s is still attached; "
                    "retaining its ARN for scheduled cleanup",
                    region,
                )
                return False
            if code != "ResourceNotFoundException":
                raise
    _delete_parameter(
        boto3.client("ssm", region_name=config.registry_region),
        config.certificate_parameter_name(region),
    )
    return True


def _retry_retired_region_cleanup(
    config: ManagerConfig,
    state: dict[str, Any],
    newly_retired: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    """Persist, retry, and remove retired regions only after complete cleanup."""
    active_regions = set(config.regions)
    pending = sorted((set(state.get("retired_regions", [])) | set(newly_retired)) - active_regions)
    if pending != state.get("retired_regions", []):
        state["retired_regions"] = pending
        _save_root_state(state)

    remaining: list[str] = []
    cleaned: list[str] = []
    for region in pending:
        if _delete_regional_certificate(config, region, defer_in_use=True):
            cleaned.append(region)
        else:
            remaining.append(region)
    if remaining != pending:
        state["retired_regions"] = remaining
        _save_root_state(state)
    return cleaned, remaining


def _cleanup(config: ManagerConfig) -> None:
    state = _load_root_state()
    registry_regions = _certificate_registry_regions(config)
    regions = set(config.regions) | set(registry_regions)
    if state is not None:
        regions.update(state.get("retired_regions", []))

    for region in sorted(regions):
        _delete_regional_certificate(config, region, defer_in_use=False)

    ssm_client = boto3.client("ssm", region_name=config.registry_region)
    _delete_parameter(ssm_client, config.root_ca_parameter_name)
    if state is not None and state.get("retired_regions"):
        state["retired_regions"] = []
        _save_root_state(state)
    LOGGER.info("Removed current and retired ACM certificates and public backend TLS parameters")


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Handle scheduled reconciliation and CDK provider lifecycle events."""
    config = ManagerConfig.from_event(event)
    if event.get("Action") == "Rotate":
        LOGGER.info("Running scheduled backend TLS reconciliation")
        return _reconcile(config)

    request_type = event.get("RequestType")
    physical_id = event.get("PhysicalResourceId") or (
        f"{config.project_name}-backend-tls-certificates"
    )
    if request_type == "Delete":
        _cleanup(config)
        return {"PhysicalResourceId": physical_id}
    if request_type not in {"Create", "Update"}:
        raise ValueError(f"Unsupported certificate manager event: {request_type!r}")

    newly_retired = _retired_regions_from_update(event, config) if request_type == "Update" else ()
    result = _reconcile(config, newly_retired)
    return {
        "PhysicalResourceId": physical_id,
        "Data": result,
    }
