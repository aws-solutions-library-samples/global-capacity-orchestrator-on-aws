"""Analytics stack cleanup handler.

Runs as a CloudFormation custom resource on stack deletion. Removes all
SageMaker apps, spaces and user profiles from the Studio domain (waiting
for each to fully drain) and all EFS access points from the Studio file
system so CloudFormation can delete the domain and EFS cleanly.

If draining apps/spaces/user-profiles fails, the handler raises — this
fails the custom resource and stops CloudFormation before it tries (and
fails) to delete the domain. EFS/security-group cleanup errors are
logged but non-fatal.

Environment variables:
    DOMAIN_ID: SageMaker Studio domain ID
    EFS_ID: EFS file system ID
    REGION: AWS region
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-07-18T01:03:40Z
# Flowchart(s) generated from this file:
#   * ``handler`` -> ``diagrams/code_diagrams/lambda/analytics-cleanup/handler.handler.html``
#     (PNG: ``diagrams/code_diagrams/lambda/analytics-cleanup/handler.handler.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
#
# These constants govern how aggressively we poll SageMaker/EFS while waiting
# for asynchronous delete operations to drain. The defaults are sized for the
# worst case we've observed in production (a domain with a handful of users,
# one JupyterLab app each). If you see timeouts in CloudWatch, raise the
# ``*_WAIT_SECONDS`` values — and correspondingly bump ``timeout=`` on the
# ``CleanupFunction`` in ``gco/stacks/analytics_stack.py`` so the Lambda
# doesn't exceed its own execution timeout.
#
# Lower the values if you want the custom resource to fail fast during
# local testing, but remember that CloudFormation will then hit the domain
# delete before the async drains finish and fail with
# ``Unable to delete Domain ... because UserProfile(s) are associated``.

# Interval between list-and-check iterations of every drain wait loop.
# Small enough to keep typical stacks responsive, large enough to avoid
# throttling SageMaker/EFS control-plane APIs.
DRAIN_POLL_INTERVAL_SECONDS = 5

# Brief pause after issuing ``delete_space`` / ``delete_user_profile`` to
# avoid ThrottlingException when a domain has dozens of resources.
DELETE_PACE_SECONDS = 1

# Maximum time to wait for SageMaker apps to reach ``Deleted``/``Failed``.
# Apps typically go terminal within 30-60 seconds; 2 minutes leaves plenty
# of headroom without risking a Lambda timeout.
APP_DELETE_WAIT_SECONDS = 120

# Maximum time to wait for SageMaker spaces to disappear from ``list_spaces``.
# Spaces drain faster than user profiles but may be gated by app deletion.
SPACE_DELETE_WAIT_SECONDS = 180

# Maximum time to wait for SageMaker user profiles to disappear from
# ``list_user_profiles``. This is the critical wait — if CloudFormation
# reaches ``DeleteDomain`` with any profiles still lingering, the whole
# stack fails with a UserProfile-in-use error and has to be manually
# unstuck.
USER_PROFILE_DELETE_WAIT_SECONDS = 180

# Maximum time to wait for SageMaker-managed EFS mount targets to drain.
# Mount target deletion is usually sub-30s but can stall briefly while
# the ENIs are detached.
MOUNT_TARGET_DELETE_WAIT_SECONDS = 120

# SageMaker-managed NFS security group retry behaviour. Right after
# ``DeleteDomain``, SageMaker's control plane briefly holds an internal
# reference on one of the two NFS SGs (typically the outbound one),
# causing ``delete_security_group`` to fail with ``DependencyViolation``
# for ~30-60 seconds before clearing. We retry that many times, pausing
# between attempts; if the reference is still there on the final try we
# promote the failure to a critical error so CloudFormation stops
# before the VPC delete hits the same dependency and fails the stack.
SG_DELETE_MAX_ATTEMPTS = 4
SG_DELETE_RETRY_BACKOFF_SECONDS = 15


def _poll_iterations(total_wait_seconds: int) -> int:
    """Return the number of iterations for a drain loop with
    ``DRAIN_POLL_INTERVAL_SECONDS`` between polls."""
    return max(1, total_wait_seconds // DRAIN_POLL_INTERVAL_SECONDS)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """CloudFormation custom resource handler."""
    request_type = event.get("RequestType", "")
    physical_id = event.get("PhysicalResourceId", "analytics-cleanup")

    if request_type != "Delete":
        logger.info("RequestType=%s, nothing to do", request_type)
        return {"Status": "SUCCESS", "PhysicalResourceId": physical_id}

    region = os.environ["REGION"]
    domain_id = os.environ["DOMAIN_ID"]
    efs_id = os.environ.get("EFS_ID", "")
    vpc_id = os.environ.get("VPC_ID", "")

    errors: list[str] = []
    # Errors from deleting apps/spaces/user-profiles block the domain
    # delete — if we return SUCCESS with these still present, CloudFormation
    # will immediately fail on ``AWS::SageMaker::Domain`` with a
    # ``Unable to delete Domain ... because UserProfile(s) are associated
    # with it`` error. We track these separately so we can fail the custom
    # resource and give the operator a useful log pointer instead.
    critical_errors: list[str] = []

    # Delete all apps (must be deleted before spaces/profiles)
    app_errors = _delete_apps(region, domain_id)
    errors.extend(app_errors)
    critical_errors.extend(app_errors)

    # Delete all spaces
    space_errors = _delete_spaces(region, domain_id)
    errors.extend(space_errors)
    critical_errors.extend(space_errors)

    # Delete all user profiles from the domain
    profile_errors = _delete_user_profiles(region, domain_id)
    errors.extend(profile_errors)
    critical_errors.extend(profile_errors)

    # Remove EFS resource policies that trigger the intersection
    # authorization model. Both the CDK-managed EFS and the SageMaker-
    # managed EFS have resource policies that block DescribeMountTargets.
    if efs_id:
        _delete_efs_resource_policy(region, efs_id)
    sm_efs_id = _get_sagemaker_home_efs_id(region, domain_id)
    if sm_efs_id:
        _delete_efs_resource_policy(region, sm_efs_id)

    # Delete SageMaker-managed EFS (created internally by the domain)
    errors.extend(_delete_sagemaker_managed_efs(region, domain_id))

    # Delete SageMaker-managed NFS security groups
    if vpc_id:
        errors.extend(_delete_sagemaker_security_groups(region, domain_id, vpc_id))

    if errors:
        logger.warning("Cleanup completed with %d errors: %s", len(errors), errors)
    else:
        logger.info("Cleanup completed successfully")

    # If apps/spaces/user-profiles still have unresolved errors, fail the
    # custom resource. CloudFormation will stop before attempting to
    # delete the domain, surface the error to the operator, and the stack
    # stays in a retriable state. EFS/security-group errors are logged
    # but non-fatal — they don't block the domain delete and are cleaned
    # up best-effort on the next attempt.
    if critical_errors:
        raise RuntimeError(
            "Analytics cleanup failed to fully drain the SageMaker domain "
            f"({len(critical_errors)} error(s)). See CloudWatch Logs for "
            f"details: {critical_errors}"
        )

    return {"Status": "SUCCESS", "PhysicalResourceId": physical_id}


def _delete_apps(region: str, domain_id: str) -> list[str]:
    """Delete all apps in the domain. Apps must be deleted before spaces/profiles."""
    errors: list[str] = []
    sm = boto3.client("sagemaker", region_name=region)

    try:
        paginator = sm.get_paginator("list_apps")
        for page in paginator.paginate(DomainIdEquals=domain_id):
            for app in page.get("Apps", []):
                if app.get("Status") in ("Deleted", "Failed"):
                    continue
                app_name = app["AppName"]
                app_type = app["AppType"]
                space_name = app.get("SpaceName")
                user_profile = app.get("UserProfileName")
                try:
                    kwargs = {
                        "DomainId": domain_id,
                        "AppType": app_type,
                        "AppName": app_name,
                    }
                    if space_name:
                        kwargs["SpaceName"] = space_name
                    if user_profile:
                        kwargs["UserProfileName"] = user_profile
                    sm.delete_app(**kwargs)
                    logger.info("Deleted app: %s (%s)", app_name, app_type)
                except ClientError as e:
                    if "does not exist" not in str(e):
                        msg = f"Failed to delete app {app_name}: {e}"
                        logger.error(msg)
                        errors.append(msg)

        # Wait for apps to finish deleting.
        for _ in range(_poll_iterations(APP_DELETE_WAIT_SECONDS)):
            time.sleep(DRAIN_POLL_INTERVAL_SECONDS)
            active = []
            for page in paginator.paginate(DomainIdEquals=domain_id):
                for app in page.get("Apps", []):
                    if app.get("Status") not in ("Deleted", "Failed"):
                        active.append(app["AppName"])
            if not active:
                break
            logger.info("Waiting for %d app(s) to delete...", len(active))

    except ClientError as e:
        msg = f"Failed to list apps: {e}"
        logger.error(msg)
        errors.append(msg)

    return errors


def _delete_spaces(region: str, domain_id: str) -> list[str]:
    """Delete all spaces in the domain and wait for them to be gone.

    Spaces must be fully removed before user profiles can be deleted, and
    user profiles must be fully removed before the domain can be deleted.
    ``delete_space`` is asynchronous, so after issuing the deletes we poll
    ``list_spaces`` until it returns empty (or a timeout elapses).
    """
    errors: list[str] = []
    sm = boto3.client("sagemaker", region_name=region)

    try:
        paginator = sm.get_paginator("list_spaces")
        for page in paginator.paginate(DomainIdEquals=domain_id):
            for space in page.get("Spaces", []):
                space_name = space["SpaceName"]
                # Skip spaces already being deleted; the wait loop below
                # will still account for them.
                if space.get("Status") == "Deleting":
                    continue
                try:
                    sm.delete_space(DomainId=domain_id, SpaceName=space_name)
                    logger.info("Deleted space: %s", space_name)
                    time.sleep(DELETE_PACE_SECONDS)
                except ClientError as e:
                    if "does not exist" not in str(e):
                        msg = f"Failed to delete space {space_name}: {e}"
                        logger.error(msg)
                        errors.append(msg)

        # Wait for spaces to finish deleting.
        for _ in range(_poll_iterations(SPACE_DELETE_WAIT_SECONDS)):
            remaining: list[str] = []
            for page in paginator.paginate(DomainIdEquals=domain_id):
                for space in page.get("Spaces", []):
                    remaining.append(space["SpaceName"])
            if not remaining:
                break
            logger.info("Waiting for %d space(s) to delete: %s", len(remaining), remaining)
            time.sleep(DRAIN_POLL_INTERVAL_SECONDS)
        else:
            msg = f"Timed out waiting for spaces to delete in {domain_id}: {remaining}"
            logger.error(msg)
            errors.append(msg)
    except ClientError as e:
        msg = f"Failed to list spaces: {e}"
        logger.error(msg)
        errors.append(msg)

    return errors


def _delete_user_profiles(region: str, domain_id: str) -> list[str]:
    """Delete all user profiles in the domain and wait for them to be gone.

    ``delete_user_profile`` is asynchronous — it puts the profile into
    ``Deleting`` state and returns immediately. If we don't wait for the
    list to drain, CloudFormation will race ahead to delete the domain
    and fail with ``Unable to delete Domain ... because UserProfile(s)
    are associated with it``. This function polls ``list_user_profiles``
    until it's empty (or a timeout elapses).
    """
    errors: list[str] = []
    sm = boto3.client("sagemaker", region_name=region)

    try:
        paginator = sm.get_paginator("list_user_profiles")
        for page in paginator.paginate(DomainIdEquals=domain_id):
            for profile in page.get("UserProfiles", []):
                name = profile["UserProfileName"]
                # Skip profiles already being deleted; the wait loop below
                # will still account for them.
                if profile.get("Status") == "Deleting":
                    continue
                try:
                    sm.delete_user_profile(DomainId=domain_id, UserProfileName=name)
                    logger.info("Deleted user profile: %s", name)
                    # Brief pause to avoid throttling
                    time.sleep(DELETE_PACE_SECONDS)
                except ClientError as e:
                    msg = f"Failed to delete profile {name}: {e}"
                    logger.error(msg)
                    errors.append(msg)

        # Wait for profiles to finish deleting. Without this, CloudFormation
        # will race ahead to delete the domain while profiles are still in
        # ``Deleting`` state and fail the stack.
        remaining: list[str] = []
        for _ in range(_poll_iterations(USER_PROFILE_DELETE_WAIT_SECONDS)):
            remaining = []
            for page in paginator.paginate(DomainIdEquals=domain_id):
                for profile in page.get("UserProfiles", []):
                    remaining.append(profile["UserProfileName"])
            if not remaining:
                break
            logger.info(
                "Waiting for %d user profile(s) to delete: %s",
                len(remaining),
                remaining,
            )
            time.sleep(DRAIN_POLL_INTERVAL_SECONDS)
        else:
            msg = f"Timed out waiting for user profiles to delete in {domain_id}: {remaining}"
            logger.error(msg)
            errors.append(msg)
    except ClientError as e:
        msg = f"Failed to list user profiles: {e}"
        logger.error(msg)
        errors.append(msg)

    return errors


def _delete_access_points(region: str, efs_id: str) -> list[str]:
    """Delete all access points on the file system. Returns a list of error messages."""
    errors: list[str] = []
    efs = boto3.client("efs", region_name=region)

    try:
        paginator = efs.get_paginator("describe_access_points")
        for page in paginator.paginate(FileSystemId=efs_id):
            for ap in page.get("AccessPoints", []):
                ap_id = ap["AccessPointId"]
                try:
                    efs.delete_access_point(AccessPointId=ap_id)
                    logger.info("Deleted access point: %s", ap_id)
                except ClientError as e:
                    msg = f"Failed to delete access point {ap_id}: {e}"
                    logger.error(msg)
                    errors.append(msg)
    except ClientError as e:
        msg = f"Failed to list access points: {e}"
        logger.error(msg)
        errors.append(msg)

    return errors


def _delete_sagemaker_security_groups(region: str, domain_id: str, vpc_id: str) -> list[str]:
    """Delete SageMaker-managed security groups for the domain.

    SageMaker creates security groups named
    ``security-group-for-outbound-nfs-<domain-id>`` and
    ``security-group-for-inbound-nfs-<domain-id>`` when the domain uses
    a custom EFS. These are tagged "[DO NOT DELETE]" but must be removed
    for the VPC to be deletable.

    The two SGs cross-reference each other (outbound rules on one point
    to the other), creating a circular dependency. We must revoke all
    ingress/egress rules before deleting.

    Right after ``DeleteDomain``, SageMaker's control plane briefly
    retains an internal reference on one of the NFS SGs — typically the
    outbound one — causing ``delete_security_group`` to fail with
    ``DependencyViolation``. The reference reliably clears within 30-60s.
    We retry the delete ``SG_DELETE_MAX_ATTEMPTS`` times with
    ``SG_DELETE_RETRY_BACKOFF_SECONDS`` between attempts, and only
    surface an error if the SG is still undeletable after the final try.
    """
    errors: list[str] = []
    ec2 = boto3.client("ec2", region_name=region)

    try:
        response = ec2.describe_security_groups(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "group-name", "Values": [f"*{domain_id}*"]},
            ]
        )
        sgs = response.get("SecurityGroups", [])

        # First pass: revoke all rules to break cross-references.
        for sg in sgs:
            sg_id = sg["GroupId"]
            try:
                if sg.get("IpPermissions"):
                    ec2.revoke_security_group_ingress(
                        GroupId=sg_id, IpPermissions=sg["IpPermissions"]
                    )
                if sg.get("IpPermissionsEgress"):
                    ec2.revoke_security_group_egress(
                        GroupId=sg_id, IpPermissions=sg["IpPermissionsEgress"]
                    )
            except ClientError as e:
                logger.warning("Failed to revoke rules on %s: %s", sg_id, e)

        # Second pass: delete the security groups, retrying on
        # ``DependencyViolation`` for ones where SageMaker still holds
        # a transient reference post-DeleteDomain.
        pending = [(sg["GroupId"], sg.get("GroupName", "")) for sg in sgs]
        for attempt in range(1, SG_DELETE_MAX_ATTEMPTS + 1):
            still_pending: list[tuple[str, str]] = []
            for sg_id, sg_name in pending:
                try:
                    ec2.delete_security_group(GroupId=sg_id)
                    logger.info("Deleted SageMaker security group: %s (%s)", sg_id, sg_name)
                except ClientError as e:
                    code = e.response.get("Error", {}).get("Code", "")
                    # ``InvalidGroup.NotFound`` means some other actor
                    # already deleted it — treat as success.
                    if code == "InvalidGroup.NotFound":
                        logger.info("SG %s (%s) already deleted", sg_id, sg_name)
                        continue
                    if code == "DependencyViolation":
                        logger.warning(
                            "SG %s (%s) has a dependent object (attempt %d/%d); will retry",
                            sg_id,
                            sg_name,
                            attempt,
                            SG_DELETE_MAX_ATTEMPTS,
                        )
                        still_pending.append((sg_id, sg_name))
                        continue
                    msg = f"Failed to delete security group {sg_id}: {e}"
                    logger.error(msg)
                    errors.append(msg)
            if not still_pending:
                break
            pending = still_pending
            if attempt < SG_DELETE_MAX_ATTEMPTS:
                time.sleep(SG_DELETE_RETRY_BACKOFF_SECONDS)
        else:
            # Exhausted retries with SGs still undeletable.
            for sg_id, sg_name in pending:
                msg = (
                    f"Failed to delete security group {sg_id} ({sg_name}) "
                    f"after {SG_DELETE_MAX_ATTEMPTS} attempts: "
                    "DependencyViolation did not clear. CloudFormation "
                    "will fail to delete the VPC; manually delete the "
                    "SG after its dependent object releases."
                )
                logger.error(msg)
                errors.append(msg)
    except ClientError as e:
        msg = f"Failed to list SageMaker security groups: {e}"
        logger.error(msg)
        errors.append(msg)

    return errors


def _get_sagemaker_home_efs_id(region: str, domain_id: str) -> str:
    """Get the HomeEfsFileSystemId from the SageMaker domain."""
    sm = boto3.client("sagemaker", region_name=region)
    try:
        resp = sm.describe_domain(DomainId=domain_id)
        return str(resp.get("HomeEfsFileSystemId", ""))
    except ClientError as e:
        logger.warning("Failed to get HomeEfsFileSystemId for %s: %s", domain_id, e)
        return ""


def _delete_efs_resource_policy(region: str, efs_id: str) -> None:
    """Delete the resource policy on the CDK-managed EFS.

    The EFS resource policy triggers the intersection authorization model
    which blocks DescribeFileSystems/DescribeAccessPoints calls even when
    the caller has IAM Resource:* permissions. Removing the policy before
    other EFS operations ensures they succeed.
    """
    efs_client = boto3.client("efs", region_name=region)
    try:
        efs_client.delete_file_system_policy(FileSystemId=efs_id)
        logger.info("Deleted EFS resource policy on %s", efs_id)
    except ClientError as e:
        # PolicyNotFound is fine — means there's no policy to delete.
        if "PolicyNotFound" not in str(e):
            logger.warning("Failed to delete EFS resource policy on %s: %s", efs_id, e)


def _delete_sagemaker_managed_efs(region: str, domain_id: str) -> list[str]:
    """Delete the SageMaker-managed EFS created internally by the domain.

    Uses sagemaker:DescribeDomain to get the HomeEfsFileSystemId directly,
    avoiding DescribeFileSystems which is blocked by the EFS resource
    policy intersection model. The domain still exists when this Lambda
    runs (it's deleted after the custom resource completes).
    """
    errors: list[str] = []
    sm = boto3.client("sagemaker", region_name=region)
    efs = boto3.client("efs", region_name=region)

    try:
        # Get the EFS ID from the domain itself — no DescribeFileSystems needed.
        domain_info = sm.describe_domain(DomainId=domain_id)
        target_fs = domain_info.get("HomeEfsFileSystemId")
        if not target_fs:
            logger.info("No HomeEfsFileSystemId found for domain %s", domain_id)
            return errors

        logger.info("Found SageMaker-managed EFS: %s", target_fs)

        # Delete all mount targets first
        mt_response = efs.describe_mount_targets(FileSystemId=target_fs)
        for mt in mt_response.get("MountTargets", []):
            mt_id = mt["MountTargetId"]
            try:
                efs.delete_mount_target(MountTargetId=mt_id)
                logger.info("Deleted mount target: %s", mt_id)
            except ClientError as e:
                msg = f"Failed to delete mount target {mt_id}: {e}"
                logger.error(msg)
                errors.append(msg)

        # Wait for mount targets to be deleted.
        for _ in range(_poll_iterations(MOUNT_TARGET_DELETE_WAIT_SECONDS)):
            time.sleep(DRAIN_POLL_INTERVAL_SECONDS)
            remaining = efs.describe_mount_targets(FileSystemId=target_fs)
            if not remaining.get("MountTargets"):
                break

        # Delete the file system
        try:
            efs.delete_file_system(FileSystemId=target_fs)
            logger.info("Deleted SageMaker-managed EFS: %s", target_fs)
        except ClientError as e:
            msg = f"Failed to delete EFS {target_fs}: {e}"
            logger.error(msg)
            errors.append(msg)

    except ClientError as e:
        msg = f"Failed to find/delete SageMaker-managed EFS: {e}"
        logger.error(msg)
        errors.append(msg)

    return errors
