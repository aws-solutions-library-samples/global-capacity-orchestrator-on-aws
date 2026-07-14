"""Ephemeral SSM bastion lifecycle for reaching a private EKS API endpoint.

``gco monitoring open --via-ssm <instance-id>`` tunnels to a private EKS
endpoint through an *existing* SSM-managed instance. This module lets the CLI
**create that instance on demand** — a minimal, self-terminating ``t3.micro`` in
the cluster VPC — and tear it down when the port-forward session ends, so an
operator doesn't have to keep a standing bastion around just to view Grafana.

Orphan safeguards (defence in depth — a crash, ``Ctrl-C``, or a forgotten
teardown must never leave a paid instance running):

* launched with ``--instance-initiated-shutdown-behavior terminate``;
* user-data schedules ``shutdown -h +<ttl>`` as an unconditional backstop;
* IMDSv2 required (``HttpTokens=required``);
* tagged ``gco:ephemeral=true`` + ``gco:purpose`` so any orphan is greppable;
* the CLI tears it down in a ``finally:`` block.

Network posture: the instance reuses the cluster's own security group (which is
self-referencing, so it can reach the private API endpoint) and is placed in a
public subnet with a public IP purely so the SSM agent can reach the Systems
Manager service. **No inbound ports are opened** — SSM is agent-initiated
outbound only — so the public IP is not an ingress surface.

Style matches :mod:`cli.ssm_tunnel`: pure, validated argv builders (list form,
never a shell string) that are fully unit-testable, plus thin runtime wrappers
that shell out to the AWS CLI (the ``cli`` package does not depend on boto3).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants — the fixed identity of the ephemeral bastion.
# --------------------------------------------------------------------------

# The bastion's IAM role, instance profile, and Name tag are scoped to the
# deployment's project key (cdk.json context.project_name, default "gco" — the
# same key cli/config.py derives cluster and stack names from), so a non-default
# deployment addresses its own resources and two differently named deployments in
# one account don't collide on a shared name. See bastion_role_name() below.
DEFAULT_PROJECT_NAME = "gco"
_ROLE_NAME_SUFFIX = "-ephemeral-bastion-role"
_PROFILE_NAME_SUFFIX = "-ephemeral-bastion-profile"
_INSTANCE_NAME_SUFFIX = "-ephemeral-ssm-bastion"

# AmazonSSMManagedInstanceCore is an AWS-managed policy — a fixed global ARN, not
# a resource GCO names — so it is intentionally not project-scoped.
SSM_MANAGED_POLICY_ARN = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"

BASTION_INSTANCE_TYPE = "t3.micro"

# Public SSM parameter that always resolves to the latest Amazon Linux 2023
# x86_64 AMI in the target region (t3.micro is x86_64).
AL2023_AMI_SSM_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"

# Tags stamped on every ephemeral instance so orphans are trivially discoverable:
#   aws ec2 describe-instances \
#     --filters Name=tag:gco:ephemeral,Values=true \
#               Name=instance-state-name,Values=running,pending
TAG_EPHEMERAL_KEY = "gco:ephemeral"
TAG_PURPOSE_KEY = "gco:purpose"
TAG_TTL_KEY = "gco:ttl-minutes"
BASTION_PURPOSE = "cluster-observability"

DEFAULT_TTL_MINUTES = 120

# EC2 instance-profile propagation to the RunInstances API is eventually
# consistent; retry the launch for a short window on the "not found" error.
_PROFILE_PROPAGATION_RETRIES = 6
_PROFILE_PROPAGATION_WAIT_SECONDS = 5.0

# EC2 trust policy: only the EC2 service may assume the bastion role.
BASTION_TRUST_POLICY: dict[str, object] = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}

# --------------------------------------------------------------------------
# Validators — every AWS-supplied id is re-validated before it enters an argv.
# --------------------------------------------------------------------------

_REGION_RE = re.compile(r"^[a-z]{2,3}-[a-z]+-\d+$")
_CLUSTER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,99}$")
_INSTANCE_RE = re.compile(r"^i-[0-9a-f]{8}([0-9a-f]{9})?$")
_VPC_RE = re.compile(r"^vpc-[0-9a-f]{8}([0-9a-f]{9})?$")
_SUBNET_RE = re.compile(r"^subnet-[0-9a-f]{8}([0-9a-f]{9})?$")
_SG_RE = re.compile(r"^sg-[0-9a-f]{8}([0-9a-f]{9})?$")
_AMI_RE = re.compile(r"^ami-[0-9a-f]{8}([0-9a-f]{9})?$")


def _validate(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(f"Invalid {what}: {value!r}")
    return value


def _validate_ttl(ttl_minutes: int) -> int:
    try:
        value = int(ttl_minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ttl-minutes {ttl_minutes!r}: must be an integer") from exc
    if not 5 <= value <= 1440:
        raise ValueError(f"Invalid ttl-minutes {value}: must be between 5 and 1440")
    return value


# Project keys follow the cdk.json context.project_name shape (alnum + hyphen).
_PROJECT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,40}$")


def _validate_project(project_name: str) -> str:
    return _validate(project_name, _PROJECT_RE, "project name")


def bastion_role_name(project_name: str = DEFAULT_PROJECT_NAME) -> str:
    """IAM role name for ``project_name``'s ephemeral bastion."""
    return f"{_validate_project(project_name)}{_ROLE_NAME_SUFFIX}"


def bastion_profile_name(project_name: str = DEFAULT_PROJECT_NAME) -> str:
    """Instance-profile name for ``project_name``'s ephemeral bastion."""
    return f"{_validate_project(project_name)}{_PROFILE_NAME_SUFFIX}"


def bastion_instance_name(project_name: str = DEFAULT_PROJECT_NAME) -> str:
    """EC2 ``Name`` tag for ``project_name``'s ephemeral bastion."""
    return f"{_validate_project(project_name)}{_INSTANCE_NAME_SUFFIX}"


# Back-compat convenience constants for the default project. The helpers above
# are the source of truth; these are the default-project values that the builder
# defaults (and tests) reference.
BASTION_ROLE_NAME = bastion_role_name()
BASTION_PROFILE_NAME = bastion_profile_name()
BASTION_NAME = bastion_instance_name()


@dataclass(frozen=True)
class BastionNetwork:
    """The VPC placement an ephemeral bastion needs to reach the private API."""

    vpc_id: str
    subnet_id: str
    security_group_id: str
    # True when ``subnet_id`` auto-assigns public IPs (SSM egress over the IGW).
    public_subnet: bool


# --------------------------------------------------------------------------
# Pure argv / payload builders (unit-tested; list form, no shell).
# --------------------------------------------------------------------------


def render_user_data(ttl_minutes: int = DEFAULT_TTL_MINUTES) -> str:
    """Return the bastion boot script: an unconditional self-terminate backstop."""
    ttl = _validate_ttl(ttl_minutes)
    return (
        "#!/bin/bash\n"
        "# GCO ephemeral SSM bastion — self-terminate backstop.\n"
        "# The instance is launched with --instance-initiated-shutdown-behavior\n"
        "# terminate, so this scheduled halt terminates it even if no explicit\n"
        "# teardown ever runs.\n"
        f'shutdown -h +{ttl} "gco ephemeral bastion self-terminate backstop"\n'
    )


def build_get_ami_command(region: str) -> list[str]:
    """``aws ssm get-parameter`` argv resolving the latest AL2023 x86_64 AMI."""
    _validate(region, _REGION_RE, "region")
    return [
        "aws",
        "ssm",
        "get-parameter",
        "--name",
        AL2023_AMI_SSM_PARAMETER,
        "--region",
        region,
        "--query",
        "Parameter.Value",
        "--output",
        "text",
    ]


def build_describe_cluster_network_command(cluster: str, region: str) -> list[str]:
    """``aws eks describe-cluster`` argv projecting VPC, cluster SG, and subnets."""
    _validate(cluster, _CLUSTER_RE, "cluster name")
    _validate(region, _REGION_RE, "region")
    return [
        "aws",
        "eks",
        "describe-cluster",
        "--name",
        cluster,
        "--region",
        region,
        "--query",
        ("cluster.resourcesVpcConfig.{vpc:vpcId,sg:clusterSecurityGroupId,subnets:subnetIds}"),
        "--output",
        "json",
    ]


def build_describe_public_subnet_command(vpc_id: str, region: str) -> list[str]:
    """``aws ec2 describe-subnets`` argv for the first public subnet in ``vpc_id``."""
    _validate(vpc_id, _VPC_RE, "vpc id")
    _validate(region, _REGION_RE, "region")
    return [
        "aws",
        "ec2",
        "describe-subnets",
        "--region",
        region,
        "--filters",
        f"Name=vpc-id,Values={vpc_id}",
        "Name=map-public-ip-on-launch,Values=true",
        "--query",
        "Subnets[0].SubnetId",
        "--output",
        "text",
    ]


def build_create_role_command(role_name: str = BASTION_ROLE_NAME) -> list[str]:
    """``aws iam create-role`` argv with the EC2 trust policy."""
    _validate(role_name, _CLUSTER_RE, "role name")
    return [
        "aws",
        "iam",
        "create-role",
        "--role-name",
        role_name,
        "--assume-role-policy-document",
        json.dumps(BASTION_TRUST_POLICY),
        "--description",
        "GCO ephemeral SSM bastion (cluster-observability); safe to delete.",
        "--tags",
        f"Key={TAG_EPHEMERAL_KEY},Value=true",
        f"Key={TAG_PURPOSE_KEY},Value={BASTION_PURPOSE}",
    ]


def build_attach_role_policy_command(role_name: str = BASTION_ROLE_NAME) -> list[str]:
    """``aws iam attach-role-policy`` argv attaching AmazonSSMManagedInstanceCore."""
    _validate(role_name, _CLUSTER_RE, "role name")
    return [
        "aws",
        "iam",
        "attach-role-policy",
        "--role-name",
        role_name,
        "--policy-arn",
        SSM_MANAGED_POLICY_ARN,
    ]


def build_create_instance_profile_command(profile_name: str = BASTION_PROFILE_NAME) -> list[str]:
    """``aws iam create-instance-profile`` argv."""
    _validate(profile_name, _CLUSTER_RE, "instance-profile name")
    return [
        "aws",
        "iam",
        "create-instance-profile",
        "--instance-profile-name",
        profile_name,
    ]


def build_add_role_to_profile_command(
    role_name: str = BASTION_ROLE_NAME, profile_name: str = BASTION_PROFILE_NAME
) -> list[str]:
    """``aws iam add-role-to-instance-profile`` argv."""
    _validate(role_name, _CLUSTER_RE, "role name")
    _validate(profile_name, _CLUSTER_RE, "instance-profile name")
    return [
        "aws",
        "iam",
        "add-role-to-instance-profile",
        "--instance-profile-name",
        profile_name,
        "--role-name",
        role_name,
    ]


def _tag_specification(ttl_minutes: int, instance_name: str = BASTION_NAME) -> str:
    """Build the ``--tag-specifications`` value stamping the ephemeral markers."""
    tags = (
        f"{{Key=Name,Value={instance_name}}},"
        f"{{Key={TAG_EPHEMERAL_KEY},Value=true}},"
        f"{{Key={TAG_PURPOSE_KEY},Value={BASTION_PURPOSE}}},"
        f"{{Key={TAG_TTL_KEY},Value={ttl_minutes}}}"
    )
    return f"ResourceType=instance,Tags=[{tags}]"


def build_run_instances_command(
    *,
    ami_id: str,
    instance_type: str,
    subnet_id: str,
    security_group_id: str,
    profile_name: str,
    region: str,
    user_data: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    associate_public_ip: bool = True,
    instance_name: str = BASTION_NAME,
) -> list[str]:
    """Build the validated ``aws ec2 run-instances`` argv, safeguards included.

    The safeguards (IMDSv2, shutdown-behaviour terminate, TTL user-data, and the
    ``gco:ephemeral`` tag set) are not optional — they are the contract that
    keeps a forgotten teardown from turning into a paid orphan.
    """
    _validate(ami_id, _AMI_RE, "AMI id")
    _validate(subnet_id, _SUBNET_RE, "subnet id")
    _validate(security_group_id, _SG_RE, "security group id")
    _validate(profile_name, _CLUSTER_RE, "instance-profile name")
    _validate(region, _REGION_RE, "region")
    ttl = _validate_ttl(ttl_minutes)
    if not re.match(r"^[a-z0-9]+\.[a-z0-9]+$", instance_type):
        raise ValueError(f"Invalid instance type {instance_type!r}")

    cmd = [
        "aws",
        "ec2",
        "run-instances",
        "--region",
        region,
        "--image-id",
        ami_id,
        "--instance-type",
        instance_type,
        "--count",
        "1",
        "--subnet-id",
        subnet_id,
        "--security-group-ids",
        security_group_id,
        "--iam-instance-profile",
        f"Name={profile_name}",
        # IMDSv2 required.
        "--metadata-options",
        "HttpTokens=required,HttpEndpoint=enabled",
        # Orphan safeguard #1: an OS-initiated shutdown terminates the instance.
        "--instance-initiated-shutdown-behavior",
        "terminate",
        # Orphan safeguard #2: schedule that shutdown from boot.
        "--user-data",
        user_data,
        # Orphan safeguard #3: greppable ephemeral tags.
        "--tag-specifications",
        _tag_specification(ttl, instance_name),
    ]
    if associate_public_ip:
        cmd.append("--associate-public-ip-address")
    else:
        cmd.append("--no-associate-public-ip-address")
    # Ask only for the instance id back.
    cmd += ["--query", "Instances[0].InstanceId", "--output", "text"]
    return cmd


def build_describe_ssm_ping_command(instance_id: str, region: str) -> list[str]:
    """``aws ssm describe-instance-information`` argv projecting the PingStatus."""
    _validate(instance_id, _INSTANCE_RE, "instance id")
    _validate(region, _REGION_RE, "region")
    return [
        "aws",
        "ssm",
        "describe-instance-information",
        "--region",
        region,
        "--filters",
        f"Key=InstanceIds,Values={instance_id}",
        "--query",
        "InstanceInformationList[0].PingStatus",
        "--output",
        "text",
    ]


def build_terminate_instances_command(instance_id: str, region: str) -> list[str]:
    """``aws ec2 terminate-instances`` argv."""
    _validate(instance_id, _INSTANCE_RE, "instance id")
    _validate(region, _REGION_RE, "region")
    return [
        "aws",
        "ec2",
        "terminate-instances",
        "--region",
        region,
        "--instance-ids",
        instance_id,
        "--query",
        "TerminatingInstances[0].CurrentState.Name",
        "--output",
        "text",
    ]


def build_iam_teardown_commands(
    role_name: str = BASTION_ROLE_NAME, profile_name: str = BASTION_PROFILE_NAME
) -> list[list[str]]:
    """Return the ordered IAM teardown argvs (profile disassociated before delete)."""
    _validate(role_name, _CLUSTER_RE, "role name")
    _validate(profile_name, _CLUSTER_RE, "instance-profile name")
    return [
        [
            "aws",
            "iam",
            "remove-role-from-instance-profile",
            "--instance-profile-name",
            profile_name,
            "--role-name",
            role_name,
        ],
        ["aws", "iam", "delete-instance-profile", "--instance-profile-name", profile_name],
        [
            "aws",
            "iam",
            "detach-role-policy",
            "--role-name",
            role_name,
            "--policy-arn",
            SSM_MANAGED_POLICY_ARN,
        ],
        ["aws", "iam", "delete-role", "--role-name", role_name],
    ]


# --------------------------------------------------------------------------
# Pure parsers (unit-tested).
# --------------------------------------------------------------------------


def parse_cluster_network(stdout: str) -> tuple[str, str, list[str]]:
    """Parse ``build_describe_cluster_network_command`` JSON → (vpc, sg, subnets)."""
    data = json.loads(stdout or "{}")
    vpc = data.get("vpc") or ""
    sg = data.get("sg") or ""
    subnets = data.get("subnets") or []
    if not vpc or not sg:
        raise RuntimeError(
            "Cluster VPC or cluster security group not found in describe-cluster output; "
            "cannot place an ephemeral bastion."
        )
    return vpc, sg, list(subnets)


def _clean_scalar(stdout: str) -> str:
    """Normalise an ``--output text`` scalar (strip; treat 'None' as empty)."""
    value = (stdout or "").strip()
    return "" if value in ("", "None") else value


# --------------------------------------------------------------------------
# Runtime wrappers (thin; shell out to the AWS CLI).
# --------------------------------------------------------------------------


def _run_aws(cmd: list[str], *, allow_exists: bool = False) -> str:
    """Run an AWS CLI argv, returning stdout. Raise ``RuntimeError`` on failure.

    ``allow_exists`` swallows idempotent "already exists" IAM errors so repeated
    runs reuse the standing role/profile instead of failing.
    """
    try:
        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - argv built by validated builders; list form, no shell=True
            cmd, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "AWS CLI not found. Please install the AWS CLI and ensure it's in your PATH."
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr or ""
        if allow_exists and ("EntityAlreadyExists" in stderr or "already exists" in stderr):
            return result.stdout or ""
        raise RuntimeError(f"AWS CLI command failed ({' '.join(cmd[:3])}): {stderr.strip()}")
    return result.stdout or ""


def resolve_bastion_ami(region: str) -> str:
    """Resolve the latest AL2023 x86_64 AMI id for ``region``."""
    ami = _clean_scalar(_run_aws(build_get_ami_command(region)))
    return _validate(ami, _AMI_RE, "resolved AMI id")


def resolve_bastion_network(cluster: str, region: str) -> BastionNetwork:
    """Discover the VPC, a usable subnet, and the cluster SG for the bastion.

    Prefers a public subnet (auto-assigned public IP for SSM egress). Falls back
    to the first cluster subnet (assumes NAT or SSM VPC endpoints) when the VPC
    exposes no public subnet.
    """
    vpc, sg, subnets = parse_cluster_network(
        _run_aws(build_describe_cluster_network_command(cluster, region))
    )
    _validate(vpc, _VPC_RE, "cluster vpc id")
    _validate(sg, _SG_RE, "cluster security group id")

    public_subnet = _clean_scalar(_run_aws(build_describe_public_subnet_command(vpc, region)))
    if public_subnet:
        return BastionNetwork(
            vpc, _validate(public_subnet, _SUBNET_RE, "public subnet id"), sg, True
        )

    if not subnets:
        raise RuntimeError(
            f"No public subnet and no cluster subnets found in VPC {vpc}; cannot place a bastion."
        )
    fallback = _validate(str(subnets[0]), _SUBNET_RE, "fallback subnet id")
    logger.warning(
        "No public subnet in %s; using cluster subnet %s without a public IP "
        "(requires NAT or SSM VPC endpoints for the agent to connect).",
        vpc,
        fallback,
    )
    return BastionNetwork(vpc, fallback, sg, False)


def ensure_bastion_iam(project_name: str = DEFAULT_PROJECT_NAME) -> None:
    """Idempotently create the bastion role + instance profile and wire them up."""
    role_name = bastion_role_name(project_name)
    profile_name = bastion_profile_name(project_name)
    _run_aws(build_create_role_command(role_name), allow_exists=True)
    _run_aws(build_attach_role_policy_command(role_name), allow_exists=True)
    _run_aws(build_create_instance_profile_command(profile_name), allow_exists=True)
    _run_aws(build_add_role_to_profile_command(role_name, profile_name), allow_exists=True)


def launch_bastion(
    *,
    network: BastionNetwork,
    ami_id: str,
    region: str,
    ttl_minutes: int,
    project_name: str = DEFAULT_PROJECT_NAME,
    instance_type: str = BASTION_INSTANCE_TYPE,
) -> str:
    """Run the instance, retrying briefly while the instance profile propagates."""
    cmd = build_run_instances_command(
        ami_id=ami_id,
        instance_type=instance_type,
        subnet_id=network.subnet_id,
        security_group_id=network.security_group_id,
        profile_name=bastion_profile_name(project_name),
        region=region,
        user_data=render_user_data(ttl_minutes),
        ttl_minutes=ttl_minutes,
        associate_public_ip=network.public_subnet,
        instance_name=bastion_instance_name(project_name),
    )
    last_error: Exception | None = None
    for attempt in range(_PROFILE_PROPAGATION_RETRIES):
        try:
            instance_id = _clean_scalar(_run_aws(cmd))
            return _validate(instance_id, _INSTANCE_RE, "launched instance id")
        except RuntimeError as exc:  # noqa: PERF203 — bounded retry loop
            last_error = exc
            if (
                "Invalid IAM Instance Profile" not in str(exc)
                and "instance profile" not in str(exc).lower()
            ):
                raise
            time.sleep(_PROFILE_PROPAGATION_WAIT_SECONDS)
            logger.info(
                "Instance profile not yet visible to EC2 (attempt %d); retrying launch.",
                attempt + 1,
            )
    raise RuntimeError(
        f"run-instances failed after {_PROFILE_PROPAGATION_RETRIES} attempts "
        f"waiting for instance-profile propagation: {last_error}"
    )


def wait_until_ssm_online(
    instance_id: str,
    region: str,
    *,
    timeout_seconds: float = 240.0,
    poll_interval_seconds: float = 10.0,
) -> None:
    """Block until the instance registers with SSM (PingStatus=Online) or time out."""
    deadline = time.monotonic() + timeout_seconds
    cmd = build_describe_ssm_ping_command(instance_id, region)
    while time.monotonic() < deadline:
        status = _clean_scalar(_run_aws(cmd))
        if status == "Online":
            return
        time.sleep(poll_interval_seconds)
    raise RuntimeError(
        f"Instance {instance_id} did not come Online in SSM within "
        f"{int(timeout_seconds)}s. Check that the instance can reach the SSM service "
        "(public subnet with egress, or SSM VPC endpoints)."
    )


def create_ephemeral_bastion(
    cluster: str,
    region: str,
    *,
    project_name: str = DEFAULT_PROJECT_NAME,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    wait_online: bool = True,
) -> str:
    """Provision a self-terminating SSM bastion in the cluster VPC. Returns its id.

    The IAM role / instance profile are named for ``project_name`` (default
    ``gco``) so they match the deployment's other project-scoped resources.
    """
    _validate(cluster, _CLUSTER_RE, "cluster name")
    _validate(region, _REGION_RE, "region")
    _validate_project(project_name)
    ttl = _validate_ttl(ttl_minutes)

    ami_id = resolve_bastion_ami(region)
    network = resolve_bastion_network(cluster, region)
    ensure_bastion_iam(project_name)
    instance_id = launch_bastion(
        network=network, ami_id=ami_id, region=region, ttl_minutes=ttl, project_name=project_name
    )
    logger.info("Launched ephemeral bastion %s in %s (%s).", instance_id, region, network.subnet_id)
    if wait_online:
        try:
            wait_until_ssm_online(instance_id, region)
        except Exception:
            # Atomic create: never leak the instance we just launched if it
            # fails to register with SSM. The self-terminate user-data is only a
            # last-resort backstop, not the normal cleanup path.
            try:
                destroy_ephemeral_bastion(instance_id, region, project_name=project_name)
            except Exception:  # pragma: no cover - best effort
                logger.exception(
                    "Failed to clean up bastion %s after online-wait failure", instance_id
                )
            raise
    return instance_id


def destroy_ephemeral_bastion(
    instance_id: str,
    region: str,
    *,
    project_name: str = DEFAULT_PROJECT_NAME,
    delete_iam: bool = True,
) -> None:
    """Terminate the bastion and (best-effort) delete its IAM role + profile.

    Termination is the cost-critical step and is always attempted. IAM cleanup is
    best-effort: a leftover role/instance-profile costs nothing and is greppable
    by its ``gco:ephemeral`` tag, so a failure here is logged, not raised. The
    role / profile deleted are the ones named for ``project_name``.
    """
    _validate(instance_id, _INSTANCE_RE, "instance id")
    _validate(region, _REGION_RE, "region")
    _validate_project(project_name)

    _run_aws(build_terminate_instances_command(instance_id, region))
    logger.info("Terminating ephemeral bastion %s.", instance_id)

    if not delete_iam:
        return
    teardown = build_iam_teardown_commands(
        bastion_role_name(project_name), bastion_profile_name(project_name)
    )
    for step in teardown:
        try:
            _run_aws(step, allow_exists=True)
        except RuntimeError as exc:  # best-effort — never mask the termination
            logger.warning("IAM teardown step failed (%s): %s", " ".join(step[:3]), exc)


@contextmanager
def ephemeral_bastion(
    cluster: str,
    region: str,
    *,
    project_name: str = DEFAULT_PROJECT_NAME,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
) -> Iterator[str]:
    """Context manager: create a bastion, yield its id, guarantee teardown."""
    instance_id = create_ephemeral_bastion(
        cluster, region, project_name=project_name, ttl_minutes=ttl_minutes
    )
    try:
        yield instance_id
    finally:
        destroy_ephemeral_bastion(instance_id, region, project_name=project_name)
