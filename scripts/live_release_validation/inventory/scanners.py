"""Per-service read-only scanners for project-owned resources.

Each ``_list_*`` helper answers one question — which resources of this
service does this project own in this Region — and fails closed by
returning nothing rather than guessing when a tag or name is ambiguous.
``project.collect_project_resources`` fans these out."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from botocore.exceptions import ClientError

from ._shared import (
    _GLOBAL_ACCELERATOR_CONTROL_REGIONS,
    _arn_is_project_owned,
    _ec2_resource_is_project_owned,
    _iam_resource_is_project_owned,
    _mapping_tags,
    _name_or_path_is_project_owned,
    _project_owned_name,
    _tags_are_project_owned,
    _tags_to_dict,
)
from .ecr import (
    collect_ecr_inventory,
)


def _list_eks_clusters(
    session: Any,
    region: str,
    project_name: str | None,
) -> list[str]:
    """List all clusters, optionally narrowing the authoritative result to the project."""
    client = session.client("eks", region_name=region)
    names: set[str] = set()
    for page in client.get_paginator("list_clusters").paginate():
        for raw_name in page.get("clusters", []):
            name = str(raw_name or "")
            if not name:
                raise RuntimeError(f"EKS returned a cluster without a name in {region}")
            names.add(name)
    if project_name is None:
        return sorted(names)
    return sorted(name for name in names if _project_owned_name(name, project_name))


def _list_sqs_queues(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("sqs", region_name=region)
    urls: list[str] = []
    for page in client.get_paginator("list_queues").paginate(QueueNamePrefix=project_name):
        for queue_url in page.get("QueueUrls", []):
            queue_name = urlparse(str(queue_url)).path.rsplit("/", 1)[-1]
            if _project_owned_name(queue_name, project_name):
                urls.append(str(queue_url))
    return sorted(set(urls))


def _list_dynamodb_tables(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("dynamodb", region_name=region)
    names: list[str] = []
    for page in client.get_paginator("list_tables").paginate():
        names.extend(
            str(name)
            for name in page.get("TableNames", [])
            if _project_owned_name(str(name), project_name)
        )
    return sorted(set(names))


def _list_load_balancers(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("elbv2", region_name=region)
    load_balancers: list[dict[str, Any]] = []
    for page in client.get_paginator("describe_load_balancers").paginate():
        load_balancers.extend(page.get("LoadBalancers", []))

    owned: list[str] = []
    for start in range(0, len(load_balancers), 20):
        batch = load_balancers[start : start + 20]
        arns = [str(item["LoadBalancerArn"]) for item in batch]
        tags_by_arn = {
            str(item["ResourceArn"]): _tags_to_dict(item.get("Tags", []))
            for item in client.describe_tags(ResourceArns=arns).get("TagDescriptions", [])
        }
        for load_balancer in batch:
            arn = str(load_balancer["LoadBalancerArn"])
            name = str(load_balancer.get("LoadBalancerName") or "")
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags_by_arn.get(arn, {}), project_name
            ):
                owned.append(arn)
    return sorted(set(owned))


def _list_instance_inventory(
    session: Any,
    region: str,
    project_name: str,
) -> tuple[list[str], list[str]]:
    """Return project-owned and all active EC2 instance IDs separately."""
    client = session.client("ec2", region_name=region)
    state_filter = {
        "Name": "instance-state-name",
        "Values": ["pending", "running", "stopping", "stopped", "shutting-down"],
    }
    project_instance_ids: set[str] = set()
    all_instance_ids: set[str] = set()
    paginator = client.get_paginator("describe_instances")
    for page in paginator.paginate(Filters=[state_filter]):
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = str(instance.get("InstanceId") or "")
                if not instance_id:
                    raise RuntimeError(f"EC2 returned an instance without an ID in {region}")
                all_instance_ids.add(instance_id)
                if _ec2_resource_is_project_owned(instance, project_name):
                    project_instance_ids.add(instance_id)
    return sorted(project_instance_ids), sorted(all_instance_ids)


def _list_instances(session: Any, region: str, project_name: str) -> list[str]:
    return _list_instance_inventory(session, region, project_name)[0]


def _list_project_kms_keys(
    session: Any,
    region: str,
    project_name: str,
    validation_run_id: str | None = None,
) -> list[dict[str, Any]]:
    """List project keys while isolating prior validation runs pending deletion."""
    client = session.client("kms", region_name=region)
    keys: list[dict[str, Any]] = []
    for page in client.get_paginator("list_keys").paginate():
        for summary in page.get("Keys", []):
            key_id = str(summary.get("KeyId") or "")
            if not key_id:
                continue
            metadata = client.describe_key(KeyId=key_id).get("KeyMetadata", {})
            if metadata.get("KeyManager") != "CUSTOMER":
                continue
            tags: dict[str, str] = {}
            marker: str | None = None
            while True:
                kwargs = {"KeyId": key_id}
                if marker:
                    kwargs["Marker"] = marker
                response = client.list_resource_tags(**kwargs)
                tags.update(
                    {
                        str(tag["TagKey"]): str(tag.get("TagValue") or "")
                        for tag in response.get("Tags", [])
                        if tag.get("TagKey") is not None
                    }
                )
                marker = response.get("NextMarker") if response.get("Truncated") else None
                if not marker:
                    break
            project_owned = _tags_are_project_owned(tags, project_name)
            validation_owner = tags.get("GcoLiveValidationRun")
            state = str(metadata.get("KeyState") or "")
            if validation_run_id:
                if validation_owner and validation_owner != validation_run_id:
                    if state == "PendingDeletion":
                        # Successful runs leave exact keys pending for seven days.
                        # They are neither baseline contamination nor authority for
                        # this run; active keys from another run still fail closed.
                        continue
                elif not validation_owner and not project_owned:
                    continue
            elif not project_owned:
                continue
            deletion_date = metadata.get("DeletionDate")
            keys.append(
                {
                    "key_id": key_id,
                    "arn": str(metadata.get("Arn") or ""),
                    "state": str(metadata.get("KeyState") or ""),
                    "description": str(metadata.get("Description") or ""),
                    "deletion_date": (
                        deletion_date.isoformat() if deletion_date is not None else None
                    ),
                    "tags": tags,
                }
            )
    return sorted(keys, key=lambda item: (item["arn"], item["key_id"]))


def _list_project_ecr_repositories(
    session: Any,
    region: str,
    project_name: str,
) -> list[str]:
    repositories = collect_ecr_inventory(session, [region])[region]
    return sorted(
        item["name"] for item in repositories if _project_owned_name(item["name"], project_name)
    )


def _global_accelerator_control_region(session: Any, seed_region: str) -> str | None:
    """Return the partition's supported Global Accelerator control Region."""
    partition = session.get_partition_for_region(seed_region)
    control_region = _GLOBAL_ACCELERATOR_CONTROL_REGIONS.get(str(partition))
    if control_region is None:
        return None
    service_regions = set(
        session.get_available_regions("globalaccelerator", partition_name=partition)
    )
    if control_region not in service_regions:
        raise RuntimeError(
            "AWS SDK does not advertise the required Global Accelerator control Region "
            f"{control_region} for partition {partition}"
        )
    return control_region


def _list_global_accelerators(
    session: Any,
    control_region: str | None,
    project_name: str,
) -> list[str]:
    if control_region is None:
        return []
    client = session.client("globalaccelerator", region_name=control_region)
    accelerators: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs = {"NextToken": token} if token else {}
        response = client.list_accelerators(**kwargs)
        accelerators.extend(response.get("Accelerators", []))
        token = response.get("NextToken")
        if not token:
            break

    owned: list[str] = []
    for accelerator in accelerators:
        arn = str(accelerator.get("AcceleratorArn") or "")
        name = str(accelerator.get("Name") or "")
        tags = _tags_to_dict(client.list_tags_for_resource(ResourceArn=arn).get("Tags", []))
        if _project_owned_name(name, project_name) or _tags_are_project_owned(tags, project_name):
            owned.append(arn)
    return sorted(set(owned))


def _list_project_tagged_resources(
    session: Any,
    region: str,
    project_name: str,
) -> list[dict[str, Any]]:
    """List project-scoped resources exposed by the regional Tagging API."""
    client = session.client("resourcegroupstaggingapi", region_name=region)
    resources: dict[str, dict[str, Any]] = {}
    for page in client.get_paginator("get_resources").paginate():
        for mapping in page.get("ResourceTagMappingList", []):
            arn = str(mapping.get("ResourceARN") or "")
            if not arn:
                raise RuntimeError(f"Resource Groups Tagging API omitted an ARN in {region}")
            tags = _tags_to_dict(mapping.get("Tags", []))
            if _tags_are_project_owned(tags, project_name) or _arn_is_project_owned(
                arn, project_name
            ):
                resources[arn] = {"arn": arn, "tags": tags}
    return [resources[arn] for arn in sorted(resources)]


def _ec2_items(client: Any, operation: str, response_key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in client.get_paginator(operation).paginate():
        items.extend(page.get(response_key, []))
    return items


_CLUSTER_TAG_PREFIX = "kubernetes.io/cluster/"


def _list_cluster_volumes(session: Any, region: str, project_name: str) -> list[str]:
    """Return EBS volumes tagged for a project cluster by the EKS CSI driver.

    These are the only project resources whose sole ownership marker is a
    Kubernetes tag: the CSI driver writes ``kubernetes.io/cluster/<cluster>``
    and never the CloudFormation or ``gco:project`` tags every other scanner
    matches on. Deleting a cluster does not delete the PersistentVolumes its
    driver provisioned, so without this scanner a teardown could strand
    billable volumes and still pass the all-zero final-inventory gate.

    Ownership is decided from the cluster name inside the tag key, which is the
    regional stack name, so a volume belonging to another project's cluster in
    the same account is never claimed. State is deliberately not filtered: any
    surviving volume tagged for a project cluster is residual, whether it is
    ``available``, still detaching, or in ``error``.

    Enumerates unfiltered and matches client-side, like the other EC2 scanners
    here. A server-side ``tag-key`` filter would need wildcard semantics to
    match the cluster-name suffix, and depending on that in the gate that
    authorizes calling a teardown clean is not worth the round-trip saved.
    """
    client = session.client("ec2", region_name=region)
    volume_ids: set[str] = set()
    for volume in _ec2_items(client, "describe_volumes", "Volumes"):
        volume_id = str(volume.get("VolumeId") or "")
        if not volume_id:
            raise RuntimeError(f"EC2 returned a volume without an ID in {region}")
        for key in _tags_to_dict(volume.get("Tags", [])):
            if not key.startswith(_CLUSTER_TAG_PREFIX):
                continue
            cluster_name = key[len(_CLUSTER_TAG_PREFIX) :]
            if cluster_name and _project_owned_name(cluster_name, project_name):
                volume_ids.add(volume_id)
                break
    return sorted(volume_ids)


def _list_project_ec2_networking(
    session: Any,
    region: str,
    project_name: str,
    project_instance_ids: Iterable[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return project-owned networking and unfiltered live ID authority separately."""
    client = session.client("ec2", region_name=region)

    vpc_ids: set[str] = set()
    all_vpc_ids: set[str] = set()
    for vpc in _ec2_items(client, "describe_vpcs", "Vpcs"):
        vpc_id = str(vpc.get("VpcId") or "")
        if not vpc_id:
            raise RuntimeError(f"EC2 returned a VPC without an ID in {region}")
        all_vpc_ids.add(vpc_id)
        if _ec2_resource_is_project_owned(vpc, project_name):
            vpc_ids.add(vpc_id)

    subnet_ids: set[str] = set()
    all_subnet_ids: set[str] = set()
    for subnet in _ec2_items(client, "describe_subnets", "Subnets"):
        subnet_id = str(subnet.get("SubnetId") or "")
        if not subnet_id:
            raise RuntimeError(f"EC2 returned a subnet without an ID in {region}")
        all_subnet_ids.add(subnet_id)
        if (
            _ec2_resource_is_project_owned(subnet, project_name)
            or str(subnet.get("VpcId") or "") in vpc_ids
        ):
            subnet_ids.add(subnet_id)

    nat_gateway_ids: set[str] = set()
    all_nat_gateway_ids: set[str] = set()
    for nat_gateway in _ec2_items(client, "describe_nat_gateways", "NatGateways"):
        nat_gateway_id = str(nat_gateway.get("NatGatewayId") or "")
        if not nat_gateway_id:
            raise RuntimeError(f"EC2 returned a NAT gateway without an ID in {region}")
        if str(nat_gateway.get("State") or "") == "deleted":
            continue
        all_nat_gateway_ids.add(nat_gateway_id)
        if (
            _ec2_resource_is_project_owned(nat_gateway, project_name)
            or str(nat_gateway.get("VpcId") or "") in vpc_ids
            or str(nat_gateway.get("SubnetId") or "") in subnet_ids
        ):
            nat_gateway_ids.add(nat_gateway_id)

    security_group_ids: set[str] = set()
    all_security_group_ids: set[str] = set()
    for security_group in _ec2_items(client, "describe_security_groups", "SecurityGroups"):
        group_id = str(security_group.get("GroupId") or "")
        if not group_id:
            raise RuntimeError(f"EC2 returned a security group without an ID in {region}")
        all_security_group_ids.add(group_id)
        if (
            _ec2_resource_is_project_owned(security_group, project_name)
            or _project_owned_name(str(security_group.get("GroupName") or ""), project_name)
            or str(security_group.get("VpcId") or "") in vpc_ids
        ):
            security_group_ids.add(group_id)

    instance_ids = set(project_instance_ids)
    network_interface_ids: set[str] = set()
    all_network_interface_ids: set[str] = set()
    for interface in _ec2_items(
        client,
        "describe_network_interfaces",
        "NetworkInterfaces",
    ):
        interface_id = str(interface.get("NetworkInterfaceId") or "")
        if not interface_id:
            raise RuntimeError(f"EC2 returned a network interface without an ID in {region}")
        all_network_interface_ids.add(interface_id)
        group_ids = {str(group.get("GroupId") or "") for group in interface.get("Groups", [])}
        attachment_instance_id = str((interface.get("Attachment") or {}).get("InstanceId") or "")
        if (
            _ec2_resource_is_project_owned(interface, project_name)
            or str(interface.get("VpcId") or "") in vpc_ids
            or str(interface.get("SubnetId") or "") in subnet_ids
            or bool(group_ids & security_group_ids)
            or attachment_instance_id in instance_ids
        ):
            network_interface_ids.add(interface_id)

    flow_log_ids: set[str] = set()
    all_flow_log_ids: set[str] = set()
    project_network_resource_ids = vpc_ids | subnet_ids | network_interface_ids | instance_ids
    for flow_log in _ec2_items(client, "describe_flow_logs", "FlowLogs"):
        flow_log_id = str(flow_log.get("FlowLogId") or "")
        if not flow_log_id:
            raise RuntimeError(f"EC2 returned a flow log without an ID in {region}")
        all_flow_log_ids.add(flow_log_id)
        if (
            _ec2_resource_is_project_owned(flow_log, project_name)
            or str(flow_log.get("ResourceId") or "") in project_network_resource_ids
        ):
            flow_log_ids.add(flow_log_id)

    elastic_ip_ids: set[str] = set()
    all_elastic_ip_ids: set[str] = set()
    for address in client.describe_addresses().get("Addresses", []):
        identifier = str(address.get("AllocationId") or address.get("PublicIp") or "")
        if not identifier:
            raise RuntimeError(f"EC2 returned an Elastic IP without an identity in {region}")
        all_elastic_ip_ids.add(identifier)
        if (
            _ec2_resource_is_project_owned(address, project_name)
            or str(address.get("NetworkInterfaceId") or "") in network_interface_ids
            or str(address.get("InstanceId") or "") in instance_ids
        ):
            elastic_ip_ids.add(identifier)

    project_resources = {
        "vpcs": sorted(vpc_ids),
        "subnets": sorted(subnet_ids),
        "nat_gateways": sorted(nat_gateway_ids),
        "flow_logs": sorted(flow_log_ids),
        "network_interfaces": sorted(network_interface_ids),
        "security_groups": sorted(security_group_ids),
        "elastic_ips": sorted(elastic_ip_ids),
    }
    authoritative_resources = {
        "vpcs": sorted(all_vpc_ids),
        "subnets": sorted(all_subnet_ids),
        "nat_gateways": sorted(all_nat_gateway_ids),
        "flow_logs": sorted(all_flow_log_ids),
        "network_interfaces": sorted(all_network_interface_ids),
        "security_groups": sorted(all_security_group_ids),
        "elastic_ips": sorted(all_elastic_ip_ids),
    }
    return project_resources, authoritative_resources


def _list_lambda_functions(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("lambda", region_name=region)
    functions: set[str] = set()
    for page in client.get_paginator("list_functions").paginate():
        for function in page.get("Functions", []):
            name = str(function.get("FunctionName") or "")
            arn = str(function.get("FunctionArn") or "")
            if not name or not arn:
                raise RuntimeError(f"Lambda returned a function without identity in {region}")
            tags = _mapping_tags(client.list_tags(Resource=arn).get("Tags"))
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                functions.add(arn)
    return sorted(functions)


def _list_api_gateway_v1_apis(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("apigateway", region_name=region)
    apis: set[str] = set()
    for page in client.get_paginator("get_rest_apis").paginate():
        for api in page.get("items", []):
            api_id = str(api.get("id") or "")
            name = str(api.get("name") or "")
            tags = _mapping_tags(api.get("tags"))
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                if not api_id:
                    raise RuntimeError(f"API Gateway v1 returned an API without an ID in {region}")
                apis.add(api_id)
    return sorted(apis)


def _list_api_gateway_v2_apis(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("apigatewayv2", region_name=region)
    apis: set[str] = set()
    for page in client.get_paginator("get_apis").paginate():
        for api in page.get("Items", []):
            api_id = str(api.get("ApiId") or "")
            name = str(api.get("Name") or "")
            tags = _mapping_tags(api.get("Tags"))
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                if not api_id:
                    raise RuntimeError(f"API Gateway v2 returned an API without an ID in {region}")
                apis.add(api_id)
    return sorted(apis)


def _list_cloudwatch_log_groups(
    session: Any,
    region: str,
    project_name: str,
) -> list[str]:
    client = session.client("logs", region_name=region)
    log_groups: set[str] = set()
    for page in client.get_paginator("describe_log_groups").paginate():
        for log_group in page.get("logGroups", []):
            name = str(log_group.get("logGroupName") or "")
            arn = str(log_group.get("logGroupArn") or log_group.get("arn") or "").removesuffix(":*")
            if not name or not arn:
                raise RuntimeError(
                    f"CloudWatch Logs returned a log group without identity in {region}"
                )
            tags = _mapping_tags(client.list_tags_for_resource(resourceArn=arn).get("tags"))
            if _name_or_path_is_project_owned(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                log_groups.add(name)
    return sorted(log_groups)


def _list_secrets(session: Any, region: str, project_name: str) -> list[str]:
    client = session.client("secretsmanager", region_name=region)
    secrets: set[str] = set()
    for page in client.get_paginator("list_secrets").paginate(IncludePlannedDeletion=True):
        for secret in page.get("SecretList", []):
            name = str(secret.get("Name") or "")
            arn = str(secret.get("ARN") or "")
            tags = _tags_to_dict(secret.get("Tags", []))
            if _project_owned_name(name, project_name) or _tags_are_project_owned(
                tags, project_name
            ):
                if not arn:
                    raise RuntimeError(
                        f"Secrets Manager returned a project-owned secret without an ARN in {region}"
                    )
                secrets.add(arn)
    return sorted(secrets)


def _list_s3_bucket_tags(client: Any, bucket_name: str) -> dict[str, str]:
    try:
        response = client.get_bucket_tagging(Bucket=bucket_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchTagSet", "NoSuchTagSetError"}:
            return {}
        raise
    return _tags_to_dict(response.get("TagSet", []))


def _list_project_s3_buckets(session: Any, seed_region: str, project_name: str) -> list[str]:
    client = session.client("s3", region_name=seed_region)
    buckets: set[str] = set()
    for bucket in client.list_buckets().get("Buckets", []):
        name = str(bucket.get("Name") or "")
        if not name:
            raise RuntimeError("S3 returned a bucket without a name")
        tags = _list_s3_bucket_tags(client, name)
        if _project_owned_name(name, project_name) or _tags_are_project_owned(tags, project_name):
            buckets.add(name)
    return sorted(buckets)


def _list_iam_tags(
    client: Any,
    operation: str,
    identifier_name: str,
    identifier: str,
) -> dict[str, str]:
    tags: dict[str, str] = {}
    marker: str | None = None
    while True:
        kwargs: dict[str, Any] = {identifier_name: identifier}
        if marker:
            kwargs["Marker"] = marker
        response = getattr(client, operation)(**kwargs)
        tags.update(_tags_to_dict(response.get("Tags", [])))
        if not response.get("IsTruncated"):
            return tags
        marker = str(response.get("Marker") or "")
        if not marker:
            raise RuntimeError(f"IAM {operation} truncated its response without a Marker")


def _list_project_iam_resources(
    session: Any,
    seed_region: str,
    project_name: str,
) -> dict[str, list[str]]:
    client = session.client("iam", region_name=seed_region)
    resources: dict[str, set[str]] = {
        "iam_roles": set(),
        "iam_policies": set(),
        "iam_instance_profiles": set(),
        "iam_users": set(),
        "iam_groups": set(),
    }

    for page in client.get_paginator("list_roles").paginate():
        for role in page.get("Roles", []):
            name = str(role.get("RoleName") or "")
            arn = str(role.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned a role without identity")
            tags = _list_iam_tags(client, "list_role_tags", "RoleName", name)
            if _iam_resource_is_project_owned(
                name, str(role.get("Path") or ""), tags, project_name
            ):
                resources["iam_roles"].add(arn)

    for page in client.get_paginator("list_policies").paginate(Scope="Local"):
        for policy in page.get("Policies", []):
            name = str(policy.get("PolicyName") or "")
            arn = str(policy.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned a customer-managed policy without identity")
            tags = _list_iam_tags(client, "list_policy_tags", "PolicyArn", arn)
            if _iam_resource_is_project_owned(
                name, str(policy.get("Path") or ""), tags, project_name
            ):
                resources["iam_policies"].add(arn)

    for page in client.get_paginator("list_instance_profiles").paginate():
        for profile in page.get("InstanceProfiles", []):
            name = str(profile.get("InstanceProfileName") or "")
            arn = str(profile.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned an instance profile without identity")
            tags = _list_iam_tags(
                client,
                "list_instance_profile_tags",
                "InstanceProfileName",
                name,
            )
            if _iam_resource_is_project_owned(
                name, str(profile.get("Path") or ""), tags, project_name
            ):
                resources["iam_instance_profiles"].add(arn)

    for page in client.get_paginator("list_users").paginate():
        for user in page.get("Users", []):
            name = str(user.get("UserName") or "")
            arn = str(user.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned a user without identity")
            tags = _list_iam_tags(client, "list_user_tags", "UserName", name)
            if _iam_resource_is_project_owned(
                name, str(user.get("Path") or ""), tags, project_name
            ):
                resources["iam_users"].add(arn)

    for page in client.get_paginator("list_groups").paginate():
        for group in page.get("Groups", []):
            name = str(group.get("GroupName") or "")
            arn = str(group.get("Arn") or "")
            if not name or not arn:
                raise RuntimeError("IAM returned a group without identity")
            if _project_owned_name(name, project_name) or _name_or_path_is_project_owned(
                str(group.get("Path") or ""), project_name
            ):
                resources["iam_groups"].add(arn)

    return {key: sorted(values) for key, values in resources.items()}


def _backup_tags(client: Any, arn: str) -> dict[str, str]:
    return _mapping_tags(client.list_tags(ResourceArn=arn).get("Tags"))


def _list_project_backup_resources(
    session: Any,
    region: str,
    project_name: str,
) -> dict[str, list[str]]:
    client = session.client("backup", region_name=region)
    resources: dict[str, set[str]] = {
        "backup_vaults": set(),
        "backup_plans": set(),
        "backup_selections": set(),
        "backup_recovery_points": set(),
    }

    vaults: list[dict[str, Any]] = []
    for page in client.get_paginator("list_backup_vaults").paginate():
        vaults.extend(page.get("BackupVaultList", []))
    owned_vault_names: set[str] = set()
    for vault in vaults:
        name = str(vault.get("BackupVaultName") or "")
        arn = str(vault.get("BackupVaultArn") or "")
        if not name or not arn:
            raise RuntimeError(f"AWS Backup returned a vault without identity in {region}")
        tags = _backup_tags(client, arn)
        if _project_owned_name(name, project_name) or _tags_are_project_owned(tags, project_name):
            owned_vault_names.add(name)
            resources["backup_vaults"].add(arn)

    for vault in vaults:
        vault_name = str(vault["BackupVaultName"])
        for page in client.get_paginator("list_recovery_points_by_backup_vault").paginate(
            BackupVaultName=vault_name
        ):
            for recovery_point in page.get("RecoveryPoints", []):
                arn = str(recovery_point.get("RecoveryPointArn") or "")
                if not arn:
                    raise RuntimeError(
                        f"AWS Backup returned a recovery point without an ARN in {region}"
                    )
                tags = _backup_tags(client, arn)
                resource_name = str(recovery_point.get("ResourceName") or "")
                resource_arn = str(recovery_point.get("ResourceArn") or "")
                if (
                    vault_name in owned_vault_names
                    or _name_or_path_is_project_owned(resource_name, project_name)
                    or _arn_is_project_owned(resource_arn, project_name)
                    or _tags_are_project_owned(tags, project_name)
                ):
                    resources["backup_recovery_points"].add(arn)

    plans: list[dict[str, Any]] = []
    for page in client.get_paginator("list_backup_plans").paginate():
        plans.extend(page.get("BackupPlansList", []))
    for plan in plans:
        plan_id = str(plan.get("BackupPlanId") or "")
        name = str(plan.get("BackupPlanName") or "")
        arn = str(plan.get("BackupPlanArn") or "")
        if not plan_id or not name or not arn:
            raise RuntimeError(f"AWS Backup returned a plan without identity in {region}")
        tags = _backup_tags(client, arn)
        owned_plan = _project_owned_name(name, project_name) or _tags_are_project_owned(
            tags, project_name
        )
        if owned_plan:
            resources["backup_plans"].add(arn)
        for page in client.get_paginator("list_backup_selections").paginate(BackupPlanId=plan_id):
            for selection in page.get("BackupSelectionsList", []):
                selection_id = str(selection.get("SelectionId") or "")
                selection_name = str(selection.get("SelectionName") or "")
                if not selection_id:
                    raise RuntimeError(f"AWS Backup returned a selection without an ID in {region}")
                if owned_plan or _project_owned_name(selection_name, project_name):
                    resources["backup_selections"].add(f"{plan_id}:{selection_id}")

    return {key: sorted(values) for key, values in resources.items()}
