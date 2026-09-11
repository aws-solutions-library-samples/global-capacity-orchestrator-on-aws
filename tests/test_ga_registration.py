"""Focused tests for the Gateway ALB registration Lambda.

The suite covers exact Gateway API discovery, fail-closed tag fallback,
optional Global Accelerator registration, mandatory SSM publication, temporary
CA removal, and both CloudFormation and Step Functions entrypoints.
"""

import json
import logging
from unittest.mock import ANY, MagicMock, call, patch

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

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


class _FakeClock:
    """Deterministic stand-in for the handler's ``time`` module.

    ``sleep`` advances the clock instead of blocking, so the polling loops run
    instantly while still observing realistic elapsed wall-clock time.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _make_ga_endpoint(endpoint_id: str, *, healthy: bool = True) -> dict:
    return {
        "EndpointId": endpoint_id,
        "Weight": 100,
        "HealthState": "HEALTHY" if healthy else "UNHEALTHY",
        "ClientIPPreservationEnabled": True,
    }


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

    def test_removes_temporary_ca_when_the_certificate_cannot_be_written(self, ga_module, tmp_path):
        """A failed CA write must not leak the mkstemp file or mask the error."""
        handler, mock_boto_client, _ = ga_module
        eks = MagicMock()
        eks.describe_cluster.return_value = {
            "cluster": {
                "endpoint": "https://eks.us-east-1.example",
                "certificateAuthority": {"data": "Y2E="},
            }
        }
        mock_boto_client.return_value = eks
        sts_client = MagicMock()
        sts_client.meta.endpoint_url = "https://sts.us-east-1.amazonaws.com"
        sts_client._request_signer.generate_presigned_url.return_value = (
            "https://sts.us-east-1.amazonaws.com/?X-Amz-Credential=scope"
        )
        session = MagicMock()
        session.client.return_value = sts_client
        real_mkstemp = handler.tempfile.mkstemp

        with (
            patch.object(handler.boto3, "Session", return_value=session),
            patch.object(
                handler.tempfile,
                "mkstemp",
                side_effect=lambda **kwargs: real_mkstemp(dir=tmp_path, **kwargs),
            ),
            patch.object(handler.os, "fchmod", side_effect=PermissionError("fchmod denied")),
            pytest.raises(PermissionError, match="fchmod denied"),
        ):
            handler.get_k8s_client("test-cluster", "us-east-1")

        assert list(tmp_path.iterdir()) == []


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

    def test_warns_and_returns_none_for_unexpected_http_status(self, ga_module, caplog):
        handler, _, _ = ga_module
        http = MagicMock()
        http.request.return_value = _response(
            403,
            {
                "kind": "Status",
                "apiVersion": "v1",
                "status": "Failure",
                "reason": "Forbidden",
                "code": 403,
            },
        )

        assert handler.find_gateway_address(http, "https://k8s", {}) is None
        assert "Gateway status request returned HTTP 403" in caplog.text

    def test_skips_malformed_address_entries(self, ga_module):
        handler, _, _ = ga_module
        http = MagicMock()
        http.request.return_value = _response(
            200,
            {
                "status": {
                    "addresses": [
                        "not-an-address-object",
                        {"type": "Hostname", "value": PLATFORM_ALB_DNS},
                    ]
                }
            },
        )

        assert handler.find_gateway_address(http, "https://k8s", {}) == PLATFORM_ALB_DNS

    def test_decodes_text_bodies(self, ga_module):
        handler, _, _ = ga_module
        http = MagicMock()
        response = MagicMock()
        response.status = 200
        response.data = json.dumps(
            _gateway_payload({"type": "Hostname", "value": PLATFORM_ALB_DNS})
        )
        http.request.return_value = response

        assert handler.find_gateway_address(http, "https://k8s", {}) == PLATFORM_ALB_DNS

    def test_ignores_non_object_documents(self, ga_module):
        handler, _, _ = ga_module
        http = MagicMock()
        response = MagicMock()
        response.status = 200
        response.data = b"[]"
        http.request.return_value = response

        assert handler.find_gateway_address(http, "https://k8s", {}) is None

    def test_request_failures_fall_back_to_none(self, ga_module, caplog):
        handler, _, _ = ga_module
        http = MagicMock()
        http.request.side_effect = handler.urllib3.exceptions.ReadTimeoutError(
            None, "https://k8s", "Read timed out. (read timeout=10.0)"
        )

        assert handler.find_gateway_address(http, "https://k8s", {}) is None
        assert "Error checking Gateway status:" in caplog.text
        assert "Read timed out" in caplog.text


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
        elb.describe_tags.return_value = {
            "TagDescriptions": [
                _make_tags(
                    PLATFORM_ALB_ARN,
                    {
                        "gco.aws/gateway": "gco-system/gco-gateway",
                        "elbv2.k8s.aws/cluster": "test-cluster",
                    },
                )
            ]
        }

        assert handler.find_alb_by_gateway_hostname(elb, PLATFORM_ALB_DNS, "test-cluster") == (
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

        assert handler.find_alb_by_gateway_hostname(elb, "other.example.com", "test-cluster") == (
            None,
            None,
            None,
        )

    def test_rejects_hostname_match_without_exact_ownership_tags(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [_make_alb(PLATFORM_ALB_ARN, "gateway", PLATFORM_ALB_DNS)]
        }
        elb.describe_tags.return_value = {
            "TagDescriptions": [
                _make_tags(
                    PLATFORM_ALB_ARN,
                    {"elbv2.k8s.aws/cluster": "other-cluster"},
                )
            ]
        }

        assert handler.find_alb_by_gateway_hostname(elb, PLATFORM_ALB_DNS, "test-cluster") == (
            None,
            None,
            None,
        )

    def test_follows_pagination_before_validating_exact_tags(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.side_effect = [
            {"LoadBalancers": [], "NextMarker": "page-2"},
            {"LoadBalancers": [_make_alb(PLATFORM_ALB_ARN, "gateway", PLATFORM_ALB_DNS)]},
        ]
        elb.describe_tags.return_value = {
            "TagDescriptions": [
                _make_tags(
                    PLATFORM_ALB_ARN,
                    {
                        "gco.aws/gateway": "gco-system/gco-gateway",
                        "elbv2.k8s.aws/cluster": "test-cluster",
                    },
                )
            ]
        }

        assert handler.find_alb_by_gateway_hostname(elb, PLATFORM_ALB_DNS, "test-cluster") == (
            PLATFORM_ALB_DNS,
            PLATFORM_ALB_ARN,
            "active",
        )
        assert elb.describe_load_balancers.call_args_list == [
            call(),
            call(Marker="page-2"),
        ]

    def test_api_failures_are_logged_and_left_to_the_polling_caller(self, ga_module, caplog):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.side_effect = _client_error(
            "Throttling", "DescribeLoadBalancers"
        )

        assert handler.find_alb_by_gateway_hostname(elb, PLATFORM_ALB_DNS, "test-cluster") == (
            None,
            None,
            None,
        )
        assert "Error finding Gateway ALB by hostname:" in caplog.text
        assert "Throttling" in caplog.text


class TestElbTagLookup:
    def test_batches_arns_by_twenty_and_ignores_unattributed_descriptions(self, ga_module):
        handler, _, _ = ga_module
        arns = [f"{PLATFORM_ALB_ARN}{index:02d}" for index in range(21)]
        elb = MagicMock()
        elb.describe_tags.side_effect = [
            {
                "TagDescriptions": [
                    _make_tags(arns[0], {"gco.aws/gateway": "gco-system/gco-gateway"}),
                    # No ResourceArn: nothing to attribute these tags to.
                    {"Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "test-cluster"}]},
                    # A tag missing its Value is dropped, not turned into "None".
                    {"ResourceArn": arns[1], "Tags": [{"Key": "orphan"}]},
                ]
            },
            {"TagDescriptions": [_make_tags(arns[20], {"elbv2.k8s.aws/cluster": "test-cluster"})]},
        ]

        tags_by_arn = handler._describe_tags(elb, arns)

        assert elb.describe_tags.call_args_list == [
            call(ResourceArns=arns[:20]),
            call(ResourceArns=arns[20:]),
        ]
        assert tags_by_arn == {
            arns[0]: {"gco.aws/gateway": "gco-system/gco-gateway"},
            arns[1]: {},
            arns[20]: {"elbv2.k8s.aws/cluster": "test-cluster"},
        }


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

    def test_repeated_pagination_marker_is_rejected_instead_of_looping(self, ga_module, caplog):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.side_effect = [
            {
                "LoadBalancers": [_make_alb(OTHER_ALB_ARN, "other", "other.elb.amazonaws.com")],
                "NextMarker": "page-2",
            },
            {"LoadBalancers": [], "NextMarker": "page-2"},
        ]

        assert handler.find_platform_alb_by_tags(elb, "test-cluster") == (None, None, None)
        assert elb.describe_load_balancers.call_count == 2
        elb.describe_tags.assert_not_called()
        assert "Error finding Gateway ALB by tags:" in caplog.text
        assert "ELB pagination repeated marker 'page-2'" in caplog.text


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

    def test_keeps_waiting_when_status_hostname_has_no_owned_alb_yet(self, ga_module, caplog):
        """A Gateway address without a matching owned ALB never falls back to tags."""
        handler, _, _ = ga_module
        caplog.set_level(logging.INFO)
        with (
            patch.object(handler, "find_gateway_address", return_value=PLATFORM_ALB_DNS),
            patch.object(
                handler,
                "find_alb_by_gateway_hostname",
                return_value=(None, None, None),
            ),
            patch.object(handler, "find_platform_alb_by_tags") as fallback,
        ):
            result = handler.find_active_alb(
                MagicMock(), MagicMock(), "https://k8s", {}, "test-cluster"
            )

        assert result == (None, None)
        fallback.assert_not_called()
        assert "waiting for 'active'" not in caplog.text

    def test_returns_nothing_while_neither_gateway_nor_alb_exists_yet(self, ga_module, caplog):
        """Before the Gateway controller has done anything, discovery finds nothing."""
        handler, _, _ = ga_module
        caplog.set_level(logging.INFO)
        http = MagicMock()
        http.request.return_value = _response(404, {"kind": "Status", "code": 404})
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {"LoadBalancers": []}

        result = handler.find_active_alb(elb, http, "https://k8s", {}, "test-cluster")

        assert result == (None, None)
        elb.describe_tags.assert_not_called()
        assert "waiting for 'active'" not in caplog.text

    def test_tag_fallback_waits_for_the_active_state(self, ga_module, caplog):
        handler, _, _ = ga_module
        caplog.set_level(logging.INFO)
        with (
            patch.object(handler, "find_gateway_address", return_value=None),
            patch.object(
                handler,
                "find_platform_alb_by_tags",
                return_value=(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN, "provisioning"),
            ),
        ):
            result = handler.find_active_alb(
                MagicMock(), MagicMock(), "https://k8s", {}, "test-cluster"
            )

        assert result == (None, None)
        assert (
            "Gateway ALB found by tags but state is 'provisioning'; waiting for 'active'"
            in caplog.text
        )


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
                "HealthCheckIntervalSeconds": 30,
                "ThresholdCount": 3,
            }
        }

        handler.ensure_https_health_check(ga, ENDPOINT_GROUP_ARN)

        ga.update_endpoint_group.assert_not_called()

    def test_reconciles_drifted_interval_and_threshold(self, ga_module):
        """A group matching on protocol/port/path but not interval is updated.

        The historical behavior early-returned on protocol/port/path alone,
        which both left a drifted interval unreconciled and silently reset a
        configured non-default interval back to 30 during path repairs.
        """
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "HealthCheckProtocol": "HTTPS",
                "HealthCheckPort": 443,
                "HealthCheckPath": "/api/v1/health",
                "HealthCheckIntervalSeconds": 30,
                "ThresholdCount": 3,
                "EndpointDescriptions": [{"EndpointId": PLATFORM_ALB_ARN, "Weight": 100}],
            }
        }

        handler.ensure_https_health_check(
            ga,
            ENDPOINT_GROUP_ARN,
            expected_alb_arn=PLATFORM_ALB_ARN,
            health_check_interval=10,
            health_check_threshold=2,
        )

        ga.update_endpoint_group.assert_called_once_with(
            EndpointGroupArn=ENDPOINT_GROUP_ARN,
            HealthCheckPort=443,
            HealthCheckProtocol="HTTPS",
            HealthCheckPath="/api/v1/health",
            HealthCheckIntervalSeconds=10,
            ThresholdCount=2,
            EndpointConfigurations=[
                {
                    "EndpointId": PLATFORM_ALB_ARN,
                    "Weight": 100,
                    "ClientIPPreservationEnabled": True,
                }
            ],
        )

    def test_configured_contract_matching_group_is_left_alone(self, ga_module):
        """A group already matching a non-default contract is not rewritten."""
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "HealthCheckProtocol": "HTTPS",
                "HealthCheckPort": 443,
                "HealthCheckPath": "/custom/health",
                "HealthCheckIntervalSeconds": 10,
                "ThresholdCount": 2,
            }
        }

        handler.ensure_https_health_check(
            ga,
            ENDPOINT_GROUP_ARN,
            health_check_path="/custom/health",
            health_check_interval=10,
            health_check_threshold=2,
        )

        ga.update_endpoint_group.assert_not_called()

    def test_health_check_contract_defaults_match_legacy_hardcoding(self, ga_module):
        """Payloads without GaHealthCheck* keys resolve to the legacy values."""
        handler, _, _ = ga_module
        assert handler._health_check_contract({}) == {
            "health_check_path": "/api/v1/health",
            "health_check_interval": 30,
            "health_check_threshold": 3,
        }
        assert handler._health_check_contract(
            {
                "GaHealthCheckPath": "/custom",
                "GaHealthCheckInterval": 10,
                "GaHealthCheckThreshold": "2",
            }
        ) == {
            "health_check_path": "/custom",
            "health_check_interval": 10,
            "health_check_threshold": 2,
        }

    def test_handle_task_threads_health_check_contract(self, ga_module):
        """The Step Functions payload's GaHealthCheck* keys reach registration."""
        handler, _, _ = ga_module
        event = {
            "Action": "publish_gateway_endpoint",
            "ClusterName": "test-cluster",
            "Region": "us-east-1",
            "RegistryRegion": "eu-west-1",
            "ProjectName": "gco",
            "EndpointGroupArn": ENDPOINT_GROUP_ARN,
            "GaHealthCheckPath": "/custom/health",
            "GaHealthCheckInterval": 10,
            "GaHealthCheckThreshold": 2,
        }
        with patch.object(handler, "register_ga_endpoint", return_value={"ok": "yes"}) as task:
            assert handler.handle_task(event) == {"ok": "yes"}

        task.assert_called_once_with(
            cluster_name="test-cluster",
            region="us-east-1",
            endpoint_group_arn=ENDPOINT_GROUP_ARN,
            registry_region="eu-west-1",
            project_name="gco",
            health_check_path="/custom/health",
            health_check_interval=10,
            health_check_threshold=2,
        )

    def test_https_enforcement_failure_is_not_treated_as_success(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {"EndpointGroup": {"HealthCheckProtocol": "TCP"}}
        ga.update_endpoint_group.side_effect = _client_error(
            "AccessDeniedException", "UpdateEndpointGroup"
        )

        with pytest.raises(ClientError):
            handler.ensure_https_health_check(ga, ENDPOINT_GROUP_ARN)

    def test_registers_unregistered_alb_with_client_ip_preservation(self, ga_module, caplog):
        handler, _, _ = ga_module
        caplog.set_level(logging.INFO)
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [_make_ga_endpoint(STALE_ALB_ARN, healthy=False)]
            }
        }

        handler.register_alb_with_ga(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        ga.add_endpoints.assert_called_once_with(
            EndpointGroupArn=ENDPOINT_GROUP_ARN,
            EndpointConfigurations=[
                {
                    "EndpointId": PLATFORM_ALB_ARN,
                    "Weight": 100,
                    "ClientIPPreservationEnabled": True,
                }
            ],
        )
        assert f"Registered Gateway ALB {PLATFORM_ALB_ARN} with Global Accelerator" in caplog.text

    def test_registration_proceeds_when_existing_endpoint_lookup_fails(self, ga_module, caplog):
        """AddEndpoints stays authoritative when DescribeEndpointGroup is denied."""
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.side_effect = _client_error(
            "AccessDeniedException", "DescribeEndpointGroup"
        )

        handler.register_alb_with_ga(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        ga.add_endpoints.assert_called_once()
        assert ga.add_endpoints.call_args.kwargs["EndpointConfigurations"][0]["EndpointId"] == (
            PLATFORM_ALB_ARN
        )
        assert "Error checking existing GA endpoints:" in caplog.text

    def test_register_tolerates_a_concurrent_registration(self, ga_module, caplog):
        handler, _, _ = ga_module
        caplog.set_level(logging.INFO)
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {"EndpointGroup": {"EndpointDescriptions": []}}
        ga.add_endpoints.side_effect = _client_error("EndpointAlreadyExists", "AddEndpoints")

        handler.register_alb_with_ga(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        assert "Gateway ALB was already registered with Global Accelerator" in caplog.text

    def test_register_surfaces_other_add_endpoint_failures(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {"EndpointGroup": {"EndpointDescriptions": []}}
        ga.add_endpoints.side_effect = _client_error("LimitExceededException", "AddEndpoints")

        with pytest.raises(ClientError, match="LimitExceededException"):
            handler.register_alb_with_ga(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

    def test_scrub_tolerates_stale_endpoints_that_already_vanished(self, ga_module, caplog):
        handler, _, _ = ga_module
        caplog.set_level(logging.INFO)
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [
                    _make_ga_endpoint(PLATFORM_ALB_ARN),
                    _make_ga_endpoint(STALE_ALB_ARN, healthy=False),
                    _make_ga_endpoint(OTHER_ALB_ARN, healthy=False),
                ]
            }
        }
        ga.remove_endpoints.side_effect = [
            _client_error("EndpointNotFoundException", "RemoveEndpoints"),
            {},
        ]

        handler.scrub_stale_ga_endpoints(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        assert [
            request.kwargs["EndpointIdentifiers"][0]["EndpointId"]
            for request in ga.remove_endpoints.call_args_list
        ] == [STALE_ALB_ARN, OTHER_ALB_ARN]
        assert f"GA endpoint {STALE_ALB_ARN} was already absent" in caplog.text


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
            health_check_path="/api/v1/health",
            expected_alb_arn=PLATFORM_ALB_ARN,
            health_check_interval=30,
            health_check_threshold=3,
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

    def test_polls_every_five_seconds_and_logs_progress_every_thirty(self, ga_module, caplog):
        handler, mock_boto_client, _ = ga_module
        mock_boto_client.return_value = MagicMock()
        caplog.set_level(logging.INFO)
        clock = _FakeClock()
        # Seven misses span 30 fake seconds, which triggers exactly one
        # progress log before the eighth poll finds the active ALB.
        discoveries = [(None, None)] * 7 + [(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN)]
        with (
            patch.object(
                handler,
                "get_k8s_client",
                return_value=("https://k8s", "token", "/tmp/gco-ca.crt"),
            ),
            patch.object(handler, "find_active_alb", side_effect=discoveries) as find_alb,
            patch.object(handler, "store_alb_hostname_in_ssm") as publish,
            patch.object(handler, "_remove_temporary_ca_file"),
            patch.object(handler, "time", clock),
        ):
            result = handler.register_ga_endpoint("test-cluster", "us-east-1")

        assert result == {"AlbArn": PLATFORM_ALB_ARN, "AlbHostname": PLATFORM_ALB_DNS}
        assert find_alb.call_count == 8
        assert clock.sleeps == [handler.ALB_POLL_INTERVAL] * 7
        assert caplog.text.count("Still waiting for Gateway ALB") == 1
        assert "Still waiting for Gateway ALB (30s elapsed)" in caplog.text
        publish.assert_called_once_with("us-east-1", PLATFORM_ALB_DNS, "us-east-2", "gco")


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

    def test_temporary_ca_removal_ignores_an_absent_path(self, ga_module):
        handler, _, _ = ga_module
        with patch.object(handler.os, "unlink") as unlink:
            handler._remove_temporary_ca_file(None)
            handler._remove_temporary_ca_file("")

        unlink.assert_not_called()

    def test_temporary_ca_removal_tolerates_an_already_deleted_file(
        self, ga_module, tmp_path, caplog
    ):
        handler, _, _ = ga_module

        handler._remove_temporary_ca_file(str(tmp_path / "already-gone.crt"))

        assert "Failed to remove temporary Kubernetes CA file" not in caplog.text

    def test_temporary_ca_removal_warns_on_other_os_errors(self, ga_module, caplog):
        handler, _, _ = ga_module
        with patch.object(
            handler.os,
            "unlink",
            side_effect=PermissionError("Operation not permitted"),
        ):
            handler._remove_temporary_ca_file("/tmp/ca.crt")

        assert (
            "Failed to remove temporary Kubernetes CA file: Operation not permitted" in caplog.text
        )

    @pytest.mark.parametrize("strict", [False, True])
    def test_registry_cleanup_tolerates_an_absent_parameter(self, ga_module, caplog, strict):
        handler, mock_boto_client, _ = ga_module
        caplog.set_level(logging.INFO)
        ssm = MagicMock()
        ssm.delete_parameter.side_effect = _client_error("ParameterNotFound", "DeleteParameter")
        mock_boto_client.return_value = ssm

        handler.delete_alb_hostname_from_ssm("us-east-1", "eu-west-1", "project", strict=strict)

        mock_boto_client.assert_called_once_with("ssm", region_name="eu-west-1")
        ssm.delete_parameter.assert_called_once_with(Name="/project/alb-hostname-us-east-1")
        assert "SSM parameter /project/alb-hostname-us-east-1 was already absent" in caplog.text

    def test_strict_registry_cleanup_raises_other_failures(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        ssm = MagicMock()
        ssm.delete_parameter.side_effect = _client_error("AccessDeniedException", "DeleteParameter")
        mock_boto_client.return_value = ssm

        with pytest.raises(ClientError, match="AccessDeniedException"):
            handler.delete_alb_hostname_from_ssm("us-east-1", "eu-west-1", "project", strict=True)

    def test_lenient_registry_cleanup_only_warns_on_other_failures(self, ga_module, caplog):
        handler, mock_boto_client, _ = ga_module
        ssm = MagicMock()
        ssm.delete_parameter.side_effect = _client_error("AccessDeniedException", "DeleteParameter")
        mock_boto_client.return_value = ssm

        handler.delete_alb_hostname_from_ssm("us-east-1", "eu-west-1", "project")

        assert "Failed to delete Gateway ALB hostname from SSM:" in caplog.text
        assert "AccessDeniedException" in caplog.text


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

    def test_wait_treats_a_missing_accelerator_as_released(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_accelerator.side_effect = _client_error(
            "AcceleratorNotFoundException", "DescribeAccelerator"
        )

        assert handler.wait_for_accelerator_deployed(ga, ENDPOINT_GROUP_ARN, strict=True) is True

    def test_wait_polls_until_deployed(self, ga_module, caplog):
        handler, _, _ = ga_module
        caplog.set_level(logging.INFO)
        clock = _FakeClock()
        ga = MagicMock()
        ga.describe_accelerator.side_effect = [
            {"Accelerator": {"AcceleratorArn": ACCELERATOR_ARN, "Status": "IN_PROGRESS"}},
            {"Accelerator": {"AcceleratorArn": ACCELERATOR_ARN, "Status": "IN_PROGRESS"}},
            {"Accelerator": {"AcceleratorArn": ACCELERATOR_ARN, "Status": "DEPLOYED"}},
        ]

        with patch.object(handler, "time", clock):
            assert handler.wait_for_accelerator_deployed(ga, ENDPOINT_GROUP_ARN) is True

        assert ga.describe_accelerator.call_count == 3
        assert clock.sleeps == [handler.GA_DEPLOYED_POLL_INTERVAL] * 2
        assert caplog.text.count("Accelerator status='IN_PROGRESS'; waiting for DEPLOYED") == 2

    def test_wait_times_out_when_accelerator_never_deploys(self, ga_module, caplog):
        """Strict mode reports the timeout honestly; the caller raises."""
        handler, _, _ = ga_module
        clock = _FakeClock()
        ga = MagicMock()
        ga.describe_accelerator.return_value = {"Accelerator": {"Status": "IN_PROGRESS"}}

        with patch.object(handler, "time", clock):
            deployed = handler.wait_for_accelerator_deployed(
                ga, ENDPOINT_GROUP_ARN, timeout_seconds=40, strict=True
            )

        assert deployed is False
        # Polls at t=0, 15 and 30; the 45s mark is past the 40s budget.
        assert clock.sleeps == [handler.GA_DEPLOYED_POLL_INTERVAL] * 3
        assert "Timed out waiting for Global Accelerator to reach DEPLOYED" in caplog.text

    def test_lenient_endpoint_cleanup_attempts_every_endpoint(self, ga_module, caplog):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [
                    _make_ga_endpoint(PLATFORM_ALB_ARN),
                    # An endpoint description without an EndpointId is skipped.
                    {"Weight": 100, "HealthState": "INITIAL"},
                    _make_ga_endpoint(STALE_ALB_ARN, healthy=False),
                    _make_ga_endpoint(OTHER_ALB_ARN, healthy=False),
                ]
            }
        }
        ga.remove_endpoints.side_effect = [
            {},
            _client_error("EndpointNotFoundException", "RemoveEndpoints"),
            _client_error("AccessDeniedException", "RemoveEndpoints"),
        ]

        handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN)

        assert [
            request.kwargs["EndpointIdentifiers"][0]["EndpointId"]
            for request in ga.remove_endpoints.call_args_list
        ] == [PLATFORM_ALB_ARN, STALE_ALB_ARN, OTHER_ALB_ARN]
        assert f"Failed to remove GA endpoint {OTHER_ALB_ARN}:" in caplog.text

    def test_strict_endpoint_cleanup_raises_the_first_removal_failure(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [
                    _make_ga_endpoint(PLATFORM_ALB_ARN),
                    _make_ga_endpoint(STALE_ALB_ARN, healthy=False),
                ]
            }
        }
        ga.remove_endpoints.side_effect = _client_error("AccessDeniedException", "RemoveEndpoints")

        with pytest.raises(ClientError, match="AccessDeniedException"):
            handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN, strict=True)

        ga.remove_endpoints.assert_called_once()

    def test_lenient_endpoint_cleanup_accepts_an_absent_accelerator(self, ga_module, caplog):
        handler, _, _ = ga_module
        caplog.set_level(logging.INFO)
        ga = MagicMock()
        ga.describe_endpoint_group.side_effect = _client_error(
            "AcceleratorNotFoundException", "DescribeEndpointGroup"
        )

        handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN)

        ga.remove_endpoints.assert_not_called()
        assert "Global Accelerator endpoint group was already absent" in caplog.text

    def test_lenient_endpoint_cleanup_warns_when_group_lookup_is_denied(self, ga_module, caplog):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.side_effect = _client_error(
            "AccessDeniedException", "DescribeEndpointGroup"
        )

        handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN)

        ga.remove_endpoints.assert_not_called()
        assert "Failed to clean up GA endpoints:" in caplog.text
        assert "AccessDeniedException" in caplog.text

    @pytest.mark.parametrize("strict", [False, True])
    def test_endpoint_cleanup_transport_failures_follow_strictness(self, ga_module, caplog, strict):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.side_effect = EndpointConnectionError(
            endpoint_url="https://globalaccelerator.us-west-2.amazonaws.com"
        )

        if strict:
            with pytest.raises(EndpointConnectionError):
                handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN, strict=True)
            assert "Failed to clean up GA endpoints" not in caplog.text
        else:
            handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN)
            assert "Failed to clean up GA endpoints:" in caplog.text
            assert "Could not connect to the endpoint URL" in caplog.text

    def test_raw_delete_responds_success_when_registry_cleanup_fails(self, ga_module, caplog):
        handler, mock_boto_client, _ = ga_module
        ssm = MagicMock()
        ssm.delete_parameter.side_effect = EndpointConnectionError(
            endpoint_url="https://ssm.eu-west-1.amazonaws.com"
        )
        mock_boto_client.return_value = ssm
        event = _make_cfn_event("Delete", endpoint_group=False)
        with patch.object(handler, "send_response") as send_response:
            handler.handle_delete(
                event,
                _context(),
                event["ResourceProperties"],
                "physical-id",
            )

        mock_boto_client.assert_called_once_with("ssm", region_name="eu-west-1")
        ssm.delete_parameter.assert_called_once_with(Name="/gco/alb-hostname-us-east-1")
        send_response.assert_called_once_with(event, ANY, "SUCCESS", {}, "physical-id")
        assert "SSM registry cleanup failed during Delete:" in caplog.text

    def test_provider_delete_deregisters_ga_then_removes_registry_parameter(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {"EndpointDescriptions": [_make_ga_endpoint(PLATFORM_ALB_ARN)]}
        }
        ga.describe_accelerator.return_value = {"Accelerator": {"Status": "DEPLOYED"}}
        ssm = MagicMock()
        mock_boto_client.side_effect = lambda service, **_kwargs: (
            ga if service == "globalaccelerator" else ssm
        )
        event = {
            "RequestType": "Delete",
            "PhysicalResourceId": "stable-id",
            "ResourceProperties": {
                "Region": "us-east-1",
                "RegistryRegion": "eu-west-1",
                "ProjectName": "project",
                "EndpointGroupArn": ENDPOINT_GROUP_ARN,
            },
        }

        result = handler.on_delete_event(event, _context())

        assert result == {"PhysicalResourceId": "stable-id"}
        assert mock_boto_client.call_args_list == [
            call("globalaccelerator", region_name="us-west-2"),
            call("ssm", region_name="eu-west-1"),
        ]
        ga.remove_endpoints.assert_called_once_with(
            EndpointGroupArn=ENDPOINT_GROUP_ARN,
            EndpointIdentifiers=[{"EndpointId": PLATFORM_ALB_ARN}],
        )
        ga.describe_accelerator.assert_called_once_with(AcceleratorArn=ACCELERATOR_ARN)
        ssm.delete_parameter.assert_called_once_with(Name="/project/alb-hostname-us-east-1")

    def test_provider_delete_guards_never_wedge_the_stack(self, ga_module, caplog):
        """Transport failures in either guard are logged; the stack still deletes."""
        handler, mock_boto_client, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {"EndpointGroup": {"EndpointDescriptions": []}}
        ga.describe_accelerator.side_effect = EndpointConnectionError(
            endpoint_url="https://globalaccelerator.us-west-2.amazonaws.com"
        )
        ssm = MagicMock()
        ssm.delete_parameter.side_effect = EndpointConnectionError(
            endpoint_url="https://ssm.us-east-2.amazonaws.com"
        )
        mock_boto_client.side_effect = lambda service, **_kwargs: (
            ga if service == "globalaccelerator" else ssm
        )
        event = {
            "RequestType": "Delete",
            "ResourceProperties": {"Region": "us-east-1", "EndpointGroupArn": ENDPOINT_GROUP_ARN},
        }

        result = handler.on_delete_event(event)

        assert result == {"PhysicalResourceId": "ga-dereg-us-east-1"}
        ssm.delete_parameter.assert_called_once_with(Name="/gco/alb-hostname-us-east-1")
        assert "GA deregistration guard failed:" in caplog.text
        assert "SSM registry cleanup guard failed:" in caplog.text

    def test_provider_delete_without_region_only_warns(self, ga_module, caplog):
        handler, mock_boto_client, _ = ga_module

        result = handler.on_delete_event({"RequestType": "Delete"})

        mock_boto_client.assert_not_called()
        assert result == {"PhysicalResourceId": "ga-dereg-unknown"}
        assert "No Region supplied; cannot identify the SSM registry parameter" in caplog.text


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
            health_check_path="/api/v1/health",
            health_check_interval=30,
            health_check_threshold=3,
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

    def test_step_functions_cleanup_without_ga_only_fences_the_registry(self, ga_module):
        """A blank EndpointGroupArn and the legacy GlobalRegion alias are honoured."""
        handler, mock_boto_client, _ = ga_module
        ssm = MagicMock()
        mock_boto_client.return_value = ssm
        event = {
            "Action": "cleanup_gateway_endpoint",
            "Region": "us-east-1",
            "GlobalRegion": "eu-west-1",
            "EndpointGroupArn": "   ",
        }

        result = handler.handle_task(event)

        mock_boto_client.assert_called_once_with("ssm", region_name="eu-west-1")
        ssm.delete_parameter.assert_called_once_with(Name="/gco/alb-hostname-us-east-1")
        assert result == {
            "RegistryParameterDeleted": True,
            "GlobalAcceleratorDeregistered": False,
        }

    def test_cloudformation_create_reports_registration_data(self, ga_module):
        handler, _, mock_pool = ga_module
        event = _make_cfn_event("Create")
        data = {"AlbArn": PLATFORM_ALB_ARN, "AlbHostname": PLATFORM_ALB_DNS}
        with patch.object(handler, "register_ga_endpoint", return_value=data) as register:
            handler.lambda_handler(event, _context())

        register.assert_called_once_with(
            cluster_name="test-cluster",
            region="us-east-1",
            endpoint_group_arn=ENDPOINT_GROUP_ARN,
            registry_region="eu-west-1",
            project_name="gco",
            health_check_path="/api/v1/health",
            health_check_interval=30,
            health_check_threshold=3,
        )
        request = mock_pool.return_value.request.call_args
        assert request.args == ("PUT", event["ResponseURL"])
        assert request.kwargs["headers"] == {"Content-Type": "application/json"}
        assert json.loads(request.kwargs["body"]) == {
            "Status": "SUCCESS",
            "Reason": "See CloudWatch Log Stream: test-log-stream",
            "PhysicalResourceId": "ga-reg-test-cluster",
            "StackId": event["StackId"],
            "RequestId": "req-123",
            "LogicalResourceId": "GaRegistration",
            "Data": data,
        }

    def test_cloudformation_update_failure_reports_failed_with_the_reason(self, ga_module):
        handler, _, mock_pool = ga_module
        event = _make_cfn_event("Update")
        event["PhysicalResourceId"] = "ga-reg-test-cluster"
        failure = TimeoutError(
            "Timed out waiting for active gco-system/gco-gateway ALB after 840 seconds"
        )
        with patch.object(handler, "register_ga_endpoint", side_effect=failure):
            handler.lambda_handler(event, _context())

        response_body = json.loads(mock_pool.return_value.request.call_args.kwargs["body"])
        assert response_body["Status"] == "FAILED"
        assert response_body["Reason"] == str(failure)
        assert response_body["PhysicalResourceId"] == "ga-reg-test-cluster"
        assert response_body["Data"] == {}

    def test_callback_delivery_failure_is_logged_not_raised(self, ga_module, caplog):
        handler, _, mock_pool = ga_module
        mock_pool.return_value.request.side_effect = handler.urllib3.exceptions.HTTPError(
            "callback unreachable"
        )
        event = _make_cfn_event("Create")

        handler.send_response(event, _context(), "SUCCESS", {}, "ga-reg-test-cluster")

        mock_pool.return_value.request.assert_called_once()
        assert "Failed to send CloudFormation response: callback unreachable" in caplog.text
