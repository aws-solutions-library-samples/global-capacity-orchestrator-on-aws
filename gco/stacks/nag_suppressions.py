"""CDK-nag suppression utilities for GCO stacks.

This module provides centralized suppression management for cdk-nag rules
that are intentionally not applicable or have documented justifications.

Supported Compliance Frameworks:
- AWS Solutions: Best practices for AWS architectures
- HIPAA Security: Healthcare compliance requirements
- NIST 800-53 Rev 5: Federal security controls
- PCI DSS 3.2.1: Payment card industry standards
- Serverless: Best practices for serverless architectures

Suppression Categories:
1. AWS Managed Policies - Required for EKS/Lambda integrations
2. Inline Policies - CDK-generated for custom resources
3. Wildcard Permissions - Required for dynamic resource access
4. Infrastructure Patterns - Intentional architectural decisions
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aws_cdk import (
    IPolicyValidationPlugin,
    Stack,
    Token,
    Validations,
)
from cdk_nag import (
    AwsSolutionsChecks,
    HIPAASecurityChecks,
    NIST80053R5Checks,
    PCIDSS321Checks,
    ServerlessChecks,
)
from constructs import IConstruct

# ---------------------------------------------------------------------------
# cdk-nag v3 acknowledgment mechanism
# ---------------------------------------------------------------------------
# cdk-nag v3 rewrote its engine from an ``IAspect`` to an
# ``IPolicyValidationPlugin`` (CDK's native policy-validation framework). The
# old ``NagSuppressions.add_(resource|stack)_suppressions`` /
# ``NagPackSuppression`` API is gone. Suppressions are now *acknowledgments*
# recorded as construct metadata under a well-known key; cdk-nag's
# ``isAcknowledged`` walks a construct's ancestor tree looking for that key, so
# an acknowledgment placed on a stack (or a role) covers every matching finding
# on that construct **and all of its descendants** — which is why there is no
# ``apply_to_children`` flag anymore (it is always effectively ``True``).
#
# Finding ids come in two shapes:
#   * scalar rules  -> the bare rule id, e.g. ``AwsSolutions-EC23``
#   * array rules   -> ``<rule>[<detail>]``, e.g.
#                      ``AwsSolutions-IAM5[Resource::*]`` — matched EXACTLY
#                      (there is no bare-id fallback for these, and no regex
#                      matching either: every detail must be spelled out to the
#                      exact string cdk-nag emits, including any synthesis-time
#                      logical-id hash such as
#                      ``Resource::<RegionalSharedBucket3FF19783.Arn>/*``).
#
# We record acknowledgments by writing the metadata key ourselves via
# ``node.add_metadata`` rather than calling ``Validations.of(x).acknowledge``.
# Every detail we scope starts with ``Resource::`` / ``Policy::`` /
# ``Action::`` / ``Condition::`` — all contain ``::``, and ``acknowledge()``
# rejects any id containing ``::`` with ``InvalidValidationId``
# (https://github.com/cdklabs/cdk-nag/issues/2351). Writing the metadata key
# directly is the documented workaround and applies uniformly to every
# suppression, scalar or array.

# The construct-metadata key cdk-nag v3 reads for acknowledged findings.
_ACK_METADATA_KEY: str = Validations.ACKNOWLEDGED_RULES_METADATA_KEY


@dataclass(frozen=True)
class NagSuppression:
    """A single cdk-nag v3 finding acknowledgment.

    Args:
        id: The rule id to acknowledge (e.g. ``"AwsSolutions-IAM5"``).
        reason: Human-readable justification recorded alongside the
            acknowledgment.
        applies_to: Optional finding *details* to scope the acknowledgment.
            Each entry is matched verbatim against the ``<rule>[<detail>]``
            finding id cdk-nag emits, so it must be the exact string —
            including any synthesis-time logical-id hash (e.g.
            ``Resource::<RegionalSharedBucket3FF19783.Arn>/*``). When empty,
            the bare rule id is acknowledged (correct for scalar rules that
            emit no ``[detail]`` suffix, such as ``AwsSolutions-EC23``).
    """

    id: str
    reason: str
    applies_to: Sequence[str] = field(default_factory=tuple)


def _normalize(supp: NagSuppression | Mapping[str, Any]) -> tuple[str, str, list[str]]:
    """Normalize a suppression to ``(rule_id, reason, details)``.

    Accepts either a :class:`NagSuppression` or a plain mapping with ``id`` /
    ``reason`` / ``applies_to`` (or the jsii spelling ``appliesTo`` used by the
    resource-scoped call sites). Every ``applies_to`` entry must be an exact
    string — cdk-nag v3 matches finding details verbatim (there is no regex
    support).
    """
    if isinstance(supp, NagSuppression):
        rule_id, reason, entries = supp.id, supp.reason, list(supp.applies_to)
    else:
        rule_id = supp["id"]
        reason = supp["reason"]
        entries = list(supp.get("applies_to") or supp.get("appliesTo") or [])

    for entry in entries:
        if not isinstance(entry, str):
            raise ValueError(
                f"Unsupported applies_to entry for {rule_id!r}: {entry!r} "
                "(expected an exact detail string; cdk-nag v3 has no regex support)"
            )
    return rule_id, reason, entries


def acknowledge_nag_findings(
    scope: IConstruct,
    suppressions: Sequence[NagSuppression | Mapping[str, Any]],
) -> None:
    """Record cdk-nag v3 acknowledgments on ``scope`` (covers its descendants).

    This is GCO's v3-native replacement for the removed
    ``NagSuppressions.add_resource_suppressions`` /
    ``add_stack_suppressions``. Each finding id (``<rule>[<detail>]`` for
    scoped details, or the bare ``<rule>`` when ``applies_to`` is empty) is
    written to the cdk-nag acknowledgment metadata key, which every rule pack
    honors natively. Because cdk-nag walks the ancestor tree, an
    acknowledgment on a stack covers all resources in that stack, and one on a
    role covers the role's generated policies.
    """
    # cdk-nag renders the CloudFormation partition/account/region
    # pseudo-parameters in a finding's detail two different ways, and which one
    # appears is decided per ARN, not per stack:
    #   * an ARN built from ``Aws.PARTITION`` / ``Aws.ACCOUNT_ID`` /
    #     ``Aws.REGION`` may retain the angle-bracket pseudo-param literal,
    #     whereas
    #   * an ARN hand-built from ``stack.partition`` / ``stack.account`` /
    #     ``stack.region`` renders concrete values when CDK can resolve them.
    # So for a concrete env we can't know from here which form cdk-nag will emit
    # for any given finding. Register the acknowledgment under both the
    # placeholder detail and every resolvable literal rendering. Extra keys
    # that match no finding are harmless; whichever exact form cdk-nag emits
    # then has a matching acknowledgment. Any detail built from a raw token is
    # normalized first because unresolved tokens cannot be metadata-map keys.
    stack = Stack.of(scope)
    dimensions = (
        (stack.partition, "<AWS::Partition>"),
        (stack.account, "<AWS::AccountId>"),
        (stack.region, "<AWS::Region>"),
    )

    def _keys_for(rule_id: str, detail: str) -> list[str]:
        for value, placeholder in dimensions:
            if Token.is_unresolved(value):
                detail = detail.replace(value, placeholder)
        if Token.is_unresolved(detail):
            raise ValueError(
                "cdk-nag acknowledgment detail contains an unresolved token and "
                f"cannot be used as a metadata key: {detail!r}. Use cdk-nag's "
                "literal rendering (e.g. '<AWS::Region>', '<LogicalId.Arn>') instead."
            )
        # Expand each placeholder into every concrete environment dimension
        # CDK resolved, producing the exact partition/account/region combinations
        # cdk-nag can render for the same policy resource.
        variants = {detail}
        for value, placeholder in dimensions:
            if not Token.is_unresolved(value):
                variants.update(v.replace(placeholder, value) for v in list(variants))
        return [f"{rule_id}[{v}]" for v in variants]

    ack: dict[str, str] = {}
    for supp in suppressions:
        rule_id, reason, details = _normalize(supp)
        if not details:
            ack[rule_id] = reason
        for detail in details:
            for key in _keys_for(rule_id, detail):
                ack[key] = reason
    if ack:
        scope.node.add_metadata(_ACK_METADATA_KEY, ack)


# The cdk-nag rules that evaluate security-group *ingress CIDRs*. When an
# ingress rule's CIDR is a CloudFormation token (e.g. a VPC CIDR resolved via
# ``Fn::GetAtt``), these rules cannot resolve it to a primitive value and
# *throw*. cdk-nag v3 surfaces a thrown rule under its bare id (the v2
# ``CdkNagValidationFailure`` aggregate rule is gone), so a single unresolvable
# ingress rule produces one bare finding per rule below. Every GCO security
# group whose ingress is pinned to its own VPC CIDR trips this exact set.
_SECURITY_GROUP_CIDR_RULES: tuple[str, ...] = (
    "AwsSolutions-EC23",
    "HIPAA.Security-EC2RestrictedCommonPorts",
    "HIPAA.Security-EC2RestrictedSSH",
    "NIST.800.53.R5-EC2RestrictedCommonPorts",
    "NIST.800.53.R5-EC2RestrictedSSH",
    "PCI.DSS.321-EC2RestrictedCommonPorts",
    "PCI.DSS.321-EC2RestrictedSSH",
)


def acknowledge_security_group_cidr_findings(scope: IConstruct, *, reason: str) -> None:
    """Acknowledge the SG-ingress rules that throw on a token (VPC CIDR) source.

    Scope this to the specific security-group-bearing construct — an EKS
    cluster, a VPC whose interface endpoints carry security groups, or a
    standalone ``SecurityGroup`` — rather than the whole stack, so the bare-id
    acknowledgment cannot mask a genuine open-ingress finding elsewhere.
    cdk-nag walks the ancestor tree, so the acknowledgment covers every ingress
    rule under ``scope``.
    """
    acknowledge_nag_findings(
        scope,
        [NagSuppression(id=rule, reason=reason) for rule in _SECURITY_GROUP_CIDR_RULES],
    )


#: The rules that require SSE-KMS default encryption on every S3 bucket.
_S3_DEFAULT_ENCRYPTION_KMS_RULES: tuple[str, ...] = (
    "HIPAA.Security-S3DefaultEncryptionKMS",
    "NIST.800.53.R5-S3DefaultEncryptionKMS",
    "PCI.DSS.321-S3DefaultEncryptionKMS",
)

#: Why a bucket that receives S3 server access logs uses SSE-S3.
ACCESS_LOG_DESTINATION_SSE_S3_REASON = (
    "This bucket is an S3 server access log destination. The S3 User Guide "
    "(Enabling Amazon S3 server access logging) requires a destination bucket "
    "to use SSE-S3: with SSE-KMS default encryption, log objects might be "
    "delivered encrypted with a key the bucket owner cannot access. The bucket "
    "whose requests it logs is SSE-KMS."
)


def acknowledge_access_log_destination_encryption(bucket: IConstruct) -> None:
    """Acknowledge the SSE-KMS rules on an S3 server access log destination bucket.

    Server access logging requires an SSE-S3 destination, so the
    ``S3DefaultEncryptionKMS`` rules cannot be met there. Scope this to the
    destination bucket, never to a stack.
    """
    acknowledge_nag_findings(
        bucket,
        [
            NagSuppression(id=rule, reason=ACCESS_LOG_DESTINATION_SSE_S3_REASON)
            for rule in _S3_DEFAULT_ENCRYPTION_KMS_RULES
        ],
    )


def nag_validation_plugins(
    scope: IConstruct, *, verbose: bool = True
) -> list[IPolicyValidationPlugin]:
    """Return the five GCO cdk-nag rule packs as v3 policy-validation plugins.

    Register with ``Validations.of(app).add_plugins(*nag_validation_plugins(app))``.
    Each pack reads the acknowledgment metadata written by
    :func:`acknowledge_nag_findings` natively, so the packs run directly with
    no wrapping.
    """
    return [
        AwsSolutionsChecks(scope, verbose=verbose),
        HIPAASecurityChecks(scope, verbose=verbose),
        NIST80053R5Checks(scope, verbose=verbose),
        PCIDSS321Checks(scope, verbose=verbose),
        ServerlessChecks(scope, verbose=verbose),
    ]


def suppress_managed_policy_opt_in(
    resource: IConstruct,
    *,
    managed_policy_name: str,
    reason: str,
) -> None:
    """Scoped ``AwsSolutions-IAM4`` suppression for an intentional managed-policy attach.

    The house pattern for GCO is to enumerate least-privilege
    statements rather than attach AWS-managed policies, but a handful
    of opt-in sub-features (e.g. SageMaker Canvas) *must* track a
    managed policy because the underlying service's per-feature
    permission surface evolves faster than we can keep up with. For
    those cases we accept the ``AwsSolutions-IAM4`` finding with a
    scoped suppression rather than a broad one.

    This helper is the single call-site format for that pattern: pass
    the resource, the managed-policy name, and a one-line reason
    describing why the policy is appropriate for your feature. The
    helper expands the standard ``Policy::arn:<AWS::Partition>:iam::
    aws:policy/<name>`` applies-to ARN so every managed-policy opt-in
    in the codebase uses the same suppression shape — reviewers can
    grep for ``suppress_managed_policy_opt_in(`` to find every one.

    Args:
        resource: The IAM role (or other CDK construct) receiving the
            managed-policy attachment.
        managed_policy_name: The bare managed-policy name (e.g.
            ``"AmazonSageMakerCanvasFullAccess"``). Must NOT include
            the ``arn:<partition>:iam::aws:policy/`` prefix — the
            helper adds that.
        reason: Human-readable justification. Must explain (a) why
            the managed policy is preferred over an enumerated
            least-privilege policy, and (b) what the toggle or
            conditional is that gates the attachment (so reviewers
            can confirm the wider permission surface is opt-in).
    """
    acknowledge_nag_findings(
        resource,
        [
            NagSuppression(
                id="AwsSolutions-IAM4",
                reason=reason,
                applies_to=[
                    f"Policy::arn:<AWS::Partition>:iam::aws:policy/{managed_policy_name}",
                ],
            ),
        ],
    )


def add_eks_suppressions(stack: Stack) -> None:
    """Add suppressions for EKS-related cdk-nag findings.

    EKS requires specific AWS managed policies that cannot be replaced
    with customer-managed policies without breaking functionality.
    """
    # EKS requires these AWS managed policies - they are AWS-recommended
    eks_managed_policies = [
        "Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKSClusterPolicy",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKSComputePolicy",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKSBlockStoragePolicy",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKSLoadBalancingPolicy",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKSNetworkingPolicy",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKSWorkerNodePolicy",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEKS_CNI_Policy",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonEFSCSIDriverPolicy",
        # CloudWatch Observability addon policies for Container Insights
        "Policy::arn:<AWS::Partition>:iam::aws:policy/CloudWatchAgentServerPolicy",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/AWSXrayWriteOnlyAccess",
    ]

    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-IAM4",
                reason=(
                    "EKS requires AWS managed policies for cluster, node, and add-on functionality. "
                    "These are AWS-recommended policies that provide necessary permissions for EKS Auto Mode. "
                    "See: https://docs.aws.amazon.com/eks/latest/userguide/security-iam-awsmanpol.html"
                ),
                applies_to=eks_managed_policies,
            ),
        ],
    )


#: The function families every stack-wide Lambda acknowledgment below covers:
#:
#: * deployment-time functions: CDK's own custom-resource handlers and provider
#:   frameworks, GCO's custom-resource handlers, and the functions the Helm
#:   convergence and teardown state machines call during a stack operation;
#: * request-path functions, which API Gateway invokes synchronously: the global
#:   API's proxy, inference streaming proxy and aggregator, each regional
#:   bridge's proxy and inference streaming proxy, and the analytics stack's
#:   Studio presigned-URL function;
#: * scheduled functions, invoked by EventBridge (capacity poller, traffic dial,
#:   backend TLS certificate manager, drift detection) or by Secrets Manager
#:   rotation (secret rotation);
#: * the optional vector-store ingest function, which S3 invokes per object.
_LAMBDA_RUNTIME_REASON = (
    "GCO's functions take their runtime from LAMBDA_PYTHON_RUNTIME and "
    "LAMBDA_NODEJS_RUNTIME in gco/stacks/constants.py, which the monthly dependency scan "
    "keeps at the newest runtime aws-cdk-lib offers. The functions aws-cdk-lib creates for "
    "custom resources use the runtime aws-cdk-lib chooses."
)
_LAMBDA_CONCURRENCY_REASON = (
    "Every function's concurrency is bounded without a reservation: deployment-time "
    "functions run only during stack operations; scheduled functions run once per "
    "EventBridge interval or secret rotation; API Gateway invokes the request-path "
    "functions synchronously under the stage's throttling limits "
    "(api_gateway.throttle_rate_limit and throttle_burst_limit); and the optional "
    "vector-store ingest function runs once per object written under the corpus prefix. "
    "Reserving concurrency would take capacity from the account's shared pool. The backend "
    "TLS certificate manager reserves 1 so two runs never overlap."
)
_LAMBDA_DLQ_REASON = (
    "No event is lost without a function dead-letter queue: deployment-time functions "
    "return their result to CloudFormation or to the Step Functions execution that called "
    "them, which fails the stack operation; API Gateway invokes the request-path functions "
    "synchronously and returns their errors to the caller; Secrets Manager retries a failed "
    "rotation; and each scheduled function's EventBridge target retries twice and keeps "
    "events it cannot deliver in a dead-letter queue, while the next scheduled run repeats "
    "the work. The vector-store ingest function, invoked asynchronously with an object to "
    "process, has a dead-letter queue of its own."
)
_LAMBDA_OUTSIDE_VPC_REASON = (
    "Functions outside a VPC call only AWS service APIs and public endpoints (the Global "
    "Accelerator and the regional API bridges' IAM-authorized execute-api endpoints), never "
    "a private address, so a VPC would add NAT or VPC endpoint cost without isolating "
    "anything. The functions that reach a private cluster endpoint or the internal ALB "
    "(kubectl applier, Helm installer, GA registration, and each regional bridge's proxy "
    "and inference streaming proxy) run in the regional VPC, and the Studio presigned-URL "
    "function, which mounts Studio_EFS, runs in the analytics VPC."
)
_LAMBDA_DEFAULT_MEMORY_REASON = (
    "Only the functions aws-cdk-lib creates for custom resources (provider frameworks and "
    "CDK's singleton handlers) use the default memory size, which aws-cdk-lib chooses; "
    "every function GCO defines sets its memory size."
)


def add_lambda_suppressions(stack: Stack) -> None:
    """Add the stack-wide acknowledgments for Lambda-related cdk-nag findings.

    Each reason holds for every function family it covers (see the comment
    above ``_LAMBDA_RUNTIME_REASON``), whichever of them the stack contains.
    """
    lambda_managed_policies = [
        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
    ]

    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-IAM4",
                reason=(
                    "Lambda basic execution and VPC access roles are AWS-recommended managed policies. "
                    "They provide minimal permissions for CloudWatch Logs and VPC ENI management. "
                    "See: https://docs.aws.amazon.com/lambda/latest/dg/lambda-intro-execution-role.html"
                ),
                applies_to=lambda_managed_policies,
            ),
            NagSuppression(id="AwsSolutions-L1", reason=_LAMBDA_RUNTIME_REASON),
            NagSuppression(
                id="HIPAA.Security-LambdaConcurrency", reason=_LAMBDA_CONCURRENCY_REASON
            ),
            NagSuppression(id="HIPAA.Security-LambdaDLQ", reason=_LAMBDA_DLQ_REASON),
            NagSuppression(id="HIPAA.Security-LambdaInsideVPC", reason=_LAMBDA_OUTSIDE_VPC_REASON),
            NagSuppression(
                id="NIST.800.53.R5-LambdaConcurrency", reason=_LAMBDA_CONCURRENCY_REASON
            ),
            NagSuppression(id="NIST.800.53.R5-LambdaDLQ", reason=_LAMBDA_DLQ_REASON),
            NagSuppression(id="NIST.800.53.R5-LambdaInsideVPC", reason=_LAMBDA_OUTSIDE_VPC_REASON),
            NagSuppression(id="PCI.DSS.321-LambdaInsideVPC", reason=_LAMBDA_OUTSIDE_VPC_REASON),
            NagSuppression(id="Serverless-LambdaLatestVersion", reason=_LAMBDA_RUNTIME_REASON),
            NagSuppression(
                id="Serverless-LambdaDefaultMemorySize", reason=_LAMBDA_DEFAULT_MEMORY_REASON
            ),
            NagSuppression(id="Serverless-LambdaDLQ", reason=_LAMBDA_DLQ_REASON),
        ],
    )


def add_iam_suppressions(
    stack: Stack,
    regions: list[str] | None = None,
    global_region: str | None = None,
    project_name: str = "gco",
) -> None:
    """Add suppressions for IAM-related cdk-nag findings.

    CDK generates inline policies for custom resources and some patterns
    require wildcard permissions for dynamic resource access.

    Args:
        stack: The CDK stack to apply suppressions to
        regions: List of regional deployment regions (for EKS addon patterns)
        global_region: Global region for SSM parameters and DynamoDB tables
        project_name: Deployment prefix (#139). Every allow-list ARN pattern
            below (``/<project>/`` SSM tree, ``<project>-*`` DynamoDB tables)
            is scoped to this so the suppression matches the
            deployment's actual, project-scoped resource names. Defaults to
            ``"gco"`` so the rendered patterns are byte-for-byte identical to
            the pre-#139 literals for the stock deployment.
    """
    # The API Gateway auth secret needs no entry here: its read grants end in
    # ``-??????`` rather than ``*`` (see ``api_gateway_auth_secret_name``), and
    # AwsSolutions-IAM5 only flags ``*``.
    #
    # Build dynamic applies_to list based on configured regions
    applies_to = [
        # The convergence Step Functions state machine's role invokes each
        # Lambda task's versions — CDK's LambdaInvoke grants `<fn>.Arn:*` for
        # the version/alias qualifier, which is what these `:*` findings flag.
        # kubectl-applier: the base and post-Helm manifest passes.
        "Resource::<KubectlApplierFunction6147DA0C.Arn>:*",
        # GA-registration: the final Global Accelerator registration task.
        "Resource::<GaRegistrationFunction4A12C41B.Arn>:*",
        # Delete-time GA deregistration guard (issue #130): its cr.Provider
        # framework-onEvent role invokes the deregistration Lambda's versions.
        "Resource::<GaDeregistrationFunction5CFAADA4.Arn>:*",
        # helm-installer: one task per Helm chart.
        "Resource::<HelmInstallerFunction3FEB04EF.Arn>:*",
        # VPC Flow Logs delivery role writes log events to every stream in the
        # flow-log group (logs:CreateLogStream/PutLogEvents on `<group>.Arn:*`).
        "Resource::<VpcFlowLogGroup86559C69.Arn>:*",
    ]

    # Add EKS addon patterns for each configured region
    if regions:
        for region in regions:
            applies_to.append(
                f"Resource::arn:<AWS::Partition>:eks:{region}:<AWS::AccountId>:addon/<GCOEksCluster841A896A>/*"
            )

    # Add SSM parameter patterns for global region and all regional regions.
    # Using ``dict.fromkeys`` (insertion-ordered) + sorting gives a stable
    # ordering so the cdk-nag metadata block doesn't churn between synths
    # when PYTHONHASHSEED changes — previous ``set()`` iteration order was
    # hash-based and produced non-deterministic template diffs.
    ssm_regions_set: set[str] = set()
    if global_region:
        ssm_regions_set.add(global_region)
    if regions:
        ssm_regions_set.update(regions)

    for region in sorted(ssm_regions_set):
        applies_to.append(
            f"Resource::arn:<AWS::Partition>:ssm:{region}:<AWS::AccountId>:parameter/{project_name}/*"
        )
        # Per-chart add-on status + replay input written by the helm installer
        # and orchestrator (gco stacks addons status/install). Scoped to the
        # project's addons subtree in each region.
        applies_to.append(
            f"Resource::arn:<AWS::Partition>:ssm:{region}:<AWS::AccountId>:parameter/{project_name}/addons/*"
        )

    # Add DynamoDB index wildcard patterns for global region
    # Tables are created in global stack, accessed from all regional stacks
    if global_region:
        applies_to.extend(
            [
                f"Resource::arn:<AWS::Partition>:dynamodb:{global_region}:<AWS::AccountId>:table/{project_name}-job-templates/index/*",
                f"Resource::arn:<AWS::Partition>:dynamodb:{global_region}:<AWS::AccountId>:table/{project_name}-webhooks/index/*",
                f"Resource::arn:<AWS::Partition>:dynamodb:{global_region}:<AWS::AccountId>:table/{project_name}-jobs/index/*",
            ]
        )

    # No stack grants S3 on a ``<project_name>-*`` bucket pattern or KMS on
    # ``key/*`` any more: the workload role reads the buckets it uses by their
    # resolved names and the MCP server role lists the model bucket only, so
    # neither pattern is acknowledged.

    acknowledge_nag_findings(
        stack,
        [
            # Inline policy suppressions for all frameworks
            NagSuppression(
                id="HIPAA.Security-IAMNoInlinePolicy",
                reason=(
                    "CDK generates inline policies for custom resources and Lambda functions. "
                    "These are scoped to specific resources and follow least-privilege principles."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-IAMNoInlinePolicy",
                reason=(
                    "CDK generates inline policies for custom resources and Lambda functions. "
                    "These are scoped to specific resources and follow least-privilege principles."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-IAMNoInlinePolicy",
                reason=(
                    "CDK generates inline policies for custom resources and Lambda functions. "
                    "These are scoped to specific resources and follow least-privilege principles."
                ),
            ),
            # Wildcard permission suppressions
            NagSuppression(
                id="AwsSolutions-IAM5",
                reason=(
                    "Wildcard permissions are required for: (1) EKS cluster admin access to manage "
                    "dynamic Kubernetes resources, (2) Custom resource providers to invoke Lambda versions, "
                    "(3) SSM parameter access for cross-region coordination, (4) EKS addon management, "
                    "(5) VPC Flow Logs to write to CloudWatch, (6) DynamoDB GSI access for the job "
                    "queue, templates, and webhooks tables. All wildcards are scoped to specific "
                    "patterns."
                ),
                applies_to=applies_to,
            ),
        ],
    )


def add_vpc_suppressions(stack: Stack) -> None:
    """Add suppressions for VPC-related cdk-nag findings.

    Public subnets and IGW routes host the NAT gateways that provide bounded
    egress for private EKS nodes and VPC Lambdas. The platform ALB remains internal.
    """
    acknowledge_nag_findings(
        stack,
        [
            # HIPAA VPC suppressions
            NagSuppression(
                id="HIPAA.Security-VPCSubnetAutoAssignPublicIpDisabled",
                reason=(
                    "Public subnets host NAT gateways that provide controlled egress "
                    "for private EKS nodes and VPC Lambdas; workloads and the platform "
                    "ALB remain in private subnets."
                ),
            ),
            NagSuppression(
                id="HIPAA.Security-VPCNoUnrestrictedRouteToIGW",
                reason=(
                    "Public-subnet NAT gateways require an Internet Gateway route to "
                    "provide outbound dependency access for private subnets. EKS nodes, "
                    "VPC Lambdas, and the platform ALB remain private."
                ),
            ),
            # NIST 800-53 VPC suppressions
            NagSuppression(
                id="NIST.800.53.R5-VPCSubnetAutoAssignPublicIpDisabled",
                reason=(
                    "Public subnets host NAT gateways that provide controlled egress "
                    "for private EKS nodes and VPC Lambdas; workloads and the platform "
                    "ALB remain in private subnets."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-VPCNoUnrestrictedRouteToIGW",
                reason=(
                    "Public-subnet NAT gateways require an Internet Gateway route to "
                    "provide outbound dependency access for private subnets. EKS nodes, "
                    "VPC Lambdas, and the platform ALB remain private."
                ),
            ),
            # PCI DSS VPC suppressions
            NagSuppression(
                id="PCI.DSS.321-VPCSubnetAutoAssignPublicIpDisabled",
                reason=(
                    "Public subnets host NAT gateways that provide controlled egress "
                    "for private EKS nodes and VPC Lambdas; workloads and the platform "
                    "ALB remain in private subnets."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-VPCNoUnrestrictedRouteToIGW",
                reason=(
                    "Public-subnet NAT gateways require an Internet Gateway route to "
                    "provide outbound dependency access for private subnets. EKS nodes, "
                    "VPC Lambdas, and the platform ALB remain private."
                ),
            ),
        ],
    )


def add_api_gateway_suppressions(stack: Stack) -> None:
    """Add suppressions for API Gateway-related cdk-nag findings."""
    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-COG4",
                reason=(
                    "API Gateway uses IAM authentication (SigV4) instead of Cognito. "
                    "This is intentional for machine-to-machine API access patterns."
                ),
            ),
            NagSuppression(
                id="AwsSolutions-APIG2",
                reason=(
                    "Request validation is performed by the backend Manifest Processor service "
                    "which has detailed schema validation. API Gateway acts as a pass-through proxy."
                ),
            ),
            # Cache suppressions - caching is intentionally disabled
            NagSuppression(
                id="HIPAA.Security-APIGWCacheEnabledAndEncrypted",
                reason=(
                    "Caching is disabled intentionally. Manifest submissions are unique "
                    "and should not be cached. Health checks need real-time data."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-APIGWCacheEnabledAndEncrypted",
                reason=(
                    "Caching is disabled intentionally. Manifest submissions are unique "
                    "and should not be cached. Health checks need real-time data."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-APIGWCacheEnabledAndEncrypted",
                reason=(
                    "Caching is disabled intentionally. Manifest submissions are unique "
                    "and should not be cached. Health checks need real-time data."
                ),
            ),
            # SSL certificate suppressions
            NagSuppression(
                id="HIPAA.Security-APIGWSSLEnabled",
                reason=(
                    "API Gateway integrates with Lambda rather than an HTTP backend that "
                    "requires an API Gateway client certificate. The public API endpoint "
                    "enforces HTTPS; Lambda signs the exact private-backend request with "
                    "a short-lived HMAC envelope."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-APIGWSSLEnabled",
                reason=(
                    "API Gateway integrates with Lambda rather than an HTTP backend that "
                    "requires an API Gateway client certificate. The public API endpoint "
                    "enforces HTTPS; Lambda signs the exact private-backend request with "
                    "a short-lived HMAC envelope."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-APIGWSSLEnabled",
                reason=(
                    "API Gateway integrates with Lambda rather than an HTTP backend that "
                    "requires an API Gateway client certificate. The public API endpoint "
                    "enforces HTTPS; Lambda signs the exact private-backend request with "
                    "a short-lived HMAC envelope."
                ),
            ),
            # CloudWatch Log Group encryption suppressions
            NagSuppression(
                id="HIPAA.Security-CloudWatchLogGroupEncrypted",
                reason=(
                    "CloudWatch Logs are encrypted by default with AWS-managed keys. "
                    "Customer-managed KMS keys can be enabled via configuration if required."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-CloudWatchLogGroupEncrypted",
                reason=(
                    "CloudWatch Logs are encrypted by default with AWS-managed keys. "
                    "Customer-managed KMS keys can be enabled via configuration if required."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-CloudWatchLogGroupEncrypted",
                reason=(
                    "CloudWatch Logs are encrypted by default with AWS-managed keys. "
                    "Customer-managed KMS keys can be enabled via configuration if required."
                ),
            ),
            # API Gateway CloudWatch role
            NagSuppression(
                id="AwsSolutions-IAM4",
                reason=(
                    "API Gateway CloudWatch role requires the AWS managed policy "
                    "AmazonAPIGatewayPushToCloudWatchLogs for logging functionality."
                ),
                applies_to=[
                    "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs",
                ],
            ),
        ],
    )


def add_monitoring_suppressions(stack: Stack) -> None:
    """Add suppressions for monitoring-related cdk-nag findings."""
    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-SNS3",
                reason="SNS topic has enforce_ssl=True enabled, which adds the required policy.",
            ),
            NagSuppression(
                id="HIPAA.Security-SNSEncryptedKMS",
                reason=(
                    "Alert notifications contain operational data (alarm names, thresholds) "
                    "not PHI. KMS encryption adds latency to time-sensitive alerts."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-SNSEncryptedKMS",
                reason=(
                    "Alert notifications contain operational data (alarm names, thresholds). "
                    "KMS encryption adds latency to time-sensitive alerts."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-SNSEncryptedKMS",
                reason=(
                    "Alert notifications contain operational data (alarm names, thresholds). "
                    "KMS encryption can be enabled if required for PCI compliance."
                ),
            ),
            # The composite alarm's inputs, which carry no action of their own,
            # are acknowledged on each input (gco/stacks/control_plane_alarms.py).
        ],
    )


def add_storage_suppressions(stack: Stack) -> None:
    """Add suppressions for storage-related cdk-nag findings."""
    acknowledge_nag_findings(
        stack,
        [
            # EFS backup suppressions
            NagSuppression(
                id="HIPAA.Security-EFSInBackupPlan",
                reason=(
                    "EFS backup is optional and can be enabled via AWS Backup if required. "
                    "Default deployment prioritizes cost optimization."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-EFSInBackupPlan",
                reason=(
                    "EFS backup is optional and can be enabled via AWS Backup if required. "
                    "Default deployment prioritizes cost optimization."
                ),
            ),
            # CloudWatch Log Group encryption
            NagSuppression(
                id="HIPAA.Security-CloudWatchLogGroupEncrypted",
                reason=(
                    "CloudWatch Logs are encrypted by default with AWS-managed keys. "
                    "CDK Provider log groups are for infrastructure automation only."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-CloudWatchLogGroupEncrypted",
                reason=(
                    "CloudWatch Logs are encrypted by default with AWS-managed keys. "
                    "CDK Provider log groups are for infrastructure automation only."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-CloudWatchLogGroupEncrypted",
                reason=(
                    "CloudWatch Logs are encrypted by default with AWS-managed keys. "
                    "CDK Provider log groups are for infrastructure automation only."
                ),
            ),
        ],
    )


def add_sqs_suppressions(stack: Stack) -> None:
    """Add suppressions for SQS-related cdk-nag findings."""
    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-SQS4",
                reason="SQS queues have enforce_ssl=True enabled, which adds the required policy.",
            ),
            NagSuppression(
                id="Serverless-SQSRedrivePolicy",
                reason=(
                    "The dead-letter queue itself does not need a redrive policy. "
                    "The main job queue has a redrive policy pointing to the DLQ."
                ),
            ),
        ],
    )


def add_secrets_suppressions(stack: Stack) -> None:
    """Add suppressions for Secrets Manager-related cdk-nag findings."""
    acknowledge_nag_findings(
        stack,
        [
            # KMS key suppressions - using AWS-managed keys is acceptable
            NagSuppression(
                id="HIPAA.Security-SecretsManagerUsingKMSKey",
                reason=(
                    "Secrets Manager encrypts secrets by default with AWS-managed keys. "
                    "Customer-managed KMS can be enabled if required for compliance."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-SecretsManagerUsingKMSKey",
                reason="Secrets Manager encrypts secrets by default with AWS-managed keys.",
            ),
            NagSuppression(
                id="PCI.DSS.321-SecretsManagerUsingKMSKey",
                reason=(
                    "Secrets Manager encrypts secrets by default with AWS-managed keys. "
                    "Customer-managed KMS can be enabled if required for PCI compliance."
                ),
            ),
        ],
    )


def add_eks_cluster_suppressions(stack: Stack) -> None:
    """Add suppressions for EKS cluster-specific findings."""
    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-EKS1",
                reason=(
                    "The production default is a private EKS API endpoint. When operators "
                    "explicitly opt into PUBLIC_AND_PRIVATE for development access, the "
                    "public endpoint is authenticated and authorized through IAM."
                ),
            ),
        ],
    )


def add_backup_suppressions(stack: Stack) -> None:
    """Add suppressions for AWS Backup-related cdk-nag findings."""
    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-IAM4",
                reason=(
                    "AWS Backup requires the AWSBackupServiceRolePolicyForBackup managed policy "
                    "attached to the backup service role to perform backup operations on DynamoDB tables. "
                    "This is the AWS-recommended policy for AWS Backup default service roles. "
                    "See: https://docs.aws.amazon.com/aws-backup/latest/devguide/iam-service-roles.html"
                ),
                applies_to=[
                    "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
                ],
            ),
        ],
    )


def add_aurora_pgvector_suppressions(stack: Stack) -> None:
    """Add suppressions for Aurora pgvector-related cdk-nag findings.

    Aurora Serverless v2 with pgvector triggers several compliance findings
    that are intentionally accepted for this deployment pattern.
    """
    acknowledge_nag_findings(
        stack,
        [
            # Secrets Manager KMS key — Aurora secret uses AWS-managed encryption
            NagSuppression(
                id="HIPAA.Security-SecretsManagerUsingKMSKey",
                reason=(
                    "Aurora Serverless v2 credentials in Secrets Manager are encrypted with "
                    "AWS-managed keys by default. Customer-managed KMS can be enabled if required."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-SecretsManagerUsingKMSKey",
                reason=(
                    "Aurora Serverless v2 credentials in Secrets Manager are encrypted with "
                    "AWS-managed keys by default."
                ),
            ),
            # Secrets Manager rotation — Aurora manages rotation via RDS integration
            NagSuppression(
                id="HIPAA.Security-SecretsManagerRotationEnabled",
                reason=(
                    "Aurora manages credential rotation via the RDS integration with Secrets "
                    "Manager. Manual rotation configuration is not required."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-SecretsManagerRotationEnabled",
                reason=(
                    "Aurora manages credential rotation via the RDS integration with Secrets "
                    "Manager. Manual rotation configuration is not required."
                ),
            ),
            # RDS in backup plan — Aurora has built-in continuous backups
            NagSuppression(
                id="HIPAA.Security-RDSInBackupPlan",
                reason=(
                    "Aurora Serverless v2 has built-in continuous backups with point-in-time "
                    "recovery. AWS Backup integration is optional and can be enabled if required."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-RDSInBackupPlan",
                reason=(
                    "Aurora Serverless v2 has built-in continuous backups with point-in-time "
                    "recovery. AWS Backup integration is optional."
                ),
            ),
            # RDS logging enabled — covered by cloudwatch_logs_exports=["postgresql"]
            # but some frameworks check for additional log types
            NagSuppression(
                id="HIPAA.Security-RDSLoggingEnabled",
                reason=(
                    "PostgreSQL logs are exported to CloudWatch via cloudwatch_logs_exports. "
                    "Aurora Serverless v2 does not support all log types available on provisioned instances."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-RDSLoggingEnabled",
                reason=(
                    "PostgreSQL logs are exported to CloudWatch via cloudwatch_logs_exports. "
                    "Aurora Serverless v2 does not support all log types available on provisioned instances."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-RDSLoggingEnabled",
                reason=(
                    "PostgreSQL logs are exported to CloudWatch via cloudwatch_logs_exports. "
                    "Aurora Serverless v2 does not support all log types available on provisioned instances."
                ),
            ),
            # CloudWatch Log Group encryption for Aurora logs
            NagSuppression(
                id="HIPAA.Security-CloudWatchLogGroupEncrypted",
                reason=(
                    "CloudWatch Logs for Aurora PostgreSQL are encrypted by default with "
                    "AWS-managed keys. Customer-managed KMS can be enabled if required."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-CloudWatchLogGroupEncrypted",
                reason=(
                    "CloudWatch Logs for Aurora PostgreSQL are encrypted by default with "
                    "AWS-managed keys."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-CloudWatchLogGroupEncrypted",
                reason=(
                    "CloudWatch Logs for Aurora PostgreSQL are encrypted by default with "
                    "AWS-managed keys."
                ),
            ),
            # Enhanced monitoring IAM role uses AWS managed policy
            NagSuppression(
                id="AwsSolutions-IAM4",
                reason=(
                    "Aurora enhanced monitoring requires the AWS managed policy "
                    "AmazonRDSEnhancedMonitoringRole for publishing OS-level metrics to CloudWatch. "
                    "This is the AWS-recommended policy for RDS enhanced monitoring. "
                    "See: https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_Monitoring.OS.Enabling.html"
                ),
                applies_to=[
                    "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole",
                ],
            ),
        ],
    )


def add_cognito_suppressions(stack: Stack) -> None:
    """Add the acknowledgment for the Studio user pool's MFA finding.

    The pool (``GCOAnalyticsStack._create_cognito_pool``) is on the Lite
    feature plan with threat protection off (``NO_ENFORCEMENT``, its own
    ``AwsSolutions-COG8`` acknowledgment), has self sign-up disabled, and does
    not configure MFA.
    """
    acknowledge_nag_findings(
        stack,
        [
            # COG2 is WARN-level: "The Cognito user pool does not require MFA."
            NagSuppression(
                id="AwsSolutions-COG2",
                reason=(
                    "The Studio user pool does not configure MFA. Users are created "
                    "by an administrator (self sign-up is disabled) and sign in "
                    "through gco analytics studio login (AdminInitiateAuth, which "
                    "also needs AWS credentials for the pool), with a password "
                    "policy of at least 12 characters including digits, symbols, "
                    "upper and lower case. Turn MFA on for the pool if your policy "
                    "requires a second factor."
                ),
            ),
        ],
    )


#: Why the analytics VPC has no flow log (an accepted gap, not a property of
#: the network: notebook egress leaves through a NAT gateway).
_ANALYTICS_FLOW_LOG_REASON = (
    "The analytics VPC has no flow log. Its private subnets hold Studio, EMR "
    "Serverless, the EFS mount targets and the VPC endpoints, and its public "
    "subnets hold one NAT gateway that gives notebooks internet egress, so "
    "notebook traffic is not flow-logged (the regional VPCs are). Accepted for "
    "the optional, off-by-default analytics environment; attach an ec2.FlowLog "
    "to AnalyticsVpc where flow logs are required."
)


def add_analytics_vpc_suppressions(stack: Stack) -> None:
    """Add suppressions for the analytics VPC and its endpoints.

    The analytics VPC uses private subnets with NAT egress for notebook
    internet access (pip install, git clone) plus a small public subnet
    that hosts only the NAT gateway ENI. Findings on the public subnet
    and IGW route are expected — no compute runs there.
    """
    acknowledge_nag_findings(
        stack,
        [
            # Flow-logs acknowledgments: an accepted gap, see the reason.
            NagSuppression(
                id="AwsSolutions-VPC7",
                reason=_ANALYTICS_FLOW_LOG_REASON,
            ),
            NagSuppression(
                id="HIPAA.Security-VPCFlowLogsEnabled",
                reason=_ANALYTICS_FLOW_LOG_REASON,
            ),
            NagSuppression(
                id="NIST.800.53.R5-VPCFlowLogsEnabled",
                reason=_ANALYTICS_FLOW_LOG_REASON,
            ),
            NagSuppression(
                id="PCI.DSS.321-VPCFlowLogsEnabled",
                reason=_ANALYTICS_FLOW_LOG_REASON,
            ),
            # Public subnet findings — the NAT gateway requires a public
            # subnet with an IGW route. No compute runs in the public
            # subnet; it only hosts the NAT gateway's ENI.
            NagSuppression(
                id="HIPAA.Security-VPCSubnetAutoAssignPublicIpDisabled",
                reason=(
                    "The public subnet exists solely to host the NAT "
                    "gateway ENI for notebook internet egress (pip install, "
                    "git clone). No EC2 instances or Studio compute runs "
                    "in this subnet."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-VPCSubnetAutoAssignPublicIpDisabled",
                reason=(
                    "The public subnet exists solely to host the NAT "
                    "gateway ENI. No compute workloads run here."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-VPCSubnetAutoAssignPublicIpDisabled",
                reason=(
                    "The public subnet exists solely to host the NAT "
                    "gateway ENI. No compute workloads run here."
                ),
            ),
            NagSuppression(
                id="HIPAA.Security-VPCNoUnrestrictedRouteToIGW",
                reason=(
                    "The 0.0.0.0/0 route to the IGW is in the public "
                    "subnet's route table, which only hosts the NAT "
                    "gateway. Private subnets route through NAT, not IGW."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-VPCNoUnrestrictedRouteToIGW",
                reason=(
                    "The 0.0.0.0/0 route to the IGW is in the public "
                    "subnet's route table for NAT gateway egress only."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-VPCNoUnrestrictedRouteToIGW",
                reason=(
                    "The 0.0.0.0/0 route to the IGW is in the public "
                    "subnet's route table for NAT gateway egress only."
                ),
            ),
        ],
    )


def add_analytics_s3_suppressions(stack: Stack) -> None:
    """Add suppressions for ``Studio_Only_Bucket`` + access-logs bucket findings.

    The analytics stack owns two buckets:

    1. ``Studio_Only_Bucket`` — KMS-encrypted with ``Analytics_KMS_Key``,
       block public access, enforce SSL, versioned. Replication is not
       enabled because this bucket is the endpoint of the SageMaker
       workload; cross-region replication would double storage cost and
       introduce eventual-consistency behavior that breaks notebook
       save/load semantics.
    2. ``AnalyticsAccessLogsBucket`` — the log sink, SSE-S3 because S3 server
       access logging requires an SSE-S3 destination (its SSE-KMS rules are
       acknowledged on the bucket by
       ``acknowledge_access_log_destination_encryption``). Replication is not
       enabled because the bucket is the log sink, not a data store.
    """
    acknowledge_nag_findings(
        stack,
        [
            # S3 replication suppressions — both buckets are single-
            # region by design. The Studio bucket is scoped to a single
            # deploy region (api-gateway region) and the access-logs
            # bucket is its log sink; cross-region replication is not
            # applicable to either.
            NagSuppression(
                id="HIPAA.Security-S3BucketReplicationEnabled",
                reason=(
                    "Studio_Only_Bucket and its access-logs bucket are "
                    "single-region by design. The Studio bucket is the "
                    "endpoint of the SageMaker workload in the api-gateway "
                    "region; cross-region replication would double storage "
                    "cost without a corresponding availability gain (the "
                    "Studio domain itself is single-region). The access-"
                    "logs bucket is the log sink and is co-located with "
                    "the data bucket by construction."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-S3BucketReplicationEnabled",
                reason=(
                    "Studio_Only_Bucket and its access-logs bucket are "
                    "single-region by design. The Studio bucket is the "
                    "endpoint of the SageMaker workload in the api-gateway "
                    "region; cross-region replication would double storage "
                    "cost without a corresponding availability gain (the "
                    "Studio domain itself is single-region). The access-"
                    "logs bucket is the log sink and is co-located with "
                    "the data bucket by construction."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-S3BucketReplicationEnabled",
                reason=(
                    "Studio_Only_Bucket and its access-logs bucket are "
                    "single-region by design. The Studio bucket is the "
                    "endpoint of the SageMaker workload in the api-gateway "
                    "region; cross-region replication would double storage "
                    "cost without a corresponding availability gain (the "
                    "Studio domain itself is single-region). The access-"
                    "logs bucket is the log sink and is co-located with "
                    "the data bucket by construction."
                ),
            ),
        ],
    )


def apply_all_suppressions(
    stack: Stack,
    stack_type: str = "regional",
    regions: list[str] | None = None,
    global_region: str | None = None,
    project_name: str = "gco",
) -> None:
    """Apply all relevant suppressions to a stack.

    Args:
        stack: The CDK stack to apply suppressions to
        stack_type: Type of stack - 'regional', 'global', 'api_gateway',
            'regional_api_gateway', 'regional_persistent', 'monitoring', or 'analytics'
        regions: List of regional deployment regions (for dynamic IAM suppression patterns)
        global_region: Global region for SSM parameters (for dynamic IAM suppression patterns)
        project_name: Deployment prefix (#139), forwarded to
            ``add_iam_suppressions`` so the IAM allow-list ARN patterns match
            the deployment's project-scoped resource names. Defaults to
            ``"gco"``.
    """
    # Common suppressions for all stacks
    add_lambda_suppressions(stack)
    add_iam_suppressions(
        stack,
        regions=regions,
        global_region=global_region,
        project_name=project_name,
    )

    if stack_type == "regional":
        add_eks_suppressions(stack)
        add_eks_cluster_suppressions(stack)
        add_vpc_suppressions(stack)
        add_storage_suppressions(stack)
        add_sqs_suppressions(stack)
        add_aurora_pgvector_suppressions(stack)

    elif stack_type == "global":
        add_backup_suppressions(stack)

    elif stack_type == "api_gateway":
        add_api_gateway_suppressions(stack)
        add_secrets_suppressions(stack)

    elif stack_type == "regional_api_gateway":
        add_api_gateway_suppressions(stack)
    elif stack_type == "regional_persistent":
        # One bucket pair and its KMS key; the bucket-level acknowledgments
        # are on the buckets in gco/stacks/regional_persistent_stack.py.
        pass

    elif stack_type == "monitoring":
        add_monitoring_suppressions(stack)

    elif stack_type == "analytics":
        # Analytics stack has S3 buckets (Studio_Only + access-logs), KMS,
        # EFS, Cognito, SageMaker, EMR Serverless, and the presigned-URL
        # Lambda. The IAM wildcards of its roles and policies are
        # acknowledged on each of them in ``gco/stacks/analytics_stack.py``.
        add_storage_suppressions(stack)
        add_cognito_suppressions(stack)
        add_analytics_vpc_suppressions(stack)
        add_analytics_s3_suppressions(stack)
