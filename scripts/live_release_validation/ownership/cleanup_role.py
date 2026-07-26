"""The delegated log-cleanup helper stack and its scoped IAM role."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

from botocore.exceptions import ClientError

from ..constants import (
    _LOG_CLEANUP_HELPER_NAMESPACE,
    _LOG_CLEANUP_HELPER_RUN_TAG,
    _LOG_CLEANUP_HELPER_STACK_PREFIX,
    _LOG_CLEANUP_HELPER_TOKEN_TAG,
    _LOG_CLEANUP_ROLE_OUTPUT,
    _LOG_CLEANUP_ROLE_POLICY_NAME,
    _LOG_CLEANUP_ROLE_RUN_TAG,
    _LOG_CLEANUP_ROLE_TOKEN_TAG,
    _LOG_CLEANUP_SESSION_SECONDS,
    _LOG_CLEANUP_STACK_POLL_ATTEMPTS,
    _LOG_CLEANUP_STACK_POLL_SECONDS,
    _LOG_CLEANUP_TOKEN_TAG,
    _RUN_STACK_TAG,
)
from ..inventory import (
    describe_stack,
)
from ..models import RunContext, utc_now
from ..ownership.log_groups import (
    _validated_owned_log_group_identity,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _cleanup_principal_identity(ctx: RunContext, caller_arn: str) -> dict[str, str]:
    """Resolve a renewable caller session to one immutable IAM principal."""
    region = ctx.config.global_region
    partition = ctx.session.get_partition_for_region(region)
    account = ctx.settings.expected_account
    if not partition:
        raise RuntimeError(f"Could not resolve AWS partition for cleanup authority in {region}")
    if not caller_arn or "*" in caller_arn:
        raise RuntimeError("Cleanup authority principal ARN is empty or contains a wildcard")

    iam = ctx.session.client("iam", region_name=region)
    iam_prefix = f"arn:{partition}:iam::{account}:"
    if caller_arn.startswith(f"{iam_prefix}user/"):
        user_name = caller_arn.rsplit("/", 1)[-1]
        user = iam.get_user(UserName=user_name).get("User")
        principal_arn = str((user or {}).get("Arn") or "")
        principal_id = str((user or {}).get("UserId") or "")
        if principal_arn != caller_arn or not principal_id:
            raise RuntimeError(f"IAM returned an invalid user identity for {caller_arn}")
        return {"arn": principal_arn, "principal_id": principal_id}

    if caller_arn.startswith(f"{iam_prefix}role/"):
        role_name = caller_arn.rsplit("/", 1)[-1]
        role = iam.get_role(RoleName=role_name).get("Role")
        principal_arn = str((role or {}).get("Arn") or "")
        principal_id = str((role or {}).get("RoleId") or "")
        if principal_arn != caller_arn or not principal_id:
            raise RuntimeError(f"IAM returned an invalid role identity for {caller_arn}")
        return {"arn": principal_arn, "principal_id": principal_id}

    assumed_prefix = f"arn:{partition}:sts::{account}:assumed-role/"
    if caller_arn.startswith(assumed_prefix):
        role_session = caller_arn.removeprefix(assumed_prefix)
        role_resource, separator, session_name = role_session.rpartition("/")
        role_name = role_resource.rsplit("/", 1)[-1]
        if not separator or not role_name or not session_name:
            raise RuntimeError(f"Malformed assumed-role caller ARN: {caller_arn}")
        role = iam.get_role(RoleName=role_name).get("Role")
        principal_arn = str((role or {}).get("Arn") or "")
        principal_id = str((role or {}).get("RoleId") or "")
        if (
            not principal_arn.startswith(f"{iam_prefix}role/")
            or principal_arn.rsplit("/", 1)[-1] != role_name
            or not principal_id
        ):
            raise RuntimeError(f"IAM returned an invalid underlying role identity for {caller_arn}")
        return {"arn": principal_arn, "principal_id": principal_id}
    raise RuntimeError(
        f"Log cleanup requires an exact IAM user or STS assumed-role caller; found {caller_arn}"
    )


def _log_cleanup_policy(
    ctx: RunContext,
    cleanup_token: str,
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    partitions: set[str] = set()
    for record in records:
        region, _name = _validated_owned_log_group_identity(ctx, record)
        partition = ctx.session.get_partition_for_region(region)
        if not partition:
            raise RuntimeError(f"Could not resolve AWS partition for log cleanup in {region}")
        partitions.add(partition)
    if len(partitions) != 1:
        raise RuntimeError("Log cleanup requires all authorized groups to share one AWS partition")
    partition = next(iter(partitions))
    return (
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "logs:DeleteLogGroup",
                    "Resource": (
                        f"arn:{partition}:logs:*:{ctx.settings.expected_account}:log-group:*"
                    ),
                    "Condition": {
                        "StringEquals": {
                            f"aws:ResourceTag/{_RUN_STACK_TAG}": ctx.settings.run_id,
                            f"aws:ResourceTag/{_LOG_CLEANUP_TOKEN_TAG}": cleanup_token,
                        }
                    },
                }
            ],
        },
        partition,
    )


def _log_cleanup_helper_spec(ctx: RunContext) -> dict[str, Any] | None:
    records = ctx.checkpoint.state.get("owned_log_groups", [])
    if not isinstance(records, list):
        raise RuntimeError("Checkpoint owned_log_groups must be a list")
    if not records:
        return None
    cleanup_token = str(ctx.checkpoint.state.get("log_group_cleanup_token") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", cleanup_token):
        raise RuntimeError("Checkpoint log-group cleanup token is malformed")
    if any(not isinstance(record, dict) for record in records):
        raise RuntimeError("Checkpoint owned_log_groups must contain objects")
    policy, partition = _log_cleanup_policy(ctx, cleanup_token, records)
    helper_region = str(ctx.config.global_region)
    if ctx.session.get_partition_for_region(helper_region) != partition:
        raise RuntimeError("Cleanup helper Region is outside the log groups' AWS partition")

    existing_helper = ctx.checkpoint.state.get("log_cleanup_helper")
    if existing_helper is not None and not isinstance(existing_helper, dict):
        raise RuntimeError("Checkpoint log_cleanup_helper must be an object")
    if isinstance(existing_helper, dict):
        first_caller_arn = str(existing_helper.get("first_caller_arn") or "")
        trusted_principal_arn = str(existing_helper.get("trusted_principal_arn") or "")
        trusted_principal_id = str(existing_helper.get("trusted_principal_id") or "")
        if not first_caller_arn or not trusted_principal_arn or not trusted_principal_id:
            raise RuntimeError("Checkpoint cleanup helper lacks immutable caller identity")
    else:
        first_caller_arn = str(ctx.checkpoint.state.get("account_arn") or "")
        principal_identity = _cleanup_principal_identity(ctx, first_caller_arn)
        trusted_principal_arn = principal_identity["arn"]
        trusted_principal_id = principal_identity["principal_id"]
    expected_iam_prefix = f"arn:{partition}:iam::{ctx.settings.expected_account}:"
    if (
        not trusted_principal_arn.startswith(
            (f"{expected_iam_prefix}user/", f"{expected_iam_prefix}role/")
        )
        or "*" in trusted_principal_arn
        or not re.fullmatch(r"[A-Z0-9]+", trusted_principal_id)
    ):
        raise RuntimeError("Checkpoint cleanup helper canonical principal is invalid")
    stable_id = uuid.uuid5(
        _LOG_CLEANUP_HELPER_NAMESPACE,
        f"{partition}:{ctx.settings.expected_account}:{ctx.settings.run_id}:{cleanup_token}",
    ).hex[:20]
    stack_name = f"{_LOG_CLEANUP_HELPER_STACK_PREFIX}-{stable_id}"
    role_name = stack_name
    project_name = str(ctx.config.project_name)
    if any(
        name == project_name or name.startswith((f"{project_name}-", f"{project_name}/"))
        for name in (stack_name, role_name)
    ):
        raise RuntimeError("Cleanup helper identity overlaps project inventory naming")
    role_arn = f"arn:{partition}:iam::{ctx.settings.expected_account}:role/{role_name}"
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": trusted_principal_arn},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": cleanup_token}},
            }
        ],
    }
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Temporary least-privilege role for live-validation log cleanup",
        "Resources": {
            "CleanupRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": role_name,
                    "MaxSessionDuration": 3600,
                    "AssumeRolePolicyDocument": trust_policy,
                    "Policies": [
                        {
                            "PolicyName": _LOG_CLEANUP_ROLE_POLICY_NAME,
                            "PolicyDocument": policy,
                        }
                    ],
                    "Tags": [
                        {"Key": _LOG_CLEANUP_ROLE_RUN_TAG, "Value": ctx.settings.run_id},
                        {"Key": _LOG_CLEANUP_ROLE_TOKEN_TAG, "Value": cleanup_token},
                    ],
                },
            }
        },
        "Outputs": {_LOG_CLEANUP_ROLE_OUTPUT: {"Value": {"Fn::GetAtt": ["CleanupRole", "Arn"]}}},
    }
    template_body = _canonical_json(template)
    return {
        "schema_version": 1,
        "region": helper_region,
        "stack_name": stack_name,
        "role_name": role_name,
        "role_arn": role_arn,
        "partition": partition,
        "run_id": ctx.settings.run_id,
        "cleanup_token": cleanup_token,
        "first_caller_arn": first_caller_arn,
        "trusted_principal_arn": trusted_principal_arn,
        "trusted_principal_id": trusted_principal_id,
        "role_policy": policy,
        "trust_policy": trust_policy,
        "template": template,
        "template_body": template_body,
        "template_sha256": hashlib.sha256(template_body.encode("utf-8")).hexdigest(),
    }


def _prepare_log_cleanup_helper_record(
    ctx: RunContext,
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    immutable_keys = (
        "schema_version",
        "region",
        "stack_name",
        "role_name",
        "role_arn",
        "partition",
        "run_id",
        "cleanup_token",
        "trusted_principal_arn",
        "trusted_principal_id",
        "template_sha256",
    )
    with ctx.state_lock:
        record = ctx.checkpoint.state.get("log_cleanup_helper")
        if record is None:
            record = {key: spec[key] for key in immutable_keys}
            record.update(
                {
                    "first_caller_arn": spec["first_caller_arn"],
                    "active_stack_id": None,
                    "lifecycle": "prepared",
                    "create_sequence": 0,
                    "stack_history": [],
                }
            )
            ctx.checkpoint.state["log_cleanup_helper"] = record
        elif not isinstance(record, dict):
            raise RuntimeError("Checkpoint log_cleanup_helper must be an object")
        elif any(record.get(key) != spec[key] for key in immutable_keys):
            raise RuntimeError("Checkpoint log cleanup helper identity changed")
        ctx.persist_callback(ctx.checkpoint)
        return record


def _helper_stack_id_prefix(ctx: RunContext, spec: Mapping[str, Any]) -> str:
    return (
        f"arn:{spec['partition']}:cloudformation:{spec['region']}:"
        f"{ctx.settings.expected_account}:stack/{spec['stack_name']}/"
    )


def _record_log_cleanup_helper_stack(
    ctx: RunContext,
    spec: Mapping[str, Any],
    stack_id: str,
    status: str,
) -> None:
    if not stack_id.startswith(_helper_stack_id_prefix(ctx, spec)):
        raise RuntimeError(f"Cleanup helper returned an invalid stack ID: {stack_id}")
    with ctx.state_lock:
        record = _prepare_log_cleanup_helper_record(ctx, spec)
        active_stack_id = str(record.get("active_stack_id") or "")
        if active_stack_id and active_stack_id != stack_id:
            raise RuntimeError("Cleanup helper stack generation changed without absence proof")
        history = record.setdefault("stack_history", [])
        if not isinstance(history, list):
            raise RuntimeError("Checkpoint cleanup helper stack_history must be a list")
        if not any(item.get("stack_id") == stack_id for item in history if isinstance(item, dict)):
            history.append({"stack_id": stack_id, "first_observed_at": utc_now()})
        record["active_stack_id"] = stack_id
        record["lifecycle"] = status
        record["last_observed_at"] = utc_now()
        ctx.persist_callback(ctx.checkpoint)


def _mark_log_cleanup_helper_absent(
    ctx: RunContext,
    stack_id: str | None,
) -> None:
    with ctx.state_lock:
        record = ctx.checkpoint.state.get("log_cleanup_helper")
        if not isinstance(record, dict):
            return
        active_stack_id = str(record.get("active_stack_id") or "")
        if stack_id and active_stack_id and active_stack_id != stack_id:
            raise RuntimeError("Cleanup helper absence proof refers to a different stack")
        record["active_stack_id"] = None
        record["lifecycle"] = "deleted"
        record["last_deleted_stack_id"] = stack_id
        record["deleted_at"] = utc_now()
        ctx.persist_callback(ctx.checkpoint)


def _template_document(template_body: Any) -> dict[str, Any]:
    if isinstance(template_body, str):
        try:
            template_body = json.loads(template_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Cleanup helper template is not canonical JSON") from exc
    if not isinstance(template_body, dict):
        raise RuntimeError("Cleanup helper template is not a JSON object")
    return template_body


def _validate_log_cleanup_helper_stack(
    ctx: RunContext,
    spec: Mapping[str, Any],
    stack: Mapping[str, Any],
) -> str:
    stack_id = str(stack.get("stack_id") or "")
    if (
        stack.get("name") != spec["stack_name"]
        or not stack_id.startswith(_helper_stack_id_prefix(ctx, spec))
        or stack.get("termination_protection")
    ):
        raise RuntimeError("Cleanup helper CloudFormation identity is invalid")
    tags = stack.get("tags") or {}
    if (
        tags.get(_LOG_CLEANUP_HELPER_RUN_TAG) != spec["run_id"]
        or tags.get(_LOG_CLEANUP_HELPER_TOKEN_TAG) != spec["cleanup_token"]
        or tags.get("gco:project") is not None
        or tags.get("Project") is not None
    ):
        raise RuntimeError("Cleanup helper CloudFormation tags are invalid")
    cfn = ctx.session.client("cloudformation", region_name=spec["region"])
    body = _template_document(
        cfn.get_template(StackName=stack_id, TemplateStage="Original").get("TemplateBody")
    )
    observed_hash = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    if observed_hash != spec["template_sha256"]:
        raise RuntimeError("Cleanup helper CloudFormation template changed")
    return stack_id


def _validate_log_cleanup_helper_role(
    ctx: RunContext,
    spec: Mapping[str, Any],
    helper_record: dict[str, Any],
    stack_id: str,
) -> dict[str, str]:
    iam = ctx.session.client("iam", region_name=spec["region"])
    role = iam.get_role(RoleName=spec["role_name"]).get("Role")
    if not isinstance(role, dict):
        raise RuntimeError("IAM omitted the cleanup helper role")
    tags = {
        str(item.get("Key")): str(item.get("Value") or "")
        for item in role.get("Tags", [])
        if item.get("Key") is not None
    }
    if (
        str(role.get("RoleName") or "") != spec["role_name"]
        or str(role.get("Arn") or "") != spec["role_arn"]
        or str(role.get("Path") or "") != "/"
        or int(role.get("MaxSessionDuration") or 0) != 3600
        or role.get("AssumeRolePolicyDocument") != spec["trust_policy"]
        or tags.get(_LOG_CLEANUP_ROLE_RUN_TAG) != spec["run_id"]
        or tags.get(_LOG_CLEANUP_ROLE_TOKEN_TAG) != spec["cleanup_token"]
    ):
        raise RuntimeError("Cleanup helper IAM role identity changed")
    inline = iam.list_role_policies(RoleName=spec["role_name"])
    if inline.get("IsTruncated") or inline.get("PolicyNames") != [_LOG_CLEANUP_ROLE_POLICY_NAME]:
        raise RuntimeError("Cleanup helper IAM inline policies changed")
    role_policy = iam.get_role_policy(
        RoleName=spec["role_name"],
        PolicyName=_LOG_CLEANUP_ROLE_POLICY_NAME,
    ).get("PolicyDocument")
    if role_policy != spec["role_policy"]:
        raise RuntimeError("Cleanup helper IAM delete policy changed")
    attached = iam.list_attached_role_policies(RoleName=spec["role_name"])
    if attached.get("IsTruncated") or attached.get("AttachedPolicies"):
        raise RuntimeError("Cleanup helper IAM role gained a managed policy")
    created = role.get("CreateDate")
    identity = {
        "arn": str(role["Arn"]),
        "role_id": str(role.get("RoleId") or ""),
        "created_at": created.isoformat() if created is not None else "",
    }
    if not identity["role_id"] or not identity["created_at"]:
        raise RuntimeError("IAM omitted immutable cleanup role identity")
    history = helper_record.get("stack_history")
    if not isinstance(history, list):
        raise RuntimeError("Checkpoint cleanup helper stack_history must be a list")
    generation = next(
        (
            item
            for item in history
            if isinstance(item, dict) and str(item.get("stack_id") or "") == stack_id
        ),
        None,
    )
    if generation is None:
        raise RuntimeError("Cleanup role identity has no exact helper stack generation")
    observed = generation.get("observed_role_identity")
    if observed is None:
        generation["observed_role_identity"] = identity
        ctx.persist()
    elif observed != identity:
        raise RuntimeError("Cleanup helper IAM role generation changed within its stack")
    return identity


def _wait_for_log_cleanup_helper(
    ctx: RunContext,
    spec: Mapping[str, Any],
    stack_id: str,
    *,
    deleting: bool,
) -> dict[str, Any] | None:
    for _attempt in range(_LOG_CLEANUP_STACK_POLL_ATTEMPTS):
        stack = describe_stack(ctx.session, str(spec["region"]), stack_id)
        status = str((stack or {}).get("status") or "")
        if deleting and (stack is None or status == "DELETE_COMPLETE"):
            return None
        if not deleting and stack is not None and status == "CREATE_COMPLETE":
            return stack
        if status == "DELETE_FAILED":
            raise RuntimeError(f"Cleanup helper stack deletion failed: {stack_id}")
        if not deleting and stack is not None and not status.endswith("_IN_PROGRESS"):
            raise RuntimeError(f"Cleanup helper stack creation ended in {status}: {stack_id}")
        time.sleep(_LOG_CLEANUP_STACK_POLL_SECONDS)
    operation = "deletion" if deleting else "creation"
    raise RuntimeError(f"Cleanup helper stack {operation} timed out: {stack_id}")


def _current_cleanup_trusted_principal(ctx: RunContext) -> tuple[str, dict[str, str]]:
    identity = ctx.session.client("sts", region_name=ctx.config.global_region).get_caller_identity()
    account = str(identity.get("Account") or "")
    caller_arn = str(identity.get("Arn") or "")
    if account != ctx.settings.expected_account:
        raise RuntimeError("Cleanup helper caller account changed")
    return caller_arn, _cleanup_principal_identity(ctx, caller_arn)


def _ensure_log_cleanup_helper(ctx: RunContext) -> dict[str, Any]:
    records = ctx.checkpoint.state.get("owned_log_groups", [])
    if not isinstance(records, list):
        raise RuntimeError("Checkpoint owned_log_groups must be a list")
    if not records or all(bool(record.get("deleted")) for record in records):
        return {"needed": False}
    spec = _log_cleanup_helper_spec(ctx)
    if spec is None:
        return {"needed": False}
    helper_record = _prepare_log_cleanup_helper_record(ctx, spec)
    caller_arn, current_principal = _current_cleanup_trusted_principal(ctx)
    if (
        current_principal["arn"] != spec["trusted_principal_arn"]
        or current_principal["principal_id"] != spec["trusted_principal_id"]
    ):
        raise RuntimeError("Cleanup helper caller principal changed since authority creation")

    region = str(spec["region"])
    cfn = ctx.session.client("cloudformation", region_name=region)
    stack: dict[str, Any] | None = None
    active_stack_id = str(helper_record.get("active_stack_id") or "")
    if active_stack_id:
        stack = describe_stack(ctx.session, region, active_stack_id)
        if stack is not None and stack.get("status") == "DELETE_COMPLETE":
            _mark_log_cleanup_helper_absent(ctx, active_stack_id)
            active_stack_id = ""
            stack = None
    if stack is None:
        named_stack = describe_stack(ctx.session, region, str(spec["stack_name"]))
        if named_stack is not None and named_stack.get("status") != "DELETE_COMPLETE":
            named_stack_id = _validate_log_cleanup_helper_stack(ctx, spec, named_stack)
            if active_stack_id and named_stack_id != active_stack_id:
                raise RuntimeError("A different cleanup helper stack generation appeared")
            _record_log_cleanup_helper_stack(
                ctx,
                spec,
                named_stack_id,
                str(named_stack.get("status") or ""),
            )
            active_stack_id = named_stack_id
            stack = named_stack
    if stack is not None and stack.get("status") == "DELETE_IN_PROGRESS":
        _wait_for_log_cleanup_helper(ctx, spec, active_stack_id, deleting=True)
        _mark_log_cleanup_helper_absent(ctx, active_stack_id)
        active_stack_id = ""
        stack = None

    if stack is None:
        with ctx.state_lock:
            helper_record = _prepare_log_cleanup_helper_record(ctx, spec)
            helper_record["create_sequence"] = int(helper_record.get("create_sequence") or 0) + 1
            sequence = helper_record["create_sequence"]
            helper_record["lifecycle"] = "create-intent"
            helper_record["create_intent_at"] = utc_now()
            ctx.persist_callback(ctx.checkpoint)
        token = f"live-validation-{spec['stack_name']}-{sequence}"
        try:
            response = cfn.create_stack(
                StackName=spec["stack_name"],
                TemplateBody=spec["template_body"],
                Capabilities=["CAPABILITY_NAMED_IAM"],
                ClientRequestToken=token[:128],
                EnableTerminationProtection=False,
                OnFailure="ROLLBACK",
                TimeoutInMinutes=10,
                Tags=[
                    {"Key": _LOG_CLEANUP_HELPER_RUN_TAG, "Value": spec["run_id"]},
                    {"Key": _LOG_CLEANUP_HELPER_TOKEN_TAG, "Value": spec["cleanup_token"]},
                ],
            )
            active_stack_id = str(response.get("StackId") or "")
            _record_log_cleanup_helper_stack(ctx, spec, active_stack_id, "CREATE_IN_PROGRESS")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "AlreadyExistsException":
                raise
            stack = describe_stack(ctx.session, region, str(spec["stack_name"]))
            if stack is None:
                raise RuntimeError("Cleanup helper name exists but cannot be described") from exc
            active_stack_id = _validate_log_cleanup_helper_stack(ctx, spec, stack)
            _record_log_cleanup_helper_stack(
                ctx,
                spec,
                active_stack_id,
                str(stack.get("status") or ""),
            )

    stack = _wait_for_log_cleanup_helper(ctx, spec, active_stack_id, deleting=False)
    if stack is None:
        raise RuntimeError("Cleanup helper disappeared after creation")
    _validate_log_cleanup_helper_stack(ctx, spec, stack)
    outputs = stack.get("outputs") or {}
    if outputs.get(_LOG_CLEANUP_ROLE_OUTPUT) != spec["role_arn"]:
        raise RuntimeError("Cleanup helper role output changed")
    helper_record = _prepare_log_cleanup_helper_record(ctx, spec)
    _validate_log_cleanup_helper_role(ctx, spec, helper_record, active_stack_id)
    _record_log_cleanup_helper_stack(ctx, spec, active_stack_id, "CREATE_COMPLETE")
    return {
        "needed": True,
        "region": region,
        "stack_id": active_stack_id,
        "stack_name": spec["stack_name"],
        "role_arn": spec["role_arn"],
        "partition": spec["partition"],
        "caller_arn": caller_arn,
        "trusted_principal_arn": spec["trusted_principal_arn"],
        "session_policy": spec["role_policy"],
        "external_id": spec["cleanup_token"],
    }


def _delete_log_cleanup_helper(ctx: RunContext) -> dict[str, Any]:
    helper_record = ctx.checkpoint.state.get("log_cleanup_helper")
    if helper_record is None:
        return {"needed": False, "deleted": True}
    if not isinstance(helper_record, dict):
        raise RuntimeError("Checkpoint log_cleanup_helper must be an object")
    spec = _log_cleanup_helper_spec(ctx)
    if spec is None:
        raise RuntimeError("Cleanup helper exists without log-group authority records")
    helper_record = _prepare_log_cleanup_helper_record(ctx, spec)
    region = str(spec["region"])
    cfn = ctx.session.client("cloudformation", region_name=region)
    active_stack_id = str(helper_record.get("active_stack_id") or "")
    stack = describe_stack(ctx.session, region, active_stack_id) if active_stack_id else None
    if stack is not None and stack.get("status") == "DELETE_COMPLETE":
        stack = None
    if stack is None:
        named_stack = describe_stack(ctx.session, region, str(spec["stack_name"]))
        if named_stack is not None and named_stack.get("status") != "DELETE_COMPLETE":
            named_stack_id = _validate_log_cleanup_helper_stack(ctx, spec, named_stack)
            if active_stack_id and named_stack_id != active_stack_id:
                raise RuntimeError("Refusing to delete a replacement cleanup helper stack")
            active_stack_id = named_stack_id
            stack = named_stack
            _record_log_cleanup_helper_stack(
                ctx,
                spec,
                active_stack_id,
                str(stack.get("status") or ""),
            )
    if stack is None:
        _mark_log_cleanup_helper_absent(ctx, active_stack_id or None)
        return {
            "needed": bool(active_stack_id),
            "deleted": True,
            "already_absent": True,
            "stack_id": active_stack_id or None,
        }

    active_stack_id = _validate_log_cleanup_helper_stack(ctx, spec, stack)
    status = str(stack.get("status") or "")
    if status == "CREATE_COMPLETE":
        _validate_log_cleanup_helper_role(ctx, spec, helper_record, active_stack_id)
    if status != "DELETE_IN_PROGRESS":
        with ctx.state_lock:
            helper_record["lifecycle"] = "delete-intent"
            helper_record["delete_intent_at"] = utc_now()
            ctx.persist_callback(ctx.checkpoint)
        cfn.delete_stack(
            StackName=active_stack_id,
            ClientRequestToken=(
                f"delete-{spec['stack_name']}-{active_stack_id.rsplit('/', 1)[-1]}"[:128]
            ),
        )
        helper_record["lifecycle"] = "DELETE_IN_PROGRESS"
        ctx.persist()
    _wait_for_log_cleanup_helper(ctx, spec, active_stack_id, deleting=True)
    replacement = describe_stack(ctx.session, region, str(spec["stack_name"]))
    if replacement is not None and replacement.get("status") != "DELETE_COMPLETE":
        raise RuntimeError("Cleanup helper stack name was replaced during deletion")
    _mark_log_cleanup_helper_absent(ctx, active_stack_id)
    return {"needed": True, "deleted": True, "stack_id": active_stack_id}


class TagConditionedLogDeleter:
    """Hand out ``logs`` clients whose DeleteLogGroup is tag-conditioned.

    Deleting a retained log group is only safe while the group still carries
    both of this run's authority tags. Rather than trust a read that happened
    moments earlier, deletion goes through a delegated role whose session
    policy makes every ``logs:DeleteLogGroup`` call conditional on those exact
    tag values: if a foreign generation replaced the group between the
    immediate pre-delete read and the request, AWS refuses the call instead of
    destroying someone else's log data.

    The helper stack, role, and STS session are created on first use, so a run
    whose checkpointed groups all turn out to be absent never provisions any of
    them. ``authorization`` is the report evidence for that decision and stays
    ``{"needed": False}`` until a session is actually established.
    """

    def __init__(self, ctx: RunContext) -> None:
        self._ctx = ctx
        self._clients: dict[str, Any] = {}
        self._credentials: dict[str, Any] | None = None
        self.authorization: dict[str, Any] = {"needed": False}

    def client(self, region: str) -> Any:
        """Return the tag-conditioned ``logs`` client for one Region."""
        if self._credentials is None:
            self._establish_session()
        credentials = self._credentials
        if credentials is None:
            raise RuntimeError("Log cleanup session was not established")
        if region not in self._clients:
            self._clients[region] = self._ctx.session.client(
                "logs",
                region_name=region,
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
            )
        return self._clients[region]

    def _establish_session(self) -> None:
        """Assume the scoped cleanup role once and verify the exact principal."""
        ctx = self._ctx
        helper = _ensure_log_cleanup_helper(ctx)
        if not helper.get("needed"):
            raise RuntimeError("Log cleanup role was not created for pending groups")
        session_name = (
            "live-validation-logs-"
            + uuid.uuid5(_LOG_CLEANUP_HELPER_NAMESPACE, ctx.settings.run_id).hex[:16]
        )
        assumption = ctx.session.client("sts", region_name=helper["region"]).assume_role(
            RoleArn=helper["role_arn"],
            RoleSessionName=session_name,
            DurationSeconds=_LOG_CLEANUP_SESSION_SECONDS,
            ExternalId=helper["external_id"],
            Policy=_canonical_json(helper["session_policy"]),
        )
        credentials = assumption.get("Credentials") or {}
        if any(
            not credentials.get(field)
            for field in ("AccessKeyId", "SecretAccessKey", "SessionToken")
        ):
            raise RuntimeError("AssumeRole omitted cleanup session credentials")
        assumed_user_arn = str((assumption.get("AssumedRoleUser") or {}).get("Arn") or "")
        expected_assumed_arn = (
            f"arn:{helper['partition']}:sts::{ctx.settings.expected_account}:assumed-role/"
            f"{helper['role_arn'].rsplit('/', 1)[-1]}/{session_name}"
        )
        if assumed_user_arn != expected_assumed_arn:
            raise RuntimeError("AssumeRole returned an unexpected cleanup principal")
        expiration = credentials.get("Expiration")
        self._credentials = credentials
        self.authorization = {
            "needed": True,
            "mode": "sts-assume-role-session-policy",
            "role_arn": helper["role_arn"],
            "helper_stack_id": helper["stack_id"],
            "atomic_resource_tag_condition": True,
            "condition_tag_keys": [_RUN_STACK_TAG, _LOG_CLEANUP_TOKEN_TAG],
            "session_expiration": (expiration.isoformat() if expiration is not None else None),
        }
