"""Focused failure-compensation tests for the backend TLS certificate manager."""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import ANY, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from cryptography import x509

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
