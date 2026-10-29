"""The AWS PCS login node behind ``gco slurm shell`` / ``output --backend pcs``.

The Slinky backend's login surface is a pod the CLI reaches with ``kubectl
exec`` through the cluster tunnel (:mod:`cli.slurm`). The PCS backend's is an
EC2 instance: the always-on node of the managed cluster's ``login`` compute
node group, which mounts the same ``/home`` (and ``/scratch``) the compute
nodes do and carries every Slurm client. GCO reaches it over AWS Systems
Manager, the way PCS documents connecting to a login node, so no SSH key, no
public address and no bastion are involved:

* The node is found from the regional stack's outputs (``PcsClusterId``,
  ``PcsLoginNodeGroupId``) and the ``aws:pcs:compute-node-group-id`` tag PCS
  puts on every instance it launches.
* An interactive shell and ``--follow`` run ``aws ssm start-session`` with
  the ``AWS-StartInteractiveCommand`` document in the foreground (the
  Session Manager plugin the ``--via-ssm`` tunnel already needs), as the
  API's Slurm identity so ``sbatch`` and ``squeue`` act as it.
* A one-shot read (``cat``, ``tail -n``) goes through ``ssm:SendCommand``
  with ``AWS-RunShellScript`` and ``GetCommandInvocation`` polling: no plugin,
  no terminal banner in the captured text, and a JSON-friendly result.
  SSM keeps the first 24,000 characters of a command's stdout; a longer file
  is reported as truncated so the caller can ask for ``--tail``.

Everything that shells out is built as an argv list and validated first; the
command strings handed to SSM are ``shlex``-quoted.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from gco.pcs_config import (
    PCS_NODE_GROUP_INSTANCE_TAG,
    PCS_OUTPUT_CLUSTER_ID,
    PCS_OUTPUT_LOGIN_NODE_GROUP_ID,
    PCS_OUTPUT_MODE,
    POSIX_NAME_PATTERN,
)

#: SSM documents: an interactive command in a Session Manager session, and a
#: shell script run by the agent whose output ``GetCommandInvocation`` returns.
INTERACTIVE_DOCUMENT = "AWS-StartInteractiveCommand"
RUN_SHELL_DOCUMENT = "AWS-RunShellScript"
#: What SSM appends to ``StandardOutputContent`` past its 24,000-character cap.
OUTPUT_TRUNCATED_MARKER = "---Output truncated---"
_TERMINAL_STATUSES = frozenset({"Success", "Failed", "Cancelled", "TimedOut", "Cancelling"})
_INSTANCE_RE = re.compile(r"^i-[0-9a-f]{8}([0-9a-f]{9})?$")
_REGION_RE = re.compile(r"^[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+$")

ClientFactory = Callable[..., Any]


class PcsLoginError(RuntimeError):
    """The login node cannot be found or used; the message says what to do."""


@dataclass(frozen=True)
class LoginNode:
    """The PCS login node of one Region."""

    region: str
    cluster_id: str
    node_group_id: str
    instance_id: str


@dataclass(frozen=True)
class CapturedOutput:
    """A one-shot read's text and whether SSM cut it at its output cap."""

    text: str
    truncated: bool


def _client(factory: ClientFactory, service: str, region: str) -> Any:
    return factory(service, region_name=region)


def stack_outputs(
    region: str, stack_name: str, *, client_factory: ClientFactory = boto3.client
) -> dict[str, str]:
    """``OutputKey -> OutputValue`` of the regional stack, ``{}`` when it does not exist."""
    cfn = _client(client_factory, "cloudformation", region)
    try:
        response = cfn.describe_stacks(StackName=stack_name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        message = exc.response.get("Error", {}).get("Message", "")
        if code == "ValidationError" and "does not exist" in message:
            return {}
        raise PcsLoginError(f"could not read stack {stack_name} in {region}: {exc}") from exc
    stacks = response.get("Stacks") or []
    if not stacks:
        return {}
    return {
        str(output.get("OutputKey")): str(output.get("OutputValue"))
        for output in stacks[0].get("Outputs") or []
        if output.get("OutputKey")
    }


def find_login_node(
    region: str, stack_name: str, *, client_factory: ClientFactory = boto3.client
) -> LoginNode:
    """The running login node of the managed PCS cluster in ``region``.

    Raises :class:`PcsLoginError` with the remedy when PCS is not deployed
    there, when the cluster is operator-run (``pcs.mode: existing``, so GCO
    manages no login node), when the login node group is disabled, or when
    its instance is not running yet (PCS launches it right after the deploy).
    """
    if not _REGION_RE.match(region):
        raise PcsLoginError(f"invalid AWS region {region!r}")
    outputs = stack_outputs(region, stack_name, client_factory=client_factory)
    cluster_id = outputs.get(PCS_OUTPUT_CLUSTER_ID)
    if not cluster_id:
        raise PcsLoginError(
            f"AWS PCS is not deployed in {region} (stack {stack_name} publishes no "
            f"{PCS_OUTPUT_CLUSTER_ID}); turn it on with `gco stacks pcs enable` and deploy"
        )
    node_group_id = outputs.get(PCS_OUTPUT_LOGIN_NODE_GROUP_ID)
    if not node_group_id:
        if outputs.get(PCS_OUTPUT_MODE) == "existing":
            raise PcsLoginError(
                f"the PCS cluster {cluster_id} in {region} is operator-run (pcs.mode existing), "
                "so GCO manages no login node there; open a session on your own login node with "
                "`aws ssm start-session --target <instance-id>` and use the Slurm clients directly"
            )
        raise PcsLoginError(
            f"the managed PCS cluster {cluster_id} in {region} has no login node "
            "(pcs.managed.login.enabled is false); enable it and deploy"
        )
    ec2 = _client(client_factory, "ec2", region)
    instances: list[dict[str, Any]] = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[
            {"Name": f"tag:{PCS_NODE_GROUP_INSTANCE_TAG}", "Values": [node_group_id]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    ):
        for reservation in page.get("Reservations") or []:
            instances.extend(reservation.get("Instances") or [])
    if not instances:
        raise PcsLoginError(
            f"the PCS login node in {region} is not running yet (compute node group "
            f"{node_group_id} has no running instance); PCS launches it within a few minutes "
            "of the deploy, retry shortly or check the node group in the PCS console"
        )
    # The newest instance when PCS is replacing the node (a template change
    # rolls it with its drain strategy): the one that will stay.
    instances.sort(key=lambda item: str(item.get("LaunchTime") or ""), reverse=True)
    instance_id = str(instances[0].get("InstanceId") or "")
    if not _INSTANCE_RE.match(instance_id):
        raise PcsLoginError(f"PCS reported an unexpected login instance id {instance_id!r}")
    return LoginNode(
        region=region, cluster_id=cluster_id, node_group_id=node_group_id, instance_id=instance_id
    )


# ---------------------------------------------------------------------------
# Interactive sessions (aws ssm start-session, Session Manager plugin)
# ---------------------------------------------------------------------------


def _session_command(node: LoginNode, command: str) -> list[str]:
    return [
        "aws",
        "ssm",
        "start-session",
        "--target",
        node.instance_id,
        "--region",
        node.region,
        "--document-name",
        INTERACTIVE_DOCUMENT,
        "--parameters",
        json.dumps({"command": [command]}),
    ]


def shell_command(node: LoginNode, *, user: str) -> list[str]:
    """``aws ssm start-session`` argv for an interactive login shell as ``user``.

    Session Manager sessions start as ``ssm-user``, which the agent gives
    passwordless sudo; ``sudo -iu <user>`` turns that into a login shell as
    the API's Slurm identity, whose home the lifecycle action created on the
    shared ``/home``, so ``sbatch`` and ``squeue`` act as it.
    """
    if not POSIX_NAME_PATTERN.match(user):
        raise PcsLoginError(f"refusing to open a shell as {user!r}: not a POSIX user name")
    return _session_command(node, f"sudo -iu {shlex.quote(user)}")


def follow_command(node: LoginNode, path: str) -> list[str]:
    """``aws ssm start-session`` argv streaming ``tail -f path`` as root until Ctrl-C."""
    return _session_command(node, "sudo tail -f " + shlex.quote(path))


# ---------------------------------------------------------------------------
# One-shot reads (ssm:SendCommand + GetCommandInvocation)
# ---------------------------------------------------------------------------


def read_file(
    node: LoginNode,
    reader: Sequence[str],
    *,
    client_factory: ClientFactory = boto3.client,
    timeout: float = 120.0,
    poll_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> CapturedOutput:
    """Run ``reader`` (``cat path`` / ``tail -n N path``) on the node and return its stdout.

    ``AWS-RunShellScript`` runs as root, so the identity's files are readable
    without sudo. The invocation is polled until it finishes; a run that is
    not ``Success`` raises :class:`PcsLoginError` with the command's stderr.
    """
    ssm = _client(client_factory, "ssm", node.region)
    response = ssm.send_command(
        InstanceIds=[node.instance_id],
        DocumentName=RUN_SHELL_DOCUMENT,
        Parameters={"commands": [shlex.join(reader)]},
        TimeoutSeconds=max(30, int(timeout)),
        Comment="gco slurm output (PCS login node)",
    )
    command_id = str(response["Command"]["CommandId"])
    deadline = clock() + timeout
    invocation: Mapping[str, Any] | None = None
    while True:
        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=node.instance_id
            )
        except ClientError as exc:
            # The invocation is registered a moment after SendCommand returns.
            if exc.response.get("Error", {}).get("Code", "") != "InvocationDoesNotExist":
                raise PcsLoginError(f"could not read the command's result: {exc}") from exc
            invocation = None
        if invocation is not None and str(invocation.get("Status")) in _TERMINAL_STATUSES:
            break
        if clock() >= deadline:
            raise PcsLoginError(
                f"timed out after {timeout:.0f}s waiting for {reader[0]} on the PCS login node "
                f"{node.instance_id} (SSM command {command_id})"
            )
        sleep(poll_interval)
    status = str(invocation.get("Status"))
    if status != "Success":
        stderr = str(invocation.get("StandardErrorContent") or "").strip()
        raise PcsLoginError(
            f"{shlex.join(reader)} failed on the PCS login node {node.instance_id} ({status})"
            + (f": {stderr}" if stderr else "")
        )
    text = str(invocation.get("StandardOutputContent") or "")
    truncated = OUTPUT_TRUNCATED_MARKER in text
    if truncated:
        text = text.split(OUTPUT_TRUNCATED_MARKER, 1)[0]
    return CapturedOutput(text=text, truncated=truncated)
