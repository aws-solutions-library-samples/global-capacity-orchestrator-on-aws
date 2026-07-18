"""
GA Registration/Deregistration Lambda Handler.

This Lambda owns both sides of the Ingress-created ALB's lifecycle with the
shared Global Accelerator endpoint group. This is necessary because the ALB is
created by the AWS Load Balancer Controller (not CDK), so CloudFormation can't
directly reference its ARN.

Registration (deploy time):
    1. Waits for the ALB to be created and become active
    2. Uses multiple detection methods (tags, Ingress status, name prefix)
    3. Registers that ALB with Global Accelerator
    4. Stores the ALB hostname in SSM for verified regional-proxy resolution
    5. Handles idempotency (won't fail if ALB already registered)

Deregistration (destroy time — issue #130):
    Registration is one-directional, so without a teardown hook the endpoint
    group keeps referencing the (LB-controller-deleted) ALB and Global
    Accelerator keeps its `global_accelerator_managed` ENIs pinned in the VPC
    subnets used by the ALB — blocking subnet deletion and leaving the stack in
    DELETE_FAILED. On delete this Lambda removes the endpoint(s) from the group
    and waits for the accelerator to redeploy (return to DEPLOYED), which is
    when Global Accelerator releases those managed ENIs.

Entrypoints (all share this module's helpers):
    - `handle_task`: the convergence Step Functions state machine's final task
      (register). Dispatched by `lambda_handler` when the event carries an
      `Action` key.
    - `lambda_handler`: legacy CloudFormation custom-resource path. Create/Update
      register (`handle_create_update`); Delete deregisters and waits
      (`handle_delete`). Responds via the CloudFormation ResponseURL protocol.
    - `on_delete_event`: the delete-time deregistration guard, invoked through a
      CDK `cr.Provider`. A no-op on Create/Update (registration stays owned by
      the state machine); on Delete it deregisters, waits for the accelerator
      to release its ENIs, and removes the region's SSM registry entry.

SSM Parameter Storage:
    The ALB hostname is stored in SSM Parameter Store at:
    /{project_name}/alb-hostname-{region}

    The VPC-attached regional API proxy uses this registry to resolve and then
    verify its regional internal ALB. The centralized aggregator does not read
    this parameter; it discovers regional API Gateway outputs through
    CloudFormation and invokes those bridges with SigV4.

Environment Variables (from CloudFormation properties):
    ClusterName: EKS cluster name
    Region: AWS region for this cluster
    EndpointGroupArn: Global Accelerator endpoint group ARN
    IngressName: Kubernetes Ingress name (default: gco-ingress)
    Namespace: Kubernetes namespace (default: gco-system)
    RegistryRegion: Region that owns the SSM endpoint registry
    GlobalRegion: Backwards-compatible alias for RegistryRegion
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
# Generated at (UTC): 2026-07-18T01:03:40Z
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

# Global Accelerator redeploy budget for the delete-time deregistration guard.
# After endpoints are removed, GA transitions the accelerator IN_PROGRESS ->
# DEPLOYED and only releases its `global_accelerator_managed` ENIs once it is
# back to DEPLOYED. Teardown must wait for that release before CloudFormation
# deletes the ALB subnets those ENIs occupy (see issue #130). Kept under the
# 14-minute ceiling recommended for cr.Provider onEvent handlers (all framework
# functions time out at 15 minutes).
GA_DEPLOYED_WAIT_SECONDS = 720  # 12 minutes waiting for the accelerator to redeploy
GA_DEPLOYED_POLL_INTERVAL = 15  # Poll accelerator status every 15 seconds
DEFAULT_REGISTRY_REGION = "us-east-2"


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
    """Find the internal platform ALB by DNS hostname.

    Given a hostname from the Ingress status, look up the matching internal
    application load balancer in the ELBv2 API. The Ingress status is the
    source of truth for which load balancer the controller created, while the
    type and scheme checks fail closed against NLBs and internet-facing ALBs.

    Returns:
        Tuple of (dns_name, arn, state) or (None, None, None) if not found
    """
    try:
        albs = elb_client.describe_load_balancers()["LoadBalancers"]
        for alb in albs:
            if (
                alb.get("Type") == "application"
                and alb.get("Scheme") == "internal"
                and alb.get("DNSName") == hostname
            ):
                state = alb.get("State", {}).get("Code", "unknown")
                logger.info(f"Found ALB by hostname: {alb['LoadBalancerName']} (state: {state})")
                return alb["DNSName"], alb["LoadBalancerArn"], state
    except Exception as e:
        logger.warning(f"Error finding ALB by hostname: {e}")
    return None, None, None


def find_platform_alb_by_tags(
    elb_client: Any, cluster_name: str
) -> tuple[str | None, str | None, str | None]:
    """Find the internal platform ALB by tags when Ingress status is empty.

    Only matches internal ALBs that belong to the platform ingress group.
    Explicitly excludes inference ALBs, internet-facing ALBs, and non-ALB load
    balancers.

    The platform ALB is identified by:
    - Type: application (not network or gateway)
    - Scheme: internal (never internet-facing)
    - Cluster tag matching
    - Ingress stack tag that does NOT contain 'inference'

    Returns:
        Tuple of (dns_name, arn, state) or (None, None, None) if not found
    """
    try:
        all_lbs = elb_client.describe_load_balancers()["LoadBalancers"]
        # CRITICAL: Only internal ALBs are valid. NLBs (Slurm, etc.) and
        # internet-facing ALBs must never be registered.
        albs = [
            alb
            for alb in all_lbs
            if alb.get("Type") == "application" and alb.get("Scheme") == "internal"
        ]
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

            # Accept only the platform Ingress ownership tags emitted by EKS
            # Auto Mode or the self-managed AWS Load Balancer Controller. Check
            # each key independently so an unrelated tag cannot mask a valid
            # ownership marker on the other controller's key.
            auto_stack = tags.get("ingress.eks.amazonaws.com/stack")
            controller_stack = tags.get("ingress.k8s.aws/stack")
            stack_match = auto_stack == "gco" or controller_stack == "gco-system/gco-ingress"
            if not stack_match:
                logger.debug(
                    f"Skipping ALB without the GCO platform ingress tag: {alb['LoadBalancerName']}"
                )
                continue
            stack = "gco" if auto_stack == "gco" else "gco-system/gco-ingress"

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


def ensure_https_health_check(
    ga_client: Any,
    endpoint_group_arn: str,
    health_check_path: str = "/api/v1/health",
) -> None:
    """Keep the endpoint-group health contract aligned with HTTPS-only ALBs.

    Global Accelerator derives Application Load Balancer endpoint health from
    the ALB target groups, so these probe settings are not used while the
    endpoint remains an ALB. Enforcing HTTPS/443 here still prevents an
    accidental plaintext fallback if the endpoint type changes later and
    repairs endpoint groups created by older releases.
    """
    try:
        endpoint_group = ga_client.describe_endpoint_group(EndpointGroupArn=endpoint_group_arn)
        eg = endpoint_group.get("EndpointGroup", {})
        current_protocol = eg.get("HealthCheckProtocol", "TCP")
        current_port = int(eg.get("HealthCheckPort", 0))
        current_path = eg.get("HealthCheckPath", "")

        if (
            current_protocol == "HTTPS"
            and current_port == 443
            and current_path == health_check_path
        ):
            logger.info(f"Health check already configured: HTTPS 443 {health_check_path}")
            return

        logger.info(
            "Updating health check from %s/%s to HTTPS/443 %s",
            current_protocol,
            current_port,
            health_check_path,
        )

        # Preserve existing endpoints when updating the endpoint group.
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
            HealthCheckPort=443,
            HealthCheckProtocol="HTTPS",
            HealthCheckPath=health_check_path,
            HealthCheckIntervalSeconds=30,
            ThresholdCount=3,
            EndpointConfigurations=existing_endpoints,
        )
        logger.info("Health check contract updated to HTTPS/443 successfully")
    except ClientError as e:
        logger.warning(f"Failed to update health check configuration: {e}")
        # Non-fatal: ALB target-group health remains authoritative for GA.


def _get_registry_region(properties: dict[str, Any], default: str | None = None) -> str | None:
    """Return the configured SSM registry region.

    ``RegistryRegion`` names the setting by its purpose. ``GlobalRegion`` is
    accepted for backwards compatibility with existing state-machine and
    legacy custom-resource payloads.
    """
    value = properties.get("RegistryRegion") or properties.get("GlobalRegion") or default
    return str(value) if value else None


def store_alb_hostname_in_ssm(
    region: str, alb_hostname: str, registry_region: str, project_name: str
) -> None:
    """Store the ALB hostname for verified regional-proxy resolution.

    The parameter lives in the global registry region. Regional VPC proxies
    read it, then independently verify ALB ownership and tags before routing.
    Global aggregation discovers regional API Gateway stacks instead.
    """
    ssm_client = boto3.client("ssm", region_name=registry_region)
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


def delete_alb_hostname_from_ssm(region: str, registry_region: str, project_name: str) -> None:
    """Delete ALB hostname from the configured SSM registry during cleanup."""
    ssm_client = boto3.client("ssm", region_name=registry_region)
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


def _accelerator_arn_from_endpoint_group(endpoint_group_arn: str) -> str:
    """Derive the accelerator ARN from one of its endpoint-group ARNs.

    Endpoint-group ARN:
        arn:aws:globalaccelerator::<acct>:accelerator/<id>/listener/<lid>/endpoint-group/<egid>
    Accelerator ARN:
        arn:aws:globalaccelerator::<acct>:accelerator/<id>
    """
    return endpoint_group_arn.split("/listener/")[0]


def wait_for_accelerator_deployed(
    ga_client: Any,
    endpoint_group_arn: str,
    timeout_seconds: int = GA_DEPLOYED_WAIT_SECONDS,
) -> bool:
    """Block until the accelerator reports ``DEPLOYED`` (or the budget expires).

    Global Accelerator only releases its ``global_accelerator_managed`` ENIs
    from a region's subnets once the accelerator finishes redeploying after an
    endpoint change (it reports ``IN_PROGRESS`` until then). During teardown we
    must wait for that release before CloudFormation deletes the ALB subnets
    those ENIs occupy — otherwise subnet deletion fails and the stack is left in
    DELETE_FAILED (see issue #130).

    Best-effort: returns ``True`` once ``DEPLOYED`` is observed (or the
    accelerator is already gone), ``False`` on timeout or a non-fatal API error.
    Never raises, so a Global Accelerator hiccup can't wedge the stack delete.
    """
    accelerator_arn = _accelerator_arn_from_endpoint_group(endpoint_group_arn)
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        try:
            accelerator = ga_client.describe_accelerator(AcceleratorArn=accelerator_arn)
            status = accelerator.get("Accelerator", {}).get("Status", "")
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "")
            if error_code == "AcceleratorNotFoundException":
                logger.info("Accelerator no longer exists; nothing to wait for")
                return True
            logger.warning(f"Failed to describe accelerator status: {e}")
            return False
        if status == "DEPLOYED":
            elapsed = int(time.time() - start_time)
            logger.info(f"Accelerator DEPLOYED after {elapsed}s; GA released its managed ENIs")
            return True
        logger.info(f"Accelerator status={status!r}; waiting for DEPLOYED...")
        # nosemgrep: arbitrary-sleep - intentional polling for GA redeploy
        time.sleep(GA_DEPLOYED_POLL_INTERVAL)

    logger.warning(
        f"Timed out after {timeout_seconds}s waiting for the accelerator to reach DEPLOYED"
    )
    return False


def deregister_alb_from_ga(ga_client: Any, endpoint_group_arn: str) -> None:
    """Remove every endpoint from the GA endpoint group and wait for GA to
    release its managed ENIs.

    Used by the delete-time teardown guard: registration is one-directional
    (the convergence state machine's final task adds the Ingress-created ALB at
    deploy time), so without this the endpoint group keeps referencing the
    LB-controller-deleted ALB and Global Accelerator keeps
    ``global_accelerator_managed`` ENIs pinned in the VPC subnets used by the
    ALB, blocking subnet — and stack — deletion (see issue #130).
    """
    remove_ga_endpoints(ga_client, endpoint_group_arn)
    wait_for_accelerator_deployed(ga_client, endpoint_group_arn)


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
    registry_region = _get_registry_region(props, DEFAULT_REGISTRY_REGION)
    assert registry_region is not None
    project_name = props.get("ProjectName", "gco")

    # Step 1: Remove all endpoints from GA endpoint group and wait for Global
    # Accelerator to release its managed ENIs, so the ALB subnets those ENIs
    # occupy can be deleted cleanly during teardown (see issue #130).
    ga = boto3.client("globalaccelerator", region_name="us-west-2")
    deregister_alb_from_ga(ga, endpoint_group_arn)

    # Step 2: Delete the Ingress to trigger ALB deletion
    delete_ingress_and_wait_for_alb_deletion(cluster_name, region, namespace, ingress_name)

    # Step 3: Clean up ALB hostname from SSM
    delete_alb_hostname_from_ssm(region, registry_region, project_name)

    send_response(event, context, "SUCCESS", {}, physical_id)


def register_ga_endpoint(
    cluster_name: str,
    region: str,
    endpoint_group_arn: str,
    ingress_name: str = "gco-ingress",
    namespace: str = "gco-system",
    registry_region: str = DEFAULT_REGISTRY_REGION,
    project_name: str = "gco",
) -> dict[str, str]:
    """Find the active platform ALB and register it with Global Accelerator.

    Shared core for both entrypoints (the Step Functions convergence task and
    the legacy CloudFormation custom resource). Polls for the active ALB,
    registers it (idempotent), scrubs stale endpoints, enforces the HTTPS/443
    health contract, and records the ALB hostname in SSM. Raises on timeout so the
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

    # Keep older endpoint groups aligned with the HTTPS-only backend contract.
    ensure_https_health_check(ga, endpoint_group_arn)

    # Publish the ALB hostname for verified regional VPC-proxy resolution.
    store_alb_hostname_in_ssm(region, alb_hostname, registry_region, project_name)

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
        registry_region=_get_registry_region(event, DEFAULT_REGISTRY_REGION)
        or DEFAULT_REGISTRY_REGION,
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
        registry_region=_get_registry_region(props, DEFAULT_REGISTRY_REGION)
        or DEFAULT_REGISTRY_REGION,
        project_name=props.get("ProjectName", "gco"),
    )

    # IMPORTANT: Keep PhysicalResourceId stable to avoid CloudFormation treating
    # updates as replacements (which would trigger a Delete of the old resource)
    send_response(event, context, "SUCCESS", data, physical_id)


def on_delete_event(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """CDK ``cr.Provider`` handler for the delete-time GA deregistration guard.

    Wired by ``regional_stack.py`` as a standalone custom resource whose sole
    job is teardown ordering. It is a **no-op on Create/Update** — registration
    stays owned by the convergence state machine's final task — and on **Delete**
    it deregisters the region's ALB from the shared Global Accelerator endpoint
    group, waits for GA to release its managed ENIs, and removes the region's
    SSM endpoint-registry entry before the stack disappears.

    Without this hook the endpoint group keeps referencing the
    LB-controller-deleted ALB, Global Accelerator keeps
    ``global_accelerator_managed`` ENIs pinned in the ALB subnets, subnet
    deletion fails, and the stack is left in DELETE_FAILED (see issue #130).

    Unlike the raw CloudFormation custom-resource path (:func:`lambda_handler`),
    this is invoked through the provider framework, so it returns a plain dict
    instead of POSTing to a response URL. Best-effort: it never raises, so a
    transient Global Accelerator error can't wedge the stack in DELETE_FAILED.
    """
    request_type = event.get("RequestType")
    props = event.get("ResourceProperties", {})
    physical_id = event.get("PhysicalResourceId") or f"ga-dereg-{props.get('Region', 'unknown')}"

    # Registration happens in the state machine; this guard only acts on Delete.
    if request_type != "Delete":
        return {"PhysicalResourceId": physical_id}

    endpoint_group_arn = props.get("EndpointGroupArn")
    if endpoint_group_arn:
        logger.info(f"Deregistering region's ALB from Global Accelerator: {endpoint_group_arn}")
        try:
            ga = boto3.client("globalaccelerator", region_name="us-west-2")
            deregister_alb_from_ga(ga, endpoint_group_arn)
        except Exception as e:  # noqa: BLE001 - teardown guard must never wedge the stack delete
            logger.error(
                f"GA deregistration guard failed (continuing teardown): {e}", exc_info=True
            )
    else:
        logger.warning("No EndpointGroupArn in resource properties; skipping GA deregistration")

    # The endpoint registry is independent of the GA endpoint group. Always
    # remove this region's hostname on Delete when the registry location was
    # supplied, even if GA deregistration was skipped or failed.
    region = props.get("Region")
    registry_region = _get_registry_region(props)
    project_name = props.get("ProjectName", "gco")
    if not region:
        logger.warning("No Region in resource properties; skipping SSM registry cleanup")
    elif not registry_region:
        logger.warning(
            "No RegistryRegion (or legacy GlobalRegion) in resource properties; "
            "skipping SSM registry cleanup"
        )
    else:
        try:
            delete_alb_hostname_from_ssm(str(region), registry_region, str(project_name))
        except Exception as e:  # noqa: BLE001 - teardown guard must never wedge the stack delete
            logger.error(f"SSM registry cleanup failed (continuing teardown): {e}", exc_info=True)

    return {"PhysicalResourceId": physical_id}


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
