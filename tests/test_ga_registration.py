"""
Tests for the Global Accelerator registration Lambda
(lambda/ga-registration/handler.py).

Exercises the full CloudFormation custom-resource lifecycle
(Create/Update/Delete) that registers per-region platform ALBs with
the shared Global Accelerator endpoint group. Covers ALB discovery
via Kubernetes Ingress status and tag-based fallback, filtering of
inference ALBs and Slurm NLBs, stale endpoint scrubbing, HTTP health
check enforcement, and SSM parameter storage of the chosen ALB
hostname. The ga_module fixture pops and reloads the handler module
under patched boto3 + urllib3 so each test starts from a clean slate.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from tests._lambda_imports import load_lambda_module

# ============================================================================
# Fixture
# ============================================================================


@pytest.fixture
def ga_module():
    """Import the ga-registration handler with mocked boto3 and urllib3.

    Loaded via :func:`load_lambda_module` — see
    ``tests/_lambda_imports.py`` for why we don't use the
    ``sys.path.insert + import handler`` pattern.
    """
    with (
        patch("boto3.client") as mock_boto_client,
        patch("boto3.Session"),
        patch("urllib3.PoolManager") as mock_pool,
        patch.dict(
            "os.environ",
            {
                "ClusterName": "test-cluster",
                "Region": "us-east-1",
                "EndpointGroupArn": "arn:aws:globalaccelerator::123:accelerator/abc/listener/def/endpoint-group/ghi",
                "IngressName": "gco-ingress",
                "Namespace": "gco-system",
                "GlobalRegion": "us-east-2",
                "ProjectName": "gco",
            },
        ),
    ):
        handler = load_lambda_module("ga-registration")
        yield handler, mock_boto_client, mock_pool


# ============================================================================
# Helpers
# ============================================================================

PLATFORM_ALB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/k8s-gco/abc"
PLATFORM_ALB_DNS = "k8s-gco-abc.us-east-1.elb.amazonaws.com"
INFERENCE_ALB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/k8s-gcoinfer/xyz"
SLURM_NLB_ARN = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/net/k8s-gcojobs/nlb1"
ENDPOINT_GROUP_ARN = (
    "arn:aws:globalaccelerator::123:accelerator/abc/listener/def/endpoint-group/ghi"
)


def _make_cfn_event(request_type="Create"):
    return {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-response.example.com/callback",
        "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
        "RequestId": "req-123",
        "LogicalResourceId": "GaRegistration",
        "ResourceProperties": {
            "ClusterName": "test-cluster",
            "Region": "us-east-1",
            "EndpointGroupArn": ENDPOINT_GROUP_ARN,
            "IngressName": "gco-ingress",
            "Namespace": "gco-system",
            "GlobalRegion": "us-east-2",
            "ProjectName": "gco",
        },
    }


def _make_context():
    ctx = MagicMock()
    ctx.log_stream_name = "test-log-stream"
    return ctx


def _make_alb(arn, name, dns, state="active", lb_type="application"):
    return {
        "LoadBalancerArn": arn,
        "LoadBalancerName": name,
        "DNSName": dns,
        "State": {"Code": state},
        "Type": lb_type,
    }


def _make_tags(arn, tags_dict):
    return {
        "ResourceArn": arn,
        "Tags": [{"Key": k, "Value": v} for k, v in tags_dict.items()],
    }


# ============================================================================
# find_alb_by_ingress_hostname Tests
# ============================================================================


class TestFindAlbByIngressHostname:
    """Tests for the most deterministic detection method — hostname lookup."""

    def test_returns_alb_when_hostname_matches(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(PLATFORM_ALB_ARN, "k8s-gco", PLATFORM_ALB_DNS),
            ]
        }

        dns, arn, state = handler.find_alb_by_ingress_hostname(elb, PLATFORM_ALB_DNS)

        assert dns == PLATFORM_ALB_DNS
        assert arn == PLATFORM_ALB_ARN
        assert state == "active"

    def test_returns_none_when_hostname_not_found(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(PLATFORM_ALB_ARN, "k8s-gco", PLATFORM_ALB_DNS),
            ]
        }

        dns, arn, state = handler.find_alb_by_ingress_hostname(elb, "nonexistent.example.com")

        assert dns is None
        assert arn is None
        assert state is None

    def test_skips_nlbs_even_if_hostname_matches(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(
                    SLURM_NLB_ARN,
                    "k8s-gcojobs",
                    "k8s-gcojobs.elb.amazonaws.com",
                    lb_type="network",
                ),
            ]
        }

        dns, arn, state = handler.find_alb_by_ingress_hostname(elb, "k8s-gcojobs.elb.amazonaws.com")

        assert dns is None

    def test_returns_provisioning_state(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(PLATFORM_ALB_ARN, "k8s-gco", PLATFORM_ALB_DNS, state="provisioning"),
            ]
        }

        dns, arn, state = handler.find_alb_by_ingress_hostname(elb, PLATFORM_ALB_DNS)

        assert arn == PLATFORM_ALB_ARN
        assert state == "provisioning"

    def test_handles_api_error_gracefully(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.side_effect = Exception("API error")

        dns, arn, state = handler.find_alb_by_ingress_hostname(elb, PLATFORM_ALB_DNS)

        assert dns is None


# ============================================================================
# find_platform_alb_by_tags Tests
# ============================================================================


class TestFindPlatformAlbByTags:
    """Tests for the tag-based fallback detection method."""

    def test_matches_eks_auto_mode_tags(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(PLATFORM_ALB_ARN, "k8s-gco", PLATFORM_ALB_DNS),
            ]
        }
        elb.describe_tags.return_value = {
            "TagDescriptions": [
                _make_tags(
                    PLATFORM_ALB_ARN,
                    {
                        "eks:eks-cluster-name": "test-cluster",
                        "ingress.eks.amazonaws.com/stack": "gco",
                    },
                ),
            ]
        }

        dns, arn, state = handler.find_platform_alb_by_tags(elb, "test-cluster")

        assert arn == PLATFORM_ALB_ARN
        assert state == "active"

    def test_matches_standard_controller_tags(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(PLATFORM_ALB_ARN, "k8s-gco", PLATFORM_ALB_DNS),
            ]
        }
        elb.describe_tags.return_value = {
            "TagDescriptions": [
                _make_tags(
                    PLATFORM_ALB_ARN,
                    {
                        "elbv2.k8s.aws/cluster": "test-cluster",
                        "ingress.k8s.aws/stack": "gco-system/gco-ingress",
                    },
                ),
            ]
        }

        dns, arn, state = handler.find_platform_alb_by_tags(elb, "test-cluster")

        assert arn == PLATFORM_ALB_ARN

    def test_skips_inference_alb(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(INFERENCE_ALB_ARN, "k8s-gcoinfer", "inf.elb.amazonaws.com"),
            ]
        }
        elb.describe_tags.return_value = {
            "TagDescriptions": [
                _make_tags(
                    INFERENCE_ALB_ARN,
                    {
                        "eks:eks-cluster-name": "test-cluster",
                        "ingress.eks.amazonaws.com/stack": "gco-inference",
                    },
                ),
            ]
        }

        dns, arn, state = handler.find_platform_alb_by_tags(elb, "test-cluster")

        assert arn is None

    def test_skips_nlbs(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(
                    SLURM_NLB_ARN,
                    "k8s-gcojobs-slinky",
                    "nlb.elb.amazonaws.com",
                    lb_type="network",
                ),
            ]
        }

        dns, arn, state = handler.find_platform_alb_by_tags(elb, "test-cluster")

        assert arn is None
        # describe_tags should never be called — NLBs are filtered before tag lookup
        elb.describe_tags.assert_not_called()

    def test_skips_alb_without_ingress_stack_tag(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(PLATFORM_ALB_ARN, "k8s-gco", PLATFORM_ALB_DNS),
            ]
        }
        elb.describe_tags.return_value = {
            "TagDescriptions": [
                _make_tags(
                    PLATFORM_ALB_ARN,
                    {
                        "eks:eks-cluster-name": "test-cluster",
                        # No ingress stack tag
                    },
                ),
            ]
        }

        dns, arn, state = handler.find_platform_alb_by_tags(elb, "test-cluster")

        assert arn is None

    def test_skips_alb_from_different_cluster(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(PLATFORM_ALB_ARN, "k8s-gco", PLATFORM_ALB_DNS),
            ]
        }
        elb.describe_tags.return_value = {
            "TagDescriptions": [
                _make_tags(
                    PLATFORM_ALB_ARN,
                    {
                        "eks:eks-cluster-name": "other-cluster",
                        "ingress.eks.amazonaws.com/stack": "gco",
                    },
                ),
            ]
        }

        dns, arn, state = handler.find_platform_alb_by_tags(elb, "test-cluster")

        assert arn is None

    def test_returns_none_when_no_albs_exist(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {"LoadBalancers": []}

        dns, arn, state = handler.find_platform_alb_by_tags(elb, "test-cluster")

        assert arn is None

    def test_picks_platform_alb_when_mixed_with_inference(self, ga_module):
        """When both platform and inference ALBs exist, only return the platform one."""
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [
                _make_alb(INFERENCE_ALB_ARN, "k8s-gcoinfer", "inf.elb.amazonaws.com"),
                _make_alb(PLATFORM_ALB_ARN, "k8s-gco", PLATFORM_ALB_DNS),
            ]
        }
        elb.describe_tags.return_value = {
            "TagDescriptions": [
                _make_tags(
                    INFERENCE_ALB_ARN,
                    {
                        "eks:eks-cluster-name": "test-cluster",
                        "ingress.eks.amazonaws.com/stack": "gco-inference",
                    },
                ),
                _make_tags(
                    PLATFORM_ALB_ARN,
                    {
                        "eks:eks-cluster-name": "test-cluster",
                        "ingress.eks.amazonaws.com/stack": "gco",
                    },
                ),
            ]
        }

        dns, arn, state = handler.find_platform_alb_by_tags(elb, "test-cluster")

        assert arn == PLATFORM_ALB_ARN

    def test_handles_api_error_gracefully(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        elb.describe_load_balancers.side_effect = Exception("API error")

        dns, arn, state = handler.find_platform_alb_by_tags(elb, "test-cluster")

        assert arn is None


# ============================================================================
# find_active_alb Tests
# ============================================================================


class TestFindActiveAlb:
    """Tests for the unified ALB detection orchestrator."""

    def test_prefers_ingress_status_over_tags(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        http = MagicMock()

        with (
            patch.object(
                handler,
                "find_alb_from_ingress_status",
                return_value=PLATFORM_ALB_DNS,
            ),
            patch.object(
                handler,
                "find_alb_by_ingress_hostname",
                return_value=(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN, "active"),
            ),
            patch.object(handler, "find_platform_alb_by_tags") as mock_tags,
        ):
            dns, arn = handler.find_active_alb(
                elb, http, "https://k8s", {}, "cluster", "ns", "ingress"
            )

        assert arn == PLATFORM_ALB_ARN
        mock_tags.assert_not_called()

    def test_falls_back_to_tags_when_ingress_status_empty(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        http = MagicMock()

        with (
            patch.object(handler, "find_alb_from_ingress_status", return_value=None),
            patch.object(
                handler,
                "find_platform_alb_by_tags",
                return_value=(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN, "active"),
            ),
        ):
            dns, arn = handler.find_active_alb(
                elb, http, "https://k8s", {}, "cluster", "ns", "ingress"
            )

        assert arn == PLATFORM_ALB_ARN

    def test_returns_none_when_ingress_alb_not_active(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        http = MagicMock()

        with (
            patch.object(
                handler,
                "find_alb_from_ingress_status",
                return_value=PLATFORM_ALB_DNS,
            ),
            patch.object(
                handler,
                "find_alb_by_ingress_hostname",
                return_value=(PLATFORM_ALB_DNS, PLATFORM_ALB_ARN, "provisioning"),
            ),
        ):
            dns, arn = handler.find_active_alb(
                elb, http, "https://k8s", {}, "cluster", "ns", "ingress"
            )

        assert dns is None
        assert arn is None

    def test_returns_none_when_nothing_found(self, ga_module):
        handler, _, _ = ga_module
        elb = MagicMock()
        http = MagicMock()

        with (
            patch.object(handler, "find_alb_from_ingress_status", return_value=None),
            patch.object(
                handler,
                "find_platform_alb_by_tags",
                return_value=(None, None, None),
            ),
        ):
            dns, arn = handler.find_active_alb(
                elb, http, "https://k8s", {}, "cluster", "ns", "ingress"
            )

        assert dns is None
        assert arn is None


# ============================================================================
# scrub_stale_ga_endpoints Tests
# ============================================================================


class TestScrubStaleGaEndpoints:
    """Tests for the safety net that removes wrong endpoints from GA."""

    def test_removes_stale_endpoints(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [
                    {"EndpointId": PLATFORM_ALB_ARN},
                    {"EndpointId": SLURM_NLB_ARN},
                    {"EndpointId": INFERENCE_ALB_ARN},
                ]
            }
        }

        handler.scrub_stale_ga_endpoints(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        assert ga.remove_endpoints.call_count == 2
        removed_ids = [
            call.kwargs["EndpointIdentifiers"][0]["EndpointId"]
            for call in ga.remove_endpoints.call_args_list
        ]
        assert SLURM_NLB_ARN in removed_ids
        assert INFERENCE_ALB_ARN in removed_ids

    def test_does_nothing_when_only_correct_alb(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [
                    {"EndpointId": PLATFORM_ALB_ARN},
                ]
            }
        }

        handler.scrub_stale_ga_endpoints(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        ga.remove_endpoints.assert_not_called()

    def test_does_nothing_when_no_endpoints(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {"EndpointGroup": {"EndpointDescriptions": []}}

        handler.scrub_stale_ga_endpoints(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        ga.remove_endpoints.assert_not_called()

    def test_handles_endpoint_not_found_gracefully(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [
                    {"EndpointId": PLATFORM_ALB_ARN},
                    {"EndpointId": SLURM_NLB_ARN},
                ]
            }
        }
        ga.remove_endpoints.side_effect = ClientError(
            {"Error": {"Code": "EndpointNotFoundException", "Message": "Not found"}},
            "RemoveEndpoints",
        )

        # Should not raise
        handler.scrub_stale_ga_endpoints(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

    def test_handles_api_error_gracefully(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.side_effect = Exception("API error")

        # Should not raise
        handler.scrub_stale_ga_endpoints(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)


# ============================================================================
# GA Registration Tests
# ============================================================================


class TestCheckExistingGaEndpoint:
    def test_returns_true_when_alb_already_registered(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {"EndpointDescriptions": [{"EndpointId": PLATFORM_ALB_ARN}]}
        }

        assert handler.check_existing_ga_endpoint(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

    def test_returns_false_when_alb_not_registered(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {"EndpointDescriptions": [{"EndpointId": "arn:other/xyz"}]}
        }

        assert not handler.check_existing_ga_endpoint(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)


class TestRegisterAlbWithGa:
    def test_skips_when_already_registered(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {"EndpointDescriptions": [{"EndpointId": PLATFORM_ALB_ARN}]}
        }

        handler.register_alb_with_ga(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)

        ga.add_endpoints.assert_not_called()

    def test_handles_endpoint_already_exists_race(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {"EndpointGroup": {"EndpointDescriptions": []}}
        ga.add_endpoints.side_effect = ClientError(
            {"Error": {"Code": "EndpointAlreadyExists", "Message": "exists"}},
            "AddEndpoints",
        )

        # Should not raise
        handler.register_alb_with_ga(ga, ENDPOINT_GROUP_ARN, PLATFORM_ALB_ARN)


# ============================================================================
# Health Check Configuration Tests
# ============================================================================


class TestEnsureHttpHealthCheck:
    def test_updates_tcp_to_http(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "HealthCheckProtocol": "TCP",
                "HealthCheckPath": "",
                "EndpointDescriptions": [
                    {
                        "EndpointId": PLATFORM_ALB_ARN,
                        "Weight": 100,
                        "ClientIPPreservationEnabled": True,
                    },
                ],
            }
        }

        handler.ensure_http_health_check(ga, ENDPOINT_GROUP_ARN)

        ga.update_endpoint_group.assert_called_once()
        kw = ga.update_endpoint_group.call_args.kwargs
        assert kw["HealthCheckProtocol"] == "HTTP"
        assert kw["HealthCheckPath"] == "/api/v1/health"
        assert kw["HealthCheckPort"] == 80
        assert kw["HealthCheckIntervalSeconds"] == 30
        assert kw["ThresholdCount"] == 3

    def test_skips_when_already_http(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "HealthCheckProtocol": "HTTP",
                "HealthCheckPath": "/api/v1/health",
                "EndpointDescriptions": [],
            }
        }

        handler.ensure_http_health_check(ga, ENDPOINT_GROUP_ARN)

        ga.update_endpoint_group.assert_not_called()

    def test_updates_when_path_differs(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "HealthCheckProtocol": "HTTP",
                "HealthCheckPath": "/healthz",
                "EndpointDescriptions": [],
            }
        }

        handler.ensure_http_health_check(ga, ENDPOINT_GROUP_ARN)

        ga.update_endpoint_group.assert_called_once()

    def test_preserves_existing_endpoints(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "HealthCheckProtocol": "TCP",
                "HealthCheckPath": "",
                "EndpointDescriptions": [
                    {"EndpointId": "arn:alb/1", "Weight": 100, "ClientIPPreservationEnabled": True},
                    {"EndpointId": "arn:alb/2", "Weight": 50, "ClientIPPreservationEnabled": False},
                ],
            }
        }

        handler.ensure_http_health_check(ga, ENDPOINT_GROUP_ARN)

        endpoints = ga.update_endpoint_group.call_args.kwargs["EndpointConfigurations"]
        assert len(endpoints) == 2

    def test_handles_api_error_gracefully(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.side_effect = ClientError(
            {"Error": {"Code": "EndpointGroupNotFoundException", "Message": "nope"}},
            "DescribeEndpointGroup",
        )

        # Should not raise
        handler.ensure_http_health_check(ga, ENDPOINT_GROUP_ARN)


# ============================================================================
# SSM Storage Tests
# ============================================================================


class TestStoreAlbHostnameInSsm:
    def test_stores_parameter_correctly(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        mock_ssm = MagicMock()
        mock_boto_client.return_value = mock_ssm

        handler.store_alb_hostname_in_ssm("us-east-1", PLATFORM_ALB_DNS, "us-east-2", "gco")

        mock_ssm.put_parameter.assert_called_once_with(
            Name="/gco/alb-hostname-us-east-1",
            Value=PLATFORM_ALB_DNS,
            Type="String",
            Overwrite=True,
            Description="ALB hostname for us-east-1 regional cluster",
        )


class TestDeleteAlbHostnameFromSsm:
    def test_handles_parameter_not_found_gracefully(self, ga_module):
        handler, mock_boto_client, _ = ga_module
        mock_ssm = MagicMock()
        mock_ssm.delete_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "nope"}},
            "DeleteParameter",
        )
        mock_boto_client.return_value = mock_ssm

        # Should not raise
        handler.delete_alb_hostname_from_ssm("us-east-1", "us-east-2", "gco")


# ============================================================================
# Lambda Handler Tests
# ============================================================================


class TestLambdaHandler:
    def test_delete_always_succeeds_even_on_error(self, ga_module):
        handler, _, mock_pool = ga_module
        event = _make_cfn_event("Delete")
        context = _make_context()

        with patch.object(handler, "handle_delete", side_effect=Exception("boom")):
            handler.lambda_handler(event, context)

            pool_instance = mock_pool.return_value
            pool_instance.request.assert_called_once()
            call_args = pool_instance.request.call_args
            body = json.loads(call_args[1]["body"] if "body" in call_args[1] else call_args[0][2])
            assert body["Status"] == "SUCCESS"

    def test_calls_handle_create_update_on_create(self, ga_module):
        handler, _, _ = ga_module
        event = _make_cfn_event("Create")
        context = _make_context()

        with patch.object(handler, "handle_create_update") as mock_create:
            handler.lambda_handler(event, context)

            mock_create.assert_called_once_with(
                event,
                context,
                event["ResourceProperties"],
                "ga-reg-test-cluster",
            )

    def test_calls_handle_create_update_on_update(self, ga_module):
        handler, _, _ = ga_module
        event = _make_cfn_event("Update")
        context = _make_context()

        with patch.object(handler, "handle_create_update") as mock_update:
            handler.lambda_handler(event, context)

            mock_update.assert_called_once()


# ============================================================================
# Delete-time GA Deregistration Guard Tests (issue #130)
# ============================================================================

ACCELERATOR_ARN = "arn:aws:globalaccelerator::123:accelerator/abc"


class TestAcceleratorArnFromEndpointGroup:
    """The accelerator ARN is derived from the endpoint-group ARN so the delete
    guard can poll the accelerator's status without a separate lookup."""

    def test_derives_accelerator_arn(self, ga_module):
        handler, _, _ = ga_module
        assert handler._accelerator_arn_from_endpoint_group(ENDPOINT_GROUP_ARN) == ACCELERATOR_ARN


class TestRemoveGaEndpoints:
    """The delete guard must clear every endpoint from the group so Global
    Accelerator releases its managed ENIs."""

    def test_removes_all_endpoints(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {
                "EndpointDescriptions": [
                    {"EndpointId": PLATFORM_ALB_ARN},
                    {"EndpointId": INFERENCE_ALB_ARN},
                ]
            }
        }

        handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN)

        assert ga.remove_endpoints.call_count == 2

    def test_no_endpoints_is_a_noop(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {"EndpointGroup": {"EndpointDescriptions": []}}

        handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN)

        ga.remove_endpoints.assert_not_called()

    def test_endpoint_not_found_is_swallowed(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_endpoint_group.return_value = {
            "EndpointGroup": {"EndpointDescriptions": [{"EndpointId": PLATFORM_ALB_ARN}]}
        }
        ga.remove_endpoints.side_effect = ClientError(
            {"Error": {"Code": "EndpointNotFoundException", "Message": "gone"}},
            "RemoveEndpoints",
        )

        # Should not raise — the endpoint is already gone, which is success.
        handler.remove_ga_endpoints(ga, ENDPOINT_GROUP_ARN)


class TestWaitForAcceleratorDeployed:
    """Global Accelerator only releases its managed ENIs once the accelerator
    redeploys to DEPLOYED; teardown blocks on that so subnet deletion succeeds."""

    def test_returns_true_when_already_deployed(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_accelerator.return_value = {"Accelerator": {"Status": "DEPLOYED"}}

        assert handler.wait_for_accelerator_deployed(ga, ENDPOINT_GROUP_ARN) is True
        ga.describe_accelerator.assert_called_once_with(AcceleratorArn=ACCELERATOR_ARN)

    def test_polls_until_deployed(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_accelerator.side_effect = [
            {"Accelerator": {"Status": "IN_PROGRESS"}},
            {"Accelerator": {"Status": "DEPLOYED"}},
        ]

        with patch.object(handler.time, "sleep"):
            assert handler.wait_for_accelerator_deployed(ga, ENDPOINT_GROUP_ARN) is True

        assert ga.describe_accelerator.call_count == 2

    def test_returns_true_when_accelerator_gone(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_accelerator.side_effect = ClientError(
            {"Error": {"Code": "AcceleratorNotFoundException", "Message": "gone"}},
            "DescribeAccelerator",
        )

        # A missing accelerator means there are no ENIs left to release.
        assert handler.wait_for_accelerator_deployed(ga, ENDPOINT_GROUP_ARN) is True

    def test_returns_false_on_other_client_error(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_accelerator.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            "DescribeAccelerator",
        )

        assert handler.wait_for_accelerator_deployed(ga, ENDPOINT_GROUP_ARN) is False

    def test_returns_false_on_timeout(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()
        ga.describe_accelerator.return_value = {"Accelerator": {"Status": "IN_PROGRESS"}}

        # A zero budget exits before the first poll — exercises the timeout path
        # without sleeping.
        assert handler.wait_for_accelerator_deployed(ga, ENDPOINT_GROUP_ARN, timeout_seconds=0) is (
            False
        )


class TestDeregisterAlbFromGa:
    """The deregistration helper removes endpoints and then waits for the ENIs
    to be released, in that order."""

    def test_removes_then_waits(self, ga_module):
        handler, _, _ = ga_module
        ga = MagicMock()

        with (
            patch.object(handler, "remove_ga_endpoints") as mock_remove,
            patch.object(handler, "wait_for_accelerator_deployed") as mock_wait,
        ):
            handler.deregister_alb_from_ga(ga, ENDPOINT_GROUP_ARN)

        mock_remove.assert_called_once_with(ga, ENDPOINT_GROUP_ARN)
        mock_wait.assert_called_once_with(ga, ENDPOINT_GROUP_ARN)


class TestOnDeleteEvent:
    """The cr.Provider entrypoint for the delete-time teardown guard."""

    def _delete_event(self):
        return {
            "RequestType": "Delete",
            "PhysicalResourceId": "ga-dereg-us-east-1",
            "ResourceProperties": {
                "EndpointGroupArn": ENDPOINT_GROUP_ARN,
                "Region": "us-east-1",
                "ProjectName": "gco",
            },
        }

    def test_delete_deregisters_alb(self, ga_module):
        handler, mock_boto_client, _ = ga_module

        with patch.object(handler, "deregister_alb_from_ga") as mock_dereg:
            result = handler.on_delete_event(self._delete_event(), None)

        mock_dereg.assert_called_once()
        # Called with (ga_client, endpoint_group_arn).
        assert mock_dereg.call_args[0][1] == ENDPOINT_GROUP_ARN
        assert result["PhysicalResourceId"] == "ga-dereg-us-east-1"

    def test_create_is_noop(self, ga_module):
        handler, _, _ = ga_module
        event = {
            "RequestType": "Create",
            "ResourceProperties": {"EndpointGroupArn": ENDPOINT_GROUP_ARN, "Region": "us-east-1"},
        }

        with patch.object(handler, "deregister_alb_from_ga") as mock_dereg:
            result = handler.on_delete_event(event, None)

        mock_dereg.assert_not_called()
        assert "PhysicalResourceId" in result

    def test_update_is_noop(self, ga_module):
        handler, _, _ = ga_module
        event = {
            "RequestType": "Update",
            "PhysicalResourceId": "pid",
            "ResourceProperties": {"EndpointGroupArn": ENDPOINT_GROUP_ARN},
        }

        with patch.object(handler, "deregister_alb_from_ga") as mock_dereg:
            result = handler.on_delete_event(event, None)

        mock_dereg.assert_not_called()
        assert result["PhysicalResourceId"] == "pid"

    def test_delete_without_endpoint_group_is_noop(self, ga_module):
        handler, _, _ = ga_module
        event = {"RequestType": "Delete", "ResourceProperties": {"Region": "us-east-1"}}

        with patch.object(handler, "deregister_alb_from_ga") as mock_dereg:
            result = handler.on_delete_event(event, None)

        mock_dereg.assert_not_called()
        assert result["PhysicalResourceId"].startswith("ga-dereg")

    def test_delete_never_raises_on_error(self, ga_module):
        handler, _, _ = ga_module

        # Even if deregistration blows up, the guard must return success so the
        # stack delete is never wedged in DELETE_FAILED by the guard itself.
        with patch.object(handler, "deregister_alb_from_ga", side_effect=Exception("boom")):
            result = handler.on_delete_event(self._delete_event(), None)

        assert result["PhysicalResourceId"] == "ga-dereg-us-east-1"
