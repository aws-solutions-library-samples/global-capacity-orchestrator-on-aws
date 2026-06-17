"""
GA Registration Lambda Handler.

This Lambda registers the Ingress-created ALB with Global Accelerator.

Workflow:
    1. Waits for the ALB to be created and become active
    2. Uses multiple detection methods (tags, Ingress status, name prefix)
    3. Registers that ALB with Global Accelerator
    4. Stores the ALB hostname in SSM for cross-region aggregation
    5. Handles idempotency (won't fail if ALB already registered)

This is necessary because the ALB is created by the AWS Load Balancer Controller
(not CDK), so we can't directly reference its ARN.

SSM Parameter Storage:
    The ALB hostname is stored in SSM Parameter Store at:
    /{project_name}/alb-hostname-{region}

    This allows the cross-region aggregator Lambda to discover all regional
    endpoints without hardcoding them in environment variables.

Environment Variables (from CloudFormation properties):
    ClusterName: EKS cluster name
    Region: AWS region for this cluster
    EndpointGroupArn: Global Accelerator endpoint group ARN
    IngressName: Kubernetes Ingress name (default: gco-ingress)
    Namespace: Kubernetes namespace (default: gco-system)
    GlobalRegion: Region for SSM parameters (default: us-east-2)
    ProjectName: Project name for SSM paths (default: gco)
"""

import base64
import json
import logging
import os
import tempfile
import time
from typing import Any

import boto3
import urllib3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``lambda_handler`` -> ``diagrams/code_diagrams/lambda/ga-registration/handler.lambda_handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/ga-registration/handler.lambda_handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration constants
# Lambda max timeout is 15 minutes (900 seconds)
# On fresh deployments, the AWS Load Balancer Controller needs time to:
# 1. Start up and become ready (nodes need to be provisioned first)
# 2. Process the Ingress resource
# 3. Create the ALB and wait for it to become active
# We use a single polling loop with 14 min budget (leaving 1 min for init/registration)
MAX_WAIT_SECONDS = 840  # 14 minutes total budget for finding active ALB
ALB_POLL_INTERVAL = 5  # Poll every 5 seconds to detect ALB quickly
ALB_DELETION_POLL_INTERVAL = 10  # 10 seconds for deletion polling
ALB_DELETION_WAIT_SECONDS = 180  # 3 minutes for ALB deletion during cleanup


def send_response(
    event: dict[str, Any],
    context: Any,
    status: str,
    data: dict[str, Any],
    physical_id: str,
    reason: str | None = None,
) -> None:
    """Send response to CloudFormation."""
    response_body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physical_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data,
    }
    logger.info(f"Sending CFN response: Status={status}, PhysicalResourceId={physical_id}")
    # Timeout is for the CFN response callback (HTTP PUT to S3 presigned URL),
    # not for GA registration operations. K8s API calls have their own timeouts.
    http = urllib3.PoolManager()
    try:
        http.request(
            "PUT",
            event["ResponseURL"],
            body=json.dumps(response_body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
    except Exception as e:
        logger.error(f"Failed to send CloudFormation response: {e}")


def get_k8s_client(cluster_name: str, region: str) -> tuple[str, str, str]:
    """Get Kubernetes API client configuration.

    Returns:
        Tuple of (endpoint, token, ca_path)
    """
    eks = boto3.client("eks", region_name=region)
    cluster_info = eks.describe_cluster(name=cluster_name)["cluster"]

    # Generate EKS authentication token using STS presigned URL
    session = boto3.Session()
    sts_url = f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15"
    request = AWSRequest(method="GET", url=sts_url, headers={"x-k8s-aws-id": cluster_name})
    SigV4Auth(session.get_credentials(), "sts", region).add_auth(request)

    # Build the presigned URL from the signed request
    signed_url = f"{request.url}"
    for header, value in request.headers.items():
        if header.lower().startswith("x-amz-"):
            separator = "&" if "?" in signed_url else "?"
            signed_url += f"{separator}{header}={value}"

    # Encode as EKS token
    token = "k8s-aws-v1." + base64.urlsafe_b64encode(signed_url.encode()).decode().rstrip("=")

    ca_cert = base64.b64decode(cluster_info["certificateAuthority"]["data"])
    fd, ca_path = tempfile.mkstemp(suffix=".crt")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(ca_cert)
    except Exception:
        os.close(fd)
        raise

    return cluster_info["endpoint"], token, ca_path


def find_alb_by_ingress_hostname(
    elb_client: Any, hostname: str
) -> tuple[str | None, str | None, str | None]:
    """Find ALB by its DNS hostname (most deterministic method).

    Given a hostname from the Ingress status, look up the matching ALB
    in the ELBv2 API. This is the most reliable approach because the
    Ingress status is the single source of truth for which ALB was
    created by the load balancer controller.

    Returns:
        Tuple of (dns_name, arn, state) or (None, None, None) if not found
    """
    try:
        albs = elb_client.describe_load_balancers()["LoadBalancers"]
        for alb in albs:
            if alb.get("Type") == "application" and alb["DNSName"] == hostname:
                state = alb.get("State", {}).get("Code", "unknown")
                logger.info(f"Found ALB by hostname: {alb['LoadBalancerName']} (state: {state})")
                return alb["DNSName"], alb["LoadBalancerArn"], state
    except Exception as e:
        logger.warning(f"Error finding ALB by hostname: {e}")
    return None, None, None


def find_platform_alb_by_tags(
    elb_client: Any, cluster_name: str
) -> tuple[str | None, str | None, str | None]:
    """Find the platform ALB by tags (fallback when Ingress status is empty).

    Only matches ALBs (not NLBs) that belong to the platform ingress group.
    Explicitly excludes inference ALBs and any non-ALB load balancers.

    The platform ALB is identified by:
    - Type: application (not network or gateway)
    - Cluster tag matching
    - Ingress stack tag that does NOT contain 'inference'

    Returns:
        Tuple of (dns_name, arn, state) or (None, None, None) if not found
    """
    try:
        all_lbs = elb_client.describe_load_balancers()["LoadBalancers"]
        # CRITICAL: Only consider ALBs — NLBs (Slurm, etc.) must never be registered
        albs = [a for a in all_lbs if a.get("Type") == "application"]
        if not albs:
            return None, None, None

        alb_arns = [alb["LoadBalancerArn"] for alb in albs]
        # describe_tags supports max 20 ARNs per call
        all_tags = {}
        for i in range(0, len(alb_arns), 20):
            batch = alb_arns[i : i + 20]
            resp = elb_client.describe_tags(ResourceArns=batch)
            for td in resp.get("TagDescriptions", []):
                all_tags[td["ResourceArn"]] = {t["Key"]: t["Value"] for t in td.get("Tags", [])}

        for alb in albs:
            arn = alb["LoadBalancerArn"]
            tags = all_tags.get(arn, {})

            # Must belong to this cluster
            cluster_match = (
                tags.get("eks:eks-cluster-name") == cluster_name
                or tags.get("elbv2.k8s.aws/cluster") == cluster_name
            )
            if not cluster_match:
                continue

            # Determine the ingress stack/group name from tags
            stack = tags.get("ingress.eks.amazonaws.com/stack", "") or tags.get(
                "ingress.k8s.aws/stack", ""
            )

            # Safety net: skip any stale inference ALBs left from previous
            # deployments that used a separate inference IngressClass.
            # Current architecture uses a single ALB for all traffic.
            if "inference" in stack.lower():
                logger.debug(f"Skipping inference ALB: {alb['LoadBalancerName']} (stack={stack})")
                continue

            # Must have an ingress stack tag (proves it's an Ingress-created ALB,
            # not a Service-created NLB that somehow passed the type filter)
            if not stack:
                logger.debug(f"Skipping ALB without ingress stack tag: {alb['LoadBalancerName']}")
                continue

            state = alb.get("State", {}).get("Code", "unknown")
            logger.info(
                f"Found platform ALB by tags: {alb['LoadBalancerName']} "
                f"(stack={stack}, state={state})"
            )
            return alb["DNSName"], arn, state

    except Exception as e:
        logger.warning(f"Error finding platform ALB by tags: {e}")
    return None, None, None


def find_alb_from_ingress_status(
    http: urllib3.PoolManager,
    endpoint: str,
    headers: dict[str, str],
    namespace: str,
    ingress_name: str,
) -> str | None:
    """Try to find ALB hostname from Ingress status.

    Returns:
        ALB hostname if found, None otherwise
    """
    try:
        resp = http.request(
            "GET",
            f"{endpoint}/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses/{ingress_name}",
            headers=headers,
            timeout=10.0,
        )
        if resp.status == 200:
            ingress = json.loads(resp.data.decode())
            lb_ingress = ingress.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            if lb_ingress and lb_ingress[0].get("hostname"):
                hostname = lb_ingress[0]["hostname"]
                assert isinstance(hostname, str)
                return hostname
        elif resp.status == 404:
            logger.debug(f"Ingress {namespace}/{ingress_name} not found yet")
    except Exception as e:
        logger.warning(f"Error checking Ingress status: {e}")
    return None


def find_active_alb(
    elb_client: Any,
    http: urllib3.PoolManager,
    k8s_endpoint: str,
    k8s_headers: dict[str, str],
    cluster_name: str,
    namespace: str,
    ingress_name: str,
) -> tuple[str | None, str | None]:
    """Find the active platform ALB deterministically.

    Uses two methods in order of reliability:
    1. Ingress status hostname → ELB lookup (most deterministic — the Ingress
       status is the single source of truth for which ALB was assigned)
    2. Tag-based detection (fallback for when Ingress status is empty, e.g.
       during initial creation before the LB controller populates it)

    Returns:
        Tuple of (dns_name, arn) if active ALB found, (None, None) otherwise
    """
    # Method 1: Ingress status → ELB lookup (most deterministic)
    hostname = find_alb_from_ingress_status(
        http, k8s_endpoint, k8s_headers, namespace, ingress_name
    )
    if hostname:
        dns_name, arn, state = find_alb_by_ingress_hostname(elb_client, hostname)
        if arn and state == "active":
            logger.info(f"Found active ALB from Ingress status: {hostname}")
            return dns_name, arn
        if arn:
            logger.info(f"ALB from Ingress status has state '{state}', waiting for 'active'")
        return None, None

    # Method 2: Tag-based detection (fallback)
    dns_name, arn, state = find_platform_alb_by_tags(elb_client, cluster_name)
    if arn:
        if state == "active":
            return dns_name, arn
        logger.info(f"ALB found by tags but state is '{state}', waiting for 'active'")

    return None, None


def check_existing_ga_endpoint(ga_client: Any, endpoint_group_arn: str, alb_arn: str) -> bool:
    """Check if ALB is already registered with Global Accelerator."""
    try:
        endpoint_group = ga_client.describe_endpoint_group(EndpointGroupArn=endpoint_group_arn)
        endpoints = endpoint_group.get("EndpointGroup", {}).get("EndpointDescriptions", [])
        for ep in endpoints:
            if ep.get("EndpointId") == alb_arn:
                logger.info(f"ALB {alb_arn} is already registered with GA")
                return True
    except Exception as e:
        logger.warning(f"Error checking existing GA endpoints: {e}")
    return False


def scrub_stale_ga_endpoints(ga_client: Any, endpoint_group_arn: str, correct_alb_arn: str) -> None:
    """Remove any GA endpoints that are NOT the correct platform ALB.

    This is a safety net that runs on every Create/Update. It ensures that
    only the platform ALB is registered with GA — no inference ALBs, no
    Slurm NLBs, no stale endpoints from previous deployments.

    Args:
        ga_client: Global Accelerator client
        endpoint_group_arn: The endpoint group to scrub
        correct_alb_arn: The ARN of the platform ALB that SHOULD be registered
    """
    try:
        endpoint_group = ga_client.describe_endpoint_group(EndpointGroupArn=endpoint_group_arn)
        endpoints = endpoint_group.get("EndpointGroup", {}).get("EndpointDescriptions", [])

        for ep in endpoints:
            endpoint_id = ep.get("EndpointId", "")
            if endpoint_id and endpoint_id != correct_alb_arn:
                logger.warning(
                    f"Removing stale GA endpoint: {endpoint_id} "
                    f"(only {correct_alb_arn} should be registered)"
                )
                try:
                    ga_client.remove_endpoints(
                        EndpointGroupArn=endpoint_group_arn,
                        EndpointIdentifiers=[{"EndpointId": endpoint_id}],
                    )
                    logger.info(f"Removed stale endpoint: {endpoint_id}")
                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code", "")
                    if error_code == "EndpointNotFoundException":
                        logger.info(f"Endpoint {endpoint_id} already gone")
                    else:
                        logger.warning(f"Failed to remove stale endpoint {endpoint_id}: {e}")
    except Exception as e:
        logger.warning(f"Error scrubbing stale GA endpoints: {e}")


def register_alb_with_ga(ga_client: Any, endpoint_group_arn: str, alb_arn: str) -> None:
    """Register ALB with Global Accelerator, handling idempotency."""
    if check_existing_ga_endpoint(ga_client, endpoint_group_arn, alb_arn):
        logger.info("ALB already registered, skipping registration")
        return

    try:
        ga_client.add_endpoints(
            EndpointGroupArn=endpoint_group_arn,
            EndpointConfigurations=[
                {"EndpointId": alb_arn, "Weight": 100, "ClientIPPreservationEnabled": True}
            ],
        )
        logger.info(f"Successfully registered ALB {alb_arn} with Global Accelerator")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "EndpointAlreadyExists":
            logger.info("ALB already registered (caught EndpointAlreadyExists)")
        else:
            raise


def ensure_http_health_check(
    ga_client: Any,
    endpoint_group_arn: str,
    health_check_path: str = "/api/v1/health",
) -> None:
    """Ensure the GA endpoint group uses HTTP health checks instead of TCP.

    TCP health checks only verify the port is open. HTTP health checks verify
    the backend services are actually responding, which is critical for
    accurate health-based routing.

    This is a safety net — the CDK stack should configure this, but if the
    endpoint group was created with defaults (TCP), this fixes it.
    """
    try:
        endpoint_group = ga_client.describe_endpoint_group(EndpointGroupArn=endpoint_group_arn)
        eg = endpoint_group.get("EndpointGroup", {})
        current_protocol = eg.get("HealthCheckProtocol", "TCP")
        current_path = eg.get("HealthCheckPath", "")

        if current_protocol == "HTTP" and current_path == health_check_path:
            logger.info(f"Health check already configured: HTTP {health_check_path}")
            return

        logger.info(f"Updating health check from {current_protocol} to HTTP {health_check_path}")

        # Preserve existing endpoints when updating the endpoint group
        existing_endpoints = [
            {
                "EndpointId": ep["EndpointId"],
                "Weight": ep.get("Weight", 100),
                "ClientIPPreservationEnabled": ep.get("ClientIPPreservationEnabled", True),
            }
            for ep in eg.get("EndpointDescriptions", [])
        ]

        ga_client.update_endpoint_group(
            EndpointGroupArn=endpoint_group_arn,
            HealthCheckPort=80,
            HealthCheckProtocol="HTTP",
            HealthCheckPath=health_check_path,
            HealthCheckIntervalSeconds=30,
            ThresholdCount=3,
            EndpointConfigurations=existing_endpoints,
        )
        logger.info("Health check updated to HTTP successfully")
    except ClientError as e:
        logger.warning(f"Failed to update health check configuration: {e}")
        # Non-fatal — the endpoint is still registered, just with TCP health checks


def store_alb_hostname_in_ssm(
    region: str, alb_hostname: str, global_region: str, project_name: str
) -> None:
    """Store ALB hostname in SSM Parameter Store for cross-region aggregation.

    The parameter is stored in the global region so the cross-region aggregator
    can discover all regional ALB endpoints.
    """
    ssm_client = boto3.client("ssm", region_name=global_region)
    parameter_name = f"/{project_name}/alb-hostname-{region}"

    try:
        ssm_client.put_parameter(
            Name=parameter_name,
            Value=alb_hostname,
            Type="String",
            Overwrite=True,
            Description=f"ALB hostname for {region} regional cluster",
        )
        logger.info(f"Stored ALB hostname in SSM: {parameter_name} = {alb_hostname}")
    except ClientError as e:
        logger.error(f"Failed to store ALB hostname in SSM: {e}")
        raise


def delete_alb_hostname_from_ssm(region: str, global_region: str, project_name: str) -> None:
    """Delete ALB hostname from SSM Parameter Store during cleanup."""
    ssm_client = boto3.client("ssm", region_name=global_region)
    parameter_name = f"/{project_name}/alb-hostname-{region}"

    try:
        ssm_client.delete_parameter(Name=parameter_name)
        logger.info(f"Deleted ALB hostname from SSM: {parameter_name}")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "ParameterNotFound":
            logger.info(f"SSM parameter {parameter_name} not found, nothing to delete")
        else:
            logger.warning(f"Failed to delete ALB hostname from SSM: {e}")


def remove_ga_endpoints(ga_client: Any, endpoint_group_arn: str) -> None:
    """Remove all endpoints from GA endpoint group."""
    try:
        endpoint_group = ga_client.describe_endpoint_group(EndpointGroupArn=endpoint_group_arn)
        endpoints = endpoint_group.get("EndpointGroup", {}).get("EndpointDescriptions", [])

        for ep in endpoints:
            endpoint_id = ep.get("EndpointId")
            if endpoint_id:
                logger.info(f"Removing endpoint {endpoint_id} from GA")
                try:
                    ga_client.remove_endpoints(
                        EndpointGroupArn=endpoint_group_arn,
                        EndpointIdentifiers=[{"EndpointId": endpoint_id}],
                    )
                except ClientError as e:
                    error_code = e.response.get("Error", {}).get("Code", "")
                    if error_code == "EndpointNotFoundException":
                        logger.info(f"Endpoint {endpoint_id} already removed")
                    else:
                        logger.warning(f"Failed to remove endpoint {endpoint_id}: {e}")
    except Exception as e:
        logger.warning(f"Failed to clean up GA endpoints: {e}")


def delete_ingress_and_wait_for_alb_deletion(
    cluster_name: str, region: str, namespace: str, ingress_name: str
) -> None:
    """Delete Ingress and wait for ALB to be deleted."""
    try:
        endpoint, token, ca_path = get_k8s_client(cluster_name, region)
        http = urllib3.PoolManager(cert_reqs="CERT_REQUIRED", ca_certs=ca_path)
        headers = {"Authorization": f"Bearer {token}"}

        logger.info(f"Deleting Ingress {namespace}/{ingress_name}")
        resp = http.request(
            "DELETE",
            f"{endpoint}/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses/{ingress_name}",
            headers=headers,
            timeout=30.0,
        )
        logger.info(f"Ingress delete response: {resp.status}")

        # Wait for ALB to be deleted — use the namespace prefix to identify
        # ALBs created by the load balancer controller for this namespace.
        elb = boto3.client("elbv2", region_name=region)
        ns_prefix = namespace.replace("-", "")[:8]
        alb_prefix = f"k8s-{ns_prefix}"
        start_time = time.time()

        while time.time() - start_time < ALB_DELETION_WAIT_SECONDS:
            albs = elb.describe_load_balancers()["LoadBalancers"]
            k8s_albs = [a for a in albs if a.get("LoadBalancerName", "").startswith(alb_prefix)]
            if not k8s_albs:
                logger.info("ALB deleted successfully")
                return
            elapsed = int(time.time() - start_time)
            logger.info(f"Waiting for ALB deletion... ({elapsed}s elapsed)")
            # nosemgrep: arbitrary-sleep - intentional polling for ALB deletion
            time.sleep(ALB_DELETION_POLL_INTERVAL)

        logger.warning("Timed out waiting for ALB deletion, continuing anyway")
    except Exception as e:
        logger.warning(f"Failed to delete Ingress: {e}")


def handle_delete(
    event: dict[str, Any], context: Any, props: dict[str, Any], physical_id: str
) -> None:
    """Handle Delete request."""
    logger.info("Processing Delete request - cleaning up GA endpoint and Ingress")

    cluster_name = props["ClusterName"]
    region = props["Region"]
    endpoint_group_arn = props["EndpointGroupArn"]
    ingress_name = props.get("IngressName", "gco-ingress")
    namespace = props.get("Namespace", "gco-system")
    global_region = props.get("GlobalRegion", "us-east-2")
    project_name = props.get("ProjectName", "gco")

    # Step 1: Remove all endpoints from GA endpoint group
    ga = boto3.client("globalaccelerator", region_name="us-west-2")
    remove_ga_endpoints(ga, endpoint_group_arn)

    # Step 2: Delete the Ingress to trigger ALB deletion
    delete_ingress_and_wait_for_alb_deletion(cluster_name, region, namespace, ingress_name)

    # Step 3: Clean up ALB hostname from SSM
    delete_alb_hostname_from_ssm(region, global_region, project_name)

    send_response(event, context, "SUCCESS", {}, physical_id)


def register_ga_endpoint(
    cluster_name: str,
    region: str,
    endpoint_group_arn: str,
    ingress_name: str = "gco-ingress",
    namespace: str = "gco-system",
    global_region: str = "us-east-2",
    project_name: str = "gco",
) -> dict[str, str]:
    """Find the active platform ALB and register it with Global Accelerator.

    Shared core for both entrypoints (the Step Functions convergence task and
    the legacy CloudFormation custom resource). Polls for the active ALB,
    registers it (idempotent), scrubs stale endpoints, enforces HTTP health
    checks, and records the ALB hostname in SSM. Raises on timeout so the
    caller can react. Returns ``{"AlbArn", "AlbHostname"}``.
    """
    # Initialize clients
    k8s_endpoint, token, ca_path = get_k8s_client(cluster_name, region)
    http = urllib3.PoolManager(cert_reqs="CERT_REQUIRED", ca_certs=ca_path)
    k8s_headers = {"Authorization": f"Bearer {token}"}
    elb = boto3.client("elbv2", region_name=region)
    ga = boto3.client("globalaccelerator", region_name="us-west-2")

    # Wait for active ALB using unified polling loop
    logger.info(f"Waiting for active ALB (max {MAX_WAIT_SECONDS / 60:.0f} minutes)...")
    start_time = time.time()
    last_log_time = start_time
    alb_hostname = None
    alb_arn = None

    while time.time() - start_time < MAX_WAIT_SECONDS:
        alb_hostname, alb_arn = find_active_alb(
            elb, http, k8s_endpoint, k8s_headers, cluster_name, namespace, ingress_name
        )

        if alb_arn:
            break

        # Log progress every 30 seconds
        if time.time() - last_log_time >= 30:
            elapsed = int(time.time() - start_time)
            remaining = int(MAX_WAIT_SECONDS - elapsed)
            logger.info(f"Still waiting for ALB... ({elapsed}s elapsed, {remaining}s remaining)")
            last_log_time = time.time()

        time.sleep(ALB_POLL_INTERVAL)  # nosemgrep: arbitrary-sleep - intentional polling

    if not alb_arn:
        elapsed = int(time.time() - start_time)
        raise Exception(
            f"Timed out waiting for active ALB after {elapsed} seconds. "
            "Check AWS Load Balancer Controller logs and ensure Ingress was created."
        )

    # By construction, find_active_alb returns either (None, None) or (str, str),
    # so when alb_arn is set alb_hostname is also set. Assert this for mypy.
    assert alb_hostname is not None

    elapsed = int(time.time() - start_time)
    logger.info(f"Found active ALB in {elapsed} seconds: {alb_hostname} ({alb_arn})")

    # Register ALB with Global Accelerator (handles idempotency)
    register_alb_with_ga(ga, endpoint_group_arn, alb_arn)

    # Scrub any stale endpoints (inference ALBs, Slurm NLBs, old ALBs from
    # previous deployments). Only the platform ALB should be in GA.
    scrub_stale_ga_endpoints(ga, endpoint_group_arn, alb_arn)

    # Ensure the endpoint group uses HTTP health checks (not TCP).
    # This is a safety net — the CDK stack should configure this, but if
    # the endpoint group was created with defaults, this fixes it.
    ensure_http_health_check(ga, endpoint_group_arn)

    # Store ALB hostname in SSM for cross-region aggregation
    store_alb_hostname_in_ssm(region, alb_hostname, global_region, project_name)

    return {"AlbArn": alb_arn, "AlbHostname": alb_hostname}


def handle_task(event: dict[str, Any]) -> dict[str, Any]:
    """Register the ALB with Global Accelerator for a Step Functions task.

    The convergence state machine invokes this as its final step, after the Helm
    charts and post-Helm manifests are applied. Reads the same parameters the
    custom-resource path takes from ``ResourceProperties`` — but flat on the
    event — and returns ``{AlbArn, AlbHostname}``. Raises on failure so the state
    machine can retry (then catch-and-continue, since GA registration must not
    wedge the rest of the pipeline).
    """
    return register_ga_endpoint(
        cluster_name=event["ClusterName"],
        region=event["Region"],
        endpoint_group_arn=event["EndpointGroupArn"],
        ingress_name=event.get("IngressName", "gco-ingress"),
        namespace=event.get("Namespace", "gco-system"),
        global_region=event.get("GlobalRegion", "us-east-2"),
        project_name=event.get("ProjectName", "gco"),
    )


def handle_create_update(
    event: dict[str, Any], context: Any, props: dict[str, Any], physical_id: str
) -> None:
    """Handle Create or Update request (CloudFormation custom-resource path)."""
    data = register_ga_endpoint(
        cluster_name=props["ClusterName"],
        region=props["Region"],
        endpoint_group_arn=props["EndpointGroupArn"],
        ingress_name=props.get("IngressName", "gco-ingress"),
        namespace=props.get("Namespace", "gco-system"),
        global_region=props.get("GlobalRegion", "us-east-2"),
        project_name=props.get("ProjectName", "gco"),
    )

    # IMPORTANT: Keep PhysicalResourceId stable to avoid CloudFormation treating
    # updates as replacements (which would trigger a Delete of the old resource)
    send_response(event, context, "SUCCESS", data, physical_id)


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    """Lambda handler for GA registration.

    Two entrypoints share this function:

    - **Step Functions task** (the convergence pipeline's final step): the event
      carries an ``Action`` key and is dispatched to :func:`handle_task`.
    - **CloudFormation custom resource** (legacy/fallback): the event carries a
      ``RequestType`` and the result is POSTed back to CloudFormation.
    """
    if event.get("Action"):
        logger.info(f"Task event: {json.dumps(event)}")
        return handle_task(event)

    logger.info(f"Event: {json.dumps(event)}")
    request_type = event["RequestType"]
    props = event["ResourceProperties"]
    physical_id = event.get("PhysicalResourceId", f"ga-reg-{props['ClusterName']}")

    try:
        if request_type == "Delete":
            handle_delete(event, context, props, physical_id)
        else:
            handle_create_update(event, context, props, physical_id)

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        if request_type == "Delete":
            # Always succeed on delete to avoid stack stuck in DELETE_FAILED
            send_response(event, context, "SUCCESS", {}, physical_id)
        else:
            send_response(event, context, "FAILED", {}, physical_id, str(e))
