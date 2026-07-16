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
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/tls-certificate-manager/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/tls-certificate-manager/handler.lambda_handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)

_SCHEMA_VERSION = 1
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-[0-9]+$")
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
    if value is not None:
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
    previous = state.get("previous", [])
    if not isinstance(previous, list):
        raise ValueError("Invalid root state: previous must be a list")
    for index, item in enumerate(previous):
        if not isinstance(item, dict) or not isinstance(item.get("certificate_pem"), str):
            raise ValueError(f"Invalid root state: previous[{index}]")
        x509.load_pem_x509_certificate(item["certificate_pem"].encode("ascii"))
        _parse_iso(item.get("retire_after"), f"previous[{index}].retire_after")
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
        }
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


def _registered_certificate(
    config: ManagerConfig,
    region: str,
) -> tuple[str | None, x509.Certificate | None]:
    parameter_name = config.certificate_parameter_name(region)
    ssm_client = boto3.client("ssm", region_name=config.registry_region)
    try:
        response = ssm_client.get_parameter(Name=parameter_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return None, None
        raise
    certificate_arn = str(response.get("Parameter", {}).get("Value", "")).strip()
    expected_prefix = (
        f"arn:{os.environ.get('AWS_PARTITION', 'aws')}:acm:{region}:"
        f"{os.environ.get('AWS_ACCOUNT_ID', '')}:certificate/"
    )
    if not certificate_arn.startswith(expected_prefix):
        raise ValueError(f"Invalid ACM certificate ARN stored for {region}")

    acm_client = boto3.client("acm", region_name=region)
    try:
        response = acm_client.get_certificate(CertificateArn=certificate_arn)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None, None
        raise
    certificate_pem = response.get("Certificate")
    if not isinstance(certificate_pem, str):
        raise ValueError(f"ACM did not return the imported certificate for {region}")
    return certificate_arn, x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))


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
    if certificate.issuer != root_certificate.subject:
        return True
    try:
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        return True
    return config.server_name not in names


def _ensure_certificate(
    config: ManagerConfig,
    state: dict[str, Any],
    region: str,
) -> tuple[str, datetime, bool]:
    _, root_certificate = _validate_root_record(state["current"], "current")
    certificate_arn, existing_certificate = _registered_certificate(config, region)
    if not _leaf_needs_rotation(config, existing_certificate, root_certificate):
        assert certificate_arn is not None
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
            {"Key": "ManagedBy", "Value": "gco-backend-tls-manager"},
        ]
    response = acm_client.import_certificate(**import_args)
    imported_arn = str(response["CertificateArn"])
    boto3.client("ssm", region_name=config.registry_region).put_parameter(
        Name=config.certificate_parameter_name(region),
        Value=imported_arn,
        Type="String",
        Overwrite=True,
        Description=f"Regional ACM certificate ARN for GCO backend TLS in {region}",
    )
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
            "MetricName": "RootCertificateDaysToExpiry",
            "Dimensions": [{"Name": "Project", "Value": config.project_name}],
            "Value": max(0.0, (root_expiry - now).total_seconds() / 86400),
            "Unit": "Count",
        }
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


def _reconcile(config: ManagerConfig) -> dict[str, Any]:
    state, root_changed = _ensure_root(config)
    expiries: dict[str, datetime] = {}
    rotated_regions: list[str] = []
    for region in config.regions:
        _, expiry, rotated = _ensure_certificate(config, state, region)
        expiries[region] = expiry
        if rotated:
            rotated_regions.append(region)
    _, root_certificate = _validate_root_record(state["current"], "current")
    _publish_expiry_metrics(config, expiries, _certificate_not_after(root_certificate))
    return {
        "RootChanged": root_changed,
        "RotatedRegions": rotated_regions,
        "ManagedRegionCount": len(config.regions),
    }


def _delete_parameter(client: Any, name: str) -> None:
    try:
        client.delete_parameter(Name=name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise


def _cleanup(config: ManagerConfig) -> None:
    ssm_client = boto3.client("ssm", region_name=config.registry_region)
    for region in config.regions:
        certificate_arn, _ = _registered_certificate(config, region)
        if certificate_arn is not None:
            try:
                boto3.client("acm", region_name=region).delete_certificate(
                    CertificateArn=certificate_arn
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    raise
        _delete_parameter(ssm_client, config.certificate_parameter_name(region))
    _delete_parameter(ssm_client, config.root_ca_parameter_name)
    LOGGER.info("Removed regional ACM certificates and public backend TLS parameters")


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

    result = _reconcile(config)
    return {
        "PhysicalResourceId": physical_id,
        "Data": result,
    }
