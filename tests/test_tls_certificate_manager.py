"""Focused failure-compensation tests for the backend TLS certificate manager."""

import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from tests._lambda_imports import load_lambda_module


def _manager_config(handler):
    return handler.ManagerConfig(
        regions=("us-west-2",),
        server_name="backend.gco-test.gco.internal",
        project_name="gco-test",
        registry_region="us-east-1",
        root_ca_parameter_name="/gco-test/backend-tls/root-ca.pem",
        certificate_parameter_prefix="/gco-test/backend-tls/certificate-arn/",
        root_generation=1,
        root_validity_days=3_650,
        root_rotate_before_days=180,
        root_activation_delay_hours=24,
        root_overlap_days=45,
        leaf_validity_days=30,
        leaf_rotate_before_days=10,
    )


def test_pending_root_activation_waits_for_confirmed_trust_publication() -> None:
    """A failed SSM publish cannot consume the root propagation window."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    now = datetime(2027, 1, 1, tzinfo=UTC)

    with patch.object(handler, "_now", return_value=now):
        current = handler._generate_root(config, 1)
        pending = handler._generate_root(config, 2)
        pending["activate_after"] = handler._iso(now - timedelta(hours=1))
        state = {
            "schema_version": handler._SCHEMA_VERSION,
            "current": current,
            "pending": pending,
            "previous": [],
            "retired_regions": [],
        }
        with (
            patch.object(handler, "_load_root_state", return_value=state),
            patch.object(handler, "_publish_trust_bundle") as publish,
            patch.object(handler, "_save_root_state") as save,
        ):
            reconciled, changed = handler._ensure_root(config)

    publish.assert_called_once_with(config, state)
    save.assert_called_once_with(state)
    assert changed is True
    assert reconciled["current"]["generation"] == 1
    assert reconciled["pending"]["trust_bundle_published_at"] == handler._iso(now)
    assert reconciled["pending"]["activate_after"] == handler._iso(now + timedelta(hours=24))


def test_unpublished_pending_root_is_not_marked_when_ssm_fails() -> None:
    """Publication failure leaves the pending root unconfirmed and unpromoted."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    now = datetime(2027, 1, 1, tzinfo=UTC)

    with patch.object(handler, "_now", return_value=now):
        current = handler._generate_root(config, 1)
        pending = handler._generate_root(config, 2)
        pending["activate_after"] = handler._iso(now - timedelta(hours=1))
        state = {
            "schema_version": handler._SCHEMA_VERSION,
            "current": current,
            "pending": pending,
            "previous": [],
            "retired_regions": [],
        }
        with (
            patch.object(handler, "_load_root_state", return_value=state),
            patch.object(
                handler,
                "_publish_trust_bundle",
                side_effect=RuntimeError("ssm unavailable"),
            ),
            patch.object(handler, "_save_root_state") as save,
            pytest.raises(RuntimeError, match="ssm unavailable"),
        ):
            handler._ensure_root(config)

    save.assert_not_called()
    assert state["current"]["generation"] == 1
    assert "trust_bundle_published_at" not in state["pending"]


def test_leaf_rotation_verifies_signature_not_only_issuer_name() -> None:
    """A same-subject certificate signed by another key must be replaced."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    now = datetime(2027, 1, 1, tzinfo=UTC)

    with patch.object(handler, "_now", return_value=now):
        issuing_root = handler._generate_root(config, 1)
        _, issuing_certificate = handler._validate_root_record(issuing_root, "current")
        certificate_pem, _, _ = handler._generate_leaf(config, issuing_root)
        certificate = x509.load_pem_x509_certificate(certificate_pem)
        assert not handler._leaf_needs_rotation(config, certificate, issuing_certificate)

        same_subject_different_key = handler._generate_root(config, 1)
        _, other_certificate = handler._validate_root_record(same_subject_different_key, "current")
        assert handler._leaf_needs_rotation(config, certificate, other_certificate)


def test_expiry_metrics_include_reconciliation_heartbeat() -> None:
    """Every successful reconcile emits the heartbeat used to detect silence."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    now = datetime(2027, 1, 1, tzinfo=UTC)
    cloudwatch = MagicMock()

    with (
        patch.object(handler, "_now", return_value=now),
        patch.object(handler.boto3, "client", return_value=cloudwatch),
    ):
        handler._publish_expiry_metrics(
            config,
            {"us-west-2": now + timedelta(days=30)},
            now + timedelta(days=3_650),
        )

    metric_data = cloudwatch.put_metric_data.call_args.kwargs["MetricData"]
    assert {metric["MetricName"] for metric in metric_data} == {
        "ReconciliationSuccess",
        "RootCertificateDaysToExpiry",
        "LeafCertificateDaysToExpiry",
    }


def test_removed_region_is_retried_then_delete_includes_persisted_retirements() -> None:
    """Update records in-use regions, Rotate retries, and Delete unions state."""
    handler = load_lambda_module("tls-certificate-manager")
    config = replace(_manager_config(handler), regions=("us-east-1",))
    now = datetime(2027, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    state = {"current": {}, "retired_regions": []}
    delete_calls: list[tuple[str, bool]] = []
    retired_attempts = 0

    def delete_region(_config, region: str, *, defer_in_use: bool) -> bool:
        nonlocal retired_attempts
        delete_calls.append((region, defer_in_use))
        if region == "us-west-2" and defer_in_use:
            retired_attempts += 1
            return retired_attempts > 1
        return True

    with (
        patch.object(handler.ManagerConfig, "from_event", return_value=config),
        patch.object(handler, "_ensure_root", return_value=(state, False)),
        patch.object(
            handler,
            "_ensure_certificate",
            return_value=("arn:certificate", expiry, False),
        ),
        patch.object(handler, "_validate_root_record", return_value=(MagicMock(), MagicMock())),
        patch.object(handler, "_certificate_not_after", return_value=expiry),
        patch.object(handler, "_publish_expiry_metrics"),
        patch.object(handler, "_delete_regional_certificate", side_effect=delete_region),
        patch.object(handler, "_save_root_state") as save_state,
        patch.object(handler, "_load_root_state", return_value=state),
        patch.object(handler, "_certificate_registry_regions", return_value=frozenset()),
        patch.object(handler, "_delete_parameter") as delete_parameter,
        patch.object(handler.boto3, "client", return_value=MagicMock()),
    ):
        update_result = handler.lambda_handler(
            {
                "RequestType": "Update",
                "PhysicalResourceId": "gco-test-backend-tls-certificates",
                "OldResourceProperties": {"Regions": ["us-east-1", "us-west-2"]},
            },
            None,
        )
        assert update_result["Data"]["PendingRetiredRegions"] == ["us-west-2"]
        assert state["retired_regions"] == ["us-west-2"]

        rotate_result = handler.lambda_handler({"Action": "Rotate"}, None)
        assert rotate_result["CleanedRetiredRegions"] == ["us-west-2"]
        assert rotate_result["PendingRetiredRegions"] == []
        assert state["retired_regions"] == []
        assert retired_attempts == 2

        state["retired_regions"] = ["eu-west-1"]
        delete_calls.clear()
        handler.lambda_handler(
            {
                "RequestType": "Delete",
                "PhysicalResourceId": "gco-test-backend-tls-certificates",
            },
            None,
        )

    assert set(delete_calls) == {("eu-west-1", False), ("us-east-1", False)}
    assert state["retired_regions"] == []
    assert save_state.call_count >= 3
    delete_parameter.assert_called_once_with(
        ANY,
        "/gco-test/backend-tls/root-ca.pem",
    )


def test_resource_in_use_retains_retired_certificate_parameter() -> None:
    """An attached retired leaf stays discoverable for the scheduled retry."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    certificate_arn = "arn:aws:acm:us-west-2:123456789012:certificate/stable"
    acm_client = MagicMock()
    acm_client.delete_certificate.side_effect = ClientError(
        {"Error": {"Code": "ResourceInUseException", "Message": "attached"}},
        "DeleteCertificate",
    )
    ssm_client = MagicMock()

    def client(service_name: str, *, region_name: str):
        if service_name == "acm":
            assert region_name == "us-west-2"
            return acm_client
        if service_name == "ssm":
            assert region_name == "us-east-1"
            return ssm_client
        raise AssertionError(f"Unexpected client: {service_name}")

    with (
        patch.object(
            handler, "_registered_certificate", return_value=(certificate_arn, MagicMock())
        ),
        patch.object(handler.boto3, "client", side_effect=client),
    ):
        assert (
            handler._delete_regional_certificate(
                config,
                "us-west-2",
                defer_in_use=True,
            )
            is False
        )

    ssm_client.delete_parameter.assert_not_called()


def test_first_import_is_deleted_when_registry_write_fails() -> None:
    """A failed first ARN registration must not orphan an ACM certificate."""
    handler = load_lambda_module("tls-certificate-manager")
    config = MagicMock()
    config.project_name = "gco-test"
    config.registry_region = "us-east-1"
    config.certificate_parameter_name.return_value = "/gco-test/tls/us-west-2/certificate-arn"

    imported_arn = "arn:aws:acm:us-west-2:123456789012:certificate/new-certificate"
    acm_client = MagicMock()
    acm_client.import_certificate.return_value = {"CertificateArn": imported_arn}
    ssm_client = MagicMock()
    ssm_client.put_parameter.side_effect = RuntimeError("registry unavailable")

    def client(service_name: str, *, region_name: str):
        if service_name == "acm":
            assert region_name == "us-west-2"
            return acm_client
        if service_name == "ssm":
            assert region_name == "us-east-1"
            return ssm_client
        raise AssertionError(f"Unexpected client: {service_name}")

    expiry = datetime(2027, 1, 1, tzinfo=UTC)
    with (
        patch.object(handler, "_validate_root_record", return_value=(MagicMock(), MagicMock())),
        patch.object(handler, "_registered_certificate", return_value=(None, None)),
        patch.object(
            handler,
            "_recover_unregistered_certificate",
            return_value=(None, None),
        ),
        patch.object(handler, "_leaf_needs_rotation", return_value=True),
        patch.object(
            handler,
            "_generate_leaf",
            return_value=(b"certificate", b"private-key", expiry),
        ),
        patch.object(handler.boto3, "client", side_effect=client),
        pytest.raises(RuntimeError, match="registry unavailable"),
    ):
        handler._ensure_certificate(config, {"current": {}}, "us-west-2")

    acm_client.delete_certificate.assert_called_once_with(CertificateArn=imported_arn)


def test_tagged_unregistered_certificate_is_adopted_before_import() -> None:
    """A dual SSM/delete failure leaves a uniquely tagged, recoverable ARN."""
    handler = load_lambda_module("tls-certificate-manager")
    config = MagicMock()
    config.project_name = "gco-test"
    config.registry_region = "us-east-1"
    config.certificate_parameter_name.return_value = "/gco-test/tls/us-west-2/certificate-arn"

    certificate_arn = "arn:aws:acm:us-west-2:123456789012:certificate/orphaned-leaf"
    acm_client = MagicMock()
    acm_client.get_paginator.return_value.paginate.return_value = [
        {"CertificateSummaryList": [{"CertificateArn": certificate_arn, "Type": "IMPORTED"}]}
    ]
    acm_client.list_tags_for_certificate.return_value = {
        "Tags": [
            {"Key": "Project", "Value": "gco-test"},
            {"Key": "ManagedBy", "Value": "gco-backend-tls-manager"},
        ]
    }
    acm_client.describe_certificate.return_value = {"Certificate": {"Type": "IMPORTED"}}
    acm_client.get_certificate.return_value = {"Certificate": "certificate-pem"}
    ssm_client = MagicMock()

    def client(service_name: str, *, region_name: str):
        if service_name == "acm":
            assert region_name == "us-west-2"
            return acm_client
        if service_name == "ssm":
            assert region_name == "us-east-1"
            return ssm_client
        raise AssertionError(f"Unexpected client: {service_name}")

    certificate = MagicMock()
    with (
        patch.dict(
            "os.environ",
            {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": "123456789012"},
        ),
        patch.object(handler.boto3, "client", side_effect=client),
        patch.object(handler.x509, "load_pem_x509_certificate", return_value=certificate),
    ):
        assert handler._recover_unregistered_certificate(config, "us-west-2") == (
            certificate_arn,
            certificate,
        )

    paginator = acm_client.get_paginator.return_value
    paginator.paginate.assert_called_once_with(
        CertificateStatuses=list(handler._CERTIFICATE_STATUSES),
        Includes={"keyTypes": ["EC_prime256v1"]},
    )
    ssm_client.put_parameter.assert_called_once_with(
        Name="/gco-test/tls/us-west-2/certificate-arn",
        Value=certificate_arn,
        Type="String",
        Overwrite=True,
        Description="Regional ACM certificate ARN for GCO backend TLS in us-west-2",
    )


def test_existing_certificate_is_not_deleted_when_registry_write_fails() -> None:
    """Registry failure after an in-place reimport must preserve the stable ARN."""
    handler = load_lambda_module("tls-certificate-manager")
    config = MagicMock()
    config.project_name = "gco-test"
    config.registry_region = "us-east-1"
    config.certificate_parameter_name.return_value = "/gco-test/tls/us-west-2/certificate-arn"

    existing_arn = "arn:aws:acm:us-west-2:123456789012:certificate/stable-certificate"
    acm_client = MagicMock()
    acm_client.import_certificate.return_value = {"CertificateArn": existing_arn}
    ssm_client = MagicMock()
    ssm_client.put_parameter.side_effect = RuntimeError("registry unavailable")

    def client(service_name: str, *, region_name: str):
        del region_name
        return acm_client if service_name == "acm" else ssm_client

    expiry = datetime(2027, 1, 1, tzinfo=UTC)
    with (
        patch.object(handler, "_validate_root_record", return_value=(MagicMock(), MagicMock())),
        patch.object(
            handler,
            "_registered_certificate",
            return_value=(existing_arn, MagicMock()),
        ),
        patch.object(handler, "_leaf_needs_rotation", return_value=True),
        patch.object(
            handler,
            "_generate_leaf",
            return_value=(b"certificate", b"private-key", expiry),
        ),
        patch.object(handler.boto3, "client", side_effect=client),
        pytest.raises(RuntimeError, match="registry unavailable"),
    ):
        handler._ensure_certificate(config, {"current": {}}, "us-west-2")

    acm_client.delete_certificate.assert_not_called()


@pytest.mark.parametrize("root_state", ["missing", "empty", "uninitialized"])
def test_cleanup_recovers_registry_inventory_without_root_state(root_state: str) -> None:
    """Missing bootstrap state cannot hide a deferred regional certificate."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    account_id = "123456789012"
    retired_region = "eu-west-1"
    secrets = MagicMock()
    if root_state == "missing":
        secrets.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
            "GetSecretValue",
        )
    elif root_state == "empty":
        secrets.get_secret_value.return_value = {"SecretString": ""}
    else:
        secrets.get_secret_value.return_value = {
            "SecretString": json.dumps({"state": "UNINITIALIZED"})
        }

    ssm = MagicMock()
    ssm.get_parameters_by_path.return_value = {
        "Parameters": [
            {
                "Name": config.certificate_parameter_name(retired_region),
                "Value": (
                    f"arn:aws:acm:{retired_region}:{account_id}:certificate/retired-certificate"
                ),
            }
        ]
    }

    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {
        "Tags": [
            {"Key": "Project", "Value": config.project_name},
            {"Key": "ManagedBy", "Value": "gco-backend-tls-manager"},
        ]
    }

    def client(service_name: str, **kwargs):
        if service_name == "secretsmanager":
            assert kwargs == {}
            return secrets
        if service_name == "ssm":
            assert kwargs == {"region_name": config.registry_region}
            return ssm
        if service_name == "acm":
            assert kwargs == {"region_name": retired_region}
            return acm
        raise AssertionError(f"Unexpected client: {service_name}")

    with (
        patch.dict(
            os.environ,
            {
                "ROOT_SECRET_ARN": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
                "AWS_PARTITION": "aws",
                "AWS_ACCOUNT_ID": account_id,
            },
        ),
        patch.object(handler.boto3, "client", side_effect=client),
        patch.object(handler, "_delete_regional_certificate", return_value=True) as delete_region,
    ):
        handler._cleanup(config)

    assert {item.args[1] for item in delete_region.call_args_list} == {
        "us-west-2",
        retired_region,
    }
    assert all(item.kwargs == {"defer_in_use": False} for item in delete_region.call_args_list)
    ssm.delete_parameter.assert_called_once_with(Name=config.root_ca_parameter_name)


def test_certificate_registry_inventory_is_paginated_and_validated() -> None:
    """Cleanup inventory consumes every SSM page before returning Regions."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    account_id = "123456789012"
    ssm = MagicMock()
    ssm.get_parameters_by_path.side_effect = [
        {
            "Parameters": [
                {
                    "Name": config.certificate_parameter_name("us-west-2"),
                    "Value": (
                        f"arn:aws:acm:us-west-2:{account_id}:certificate/current-certificate"
                    ),
                }
            ],
            "NextToken": "page-2",
        },
        {
            "Parameters": [
                {
                    "Name": config.certificate_parameter_name("eu-west-1"),
                    "Value": (
                        f"arn:aws:acm:eu-west-1:{account_id}:certificate/retired-certificate"
                    ),
                }
            ]
        },
    ]

    acm_clients = {region: MagicMock() for region in ("us-west-2", "eu-west-1")}
    for acm in acm_clients.values():
        acm.list_tags_for_certificate.return_value = {
            "Tags": [
                {"Key": "Project", "Value": config.project_name},
                {"Key": "ManagedBy", "Value": "gco-backend-tls-manager"},
            ]
        }

    def client(service_name: str, *, region_name: str):
        if service_name == "ssm":
            assert region_name == config.registry_region
            return ssm
        if service_name == "acm":
            return acm_clients[region_name]
        raise AssertionError(f"Unexpected client: {service_name}")

    with (
        patch.dict(
            os.environ,
            {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": account_id},
        ),
        patch.object(handler.boto3, "client", side_effect=client),
    ):
        regions = handler._certificate_registry_regions(config)

    assert regions == frozenset({"us-west-2", "eu-west-1"})
    first_request = ssm.get_parameters_by_path.call_args_list[0].kwargs
    second_request = ssm.get_parameters_by_path.call_args_list[1].kwargs
    assert first_request == {
        "Path": config.certificate_parameter_prefix,
        "Recursive": True,
        "WithDecryption": False,
    }
    assert second_request == {**first_request, "NextToken": "page-2"}
    for region, certificate_id in (
        ("us-west-2", "current-certificate"),
        ("eu-west-1", "retired-certificate"),
    ):
        acm_clients[region].list_tags_for_certificate.assert_called_once_with(
            CertificateArn=f"arn:aws:acm:{region}:{account_id}:certificate/{certificate_id}"
        )


@pytest.mark.parametrize("invalid_entry", ["nested-name", "wrong-account-arn"])
def test_cleanup_rejects_malformed_registry_before_mutation(invalid_entry: str) -> None:
    """Untrusted SSM inventory cannot authorize partial cleanup."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    account_id = "123456789012"
    name = config.certificate_parameter_name("eu-west-1")
    value = f"arn:aws:acm:eu-west-1:{account_id}:certificate/retired-certificate"
    if invalid_entry == "nested-name":
        name = f"{name}/nested"
    else:
        value = "arn:aws:acm:eu-west-1:999999999999:certificate/foreign"

    ssm = MagicMock()
    ssm.get_parameters_by_path.return_value = {"Parameters": [{"Name": name, "Value": value}]}
    with (
        patch.dict(
            os.environ,
            {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": account_id},
        ),
        patch.object(handler, "_load_root_state", return_value=None),
        patch.object(handler.boto3, "client", return_value=ssm),
        patch.object(handler, "_delete_regional_certificate") as delete_region,
        pytest.raises(ValueError),
    ):
        handler._cleanup(config)

    delete_region.assert_not_called()
    ssm.delete_parameter.assert_not_called()


@pytest.mark.parametrize(
    "tags",
    (
        [],
        [{"Key": "Project", "Value": "another-project"}],
        [
            {"Key": "Project", "Value": "gco-test"},
            {"Key": "ManagedBy", "Value": "another-manager"},
        ],
    ),
    ids=("absent", "wrong-project", "wrong-manager"),
)
def test_cleanup_rejects_unowned_registry_before_any_mutation(tags) -> None:
    """An SSM ARN cannot authorize ACM or SSM deletion without ownership."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    account_id = "123456789012"
    certificate_arn = f"arn:aws:acm:us-west-2:{account_id}:certificate/untrusted-certificate"
    ssm = MagicMock()
    ssm.get_parameters_by_path.return_value = {
        "Parameters": [
            {
                "Name": config.certificate_parameter_name("us-west-2"),
                "Value": certificate_arn,
            }
        ]
    }
    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {"Tags": tags}

    def client(service_name: str, *, region_name: str):
        if service_name == "ssm":
            assert region_name == config.registry_region
            return ssm
        if service_name == "acm":
            assert region_name == "us-west-2"
            return acm
        raise AssertionError(f"Unexpected client: {service_name}")

    with (
        patch.dict(
            os.environ,
            {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": account_id},
        ),
        patch.object(handler, "_load_root_state", return_value=None),
        patch.object(handler.boto3, "client", side_effect=client),
        patch.object(handler, "_delete_regional_certificate") as delete_region,
        pytest.raises(PermissionError, match="ownership tags"),
    ):
        handler._cleanup(config)

    delete_region.assert_not_called()
    acm.delete_certificate.assert_not_called()
    acm.add_tags_to_certificate.assert_not_called()
    ssm.delete_parameter.assert_not_called()


def test_reconciliation_migrates_a_verified_legacy_leaf() -> None:
    """Only reconciliation may tag a legacy leaf proven by SAN and root signature."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    account_id = "123456789012"
    certificate_arn = f"arn:aws:acm:us-west-2:{account_id}:certificate/legacy-leaf"
    now = datetime(2027, 1, 1, tzinfo=UTC)

    with patch.object(handler, "_now", return_value=now):
        current = handler._generate_root(config, 1)
        certificate_pem, _, expected_expiry = handler._generate_leaf(config, current)
        state = {
            "schema_version": handler._SCHEMA_VERSION,
            "current": current,
            "pending": None,
            "previous": [],
            "retired_regions": [],
        }

        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": certificate_arn}}
        acm = MagicMock()
        acm.list_tags_for_certificate.return_value = {"Tags": []}
        acm.describe_certificate.return_value = {"Certificate": {"Type": "IMPORTED"}}
        acm.get_certificate.return_value = {"Certificate": certificate_pem.decode("ascii")}

        def client(service_name: str, *, region_name: str):
            if service_name == "ssm":
                assert region_name == config.registry_region
                return ssm
            if service_name == "acm":
                assert region_name == "us-west-2"
                return acm
            raise AssertionError(f"Unexpected client: {service_name}")

        with (
            patch.dict(
                os.environ,
                {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": account_id},
            ),
            patch.object(handler.boto3, "client", side_effect=client),
        ):
            arn, expiry, rotated = handler._ensure_certificate(
                config,
                state,
                "us-west-2",
            )

    assert arn == certificate_arn
    assert expiry == expected_expiry
    assert rotated is False
    acm.add_tags_to_certificate.assert_called_once_with(
        CertificateArn=certificate_arn,
        Tags=[
            {"Key": "Project", "Value": config.project_name},
            {"Key": "ManagedBy", "Value": "gco-backend-tls-manager"},
        ],
    )
    acm.import_certificate.assert_not_called()
    ssm.put_parameter.assert_not_called()


def test_delete_does_not_migrate_an_untagged_registered_leaf() -> None:
    """Delete fails closed instead of tagging or deleting a legacy registry ARN."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    account_id = "123456789012"
    certificate_arn = f"arn:aws:acm:us-west-2:{account_id}:certificate/legacy-leaf"
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": certificate_arn}}
    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {"Tags": []}

    def client(service_name: str, *, region_name: str):
        if service_name == "ssm":
            assert region_name == config.registry_region
            return ssm
        if service_name == "acm":
            assert region_name == "us-west-2"
            return acm
        raise AssertionError(f"Unexpected client: {service_name}")

    with (
        patch.dict(
            os.environ,
            {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": account_id},
        ),
        patch.object(handler.boto3, "client", side_effect=client),
        pytest.raises(PermissionError, match="missing ownership tags"),
    ):
        handler._delete_regional_certificate(
            config,
            "us-west-2",
            defer_in_use=False,
        )

    acm.add_tags_to_certificate.assert_not_called()
    acm.delete_certificate.assert_not_called()
    ssm.delete_parameter.assert_not_called()


def test_never_initialized_delete_with_empty_registry_remains_safe() -> None:
    """A genuine Create rollback can clean configured paths without root state."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    ssm = MagicMock()
    ssm.get_parameters_by_path.return_value = {"Parameters": []}

    with (
        patch.object(handler, "_load_root_state", return_value=None),
        patch.object(handler.boto3, "client", return_value=ssm),
        patch.object(handler, "_delete_regional_certificate", return_value=True) as delete_region,
    ):
        handler._cleanup(config)

    delete_region.assert_called_once_with(config, "us-west-2", defer_in_use=False)
    ssm.delete_parameter.assert_called_once_with(Name=config.root_ca_parameter_name)


# ---------------------------------------------------------------------------
# Shared helpers for the boundary tests below.
# ---------------------------------------------------------------------------

_ACCOUNT_ID = "123456789012"
_NOW = datetime(2027, 1, 1, tzinfo=UTC)
_OWNED_TAGS = [
    {"Key": "Project", "Value": "gco-test"},
    {"Key": "ManagedBy", "Value": "gco-backend-tls-manager"},
]


def _resource_properties(**overrides):
    """Return a complete, valid custom-resource property set."""
    properties = {
        "Regions": ["us-west-2", "eu-west-1"],
        "ServerName": "backend.gco-test.gco.internal",
        "ProjectName": "gco-test",
        "RegistryRegion": "us-east-1",
        "RootCaParameterName": "/gco-test/backend-tls/root-ca.pem",
        "CertificateParameterPrefix": "/gco-test/backend-tls/certificate-arn/",
    }
    properties.update(overrides)
    return properties


def _root_state(
    handler,
    config,
    *,
    current_generation=1,
    pending_generation=None,
    published=False,
    previous_generations=(),
    now=_NOW,
):
    """Build a schema-valid root secret backed by real ECDSA roots."""
    with patch.object(handler, "_now", return_value=now):
        state = {
            "schema_version": handler._SCHEMA_VERSION,
            "current": handler._generate_root(config, current_generation),
            "pending": None,
            "previous": [],
            "retired_regions": [],
        }
        if pending_generation is not None:
            pending = handler._generate_root(config, pending_generation)
            pending["activate_after"] = handler._iso(
                now + timedelta(hours=config.root_activation_delay_hours)
            )
            if published:
                pending["trust_bundle_published_at"] = handler._iso(now)
            state["pending"] = pending
        for generation in previous_generations:
            retired = handler._generate_root(config, generation)
            state["previous"].append(
                {
                    "generation": generation,
                    "certificate_pem": retired["certificate_pem"],
                    "retire_after": handler._iso(now + timedelta(days=config.root_overlap_days)),
                }
            )
    return state


def _client_factory(**clients):
    """Return a ``boto3.client`` stand-in that dispatches on service name."""

    def client(service_name: str, **kwargs):
        if service_name not in clients:
            raise AssertionError(f"Unexpected client: {service_name}")
        return clients[service_name]

    return client


def _self_signed_record(handler, config, *, basic_constraints, now=_NOW):
    """Build a root record whose certificate carries the given constraints."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = handler._root_subject(config.project_name, 1)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=10))
    )
    if basic_constraints is not None:
        builder = builder.add_extension(basic_constraints, critical=True)
    certificate = builder.sign(key, hashes.SHA256())
    return {
        "generation": 1,
        "private_key_pem": handler._serialize_private_key(key),
        "certificate_pem": handler._serialize_certificate(certificate),
        "not_after": handler._iso(now + timedelta(days=10)),
    }


# ---------------------------------------------------------------------------
# ManagerConfig parsing and validation.
# ---------------------------------------------------------------------------


def test_from_event_reads_resource_properties_and_dedupes_regions() -> None:
    """Custom-resource properties win over the environment and are normalised."""
    handler = load_lambda_module("tls-certificate-manager")
    event = {
        "RequestType": "Create",
        "ResourceProperties": _resource_properties(
            Regions=[" us-west-2 ", "eu-west-1", "us-west-2"],
            RootGeneration="3",
            RootValidityDays=400,
            RootRotateBeforeDays="60",
            RootActivationDelayHours="12",
            RootOverlapDays="50",
            LeafValidityDays="20",
            LeafRotateBeforeDays="5",
        ),
    }

    with patch.dict(os.environ, {"PROJECT_NAME": "ignored-env-project"}):
        config = handler.ManagerConfig.from_event(event)

    assert config.regions == ("us-west-2", "eu-west-1")
    assert config.project_name == "gco-test"
    assert config.server_name == "backend.gco-test.gco.internal"
    assert config.registry_region == "us-east-1"
    assert config.root_generation == 3
    assert config.root_validity_days == 400
    assert config.root_rotate_before_days == 60
    assert config.root_activation_delay_hours == 12
    assert config.root_overlap_days == 50
    assert config.leaf_validity_days == 20
    assert config.leaf_rotate_before_days == 5
    assert config.certificate_parameter_name("eu-west-1") == (
        "/gco-test/backend-tls/certificate-arn/eu-west-1"
    )


def test_from_event_falls_back_to_environment_for_scheduled_events() -> None:
    """A bare scheduler event is configured entirely from the environment."""
    handler = load_lambda_module("tls-certificate-manager")
    environment = {
        "CERTIFICATE_REGIONS": json.dumps(["ap-southeast-1"]),
        "BACKEND_TLS_SERVER_NAME": "backend.gco-env.gco.internal",
        "PROJECT_NAME": "gco-env",
        "REGISTRY_REGION": "eu-central-1",
        "ROOT_CA_PARAMETER_NAME": "/gco-env/backend-tls/root-ca.pem",
        "CERTIFICATE_PARAMETER_PREFIX": "/gco-env/backend-tls/certificate-arn/",
        "ROOT_GENERATION": "2",
        "ROOT_VALIDITY_DAYS": "3650",
        "ROOT_ROTATE_BEFORE_DAYS": "180",
        "ROOT_ACTIVATION_DELAY_HOURS": "24",
        "ROOT_OVERLAP_DAYS": "45",
        "LEAF_VALIDITY_DAYS": "30",
        "LEAF_ROTATE_BEFORE_DAYS": "10",
    }

    with patch.dict(os.environ, environment):
        config = handler.ManagerConfig.from_event({"Action": "Rotate"})

    assert config.regions == ("ap-southeast-1",)
    assert config.server_name == "backend.gco-env.gco.internal"
    assert config.project_name == "gco-env"
    assert config.registry_region == "eu-central-1"
    assert config.root_generation == 2
    assert config.root_validity_days == 3650


@pytest.mark.parametrize(
    ("regions", "message"),
    (
        ("us-west-2", "Regions must be a list"),
        ([], "At least one valid AWS workload region"),
        (["us-west-2", "not a region"], "At least one valid AWS workload region"),
    ),
    ids=("string", "empty", "invalid-entry"),
)
def test_from_event_rejects_invalid_region_lists(regions, message: str) -> None:
    """Region lists are validated before any AWS client is created."""
    handler = load_lambda_module("tls-certificate-manager")
    event = {"ResourceProperties": _resource_properties(Regions=regions)}

    with pytest.raises(ValueError, match=message):
        handler.ManagerConfig.from_event(event)


def test_from_event_without_regions_anywhere_is_rejected() -> None:
    """No Regions property and no CERTIFICATE_REGIONS environment fails closed."""
    handler = load_lambda_module("tls-certificate-manager")
    properties = _resource_properties()
    del properties["Regions"]
    environment = {key: value for key, value in os.environ.items() if key != "CERTIFICATE_REGIONS"}

    with (
        patch.dict(os.environ, environment, clear=True),
        pytest.raises(ValueError, match="At least one valid AWS workload region"),
    ):
        handler.ManagerConfig.from_event({"ResourceProperties": properties})


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"server_name": "not a dns name"}, "ServerName must be a valid private DNS name"),
        ({"registry_region": "nowhere"}, "RegistryRegion must be a valid AWS region"),
        ({"project_name": ""}, "ProjectName is required"),
        (
            {"root_ca_parameter_name": "/other/backend-tls/root-ca.pem"},
            "RootCaParameterName must stay inside",
        ),
        (
            {"certificate_parameter_prefix": "/gco-test/other/"},
            "CertificateParameterPrefix must stay inside",
        ),
        (
            {"root_rotate_before_days": 3_650},
            "RootRotateBeforeDays must be less than RootValidityDays",
        ),
        (
            {"leaf_rotate_before_days": 30},
            "LeafRotateBeforeDays must be less than LeafValidityDays",
        ),
        (
            {"root_validity_days": 30, "root_rotate_before_days": 5},
            "RootValidityDays must exceed LeafValidityDays",
        ),
        ({"root_overlap_days": 30}, "RootOverlapDays must exceed LeafValidityDays"),
    ),
    ids=(
        "server-name",
        "registry-region",
        "project-name",
        "root-parameter-namespace",
        "certificate-prefix-namespace",
        "root-rotate-window",
        "leaf-rotate-window",
        "root-shorter-than-leaf",
        "overlap-shorter-than-leaf",
    ),
)
def test_validate_rejects_unsafe_certificate_policy(overrides, message: str) -> None:
    """Each policy invariant has a dedicated, actionable error."""
    handler = load_lambda_module("tls-certificate-manager")
    config = replace(_manager_config(handler), **overrides)

    with pytest.raises(ValueError, match=message):
        config.validate()


@pytest.mark.parametrize(
    "value", ("abc", None, "0", -4, 0), ids=("text", "none", "zero-str", "negative", "zero")
)
def test_positive_int_rejects_non_positive_values(value) -> None:
    """Policy integers must be strictly positive whatever their source type."""
    handler = load_lambda_module("tls-certificate-manager")

    with pytest.raises(ValueError, match="RootGeneration must be a positive integer"):
        handler._positive_int(value, "RootGeneration")


def test_positive_int_parses_numeric_strings() -> None:
    """Environment strings and CloudFormation numbers parse identically."""
    handler = load_lambda_module("tls-certificate-manager")

    assert handler._positive_int("7", "RootGeneration") == 7
    assert handler._positive_int(7, "RootGeneration") == 7


def test_from_event_reports_which_property_is_not_a_positive_integer() -> None:
    """A malformed CloudFormation number names the offending property."""
    handler = load_lambda_module("tls-certificate-manager")
    event = {"ResourceProperties": _resource_properties(LeafValidityDays="soon")}

    with pytest.raises(ValueError, match="LeafValidityDays must be a positive integer"):
        handler.ManagerConfig.from_event(event)


# ---------------------------------------------------------------------------
# Time and PEM helpers.
# ---------------------------------------------------------------------------


def test_now_returns_timezone_aware_utc() -> None:
    """Unpatched ``_now`` is UTC-aware so ISO round-trips carry a ``Z`` suffix."""
    handler = load_lambda_module("tls-certificate-manager")
    before = datetime.now(UTC)

    now = handler._now()

    assert now.tzinfo is UTC
    assert before <= now <= datetime.now(UTC)
    assert handler._iso(now).endswith("Z")


@pytest.mark.parametrize(
    ("value", "message"),
    (
        (None, "pending.activate_after is missing"),
        (12345, "pending.activate_after is missing"),
        ("yesterday", "pending.activate_after is not an ISO timestamp"),
    ),
    ids=("none", "number", "text"),
)
def test_parse_iso_rejects_malformed_timestamps(value, message: str) -> None:
    """Root-state timestamps must be ISO strings; the field name is reported."""
    handler = load_lambda_module("tls-certificate-manager")

    with pytest.raises(ValueError, match=message):
        handler._parse_iso(value, "pending.activate_after")


def test_parse_iso_normalises_naive_and_offset_timestamps_to_utc() -> None:
    """Naive values are treated as UTC and offsets are converted, not dropped."""
    handler = load_lambda_module("tls-certificate-manager")

    naive = handler._parse_iso("2027-01-01T12:00:00", "field")
    offset = handler._parse_iso("2027-01-01T14:00:00+02:00", "field")
    zulu = handler._parse_iso("2027-01-01T12:00:00Z", "field")

    assert naive == offset == zulu == datetime(2027, 1, 1, 12, tzinfo=UTC)
    assert naive.tzinfo is UTC


def test_certificate_not_after_falls_back_to_naive_attribute() -> None:
    """Certificates without ``not_valid_after_utc`` are treated as UTC."""
    handler = load_lambda_module("tls-certificate-manager")
    certificate = SimpleNamespace(not_valid_after=datetime(2028, 6, 1, 8, 30))

    assert handler._certificate_not_after(certificate) == datetime(2028, 6, 1, 8, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Root record validation.
# ---------------------------------------------------------------------------


def test_validate_root_record_rejects_structural_problems() -> None:
    """Missing or mistyped fields are rejected before any PEM parsing."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    root = _root_state(handler, config)["current"]

    with pytest.raises(ValueError, match="Invalid root state: current is missing"):
        handler._validate_root_record("not-a-record", "current")
    for generation in ("1", 0, -1, True):
        with pytest.raises(ValueError, match=r"Invalid root state: pending\.generation"):
            handler._validate_root_record({**root, "generation": generation}, "pending")
    with pytest.raises(ValueError, match="current key or certificate is missing"):
        handler._validate_root_record({**root, "private_key_pem": None}, "current")
    with pytest.raises(ValueError, match="current key or certificate is missing"):
        handler._validate_root_record({**root, "certificate_pem": 42}, "current")
    with pytest.raises(ValueError, match="current contains malformed PEM"):
        handler._validate_root_record(
            {**root, "private_key_pem": "-----BEGIN JUNK-----"}, "current"
        )
    with pytest.raises(ValueError, match=r"current\.not_after is missing"):
        handler._validate_root_record({**root, "not_after": None}, "current")


def test_validate_root_record_requires_matching_ecdsa_ca_material() -> None:
    """Non-EC keys, mismatched pairs, and non-CA certificates are all rejected."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    with patch.object(handler, "_now", return_value=_NOW):
        root = handler._generate_root(config, 1)
        other_root = handler._generate_root(config, 1)
    ed_key_pem = (
        ed25519.Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode("ascii")
    )

    with pytest.raises(ValueError, match="current key must be ECDSA"):
        handler._validate_root_record({**root, "private_key_pem": ed_key_pem}, "current")
    with pytest.raises(ValueError, match="current key does not match its certificate"):
        handler._validate_root_record(
            {**root, "certificate_pem": other_root["certificate_pem"]}, "current"
        )
    with pytest.raises(ValueError, match="current is not a CA"):
        handler._validate_root_record(
            _self_signed_record(handler, config, basic_constraints=None), "current"
        )
    with pytest.raises(ValueError, match="current is not a CA"):
        handler._validate_root_record(
            _self_signed_record(
                handler,
                config,
                basic_constraints=x509.BasicConstraints(ca=False, path_length=None),
            ),
            "current",
        )


# ---------------------------------------------------------------------------
# Root state persistence and trust bundle publication.
# ---------------------------------------------------------------------------


def test_load_root_state_propagates_unexpected_secret_errors() -> None:
    """Only ResourceNotFound means "never initialised"; other failures surface."""
    handler = load_lambda_module("tls-certificate-manager")
    secrets = MagicMock()
    secrets.get_secret_value.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "GetSecretValue",
    )

    with (
        patch.dict(os.environ, {"ROOT_SECRET_ARN": "arn:secret"}),
        patch.object(handler.boto3, "client", return_value=secrets),
        pytest.raises(ClientError, match="AccessDeniedException"),
    ):
        handler._load_root_state()


@pytest.mark.parametrize(
    ("secret_string", "message"),
    (
        ("{not json", "Root CA secret contains invalid JSON"),
        (json.dumps(["list"]), "Root CA secret has an unsupported schema"),
        (json.dumps({"schema_version": 99, "current": {}}), "unsupported schema"),
    ),
    ids=("invalid-json", "not-an-object", "wrong-schema-version"),
)
def test_load_root_state_rejects_unreadable_secrets(secret_string: str, message: str) -> None:
    """A corrupted secret is an error, never silently treated as uninitialised."""
    handler = load_lambda_module("tls-certificate-manager")
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": secret_string}

    with (
        patch.dict(os.environ, {"ROOT_SECRET_ARN": "arn:secret"}),
        patch.object(handler.boto3, "client", return_value=secrets),
        pytest.raises(ValueError, match=message),
    ):
        handler._load_root_state()


@pytest.mark.parametrize(
    ("pending_generation", "published"),
    ((None, False), (2, False), (2, True)),
    ids=("no-pending", "pending-unpublished", "pending-published"),
)
def test_load_root_state_returns_validated_state(pending_generation, published: bool) -> None:
    """A schema-valid secret round-trips with every optional section intact."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = _root_state(
        handler,
        config,
        current_generation=2 if pending_generation else 1,
        pending_generation=pending_generation,
        published=published,
        previous_generations=(1,) if pending_generation else (),
    )
    state["retired_regions"] = ["eu-west-1"]
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": json.dumps(state)}

    with (
        patch.dict(os.environ, {"ROOT_SECRET_ARN": "arn:secret"}),
        patch.object(handler.boto3, "client", return_value=secrets),
    ):
        loaded = handler._load_root_state()

    assert loaded == state
    secrets.get_secret_value.assert_called_once_with(SecretId="arn:secret")


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"previous": "none"}, "previous must be a list"),
        ({"previous": [{"retire_after": "2027-01-01T00:00:00Z"}]}, r"previous\[0\]"),
        ({"retired_regions": "eu-west-1"}, "retired_regions"),
        ({"retired_regions": ["eu-west-1", "eu-west-1"]}, "retired_regions"),
        ({"retired_regions": ["not-a-region"]}, "retired_regions"),
        ({"pending": {"generation": 2}}, "pending key or certificate is missing"),
    ),
    ids=(
        "previous-not-list",
        "previous-entry-without-pem",
        "retired-not-list",
        "retired-duplicate",
        "retired-invalid",
        "pending-incomplete",
    ),
)
def test_load_root_state_rejects_malformed_sections(mutation, message: str) -> None:
    """Every optional section is validated, not only the current root."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = {**_root_state(handler, config), **mutation}
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": json.dumps(state)}

    with (
        patch.dict(os.environ, {"ROOT_SECRET_ARN": "arn:secret"}),
        patch.object(handler.boto3, "client", return_value=secrets),
        pytest.raises(ValueError, match=message),
    ):
        handler._load_root_state()


def test_save_root_state_writes_compact_json_to_the_root_secret() -> None:
    """The secret holds the exact state document without pretty-print padding."""
    handler = load_lambda_module("tls-certificate-manager")
    secrets = MagicMock()
    state = {"schema_version": 1, "current": {"generation": 1}, "retired_regions": []}

    with (
        patch.dict(os.environ, {"ROOT_SECRET_ARN": "arn:secret"}),
        patch.object(handler.boto3, "client", return_value=secrets) as client,
    ):
        handler._save_root_state(state)

    client.assert_called_once_with("secretsmanager")
    secrets.put_secret_value.assert_called_once_with(
        SecretId="arn:secret",
        SecretString='{"schema_version":1,"current":{"generation":1},"retired_regions":[]}',
    )


@pytest.mark.parametrize("with_pending", (False, True), ids=("current-only", "with-pending"))
def test_publish_trust_bundle_concatenates_public_roots_only(with_pending: bool) -> None:
    """The SSM bundle lists current, pending, and previous roots; never a key."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = _root_state(
        handler,
        config,
        current_generation=2,
        pending_generation=3 if with_pending else None,
        previous_generations=(1,),
    )
    ssm = MagicMock()

    with patch.object(handler.boto3, "client", return_value=ssm) as client:
        handler._publish_trust_bundle(config, state)

    client.assert_called_once_with("ssm", region_name="us-east-1")
    expected = [state["current"]["certificate_pem"]]
    if with_pending:
        expected.append(state["pending"]["certificate_pem"])
    expected.append(state["previous"][0]["certificate_pem"])
    bundle = ssm.put_parameter.call_args.kwargs["Value"]
    assert bundle == "".join(pem.rstrip() + "\n" for pem in expected)
    assert "PRIVATE KEY" not in bundle
    assert ssm.put_parameter.call_args.kwargs == {
        "Name": "/gco-test/backend-tls/root-ca.pem",
        "Value": bundle,
        "Type": "String",
        "Overwrite": True,
        "Description": "Public GCO backend TLS root trust bundle; contains no private key",
    }


# ---------------------------------------------------------------------------
# Root bootstrap, staging, promotion, and retirement.
# ---------------------------------------------------------------------------


def test_ensure_root_bootstraps_a_new_root_when_no_state_exists() -> None:
    """First run mints the configured generation, saves it, and publishes trust."""
    handler = load_lambda_module("tls-certificate-manager")
    config = replace(_manager_config(handler), root_generation=4)

    with (
        patch.object(handler, "_now", return_value=_NOW),
        patch.object(handler, "_load_root_state", return_value=None),
        patch.object(handler, "_publish_trust_bundle") as publish,
        patch.object(handler, "_save_root_state") as save,
    ):
        state, changed = handler._ensure_root(config)

    assert changed is True
    assert state["schema_version"] == handler._SCHEMA_VERSION
    assert state["current"]["generation"] == 4
    assert state["current"]["not_after"] == handler._iso(_NOW + timedelta(days=3_650))
    assert state["pending"] is None
    assert state["previous"] == []
    assert state["retired_regions"] == []
    save.assert_called_once_with(state)
    publish.assert_called_once_with(config, state)
    _, certificate = handler._validate_root_record(state["current"], "current")
    assert certificate.issuer == certificate.subject


def test_ensure_root_migrates_legacy_state_without_retired_regions() -> None:
    """Secrets written before retirement tracking gain the list without rollover."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = _root_state(handler, config)
    del state["retired_regions"]

    with (
        patch.object(handler, "_now", return_value=_NOW),
        patch.object(handler, "_load_root_state", return_value=state),
        patch.object(handler, "_publish_trust_bundle"),
        patch.object(handler, "_save_root_state") as save,
    ):
        reconciled, changed = handler._ensure_root(config)

    assert changed is True
    assert reconciled["retired_regions"] == []
    assert reconciled["pending"] is None
    assert reconciled["current"]["generation"] == 1
    save.assert_called_once_with(state)


def test_ensure_root_stages_operator_requested_generation_bump(caplog) -> None:
    """Raising RootGeneration stages a pending root instead of replacing current."""
    handler = load_lambda_module("tls-certificate-manager")
    config = replace(_manager_config(handler), root_generation=2)
    state = _root_state(handler, config)

    with (
        patch.object(handler, "_now", return_value=_NOW),
        patch.object(handler, "_load_root_state", return_value=state),
        patch.object(handler, "_publish_trust_bundle") as publish,
        patch.object(handler, "_save_root_state") as save,
        caplog.at_level(logging.INFO),
    ):
        reconciled, changed = handler._ensure_root(config)

    assert changed is True
    assert reconciled["current"]["generation"] == 1
    assert reconciled["pending"]["generation"] == 2
    assert reconciled["pending"]["trust_bundle_published_at"] == handler._iso(_NOW)
    assert reconciled["pending"]["activate_after"] == handler._iso(_NOW + timedelta(hours=24))
    # Saved once when staged and once more after SSM confirmed the bundle.
    assert save.call_args_list == [((state,),), ((state,),)]
    publish.assert_called_once_with(config, state)
    assert "Staged root generation 2" in caplog.text
    assert "Confirmed trust publication for pending root generation 2" in caplog.text


def test_ensure_root_stages_rollover_when_current_root_nears_expiry() -> None:
    """Expiry inside the rotate-before window stages the next generation."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = _root_state(handler, config, current_generation=3)
    near_expiry = _NOW + timedelta(days=3_650 - 100)

    with (
        patch.object(handler, "_now", return_value=near_expiry),
        patch.object(handler, "_load_root_state", return_value=state),
        patch.object(handler, "_publish_trust_bundle"),
        patch.object(handler, "_save_root_state"),
    ):
        reconciled, changed = handler._ensure_root(config)

    assert changed is True
    assert reconciled["current"]["generation"] == 3
    assert reconciled["pending"]["generation"] == 4
    assert reconciled["pending"]["not_after"] == handler._iso(near_expiry + timedelta(days=3_650))


def test_ensure_root_promotes_published_pending_root_after_delay(caplog) -> None:
    """A confirmed pending root becomes current and the old root is retained."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = _root_state(handler, config, pending_generation=2, published=True)
    old_current_pem = state["current"]["certificate_pem"]
    promoted_pem = state["pending"]["certificate_pem"]
    later = _NOW + timedelta(hours=25)

    with (
        patch.object(handler, "_now", return_value=later),
        patch.object(handler, "_load_root_state", return_value=state),
        patch.object(handler, "_publish_trust_bundle") as publish,
        patch.object(handler, "_save_root_state") as save,
        caplog.at_level(logging.INFO),
    ):
        reconciled, changed = handler._ensure_root(config)

    assert changed is True
    assert reconciled["current"]["generation"] == 2
    assert reconciled["current"]["certificate_pem"] == promoted_pem
    assert "activate_after" not in reconciled["current"]
    assert "trust_bundle_published_at" not in reconciled["current"]
    assert reconciled["pending"] is None
    assert reconciled["previous"] == [
        {
            "generation": 1,
            "certificate_pem": old_current_pem,
            "retire_after": handler._iso(later + timedelta(days=45)),
        }
    ]
    save.assert_called_once_with(state)
    publish.assert_called_once_with(config, state)
    assert "Promoted root generation 2" in caplog.text


def test_ensure_root_drops_previous_roots_after_overlap_window(caplog) -> None:
    """Retired roots leave the bundle once their overlap window has elapsed."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = _root_state(handler, config, current_generation=2, previous_generations=(1,))
    after_overlap = _NOW + timedelta(days=46)

    with (
        patch.object(handler, "_now", return_value=after_overlap),
        patch.object(handler, "_load_root_state", return_value=state),
        patch.object(handler, "_publish_trust_bundle"),
        patch.object(handler, "_save_root_state") as save,
        caplog.at_level(logging.INFO),
    ):
        reconciled, changed = handler._ensure_root(config)

    assert changed is True
    assert reconciled["previous"] == []
    assert reconciled["pending"] is None
    assert reconciled["current"]["generation"] == 2
    save.assert_called_once_with(state)
    assert "Removed expired previous root certificates" in caplog.text


def test_ensure_root_is_a_no_op_for_healthy_state() -> None:
    """Steady state republishes the bundle without touching the secret."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = _root_state(handler, config, current_generation=2, previous_generations=(1,))
    snapshot = json.dumps(state)

    with (
        patch.object(handler, "_now", return_value=_NOW + timedelta(days=1)),
        patch.object(handler, "_load_root_state", return_value=state),
        patch.object(handler, "_publish_trust_bundle") as publish,
        patch.object(handler, "_save_root_state") as save,
    ):
        reconciled, changed = handler._ensure_root(config)

    assert changed is False
    assert json.dumps(reconciled) == snapshot
    save.assert_not_called()
    publish.assert_called_once_with(config, state)


# ---------------------------------------------------------------------------
# Leaf issuance.
# ---------------------------------------------------------------------------


def test_generate_leaf_refuses_when_root_expires_inside_rotation_window() -> None:
    """A leaf that could not be rotated safely before root expiry is not issued."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    root = _root_state(handler, config)["current"]

    with (
        patch.object(handler, "_now", return_value=_NOW + timedelta(days=3_650 - 5)),
        pytest.raises(RuntimeError, match="expires too soon"),
    ):
        handler._generate_leaf(config, root)


def test_generate_leaf_never_outlives_the_root() -> None:
    """Leaf validity is clamped one day inside the root's expiry."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    root = _root_state(handler, config)["current"]
    root_expiry = _NOW + timedelta(days=3_650)

    with patch.object(handler, "_now", return_value=root_expiry - timedelta(days=20)):
        certificate_pem, private_key_pem, expiry = handler._generate_leaf(config, root)

    assert expiry == root_expiry - timedelta(days=1)
    certificate = x509.load_pem_x509_certificate(certificate_pem)
    assert handler._certificate_not_after(certificate) == expiry
    assert isinstance(
        serialization.load_pem_private_key(private_key_pem, None),
        ec.EllipticCurvePrivateKey,
    )


# ---------------------------------------------------------------------------
# ACM ARN, tag, and root-set validation helpers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    (None, 42, "arn:aws:acm:us-east-1:123456789012:certificate/wrong-region", "certificate/x"),
    ids=("none", "number", "other-region", "not-an-arn"),
)
def test_validated_certificate_arn_rejects_foreign_values(value) -> None:
    """Only account-, partition-, and Region-scoped ACM ARNs authorise mutation."""
    handler = load_lambda_module("tls-certificate-manager")

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        pytest.raises(ValueError, match="Invalid ACM certificate ARN stored for us-west-2"),
    ):
        handler._validated_certificate_arn("us-west-2", value)


def test_validated_certificate_arn_strips_surrounding_whitespace() -> None:
    """A padded SSM value normalises to the bare ARN."""
    handler = load_lambda_module("tls-certificate-manager")
    arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/abc-123"

    with patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}):
        assert handler._validated_certificate_arn("us-west-2", f"  {arn}\n") == arn


@pytest.mark.parametrize(
    ("tags", "message"),
    (
        ({"Key": "Project"}, "malformed certificate tags"),
        (["Project=gco-test"], "malformed certificate tag"),
        ([{"Key": 1, "Value": "gco-test"}], "malformed certificate tag"),
        ([{"Key": "Project", "Value": None}], "malformed certificate tag"),
        (
            [{"Key": "Project", "Value": "a"}, {"Key": "Project", "Value": "b"}],
            "malformed certificate tag",
        ),
    ),
    ids=("not-a-list", "entry-not-object", "key-not-str", "value-not-str", "duplicate-key"),
)
def test_certificate_tags_rejects_malformed_acm_responses(tags, message: str) -> None:
    """Ownership decisions never rest on a tag list ACM returned malformed."""
    handler = load_lambda_module("tls-certificate-manager")
    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {"Tags": tags}

    with pytest.raises(ValueError, match=message):
        handler._certificate_tags(acm, "arn:certificate")


def test_certificate_tags_returns_strict_map() -> None:
    """Well-formed tags become a plain key/value map."""
    handler = load_lambda_module("tls-certificate-manager")
    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {"Tags": _OWNED_TAGS}

    assert handler._certificate_tags(acm, "arn:certificate") == {
        "Project": "gco-test",
        "ManagedBy": "gco-backend-tls-manager",
    }
    acm.list_tags_for_certificate.assert_called_once_with(CertificateArn="arn:certificate")


def test_managed_root_certificates_includes_pending_and_previous_roots() -> None:
    """Migration proofs accept every root the authenticated state still trusts."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = _root_state(
        handler,
        config,
        current_generation=2,
        pending_generation=3,
        previous_generations=(1,),
    )

    roots = handler._managed_root_certificates(state)

    assert [root.public_bytes(serialization.Encoding.PEM).decode("ascii") for root in roots] == [
        state["current"]["certificate_pem"],
        state["pending"]["certificate_pem"],
        state["previous"][0]["certificate_pem"],
    ]


@pytest.mark.parametrize(
    ("previous", "message"),
    (
        (["pem"], r"previous\[0\]$"),
        ([{"certificate_pem": None}], r"previous\[0\]$"),
        (
            [{"certificate_pem": "-----BEGIN CERTIFICATE-----\nnope\n"}],
            r"previous\[0\] certificate",
        ),
    ),
    ids=("entry-not-object", "pem-not-str", "pem-malformed"),
)
def test_managed_root_certificates_rejects_malformed_previous_roots(previous, message) -> None:
    """A corrupt previous root cannot be used to prove a legacy leaf."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = {**_root_state(handler, config), "previous": previous}

    with pytest.raises(ValueError, match=message):
        handler._managed_root_certificates(state)


def test_certificate_without_san_never_matches_the_server_name() -> None:
    """A root (no SAN) is not mistaken for a leaf for the backend host."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    _, root_certificate = handler._validate_root_record(
        _root_state(handler, config)["current"], "current"
    )

    assert handler._certificate_has_server_name(root_certificate, config.server_name) is False


def test_certificate_signed_by_root_requires_matching_issuer_and_ec_key() -> None:
    """Issuer mismatch and non-ECDSA root keys short-circuit before verification."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    with patch.object(handler, "_now", return_value=_NOW):
        issuing_root = handler._generate_root(config, 1)
        other_generation = handler._generate_root(config, 2)
        leaf_pem, _, _ = handler._generate_leaf(config, issuing_root)
    leaf = x509.load_pem_x509_certificate(leaf_pem)
    _, other_certificate = handler._validate_root_record(other_generation, "current")

    ed_key = ed25519.Ed25519PrivateKey.generate()
    subject = handler._root_subject(config.project_name, 1)
    ed_root = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(ed_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOW)
        .not_valid_after(_NOW + timedelta(days=10))
        .sign(ed_key, None)
    )

    assert handler._certificate_signed_by_root(leaf, other_certificate) is False
    assert leaf.issuer == ed_root.subject
    assert handler._certificate_signed_by_root(leaf, ed_root) is False


# ---------------------------------------------------------------------------
# Unregistered certificate recovery.
# ---------------------------------------------------------------------------


def test_recovery_ignores_amazon_issued_and_unowned_certificates() -> None:
    """Only imported, dual-tagged leaves are candidates; none means no recovery."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    amazon_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/amazon-issued"
    legacy_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/legacy-import"
    acm = MagicMock()
    acm.get_paginator.return_value.paginate.return_value = [
        {
            "CertificateSummaryList": [
                {"CertificateArn": amazon_arn, "Type": "AMAZON_ISSUED"},
                {"CertificateArn": legacy_arn, "Type": "IMPORTED"},
            ]
        }
    ]
    acm.list_tags_for_certificate.return_value = {"Tags": [{"Key": "Name", "Value": "legacy"}]}
    ssm = MagicMock()

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
    ):
        assert handler._recover_unregistered_certificate(config, "us-west-2") == (None, None)

    acm.list_tags_for_certificate.assert_called_once_with(CertificateArn=legacy_arn)
    acm.describe_certificate.assert_not_called()
    acm.get_certificate.assert_not_called()
    ssm.put_parameter.assert_not_called()


def test_recovery_fails_closed_on_non_imported_owned_certificate() -> None:
    """Ownership tags on an Amazon-issued certificate indicate tampering."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/tagged-public"
    acm = MagicMock()
    acm.get_paginator.return_value.paginate.return_value = [
        {"CertificateSummaryList": [{"CertificateArn": certificate_arn}]}
    ]
    acm.list_tags_for_certificate.return_value = {"Tags": _OWNED_TAGS}
    acm.describe_certificate.return_value = {"Certificate": {"Type": "AMAZON_ISSUED"}}

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", return_value=acm),
        pytest.raises(RuntimeError, match="contains a non-imported leaf"),
    ):
        handler._recover_unregistered_certificate(config, "us-west-2")

    acm.get_certificate.assert_not_called()


def test_recovery_rejects_missing_certificate_body() -> None:
    """ACM must return the PEM body of a candidate before it can be adopted."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/orphan"
    acm = MagicMock()
    acm.get_paginator.return_value.paginate.return_value = [
        {"CertificateSummaryList": [{"CertificateArn": certificate_arn, "Type": "IMPORTED"}]}
    ]
    acm.list_tags_for_certificate.return_value = {"Tags": _OWNED_TAGS}
    acm.describe_certificate.return_value = {"Certificate": {"Type": "IMPORTED"}}
    acm.get_certificate.return_value = {}

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", return_value=acm),
        pytest.raises(ValueError, match="did not return a managed imported certificate"),
    ):
        handler._recover_unregistered_certificate(config, "us-west-2")


def test_recovery_refuses_ambiguous_managed_inventory() -> None:
    """Two tagged orphans cannot be disambiguated, so nothing is registered."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    root = _root_state(handler, config)["current"]
    with patch.object(handler, "_now", return_value=_NOW):
        certificate_pem, _, _ = handler._generate_leaf(config, root)
    arns = [f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/orphan-{index}" for index in (1, 2)]
    acm = MagicMock()
    acm.get_paginator.return_value.paginate.return_value = [
        {"CertificateSummaryList": [{"CertificateArn": arns[0], "Type": "IMPORTED"}]},
        {"CertificateSummaryList": [{"CertificateArn": arns[1], "Type": "IMPORTED"}]},
    ]
    acm.list_tags_for_certificate.return_value = {"Tags": _OWNED_TAGS}
    acm.describe_certificate.return_value = {"Certificate": {"Type": "IMPORTED"}}
    acm.get_certificate.return_value = {"Certificate": certificate_pem.decode("ascii")}
    ssm = MagicMock()

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
        pytest.raises(RuntimeError, match="Multiple unregistered managed ACM certificates"),
    ):
        handler._recover_unregistered_certificate(config, "us-west-2")

    ssm.put_parameter.assert_not_called()


# ---------------------------------------------------------------------------
# Registered certificate lookup.
# ---------------------------------------------------------------------------


def test_registered_certificate_treats_missing_parameter_as_unregistered() -> None:
    """ParameterNotFound is the normal first-run answer, not an error."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    ssm = MagicMock()
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "missing"}},
        "GetParameter",
    )

    with patch.object(handler.boto3, "client", return_value=ssm):
        assert handler._registered_certificate(config, "us-west-2") == (None, None)

    ssm.get_parameter.assert_called_once_with(
        Name="/gco-test/backend-tls/certificate-arn/us-west-2"
    )


def test_registered_certificate_propagates_other_ssm_errors() -> None:
    """Throttling or access failures must not be mistaken for "no certificate"."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    ssm = MagicMock()
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "GetParameter",
    )

    with (
        patch.object(handler.boto3, "client", return_value=ssm),
        pytest.raises(ClientError, match="ThrottlingException"),
    ):
        handler._registered_certificate(config, "us-west-2")


def test_registered_certificate_rejects_conflicting_ownership() -> None:
    """A registry ARN pointing at another project's certificate is refused."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/foreign"
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": certificate_arn}}
    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {
        "Tags": [{"Key": "Project", "Value": "another-project"}]
    }

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
        pytest.raises(PermissionError, match="conflicting ownership tags"),
    ):
        handler._registered_certificate(config, "us-west-2", migration_roots=())

    acm.get_certificate.assert_not_called()


def test_registered_certificate_returns_owned_leaf_without_migration_calls() -> None:
    """An owned certificate is loaded directly; no describe or tagging happens."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    root = _root_state(handler, config)["current"]
    with patch.object(handler, "_now", return_value=_NOW):
        certificate_pem, _, _ = handler._generate_leaf(config, root)
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/owned"
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": certificate_arn}}
    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {"Tags": _OWNED_TAGS}
    acm.get_certificate.return_value = {"Certificate": certificate_pem.decode("ascii")}

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
    ):
        arn, certificate = handler._registered_certificate(config, "us-west-2")

    assert arn == certificate_arn
    assert certificate.public_bytes(serialization.Encoding.PEM) == certificate_pem
    acm.describe_certificate.assert_not_called()
    acm.add_tags_to_certificate.assert_not_called()


def test_registered_certificate_rejects_legacy_non_imported_certificate() -> None:
    """Legacy migration only considers imported certificates."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    _, root_certificate = handler._validate_root_record(
        _root_state(handler, config)["current"], "current"
    )
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/legacy"
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": certificate_arn}}
    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {"Tags": []}
    acm.describe_certificate.return_value = {"Certificate": {"Type": "AMAZON_ISSUED"}}

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
        pytest.raises(PermissionError, match="is not an imported certificate"),
    ):
        handler._registered_certificate(config, "us-west-2", migration_roots=(root_certificate,))

    acm.get_certificate.assert_not_called()
    acm.add_tags_to_certificate.assert_not_called()


def test_registered_certificate_rejects_missing_certificate_body() -> None:
    """A registered ARN whose body ACM cannot return is an error, not a rotation."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/owned"
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": certificate_arn}}
    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {"Tags": _OWNED_TAGS}
    acm.get_certificate.return_value = {"CertificateChain": "only-a-chain"}

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
        pytest.raises(ValueError, match="did not return the imported certificate"),
    ):
        handler._registered_certificate(config, "us-west-2")


@pytest.mark.parametrize("defect", ("foreign-root", "wrong-server-name"))
def test_registered_certificate_refuses_unproven_legacy_leaf(defect: str) -> None:
    """Legacy leaves are tagged only when both SAN and signature match."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    with patch.object(handler, "_now", return_value=_NOW):
        trusted_root = handler._generate_root(config, 1)
        if defect == "foreign-root":
            foreign_root = handler._generate_root(config, 1)
            certificate_pem, _, _ = handler._generate_leaf(config, foreign_root)
        else:
            other_name = replace(config, server_name="other.gco-test.gco.internal")
            certificate_pem, _, _ = handler._generate_leaf(other_name, trusted_root)
    _, root_certificate = handler._validate_root_record(trusted_root, "current")
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/legacy"
    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": certificate_arn}}
    acm = MagicMock()
    acm.list_tags_for_certificate.return_value = {"Tags": []}
    acm.describe_certificate.return_value = {"Certificate": {"Type": "IMPORTED"}}
    acm.get_certificate.return_value = {"Certificate": certificate_pem.decode("ascii")}

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
        pytest.raises(PermissionError, match="is not a managed backend leaf"),
    ):
        handler._registered_certificate(config, "us-west-2", migration_roots=(root_certificate,))

    acm.add_tags_to_certificate.assert_not_called()


# ---------------------------------------------------------------------------
# Leaf rotation policy and import.
# ---------------------------------------------------------------------------


def test_leaf_needs_rotation_for_missing_or_expiring_certificates() -> None:
    """No leaf, or a leaf inside the rotate-before window, triggers issuance."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    root = _root_state(handler, config)["current"]
    _, root_certificate = handler._validate_root_record(root, "current")
    with patch.object(handler, "_now", return_value=_NOW):
        certificate_pem, _, _ = handler._generate_leaf(config, root)
    leaf = x509.load_pem_x509_certificate(certificate_pem)

    assert handler._leaf_needs_rotation(config, None, root_certificate) is True
    with patch.object(handler, "_now", return_value=_NOW + timedelta(days=19)):
        assert handler._leaf_needs_rotation(config, leaf, root_certificate) is False
    with patch.object(handler, "_now", return_value=_NOW + timedelta(days=20)):
        assert handler._leaf_needs_rotation(config, leaf, root_certificate) is True


def test_first_import_tags_certificate_and_registers_its_arn(caplog) -> None:
    """A region without a leaf gets a root-signed import and an SSM association."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    state = _root_state(handler, config)
    _, root_certificate = handler._validate_root_record(state["current"], "current")
    imported_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/fresh"
    ssm = MagicMock()
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "missing"}},
        "GetParameter",
    )
    acm = MagicMock()
    acm.get_paginator.return_value.paginate.return_value = [{"CertificateSummaryList": []}]
    acm.import_certificate.return_value = {"CertificateArn": imported_arn}

    with (
        patch.object(handler, "_now", return_value=_NOW),
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
        caplog.at_level(logging.INFO),
    ):
        arn, expiry, rotated = handler._ensure_certificate(config, state, "us-west-2")

    assert (arn, expiry, rotated) == (imported_arn, _NOW + timedelta(days=30), True)
    import_args = acm.import_certificate.call_args.kwargs
    assert set(import_args) == {"Certificate", "PrivateKey", "Tags"}
    assert import_args["Tags"] == _OWNED_TAGS
    imported = x509.load_pem_x509_certificate(import_args["Certificate"])
    assert handler._certificate_signed_by_root(imported, root_certificate)
    assert handler._certificate_has_server_name(imported, config.server_name)
    private_key = serialization.load_pem_private_key(import_args["PrivateKey"], None)
    assert private_key.public_key().public_numbers() == imported.public_key().public_numbers()
    ssm.put_parameter.assert_called_once_with(
        Name="/gco-test/backend-tls/certificate-arn/us-west-2",
        Value=imported_arn,
        Type="String",
        Overwrite=True,
        Description="Regional ACM certificate ARN for GCO backend TLS in us-west-2",
    )
    acm.delete_certificate.assert_not_called()
    assert "Imported backend leaf certificate in us-west-2" in caplog.text


def test_failed_compensation_is_logged_and_original_error_wins(caplog) -> None:
    """When the compensating delete also fails, the SSM error still propagates."""
    handler = load_lambda_module("tls-certificate-manager")
    config = MagicMock()
    config.project_name = "gco-test"
    config.registry_region = "us-east-1"
    config.certificate_parameter_name.return_value = "/gco-test/tls/us-west-2/certificate-arn"
    imported_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/new-certificate"
    acm = MagicMock()
    acm.import_certificate.return_value = {"CertificateArn": imported_arn}
    acm.delete_certificate.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "DeleteCertificate",
    )
    ssm = MagicMock()
    ssm.put_parameter.side_effect = RuntimeError("registry unavailable")

    with (
        patch.object(handler, "_validate_root_record", return_value=(MagicMock(), MagicMock())),
        patch.object(handler, "_registered_certificate", return_value=(None, None)),
        patch.object(handler, "_recover_unregistered_certificate", return_value=(None, None)),
        patch.object(handler, "_leaf_needs_rotation", return_value=True),
        patch.object(
            handler,
            "_generate_leaf",
            return_value=(b"certificate", b"private-key", _NOW),
        ),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
        caplog.at_level(logging.ERROR),
        pytest.raises(RuntimeError, match="registry unavailable"),
    ):
        handler._ensure_certificate(config, {"current": {}}, "us-west-2")

    acm.delete_certificate.assert_called_once_with(CertificateArn=imported_arn)
    assert "Could not remove unregistered backend leaf certificate in us-west-2" in caplog.text


def test_expiry_metrics_failure_is_logged_not_raised(caplog) -> None:
    """A CloudWatch outage must not fail an otherwise successful reconcile."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    cloudwatch = MagicMock()
    cloudwatch.put_metric_data.side_effect = ClientError(
        {"Error": {"Code": "InternalServiceError", "Message": "unavailable"}},
        "PutMetricData",
    )

    with (
        patch.object(handler, "_now", return_value=_NOW),
        patch.object(handler.boto3, "client", return_value=cloudwatch),
        caplog.at_level(logging.WARNING),
    ):
        handler._publish_expiry_metrics(config, {}, _NOW - timedelta(days=1))

    assert "Could not publish backend TLS expiry metrics" in caplog.text
    metric_data = cloudwatch.put_metric_data.call_args.kwargs["MetricData"]
    root_metric = next(m for m in metric_data if m["MetricName"] == "RootCertificateDaysToExpiry")
    assert root_metric["Value"] == 0.0


# ---------------------------------------------------------------------------
# Custom-resource event handling.
# ---------------------------------------------------------------------------


def test_create_reports_rotated_regions_and_default_physical_id() -> None:
    """Create reconciles every region and reports which ones imported a leaf."""
    handler = load_lambda_module("tls-certificate-manager")
    config = replace(_manager_config(handler), regions=("us-west-2", "eu-west-1"))
    state = {"current": {}, "retired_regions": []}
    expiry = _NOW + timedelta(days=30)

    def ensure_certificate(_config, _state, region):
        return f"arn:{region}", expiry, region == "eu-west-1"

    with (
        patch.object(handler.ManagerConfig, "from_event", return_value=config),
        patch.object(handler, "_ensure_root", return_value=(state, True)),
        patch.object(handler, "_ensure_certificate", side_effect=ensure_certificate),
        patch.object(handler, "_validate_root_record", return_value=(MagicMock(), MagicMock())),
        patch.object(handler, "_certificate_not_after", return_value=expiry),
        patch.object(handler, "_publish_expiry_metrics") as metrics,
        patch.object(handler, "_save_root_state"),
        patch.object(handler, "_delete_regional_certificate") as delete_region,
    ):
        result = handler.lambda_handler({"RequestType": "Create"}, None)

    assert result == {
        "PhysicalResourceId": "gco-test-backend-tls-certificates",
        "Data": {
            "RootChanged": True,
            "RotatedRegions": ["eu-west-1"],
            "ManagedRegionCount": 2,
            "CleanedRetiredRegions": [],
            "PendingRetiredRegions": [],
        },
    }
    metrics.assert_called_once_with(config, {"us-west-2": expiry, "eu-west-1": expiry}, expiry)
    delete_region.assert_not_called()


def test_unsupported_request_type_is_rejected_after_config_validation() -> None:
    """Unknown lifecycle events fail loudly instead of reconciling silently."""
    handler = load_lambda_module("tls-certificate-manager")
    event = {"RequestType": "Read", "ResourceProperties": _resource_properties()}

    with (
        patch.object(handler, "_reconcile") as reconcile,
        patch.object(handler, "_cleanup") as cleanup,
        pytest.raises(ValueError, match="Unsupported certificate manager event: 'Read'"),
    ):
        handler.lambda_handler(event, None)

    reconcile.assert_not_called()
    cleanup.assert_not_called()


@pytest.mark.parametrize(
    ("properties", "message"),
    (
        (None, "OldResourceProperties must be an object"),
        ({}, r"OldResourceProperties\.Regions must be a non-empty list"),
        ({"Regions": []}, r"Regions must be a non-empty list"),
        ({"Regions": "us-west-2"}, r"Regions must be a non-empty list"),
        ({"Regions": ["us-west-2", 7]}, "contains an invalid region"),
        ({"Regions": ["us-west-2", "US-WEST-2"]}, "contains an invalid region"),
        ({"Regions": ["us-west-2", " us-west-2 "]}, "contains a duplicate region"),
    ),
    ids=(
        "not-object",
        "missing-regions",
        "empty-regions",
        "regions-not-list",
        "non-string-region",
        "invalid-region",
        "duplicate-region",
    ),
)
def test_event_regions_rejects_malformed_old_properties(properties, message: str) -> None:
    """Retirement is derived only from a strictly valid previous region list."""
    handler = load_lambda_module("tls-certificate-manager")

    with pytest.raises(ValueError, match=message):
        handler._event_regions(properties, "OldResourceProperties")


def test_retired_regions_from_update_preserves_old_order() -> None:
    """Regions dropped by an Update are returned in their previous order."""
    handler = load_lambda_module("tls-certificate-manager")
    config = replace(_manager_config(handler), regions=("eu-west-1",))
    event = {"OldResourceProperties": {"Regions": ["us-west-2", " eu-west-1", "ap-south-1"]}}

    assert handler._retired_regions_from_update(event, config) == ("us-west-2", "ap-south-1")


# ---------------------------------------------------------------------------
# Cleanup helpers.
# ---------------------------------------------------------------------------


def test_delete_parameter_ignores_missing_but_raises_other_errors() -> None:
    """Cleanup is idempotent for absent parameters and loud for real failures."""
    handler = load_lambda_module("tls-certificate-manager")
    ssm = MagicMock()
    ssm.delete_parameter.side_effect = [
        ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "gone"}},
            "DeleteParameter",
        ),
        ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "DeleteParameter",
        ),
    ]

    handler._delete_parameter(ssm, "/gco-test/backend-tls/root-ca.pem")
    with pytest.raises(ClientError, match="AccessDeniedException"):
        handler._delete_parameter(ssm, "/gco-test/backend-tls/root-ca.pem")

    assert ssm.delete_parameter.call_count == 2


@pytest.mark.parametrize(
    ("responses", "message"),
    (
        ([{"Parameters": "nope"}], "returned malformed parameters"),
        ([{"Parameters": ["nope"]}], "contains a malformed entry"),
        ([{"Parameters": [{"Name": 7, "Value": "x"}]}], "outside the project prefix"),
        (
            [{"Parameters": [{"Name": "/other/backend-tls/certificate-arn/us-west-2"}]}],
            "outside the project prefix",
        ),
        (
            [
                {
                    "Parameters": [
                        {
                            "Name": "/gco-test/backend-tls/certificate-arn/us-west-2",
                            "Value": f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/a",
                        },
                        {
                            "Name": "/gco-test/backend-tls/certificate-arn/us-west-2",
                            "Value": f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/b",
                        },
                    ]
                }
            ],
            "Duplicate certificate registry parameter for us-west-2",
        ),
        ([{"Parameters": [], "NextToken": ""}], "invalid pagination token"),
        ([{"Parameters": [], "NextToken": 3}], "invalid pagination token"),
        (
            [{"Parameters": [], "NextToken": "page"}, {"Parameters": [], "NextToken": "page"}],
            "invalid pagination token",
        ),
    ),
    ids=(
        "parameters-not-list",
        "entry-not-object",
        "name-not-str",
        "name-outside-prefix",
        "duplicate-region",
        "empty-token",
        "token-not-str",
        "repeated-token",
    ),
)
def test_registry_inventory_rejects_malformed_ssm_responses(responses, message: str) -> None:
    """Inventory validation fails before any ownership lookup or deletion."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    ssm = MagicMock()
    ssm.get_parameters_by_path.side_effect = responses
    acm = MagicMock()

    with (
        patch.dict(os.environ, {"AWS_PARTITION": "aws", "AWS_ACCOUNT_ID": _ACCOUNT_ID}),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
        pytest.raises(ValueError, match=message),
    ):
        handler._certificate_registry_regions(config)

    acm.list_tags_for_certificate.assert_not_called()


def test_delete_regional_certificate_recovers_orphan_before_deleting() -> None:
    """An unregistered tagged leaf is adopted and then deleted with its parameter."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/orphan"
    acm = MagicMock()
    ssm = MagicMock()

    with (
        patch.object(handler, "_registered_certificate", return_value=(None, None)),
        patch.object(
            handler,
            "_recover_unregistered_certificate",
            return_value=(certificate_arn, MagicMock()),
        ) as recover,
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
    ):
        assert handler._delete_regional_certificate(config, "us-west-2", defer_in_use=False)

    recover.assert_called_once_with(config, "us-west-2")
    acm.delete_certificate.assert_called_once_with(CertificateArn=certificate_arn)
    ssm.delete_parameter.assert_called_once_with(
        Name="/gco-test/backend-tls/certificate-arn/us-west-2"
    )


def test_delete_regional_certificate_without_any_certificate_clears_parameter() -> None:
    """No registered or recoverable leaf still removes the stale parameter."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    acm = MagicMock()
    ssm = MagicMock()

    with (
        patch.object(handler, "_registered_certificate", return_value=(None, None)),
        patch.object(handler, "_recover_unregistered_certificate", return_value=(None, None)),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
    ):
        assert handler._delete_regional_certificate(config, "eu-west-1", defer_in_use=True)

    acm.delete_certificate.assert_not_called()
    ssm.delete_parameter.assert_called_once_with(
        Name="/gco-test/backend-tls/certificate-arn/eu-west-1"
    )


def test_delete_regional_certificate_tolerates_already_deleted_certificate() -> None:
    """ResourceNotFound from ACM still completes the parameter cleanup."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/gone"
    acm = MagicMock()
    acm.delete_certificate.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
        "DeleteCertificate",
    )
    ssm = MagicMock()

    with (
        patch.object(
            handler, "_registered_certificate", return_value=(certificate_arn, MagicMock())
        ),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
    ):
        assert handler._delete_regional_certificate(config, "us-west-2", defer_in_use=False)

    ssm.delete_parameter.assert_called_once_with(
        Name="/gco-test/backend-tls/certificate-arn/us-west-2"
    )


@pytest.mark.parametrize(
    ("code", "defer_in_use"),
    (("ResourceInUseException", False), ("AccessDeniedException", True)),
    ids=("in-use-on-delete-event", "unexpected-error"),
)
def test_delete_regional_certificate_raises_when_it_cannot_defer(
    code: str, defer_in_use: bool
) -> None:
    """Delete events fail on attached leaves; unexpected ACM errors always fail."""
    handler = load_lambda_module("tls-certificate-manager")
    config = _manager_config(handler)
    certificate_arn = f"arn:aws:acm:us-west-2:{_ACCOUNT_ID}:certificate/attached"
    acm = MagicMock()
    acm.delete_certificate.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "refused"}},
        "DeleteCertificate",
    )
    ssm = MagicMock()

    with (
        patch.object(
            handler, "_registered_certificate", return_value=(certificate_arn, MagicMock())
        ),
        patch.object(handler.boto3, "client", side_effect=_client_factory(acm=acm, ssm=ssm)),
        pytest.raises(ClientError, match=code),
    ):
        handler._delete_regional_certificate(config, "us-west-2", defer_in_use=defer_in_use)

    ssm.delete_parameter.assert_not_called()
