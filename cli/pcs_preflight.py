"""Preflight for the ``pcs`` block: ``gco stacks pcs preflight`` and the deploy hook.

A regional deploy with PCS on can fail an hour in, or succeed and leave an
API that answers 503, for reasons CloudFormation cannot see at synth time.
This module checks them against the live account first, with the AWS APIs
alone (no kubectl, no CDK), from the validated block the CLI reads out of
``cdk.json`` (:func:`load_pcs_config`).

Existing mode (an operator-run cluster the API fronts):

* the cluster exists, is ``ACTIVE``, runs a Slurm version whose slurmrestd
  serves the API's OpenAPI version (25.11, 26.05), has ``slurmRest``
  ``STANDARD`` and a ``SLURMRESTD`` endpoint;
* the cluster sits in the regional VPC, or ``pcs.endpoint_cidr`` covers its
  REST endpoint (the NetworkPolicy the API pod gets is cut from that CIDR);
* the cluster's security group admits tcp/6820 from the EKS cluster security
  group (or a CIDR covering the endpoint's VPC);
* the JWT signing key secret ``GetCluster`` names exists.

Managed mode (GCO creates the cluster):

* PCS answers in the Region;
* every instance type exists there, has the architecture the block derived
  from its name, supports EFA when the group asks for it with at least
  ``efa_interfaces`` network cards, and is offered in the group's zones;
* every capacity reservation exists, is usable (``active`` or, for a
  Capacity Block, ``scheduled``), and matches the group's instance type,
  zone and kind;
* the EC2 Spot service-linked role exists when a group buys Spot.

Every check is ``pass``, ``fail``, ``warn`` or ``skip``. An AWS error while
checking (a denied call, throttling) is a ``warn`` carrying the error: the
preflight never blocks a deploy over its own permissions. Only a fact that
would break the deploy or the API is a ``fail``.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from gco.pcs_config import (
    CAPACITY_RESERVATION_TYPES,
    PCS_FEATURE_KEY,
    PCS_REGIONS_KEY,
    PCS_RESTAPI_PORT,
    PCS_SUPPORTED_SLURM_VERSIONS,
    PcsConfigError,
    is_managed,
    managed_cluster_name,
    validate_pcs_config,
)

ClientFactory = Callable[..., Any]

PASS = "pass"
FAIL = "fail"
WARN = "warn"
SKIP = "skip"

#: The service-linked role EC2 Spot needs in the account before PCS can buy Spot.
SPOT_SERVICE_LINKED_ROLE = "AWSServiceRoleForEC2Spot"
#: Capacity reservation states a node group can launch from.
_USABLE_RESERVATION_STATES: dict[str, frozenset[str]] = {
    "odcr": frozenset({"active"}),
    "interruptible": frozenset({"active"}),
    "capacity_block": frozenset({"active", "scheduled"}),
}


class PcsPreflightError(RuntimeError):
    """The block could not be read or validated; nothing was checked."""


@dataclass(frozen=True)
class Check:
    """One preflight finding."""

    name: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class PreflightReport:
    """Every check for one Region's block."""

    region: str
    mode: str
    enabled: bool
    cluster: str
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [check for check in self.checks if check.status == FAIL]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "mode": self.mode,
            "enabled": self.enabled,
            "cluster": self.cluster,
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
        }


# ---------------------------------------------------------------------------
# cdk.json
# ---------------------------------------------------------------------------


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _cdk_context(cdk_json_path: Path | None) -> dict[str, Any]:
    path = cdk_json_path or Path.cwd() / "cdk.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PcsPreflightError(f"cdk.json not found at {path}") from exc
    except ValueError as exc:
        raise PcsPreflightError(f"cdk.json at {path} is not valid JSON: {exc}") from exc
    context = document.get("context") if isinstance(document, dict) else None
    return context if isinstance(context, dict) else {}


def _fsx_enabled(context: Mapping[str, Any], region: str) -> bool:
    """The Region's ``fsx_lustre`` toggle, with its ``fsx_lustre_regions`` overlay."""
    block = context.get("fsx_lustre")
    enabled = bool(block.get("enabled")) if isinstance(block, Mapping) else False
    overlays = context.get("fsx_lustre_regions")
    if isinstance(overlays, Mapping) and isinstance(overlays.get(region), Mapping):
        enabled = bool(overlays[region].get("enabled", enabled))
    return enabled


def raw_pcs_config(context: Mapping[str, Any], region: str) -> dict[str, Any]:
    """The ``pcs`` block with the Region's ``pcs_regions`` overlay, unvalidated."""
    block = context.get(PCS_FEATURE_KEY)
    raw: dict[str, Any] = dict(block) if isinstance(block, Mapping) else {}
    overlays = context.get(PCS_REGIONS_KEY)
    if isinstance(overlays, Mapping) and isinstance(overlays.get(region), Mapping):
        raw = _deep_merge(raw, overlays[region])
    return raw


def load_pcs_config(
    region: str, *, cdk_json_path: Path | None = None, force_enabled: bool = False
) -> dict[str, Any]:
    """The validated ``pcs`` block for ``region`` as the regional stack will read it.

    ``force_enabled`` mirrors the run-scoped ``--enable pcs`` override. The
    block is validated the way the config loader validates it, so a typo
    fails here with the same message a synth would give.
    """
    context = _cdk_context(cdk_json_path)
    try:
        config = validate_pcs_config(
            raw_pcs_config(context, region), fsx_enabled=_fsx_enabled(context, region)
        )
    except PcsConfigError as exc:
        raise PcsPreflightError(f"cdk.json pcs block is invalid for {region}: {exc}") from exc
    if force_enabled:
        config["enabled"] = True
    return config


def project_name(cdk_json_path: Path | None = None) -> str:
    """``context.project_name`` (``gco`` when absent)."""
    return str(_cdk_context(cdk_json_path).get("project_name") or "gco")


# ---------------------------------------------------------------------------
# Live checks
# ---------------------------------------------------------------------------


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code") or "ClientError")
        message = str(error.get("Message") or "")
        return f"{code}: {message}" if message else code
    return f"{type(exc).__name__}: {exc}"


class _Checker:
    """Collects checks; AWS failures become warnings instead of exceptions."""

    def __init__(self, region: str, client_factory: ClientFactory) -> None:
        self.region = region
        self._factory = client_factory
        self._clients: dict[str, Any] = {}
        self.checks: list[Check] = []

    def client(self, service: str) -> Any:
        if service not in self._clients:
            self._clients[service] = self._factory(service, region_name=self.region)
        return self._clients[service]

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name=name, status=status, detail=detail))

    def guarded(self, name: str, call: Callable[[], Any]) -> Any:
        """Run ``call``; an AWS error records a warning and returns ``None``."""
        try:
            result: Any = call()
        except (ClientError, BotoCoreError) as exc:
            self.add(name, WARN, f"could not check: {_error_text(exc)}")
            return None
        return result


def _eks_cluster(checker: _Checker, cluster_name: str) -> dict[str, Any] | None:
    """The regional EKS cluster's VPC and cluster security group, or ``None`` before the first deploy."""

    def describe() -> dict[str, Any] | None:
        try:
            response = checker.client("eks").describe_cluster(name=cluster_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise
        return dict(response.get("cluster") or {})

    cluster = checker.guarded("eks-cluster", describe)
    return dict(cluster) if isinstance(cluster, dict) else None


def _existing_mode_checks(
    checker: _Checker, config: Mapping[str, Any], *, eks_cluster_name: str
) -> None:
    cluster_id = str(config["cluster_id"])
    pcs = checker.client("pcs")

    def get_cluster() -> dict[str, Any] | None:
        try:
            return dict(pcs.get_cluster(clusterIdentifier=cluster_id).get("cluster") or {})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise

    cluster = checker.guarded("cluster", get_cluster)
    if cluster is None:
        if not any(check.name == "cluster" for check in checker.checks):
            checker.add(
                "cluster",
                FAIL,
                f"PCS cluster {cluster_id} does not exist in {checker.region} "
                "(aws pcs list-clusters); fix pcs.cluster_id",
            )
        return
    checker.add("cluster", PASS, f"{cluster_id} ({cluster.get('name')}) found")

    status = str(cluster.get("status") or "")
    checker.add(
        "cluster-status",
        PASS if status == "ACTIVE" else FAIL,
        f"status {status}" + ("" if status == "ACTIVE" else "; the API needs ACTIVE"),
    )

    version = str((cluster.get("scheduler") or {}).get("version") or "")
    checker.add(
        "slurm-version",
        PASS if version in PCS_SUPPORTED_SLURM_VERSIONS else FAIL,
        f"Slurm {version or '?'}"
        + (
            ""
            if version in PCS_SUPPORTED_SLURM_VERSIONS
            else f"; the API speaks slurmrestd v0.0.44, served by Slurm "
            f"{' or '.join(PCS_SUPPORTED_SLURM_VERSIONS)}"
        ),
    )

    slurm_configuration = cluster.get("slurmConfiguration") or {}
    rest_mode = str((slurm_configuration.get("slurmRest") or {}).get("mode") or "NONE")
    checker.add(
        "rest-api",
        PASS if rest_mode == "STANDARD" else FAIL,
        f"slurmRest {rest_mode}"
        + (
            ""
            if rest_mode == "STANDARD"
            else "; enable the REST API on the cluster (aws pcs update-cluster "
            "--slurm-configuration slurmRest={mode=STANDARD})"
        ),
    )

    endpoints = [
        endpoint
        for endpoint in cluster.get("endpoints") or []
        if isinstance(endpoint, Mapping) and endpoint.get("type") == "SLURMRESTD"
    ]
    endpoint_ip = str(endpoints[0].get("privateIpAddress") or "") if endpoints else ""
    if endpoints:
        checker.add(
            "rest-endpoint", PASS, f"SLURMRESTD at {endpoint_ip}:{endpoints[0].get('port')}"
        )
    else:
        checker.add(
            "rest-endpoint",
            FAIL if rest_mode == "STANDARD" else SKIP,
            "the cluster reports no SLURMRESTD endpoint"
            + ("" if rest_mode == "STANDARD" else " (the REST API is off)"),
        )

    secret_arn = str(
        ((slurm_configuration.get("jwtAuth") or {}).get("jwtKey") or {}).get("secretArn") or ""
    )
    if secret_arn:

        def describe_secret() -> Any:
            return checker.client("secretsmanager").describe_secret(SecretId=secret_arn)

        if checker.guarded("jwt-key", describe_secret) is not None:
            checker.add("jwt-key", PASS, f"signing key secret {secret_arn.rsplit(':', 1)[-1]}")
    else:
        checker.add("jwt-key", FAIL, "GetCluster reports no JWT signing key secret")

    networking = cluster.get("networking") or {}
    subnet_ids = [str(s) for s in networking.get("subnetIds") or []]
    security_group_ids = [str(s) for s in networking.get("securityGroupIds") or []]
    eks = _eks_cluster(checker, eks_cluster_name)
    vpc_config = (eks or {}).get("resourcesVpcConfig") or {}
    regional_vpc = str(vpc_config.get("vpcId") or "")
    eks_security_group = str(vpc_config.get("clusterSecurityGroupId") or "")

    def cluster_vpc() -> str:
        if not subnet_ids:
            return ""
        subnets = checker.client("ec2").describe_subnets(SubnetIds=subnet_ids).get("Subnets") or []
        return str(subnets[0].get("VpcId") or "") if subnets else ""

    pcs_vpc = checker.guarded("network", cluster_vpc)
    if pcs_vpc is not None:
        checker.checks.append(
            _network_check(
                pcs_vpc=pcs_vpc,
                regional_vpc=regional_vpc,
                eks_deployed=eks is not None,
                eks_cluster_name=eks_cluster_name,
                endpoint_cidr=config.get("endpoint_cidr"),
                endpoint_ip=endpoint_ip,
            )
        )

    if eks is None or not eks_security_group or not regional_vpc:
        checker.add(
            "security-group",
            SKIP,
            f"EKS cluster {eks_cluster_name} is not deployed yet; after the first deploy, the "
            f"cluster security group must admit tcp/{PCS_RESTAPI_PORT} from the EKS cluster "
            "security group",
        )
        return

    def security_groups() -> tuple[list[dict[str, Any]], list[str]]:
        ec2 = checker.client("ec2")
        groups: list[dict[str, Any]] = []
        if security_group_ids:
            response = ec2.describe_security_groups(GroupIds=security_group_ids)
            groups = [dict(group) for group in response.get("SecurityGroups") or []]
        # A rule may admit the API pods by CIDR instead of by group: the
        # regional VPC's ranges count (EKS does not report them itself).
        cidrs: list[str] = []
        for vpc in ec2.describe_vpcs(VpcIds=[regional_vpc]).get("Vpcs") or []:
            cidrs.extend(
                str(assoc.get("CidrBlock"))
                for assoc in vpc.get("CidrBlockAssociationSet") or []
                if assoc.get("CidrBlock")
            )
        return groups, cidrs

    described = checker.guarded("security-group", security_groups)
    if described is None:
        return
    groups, vpc_cidrs = described
    admitted = any(
        _rule_admits(rule, source_group=eks_security_group, vpc_cidrs=vpc_cidrs)
        for group in groups
        for rule in group.get("IpPermissions") or []
    )
    checker.add(
        "security-group",
        PASS if admitted else FAIL,
        f"{', '.join(security_group_ids) or 'no security group'} "
        + (
            f"admits tcp/{PCS_RESTAPI_PORT} from {eks_security_group}"
            if admitted
            else f"does not admit tcp/{PCS_RESTAPI_PORT} from the EKS cluster security group "
            f"{eks_security_group}; add that ingress rule to the PCS cluster's security group"
        ),
    )


def _network_check(
    *,
    pcs_vpc: str,
    regional_vpc: str,
    eks_deployed: bool,
    eks_cluster_name: str,
    endpoint_cidr: object,
    endpoint_ip: str,
) -> Check:
    """Can the API pod reach the REST endpoint: same VPC, or a peered range it names."""
    if endpoint_cidr:
        network = ipaddress.ip_network(str(endpoint_cidr), strict=False)
        covered = bool(endpoint_ip) and ipaddress.ip_address(endpoint_ip) in network
        peering = (
            f" (cluster VPC {pcs_vpc}, regional VPC {regional_vpc}; the VPCs must be peered)"
            if regional_vpc and pcs_vpc != regional_vpc
            else ""
        )
        if covered:
            return Check(
                "network",
                PASS,
                f"pcs.endpoint_cidr {endpoint_cidr} covers the REST endpoint {endpoint_ip}"
                + peering,
            )
        return Check(
            "network",
            FAIL,
            f"pcs.endpoint_cidr {endpoint_cidr} does not cover the REST endpoint "
            f"{endpoint_ip or '(unknown)'}; the API pod's NetworkPolicy would block it" + peering,
        )
    if not eks_deployed:
        return Check(
            "network",
            SKIP,
            f"EKS cluster {eks_cluster_name} is not deployed yet; the cluster must sit in the "
            f"regional VPC (it is in {pcs_vpc or 'an unknown VPC'}) or pcs.endpoint_cidr must "
            "name a peered range",
        )
    if pcs_vpc and pcs_vpc == regional_vpc:
        return Check("network", PASS, f"cluster VPC {pcs_vpc} is the regional VPC")
    return Check(
        "network",
        FAIL,
        f"cluster VPC {pcs_vpc or '(unknown)'} is not the regional VPC {regional_vpc}; move the "
        "cluster, or set pcs.endpoint_cidr to the peered range the REST endpoint lives in",
    )


def _rule_admits(rule: Mapping[str, Any], *, source_group: str, vpc_cidrs: Sequence[str]) -> bool:
    """Whether one ingress rule lets ``source_group`` (or its whole VPC) reach tcp/6820."""
    protocol = str(rule.get("IpProtocol") or "")
    if protocol not in ("-1", "tcp"):
        return False
    if protocol == "tcp":
        from_port = int(rule.get("FromPort", 0) or 0)
        to_port = int(rule.get("ToPort", 0) or 0)
        if not from_port <= PCS_RESTAPI_PORT <= to_port:
            return False
    for pair in rule.get("UserIdGroupPairs") or []:
        if str(pair.get("GroupId") or "") == source_group:
            return True
    for ip_range in rule.get("IpRanges") or []:
        if _covers_any(str(ip_range.get("CidrIp") or ""), vpc_cidrs):
            return True
    return False


def _covers_any(cidr: str, vpc_cidrs: Sequence[str]) -> bool:
    """Whether the IPv4 ``cidr`` contains one of the VPC's IPv4 ranges."""
    try:
        network = ipaddress.IPv4Network(cidr, strict=False)
    except ValueError:
        return False
    for vpc_cidr in vpc_cidrs:
        try:
            vpc_network = ipaddress.IPv4Network(vpc_cidr, strict=False)
        except ValueError:
            continue
        if vpc_network.subnet_of(network):
            return True
    return False


def _managed_mode_checks(checker: _Checker, config: Mapping[str, Any], *, project: str) -> None:
    managed = config["managed"]
    cluster_name = managed_cluster_name(config, project)

    def list_clusters() -> list[dict[str, Any]]:
        clusters: list[dict[str, Any]] = []
        paginator = checker.client("pcs").get_paginator("list_clusters")
        for page in paginator.paginate():
            clusters.extend(dict(c) for c in page.get("clusters") or [])
        return clusters

    clusters = checker.guarded("pcs-service", list_clusters)
    if clusters is not None:
        checker.add("pcs-service", PASS, f"AWS PCS answers in {checker.region}")
        same_name = [c for c in clusters if str(c.get("name")) == cluster_name]
        if same_name:
            checker.add(
                "cluster-name",
                WARN,
                f"a PCS cluster named {cluster_name} already exists ({same_name[0].get('id')}); "
                "a first deploy would fail on the name, a redeploy of GCO's own stack is fine",
            )
        else:
            checker.add("cluster-name", PASS, f"{cluster_name} is free")

    groups = list(managed["node_groups"])
    login = managed["login"]
    wanted: dict[str, dict[str, Any]] = {}
    for group in groups:
        for instance_type in group["instance_types"]:
            wanted.setdefault(instance_type, {"groups": [], "architecture": group["_architecture"]})
            wanted[instance_type]["groups"].append(group["name"])
    if login["enabled"]:
        wanted.setdefault(
            login["instance_type"], {"groups": [], "architecture": login["_architecture"]}
        )
        wanted[login["instance_type"]]["groups"].append("login")

    def describe_types() -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        names = sorted(wanted)
        ec2 = checker.client("ec2")
        for start in range(0, len(names), 100):
            batch = names[start : start + 100]
            try:
                response = ec2.describe_instance_types(InstanceTypes=batch)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "InvalidInstanceType":
                    raise
                # One unknown type fails the whole batch: describe one by one.
                response = {"InstanceTypes": []}
                for name in batch:
                    try:
                        single = ec2.describe_instance_types(InstanceTypes=[name])
                    except ClientError as single_exc:
                        if (
                            single_exc.response.get("Error", {}).get("Code")
                            != "InvalidInstanceType"
                        ):
                            raise
                        continue
                    response["InstanceTypes"].extend(single.get("InstanceTypes") or [])
            for item in response.get("InstanceTypes") or []:
                found[str(item.get("InstanceType"))] = dict(item)
        return found

    described = checker.guarded("instance-types", describe_types)
    if described is not None:
        for instance_type, info in sorted(wanted.items()):
            label = f"{instance_type} ({', '.join(info['groups'])})"
            item = described.get(instance_type)
            if item is None:
                checker.add(
                    "instance-types",
                    FAIL,
                    f"{label} is not available in {checker.region}",
                )
                continue
            architectures = (item.get("ProcessorInfo") or {}).get("SupportedArchitectures") or []
            if info["architecture"] not in architectures:
                checker.add(
                    "instance-types",
                    FAIL,
                    f"{label} is {'/'.join(architectures) or 'of unknown architecture'} but its "
                    f"name reads as {info['architecture']}; a node group launches one AMI, so "
                    "group it with types of its own architecture",
                )
                continue
            checker.add("instance-types", PASS, f"{label} available, {info['architecture']}")

        for group in groups:
            if not group["efa"]:
                continue
            for instance_type in group["instance_types"]:
                item = described.get(instance_type)
                if item is None:
                    continue
                network = item.get("NetworkInfo") or {}
                cards = int(network.get("MaximumNetworkCards") or 1)
                if not network.get("EfaSupported"):
                    checker.add(
                        "efa",
                        FAIL,
                        f"{group['name']}: {instance_type} does not support EFA; drop efa or "
                        "pick an EFA-capable type",
                    )
                elif int(group["efa_interfaces"]) > cards:
                    checker.add(
                        "efa",
                        FAIL,
                        f"{group['name']}: {instance_type} has {cards} network card(s) but "
                        f"efa_interfaces is {group['efa_interfaces']}",
                    )
                else:
                    checker.add(
                        "efa",
                        PASS,
                        f"{group['name']}: {instance_type} supports EFA on "
                        f"{group['efa_interfaces']} of {cards} network card(s)",
                    )

    zoned = [(g, z) for g in groups for z in g["availability_zones"]]
    if login["enabled"]:
        zoned.extend(
            (login | {"name": "login", "instance_types": [login["instance_type"]]}, z)
            for z in login["availability_zones"]
        )
    if zoned:

        def offerings() -> set[tuple[str, str]]:
            pairs: set[tuple[str, str]] = set()
            types = sorted({t for g, _z in zoned for t in g["instance_types"]})
            paginator = checker.client("ec2").get_paginator("describe_instance_type_offerings")
            for page in paginator.paginate(
                LocationType="availability-zone",
                Filters=[{"Name": "instance-type", "Values": types}],
            ):
                for offering in page.get("InstanceTypeOfferings") or []:
                    pairs.add((str(offering.get("InstanceType")), str(offering.get("Location"))))
            return pairs

        offered = checker.guarded("zones", offerings)
        if offered is not None:
            for group, zone in zoned:
                for instance_type in group["instance_types"]:
                    ok = (instance_type, zone) in offered
                    checker.add(
                        "zones",
                        PASS if ok else FAIL,
                        f"{group['name']}: {instance_type} "
                        + ("is offered" if ok else "is not offered")
                        + f" in {zone}",
                    )

    reservations = [(g, g["capacity_reservation"]) for g in groups if g["capacity_reservation"]]
    if reservations:

        def describe_reservations() -> dict[str, dict[str, Any]]:
            ids = sorted({r["id"] for _g, r in reservations})
            response = checker.client("ec2").describe_capacity_reservations(
                CapacityReservationIds=ids
            )
            return {
                str(item.get("CapacityReservationId")): dict(item)
                for item in response.get("CapacityReservations") or []
            }

        described_reservations = checker.guarded("capacity-reservations", describe_reservations)
        if described_reservations is not None:
            for group, reservation in reservations:
                _reservation_check(checker, group, reservation, described_reservations)

    if any(group["purchase_option"] == "SPOT" for group in groups):

        def spot_role() -> bool:
            try:
                checker.client("iam").get_role(RoleName=SPOT_SERVICE_LINKED_ROLE)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
                    return False
                raise
            return True

        present = checker.guarded("spot-role", spot_role)
        if present is not None:
            checker.add(
                "spot-role",
                PASS if present else FAIL,
                f"{SPOT_SERVICE_LINKED_ROLE} "
                + (
                    "exists"
                    if present
                    else "is missing; create it once per account: aws iam "
                    "create-service-linked-role --aws-service-name spot.amazonaws.com"
                ),
            )


def _reservation_check(
    checker: _Checker,
    group: Mapping[str, Any],
    reservation: Mapping[str, str],
    described: Mapping[str, Mapping[str, Any]],
) -> None:
    name = group["name"]
    kind = reservation["type"]
    item = described.get(reservation["id"])
    if item is None:
        checker.add(
            "capacity-reservations",
            FAIL,
            f"{name}: capacity reservation {reservation['id']} does not exist in {checker.region}",
        )
        return
    problems: list[str] = []
    state = str(item.get("State") or "")
    if state not in _USABLE_RESERVATION_STATES[kind]:
        problems.append(f"state is {state}")
    instance_type = str(item.get("InstanceType") or "")
    if instance_type not in group["instance_types"]:
        problems.append(
            f"it reserves {instance_type}, the group lists {', '.join(group['instance_types'])}"
        )
    zone = str(item.get("AvailabilityZone") or "")
    if zone not in group["availability_zones"]:
        problems.append(
            f"it is in {zone}, the group names {', '.join(group['availability_zones'])}"
        )
    is_block = str(item.get("ReservationType") or "default") == "capacity-block"
    if is_block != (kind == "capacity_block"):
        problems.append(
            f"it is a {'Capacity Block' if is_block else 'standard reservation'} but "
            f"capacity_reservation.type is {kind}"
        )
    if kind == "interruptible" and not item.get("Interruptible"):
        problems.append("it is not an interruptible reservation")
    if kind == "odcr" and str(item.get("InstanceMatchCriteria") or "") == "open":
        problems.append(
            "it is an open reservation: PCS consumes it without targeting, drop "
            "capacity_reservation or make it targeted"
        )
    if problems:
        checker.add(
            "capacity-reservations",
            FAIL,
            f"{name}: {reservation['id']} ({CAPACITY_RESERVATION_TYPES[kind]}): "
            + "; ".join(problems),
        )
    else:
        checker.add(
            "capacity-reservations",
            PASS,
            f"{name}: {reservation['id']} {state}, {instance_type} in {zone}",
        )


def run_preflight(
    region: str,
    config: Mapping[str, Any],
    *,
    project: str,
    client_factory: ClientFactory = boto3.client,
) -> PreflightReport:
    """Check the validated ``pcs`` block for ``region`` against the live account."""
    checker = _Checker(region, client_factory)
    managed = is_managed(config)
    if managed:
        cluster = managed_cluster_name(config, project)
        _managed_mode_checks(checker, config, project=project)
    else:
        cluster = str(config["cluster_id"])
        _existing_mode_checks(checker, config, eks_cluster_name=f"{project}-{region}")
    return PreflightReport(
        region=region,
        mode="managed" if managed else "existing",
        enabled=bool(config.get("enabled")),
        cluster=cluster,
        checks=checker.checks,
    )


_MARKS = {PASS: "✓", FAIL: "✗", WARN: "!", SKIP: "-"}


def render_report(report: PreflightReport) -> str:
    """The report as the terminal shows it."""
    lines = [
        f"\n  AWS PCS preflight — {report.region} ({report.mode} mode, "
        f"{'enabled' if report.enabled else 'disabled in cdk.json'}): {report.cluster}",
        "  " + "-" * 70,
    ]
    for check in report.checks:
        lines.append(f"  {_MARKS[check.status]} {check.name:<22} {check.detail}")
    if not report.checks:
        lines.append("  (nothing to check)")
    failures = report.failures
    lines.append("")
    lines.append(f"  {len(failures)} failure(s)" if failures else "  ready: no failures")
    return "\n".join(lines)


def summarize(reports: Iterable[PreflightReport]) -> dict[str, Any]:
    """The machine-readable document ``--output json`` prints."""
    items = [report.to_dict() for report in reports]
    return {"ok": all(item["ok"] for item in items), "regions": items}
