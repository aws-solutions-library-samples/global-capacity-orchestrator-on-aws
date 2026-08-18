"""Gateway ALB registration and endpoint-publication Lambda handler.

The handler converges the regional Gateway API ALB after the Gateway manifests
have been applied:

1. Read the exact ``gco-system/gco-gateway`` Gateway status address.
2. Resolve that address to an active internal ALB, with an exact-tag fallback
   while the Gateway status is still being populated.
3. When a Global Accelerator endpoint group is configured, register only that
   ALB, remove stale endpoints, and enforce the HTTPS health-check contract.
4. Always publish the selected ALB hostname to the SSM endpoint registry.

``EndpointGroupArn`` is optional. Deployments without Global Accelerator still
publish the Gateway hostname.

Entrypoints:
    - ``handle_task`` is the final Step Functions convergence task.
    - ``lambda_handler`` preserves the legacy raw CloudFormation custom-resource
      protocol and dispatches Step Functions events carrying ``Action``.
    - ``on_delete_event`` is the CDK provider delete guard.

The endpoint registry parameter is always:
``/{ProjectName}/alb-hostname-{Region}`` in ``RegistryRegion``. The legacy
``GlobalRegion`` property remains accepted for existing deployments.
"""

import base64
import json
import logging
import os
import tempfile
import time
from contextlib import suppress
from typing import Any

import boto3
import urllib3
from botocore.exceptions import ClientError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-08-14T03:46:22Z
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/ga-registration/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/ga-registration/handler.lambda_handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_WAIT_SECONDS = 840
ALB_POLL_INTERVAL = 5
GA_DEPLOYED_WAIT_SECONDS = 720
GA_DEPLOYED_POLL_INTERVAL = 15
DEFAULT_REGISTRY_REGION = "us-east-2"

GATEWAY_NAMESPACE = "gco-system"
GATEWAY_NAME = "gco-gateway"
GATEWAY_REFERENCE = f"{GATEWAY_NAMESPACE}/{GATEWAY_NAME}"
GATEWAY_TAG = "gco.aws/gateway"
CLUSTER_TAG = "elbv2.k8s.aws/cluster"
GATEWAY_API_PATH = (
    f"/apis/gateway.networking.k8s.io/v1/namespaces/{GATEWAY_NAMESPACE}/gateways/{GATEWAY_NAME}"
)


def send_response(
    event: dict[str, Any],
    context: Any,
    status: str,
    data: dict[str, Any],
    physical_id: str,
    reason: str | None = None,
) -> None:
    """Send a response to a raw CloudFormation custom resource."""
    response_body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data,
    }
    logger.info("Sending CFN response: Status=%s, PhysicalResourceId=%s", status, physical_id)
    http = urllib3.PoolManager()
    try:
        http.request(
            "PUT",
            event["ResponseURL"],
            body=json.dumps(response_body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001 - callback failure can only be logged
        logger.error("Failed to send CloudFormation response: %s", exc)


def _remove_temporary_ca_file(ca_path: str | None) -> None:
    """Unlink a temporary Kubernetes CA file without masking the real result."""
    if not ca_path:
        return
    try:
        os.unlink(ca_path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Failed to remove temporary Kubernetes CA file: %s", exc)


def get_k8s_client(cluster_name: str, region: str) -> tuple[str, str, str]:
    """Return ``(endpoint, bearer_token, temporary_ca_path)`` for an EKS cluster.

    The caller owns the returned CA path and must remove it in a ``finally``
    block. ``mkstemp`` prevents name races; the explicit mode keeps the
    certificate readable only by the Lambda process.
    """
    eks = boto3.client("eks", region_name=region)
    cluster_info = eks.describe_cluster(name=cluster_name)["cluster"]

    session = boto3.Session()
    sts_client = session.client("sts", region_name=region)
    sts_endpoint = str(sts_client.meta.endpoint_url).rstrip("/")
    sts_url = f"{sts_endpoint}/?Action=GetCallerIdentity&Version=2011-06-15"
    signed_url = sts_client._request_signer.generate_presigned_url(  # noqa: SLF001
        request_dict={
            "method": "GET",
            "url": sts_url,
            "body": {},
            "headers": {"x-k8s-aws-id": cluster_name},
            "context": {},
        },
        operation_name="GetCallerIdentity",
        expires_in=60,
    )
    token = "k8s-aws-v1." + base64.urlsafe_b64encode(signed_url.encode()).decode().rstrip("=")

    ca_cert = base64.b64decode(cluster_info["certificateAuthority"]["data"])
    fd, ca_path = tempfile.mkstemp(suffix=".crt")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as ca_file:
            ca_file.write(ca_cert)
    except Exception:
        with suppress(OSError):
            os.close(fd)
        _remove_temporary_ca_file(ca_path)
        raise

    return str(cluster_info["endpoint"]), token, ca_path


def _response_json(response: Any) -> dict[str, Any]:
    """Decode a Kubernetes JSON response, returning an object mapping."""
    raw_data = response.data
    if isinstance(raw_data, bytes):
        raw_data = raw_data.decode("utf-8")
    document = json.loads(raw_data)
    return document if isinstance(document, dict) else {}


def find_gateway_address(
    http: urllib3.PoolManager,
    endpoint: str,
    headers: dict[str, str],
) -> str | None:
    """Read the nonempty hostname from the exact GCO Gateway status."""
    try:
        response = http.request(
            "GET",
            f"{endpoint}{GATEWAY_API_PATH}",
            headers=headers,
            timeout=10.0,
        )
        if response.status == 404:
            logger.debug("Gateway %s not found yet", GATEWAY_REFERENCE)
            return None
        if response.status != 200:
            logger.warning("Gateway status request returned HTTP %s", response.status)
            return None

        addresses = _response_json(response).get("status", {}).get("addresses", [])
        for address in addresses:
            if not isinstance(address, dict):
                continue
            address_type = address.get("type", "Hostname")
            value = address.get("value")
            if address_type == "Hostname" and isinstance(value, str) and value.strip():
                return value.strip()
    except Exception as exc:  # noqa: BLE001 - polling falls back to exact tags
        logger.warning("Error checking Gateway status: %s", exc)
    return None


def _list_load_balancers(elb_client: Any) -> list[dict[str, Any]]:
    """List every load balancer, following ELBv2 marker pagination."""
    load_balancers: list[dict[str, Any]] = []
    marker: str | None = None
    seen_markers: set[str] = set()
    while True:
        kwargs = {"Marker": marker} if marker else {}
        response = elb_client.describe_load_balancers(**kwargs)
        load_balancers.extend(response.get("LoadBalancers", []))
        next_marker = response.get("NextMarker")
        if not isinstance(next_marker, str) or not next_marker:
            return load_balancers
        if next_marker in seen_markers:
            raise RuntimeError(f"ELB pagination repeated marker {next_marker!r}")
        seen_markers.add(next_marker)
        marker = next_marker


def find_alb_by_gateway_hostname(
    elb_client: Any, hostname: str, cluster_name: str
) -> tuple[str | None, str | None, str | None]:
    """Resolve a Gateway hostname only to its exactly owned internal ALB."""
    try:
        candidates = [
            load_balancer
            for load_balancer in _list_load_balancers(elb_client)
            if load_balancer.get("Type") == "application"
            and load_balancer.get("Scheme") == "internal"
            and load_balancer.get("DNSName") == hostname
        ]
        if not candidates:
            return None, None, None

        arns = [str(load_balancer["LoadBalancerArn"]) for load_balancer in candidates]
        tags_by_arn = _describe_tags(elb_client, arns)
        for load_balancer in candidates:
            arn = str(load_balancer["LoadBalancerArn"])
            tags = tags_by_arn.get(arn, {})
            if not (
                tags.get(GATEWAY_TAG) == GATEWAY_REFERENCE and tags.get(CLUSTER_TAG) == cluster_name
            ):
                logger.warning("Rejecting hostname-matched ALB without exact ownership: %s", arn)
                continue
            state = str(load_balancer.get("State", {}).get("Code", "unknown"))
            logger.info(
                "Found exactly owned Gateway ALB by hostname: %s (state: %s)",
                load_balancer.get("LoadBalancerName", "<unknown>"),
                state,
            )
            return str(load_balancer["DNSName"]), arn, state
    except Exception as exc:  # noqa: BLE001 - discovery polling retries
        logger.warning("Error finding Gateway ALB by hostname: %s", exc)
    return None, None, None


def _describe_tags(elb_client: Any, load_balancer_arns: list[str]) -> dict[str, dict[str, str]]:
    """Return ELB tags by ARN, respecting the API's 20-resource limit."""
    tags_by_arn: dict[str, dict[str, str]] = {}
    for index in range(0, len(load_balancer_arns), 20):
        response = elb_client.describe_tags(ResourceArns=load_balancer_arns[index : index + 20])
        for description in response.get("TagDescriptions", []):
            arn = description.get("ResourceArn")
            if not isinstance(arn, str):
                continue
            tags_by_arn[arn] = {
                str(tag["Key"]): str(tag["Value"])
                for tag in description.get("Tags", [])
                if "Key" in tag and "Value" in tag
            }
    return tags_by_arn


def find_platform_alb_by_tags(
    elb_client: Any, cluster_name: str
) -> tuple[str | None, str | None, str | None]:
    """Find the Gateway ALB only when both exact ownership tags match.

    This fallback is used only while the exact Gateway status has no address.
    Cluster-only matches, alternative cluster tags, NLBs, and internet-facing
    ALBs are deliberately rejected.
    """
    try:
        load_balancers = [
            load_balancer
            for load_balancer in _list_load_balancers(elb_client)
            if load_balancer.get("Type") == "application"
            and load_balancer.get("Scheme") == "internal"
        ]
        if not load_balancers:
            return None, None, None

        arns = [str(load_balancer["LoadBalancerArn"]) for load_balancer in load_balancers]
        tags_by_arn = _describe_tags(elb_client, arns)
        for load_balancer in load_balancers:
            arn = str(load_balancer["LoadBalancerArn"])
            tags = tags_by_arn.get(arn, {})
            if not (
                tags.get(GATEWAY_TAG) == GATEWAY_REFERENCE and tags.get(CLUSTER_TAG) == cluster_name
            ):
                continue
            state = str(load_balancer.get("State", {}).get("Code", "unknown"))
            logger.info(
                "Found Gateway ALB by exact tags: %s (state: %s)",
                load_balancer.get("LoadBalancerName", "<unknown>"),
                state,
            )
            return str(load_balancer["DNSName"]), arn, state
    except Exception as exc:  # noqa: BLE001 - discovery polling retries
        logger.warning("Error finding Gateway ALB by tags: %s", exc)
    return None, None, None


def find_active_alb(
    elb_client: Any,
    http: urllib3.PoolManager,
    k8s_endpoint: str,
    k8s_headers: dict[str, str],
    cluster_name: str,
) -> tuple[str | None, str | None]:
    """Find the active ALB owned by the exact GCO Gateway.

    A nonempty Gateway status address is authoritative. Tag fallback is allowed
    only when that address is absent; it never overrides a provisioning or
    otherwise unresolved ALB named by Gateway status.
    """
    hostname = find_gateway_address(http, k8s_endpoint, k8s_headers)
    if hostname:
        dns_name, arn, state = find_alb_by_gateway_hostname(elb_client, hostname, cluster_name)
        if arn and state == "active":
            logger.info("Found active ALB from Gateway status: %s", hostname)
            return dns_name, arn
        if arn:
            logger.info("Gateway ALB state is %r; waiting for 'active'", state)
        return None, None

    dns_name, arn, state = find_platform_alb_by_tags(elb_client, cluster_name)
    if arn and state == "active":
        return dns_name, arn
    if arn:
        logger.info("Gateway ALB found by tags but state is %r; waiting for 'active'", state)
    return None, None


def check_existing_ga_endpoint(ga_client: Any, endpoint_group_arn: str, alb_arn: str) -> bool:
    """Return whether the exact ALB is already registered with GA."""
    try:
        endpoint_group = ga_client.describe_endpoint_group(EndpointGroupArn=endpoint_group_arn)
        endpoints = endpoint_group.get("EndpointGroup", {}).get("EndpointDescriptions", [])
        if any(endpoint.get("EndpointId") == alb_arn for endpoint in endpoints):
            logger.info("ALB %s is already registered with GA", alb_arn)
            return True
    except Exception as exc:  # noqa: BLE001 - add_endpoints remains authoritative
        logger.warning("Error checking existing GA endpoints: %s", exc)
    return False


def scrub_stale_ga_endpoints(ga_client: Any, endpoint_group_arn: str, correct_alb_arn: str) -> None:
    """Remove every GA endpoint other than the exact Gateway ALB."""
    try:
        endpoint_group = ga_client.describe_endpoint_group(EndpointGroupArn=endpoint_group_arn)
        endpoints = endpoint_group.get("EndpointGroup", {}).get("EndpointDescriptions", [])
        for endpoint in endpoints:
            endpoint_id = endpoint.get("EndpointId", "")
            if not endpoint_id or endpoint_id == correct_alb_arn:
                continue
            logger.warning(
                "Removing stale GA endpoint %s; exact Gateway ALB is %s",
                endpoint_id,
                correct_alb_arn,
            )
            try:
                ga_client.remove_endpoints(
                    EndpointGroupArn=endpoint_group_arn,
                    EndpointIdentifiers=[{"EndpointId": endpoint_id}],
                )
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code == "EndpointNotFoundException":
                    logger.info("GA endpoint %s was already absent", endpoint_id)
                else:
                    logger.error("Failed to remove stale GA endpoint %s: %s", endpoint_id, exc)
                    raise
    except Exception as exc:
        logger.error("Error scrubbing stale GA endpoints: %s", exc)
        raise


def register_alb_with_ga(ga_client: Any, endpoint_group_arn: str, alb_arn: str) -> None:
    """Register the exact Gateway ALB with GA, idempotently."""
    if check_existing_ga_endpoint(ga_client, endpoint_group_arn, alb_arn):
        return
    try:
        ga_client.add_endpoints(
            EndpointGroupArn=endpoint_group_arn,
            EndpointConfigurations=[
                {
                    "EndpointId": alb_arn,
                    "Weight": 100,
                    "ClientIPPreservationEnabled": True,
                }
            ],
        )
        logger.info("Registered Gateway ALB %s with Global Accelerator", alb_arn)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "EndpointAlreadyExists":
            logger.info("Gateway ALB was already registered with Global Accelerator")
            return
        raise


def ensure_https_health_check(
    ga_client: Any,
    endpoint_group_arn: str,
    health_check_path: str = "/api/v1/health",
    expected_alb_arn: str | None = None,
) -> None:
    """Enforce the endpoint group's HTTPS/443 health-check contract.

    When ``expected_alb_arn`` is supplied, only that endpoint is preserved in an
    update, preventing an eventually consistent stale endpoint description from
    being reintroduced after the scrub.
    """
    try:
        endpoint_group = ga_client.describe_endpoint_group(EndpointGroupArn=endpoint_group_arn)
        group = endpoint_group.get("EndpointGroup", {})
        current_protocol = group.get("HealthCheckProtocol", "TCP")
        current_port = int(group.get("HealthCheckPort", 0))
        current_path = group.get("HealthCheckPath", "")
        if (
            current_protocol == "HTTPS"
            and current_port == 443
            and current_path == health_check_path
        ):
            return

        existing_endpoints = [
            {
                "EndpointId": endpoint["EndpointId"],
                "Weight": endpoint.get("Weight", 100),
                "ClientIPPreservationEnabled": endpoint.get("ClientIPPreservationEnabled", True),
            }
            for endpoint in group.get("EndpointDescriptions", [])
            if endpoint.get("EndpointId")
            and (expected_alb_arn is None or endpoint.get("EndpointId") == expected_alb_arn)
        ]
        ga_client.update_endpoint_group(
            EndpointGroupArn=endpoint_group_arn,
            HealthCheckPort=443,
            HealthCheckProtocol="HTTPS",
            HealthCheckPath=health_check_path,
            HealthCheckIntervalSeconds=30,
            ThresholdCount=3,
            EndpointConfigurations=existing_endpoints,
        )
        logger.info("Global Accelerator health check set to HTTPS/443 %s", health_check_path)
    except ClientError as exc:
        logger.error("Failed to enforce GA health-check configuration: %s", exc)
        raise


def _get_registry_region(properties: dict[str, Any], default: str | None = None) -> str | None:
    """Return ``RegistryRegion``, accepting ``GlobalRegion`` for compatibility."""
    value = properties.get("RegistryRegion") or properties.get("GlobalRegion") or default
    return str(value) if value else None


def store_alb_hostname_in_ssm(
    region: str, alb_hostname: str, registry_region: str, project_name: str
) -> None:
    """Publish the Gateway ALB hostname to the regional endpoint registry."""
    ssm_client = boto3.client("ssm", region_name=registry_region)
    parameter_name = f"/{project_name}/alb-hostname-{region}"
    ssm_client.put_parameter(
        Name=parameter_name,
        Value=alb_hostname,
        Type="String",
        Overwrite=True,
        Description=f"ALB hostname for {region} regional cluster",
    )
    logger.info("Stored Gateway ALB hostname in SSM: %s = %s", parameter_name, alb_hostname)


def delete_alb_hostname_from_ssm(
    region: str,
    registry_region: str,
    project_name: str,
    *,
    strict: bool = False,
) -> None:
    """Remove this region's endpoint-registry parameter during cleanup."""
    ssm_client = boto3.client("ssm", region_name=registry_region)
    parameter_name = f"/{project_name}/alb-hostname-{region}"
    try:
        ssm_client.delete_parameter(Name=parameter_name)
        logger.info("Deleted Gateway ALB hostname from SSM: %s", parameter_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            logger.info("SSM parameter %s was already absent", parameter_name)
        elif strict:
            raise
        else:
            logger.warning("Failed to delete Gateway ALB hostname from SSM: %s", exc)


def remove_ga_endpoints(
    ga_client: Any,
    endpoint_group_arn: str,
    *,
    strict: bool = False,
) -> None:
    """Remove every endpoint from one regional GA group."""
    try:
        endpoint_group = ga_client.describe_endpoint_group(EndpointGroupArn=endpoint_group_arn)
        endpoints = endpoint_group.get("EndpointGroup", {}).get("EndpointDescriptions", [])
        for endpoint in endpoints:
            endpoint_id = endpoint.get("EndpointId")
            if not endpoint_id:
                continue
            try:
                ga_client.remove_endpoints(
                    EndpointGroupArn=endpoint_group_arn,
                    EndpointIdentifiers=[{"EndpointId": endpoint_id}],
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "EndpointNotFoundException":
                    continue
                if strict:
                    raise
                logger.warning("Failed to remove GA endpoint %s: %s", endpoint_id, exc)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"EndpointGroupNotFoundException", "AcceleratorNotFoundException"}:
            logger.info("Global Accelerator endpoint group was already absent")
            return
        if strict:
            raise
        logger.warning("Failed to clean up GA endpoints: %s", exc)
    except Exception as exc:  # noqa: BLE001 - delete guards remain best effort
        if strict:
            raise
        logger.warning("Failed to clean up GA endpoints: %s", exc)


def _accelerator_arn_from_endpoint_group(endpoint_group_arn: str) -> str:
    """Derive an accelerator ARN from one of its endpoint-group ARNs."""
    return endpoint_group_arn.split("/listener/")[0]


def wait_for_accelerator_deployed(
    ga_client: Any,
    endpoint_group_arn: str,
    timeout_seconds: int = GA_DEPLOYED_WAIT_SECONDS,
    *,
    strict: bool = False,
) -> bool:
    """Wait until GA finishes redeploying and releases its managed ENIs.

    In strict mode a describe failure raises immediately instead of being
    reported as a timeout; a permissions gap must surface as itself.
    """
    accelerator_arn = _accelerator_arn_from_endpoint_group(endpoint_group_arn)
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        try:
            accelerator = ga_client.describe_accelerator(AcceleratorArn=accelerator_arn)
            status = accelerator.get("Accelerator", {}).get("Status", "")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "AcceleratorNotFoundException":
                return True
            if strict:
                raise
            logger.warning("Failed to describe accelerator status: %s", exc)
            return False
        if status == "DEPLOYED":
            return True
        logger.info("Accelerator status=%r; waiting for DEPLOYED", status)
        # nosemgrep: arbitrary-sleep - intentional GA redeployment polling
        time.sleep(GA_DEPLOYED_POLL_INTERVAL)
    logger.warning("Timed out waiting for Global Accelerator to reach DEPLOYED")
    return False


def deregister_alb_from_ga(
    ga_client: Any,
    endpoint_group_arn: str,
    *,
    strict: bool = False,
) -> None:
    """Remove regional GA endpoints, then wait for managed ENI release."""
    if strict:
        remove_ga_endpoints(ga_client, endpoint_group_arn, strict=True)
    else:
        remove_ga_endpoints(ga_client, endpoint_group_arn)
    deployed = wait_for_accelerator_deployed(ga_client, endpoint_group_arn, strict=strict)
    if strict and not deployed:
        raise TimeoutError("Global Accelerator did not reach DEPLOYED after endpoint removal")


def _optional_endpoint_group_arn(properties: dict[str, Any]) -> str | None:
    """Normalize an optional endpoint-group property."""
    value = properties.get("EndpointGroupArn")
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def register_ga_endpoint(
    cluster_name: str,
    region: str,
    endpoint_group_arn: str | None = None,
    registry_region: str = DEFAULT_REGISTRY_REGION,
    project_name: str = "gco",
) -> dict[str, str]:
    """Converge the Gateway ALB, optional GA endpoint, registry, and migration."""
    ca_path: str | None = None
    try:
        k8s_endpoint, token, ca_path = get_k8s_client(cluster_name, region)
        http = urllib3.PoolManager(cert_reqs="CERT_REQUIRED", ca_certs=ca_path)
        k8s_headers = {"Authorization": f"Bearer {token}"}
        elb_client = boto3.client("elbv2", region_name=region)

        logger.info("Waiting for active %s ALB", GATEWAY_REFERENCE)
        start_time = time.time()
        last_log_time = start_time
        alb_hostname: str | None = None
        alb_arn: str | None = None
        while time.time() - start_time < MAX_WAIT_SECONDS:
            alb_hostname, alb_arn = find_active_alb(
                elb_client,
                http,
                k8s_endpoint,
                k8s_headers,
                cluster_name,
            )
            if alb_arn:
                break
            if time.time() - last_log_time >= 30:
                elapsed = int(time.time() - start_time)
                logger.info("Still waiting for Gateway ALB (%ss elapsed)", elapsed)
                last_log_time = time.time()
            # nosemgrep: arbitrary-sleep - intentional Gateway/ALB polling
            time.sleep(ALB_POLL_INTERVAL)

        if not alb_arn or not alb_hostname:
            elapsed = int(time.time() - start_time)
            raise TimeoutError(
                f"Timed out waiting for active {GATEWAY_REFERENCE} ALB after {elapsed} seconds"
            )

        normalized_endpoint_group = (
            str(endpoint_group_arn).strip() if endpoint_group_arn is not None else ""
        )
        if normalized_endpoint_group:
            ga_client = boto3.client("globalaccelerator", region_name="us-west-2")
            register_alb_with_ga(ga_client, normalized_endpoint_group, alb_arn)
            scrub_stale_ga_endpoints(ga_client, normalized_endpoint_group, alb_arn)
            ensure_https_health_check(
                ga_client,
                normalized_endpoint_group,
                expected_alb_arn=alb_arn,
            )
            # AddEndpoints/RemoveEndpoints/UpdateEndpointGroup only submit a
            # configuration change; the accelerator serves it from its edge
            # locations only after returning to DEPLOYED. Returning success
            # earlier reports the deployment as complete while brand-new
            # connections to the global endpoint still black-hole for several
            # minutes, which a live release run proved by timing out on the
            # first health probe after deploy. Wait strictly, within whatever
            # remains of this handler's wall-clock budget.
            remaining_budget = min(
                GA_DEPLOYED_WAIT_SECONDS,
                int(MAX_WAIT_SECONDS - (time.time() - start_time)),
            )
            if remaining_budget <= 0 or not wait_for_accelerator_deployed(
                ga_client,
                normalized_endpoint_group,
                timeout_seconds=remaining_budget,
                strict=True,
            ):
                raise TimeoutError(
                    "Global Accelerator did not reach DEPLOYED after endpoint registration"
                )
        else:
            logger.info("EndpointGroupArn is not configured; skipping Global Accelerator")

        # Publication is mandatory even when Global Accelerator is disabled.
        store_alb_hostname_in_ssm(
            region,
            alb_hostname,
            registry_region,
            project_name,
        )
        return {"AlbArn": alb_arn, "AlbHostname": alb_hostname}
    finally:
        _remove_temporary_ca_file(ca_path)


def cleanup_gateway_endpoint(
    *,
    region: str,
    endpoint_group_arn: str | None,
    registry_region: str,
    project_name: str,
) -> dict[str, bool]:
    """Strictly fence endpoint publication before Gateway deletion."""
    delete_alb_hostname_from_ssm(
        region,
        registry_region,
        project_name,
        strict=True,
    )
    if endpoint_group_arn:
        ga_client = boto3.client("globalaccelerator", region_name="us-west-2")
        deregister_alb_from_ga(ga_client, endpoint_group_arn, strict=True)
    return {
        "RegistryParameterDeleted": True,
        "GlobalAcceleratorDeregistered": bool(endpoint_group_arn),
    }


def handle_task(event: dict[str, Any]) -> dict[str, Any]:
    """Handle Step Functions convergence and teardown task invocations."""
    if event.get("Action") == "cleanup_gateway_endpoint":
        return cleanup_gateway_endpoint(
            region=str(event["Region"]),
            endpoint_group_arn=_optional_endpoint_group_arn(event),
            registry_region=_get_registry_region(event, DEFAULT_REGISTRY_REGION)
            or DEFAULT_REGISTRY_REGION,
            project_name=str(event.get("ProjectName", "gco")),
        )

    return register_ga_endpoint(
        cluster_name=str(event["ClusterName"]),
        region=str(event["Region"]),
        endpoint_group_arn=_optional_endpoint_group_arn(event),
        registry_region=_get_registry_region(event, DEFAULT_REGISTRY_REGION)
        or DEFAULT_REGISTRY_REGION,
        project_name=str(event.get("ProjectName", "gco")),
    )


def handle_create_update(
    event: dict[str, Any], context: Any, props: dict[str, Any], physical_id: str
) -> None:
    """Handle Create/Update through the raw CloudFormation protocol."""
    data = register_ga_endpoint(
        cluster_name=str(props["ClusterName"]),
        region=str(props["Region"]),
        endpoint_group_arn=_optional_endpoint_group_arn(props),
        registry_region=_get_registry_region(props, DEFAULT_REGISTRY_REGION)
        or DEFAULT_REGISTRY_REGION,
        project_name=str(props.get("ProjectName", "gco")),
    )
    send_response(event, context, "SUCCESS", data, physical_id)


def handle_delete(
    event: dict[str, Any], context: Any, props: dict[str, Any], physical_id: str
) -> None:
    """Always remove SSM and conditionally deregister/wait for GA."""
    endpoint_group_arn = _optional_endpoint_group_arn(props)
    if endpoint_group_arn:
        try:
            ga_client = boto3.client("globalaccelerator", region_name="us-west-2")
            deregister_alb_from_ga(ga_client, endpoint_group_arn)
        except Exception as exc:  # noqa: BLE001 - Delete must continue to SSM cleanup
            logger.error("GA deregistration failed during Delete: %s", exc, exc_info=True)

    region = str(props["Region"])
    registry_region = _get_registry_region(props, DEFAULT_REGISTRY_REGION)
    assert registry_region is not None
    project_name = str(props.get("ProjectName", "gco"))
    try:
        delete_alb_hostname_from_ssm(region, registry_region, project_name)
    except Exception as exc:  # noqa: BLE001 - raw Delete must always respond success
        logger.error("SSM registry cleanup failed during Delete: %s", exc, exc_info=True)

    send_response(event, context, "SUCCESS", {}, physical_id)


def on_delete_event(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """CDK provider guard: no-op on Create/Update, cleanup on Delete."""
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    physical_id = event.get("PhysicalResourceId") or f"ga-dereg-{props.get('Region', 'unknown')}"
    if request_type != "Delete":
        return {"PhysicalResourceId": physical_id}

    endpoint_group_arn = _optional_endpoint_group_arn(props)
    if endpoint_group_arn:
        try:
            ga_client = boto3.client("globalaccelerator", region_name="us-west-2")
            deregister_alb_from_ga(ga_client, endpoint_group_arn)
        except Exception as exc:  # noqa: BLE001 - provider Delete must never wedge the stack
            logger.error("GA deregistration guard failed: %s", exc, exc_info=True)

    region = props.get("Region")
    if region:
        registry_region = _get_registry_region(props, DEFAULT_REGISTRY_REGION)
        assert registry_region is not None
        try:
            delete_alb_hostname_from_ssm(
                str(region),
                registry_region,
                str(props.get("ProjectName", "gco")),
            )
        except Exception as exc:  # noqa: BLE001 - provider Delete must never wedge the stack
            logger.error("SSM registry cleanup guard failed: %s", exc, exc_info=True)
    else:
        logger.warning("No Region supplied; cannot identify the SSM registry parameter")

    return {"PhysicalResourceId": physical_id}


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    """Dispatch Step Functions tasks or raw CloudFormation resource events."""
    if event.get("Action"):
        logger.info("Task event: %s", json.dumps(event))
        return handle_task(event)

    logger.info("CloudFormation event: %s", json.dumps(event))
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    physical_id = event.get("PhysicalResourceId", f"ga-reg-{props['ClusterName']}")
    try:
        if request_type == "Delete":
            handle_delete(event, context, props, physical_id)
        else:
            handle_create_update(event, context, props, physical_id)
    except Exception as exc:  # noqa: BLE001 - must answer the raw custom resource
        logger.error("Registration handler failed: %s", exc, exc_info=True)
        if request_type == "Delete":
            send_response(event, context, "SUCCESS", {}, physical_id)
        else:
            send_response(event, context, "FAILED", {}, physical_id, str(exc))
