"""CloudFormation custom resources that look up AWS PCS facts the SDK cannot shape.

One ``cr.Provider`` (``gco/stacks/pcs_cluster.py`` ``PcsLookup``) serves two
resource types, told apart by the event's ``ResourceType``:

* ``Custom::PcsAmiLookup`` resolves the AMI a compute node group launches
  (below).
* ``Custom::PcsControllerLookup`` resolves the cluster controller's address
  for the Prometheus scrape of its Slurm metrics: ``GetCluster`` lists the
  controller's endpoints (``SLURMCTLD``, ``SLURMDBD``, ``SLURMRESTD``) in no
  guaranteed order, and ``cr.AwsCustomResource`` can only address a list by
  index, so this handler picks the ``SLURMCTLD`` entry by type. The response
  carries ``Data.PrivateIpAddress``, ``Data.Port`` and ``Data.Address``
  (``ip:port``). Properties: ``ClusterId``, ``Region``.

A PCS compute node group launches one AMI. GCO lets the ``pcs`` block name it
three ways (``gco/pcs_config.py`` ``ami``):

* ``sample`` — the AWS PCS sample AMI (Amazon Linux 2023), which AWS publishes
  as a public image named ``aws-pcs-sample_ami-al2023-<arch>*`` whose
  description lists the Slurm versions it supports. There is no SSM public
  parameter for it; the documented way to find it is ``describe-images``
  filtered by name, description and state, newest ``CreationDate`` first.
* ``dlami`` — the PCS-ready Deep Learning Base GPU AMI (Ubuntu 24.04), which
  AWS publishes behind the SSM public parameter
  ``/aws/service/pcs/ami/dlami-base-ubuntu2404/<arch>/latest/ami-id`` and as
  public images named ``aws-pcs-ready-dlami-base-ubuntu2404-<arch>-*``.
* an ``ami-`` id, passed through by the stack without calling this function.

``cr.AwsCustomResource`` cannot sort a ``describe-images`` result, so this
``cr.Provider`` on-event handler does the lookup. It never creates anything:
Create and Update resolve the id (the DLAMI through the SSM parameter first,
kept only when its description lists the cluster's Slurm version; the name
search filtered by that version otherwise, for both kinds) and Delete is a
no-op. Both AWS-published kinds bundle several Slurm releases and name them
in the AMI description, so the version filter is what keeps a 26.05 cluster
from launching nodes whose newest Slurm is 25.11. The
physical id is stable per node group so an unchanged lookup never replaces
the resource; a new AMI release surfaces on the next deploy, when the
resource re-runs.

AMI event shape (``ResourceProperties``): ``Kind`` (``sample`` | ``dlami``),
``Architecture`` (``x86_64`` | ``arm64``), ``SlurmVersion`` (major.minor, used
as the sample AMI's description filter), ``Region``, ``NodeGroup`` (for the
physical id and the log line). The response carries ``Data.ImageId``,
``Data.Name`` and ``Data.Description``.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SAMPLE_NAME_PATTERN = "aws-pcs-sample_ami-al2023-{architecture}*"
DLAMI_NAME_PATTERN = "aws-pcs-ready-dlami-base-ubuntu2404-{architecture}-*"
DLAMI_SSM_PARAMETER = "/aws/service/pcs/ami/dlami-base-ubuntu2404/{architecture}/latest/ami-id"
KINDS = ("sample", "dlami")
ARCHITECTURES = ("x86_64", "arm64")

#: The two custom resource types this provider serves (``event["ResourceType"]``).
AMI_RESOURCE_TYPE = "Custom::PcsAmiLookup"
CONTROLLER_RESOURCE_TYPE = "Custom::PcsControllerLookup"
RESOURCE_TYPES = (AMI_RESOURCE_TYPE, CONTROLLER_RESOURCE_TYPE)
#: ``GetCluster`` endpoint type of slurmctld, whose port also serves the metrics.
CONTROLLER_ENDPOINT_TYPE = "SLURMCTLD"

_CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 5, "mode": "standard"},
)


def _ec2_client(region: str) -> Any:
    return boto3.client("ec2", region_name=region, config=_CLIENT_CONFIG)


def _ssm_client(region: str) -> Any:
    return boto3.client("ssm", region_name=region, config=_CLIENT_CONFIG)


def _pcs_client(region: str) -> Any:
    return boto3.client("pcs", region_name=region, config=_CLIENT_CONFIG)


def _resource_type(event: dict[str, Any]) -> str:
    """The event's ``ResourceType``; absent (older events) reads as the AMI lookup."""
    resource_type = str(event.get("ResourceType") or AMI_RESOURCE_TYPE).strip()
    if resource_type not in RESOURCE_TYPES:
        raise ValueError(
            f"ResourceType must be one of {', '.join(RESOURCE_TYPES)}, got {resource_type!r}"
        )
    return resource_type


def _controller_properties(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("ResourceProperties") or {}
    cluster_id = str(raw.get("ClusterId") or "").strip()
    region = str(raw.get("Region") or "").strip()
    if not cluster_id:
        raise ValueError("ClusterId is required")
    if not region:
        raise ValueError("Region is required")
    return {"ClusterId": cluster_id, "Region": region}


def controller_endpoint(cluster: dict[str, Any]) -> dict[str, str]:
    """The ``SLURMCTLD`` endpoint of a ``GetCluster`` document as ``ip``, ``port``, ``ip:port``."""
    for endpoint in cluster.get("endpoints") or []:
        if str(endpoint.get("type") or "").upper() != CONTROLLER_ENDPOINT_TYPE:
            continue
        address = str(endpoint.get("privateIpAddress") or "").strip()
        port = str(endpoint.get("port") or "").strip()
        if address and port:
            return {"PrivateIpAddress": address, "Port": port, "Address": f"{address}:{port}"}
    raise LookupError(
        f"PCS cluster {cluster.get('id') or '?'} reports no {CONTROLLER_ENDPOINT_TYPE} endpoint "
        f"with a private IP address (status {cluster.get('status') or '?'})"
    )


def resolve_controller(properties: dict[str, str]) -> dict[str, str]:
    """``GetCluster`` and pick the controller endpoint by type."""
    response = _pcs_client(properties["Region"]).get_cluster(
        clusterIdentifier=properties["ClusterId"]
    )
    return controller_endpoint(dict(response.get("cluster") or {}))


def _properties(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("ResourceProperties") or {}
    kind = str(raw.get("Kind") or "").strip().lower()
    architecture = str(raw.get("Architecture") or "").strip()
    region = str(raw.get("Region") or "").strip()
    if kind not in KINDS:
        raise ValueError(f"Kind must be one of {', '.join(KINDS)}, got {kind!r}")
    if architecture not in ARCHITECTURES:
        raise ValueError(
            f"Architecture must be one of {', '.join(ARCHITECTURES)}, got {architecture!r}"
        )
    if not region:
        raise ValueError("Region is required")
    return {
        "Kind": kind,
        "Architecture": architecture,
        "Region": region,
        "SlurmVersion": str(raw.get("SlurmVersion") or "").strip(),
        "NodeGroup": str(raw.get("NodeGroup") or "node-group").strip(),
    }


def newest_image(images: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The image with the latest ``CreationDate`` (ISO-8601 strings sort lexically)."""
    available = [image for image in images if image.get("ImageId")]
    if not available:
        return None
    return max(available, key=lambda image: str(image.get("CreationDate") or ""))


def find_by_name(ec2: Any, *, name_pattern: str, description_pattern: str | None) -> dict[str, Any]:
    """``describe-images`` owned by Amazon, newest first, as the PCS user guide shows."""
    filters = [
        {"Name": "name", "Values": [name_pattern]},
        {"Name": "state", "Values": ["available"]},
    ]
    if description_pattern:
        filters.append({"Name": "description", "Values": [description_pattern]})
    images: list[dict[str, Any]] = []
    paginator = ec2.get_paginator("describe_images")
    for page in paginator.paginate(Owners=["amazon"], Filters=filters):
        images.extend(page.get("Images", []))
    newest = newest_image(images)
    if newest is None:
        wanted = name_pattern + (
            f" with description {description_pattern}" if description_pattern else ""
        )
        raise LookupError(f"no available Amazon-owned AMI matches {wanted} in this Region")
    return newest


def find_dlami_by_parameter(ssm: Any, architecture: str) -> str | None:
    """The DLAMI id from its SSM public parameter, or ``None`` when the parameter is absent."""
    name = DLAMI_SSM_PARAMETER.format(architecture=architecture)
    try:
        response = ssm.get_parameter(Name=name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ParameterNotFound", "AccessDeniedException"):
            logger.info(
                "SSM parameter %s unavailable (%s); falling back to a name search", name, code
            )
            return None
        raise
    value = str(response.get("Parameter", {}).get("Value") or "").strip()
    return value or None


def supports_version(image: dict[str, Any], version: str) -> bool:
    """Whether the AMI's description names the Slurm ``version`` (empty version: always).

    Both AWS-published kinds bundle several Slurm releases and list them in
    the description (``Slurm: 25.05, 25.11, 26.05`` on the sample AMI,
    ``Slurm: 24.11.7-1, 25.05.7-1, 25.11.2-1`` on the DLAMI); a compute node
    whose AMI lacks the controller's version never registers.
    """
    return not version or version in str(image.get("Description") or "")


def resolve(properties: dict[str, str]) -> dict[str, str]:
    """Resolve the AMI the properties describe; raises when nothing matches."""
    kind = properties["Kind"]
    architecture = properties["Architecture"]
    region = properties["Region"]
    version = properties["SlurmVersion"]
    description_pattern = f"*{version}*" if version else None
    ec2 = _ec2_client(region)
    if kind == "dlami":
        image: dict[str, Any] | None = None
        image_id = find_dlami_by_parameter(_ssm_client(region), architecture)
        if image_id:
            described = ec2.describe_images(ImageIds=[image_id]).get("Images", [])
            candidate = described[0] if described else {"ImageId": image_id}
            if supports_version(candidate, version):
                image = candidate
            else:
                logger.info(
                    "DLAMI %s from SSM does not list Slurm %s (%s); searching by name",
                    image_id,
                    version,
                    candidate.get("Description"),
                )
        if image is None:
            image = find_by_name(
                ec2,
                name_pattern=DLAMI_NAME_PATTERN.format(architecture=architecture),
                description_pattern=description_pattern,
            )
    else:
        image = find_by_name(
            ec2,
            name_pattern=SAMPLE_NAME_PATTERN.format(architecture=architecture),
            description_pattern=description_pattern,
        )
    return {
        "ImageId": str(image["ImageId"]),
        "Name": str(image.get("Name") or ""),
        "Description": str(image.get("Description") or ""),
    }


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """``cr.Provider`` on-event handler: resolve on Create/Update, no-op on Delete."""
    request_type = str(event.get("RequestType") or "")
    if request_type == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId") or "gco-pcs-lookup"}
    if request_type not in ("Create", "Update"):
        raise ValueError(f"Unsupported RequestType: {request_type!r}")
    if _resource_type(event) == CONTROLLER_RESOURCE_TYPE:
        controller = _controller_properties(event)
        data = resolve_controller(controller)
        logger.info(
            "PCS cluster %s controller (slurmctld) is at %s",
            controller["ClusterId"],
            data["Address"],
        )
        physical_id = (
            event.get("PhysicalResourceId") or f"gco-pcs-controller-{controller['ClusterId']}"
        )
        return {"PhysicalResourceId": physical_id, "Data": data}
    properties = _properties(event)
    data = resolve(properties)
    logger.info(
        "PCS node group %s (%s, %s, Slurm %s) uses AMI %s (%s)",
        properties["NodeGroup"],
        properties["Kind"],
        properties["Architecture"],
        properties["SlurmVersion"] or "any",
        data["ImageId"],
        data["Name"],
    )
    physical_id = event.get("PhysicalResourceId") or (
        f"gco-pcs-ami-{properties['NodeGroup']}-{properties['Kind']}-{properties['Architecture']}"
    )
    return {"PhysicalResourceId": physical_id, "Data": data}
