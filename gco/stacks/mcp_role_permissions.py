"""What the MCP server role (``McpServerRole``) may do, derived from the default tools.

With ``GCO_MCP_ROLE_ARN`` set, the MCP server and every ``gco`` and
``kubectl`` process it starts run as this role. The role covers the tools the
server registers with no ``GCO_ENABLE_*`` flag (``safe`` and ``low-risk``
tags): the read-only AWS calls behind them, ``execute-api:Invoke`` on the GCO
APIs for everything the GCO API does on their behalf, and job submission to
the regional job queues. It cannot assume a role (so not CDK's bootstrap
roles), pass a role, or write a stack, an image, a model, a bucket object or
an inference endpoint record.

The gated families (deploy, destroy, build, push, upload, delete, capacity
purchase, cdk.json edits, Mission, Swarm) and the default tools listed in
:data:`DEFAULT_TOOLS_OUTSIDE_ROLE` need the operator's own credentials (unset
``GCO_MCP_ROLE_ARN``) or a role the operator extends. gco_mcp/README.md
documents the boundary; ``tests/test_mcp_iam_role.py`` keeps this module, the
regional stack and that document in step.

Kubernetes access is not IAM: the regional stack gives the role an EKS access
entry with ``AmazonEKSViewPolicy`` (read most namespaced objects, no Secrets,
no exec, port-forward or writes) and the :data:`MCP_KUBERNETES_GROUP` group,
which ``02-rbac.yaml`` binds to reading nodes and NodePools.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from aws_cdk import aws_iam as iam

#: Kubernetes group of the role's EKS access entry; ``02-rbac.yaml`` binds it.
MCP_KUBERNETES_GROUP = "gco:mcp-server-read"

#: EKS access policy the role's access entry carries, cluster-wide.
MCP_EKS_ACCESS_POLICY = "AmazonEKSViewPolicy"

#: Why the inference tools that change an endpoint are outside the role: they
#: write the endpoint's record in ``<project>-inference-endpoints`` directly
#: (the CLI's InferenceEndpointStore, not the GCO API), and the regional
#: monitors deploy whatever the record says.
_CREATES_ENDPOINT_RECORD = (
    "creates the endpoint's record in the inference endpoints table (DynamoDB PutItem)"
)
_UPDATES_ENDPOINT_RECORD = (
    "updates the endpoint's record in the inference endpoints table (DynamoDB UpdateItem)"
)

#: Default (ungated) MCP tools the role does not cover, and why. Each needs
#: the operator's credentials or an extended role.
DEFAULT_TOOLS_OUTSIDE_ROLE: Mapping[str, str] = {
    "setup_cluster_access": (
        "creates an EKS access entry for the caller with cluster-admin "
        "(AmazonEKSClusterAdminPolicy); the role has its own read-only entry"
    ),
    "monitoring_users_list": "reads the Grafana admin Secret and port-forwards to Grafana",
    "monitoring_user_add": "reads the Grafana admin Secret and port-forwards to Grafana",
    "list_storage_contents": "starts, execs into and deletes a helper pod",
    "metrics_from_shared_storage_file": "starts, execs into and deletes a helper pod",
    "analytics_users_list": "reads the Cognito users of the analytics user pool",
    "analytics_login_url": "signs in to the analytics user pool (AdminInitiateAuth)",
    "analytics_user_add": "creates a Cognito user",
    "cost_allocation_activate": "changes the account's cost allocation tag settings",
    "cost_k8s_namespaces": "queries Athena over the monitoring stack's cost report bucket",
    "cost_k8s_regions": "queries Athena over the monitoring stack's cost report bucket",
    "cost_k8s_trend": "queries Athena over the monitoring stack's cost report bucket",
    "cost_k8s_top": "queries Athena over the monitoring stack's cost report bucket",
    "images_init": "creates an ECR repository",
    "images_lifecycle_set": "writes an ECR lifecycle policy",
    "images_replication_sync": "writes the registry's ECR replication configuration",
    "deploy_inference": _CREATES_ENDPOINT_RECORD,
    "deploy_disaggregated_inference": _CREATES_ENDPOINT_RECORD,
    "scale_inference": _UPDATES_ENDPOINT_RECORD,
    "update_inference_image": _UPDATES_ENDPOINT_RECORD,
    "stop_inference": _UPDATES_ENDPOINT_RECORD,
    "start_inference": _UPDATES_ENDPOINT_RECORD,
    "add_inference_region": _UPDATES_ENDPOINT_RECORD,
    "remove_inference_region": _UPDATES_ENDPOINT_RECORD,
    "set_mooncake_topology": _UPDATES_ENDPOINT_RECORD,
    "configure_mooncake_store": _UPDATES_ENDPOINT_RECORD,
    "canary_deploy": _UPDATES_ENDPOINT_RECORD,
    "promote_canary": _UPDATES_ENDPOINT_RECORD,
    "rollback_canary": _UPDATES_ENDPOINT_RECORD,
}

#: Read-only actions with no resource-level permissions (or, for the listing
#: calls, none narrower than every resource of the type), by the tools that
#: call them. Resource ``*``.
UNSCOPED_READ_ACTIONS: tuple[str, ...] = (
    # Stack discovery's all-Region fallback, CDK synth's zone lookups,
    # capacity and HyperPod zone checks.
    "ec2:DescribeRegions",
    "ec2:DescribeAvailabilityZones",
    # Capacity tools (check, recommend, spot prices, reservations, blocks,
    # instance info), nodepools create-odcr / create-capacity-block, PCS
    # node counts behind recommend_region.
    "ec2:DescribeInstanceTypes",
    "ec2:DescribeInstanceTypeOfferings",
    "ec2:DescribeSpotPriceHistory",
    "ec2:GetSpotPlacementScores",
    "ec2:DescribeCapacityReservations",
    "ec2:DescribeCapacityBlockOfferings",
    "ec2:DescribeInstances",
    "pricing:GetProducts",
    # find_training_plans, list_training_plans, hyperpod_preflight (which
    # reads AWS's default quota for an instance type with none applied).
    "sagemaker:ListClusters",
    "sagemaker:ListTrainingPlans",
    "sagemaker:SearchTrainingPlanOfferings",
    "servicequotas:ListServiceQuotas",
    "servicequotas:ListAWSDefaultServiceQuotas",
    # cost_summary, cost_by_region, cost_trend, cost_forecast,
    # cost_allocation_status, fleet_status.
    "ce:GetCostAndUsage",
    "ce:GetCostForecast",
    "ce:ListCostAllocationTags",
    "ce:ListCostAllocationTagBackfillHistory",
    # metrics_cloudwatch_get, capacity_status.
    "cloudwatch:GetMetricData",
    "cloudwatch:GetMetricStatistics",
    "cloudwatch:ListMetrics",
    # get_job_logs' CloudWatch Logs Insights fallback (StartQuery is scoped).
    "logs:GetQueryResults",
    # images_list (an unfiltered listing), images_replication_get.
    "ecr:DescribeRepositories",
    "ecr:DescribeRegistry",
    # list_file_systems, files_get, files_access_points, analytics_doctor.
    "elasticfilesystem:DescribeFileSystems",
    "elasticfilesystem:DescribeAccessPoints",
    "elasticfilesystem:DescribeMountTargets",
    "elasticfilesystem:DescribeTags",
    "elasticfilesystem:ListTagsForResource",
    "fsx:DescribeFileSystems",
    # analytics_status / analytics_doctor: user pool metadata, no users.
    "cognito-idp:ListUserPools",
)


@dataclass(frozen=True)
class McpRoleScope:
    """The deployment facts the role's resource ARNs are built from.

    ``api_regions`` are the global API's Region and every workload Region
    (one regional API bridge each); ``workload_regions`` own the job queues.
    ``model_bucket_arn`` is the model weights bucket resolved from SSM.
    """

    partition: str
    account: str
    project_name: str
    global_region: str
    api_regions: tuple[str, ...]
    workload_regions: tuple[str, ...]
    model_bucket_arn: str


def _arn(
    scope: McpRoleScope, service: str, region: str, resource: str, *, account: bool = True
) -> str:
    owner = scope.account if account else ""
    return f"arn:{scope.partition}:{service}:{region}:{owner}:{resource}"


def mcp_server_role_statements(scope: McpRoleScope) -> list[iam.PolicyStatement]:
    """Every statement of the MCP server role, in a stable order."""
    project = scope.project_name
    api_arns = [
        _arn(scope, "execute-api", region, f"*/prod/*/{path}")
        for region in scope.api_regions
        for path in ("api/v1/*", "inference/*")
    ]
    job_queues = [
        _arn(scope, "sqs", region, f"{project}-jobs-{region}") for region in scope.workload_regions
    ]
    dead_letter_queues = [
        _arn(scope, "sqs", region, f"{project}-jobs-dlq-{region}")
        for region in scope.workload_regions
    ]
    log_group = f"log-group:/aws/containerinsights/{project}-*/application"
    capacity_table = _arn(
        scope, "dynamodb", scope.global_region, f"table/{project}-capacity-history"
    )
    inference_endpoints_table = _arn(
        scope, "dynamodb", scope.global_region, f"table/{project}-inference-endpoints"
    )
    return [
        # Jobs, queue, templates, webhooks, inference, Slurm, DAG runs, cost
        # reports and health go through the GCO API, which authorizes and
        # validates each request itself.
        iam.PolicyStatement(actions=["execute-api:Invoke"], resources=api_arns),
        # Stack discovery and outputs, storage inventory, stacks status,
        # stack_diff's deployed template.
        iam.PolicyStatement(
            actions=[
                "cloudformation:DescribeStacks",
                "cloudformation:ListStackResources",
                "cloudformation:GetTemplate",
            ],
            resources=[_arn(scope, "cloudformation", "*", f"stack/{project}-*/*")],
        ),
        # Bucket identities, the model bucket name, add-on status, the
        # HyperPod agent image (an AWS public parameter tree).
        iam.PolicyStatement(
            actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
            resources=[
                _arn(scope, "ssm", "*", f"parameter/{project}/*"),
                _arn(
                    scope,
                    "ssm",
                    "*",
                    "parameter/aws/service/sagemaker-hyperpod/*",
                    account=False,
                ),
            ],
        ),
        # Kubeconfig and tunnel plans, endpoint drift, EKS Capabilities status.
        iam.PolicyStatement(
            actions=["eks:DescribeCluster", "eks:ListCapabilities", "eks:DescribeCapability"],
            resources=[
                _arn(scope, "eks", "*", f"cluster/{project}-*"),
                _arn(scope, "eks", "*", f"capability/{project}-*/*"),
            ],
        ),
        # submit_job_sqs; queue_status and capacity_status read the depths.
        iam.PolicyStatement(
            actions=["sqs:SendMessage", "sqs:GetQueueAttributes"], resources=job_queues
        ),
        iam.PolicyStatement(actions=["sqs:GetQueueAttributes"], resources=dead_letter_queues),
        iam.PolicyStatement(
            actions=["logs:StartQuery"],
            resources=[
                _arn(scope, "logs", "*", log_group),
                _arn(scope, "logs", "*", f"{log_group}:*"),
            ],
        ),
        # list_models; no default tool reads or writes model objects.
        iam.PolicyStatement(actions=["s3:ListBucket"], resources=[scope.model_bucket_arn]),
        iam.PolicyStatement(
            actions=["dynamodb:Query"], resources=[capacity_table, f"{capacity_table}/index/*"]
        ),
        # images_orphans reads every inference endpoint record for the images
        # they name (and fails when it cannot); list_inference_endpoints and
        # fleet_status make the same scan. inference_status,
        # mooncake_topology_status, invoke_inference, chat_inference,
        # inference_health, list_endpoint_models and the
        # gco://inference/{endpoint_name} resource read one record. The table
        # has no index.
        iam.PolicyStatement(
            actions=["dynamodb:GetItem", "dynamodb:Scan"], resources=[inference_endpoints_table]
        ),
        # images_tags, images_describe, images_orphans, images_mirror_*,
        # images_replication_status, images_lifecycle_get.
        iam.PolicyStatement(
            actions=[
                "ecr:DescribeImages",
                "ecr:GetLifecyclePolicy",
                "ecr:DescribeImageReplicationStatus",
            ],
            resources=[_arn(scope, "ecr", "*", f"repository/{project}/*")],
        ),
        # hyperpod_status, hyperpod_preflight, recommend_region's backends.
        # HyperPod and PCS clusters are addressed by generated ids.
        iam.PolicyStatement(
            actions=[
                "sagemaker:DescribeCluster",
                "sagemaker:ListTags",
                "sagemaker:DescribeTrainingPlan",
                "pcs:GetCluster",
                "pcs:ListComputeNodeGroups",
                "pcs:GetComputeNodeGroup",
            ],
            resources=[
                _arn(scope, "sagemaker", "*", "cluster/*"),
                _arn(scope, "sagemaker", "*", "training-plan/*"),
                _arn(scope, "pcs", "*", "cluster/*"),
            ],
        ),
        # ai_recommend and capacity_predict ask a Bedrock model.
        iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                _arn(scope, "bedrock", "*", "foundation-model/*", account=False),
                _arn(scope, "bedrock", "*", "inference-profile/*"),
            ],
        ),
        iam.PolicyStatement(
            actions=["cognito-idp:DescribeUserPool"],
            resources=[_arn(scope, "cognito-idp", "*", "userpool/*")],
        ),
        iam.PolicyStatement(actions=list(UNSCOPED_READ_ACTIONS), resources=["*"]),
    ]


def wildcard_resources(statements: Iterable[iam.PolicyStatement]) -> list[str]:
    """The ``Resource::`` cdk-nag details (IAM5) of every wildcard resource, deduplicated."""
    details: dict[str, None] = {}
    for statement in statements:
        for resource in statement.resources:
            if "*" in resource:
                details[f"Resource::{resource}"] = None
    return list(details)
