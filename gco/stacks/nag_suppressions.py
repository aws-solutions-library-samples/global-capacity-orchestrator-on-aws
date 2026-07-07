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

from gco.stacks.constants import API_GATEWAY_AUTH_SECRET_NAME

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
    # cdk-nag renders the CloudFormation account/region pseudo-parameters in a
    # finding's detail two different ways, and which one appears is decided
    # per-ARN, not per-stack:
    #   * an ARN built from an ``Aws.ACCOUNT_ID`` / ``Aws.REGION`` pseudo-param
    #     always renders as the angle-bracket literal ``<AWS::AccountId>`` /
    #     ``<AWS::Region>`` (CloudFormation resolves the pseudo-param at deploy,
    #     never at synth), whereas
    #   * an ARN hand-built from ``stack.account`` / ``stack.region`` renders as
    #     the *concrete* value when the stack is environment-specific, and as
    #     the pseudo-param literal when it is environment-agnostic.
    # So for a concrete env we can't know from here which form cdk-nag will
    # emit for any given finding. We therefore register the acknowledgment
    # under BOTH the placeholder detail and its literal rendering; extra keys
    # that match no finding are harmless, and whichever form cdk-nag emits then
    # has a matching acknowledgment. A detail we ourselves built from a region/
    # account *token* is first normalized to the placeholder form — a raw token
    # can't be used as a metadata-map key (synth fails with
    # ``KeyMustResolveToString``).
    stack = Stack.of(scope)

    def _keys_for(rule_id: str, detail: str) -> list[str]:
        if Token.is_unresolved(stack.region):
            detail = detail.replace(stack.region, "<AWS::Region>")
        if Token.is_unresolved(stack.account):
            detail = detail.replace(stack.account, "<AWS::AccountId>")
        if Token.is_unresolved(detail):
            raise ValueError(
                "cdk-nag acknowledgment detail contains an unresolved token and "
                f"cannot be used as a metadata key: {detail!r}. Use cdk-nag's "
                "literal rendering (e.g. '<AWS::Region>', '<LogicalId.Arn>') instead."
            )
        # Expand the placeholder detail into every form cdk-nag might emit: for
        # each concrete env dimension, add a variant with the pseudo-param
        # placeholder swapped for its literal value.
        variants = {detail}
        if not Token.is_unresolved(stack.account):
            variants.update(v.replace("<AWS::AccountId>", stack.account) for v in list(variants))
        if not Token.is_unresolved(stack.region):
            variants.update(v.replace("<AWS::Region>", stack.region) for v in list(variants))
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


def add_lambda_suppressions(stack: Stack) -> None:
    """Add suppressions for Lambda-related cdk-nag findings.

    Lambda functions used for CDK custom resources and infrastructure
    automation have specific requirements that trigger cdk-nag warnings.
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
            NagSuppression(
                id="AwsSolutions-L1",
                reason=(
                    "CDK Provider framework Lambda functions use a specific runtime version "
                    "managed by CDK. These are internal functions not exposed to users."
                ),
            ),
            # HIPAA Lambda suppressions
            NagSuppression(
                id="HIPAA.Security-LambdaConcurrency",
                reason=(
                    "Infrastructure Lambda functions (custom resources) are invoked only during "
                    "stack deployment and do not require concurrency limits. They are not user-facing."
                ),
            ),
            NagSuppression(
                id="HIPAA.Security-LambdaDLQ",
                reason=(
                    "CDK custom resource Lambda functions have built-in retry logic and report "
                    "failures directly to CloudFormation. DLQ is not applicable for this pattern."
                ),
            ),
            NagSuppression(
                id="HIPAA.Security-LambdaInsideVPC",
                reason=(
                    "CDK Provider framework Lambda functions need internet access to communicate "
                    "with CloudFormation. VPC placement would require NAT Gateway configuration. "
                    "User-facing Lambda functions (kubectl applier) ARE placed in VPC."
                ),
            ),
            # NIST 800-53 Lambda suppressions
            NagSuppression(
                id="NIST.800.53.R5-LambdaConcurrency",
                reason=(
                    "Infrastructure Lambda functions (custom resources) are invoked only during "
                    "stack deployment and do not require concurrency limits."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-LambdaDLQ",
                reason=(
                    "CDK custom resource Lambda functions have built-in retry logic and report "
                    "failures directly to CloudFormation. DLQ is not applicable."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-LambdaInsideVPC",
                reason=(
                    "CDK Provider framework Lambda functions need internet access to communicate "
                    "with CloudFormation. User-facing Lambda functions ARE placed in VPC."
                ),
            ),
            # PCI DSS Lambda suppressions
            NagSuppression(
                id="PCI.DSS.321-LambdaInsideVPC",
                reason=(
                    "CDK Provider framework Lambda functions need internet access to communicate "
                    "with CloudFormation. User-facing Lambda functions ARE placed in VPC."
                ),
            ),
            # Serverless Lambda suppressions
            NagSuppression(
                id="Serverless-LambdaLatestVersion",
                reason=(
                    "CDK Provider framework Lambda functions use a specific runtime version "
                    "managed by CDK. These are internal functions not exposed to users."
                ),
            ),
            NagSuppression(
                id="Serverless-LambdaDefaultMemorySize",
                reason=(
                    "CDK Provider framework Lambda functions have appropriate memory for their "
                    "workload. Custom Lambda functions have explicit memory configuration."
                ),
            ),
            NagSuppression(
                id="Serverless-LambdaDLQ",
                reason=(
                    "CDK custom resource Lambda functions have built-in retry logic and report "
                    "failures directly to CloudFormation. DLQ is not applicable."
                ),
            ),
        ],
    )


def add_iam_suppressions(
    stack: Stack,
    regions: list[str] | None = None,
    global_region: str | None = None,
    api_gateway_region: str | None = None,
) -> None:
    """Add suppressions for IAM-related cdk-nag findings.

    CDK generates inline policies for custom resources and some patterns
    require wildcard permissions for dynamic resource access.

    Args:
        stack: The CDK stack to apply suppressions to
        regions: List of regional deployment regions (for EKS addon patterns)
        global_region: Global region for SSM parameters and DynamoDB tables
        api_gateway_region: Region where the API Gateway auth secret lives.
            The regional service-account role's read grant is scoped to a
            deterministic ARN in this region (see ``GCORegionalStack`` and
            issue #125). Falls back to ``global_region`` for callers that
            don't split the API Gateway stack into its own region — the two
            are co-located in the default topology.
    """
    # Region the API Gateway auth secret is created in. It is normally the
    # same as ``global_region`` (default cdk.json co-locates them), but the
    # secret physically lives in the API Gateway stack's region, so scope the
    # grant/suppression to that region when it is provided.
    secret_region = api_gateway_region or global_region or "us-east-2"

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
        # Secrets Manager access for the API Gateway auth token, with a
        # trailing wildcard for the random 6-char suffix. The regional
        # service-account role builds this exact ARN deterministically (from
        # the secret name + API Gateway region + account) so it matches in
        # both single-region and cross-region topologies — see issue #125.
        f"Resource::arn:aws:secretsmanager:{secret_region}:<AWS::AccountId>:secret:{API_GATEWAY_AUTH_SECRET_NAME}*",
    ]

    # Add EKS addon patterns for each configured region
    if regions:
        for region in regions:
            applies_to.append(
                f"Resource::arn:aws:eks:{region}:<AWS::AccountId>:addon/<GCOEksCluster841A896A>/*"
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
        applies_to.append(f"Resource::arn:aws:ssm:{region}:<AWS::AccountId>:parameter/gco/*")
        # Per-chart add-on status + replay input written by the helm installer
        # and orchestrator (gco stacks addons status/install). Scoped to the
        # project's addons subtree in each region.
        applies_to.append(f"Resource::arn:aws:ssm:{region}:<AWS::AccountId>:parameter/gco/addons/*")

    # Add DynamoDB index wildcard patterns for global region
    # Tables are created in global stack, accessed from all regional stacks
    if global_region:
        applies_to.extend(
            [
                f"Resource::arn:aws:dynamodb:{global_region}:<AWS::AccountId>:table/gco-job-templates/index/*",
                f"Resource::arn:aws:dynamodb:{global_region}:<AWS::AccountId>:table/gco-webhooks/index/*",
                f"Resource::arn:aws:dynamodb:{global_region}:<AWS::AccountId>:table/gco-jobs/index/*",
                f"Resource::arn:aws:dynamodb:{global_region}:<AWS::AccountId>:table/gco-inference-endpoints/index/*",
            ]
        )

    # Add S3 wildcard patterns for model weights bucket
    # Bucket name is auto-generated by CDK, so we use a prefix pattern
    applies_to.extend(
        [
            "Resource::arn:aws:s3:::gco-*",
            "Resource::arn:aws:s3:::gco-*/*",
        ]
    )

    # KMS wildcard scoped to S3 via condition for model weights bucket decryption
    applies_to.append("Resource::arn:aws:kms:*:<AWS::AccountId>:key/*")

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
                    "(5) VPC Flow Logs to write to CloudWatch, (6) Secrets Manager cross-region access "
                    "with wildcard suffix for auth token, (7) DynamoDB GSI access for job queue, templates, "
                    "webhooks, and inference endpoints tables, (8) S3 access for model weights bucket "
                    "(auto-generated name). All wildcards are scoped to specific patterns. "
                    "(9) KMS decrypt scoped to S3 via condition for model weights bucket."
                ),
                applies_to=applies_to,
            ),
        ],
    )


def add_vpc_suppressions(stack: Stack) -> None:
    """Add suppressions for VPC-related cdk-nag findings.

    Public subnets and IGW routes are required for ALB and NAT Gateway
    functionality in a multi-tier architecture.
    """
    acknowledge_nag_findings(
        stack,
        [
            # HIPAA VPC suppressions
            NagSuppression(
                id="HIPAA.Security-VPCSubnetAutoAssignPublicIpDisabled",
                reason=(
                    "Public subnets are required for internet-facing ALB. EC2 instances "
                    "(EKS nodes) are deployed only in private subnets."
                ),
            ),
            NagSuppression(
                id="HIPAA.Security-VPCNoUnrestrictedRouteToIGW",
                reason=(
                    "Public subnets require IGW route for ALB to receive traffic from "
                    "Global Accelerator. All compute resources are in private subnets."
                ),
            ),
            # NIST 800-53 VPC suppressions
            NagSuppression(
                id="NIST.800.53.R5-VPCSubnetAutoAssignPublicIpDisabled",
                reason=(
                    "Public subnets are required for internet-facing ALB. EC2 instances "
                    "(EKS nodes) are deployed only in private subnets."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-VPCNoUnrestrictedRouteToIGW",
                reason=(
                    "Public subnets require IGW route for ALB to receive traffic from "
                    "Global Accelerator. All compute resources are in private subnets."
                ),
            ),
            # PCI DSS VPC suppressions
            NagSuppression(
                id="PCI.DSS.321-VPCSubnetAutoAssignPublicIpDisabled",
                reason=(
                    "Public subnets are required for internet-facing ALB. EC2 instances "
                    "(EKS nodes) are deployed only in private subnets."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-VPCNoUnrestrictedRouteToIGW",
                reason=(
                    "Public subnets require IGW route for ALB to receive traffic from "
                    "Global Accelerator. All compute resources are in private subnets."
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
                    "Backend SSL certificates are not required as traffic flows through "
                    "Global Accelerator (TLS terminated) to internal ALB (HTTPS)."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-APIGWSSLEnabled",
                reason=(
                    "Backend SSL certificates are not required as traffic flows through "
                    "Global Accelerator (TLS terminated) to internal ALB (HTTPS)."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-APIGWSSLEnabled",
                reason=(
                    "Backend SSL certificates are not required as traffic flows through "
                    "Global Accelerator (TLS terminated) to internal ALB (HTTPS)."
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
            # CloudWatch Log Group encryption
            NagSuppression(
                id="HIPAA.Security-CloudWatchLogGroupEncrypted",
                reason="CloudWatch Logs are encrypted by default with AWS-managed keys.",
            ),
            NagSuppression(
                id="NIST.800.53.R5-CloudWatchLogGroupEncrypted",
                reason="CloudWatch Logs are encrypted by default with AWS-managed keys.",
            ),
            NagSuppression(
                id="PCI.DSS.321-CloudWatchLogGroupEncrypted",
                reason="CloudWatch Logs are encrypted by default with AWS-managed keys.",
            ),
            # CloudWatch Alarm Action suppressions for composite alarm inputs
            # These alarms are intentionally used only as inputs to composite alarms
            # The composite alarms have actions attached, not the individual alarms
            NagSuppression(
                id="HIPAA.Security-CloudWatchAlarmAction",
                reason=(
                    "These alarms are inputs to composite alarms which have SNS actions. "
                    "Individual alarms don't need actions as they're aggregated for better signal-to-noise."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-CloudWatchAlarmAction",
                reason=(
                    "These alarms are inputs to composite alarms which have SNS actions. "
                    "Individual alarms don't need actions as they're aggregated for better signal-to-noise."
                ),
            ),
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
                    "EKS public endpoint is enabled for kubectl access from CI/CD pipelines "
                    "and developer workstations. Access is controlled via IAM."
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


def add_sagemaker_suppressions(
    stack: Stack,
    api_gateway_region: str | None = None,
    global_region: str | None = None,
) -> None:
    """Add suppressions for SageMaker Studio Domain + execution role findings.

    The analytics stack uses a private-VPC SageMaker Studio domain whose
    execution role needs wildcard access to a known set of ARN patterns:
    regional SQS job queues (one per regional stack, name pattern
    ``<project>-jobs-<region>``), GCO API Gateway GET routes (any REST API
    id under ``/prod/GET/api/v1/*``), and ``Cluster_Shared_Bucket`` objects
    resolved from cross-region SSM. Each wildcard is scoped on the literal
    patterns below so cdk-nag's ``AwsSolutions-IAM5`` check surfaces only
    the documented escape hatches.

    Args:
        stack: The analytics stack to apply suppressions to.
        api_gateway_region: Concrete region where the API Gateway stack
            lives (used to resolve the execute-api ARN pattern).
        global_region: Concrete global region (used to resolve the
            KMS ``ViaService`` condition's service endpoint — the KMS
            decrypt ARN itself is ``*`` because the cluster-shared KMS
            key lives in a different stack).
    """
    api_region = api_gateway_region or "*"
    gbl_region = global_region or "*"

    applies_to: list[str] = [
        # SageMaker execution role — SQS submit to any regional queue under
        # the project's ``<project>-jobs-*`` pattern. The SQS queue ARNs
        # are owned by the regional stacks and not directly importable.
        "Resource::arn:aws:sqs:*:<AWS::AccountId>:gco-jobs-*",
        # SageMaker execution role — ``ssm:GetParameter`` on the
        # Cluster_Shared_Bucket metadata parameters under
        # ``/gco/cluster-shared-bucket/*`` in the global region. The path
        # wildcard covers exactly three literal parameter names
        # (name / arn / region) defined by ``GCOGlobalStack``; the rest of
        # the ARN is fully scoped (global region + account).
        f"Resource::arn:aws:ssm:{gbl_region}:<AWS::AccountId>:parameter/gco/cluster-shared-bucket/*",
        # SageMaker execution role — execute-api on any REST API id
        # under ``/prod/*/api/v1/*`` and ``/prod/*/inference/*`` in the
        # api-gateway region. The concrete region value is templated in
        # so the nag match works regardless of which region the user
        # deploys to. The HTTP-method segment is ``*`` (instead of
        # pinning ``GET``) so notebooks can submit jobs, update
        # templates, and manage inference endpoints in addition to
        # read-only GETs.
        f"Resource::arn:aws:execute-api:{api_region}:<AWS::AccountId>:*/prod/*/api/v1/*",
        f"Resource::arn:aws:execute-api:{api_region}:<AWS::AccountId>:*/prod/*/inference/*",
        # KMS decrypt scoped by ``kms:ViaService=s3.<global-region>.amazonaws.com``
        # condition — the resource ARN is unknown to this stack (cluster-
        # shared KMS key lives in the global region) so Resource::* is the
        # documented pattern, narrowed by the ViaService condition.
        "Resource::*",
        # S3 grant_read_write on Studio_Only_Bucket produces the AWS-
        # recommended set of S3 action wildcards. Each one covers a
        # closed, read-or-write intent on a single literal bucket ARN.
        "Action::s3:Abort*",
        "Action::s3:DeleteObject*",
        "Action::s3:GetBucket*",
        "Action::s3:GetObject*",
        "Action::s3:List*",
        # KMS grant_encrypt_decrypt on Analytics_KMS_Key produces the
        # AWS-recommended set of KMS action wildcards. Each covers a
        # single key ARN.
        "Action::kms:GenerateDataKey*",
        "Action::kms:ReEncrypt*",
        # Object-key wildcard on the literal Studio_Only_Bucket ARN — the
        # RW grant must cover every object key under the bucket.
        "Resource::<StudioOnlyBucket80FF5E65.Arn>/*",
        # ``kms:ViaService`` condition-scoped wildcard on the cluster-
        # shared bucket's KMS key — only matched when s3 is the invoking
        # service in the global region.
        f"Condition::kms:ViaService:s3.{gbl_region}.amazonaws.com",
        # Studio UI actions — the execution role is assumed by the Studio
        # runtime and needs domain/space/app/user-profile wildcards to
        # render the IDE and manage notebook apps.
        f"Resource::arn:aws:sagemaker:{api_region}:<AWS::AccountId>:domain/*",
        f"Resource::arn:aws:sagemaker:{api_region}:<AWS::AccountId>:user-profile/*/*",
        f"Resource::arn:aws:sagemaker:{api_region}:<AWS::AccountId>:space/*/*",
        f"Resource::arn:aws:sagemaker:{api_region}:<AWS::AccountId>:app/*/*/*/*",
        # EMR Serverless — Studio discovers and manages EMR apps via these
        # actions. Resource::* is required because EMR Serverless does not
        # support resource-level scoping on most actions.
        "Action::emr-serverless:*",
        # SageMaker MLflow tracking servers + MLflow Apps. The
        # ``sagemaker-mlflow:*`` data-plane namespace is the one the
        # MLflow SDK talks to via SigV4 (``log_metric``,
        # ``log_artifact``, ``register_model``, etc.); the managed
        # ``AmazonSageMakerFullAccess`` policy covers the
        # ``sagemaker:*`` control-plane namespace (MLflow Apps,
        # Tracking Servers, Model Registry) but NOT the
        # ``sagemaker-mlflow`` service prefix, so this statement stays
        # inline and scoped to the api-gateway region + account.
        "Action::sagemaker-mlflow:*",
        f"Resource::arn:aws:sagemaker:{api_region}:<AWS::AccountId>:mlflow-tracking-server/*",
        f"Resource::arn:aws:sagemaker:{api_region}:<AWS::AccountId>:mlflow-app/*",
        # ``sts:GetCallerIdentity`` does not support resource-level
        # scoping; the MLflow SigV4 plug-in calls it on every request.
        "Action::sts:GetCallerIdentity",
    ]

    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-IAM5",
                reason=(
                    "SageMaker_Execution_Role uses wildcard ARNs and actions for: "
                    "(1) SQS SendMessage on any regional job queue matching "
                    "``<project>-jobs-<region>``, (2) execute-api:Invoke on "
                    "any REST API id under /prod/*/api/v1/* and "
                    "/prod/*/inference/* in the api-gateway region (all "
                    "HTTP methods, so notebooks can submit jobs and "
                    "manage inference endpoints in addition to read-only "
                    "GETs), (3) KMS Decrypt/GenerateDataKey "
                    "scoped by kms:ViaService=s3.<global-region>.amazonaws.com "
                    "condition (the cluster-shared KMS key ARN is not known "
                    "to the analytics stack — it lives in the global region), "
                    "(4) S3 action wildcards (``s3:Abort*``, ``s3:DeleteObject*``, "
                    "``s3:GetBucket*``, ``s3:GetObject*``, ``s3:List*``) produced "
                    "by ``bucket.grant_read_write(role)`` on the literal "
                    "Studio_Only_Bucket ARN, (5) KMS action wildcards "
                    "(``kms:GenerateDataKey*``, ``kms:ReEncrypt*``) produced by "
                    "``kms_key.grant_encrypt_decrypt(role)`` on the literal "
                    "Analytics_KMS_Key ARN, (6) ``<StudioOnlyBucket.Arn>/*`` "
                    "object-key wildcard on the single literal bucket, and "
                    "(7) ``ssm:GetParameter`` on "
                    "``/gco/cluster-shared-bucket/*`` in the global region "
                    "(covers exactly three literal parameter names — "
                    "name/arn/region — defined by ``GCOGlobalStack``; lets "
                    "Studio notebooks resolve the shared-bucket metadata at "
                    "runtime without a per-user export step), (8) "
                    "``sagemaker-mlflow:*`` on MLflow tracking server and "
                    "MLflow App ARN wildcards in the api-gateway region + "
                    "account so notebooks can log experiments, runs, "
                    "metrics, artifacts, and registered-model versions "
                    "via the MLflow SDK's SigV4 plug-in (the companion "
                    "``sagemaker:*Mlflow*`` / ``sagemaker:*ModelPackage*`` "
                    "control-plane actions are now attached via the "
                    "``AmazonSageMakerFullAccess`` managed policy instead "
                    "of enumerated inline), and (9) "
                    "``sts:GetCallerIdentity`` on ``*`` which is required "
                    "by MLflow's SigV4 plug-in and does not support "
                    "resource-level scoping. Each wildcard is scoped on a "
                    "narrow literal pattern."
                ),
                applies_to=applies_to,
            ),
            # SageMaker execution role does not require MFA — callers reach
            # the role through Cognito-gated presigned URLs rather
            # than direct AssumeRole calls from operator terminals.
            NagSuppression(
                id="AwsSolutions-IAM4",
                reason=(
                    "SageMaker_Execution_Role does not attach AWS managed "
                    "policies. The role is assumed only by sagemaker.amazonaws.com "
                    "and used exclusively by notebooks running inside the "
                    "Studio domain."
                ),
            ),
            # The Studio domain itself — VpcOnly network mode is the
            # primary security control; additional HIPAA/NIST checks that
            # assume a customer-managed image (``AwsSolutions-SM2`` etc.)
            # are suppressed because this deployment intentionally uses
            # the stock AWS-published SageMaker Distribution images.
            NagSuppression(
                id="AwsSolutions-SM2",
                reason=(
                    "The Studio domain uses AWS-published stock SageMaker "
                    "Distribution images and does not define custom "
                    "images or app image configs. Per-user EFS access points "
                    "give POSIX isolation without a custom image."
                ),
            ),
            NagSuppression(
                id="AwsSolutions-SM3",
                reason=(
                    "SageMaker Studio domain is provisioned with "
                    "``app_network_access_type=VpcOnly`` — all Studio traffic "
                    "stays on the analytics stack's private-isolated VPC. "
                    "Direct internet access is structurally unavailable."
                ),
            ),
        ],
    )

    # The separate SagemakerClusterSharedBucketGrant inline Policy (a
    # sibling construct to the role, created by
    # ``_grant_sagemaker_role_on_cluster_shared_bucket``) has its own
    # ``<ReadClusterSharedBucketArn*.Parameter.Value>/*`` object-key
    # wildcard on the literal cluster-shared bucket ARN resolved from
    # cross-region SSM. Resource-level scoping isn't possible here —
    # the parent role's resource suppression has ``apply_to_children``
    # semantics that only traverse CDK children, not siblings.
    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-IAM5",
                reason=(
                    "SagemakerClusterSharedBucketGrant attaches the RW "
                    "policy on the single literal Cluster_Shared_Bucket "
                    "ARN resolved from /gco/cluster-shared-bucket/arn. "
                    "The ``<arn>/*`` object-key wildcard covers every "
                    "object key inside the single always-on "
                    "gco-cluster-shared-<account>-<region> bucket, "
                    "identical in shape and intent to the regional stack's "
                    "analogous job-pod grant."
                ),
                applies_to=[
                    "Resource::<ReadClusterSharedBucketArn4B0BD291.Parameter.Value>/*",
                ],
            ),
        ],
    )


def add_cognito_suppressions(stack: Stack) -> None:
    """Add suppressions for Cognito user pool findings.

    Most Cognito-related checks are handled by
    ``advanced_security_mode=ENFORCED`` and the password-policy
    configuration set on the pool itself. Only a small number of
    structural findings need an explicit suppression — these are the ones
    that don't apply to a machine-to-machine + presigned-URL model where
    there is no hosted UI callback to harden.
    """
    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-COG3",
                reason=(
                    "The Cognito user pool has ``advanced_security_mode=ENFORCED`` "
                    "which provides adaptive risk-based authentication, replacing "
                    "the need for an additional MFA enforcement step at this level. "
                    "Admins add MFA via ``gco analytics users add --require-mfa`` "
                    "when required."
                ),
            ),
            # COG2 is WARN-level: "The Cognito user pool does not require
            # MFA." MFA is configured at the per-user level through the
            # ``gco analytics users add --require-mfa`` CLI path rather
            # than being enforced pool-wide; enforcing it at the pool
            # level would lock out admins bootstrapping the first user
            # during initial deploy.
            NagSuppression(
                id="AwsSolutions-COG2",
                reason=(
                    "MFA is managed per-user through the ``gco analytics "
                    "users add --require-mfa`` CLI command rather than "
                    "enforced pool-wide. ``advanced_security_mode=ENFORCED`` "
                    "provides adaptive risk-based authentication that "
                    "triggers MFA challenges on suspicious sign-in attempts. "
                    "Pool-wide MFA enforcement would lock out the first "
                    "admin bootstrapping user during initial deploy."
                ),
            ),
        ],
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
            # Flow-logs suppressions — analytics VPC is private-isolated,
            # has no IGW/NAT, and every egress path is a VPC endpoint. The
            # service endpoints already emit CloudTrail data events that
            # cover every packet-producing API call on the VPC.
            NagSuppression(
                id="AwsSolutions-VPC7",
                reason=(
                    "The analytics VPC is private-isolated (no IGW, no "
                    "NAT Gateway). All egress flows through VPC interface/"
                    "gateway endpoints for SageMaker, S3, STS, Logs, ECR, "
                    "and EFS, each of which emits CloudTrail data events. "
                    "Flow logs would duplicate that telemetry at "
                    "significant storage cost without adding visibility."
                ),
            ),
            NagSuppression(
                id="HIPAA.Security-VPCFlowLogsEnabled",
                reason=(
                    "The analytics VPC is private-isolated (no IGW, no "
                    "NAT Gateway). All egress flows through VPC interface/"
                    "gateway endpoints for SageMaker, S3, STS, Logs, ECR, "
                    "and EFS, each of which emits CloudTrail data events. "
                    "Flow logs would duplicate that telemetry at "
                    "significant storage cost without adding visibility."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-VPCFlowLogsEnabled",
                reason=(
                    "The analytics VPC is private-isolated (no IGW, no "
                    "NAT Gateway). All egress flows through VPC interface/"
                    "gateway endpoints for SageMaker, S3, STS, Logs, ECR, "
                    "and EFS, each of which emits CloudTrail data events. "
                    "Flow logs would duplicate that telemetry at "
                    "significant storage cost without adding visibility."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-VPCFlowLogsEnabled",
                reason=(
                    "The analytics VPC is private-isolated (no IGW, no "
                    "NAT Gateway). All egress flows through VPC interface/"
                    "gateway endpoints for SageMaker, S3, STS, Logs, ECR, "
                    "and EFS, each of which emits CloudTrail data events. "
                    "Flow logs would duplicate that telemetry at "
                    "significant storage cost without adding visibility."
                ),
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
    2. ``AnalyticsAccessLogsBucket`` — SSE-S3 encrypted because S3
       server-access-log delivery to a KMS-encrypted bucket requires
       additional log-delivery role plumbing that the CDK ``s3.Bucket``
       construct does not wire automatically. Replication is not enabled
       because the bucket is the log sink, not a data store.
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
            # Access-logs bucket KMS encryption suppressions — SSE-S3 is
            # the AWS-documented pattern for server-access-log delivery
            # sinks. Switching to SSE-KMS would require an additional
            # log-delivery role that the CDK ``s3.Bucket`` construct does
            # not wire automatically.
            NagSuppression(
                id="HIPAA.Security-S3DefaultEncryptionKMS",
                reason=(
                    "The analytics access-logs bucket uses SSE-S3 because "
                    "S3 server-access-log delivery to a KMS-encrypted "
                    "bucket requires an additional log-delivery role "
                    "plumbing that CDK does not wire by default. Studio_"
                    "Only_Bucket (the actual data bucket) IS KMS-encrypted "
                    "with ``Analytics_KMS_Key``."
                ),
            ),
            NagSuppression(
                id="NIST.800.53.R5-S3DefaultEncryptionKMS",
                reason=(
                    "The analytics access-logs bucket uses SSE-S3 because "
                    "S3 server-access-log delivery to a KMS-encrypted "
                    "bucket requires an additional log-delivery role "
                    "plumbing that CDK does not wire by default. Studio_"
                    "Only_Bucket (the actual data bucket) IS KMS-encrypted "
                    "with ``Analytics_KMS_Key``."
                ),
            ),
            NagSuppression(
                id="PCI.DSS.321-S3DefaultEncryptionKMS",
                reason=(
                    "The analytics access-logs bucket uses SSE-S3 because "
                    "S3 server-access-log delivery to a KMS-encrypted "
                    "bucket requires an additional log-delivery role "
                    "plumbing that CDK does not wire by default. Studio_"
                    "Only_Bucket (the actual data bucket) IS KMS-encrypted "
                    "with ``Analytics_KMS_Key``."
                ),
            ),
        ],
    )


def add_presigned_url_lambda_suppressions(
    stack: Stack, api_gateway_region: str | None = None
) -> None:
    """Add suppressions for the analytics presigned-URL Lambda role.

    The Lambda needs wildcard access to SageMaker domain and user-profile
    ARNs because ``CreatePresignedDomainUrl``, ``DescribeUserProfile``,
    and ``CreateUserProfile`` all take ARN shapes that can only be
    resolved at invoke time from the incoming Cognito username. At synth
    time, ``domain/*`` and ``user-profile/*/*`` are the tightest literal
    ARN shapes we can bind in the IAM policy.
    """
    region = api_gateway_region or "*"
    acknowledge_nag_findings(
        stack,
        [
            NagSuppression(
                id="AwsSolutions-IAM5",
                reason=(
                    "The presigned-URL Lambda role uses SageMaker ARN "
                    "wildcards on ``domain/*`` and ``user-profile/*/*`` "
                    "because DomainId and UserProfileName are only "
                    "resolvable at invoke time from the incoming Cognito "
                    "username. ``ListDomains`` does not support resource-"
                    "level scoping — the AWS API only accepts Resource::* "
                    "— so a ``Resource::*`` suppression is required for "
                    "that specific action. The effective blast radius is "
                    "a single paginated list call per Lambda invocation "
                    "against this account's SageMaker control plane in "
                    "the api-gateway region."
                ),
                applies_to=[
                    "Resource::*",
                    (f"Resource::arn:aws:sagemaker:{region}:<AWS::AccountId>:domain/*"),
                    (f"Resource::arn:aws:sagemaker:{region}:<AWS::AccountId>:user-profile/*/*"),
                    # Generic shapes — catch tokenized-region variants
                    # (``<AWS::Region>``) produced when CDK synthesizes
                    # the policy without pinning the stack's env region.
                    ("Resource::arn:aws:sagemaker:<AWS::Region>:<AWS::AccountId>:domain/*"),
                    ("Resource::arn:aws:sagemaker:<AWS::Region>:<AWS::AccountId>:user-profile/*/*"),
                ],
            ),
        ],
    )


def add_emr_serverless_suppressions(stack: Stack) -> None:
    """Add suppressions for EMR Serverless Application findings.

    EMR Serverless doesn't have the same set of nag rules as EKS or Lambda;
    the main structural findings relate to the application's network
    configuration (which we pin to the private-isolated subnets + a
    dedicated SG) and the release-label pinning (covered by a constant in
    ``gco.stacks.constants``).
    """
    acknowledge_nag_findings(
        stack,
        [
            # Placeholder — EMR Serverless currently has no nag rules that
            # fire on a plain ``CfnApplication`` built against private
            # subnets. This helper exists so the analytics branch in
            # ``apply_all_suppressions`` has a single, predictable entry
            # point for EMR Serverless — future EMR-related rules land
            # here without touching the branch dispatch.
            NagSuppression(
                id="AwsSolutions-EMR1",
                reason=(
                    "EMR Serverless application is created with explicit "
                    "private-isolated subnet ids and a dedicated security "
                    "group — the application never lands on public subnets."
                ),
            ),
        ],
    )


def apply_all_suppressions(
    stack: Stack,
    stack_type: str = "regional",
    regions: list[str] | None = None,
    global_region: str | None = None,
    api_gateway_region: str | None = None,
) -> None:
    """Apply all relevant suppressions to a stack.

    Args:
        stack: The CDK stack to apply suppressions to
        stack_type: Type of stack - 'regional', 'global', 'api_gateway',
            'monitoring', or 'analytics'
        regions: List of regional deployment regions (for dynamic IAM suppression patterns)
        global_region: Global region for SSM parameters (for dynamic IAM suppression patterns)
        api_gateway_region: API Gateway region (for analytics stack — used to
            scope SageMaker execute-api and presigned-URL Lambda ARN patterns)
    """
    # Common suppressions for all stacks
    add_lambda_suppressions(stack)
    add_iam_suppressions(
        stack,
        regions=regions,
        global_region=global_region,
        api_gateway_region=api_gateway_region,
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

    elif stack_type == "monitoring":
        add_monitoring_suppressions(stack)

    elif stack_type == "analytics":
        # Analytics stack has S3 buckets (Studio_Only + access-logs), KMS,
        # EFS, Cognito, SageMaker, EMR Serverless, and the presigned-URL
        # Lambda. Each helper scopes its own applies_to list.
        add_storage_suppressions(stack)
        add_sagemaker_suppressions(
            stack,
            api_gateway_region=api_gateway_region,
            global_region=global_region,
        )
        add_cognito_suppressions(stack)
        add_emr_serverless_suppressions(stack)
        add_analytics_vpc_suppressions(stack)
        add_analytics_s3_suppressions(stack)
        add_presigned_url_lambda_suppressions(stack, api_gateway_region=api_gateway_region)
