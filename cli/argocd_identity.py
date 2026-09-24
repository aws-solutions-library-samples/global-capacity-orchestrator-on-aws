"""IAM Identity Center bootstrap for the hosted Argo CD capability.

Backs ``gco stacks capabilities argocd bootstrap-identity``, the
``argocd_bootstrap_identity`` MCP tool and the live-validation harness's
``argocd-identity`` action. The AWS-managed Argo CD authenticates users only
through IAM Identity Center (no local accounts), so enabling it needs an
Identity Center *instance* plus at least one user or group mapped to an Argo CD
role — inputs an operator would otherwise dig out of two consoles. Everything
here is API-driven:

* **Instance discovery** — ``sso-admin ListInstances`` in the preferred
  Region, then every Region Identity Center serves. Organization instances
  (owned by the management account) and account instances (owned by this
  account) are both found.
* **Instance creation** — an *account instance* through ``CreateInstance``,
  which a standalone account or an Organizations member account may create
  (one per account, all Regions). Never an organization instance: those are
  console-only in the management account. Creation is opt-in for the CLI and
  automatic for the harness, which deletes the instance again on teardown.
* **Group** — one Identity Center group (``get_group_id`` / ``CreateGroup``)
  mapped to an Argo CD role as ``SSO_GROUP``: a stable id that survives user
  churn and can be committed to ``cdk.json``. Existing users are added by
  user name. Group writes need an instance this account owns; the org
  instance's identity store is read-only from a member account.

What stays human: a password. ``CreateUser`` creates an identity without a
credential, so someone who wants to *open* the Argo CD UI still signs in once
through the Identity Center console (or comes from an external identity
provider). Nothing else in the hand-off needs a human — the harness proves
the capability, the repository and the sync with an empty group.

Imports nothing from ``aws_cdk``; boto3 clients are injected so the logic is
unit-testable and the harness can route them through its throttle-resilient
session.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from gco.eks_capabilities_config import ARGOCD_RBAC_ROLES

#: ``DescribeInstance`` status that means the instance can be used.
INSTANCE_ACTIVE = "ACTIVE"
#: How long an account instance may take to leave CREATE_IN_PROGRESS.
INSTANCE_ACTIVE_TIMEOUT_SECONDS = 300

#: Default names, derived from the project name at call time.
DEFAULT_INSTANCE_NAME_SUFFIX = "identity-center"
DEFAULT_GROUP_NAME_SUFFIX = "argocd-admins"
DEFAULT_ROLE = "ADMIN"


class ArgoCdIdentityError(RuntimeError):
    """The Identity Center bootstrap cannot proceed as requested."""


@dataclass(frozen=True)
class IdentityCenterInstance:
    """One Identity Center instance as ``ListInstances``/``DescribeInstance`` report it."""

    instance_arn: str
    identity_store_id: str
    region: str
    owner_account_id: str | None
    name: str | None
    status: str | None

    def owned_by(self, account_id: str) -> bool:
        return self.owner_account_id is None or self.owner_account_id == account_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BootstrapResult:
    """What the bootstrap found or created, plus the cdk.json fragment it implies."""

    instance: IdentityCenterInstance
    instance_created: bool
    group_id: str | None
    group_name: str | None
    group_created: bool
    members_added: tuple[str, ...]
    members_already_present: tuple[str, ...]
    role: str
    identities: tuple[dict[str, str], ...]

    def role_mapping(self) -> dict[str, Any]:
        return {"role": self.role, "identities": [dict(item) for item in self.identities]}

    def cdk_json_fragment(self) -> dict[str, Any]:
        """The ``eks_capabilities.argocd`` keys this result sets."""
        fragment: dict[str, Any] = {
            "idc_instance_arn": self.instance.instance_arn,
            "rbac_role_mappings": [self.role_mapping()],
        }
        fragment["idc_region"] = self.instance.region
        return fragment

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance": self.instance.to_dict(),
            "instance_created": self.instance_created,
            "group_id": self.group_id,
            "group_name": self.group_name,
            "group_created": self.group_created,
            "members_added": list(self.members_added),
            "members_already_present": list(self.members_already_present),
            "role_mapping": self.role_mapping(),
            "cdk_json_fragment": self.cdk_json_fragment(),
        }


ClientFactory = Callable[[str, str], Any]
"""``(service_name, region) -> boto3 client``; lets callers pick a session."""


def default_client_factory(service_name: str, region: str) -> Any:
    import boto3

    return boto3.client(service_name, region_name=region)


def _client_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        return str(response.get("Error", {}).get("Code") or "")
    return ""


def default_instance_name(project_name: str) -> str:
    return f"{project_name}-{DEFAULT_INSTANCE_NAME_SUFFIX}"


def default_group_name(project_name: str) -> str:
    return f"{project_name}-{DEFAULT_GROUP_NAME_SUFFIX}"


# ─── instances ───────────────────────────────────────────────────────────────


def _instance_from(item: Mapping[str, Any], region: str) -> IdentityCenterInstance:
    return IdentityCenterInstance(
        instance_arn=str(item.get("InstanceArn") or ""),
        identity_store_id=str(item.get("IdentityStoreId") or ""),
        region=region,
        owner_account_id=str(item["OwnerAccountId"]) if item.get("OwnerAccountId") else None,
        name=str(item["Name"]) if item.get("Name") else None,
        status=str(item["Status"]) if item.get("Status") else None,
    )


def list_instances(client_factory: ClientFactory, region: str) -> list[IdentityCenterInstance]:
    """Every Identity Center instance visible from this account in ``region``.

    Regions where Identity Center is not offered (or the account may not call
    it) read as "none" rather than as a failure, so a discovery sweep never
    aborts on an opt-in Region.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    client = client_factory("sso-admin", region)
    instances: list[IdentityCenterInstance] = []
    try:
        paginator = client.get_paginator("list_instances")
        for page in paginator.paginate():
            for item in page.get("Instances") or []:
                instances.append(_instance_from(item, region))
    except ClientError, BotoCoreError:
        return []
    return instances


def identity_center_regions(client_factory: ClientFactory, partition_seed_region: str) -> list[str]:
    """Regions Identity Center serves in this partition (``sso-admin`` endpoint list)."""
    import boto3

    session = boto3.session.Session()
    partition = session.get_partition_for_region(partition_seed_region)
    regions = session.get_available_regions("sso-admin", partition_name=partition)
    return sorted(set(regions))


def discover_instance(
    client_factory: ClientFactory,
    *,
    preferred_region: str,
    sweep_regions: bool = True,
    instance_arn: str | None = None,
) -> IdentityCenterInstance | None:
    """Find the instance to use: by ARN when given, else the first one found.

    Looks in ``preferred_region`` first, then (when ``sweep_regions``) in every
    other Identity Center Region — an account instance is visible only in the
    Region it was enabled in, and an organization instance only in its home
    Region, so a single-Region look would miss either.
    """
    regions = [preferred_region]
    if sweep_regions:
        regions.extend(
            region
            for region in identity_center_regions(client_factory, preferred_region)
            if region != preferred_region
        )
    for region in regions:
        for instance in list_instances(client_factory, region):
            if instance_arn is None or instance.instance_arn == instance_arn:
                return instance
    return None


def describe_instance(
    client_factory: ClientFactory, region: str, instance_arn: str
) -> IdentityCenterInstance:
    client = client_factory("sso-admin", region)
    response = client.describe_instance(InstanceArn=instance_arn)
    return _instance_from(response, region)


def create_account_instance(
    client_factory: ClientFactory,
    *,
    region: str,
    name: str,
    tags: Mapping[str, str] | None = None,
    timeout_seconds: float = INSTANCE_ACTIVE_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> IdentityCenterInstance:
    """Create an account instance of Identity Center in ``region`` and wait until ACTIVE.

    Translates the two refusals an operator can hit into plain sentences: the
    management account (which must enable an *organization* instance from the
    console) and a member account whose organization disabled account instances.
    """
    from botocore.exceptions import ClientError

    client = client_factory("sso-admin", region)
    request: dict[str, Any] = {"Name": name, "ClientToken": str(uuid.uuid4())}
    if tags:
        request["Tags"] = [{"Key": key, "Value": value} for key, value in sorted(tags.items())]
    try:
        response = client.create_instance(**request)
    except ClientError as exc:
        code = _client_error_code(exc)
        if code in {"AccessDeniedException", "ValidationException", "ConflictException"}:
            raise ArgoCdIdentityError(
                f"Identity Center refused to create an account instance in {region} ({code}: "
                f"{exc}). An Organizations management account must enable an organization "
                "instance from the Identity Center console; a member account may be blocked by "
                "its organization; an account already holding an instance in another Region "
                "must reuse it (pass its ARN / Region explicitly)."
            ) from exc
        raise
    instance_arn = str(response["InstanceArn"])
    deadline = time.monotonic() + timeout_seconds
    while True:
        instance = describe_instance(client_factory, region, instance_arn)
        if instance.status == INSTANCE_ACTIVE and instance.identity_store_id:
            return instance
        if time.monotonic() >= deadline:
            raise ArgoCdIdentityError(
                f"Identity Center instance {instance_arn} did not become {INSTANCE_ACTIVE} within "
                f"{int(timeout_seconds)}s (status {instance.status!r})"
            )
        sleep(5)


def delete_account_instance(client_factory: ClientFactory, region: str, instance_arn: str) -> bool:
    """Delete an account instance; ``False`` when it was already gone."""
    from botocore.exceptions import ClientError

    client = client_factory("sso-admin", region)
    try:
        client.delete_instance(InstanceArn=instance_arn)
    except ClientError as exc:
        if _client_error_code(exc) == "ResourceNotFoundException":
            return False
        raise
    return True


# ─── groups and members ──────────────────────────────────────────────────────


def find_group_id(client: Any, identity_store_id: str, display_name: str) -> str | None:
    from botocore.exceptions import ClientError

    try:
        response = client.get_group_id(
            IdentityStoreId=identity_store_id,
            AlternateIdentifier={
                "UniqueAttribute": {"AttributePath": "displayName", "AttributeValue": display_name}
            },
        )
    except ClientError as exc:
        if _client_error_code(exc) == "ResourceNotFoundException":
            return None
        raise
    return str(response["GroupId"])


def ensure_group(
    client: Any, *, identity_store_id: str, display_name: str, description: str
) -> tuple[str, bool]:
    """``(group_id, created)`` for the group named ``display_name``."""
    from botocore.exceptions import ClientError

    existing = find_group_id(client, identity_store_id, display_name)
    if existing is not None:
        return existing, False
    try:
        response = client.create_group(
            IdentityStoreId=identity_store_id, DisplayName=display_name, Description=description
        )
    except ClientError as exc:
        if _client_error_code(exc) == "ConflictException":
            found = find_group_id(client, identity_store_id, display_name)
            if found is not None:
                return found, False
        if _client_error_code(exc) == "AccessDeniedException":
            raise ArgoCdIdentityError(
                f"not allowed to create groups in identity store {identity_store_id} "
                f"({exc}). Groups can only be written in an instance this account owns; for an "
                "organization instance, create the group from the management account and pass "
                "it as SSO_GROUP:<id>."
            ) from exc
        raise
    return str(response["GroupId"]), True


def find_user_id(client: Any, identity_store_id: str, user_name: str) -> str:
    from botocore.exceptions import ClientError

    try:
        response = client.get_user_id(
            IdentityStoreId=identity_store_id,
            AlternateIdentifier={
                "UniqueAttribute": {"AttributePath": "userName", "AttributeValue": user_name}
            },
        )
    except ClientError as exc:
        if _client_error_code(exc) == "ResourceNotFoundException":
            raise ArgoCdIdentityError(
                f"Identity Center user {user_name!r} does not exist in identity store "
                f"{identity_store_id}; create it (and its password) in the Identity Center "
                "console first"
            ) from exc
        raise
    return str(response["UserId"])


def add_group_members(
    client: Any, *, identity_store_id: str, group_id: str, user_names: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Add existing users to the group; ``(added, already_present)`` by user name."""
    from botocore.exceptions import ClientError

    added: list[str] = []
    present: list[str] = []
    for user_name in user_names:
        user_id = find_user_id(client, identity_store_id, user_name)
        try:
            client.create_group_membership(
                IdentityStoreId=identity_store_id, GroupId=group_id, MemberId={"UserId": user_id}
            )
        except ClientError as exc:
            if _client_error_code(exc) != "ConflictException":
                raise
            present.append(user_name)
            continue
        added.append(user_name)
    return tuple(added), tuple(present)


def delete_group(client: Any, *, identity_store_id: str, group_id: str) -> bool:
    """Delete a group; ``False`` when it was already gone."""
    from botocore.exceptions import ClientError

    try:
        client.delete_group(IdentityStoreId=identity_store_id, GroupId=group_id)
    except ClientError as exc:
        if _client_error_code(exc) == "ResourceNotFoundException":
            return False
        raise
    return True


# ─── the bootstrap ───────────────────────────────────────────────────────────


def bootstrap_argocd_identity(
    *,
    account_id: str,
    project_name: str,
    preferred_region: str,
    client_factory: ClientFactory | None = None,
    instance_arn: str | None = None,
    create_account_instance_if_missing: bool = False,
    instance_name: str | None = None,
    instance_tags: Mapping[str, str] | None = None,
    group_name: str | None = None,
    role: str = DEFAULT_ROLE,
    user_names: tuple[str, ...] = (),
    identities: tuple[dict[str, str], ...] = (),
    sweep_regions: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> BootstrapResult:
    """Resolve (or create) everything ``eks_capabilities.argocd`` needs and return it.

    * With ``identities`` (pre-existing ``SSO_USER``/``SSO_GROUP`` ids) only
      the instance is resolved: no group is created, so an organization
      instance works too.
    * Otherwise a group named ``group_name`` is ensured in the instance's
      identity store (which must belong to this account) and mapped to
      ``role``; ``user_names`` are added to it.
    * No instance anywhere: create an account instance when
      ``create_account_instance_if_missing``, else stop with guidance.
    """
    if role not in ARGOCD_RBAC_ROLES:
        raise ArgoCdIdentityError(
            f"role must be one of {', '.join(ARGOCD_RBAC_ROLES)}, got {role!r}"
        )
    if client_factory is None:
        # Resolved at call time so tests and embedders can swap the factory.
        client_factory = default_client_factory
    instance = discover_instance(
        client_factory,
        preferred_region=preferred_region,
        sweep_regions=sweep_regions,
        instance_arn=instance_arn,
    )
    if instance is None and instance_arn is not None:
        raise ArgoCdIdentityError(
            f"Identity Center instance {instance_arn} was not found from this account "
            f"(looked in {preferred_region}"
            + (" and every Identity Center Region" if sweep_regions else "")
            + ")"
        )
    created = False
    if instance is None:
        if not create_account_instance_if_missing:
            raise ArgoCdIdentityError(
                "no IAM Identity Center instance is visible from this account. Enable one in "
                "the Identity Center console (an organization instance from the management "
                "account, or an account instance here), or re-run with "
                "--create-account-instance to create an account instance in "
                f"{preferred_region}."
            )
        instance = create_account_instance(
            client_factory,
            region=preferred_region,
            name=instance_name or default_instance_name(project_name),
            tags=instance_tags,
            sleep=sleep,
        )
        created = True

    if identities:
        return BootstrapResult(
            instance=instance,
            instance_created=created,
            group_id=None,
            group_name=None,
            group_created=False,
            members_added=(),
            members_already_present=(),
            role=role,
            identities=tuple(dict(item) for item in identities),
        )

    if not instance.owned_by(account_id):
        raise ArgoCdIdentityError(
            f"Identity Center instance {instance.instance_arn} belongs to account "
            f"{instance.owner_account_id} (an organization instance), whose identity store this "
            f"account cannot write. Create a group there and pass --identity SSO_GROUP:<id>, "
            "or create an account instance here with --create-account-instance."
        )
    identitystore = client_factory("identitystore", instance.region)
    resolved_group_name = group_name or default_group_name(project_name)
    group_id, group_created = ensure_group(
        identitystore,
        identity_store_id=instance.identity_store_id,
        display_name=resolved_group_name,
        description=(
            f"Members hold the Argo CD {role} role on the {project_name} EKS clusters "
            "(managed by gco stacks capabilities argocd bootstrap-identity)"
        ),
    )
    added, present = add_group_members(
        identitystore,
        identity_store_id=instance.identity_store_id,
        group_id=group_id,
        user_names=user_names,
    )
    return BootstrapResult(
        instance=instance,
        instance_created=created,
        group_id=group_id,
        group_name=resolved_group_name,
        group_created=group_created,
        members_added=added,
        members_already_present=present,
        role=role,
        identities=({"id": group_id, "type": "SSO_GROUP"},),
    )
