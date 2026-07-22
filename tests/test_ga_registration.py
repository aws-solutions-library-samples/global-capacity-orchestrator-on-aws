"""Focused tests for the Gateway ALB registration Lambda.

The suite covers exact Gateway API discovery, fail-closed tag fallback,
optional Global Accelerator registration, mandatory SSM publication, temporary
CA removal, and both CloudFormation and Step Functions entrypoints.
"""

import json
from unittest.mock import ANY, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests._lambda_imports import load_lambda_module


@pytest.fixture
def ga_module():
    """Load ga-registration with AWS and HTTP constructors isolated."""
    with (
        patch("boto3.client") as mock_boto_client,
        patch("boto3.Session"),
        patch("urllib3.PoolManager") as mock_pool,
    ):
        handler = load_lambda_module("ga-registration")
        yield handler, mock_boto_client, mock_pool


PLATFORM_ALB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/k8s-gcogateway/abc"
PLATFORM_ALB_DNS = "k8s-gcogateway-abc.us-east-1.elb.amazonaws.com"
OTHER_ALB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/k8s-gcosyste/other"
STALE_ALB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/k8s-stale/old"
SLURM_NLB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/net/k8s-gcojobs/nlb"
ENDPOINT_GROUP_ARN = (
    "arn:aws:globalaccelerator::123:accelerator/abc/listener/def/endpoint-group/ghi"
)
ACCELERATOR_ARN = "arn:aws:globalaccelerator::123:accelerator/abc"


def _response(status: int, payload: dict | None = None) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.data = json.dumps(payload or {}).encode("utf-8")
    return response


def _gateway_payload(*addresses: dict) -> dict:
    return {"status": {"addresses": list(addresses)}}


def _make_alb(
    arn: str,
    name: str,
    dns: str,
    *,
    state: str = "active",
    lb_type: str = "application",
    scheme: str = "internal",
) -> dict:
    return {
        "LoadBalancerArn": arn,
        "LoadBalancerName": name,
        "DNSName": dns,
        "State": {"Code": state},
        "Type": lb_type,
        "Scheme": scheme,
    }


def _make_tags(arn: str, tags: dict[str, str]) -> dict:
    return {
        "ResourceArn": arn,
        "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
    }


def _client_error(code: str, operation: str = "Operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _make_cfn_event(request_type: str = "Create", *, endpoint_group: bool = True) -> dict:
    properties = {
        "ClusterName": "test-cluster",
        "Region": "us-east-1",
        "RegistryRegion": "eu-west-1",
        "ProjectName": "gco",
    }
    if endpoint_group:
        properties["EndpointGroupArn"] = ENDPOINT_GROUP_ARN
    return {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-response.example.com/callback",
        "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
        "RequestId": "req-123",
        "LogicalResourceId": "GaRegistration",
        "ResourceProperties": properties,
    }


def _context() -> MagicMock:
    context = MagicMock()
    context.log_stream_name = "test-log-stream"
    return context


class TestEksAuthentication:
    @pytest.mark.parametrize(
        "region,sts_endpoint,signed_url",
        [
            (
                "cn-north-1",
                "https://sts.cn-north-1.amazonaws.com.cn",
                "https://sts.cn-north-1.amazonaws.com.cn/?X-Amz-Credential=cn-scope",
            ),
            (
                "us-west-2",
                "https://sts.amazonaws.com",
                "https://sts.amazonaws.com/?X-Amz-Credential=us-east-1-scope",
            ),
        ],
    )
    def test_uses_resolved_endpoint_and_client_signing_scope(
        self,
        ga_module,
        region,
        sts_endpoint,
        signed_url,
    ):
        handler, mock_boto_client, _ = ga_module
        eks = MagicMock()
        eks.describe_cluster.return_value = {
            "cluster": {
                "endpoint": f"https://eks.{region}.example",
                "certificateAuthority": {"data": "Y2E="},
            }
        }
        mock_boto_client.return_value = eks
        sts_client = MagicMock()
        sts_client.meta.endpoint_url = sts_endpoint
        sts_client._request_signer.generate_presigned_url.return_value = signed_url
        session = MagicMock()
        session.client.return_value = sts_client

        with patch.object(handler.boto3, "Session", return_value=session):
            endpoint, token, ca_path = handler.get_k8s_client(
                f"gco-{region}",
                region,
            )

        try:
            session.client.assert_called_once_with("sts", region_name=region)
            sts_client._request_signer.generate_presigned_url.assert_called_once_with(
                request_dict={
                    "method": "GET",
                    "url": (f"{sts_endpoint}/?Action=GetCallerIdentity&Version=2011-06-15"),
                    "body": {},
                    "headers": {"x-k8s-aws-id": f"gco-{region}"},
                    "context": {},
                },
                operation_name="GetCallerIdentity",
                expires_in=60,
            )
            assert endpoint == f"https://eks.{region}.example"
            assert token.startswith("k8s-aws-v1.")
        finally:
            handler._remove_temporary_ca_file(ca_path)


class TestGatewayStatusDiscovery:
    def test_reads_only_the_exact_gateway_path_and_nonempty_hostname(self, ga_module):
        handler, _, _ = ga_module
        http = MagicMock()
        http.request.return_value = _response(
            200,
            _gateway_payload(
                {"type": "Hostname", "value": "   "},
                {"type": "IPAddress", "value": "10.0.0.1"},
                {"type": "Hostname", "value": f" {PLATFORM_ALB_DNS} "},
            ),
        )

        address = handler.find_gateway_address(http, "https://k8s.example", {"Auth": "x"})

        assert address == PLATFORM_ALB_DNS
        http.request.assert_called_once_with(
            "GET",
            "https://k8s.example/apis/gateway.networking.k8s.io/v1/"
            "namespaces/gco-system/gateways/gco-gateway",
            headers={"Auth": "x"},
            timeout=10.0,
        )

    @pytest.mark.parametrize(
        "status,payload",
        [
            (404, {}),
            (200, _gateway_payload()),
            (200, _gateway_payload({"type": "Hostname", "value": ""})),
        ],
    )
    def test_returns_none_until_exact_gateway_has_an_address(self, ga_module, status, payload):
        handler, _, _ = ga_module
        http = MagicMock()
        http.request.return_value = _response(status, payload)

        assert handler.find_gateway_address(http, "https://k8s", {}) is None

    def test_accepts_default_hostname_type(self, ga_module):
        handler, _, _ = ga_module
        http = MagicMock()
        http.request.return_value = _response(200, _gateway_payload({"value": PLATFORM_ALB_DNS}))

        assert handler.find_gateway_address(http, "https://k8s", {}) == PLATFORM_ALB_DNS


class TestGatewayHostnameLookup:
    def test_returns_only_matching_internal_application_alb(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(
                    SLURM_NLB_ARN,
                    "same-dns-nlb",
                    PLATFORM_ALB_DNS,
                    lb_type="network",
                ),
                _make_alb(
                    STALE_ALB_ARN,
                    "same-dns-public",
                    PLATFORM_ALB_DNS,
                    scheme="internet-facing",
                ),
                _make_alb(
                    PLATFORM_ALB_ARN,
                    "gateway",
                    PLATFORM_ALB_DNS,
                    state="provisioning",
                ),
            ]
        }

        assert handler.find_alb_by_gateway_hostname(elb, PLATFORM_ALB_DNS) == (
            PLATFORM_ALB_DNS,
            PLATFORM_ALB_ARN,
            "provisioning",
        )

    def test_returns_none_for_an_unrelated_hostname(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [_make_alb(PLATFORM_ALB_ARN, "gateway", PLATFORM_ALB_DNS)]
        }

        assert handler.find_alb_by_gateway_hostname(elb, "other.example.com") == (
            None,
            None,
            None,
        )


class TestExactTagFallback:
    def _find(self, handler, tags, *, lb_type="application", scheme="internal"):
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(
                    PLATFORM_ALB_ARN,
                    "gateway",
                    PLATFORM_ALB_DNS,
                    lb_type=lb_type,
                    scheme=scheme,
                )
            ]
        }
        elb.describe_tags.return_value = {"TagDescriptions": [_make_tags(PLATFORM_ALB_ARN, tags)]}
        return handler.find_platform_alb_by_tags(elb, "test-cluster"), elb

    def test_requires_both_exact_gateway_and_cluster_tags(self, ga_module):
        handler, _, _ = ga_module

        result, _ = self._find(
            handler,
            {
                "gco.aws/gateway": "gco-system/gco-gateway",
                "elbv2.k8s.aws/cluster": "test-cluster",
            },
        )

        assert result == (PLATFORM_ALB_DNS, PLATFORM_ALB_ARN, "active")

    @pytest.mark.parametrize(
        "tags",
        [
            {"elbv2.k8s.aws/cluster": "test-cluster"},
            {"gco.aws/gateway": "gco-system/gco-gateway"},
            {
                "gco.aws/gateway": "gco-system/gco-gateway",
                "elbv2.k8s.aws/cluster": "other-cluster",
            },
            {
                "gco.aws/gateway": "gco-system/other-gateway",
                "elbv2.k8s.aws/cluster": "test-cluster",
            },
            {
                "gco.aws/gateway": "gco-system/gco-gateway",
                "eks:eks-cluster-name": "test-cluster",
            },
            {
                "ingress.k8s.aws/stack": "gco-system/gco-ingress",
                "elbv2.k8s.aws/cluster": "test-cluster",
            },
        ],
    )
    def test_rejects_partial_alternative_or_legacy_tag_matches(self, ga_module, tags):
        handler, _, _ = ga_module

        result, _ = self._find(handler, tags)

        assert result == (None, None, None)

    @pytest.mark.parametrize(
        "lb_type,scheme",
        [("network", "internal"), ("application", "internet-facing")],
    )
    def test_rejects_non_internal_albs(self, ga_module, lb_type, scheme):
        handler, _, _ = ga_module

        result, elb = self._find(
            handler,
            {
                "gco.aws/gateway": "gco-system/gco-gateway",
                "elbv2.k8s.aws/cluster": "test-cluster",
            },
            lb_type=lb_type,
            scheme=scheme,
        )

        assert result == (None, None, None)
        elb.describe_tags.assert_not_called()


class TestFindActiveGatewayAlb:
    def test_gateway_status_is_authoritative(self, ga_module):
        handler, _, _ = ga_module
        with (
            patch.object(handler, "find_gateway_address", return_value=PLATFORM_ALB_DNS),
            patch.object(
                handler,
                "find_alb_by_gateway_hostname",
                return_value=(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN, "active"),
            ),
            patch.object(handler, "find_platform_alb_by_tags") as fallback,
        ):
            result = handler.find_active_alb(
                MagicMock(), MagicMock(), "https://k8s", {}, "test-cluster"
            )

        assert result == (PLATFORM_ALB_DNS, PLATFORM_ALB_ARN)
        fallback.assert_not_called()

    def test_does_not_fallback_when_status_alb_is_still_provisioning(self, ga_module):
        handler, _, _ = ga_module
        with (
            patch.object(handler, "find_gateway_address", return_value=PLATFORM_ALB_DNS),
            patch.object(
                handler,
                "find_alb_by_gateway_hostname",
                return_value=(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN, "provisioning"),
            ),
            patch.object(handler, "find_platform_alb_by_tags") as fallback,
        ):
            result = handler.find_active_alb(
                MagicMock(), MagicMock(), "https://k8s", {}, "test-cluster"
            )

        assert result == (None, None)
        fallback.assert_not_called()

    def test_uses_exact_tags_only_when_gateway_address_is_empty(self, ga_module):
        handler, _, _ = ga_module
        with (
            patch.object(handler, "find_gateway_address", return_value=None),
            patch.object(
                handler,
                "find_platform_alb_by_tags",
                return_value=(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN, "active"),
            ) as fallback,
        ):
            result = handler.find_active_alb(
                MagicMock(), MagicMock(), "https://k8s", {}, "test-cluster"
            )

        assert result == (PLATFORM_ALB_DNS, PLATFORM_ALB_ARN)
        fallback.assert_called_once()


class TestGlobalAcceleratorConvergence:
    def test_register_is_idempotent(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {"EndpointDescriptions": [{"EndpointId": PLATFORM_ALB_ARN}]}
        }

        handler.register_alb_with_ga(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        ga.add_endpoints.assert_not_called()

    def test_scrubs_every_endpoint_except_exact_gateway_alb(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [
                    {"EndpointId": PLATFORM_ALB_ARN},
                    {"EndpointId": OTHER_ALB_ARN},
                    {"EndpointId": STALE_ALB_ARN},
                ]
            }
        }

        handler.scrub_stale_ga_endpoints(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        assert {
            request.kwargs["EndpointIdentifiers"][0]["EndpointId"]
            for request in ga.remove_endpoints.call_args_list
        } == {OTHER_ALB_ARN, STALE_ALB_ARN}

    def test_stale_endpoint_removal_failure_is_not_treated_as_success(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [
                    {"EndpointId": PLATFORM_ALB_ARN},
                    {"EndpointId": STALE_ALB_ARN},
                ]
            }
        }
        ga.remove_endpoints.side_effect = _client_error("AccessDeniedException", "RemoveEndpoints")

        with pytest.raises(ClientError):
            handler.scrub_stale_ga_endpoints(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

    def test_enforces_https_and_preserves_only_expected_alb(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "HealthCheckProtocol": "TCP",
                "HealthCheckPort": 80,
                "EndpointDescriptions": [
                    {"EndpointId": PLATFORM_ALB_ARN, "Weight": 100},
                    {"EndpointId": STALE_ALB_ARN, "Weight": 50},
                ],
            }
        }

        handler.ensure_https_health_check(
            ga,
            ENDPOINT_GROUP_ARN,
            expected_alb_arn=PLATFORM_ALB_ARN,
        )

        ga.update_endpoint_group.assert_called_once_with(
            EndpointGroupArn=ENDPOINT_GROUP_ARN,
            HealthCheckPort=443,
            HealthCheckProtocol="HTTPS",
            HealthCheckPath="/api/v1/health",
            HealthCheckIntervalSeconds=30,
            ThresholdCount=3,
            EndpointConfigurations=[
                {
                    "EndpointId": PLATFORM_ALB_ARN,
                    "Weight": 100,
                    "ClientIPPreservationEnabled": True,
                }
            ],
        )

    def test_skips_health_update_when_contract_already_matches(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "HealthCheckProtocol": "HTTPS",
                "HealthCheckPort": 443,
                "HealthCheckPath": "/api/v1/health",
            }
        }

        handler.ensure_https_health_check(ga, ENDPOINT_GROUP_ARN)

        ga.update_endpoint_group.assert_not_called()

    def test_https_enforcement_failure_is_not_treated_as_success(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {"EndpointGroup": {"HealthCheckProtocol": "TCP"}}
        ga.update_endpoint_group.side_effect = _client_error(
            "AccessDeniedException", "UpdateEndpointGroup"
        )

        with pytest.raises(ClientError):
            handler.ensure_https_health_check(ga, ENDPOINT_GROUP_ARN)


class TestRegisterGatewayEndpoint:
    def _core_patches(self, handler, *, order=None):
        call_order = order if order is not None else []
        return (
            patch.object(
                handler,
                "get_k8s_client",
                return_value=("https://k8s", "token", "/tmp/gco-ca.crt"),
            ),
            patch.object(
                handler,
                "find_active_alb",
                return_value=(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN),
            ),
            patch.object(
                handler,
                "store_alb_hostname_in_ssm",
                side_effect=lambda *_args: call_order.append("publish"),
            ),
            patch.object(handler, "_remove_temporary_ca_file"),
        )

    def test_without_ga_always_publishes(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        elb = MagicMock()
        mock_boto_client.return_value = elb
        order = []
        get_k8s, find_alb, publish, remove_ca = self._core_patches(handler, order=order)
        with get_k8s, find_alb, publish as publish_mock, remove_ca:
            result = handler.register_ga_endpoint(
                "test-cluster",
                "us-east-1",
                endpoint_group_arn=None,
                registry_region="eu-west-1",
                project_name="project",
            )

        assert result == {"AlbArn": PLATFORM_ALB_ARN, "AlbHostname": PLATFORM_ALB_DNS}
        assert [request.args[0] for request in mock_boto_client.call_args_list] == ["elbv2"]
        publish_mock.assert_called_once_with("us-east-1", PLATFORM_ALB_DNS, "eu-west-1", "project")
        assert order == ["publish"]

    def test_with_ga_registers_scrubs_enforces_then_publishes(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        elb = MagicMock()
        ga = MagicMock()
        mock_boto_client.side_effect = lambda service, **_kwargs: (
            ga if service == "globalaccelerator" else elb
        )
        order = []
        get_k8s, find_alb, publish, remove_ca = self._core_patches(handler, order=order)
        with (
            get_k8s,
            find_alb,
            publish,
            remove_ca,
            patch.object(handler, "register_alb_with_ga") as register,
            patch.object(handler, "scrub_stale_ga_endpoints") as scrub,
            patch.object(handler, "ensure_https_health_check") as health,
            patch.object(
                handler,
                "wait_for_accelerator_deployed",
                side_effect=lambda *_args, **_kwargs: order.append("deployed") or True,
            ) as wait,
        ):
            handler.register_ga_endpoint(
                "test-cluster",
                "us-east-1",
                endpoint_group_arn=ENDPOINT_GROUP_ARN,
            )

        register.assert_called_once_with(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)
        scrub.assert_called_once_with(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)
        health.assert_called_once_with(
            ga,
            ENDPOINT_GROUP_ARN,
            expected_alb_arn=PLATFORM_ALB_ARN,
        )
        # AddEndpoints only submits a configuration change: publication (and
        # therefore deploy success) must wait for the accelerator to serve the
        # endpoint from its edge locations, strictly and within a bounded wait.
        wait.assert_called_once_with(ga, ENDPOINT_GROUP_ARN, timeout_seconds=ANY, strict=True)
        budget = wait.call_args.kwargs["timeout_seconds"]
        assert 0 < budget <= handler.GA_DEPLOYED_WAIT_SECONDS
        assert order == ["deployed", "publish"]

    def test_registration_never_deployed_blocks_publication(self, ga_module):
        # Regression: a live run's first health probe black-holed because
        # registration returned success while Global Accelerator was still
        # propagating the new endpoint. A wait that ends without DEPLOYED must
        # fail the registration instead of publishing a dead endpoint.
        handler, mock_boto_client, _ = ga_module
        elb = MagicMock()
        ga = MagicMock()
        mock_boto_client.side_effect = lambda service, **_kwargs: (
            ga if service == "globalaccelerator" else elb
        )
        get_k8s, find_alb, publish, remove_ca = self._core_patches(handler)
        with (
            get_k8s,
            find_alb,
            publish as publish_mock,
            remove_ca as remove_ca_mock,
            patch.object(handler, "register_alb_with_ga"),
            patch.object(handler, "scrub_stale_ga_endpoints"),
            patch.object(handler, "ensure_https_health_check"),
            patch.object(handler, "wait_for_accelerator_deployed", return_value=False),
            pytest.raises(TimeoutError, match="did not reach DEPLOYED"),
        ):
            handler.register_ga_endpoint(
                "test-cluster",
                "us-east-1",
                endpoint_group_arn=ENDPOINT_GROUP_ARN,
            )

        publish_mock.assert_not_called()
        remove_ca_mock.assert_called_once_with("/tmp/gco-ca.crt")

    def test_registration_with_exhausted_budget_fails_without_waiting(self, ga_module):
        # When no wall-clock budget remains for the DEPLOYED wait (for example
        # after a pathologically slow ALB wait), the handler must fail
        # honestly and immediately rather than wait past its own budget.
        # Zeroing the wait constant drives remaining_budget to the <= 0 branch.
        handler, mock_boto_client, _ = ga_module
        elb = MagicMock()
        ga = MagicMock()
        mock_boto_client.side_effect = lambda service, **_kwargs: (
            ga if service == "globalaccelerator" else elb
        )
        get_k8s, find_alb, publish, remove_ca = self._core_patches(handler)
        with (
            get_k8s,
            find_alb,
            publish as publish_mock,
            remove_ca,
            patch.object(handler, "register_alb_with_ga"),
            patch.object(handler, "scrub_stale_ga_endpoints"),
            patch.object(handler, "ensure_https_health_check"),
            patch.object(handler, "wait_for_accelerator_deployed") as wait,
            patch.object(handler, "GA_DEPLOYED_WAIT_SECONDS", 0),
            pytest.raises(TimeoutError, match="did not reach DEPLOYED"),
        ):
            handler.register_ga_endpoint(
                "test-cluster",
                "us-east-1",
                endpoint_group_arn=ENDPOINT_GROUP_ARN,
            )

        wait.assert_not_called()
        publish_mock.assert_not_called()

    def test_ga_convergence_failure_blocks_publication(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        elb = MagicMock()
        ga = MagicMock()
        mock_boto_client.side_effect = lambda service, **_kwargs: (
            ga if service == "globalaccelerator" else elb
        )
        get_k8s, find_alb, publish, remove_ca = self._core_patches(handler)
        with (
            get_k8s,
            find_alb,
            publish as publish_mock,
            remove_ca as remove_ca_mock,
            patch.object(handler, "register_alb_with_ga"),
            patch.object(
                handler,
                "scrub_stale_ga_endpoints",
                side_effect=RuntimeError("scrub failed"),
            ),
            pytest.raises(RuntimeError, match="scrub failed"),
        ):
            handler.register_ga_endpoint(
                "test-cluster",
                "us-east-1",
                endpoint_group_arn=ENDPOINT_GROUP_ARN,
            )

        publish_mock.assert_not_called()
        remove_ca_mock.assert_called_once_with("/tmp/gco-ca.crt")

    def test_always_unlinks_ca_when_publication_fails(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        mock_boto_client.return_value = MagicMock()
        with (
            patch.object(
                handler,
                "get_k8s_client",
                return_value=("https://k8s", "token", "/tmp/gco-ca.crt"),
            ),
            patch.object(
                handler,
                "find_active_alb",
                return_value=(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN),
            ),
            patch.object(
                handler,
                "store_alb_hostname_in_ssm",
                side_effect=RuntimeError("SSM failed"),
            ),
            patch.object(handler, "_remove_temporary_ca_file") as remove_ca,
            pytest.raises(RuntimeError, match="SSM failed"),
        ):
            handler.register_ga_endpoint("test-cluster", "us-east-1")

        remove_ca.assert_called_once_with("/tmp/gco-ca.crt")

    def test_unlinks_ca_when_gateway_discovery_times_out(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        mock_boto_client.return_value = MagicMock()
        with (
            patch.object(
                handler,
                "get_k8s_client",
                return_value=("https://k8s", "token", "/tmp/gco-ca.crt"),
            ),
            patch.object(handler, "MAX_WAIT_SECONDS", 0),
            patch.object(handler, "_remove_temporary_ca_file") as remove_ca,
            pytest.raises(TimeoutError, match="gco-system/gco-gateway"),
        ):
            handler.register_ga_endpoint("test-cluster", "us-east-1")

        remove_ca.assert_called_once_with("/tmp/gco-ca.crt")


class TestRegistryPublication:
    def test_stores_exact_parameter_in_registry_region(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        ssm = MagicMock()
        mock_boto_client.return_value = ssm

        handler.store_alb_hostname_in_ssm("us-east-1", PLATFORM_ALB_DNS, "eu-west-1", "project")

        mock_boto_client.assert_called_once_with("ssm", region_name="eu-west-1")
        ssm.put_parameter.assert_called_once_with(
            Name="/project/alb-hostname-us-east-1",
            Value=PLATFORM_ALB_DNS,
            Type="String",
            Overwrite=True,
            Description="ALB hostname for us-east-1 regional cluster",
        )

    def test_registry_region_precedes_legacy_alias(self, ga_module):
        handler, _, _ = ga_module

        assert (
            handler._get_registry_region(
                {"RegistryRegion": "eu-west-1", "GlobalRegion": "us-east-2"}
            )
            == "eu-west-1"
        )

    def test_temporary_ca_removal_uses_unlink(self, ga_module):
        handler, _, _ = ga_module
        with patch.object(handler.os, "unlink") as unlink:
            handler._remove_temporary_ca_file("/tmp/ca.crt")

        unlink.assert_called_once_with("/tmp/ca.crt")


class TestDeletePaths:
    def test_raw_delete_without_ga_still_removes_ssm(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        event = _make_cfn_event("Delete", endpoint_group=False)
        with (
            patch.object(handler, "delete_alb_hostname_from_ssm") as delete_ssm,
            patch.object(handler, "send_response") as send_response,
        ):
            handler.handle_delete(
                event,
                _context(),
                event["ResourceProperties"],
                "physical-id",
            )

        mock_boto_client.assert_not_called()
        delete_ssm.assert_called_once_with("us-east-1", "eu-west-1", "gco")
        send_response.assert_called_once_with(event, ANY, "SUCCESS", {}, "physical-id")

    def test_raw_delete_attempts_ssm_even_when_ga_deregistration_fails(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        event = _make_cfn_event("Delete")
        ga = MagicMock()
        mock_boto_client.return_value = ga
        with (
            patch.object(
                handler,
                "deregister_alb_from_ga",
                side_effect=RuntimeError("GA failed"),
            ) as deregister,
            patch.object(handler, "delete_alb_hostname_from_ssm") as delete_ssm,
            patch.object(handler, "send_response"),
        ):
            handler.handle_delete(
                event,
                _context(),
                event["ResourceProperties"],
                "physical-id",
            )

        deregister.assert_called_once_with(ga, ENDPOINT_GROUP_ARN)
        delete_ssm.assert_called_once_with("us-east-1", "eu-west-1", "gco")

    def test_provider_delete_without_ga_uses_default_registry_region(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        event = {
            "RequestType": "Delete",
            "ResourceProperties": {"Region": "us-east-1", "ProjectName": "gco"},
        }
        with patch.object(handler, "delete_alb_hostname_from_ssm") as delete_ssm:
            result = handler.on_delete_event(event)

        mock_boto_client.assert_not_called()
        delete_ssm.assert_called_once_with("us-east-1", "us-east-2", "gco")
        assert result["PhysicalResourceId"] == "ga-dereg-us-east-1"

    def test_provider_create_and_update_remain_noops(self, ga_module):
        handler, _, _ = ga_module
        for request_type in ("Create", "Update"):
            event = {
                "RequestType": request_type,
                "PhysicalResourceId": "stable-id",
                "ResourceProperties": {"Region": "us-east-1"},
            }
            with patch.object(handler, "delete_alb_hostname_from_ssm") as delete_ssm:
                assert handler.on_delete_event(event) == {"PhysicalResourceId": "stable-id"}
            delete_ssm.assert_not_called()

    def test_deregister_removes_endpoints_then_waits(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        with (
            patch.object(handler, "remove_ga_endpoints") as remove,
            patch.object(handler, "wait_for_accelerator_deployed") as wait,
        ):
            handler.deregister_alb_from_ga(ga, ENDPOINT_GROUP_ARN)

        remove.assert_called_once_with(ga, ENDPOINT_GROUP_ARN)
        wait.assert_called_once_with(ga, ENDPOINT_GROUP_ARN, strict=False)

    def test_strict_deregister_rejects_failed_deployment_wait(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        with (
            patch.object(handler, "remove_ga_endpoints") as remove,
            patch.object(handler, "wait_for_accelerator_deployed", return_value=False),
            pytest.raises(TimeoutError, match="did not reach DEPLOYED"),
        ):
            handler.deregister_alb_from_ga(ga, ENDPOINT_GROUP_ARN, strict=True)

        remove.assert_called_once_with(ga, ENDPOINT_GROUP_ARN, strict=True)

    def test_strict_endpoint_cleanup_accepts_absent_endpoint_group(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.side_effect = _client_error(
            "EndpointGroupNotFoundException",
            "DescribeEndpointGroup",
        )

        handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN, strict=True)

        ga.remove_endpoints.assert_not_called()

    def test_strict_wait_raises_describe_failures_instead_of_timeout(self, ga_module):
        """Regression: an AccessDenied describe was mislabeled as a GA timeout.

        The teardown-time strict wait must surface the real error; reporting a
        permissions gap as "did not reach DEPLOYED" sent operators debugging
        Global Accelerator propagation instead of IAM.
        """
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_accelerator.side_effect = _client_error(
            "AccessDeniedException", "DescribeAccelerator"
        )

        with pytest.raises(ClientError, match="AccessDeniedException"):
            handler.deregister_alb_from_ga(ga, ENDPOINT_GROUP_ARN, strict=True)

        # The lenient path retains its best-effort behavior.
        assert handler.wait_for_accelerator_deployed(ga, ENDPOINT_GROUP_ARN) is False

    def test_wait_for_accelerator_uses_derived_arn(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_accelerator.return_value = {"Accelerator": {"Status": "DEPLOYED"}}

        assert handler.wait_for_accelerator_deployed(ga, ENDPOINT_GROUP_ARN) is True
        ga.describe_accelerator.assert_called_once_with(AcceleratorArn=ACCELERATOR_ARN)


class TestInvocationContracts:
    def test_step_functions_task_allows_missing_endpoint_group(self, ga_module):
        handler, _, _ = ga_module
        event = {
            "Action": "Register",
            "ClusterName": "test-cluster",
            "Region": "us-east-1",
            "RegistryRegion": "eu-west-1",
            "ProjectName": "project",
        }
        expected = {"AlbArn": PLATFORM_ALB_ARN, "AlbHostname": PLATFORM_ALB_DNS}
        with patch.object(handler, "register_ga_endpoint", return_value=expected) as register:
            assert handler.handle_task(event) == expected

        register.assert_called_once_with(
            cluster_name="test-cluster",
            region="us-east-1",
            endpoint_group_arn=None,
            registry_region="eu-west-1",
            project_name="project",
        )

    def test_step_functions_cleanup_strictly_fences_registry_and_ga(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        ga = MagicMock()
        mock_boto_client.return_value = ga
        event = {
            "Action": "cleanup_gateway_endpoint",
            "Region": "us-east-1",
            "RegistryRegion": "eu-west-1",
            "ProjectName": "project",
            "EndpointGroupArn": ENDPOINT_GROUP_ARN,
        }
        with (
            patch.object(handler, "delete_alb_hostname_from_ssm") as delete_ssm,
            patch.object(handler, "deregister_alb_from_ga") as deregister,
        ):
            result = handler.handle_task(event)

        delete_ssm.assert_called_once_with(
            "us-east-1",
            "eu-west-1",
            "project",
            strict=True,
        )
        mock_boto_client.assert_called_once_with(
            "globalaccelerator",
            region_name="us-west-2",
        )
        deregister.assert_called_once_with(ga, ENDPOINT_GROUP_ARN, strict=True)
        assert result == {
            "RegistryParameterDeleted": True,
            "GlobalAcceleratorDeregistered": True,
        }

    def test_lambda_dispatches_step_functions_action(self, ga_module):
        handler, _, _ = ga_module
        event = {"Action": "Register"}
        with patch.object(handler, "handle_task", return_value={"ok": True}) as task:
            assert handler.lambda_handler(event, MagicMock()) == {"ok": True}

        task.assert_called_once_with(event)

    def test_cloudformation_create_preserves_stable_physical_id(self, ga_module):
        handler, _, _ = ga_module
        event = _make_cfn_event("Create", endpoint_group=False)
        with patch.object(handler, "handle_create_update") as create:
            handler.lambda_handler(event, _context())

        create.assert_called_once_with(
            event,
            ANY,
            event["ResourceProperties"],
            "ga-reg-test-cluster",
        )

    def test_cloudformation_delete_always_responds_success_on_unhandled_error(self, ga_module):
        handler, _, mock_pool = ga_module
        event = _make_cfn_event("Delete", endpoint_group=False)
        with patch.object(handler, "handle_delete", side_effect=RuntimeError("boom")):
            handler.lambda_handler(event, _context())

        request = mock_pool.return_value.request.call_args
        response_body = json.loads(request.kwargs["body"])
        assert response_body["Status"] == "SUCCESS"
