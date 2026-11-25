"""The analytics environment: Studio sign-in, profile ownership, the EFS home and user removal.

Every release run deploys ``<project>-analytics`` through the run-scoped
``analytics_environment_overrides`` CDK context
(:data:`RELEASE_ANALYTICS_OVERRIDES`). The probe drives the real
``gco analytics`` commands, as an operator runs them, and reads SageMaker,
Cognito, IAM and S3 for the evidence:

1. ``gco analytics doctor`` passes against the deployment;
2. ``users add`` refuses a name SageMaker cannot use as a profile name, and
   the presigned-URL Lambda refuses one Cognito accepts
   (HTTP 400 ``UnsupportedUsername``);
3. a user added with a temporary password chooses a new one at the first
   ``studio login`` and signs in with it again; both print a presigned Studio
   URL, and the profile the Lambda created is ``InService``, tagged with the
   user's Cognito ``sub`` and given ``/home/<username>`` on the Studio EFS;
4. a private JupyterLab space on that profile, with the Studio EFS attached,
   starts an app whose lifecycle configuration writes into that directory and
   reports what it saw to the stack's Studio-only bucket;
5. after ``users remove --keep-studio-profile`` the same name, added again,
   is refused by the Lambda (HTTP 403 ``UserProfileOwnedByAnotherUser``), and
   ``users remove`` then leaves the profile, whose owner tag names the first
   user, in place;
6. ``users remove`` deletes a second user's profile and its space;
7. ``iam:SimulatePrincipalPolicy`` allows the Studio execution role the
   grants notebooks rely on and denies a Logs Insights query outside them.

The first user's profile, space and running app stay for the ``destroy``
action: the stack's cleanup custom resource must delete them before
CloudFormation can delete the domain. The lifecycle configuration is
account-level and attached to that profile, so the teardown sweep deletes it
once the stack is gone (``cleanup/analytics.py``). Passwords reach the CLI
through its environment variables, never its argv, and no password or
presigned token is recorded.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from botocore.exceptions import ClientError

from cli.analytics_user_mgmt import STUDIO_REMOVAL_TIMEOUT_SECONDS, generate_strong_password
from gco.config.config_loader import (
    ANALYTICS_OVERRIDES_CONTEXT_KEY,
    apply_analytics_environment_overrides,
)

from ..models import RunContext, utc_now

#: The ``analytics_environment_overrides`` context every release-run CDK
#: invocation carries.
RELEASE_ANALYTICS_OVERRIDES: dict[str, Any] = {"enabled": True}
#: Where the checkpoint lists the Studio lifecycle configurations the probe
#: created, recorded before each is created, for the teardown sweep.
LIFECYCLE_CONFIGS_STATE = "analytics_lifecycle_configs"
#: The tag the presigned-URL Lambda writes on each profile it creates.
PROFILE_OWNER_TAG = "gco:analytics:cognito-sub"

_INSTANCE_TYPE = "ml.t3.medium"
_APP_TYPE = "JupyterLab"
_APP_NAME = "default"
#: The account that publishes the SageMaker Distribution images in each Region
#: (https://docs.aws.amazon.com/sagemaker/latest/dg/notebooks-available-images.html):
#: ``CreateApp`` requires an image ARN for a JupyterLab app.
_DISTRIBUTION_IMAGE_ACCOUNTS: dict[str, str] = {
    "us-east-1": "885854791233",
    "us-east-2": "137914896644",
    "us-west-1": "053634841547",
    "us-west-2": "542918446943",
    "af-south-1": "238384257742",
    "ap-east-1": "523751269255",
    "ap-south-1": "245090515133",
    "ap-northeast-1": "010972774902",
    "ap-northeast-2": "064688005998",
    "ap-northeast-3": "564864627153",
    "ap-southeast-1": "022667117163",
    "ap-southeast-2": "648430277019",
    "ap-southeast-3": "370607712162",
    "ca-central-1": "481561238223",
    "eu-central-1": "545423591354",
    "eu-north-1": "175620155138",
    "eu-south-1": "810671768855",
    "eu-west-1": "819792524951",
    "eu-west-2": "021081402939",
    "eu-west-3": "856416204555",
    "me-central-1": "358593528301",
    "me-south-1": "523774347010",
    "sa-east-1": "567556641782",
}
_POLL_SECONDS = 15
_PROFILE_TIMEOUT_SECONDS = 600
_SPACE_TIMEOUT_SECONDS = 600
_APP_TIMEOUT_SECONDS = 1500
_REPORT_TIMEOUT_SECONDS = 600
#: ``studio login`` waits 120 seconds for a new profile; a slower one is
#: answered "did not become ready" and the login is repeated.
_LOGIN_ATTEMPTS = 3
_PROFILE_NOT_READY = "did not become ready"
_MAX_OUTPUT = 4000
#: ``users remove`` waits up to ``STUDIO_REMOVAL_TIMEOUT_SECONDS`` for SageMaker
#: to delete the profile's apps and spaces and the profile, longer than the
#: run's command timeout; the margin covers Cognito and the CLI's start.
_USERS_REMOVE_TIMEOUT_SECONDS = STUDIO_REMOVAL_TIMEOUT_SECONDS + 300
#: The CLI's password variables; an operator's own values never reach a probe.
_PASSWORD_ENV = frozenset({"GCO_STUDIO_PASSWORD", "GCO_STUDIO_NEW_PASSWORD"})
_PASSWORD_FIELD = re.compile(r'("password"\s*:\s*)"(?:[^"\\]|\\.)*"')
_URL_TOKEN = re.compile(r"(token=)[^&\s\"']+")

#: The lifecycle configuration: as the notebook user, list and write the
#: Studio EFS where Studio links it, and upload what it found. Exits 0 so a
#: failed probe is reported, not an app that never starts.
_HOME_PROBE_SCRIPT = """#!/bin/bash
python3 - <<'PY' || true
import json, os
import boto3
root = os.path.expanduser("~/custom-file-systems/efs/__EFS_ID__")
report = {"uid": os.getuid(), "gid": os.getgid(), "path": root}
try:
    report["entries"] = sorted(os.listdir(root))
    probe = os.path.join(root, ".gco-live-validation-probe")
    with open(probe, "w") as handle:
        handle.write("probe\\n")
    os.remove(probe)
    report["writable"] = True
except OSError as exc:
    report["writable"] = False
    report["error"] = f"{type(exc).__name__}: {exc}"
boto3.client("s3").put_object(
    Bucket="__BUCKET__", Key="__KEY__", Body=json.dumps(report).encode()
)
PY
exit 0
"""


class AnalyticsValidationError(RuntimeError):
    """The analytics environment did not behave as the release promises."""


def analytics_overrides_json() -> str:
    """Canonical JSON for the ``analytics_environment_overrides`` context."""
    return json.dumps(RELEASE_ANALYTICS_OVERRIDES, sort_keys=True, separators=(",", ":"))


def effective_analytics_enablement(ctx: RunContext) -> dict[str, Any]:
    """Whether the run deploys the analytics environment, and what decided it."""
    overrides = ctx.settings.analytics_overrides_json or None
    merged = apply_analytics_environment_overrides(
        ctx.cdk_context.get("analytics_environment"), overrides
    )
    enabled = isinstance(merged, dict) and merged.get("enabled") is True
    if overrides and json.loads(overrides).get("enabled"):
        source = f"run-override ({ANALYTICS_OVERRIDES_CONTEXT_KEY})"
    else:
        source = "cdk.json analytics_environment.enabled"
    return {"enabled": enabled, "source": source, "region": str(ctx.config.api_gateway_region)}


def _redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    text = _PASSWORD_FIELD.sub(r'\1"<redacted>"', text)
    return _URL_TOKEN.sub(r"\1<redacted>", text)


def _run_gco(
    ctx: RunContext,
    commands: list[dict[str, Any]],
    stage: str,
    args: Sequence[str],
    *,
    secrets: Mapping[str, str] | None = None,
    minimum_timeout: float = 0.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``gco <args>`` from the checkout and record it without its secrets.

    ``secrets`` are environment variables the CLI reads its passwords from
    (``GCO_STUDIO_PASSWORD``, ``GCO_STUDIO_NEW_PASSWORD``). The run's command
    timeout applies, or ``minimum_timeout`` when that is longer.
    """
    argv = [sys.executable, "-m", "cli.main", *args]
    environment = {key: value for key, value in os.environ.items() if key not in _PASSWORD_ENV}
    environment.update(secrets or {})
    hidden = [value for value in (secrets or {}).values() if value]
    try:
        result = subprocess.run(
            argv,
            cwd=ctx.settings.repo_root,
            capture_output=True,
            text=True,
            timeout=max(float(ctx.settings.command_timeout_seconds), minimum_timeout),
            env=environment,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        commands.append({"stage": stage, "argv": argv, "timed_out": True})
        raise AnalyticsValidationError(f"{stage}: gco timed out after {exc.timeout:.0f}s") from None
    commands.append(
        {
            "stage": stage,
            "argv": argv,
            "returncode": result.returncode,
            "stdout": _redact(result.stdout, hidden)[-_MAX_OUTPUT:],
            "stderr": _redact(result.stderr, hidden)[-_MAX_OUTPUT:],
        }
    )
    return result


def _require_exit(result: subprocess.CompletedProcess[str], stage: str, expected: int) -> None:
    if result.returncode != expected:
        raise AnalyticsValidationError(
            f"{stage}: gco exited {result.returncode}, expected {expected}: "
            f"{result.stderr.strip()[-600:]}"
        )


def _json_output(result: subprocess.CompletedProcess[str], stage: str) -> dict[str, Any]:
    try:
        document = json.loads(result.stdout)
    except ValueError as exc:
        raise AnalyticsValidationError(f"{stage}: gco printed no JSON document") from exc
    if not isinstance(document, dict):
        raise AnalyticsValidationError(f"{stage}: gco printed {type(document).__name__}")
    return document


def _wait_for(
    read: Callable[[], str | None],
    *,
    done: set[str],
    failed: set[str],
    what: str,
    timeout: int,
) -> str | None:
    """Poll ``read`` until it returns a status in ``done``; raise on ``failed`` or the timeout."""
    deadline = time.monotonic() + timeout
    while True:
        status = read()
        if status in done:
            return status
        if status in failed:
            raise AnalyticsValidationError(f"{what} is {status}")
        if time.monotonic() >= deadline:
            raise AnalyticsValidationError(f"{what} is still {status} after {timeout}s")
        time.sleep(_POLL_SECONDS)


def _missing(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code") or "") in {
        "ResourceNotFound",
        "ValidationException",
        "UserNotFoundException",
    }


def _deployment(ctx: RunContext) -> dict[str, str]:
    """The analytics stack's Studio domain, user pool, EFS, bucket and execution role."""
    region = str(ctx.config.api_gateway_region)
    stack_name = f"{ctx.config.project_name}-analytics"
    cloudformation = ctx.session.client("cloudformation", region_name=region)
    stacks = cloudformation.describe_stacks(StackName=stack_name).get("Stacks") or []
    outputs = {
        str(output.get("OutputKey")): str(output.get("OutputValue"))
        for stack in stacks
        for output in stack.get("Outputs") or []
    }
    resources: dict[tuple[str, str], str] = {}
    for page in cloudformation.get_paginator("list_stack_resources").paginate(StackName=stack_name):
        for item in page.get("StackResourceSummaries", []):
            for prefix in ("StudioEfs", "StudioOnlyBucket"):
                if str(item.get("LogicalResourceId") or "").startswith(prefix):
                    key = (prefix, str(item.get("ResourceType") or ""))
                    resources[key] = str(item.get("PhysicalResourceId") or "")
    deployment = {
        "region": region,
        "stack_name": stack_name,
        "domain_id": outputs.get("StudioDomainId", ""),
        "pool_id": outputs.get("CognitoUserPoolId", ""),
        "efs_id": resources.get(("StudioEfs", "AWS::EFS::FileSystem"), ""),
        "bucket": resources.get(("StudioOnlyBucket", "AWS::S3::Bucket"), ""),
    }
    missing = sorted(name for name, value in deployment.items() if not value)
    if missing:
        raise AnalyticsValidationError(
            f"{stack_name} in {region} does not name its {', '.join(missing)}"
        )
    domain = ctx.session.client("sagemaker", region_name=region).describe_domain(
        DomainId=deployment["domain_id"]
    )
    deployment["execution_role"] = str(
        (domain.get("DefaultUserSettings") or {}).get("ExecutionRole") or ""
    )
    deployment["domain_arn"] = str(domain.get("DomainArn") or "")
    deployment["home_efs_id"] = str(domain.get("HomeEfsFileSystemId") or "")
    if not deployment["execution_role"]:
        raise AnalyticsValidationError(f"Studio domain {deployment['domain_id']} has no role")
    return deployment


def _names(ctx: RunContext, attempt: int) -> dict[str, str]:
    """Run-scoped names under the project prefix, so the inventory owns any residue."""
    token = hashlib.sha256(ctx.settings.run_id.encode()).hexdigest()[:8]
    prefix = f"{ctx.config.project_name}-lv{token}{attempt}"
    return {
        "first": f"{prefix}-first",
        "second": f"{prefix}-second",
        "refused": f"{prefix}_refused",
        "unsupported": f"{prefix}_unsupported",
        "home_space": f"{prefix}-home",
        "second_space": f"{prefix}-second-space",
        "lifecycle_config": f"{prefix}-home-probe",
        "report_key": f"gco-live-validation/{prefix}/home-probe.json",
    }


def _cognito_sub(cognito: Any, pool_id: str, username: str) -> str:
    attributes = cognito.admin_get_user(UserPoolId=pool_id, Username=username).get("UserAttributes")
    for attribute in attributes or []:
        if attribute.get("Name") == "sub":
            return str(attribute.get("Value") or "")
    raise AnalyticsValidationError(f"Cognito user {username} has no sub")


def _check_doctor(ctx: RunContext, commands: list[dict[str, Any]]) -> dict[str, Any]:
    result = _run_gco(ctx, commands, "doctor", ["--output", "json", "analytics", "doctor"])
    _require_exit(result, "doctor", 0)
    document = _json_output(result, "doctor")
    failed = [check.get("name") for check in document.get("checks", []) if not check.get("ok")]
    if document.get("passed") is not True or failed:
        raise AnalyticsValidationError(f"gco analytics doctor failed: {failed}")
    return {"passed": True, "checks": len(document.get("checks", []))}


def _check_username_rule(
    ctx: RunContext,
    commands: list[dict[str, Any]],
    cognito: Any,
    deployment: Mapping[str, str],
    username: str,
) -> dict[str, Any]:
    """``users add`` refuses a name SageMaker cannot use, before Cognito sees it."""
    args = ["--output", "json", "analytics", "users", "add", "--username", username, "--no-email"]
    result = _run_gco(ctx, commands, "username-rule", args)
    _require_exit(result, "username-rule", 1)
    if "Cannot create user" not in result.stderr:
        raise AnalyticsValidationError("users add refused the name without the profile-name rule")
    try:
        cognito.admin_get_user(UserPoolId=deployment["pool_id"], Username=username)
    except ClientError as exc:
        if not _missing(exc):
            raise
    else:
        raise AnalyticsValidationError(f"users add refused {username} but Cognito has it")
    return {"username": username, "refused": True}


def _login(
    ctx: RunContext,
    commands: list[dict[str, Any]],
    stage: str,
    username: str,
    secrets: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """``gco analytics studio login``, repeated while the new profile is not ready."""
    args = ["analytics", "studio", "login", "--username", username]
    for _attempt in range(_LOGIN_ATTEMPTS):
        result = _run_gco(ctx, commands, stage, args, secrets=secrets)
        if result.returncode != 2 or _PROFILE_NOT_READY not in result.stderr:
            return result
    return result


def _studio_url(
    result: subprocess.CompletedProcess[str], stage: str, deployment: Mapping[str, str]
) -> dict[str, str]:
    """The presigned URL's host and path, once they are Studio's for this domain."""
    _require_exit(result, stage, 0)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    url = urllib.parse.urlsplit(lines[-1].strip() if lines else "")
    # Studio's host is ``studio-<domain>`` (``<domain>`` for Studio Classic).
    suffix = f"{deployment['domain_id']}.studio.{deployment['region']}.sagemaker.aws"
    query = urllib.parse.parse_qs(url.query)
    if (
        url.scheme != "https"
        or url.hostname not in {suffix, f"studio-{suffix}"}
        or not query.get("token")
    ):
        raise AnalyticsValidationError(
            f"{stage}: studio login printed no presigned URL for {suffix}"
        )
    return {"host": str(url.hostname), "path": url.path}


def _require_refusal(
    result: subprocess.CompletedProcess[str], stage: str, status: str
) -> dict[str, Any]:
    _require_exit(result, stage, 2)
    if f"login failed: HTTP {status}" not in result.stderr:
        raise AnalyticsValidationError(
            f"{stage}: studio login was not refused with HTTP {status}: "
            f"{result.stderr.strip()[-300:]}"
        )
    return {"refused": f"HTTP {status}"}


def _profile(sagemaker: Any, domain_id: str, username: str) -> dict[str, Any] | None:
    try:
        return dict(sagemaker.describe_user_profile(DomainId=domain_id, UserProfileName=username))
    except ClientError as exc:
        if _missing(exc):
            return None
        raise


def _profile_tags(sagemaker: Any, arn: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    kwargs: dict[str, Any] = {"ResourceArn": arn}
    while True:
        response = sagemaker.list_tags(**kwargs)
        tags.update({str(t.get("Key")): str(t.get("Value")) for t in response.get("Tags", [])})
        if not response.get("NextToken"):
            return tags
        kwargs["NextToken"] = response["NextToken"]


def _check_profile(
    sagemaker: Any, deployment: Mapping[str, str], username: str, sub: str
) -> dict[str, Any]:
    """The Lambda's profile: InService, owned by ``sub``, on ``/home/<username>``."""
    profile = _profile(sagemaker, deployment["domain_id"], username)
    if profile is None or profile.get("Status") != "InService":
        status = profile.get("Status") if profile else "absent"
        raise AnalyticsValidationError(f"Studio profile {username} is {status}")
    tags = _profile_tags(sagemaker, str(profile["UserProfileArn"]))
    if tags.get(PROFILE_OWNER_TAG) != sub:
        raise AnalyticsValidationError(f"Studio profile {username} is not tagged with its owner")
    settings = profile.get("UserSettings") or {}
    expected = {"FileSystemId": deployment["efs_id"], "FileSystemPath": f"/home/{username}"}
    configs = [
        dict(config.get("EFSFileSystemConfig") or {})
        for config in settings.get("CustomFileSystemConfigs") or []
    ]
    if configs != [expected]:
        raise AnalyticsValidationError(
            f"Studio profile {username} has custom file systems {configs}, expected [{expected}]"
        )
    return {"status": "InService", "owner_tag": True, "efs_path": expected["FileSystemPath"]}


def _check_first_sign_in(
    ctx: RunContext,
    commands: list[dict[str, Any]],
    sagemaker: Any,
    cognito: Any,
    deployment: Mapping[str, str],
    username: str,
) -> tuple[dict[str, Any], str]:
    """The temporary password is replaced at the first login; return the evidence and the sub."""
    args = ["--output", "json", "analytics", "users", "add", "--username", username, "--no-email"]
    result = _run_gco(ctx, commands, "first-user-add", args)
    _require_exit(result, "first-user-add", 0)
    created = _json_output(result, "first-user-add")
    temporary = str(created.get("password") or "")
    state = created.get("password_state")
    if state != "temporary" or not temporary:  # nosec B105  # a state label, not a credential
        raise AnalyticsValidationError("users add printed no temporary password")
    if created.get("invitation_emailed") is not False:
        raise AnalyticsValidationError("users add --no-email reported an emailed invitation")
    chosen = generate_strong_password()
    first = _login(
        ctx,
        commands,
        "first-login",
        username,
        {"GCO_STUDIO_PASSWORD": temporary, "GCO_STUDIO_NEW_PASSWORD": chosen},
    )
    first_url = _studio_url(first, "first-login", deployment)
    again = _login(ctx, commands, "second-login", username, {"GCO_STUDIO_PASSWORD": chosen})
    _studio_url(again, "second-login", deployment)
    sub = _cognito_sub(cognito, deployment["pool_id"], username)
    evidence = {
        "username": username,
        "temporary_credential_replaced": True,
        "url": first_url,
        "profile": _check_profile(sagemaker, deployment, username, sub),
    }
    return evidence, sub


def _check_unsupported_username(
    ctx: RunContext,
    commands: list[dict[str, Any]],
    cognito: Any,
    deployment: Mapping[str, str],
    username: str,
) -> dict[str, Any]:
    """A Cognito user whose name SageMaker cannot use is refused by the Lambda."""
    password = generate_strong_password()
    pool_id = deployment["pool_id"]
    cognito.admin_create_user(
        UserPoolId=pool_id,
        Username=username,
        TemporaryPassword=generate_strong_password(),
        MessageAction="SUPPRESS",
    )
    try:
        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=username, Password=password, Permanent=True
        )
        result = _login(
            ctx, commands, "unsupported-login", username, {"GCO_STUDIO_PASSWORD": password}
        )
        evidence = _require_refusal(result, "unsupported-login", "400 UnsupportedUsername")
    finally:
        cognito.admin_delete_user(UserPoolId=pool_id, Username=username)
    return {"username": username, **evidence}


def _space_status(sagemaker: Any, domain_id: str, space: str) -> str | None:
    try:
        return str(sagemaker.describe_space(DomainId=domain_id, SpaceName=space).get("Status"))
    except ClientError as exc:
        if _missing(exc):
            return None
        raise


def _app(sagemaker: Any, domain_id: str, space: str) -> dict[str, Any] | None:
    try:
        return dict(
            sagemaker.describe_app(
                DomainId=domain_id, SpaceName=space, AppType=_APP_TYPE, AppName=_APP_NAME
            )
        )
    except ClientError as exc:
        if _missing(exc):
            return None
        raise


def _create_space(
    ctx: RunContext,
    sagemaker: Any,
    deployment: Mapping[str, str],
    *,
    owner: str,
    space: str,
    lifecycle_config_arn: str | None = None,
) -> None:
    """A private JupyterLab space of ``owner`` with the Studio EFS attached, run-tagged."""
    resource_spec: dict[str, str] = {"InstanceType": _INSTANCE_TYPE}
    if lifecycle_config_arn:
        resource_spec["LifecycleConfigArn"] = lifecycle_config_arn
    sagemaker.create_space(
        DomainId=deployment["domain_id"],
        SpaceName=space,
        OwnershipSettings={"OwnerUserProfileName": owner},
        SpaceSharingSettings={"SharingType": "Private"},
        SpaceSettings={
            "AppType": _APP_TYPE,
            "CustomFileSystems": [{"EFSFileSystem": {"FileSystemId": deployment["efs_id"]}}],
            "JupyterLabAppSettings": {"DefaultResourceSpec": resource_spec},
        },
        Tags=_run_tags(ctx),
    )
    _wait_for(
        lambda: _space_status(sagemaker, deployment["domain_id"], space),
        done={"InService"},
        failed={"Failed", "Update_Failed", "Delete_Failed"},
        what=f"Studio space {space}",
        timeout=_SPACE_TIMEOUT_SECONDS,
    )


def _run_tags(ctx: RunContext) -> list[dict[str, str]]:
    return [
        {"Key": "gco:project", "Value": str(ctx.config.project_name)},
        {"Key": "gco:validation-run", "Value": ctx.settings.run_id},
    ]


def _record_lifecycle_config(ctx: RunContext, region: str, name: str) -> None:
    with ctx.state_lock:
        records = ctx.checkpoint.state.setdefault(LIFECYCLE_CONFIGS_STATE, [])
        if not any(r.get("name") == name and r.get("region") == region for r in records):
            records.append({"region": region, "name": name, "recorded_at": utc_now()})
    ctx.persist()


def _start_home_probe(
    ctx: RunContext, sagemaker: Any, deployment: Mapping[str, str], names: Mapping[str, str]
) -> dict[str, Any]:
    """Start a JupyterLab app on the first user's space whose lifecycle config probes the EFS."""
    region = deployment["region"]
    account = _DISTRIBUTION_IMAGE_ACCOUNTS.get(region)
    if account is None:
        raise AnalyticsValidationError(f"No SageMaker Distribution image account for {region}")
    partition = ctx.session.get_partition_for_region(region)
    image_arn = f"arn:{partition}:sagemaker:{region}:{account}:image/sagemaker-distribution-cpu"
    content = (
        _HOME_PROBE_SCRIPT.replace("__EFS_ID__", deployment["efs_id"])
        .replace("__BUCKET__", deployment["bucket"])
        .replace("__KEY__", names["report_key"])
    )
    name = names["lifecycle_config"]
    _record_lifecycle_config(ctx, region, name)
    arn = sagemaker.create_studio_lifecycle_config(
        StudioLifecycleConfigName=name,
        StudioLifecycleConfigContent=base64.b64encode(content.encode()).decode(),
        StudioLifecycleConfigAppType=_APP_TYPE,
        Tags=_run_tags(ctx),
    )["StudioLifecycleConfigArn"]
    owner = names["first"]
    # An app may run only a lifecycle configuration its profile or domain lists.
    sagemaker.update_user_profile(
        DomainId=deployment["domain_id"],
        UserProfileName=owner,
        UserSettings={"JupyterLabAppSettings": {"LifecycleConfigArns": [arn]}},
    )
    _wait_for(
        lambda: str((_profile(sagemaker, deployment["domain_id"], owner) or {}).get("Status")),
        done={"InService"},
        failed={"Failed", "Update_Failed", "Delete_Failed"},
        what=f"Studio profile {owner}",
        timeout=_PROFILE_TIMEOUT_SECONDS,
    )
    _create_space(
        ctx,
        sagemaker,
        deployment,
        owner=owner,
        space=names["home_space"],
        lifecycle_config_arn=arn,
    )
    sagemaker.create_app(
        DomainId=deployment["domain_id"],
        SpaceName=names["home_space"],
        AppType=_APP_TYPE,
        AppName=_APP_NAME,
        ResourceSpec={
            "InstanceType": _INSTANCE_TYPE,
            "SageMakerImageArn": image_arn,
            "LifecycleConfigArn": arn,
        },
        Tags=_run_tags(ctx),
    )
    return {"lifecycle_config": name, "space": names["home_space"], "image": image_arn}


def _finish_home_probe(
    ctx: RunContext,
    sagemaker: Any,
    deployment: Mapping[str, str],
    names: Mapping[str, str],
    started: Mapping[str, Any],
) -> dict[str, Any]:
    """The app is InService and its lifecycle config wrote into the user's EFS directory."""
    space = names["home_space"]
    deadline = time.monotonic() + _APP_TIMEOUT_SECONDS
    while True:
        app = _app(sagemaker, deployment["domain_id"], space) or {}
        status = app.get("Status")
        if status == "InService":
            break
        if status in {"Failed", "Deleted", "Deleting"} or time.monotonic() >= deadline:
            raise AnalyticsValidationError(
                f"JupyterLab app in {space} is {status}: {app.get('FailureReason') or 'no reason'}"
            )
        time.sleep(_POLL_SECONDS)
    s3 = ctx.session.client("s3", region_name=deployment["region"])
    deadline = time.monotonic() + _REPORT_TIMEOUT_SECONDS
    while True:
        try:
            body = s3.get_object(Bucket=deployment["bucket"], Key=names["report_key"])["Body"]
            report = json.loads(body.read())
            break
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) not in {"NoSuchKey", "404"}:
                raise
        if time.monotonic() >= deadline:
            raise AnalyticsValidationError(
                f"The lifecycle config of {space} wrote no report in {_REPORT_TIMEOUT_SECONDS}s"
            )
        time.sleep(_POLL_SECONDS)
    if report.get("writable") is not True:
        raise AnalyticsValidationError(
            f"The notebook user cannot write its Studio EFS directory: {report.get('error')}"
        )
    if "home" in (report.get("entries") or []):
        raise AnalyticsValidationError("The space mounted the Studio EFS root, not /home/<user>")
    return {
        **started,
        "app": "InService",
        "writable": True,
        "uid": report.get("uid"),
        "gid": report.get("gid"),
    }


def _check_owner_refusal(
    ctx: RunContext,
    commands: list[dict[str, Any]],
    sagemaker: Any,
    cognito: Any,
    deployment: Mapping[str, str],
    username: str,
    first_sub: str,
) -> dict[str, Any]:
    """A new user with the first one's name is refused that profile, and cannot remove it."""
    args = [
        "--output",
        "json",
        "analytics",
        "users",
        "remove",
        "--username",
        username,
        "--keep-studio-profile",
        "--yes",
    ]
    kept = _users_remove(ctx, commands, "keep-profile-remove", args)
    if kept.get("cognito_user_deleted") is not True or kept.get("studio_profile") is not None:
        raise AnalyticsValidationError(f"users remove --keep-studio-profile reported {kept}")
    password = generate_strong_password()
    add = ["--output", "json", "analytics", "users", "add", "--username", username]
    created = _json_output(
        _checked(
            _run_gco(ctx, commands, "readd", add, secrets={"GCO_STUDIO_PASSWORD": password}),
            "readd",
        ),
        "readd",
    )
    state = created.get("password_state")
    if state != "permanent":  # nosec B105  # a state label, not a credential
        raise AnalyticsValidationError("users add --password did not set a permanent password")
    if _cognito_sub(cognito, deployment["pool_id"], username) == first_sub:
        raise AnalyticsValidationError(f"Cognito gave the new {username} the old sub")
    refused = _login(ctx, commands, "foreign-login", username, {"GCO_STUDIO_PASSWORD": password})
    evidence = _require_refusal(refused, "foreign-login", "403 UserProfileOwnedByAnotherUser")
    remove = ["--output", "json", "analytics", "users", "remove", "--username", username, "--yes"]
    removed = _users_remove(ctx, commands, "foreign-remove", remove)
    studio = removed.get("studio_profile") or {}
    if studio.get("profile") != "owned_by_another_user":
        raise AnalyticsValidationError(f"users remove acted on another user's profile: {studio}")
    if _profile(sagemaker, deployment["domain_id"], username) is None:
        raise AnalyticsValidationError(f"users remove deleted the profile {username}")
    return {"username": username, **evidence, "profile_left": True}


def _checked(
    result: subprocess.CompletedProcess[str], stage: str
) -> subprocess.CompletedProcess[str]:
    _require_exit(result, stage, 0)
    return result


def _users_remove(
    ctx: RunContext, commands: list[dict[str, Any]], stage: str, args: Sequence[str]
) -> dict[str, Any]:
    """``gco analytics users remove``, given the time its own SageMaker waits may take."""
    result = _run_gco(ctx, commands, stage, args, minimum_timeout=_USERS_REMOVE_TIMEOUT_SECONDS)
    return _json_output(_checked(result, stage), stage)


def _check_profile_removal(
    ctx: RunContext,
    commands: list[dict[str, Any]],
    sagemaker: Any,
    cognito: Any,
    deployment: Mapping[str, str],
    names: Mapping[str, str],
) -> dict[str, Any]:
    """``users remove`` deletes the user's profile with the space it owns."""
    username = names["second"]
    password = generate_strong_password()
    add = ["--output", "json", "analytics", "users", "add", "--username", username]
    _checked(
        _run_gco(ctx, commands, "second-user-add", add, secrets={"GCO_STUDIO_PASSWORD": password}),
        "second-user-add",
    )
    login = _login(ctx, commands, "second-user-login", username, {"GCO_STUDIO_PASSWORD": password})
    _studio_url(login, "second-user-login", deployment)
    sub = _cognito_sub(cognito, deployment["pool_id"], username)
    _check_profile(sagemaker, deployment, username, sub)
    _create_space(ctx, sagemaker, deployment, owner=username, space=names["second_space"])
    remove = ["--output", "json", "analytics", "users", "remove", "--username", username, "--yes"]
    removed = _users_remove(ctx, commands, "second-user-remove", remove)
    studio = removed.get("studio_profile") or {}
    if studio.get("profile") != "deleted" or studio.get("spaces") != [names["second_space"]]:
        raise AnalyticsValidationError(f"users remove reported {studio}")
    if _profile(sagemaker, deployment["domain_id"], username) is not None:
        raise AnalyticsValidationError(f"Studio profile {username} survived users remove")
    if _space_status(sagemaker, deployment["domain_id"], names["second_space"]) is not None:
        raise AnalyticsValidationError(f"Studio space {names['second_space']} survived")
    try:
        cognito.admin_get_user(UserPoolId=deployment["pool_id"], Username=username)
    except ClientError as exc:
        if not _missing(exc):
            raise
    else:
        raise AnalyticsValidationError(f"Cognito user {username} survived users remove")
    return {"username": username, "profile": "deleted", "spaces": studio.get("spaces")}


def _check_studio_role(ctx: RunContext, deployment: Mapping[str, str]) -> dict[str, Any]:
    """The execution role may do what notebooks rely on, and no Logs Insights query beyond it."""
    iam = ctx.session.client("iam")
    project = str(ctx.config.project_name)
    account = ctx.settings.expected_account
    partition = ctx.session.get_partition_for_region(deployment["region"])
    allowed: list[tuple[str, str]] = [
        ("logs:GetQueryResults", "*"),
        ("s3:PutObject", f"arn:{partition}:s3:::{deployment['bucket']}/gco-live-validation/x"),
        ("s3:GetObject", f"arn:{partition}:s3:::{deployment['bucket']}/gco-live-validation/x"),
        (
            "elasticfilesystem:ClientMount",
            f"arn:{partition}:elasticfilesystem:{deployment['region']}:{account}:"
            f"file-system/{deployment['efs_id']}",
        ),
    ]
    for region in ctx.deployment_regions:
        allowed.append(
            (
                "logs:StartQuery",
                f"arn:{partition}:logs:{region}:{account}:log-group:"
                f"/aws/containerinsights/{project}-{region}/application",
            )
        )
        allowed.append(
            ("sqs:SendMessage", f"arn:{partition}:sqs:{region}:{account}:{project}-jobs-{region}")
        )
    denied = [
        (
            "logs:StartQuery",
            f"arn:{partition}:logs:{ctx.deployment_regions[0]}:{account}:log-group:"
            f"/aws/lambda/{project}-not-granted",
        )
    ]
    decisions: list[dict[str, str]] = []
    problems: list[str] = []
    for expected_allowed, pairs in ((True, allowed), (False, denied)):
        for action, resource in pairs:
            result = iam.simulate_principal_policy(
                PolicySourceArn=deployment["execution_role"],
                ActionNames=[action],
                ResourceArns=[resource],
            )
            decision = str((result.get("EvaluationResults") or [{}])[0].get("EvalDecision"))
            decisions.append({"action": action, "resource": resource, "decision": decision})
            if (decision == "allowed") != expected_allowed:
                problems.append(f"{action} on {resource}: {decision}")
    if problems:
        raise AnalyticsValidationError("Studio execution role: " + "; ".join(problems))
    return {"decisions": decisions}


def probe_analytics(ctx: RunContext) -> dict[str, Any]:
    """Run the analytics checks; leave the first user's profile, space and app for the teardown."""
    with ctx.state_lock:
        attempt = int(ctx.checkpoint.state.get("analytics_attempts", 0)) + 1
        ctx.checkpoint.state["analytics_attempts"] = attempt
    ctx.persist()
    deployment = _deployment(ctx)
    names = _names(ctx, attempt)
    region = deployment["region"]
    sagemaker = ctx.session.client("sagemaker", region_name=region)
    cognito = ctx.session.client("cognito-idp", region_name=region)
    commands: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "attempt": attempt,
        "region": region,
        "domain_id": deployment["domain_id"],
        "domain_arn": deployment["domain_arn"],
        "home_efs_id": deployment["home_efs_id"],
        "commands": commands,
    }
    with ctx.state_lock:
        # The teardown proves the SageMaker-managed home EFS gone with the domain.
        ctx.checkpoint.state["analytics_domain"] = {
            key: evidence[key] for key in ("region", "domain_id", "domain_arn", "home_efs_id")
        }
        # Kept as it grows, so a failed step leaves its commands in the checkpoint.
        ctx.checkpoint.state["analytics_probe"] = evidence
    ctx.persist()
    evidence["doctor"] = _check_doctor(ctx, commands)
    evidence["username_rule"] = _check_username_rule(
        ctx, commands, cognito, deployment, names["refused"]
    )
    evidence["first_sign_in"], first_sub = _check_first_sign_in(
        ctx, commands, sagemaker, cognito, deployment, names["first"]
    )
    started = _start_home_probe(ctx, sagemaker, deployment, names)
    evidence["unsupported_username"] = _check_unsupported_username(
        ctx, commands, cognito, deployment, names["unsupported"]
    )
    evidence["profile_removal"] = _check_profile_removal(
        ctx, commands, sagemaker, cognito, deployment, names
    )
    evidence["owner_refusal"] = _check_owner_refusal(
        ctx, commands, sagemaker, cognito, deployment, names["first"], first_sub
    )
    evidence["studio_role"] = _check_studio_role(ctx, deployment)
    evidence["efs_home"] = _finish_home_probe(ctx, sagemaker, deployment, names, started)
    evidence["left_for_teardown"] = {
        "profile": names["first"],
        "space": names["home_space"],
        "app": f"{_APP_TYPE}/{_APP_NAME}",
        "lifecycle_config": names["lifecycle_config"],
    }
    return evidence
