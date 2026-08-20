"""Live PVC/PV/EBS observation helpers for the pre-destroy volume inventory.

Everything the ``volume-inventory`` action needs to turn live cluster and EC2
state into durable evidence: tunnelled kubectl access to one exact cluster,
PVC/PV record building, and normalized EBS facts for the volumes those PVCs
actually produced.

Two rules shape this module:

* **Identity comes from live objects, never from a name or a size.** A PVC
  participates only because its bound PV declares the EBS CSI driver and a
  ``vol-`` handle, and an EBS volume enters the inventory only because
  ``cli.volume_cleanup.normalize_volume_snapshot`` accepts it inside the exact
  regional target scope. Expected observability sizes are assertions on what
  was observed, never a way to decide which volume is which.
* **One PVC's problem is not the inventory's problem.** Unbound, non-EBS, and
  missing-handle PVCs are recorded with an explicit non-participation reason and
  the remaining PVCs continue to be recorded.

Normalization is deliberately *not* reimplemented here: the same
``VolumeSnapshot`` the CLI's cleanup service decides against is the one this
harness records, so validation cannot pass against a different notion of an
EBS volume than production uses.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from cli.volume_cleanup import (
    RegionalVolumeTarget,
    VolumeNormalizationError,
    VolumeSnapshot,
    normalize_volume_snapshot,
)

#: How this module invokes kubectl: kubectl args in, ``(code, stdout, stderr)``
#: out. Injected so every test drives real record-building logic against
#: canned API objects instead of a cluster.
KubectlRunner = Callable[..., tuple[int, str, str]]

#: The only CSI driver whose PVs are backed by an EBS volume this run may
#: record. Any other driver is an explicit non-participation reason.
EBS_CSI_DRIVER = "ebs.csi.aws.com"

#: Label the kube-prometheus-stack components carry on their PVCs. Component
#: identity is read from this live label, never from the PVC name or its size.
COMPONENT_LABEL = "app.kubernetes.io/name"

#: Observability components whose PVC-backed EBS volumes must participate in the
#: selected volume policy, with the cdk.json block their configured size lives
#: in and the documented default that block ships with.
OBSERVABILITY_COMPONENTS: tuple[tuple[str, str, int], ...] = (
    ("prometheus", "prometheus", 50),
    ("alertmanager", "alertmanager", 5),
)

_VOLUME_ID_PATTERN = re.compile(r"^vol-[A-Za-z0-9]+$")
_QUANTITY_PATTERN = re.compile(r"^(\d+)([EPTGMK]i?)?$")
_QUANTITY_MULTIPLIERS: dict[str, int] = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "Pi": 1024**5,
    "Ei": 1024**6,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}
_GIB = 1024**3


class _QuietTunnelFormatter:
    """Adapter for the CLI tunnel helpers' formatter callbacks."""

    @staticmethod
    def print_info(message: str) -> None:
        """Echo one tunnel lifecycle message with a harness prefix."""
        print(f"[volume-inventory] {message}")

    @staticmethod
    def print_success(message: str) -> None:
        """Echo one tunnel success message."""
        print(f"[volume-inventory] {message}")

    @staticmethod
    def print_warning(message: str) -> None:
        """Echo one tunnel warning."""
        print(f"[volume-inventory] {message}")

    @staticmethod
    def print_error(message: str) -> None:
        """Echo one tunnel error."""
        print(f"[volume-inventory] {message}")


def _point_kubeconfig_at_tunnel(
    cluster_name: str,
    region: str,
    *,
    server: str,
    tls_server_name: str,
) -> None:
    """Refresh kubeconfig for one cluster and aim it at the local tunnel.

    ``aws eks update-kubeconfig`` writes the private endpoint URL, which is
    unreachable from outside the VPC; rewriting ``cluster.server`` to the
    tunnel and pinning ``tls-server-name`` to the real endpoint host makes
    plain ``kubectl`` calls work without per-invocation flags.
    """
    import yaml

    subprocess.run(
        ["aws", "eks", "update-kubeconfig", "--name", cluster_name, "--region", region],
        check=True,
        capture_output=True,
        text=True,
    )
    path = Path.home() / ".kube" / "config"
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    expected_suffix = f"cluster/{cluster_name}"
    for entry in config.get("clusters", []) or []:
        name = str(entry.get("name", ""))
        if name == cluster_name or name.endswith(expected_suffix):
            entry["cluster"]["server"] = server
            entry["cluster"]["tls-server-name"] = tls_server_name
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


@contextmanager
def cluster_kubectl(cluster_name: str, region: str) -> Iterator[KubectlRunner]:
    """Yield a kubectl runner for one exact cluster over the CLI's own tunnel.

    GCO clusters default to a private EKS API endpoint, so read-only cluster
    access reuses ``cli.cluster_tunnel`` (ephemeral SSM bastion included) rather
    than assuming the harness host sits inside the VPC.
    """
    from cli import cluster_tunnel

    formatter = _QuietTunnelFormatter()
    with cluster_tunnel.open_api_server_tunnel(
        formatter,
        cluster=cluster_name,
        region=region,
        via_ssm=cluster_tunnel.AUTO_BASTION,
        assume_yes=True,
    ) as session:
        if session.active and session.server and session.tls_server_name:
            _point_kubeconfig_at_tunnel(
                cluster_name,
                region,
                server=session.server,
                tls_server_name=session.tls_server_name,
            )
        else:
            subprocess.run(
                ["aws", "eks", "update-kubeconfig", "--name", cluster_name, "--region", region],
                check=True,
                capture_output=True,
                text=True,
            )

        def kubectl(*args: str, timeout: int = 120) -> tuple[int, str, str]:
            result = subprocess.run(
                ["kubectl", *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode, result.stdout, result.stderr

        yield kubectl


def _kubectl_items(
    kubectl: KubectlRunner,
    resource: str,
    *,
    namespaced: bool,
) -> list[dict[str, Any]]:
    """Return one cluster-wide resource list as JSON items, failing closed."""
    scope = ("--all-namespaces",) if namespaced else ()
    code, stdout, stderr = kubectl("get", resource, *scope, "-o", "json")
    if code != 0:
        raise RuntimeError(
            f"kubectl get {resource} failed with exit code {code}: {stderr.strip()[:500]}"
        )
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"kubectl get {resource} did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"kubectl get {resource} did not return a JSON object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"kubectl get {resource} returned no item list")
    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError(f"kubectl get {resource} returned a malformed item")
        records.append(item)
    return records


def read_volume_objects(
    kubectl: KubectlRunner,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return one cluster's live PVCs and PVs, in that order."""
    return (
        _kubectl_items(kubectl, "persistentvolumeclaims", namespaced=True),
        _kubectl_items(kubectl, "persistentvolumes", namespaced=False),
    )


def quantity_to_gib(value: object) -> int | None:
    """Convert a Kubernetes storage quantity to whole GiB, or ``None``.

    ``None`` means the quantity is absent or not a form this harness will
    silently reinterpret; the caller records that as observed state instead of
    guessing a size.
    """
    if not isinstance(value, str) or not value:
        return None
    match = _QUANTITY_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    amount = int(match.group(1))
    suffix = match.group(2)
    multiplier = 1 if suffix is None else _QUANTITY_MULTIPLIERS.get(suffix)
    if multiplier is None:
        return None
    total = amount * multiplier
    if total % _GIB:
        return None
    return total // _GIB


def _mapping(value: object) -> Mapping[str, Any]:
    """Return one nested API object, or an empty mapping when it is absent."""
    return value if isinstance(value, Mapping) else {}


def _pv_identity(pv: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the bound PV's identity and CSI facts from a live object."""
    metadata = _mapping(pv.get("metadata"))
    csi = _mapping(_mapping(pv.get("spec")).get("csi"))
    driver = csi.get("driver")
    handle = csi.get("volumeHandle")
    return {
        "name": str(metadata.get("name") or "") or None,
        "uid": str(metadata.get("uid") or "") or None,
        "csi_driver": str(driver) if isinstance(driver, str) and driver else None,
        "volume_handle": str(handle) if isinstance(handle, str) and handle else None,
    }


def _non_participation(pvc_record: dict[str, Any], reason_code: str, reason: str) -> None:
    pvc_record["participating"] = False
    pvc_record["reason_code"] = reason_code
    pvc_record["reason"] = reason


def pvc_records(
    pvcs: Sequence[Mapping[str, Any]],
    pvs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build one durable record per live PVC, in namespace/name order.

    Each record carries the PVC's namespace, name, UID, and requested size, the
    bound PV's identity/CSI driver/handle when one exists, the component label
    the PVC itself declares, and either an EBS volume ID to observe or an
    explicit machine-readable non-participation reason.
    """
    pvs_by_name: dict[str, Mapping[str, Any]] = {}
    for pv in pvs:
        name = str(_mapping(pv.get("metadata")).get("name") or "")
        if name:
            pvs_by_name[name] = pv

    records: list[dict[str, Any]] = []
    for pvc in pvcs:
        metadata = _mapping(pvc.get("metadata"))
        spec = _mapping(pvc.get("spec"))
        status = _mapping(pvc.get("status"))
        labels = _mapping(metadata.get("labels"))
        requests = _mapping(_mapping(spec.get("resources")).get("requests"))
        requested = requests.get("storage")
        component = labels.get(COMPONENT_LABEL)
        record: dict[str, Any] = {
            "namespace": str(metadata.get("namespace") or ""),
            "name": str(metadata.get("name") or ""),
            "uid": str(metadata.get("uid") or "") or None,
            "requested_size": str(requested) if isinstance(requested, str) else None,
            "requested_size_gib": quantity_to_gib(requested),
            "phase": str(status.get("phase") or "") or None,
            "storage_class": str(spec.get("storageClassName") or "") or None,
            "component": str(component) if isinstance(component, str) and component else None,
            "volume_name": str(spec.get("volumeName") or "") or None,
            "persistent_volume": None,
            "volume_id": None,
            "participating": False,
            "reason_code": None,
            "reason": None,
        }
        records.append(record)

        if record["phase"] != "Bound" or not record["volume_name"]:
            _non_participation(
                record,
                "pvc-not-bound",
                f"PVC phase is {record['phase']!r} with volumeName "
                f"{record['volume_name']!r}; it produced no EBS volume",
            )
            continue
        bound_pv = pvs_by_name.get(str(record["volume_name"]))
        if bound_pv is None:
            _non_participation(
                record,
                "persistent-volume-absent",
                f"Bound PersistentVolume {record['volume_name']!r} is not present in the cluster",
            )
            continue
        identity = _pv_identity(bound_pv)
        record["persistent_volume"] = identity
        if identity["csi_driver"] != EBS_CSI_DRIVER:
            _non_participation(
                record,
                "persistent-volume-not-ebs-csi",
                f"PersistentVolume CSI driver is {identity['csi_driver']!r}, not {EBS_CSI_DRIVER}",
            )
            continue
        handle = identity["volume_handle"]
        if not handle:
            _non_participation(
                record,
                "persistent-volume-missing-volume-handle",
                "EBS CSI PersistentVolume declares no volumeHandle",
            )
            continue
        if _VOLUME_ID_PATTERN.fullmatch(str(handle)) is None:
            _non_participation(
                record,
                "volume-handle-is-not-an-ebs-volume-id",
                f"PersistentVolume volumeHandle {handle!r} is not an EBS volume ID",
            )
            continue
        record["participating"] = True
        record["volume_id"] = str(handle)

    records.sort(key=lambda record: (record["namespace"], record["name"]))
    return records


def _snapshot_record(
    snapshot: VolumeSnapshot,
    *,
    cluster_tag_key: str,
) -> dict[str, Any]:
    return {
        "volume_id": snapshot.volume_id,
        "region": snapshot.region,
        "availability_zone": snapshot.availability_zone,
        "size_gib": snapshot.size_gib,
        "state": snapshot.state,
        "cluster_tag_key": cluster_tag_key,
        "cluster_tag_value": snapshot.cluster_tag_value,
        "attachment_ids": list(snapshot.attachment_ids),
    }


def describe_recorded_volumes(
    session: Any,
    *,
    target: RegionalVolumeTarget,
    volume_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Return normalized EBS facts for exact volume IDs in the target Region.

    One request per exact volume ID keeps an absent or malformed volume from
    hiding the others: each ID resolves to a normalized snapshot record or to a
    machine-readable observation error, never to a silently missing entry.
    """
    client = session.client("ec2", region_name=target.region)
    observations: dict[str, dict[str, Any]] = {}
    for volume_id in sorted(set(volume_ids)):
        try:
            response = client.describe_volumes(VolumeIds=[volume_id])
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "")
            if code == "InvalidVolume.NotFound":
                observations[volume_id] = {
                    "volume_id": volume_id,
                    "observed": False,
                    "reason_code": "ebs-volume-absent",
                    "reason": f"EC2 reports {volume_id} does not exist in {target.region}",
                }
                continue
            observations[volume_id] = {
                "volume_id": volume_id,
                "observed": False,
                "reason_code": "ebs-describe-error",
                "reason": f"DescribeVolumes failed with {code or type(exc).__name__}",
            }
            continue
        volumes = response.get("Volumes") or []
        if len(volumes) != 1:
            observations[volume_id] = {
                "volume_id": volume_id,
                "observed": False,
                "reason_code": "ebs-volume-ambiguous",
                "reason": f"DescribeVolumes returned {len(volumes)} volumes for {volume_id}",
            }
            continue
        try:
            snapshot = normalize_volume_snapshot(volumes[0], target=target)
        except VolumeNormalizationError as exc:
            observations[volume_id] = {
                "volume_id": volume_id,
                "observed": False,
                "reason_code": "ebs-normalization-error",
                "reason": f"EBS volume {volume_id} could not be normalized safely: {exc}",
            }
            continue
        if snapshot is None:
            observations[volume_id] = {
                "volume_id": volume_id,
                "observed": False,
                "reason_code": "ebs-volume-outside-target-scope",
                "reason": (
                    f"EBS volume {volume_id} carries no {target.cluster_tag_key} tag or lies "
                    f"outside {target.region}"
                ),
            }
            continue
        observations[volume_id] = {
            **_snapshot_record(snapshot, cluster_tag_key=target.cluster_tag_key),
            "observed": True,
        }
    return observations


def configured_component_size_gib(
    cdk_context: Mapping[str, Any],
    *,
    block: str,
    documented_default_gib: int,
) -> tuple[int, str]:
    """Return the configured PVC size for one observability component in GiB."""
    observability = cdk_context.get("cluster_observability")
    component = observability.get(block) if isinstance(observability, Mapping) else None
    configured = component.get("persistence_size") if isinstance(component, Mapping) else None
    parsed = quantity_to_gib(configured)
    if parsed is None:
        return documented_default_gib, f"documented default for {block}"
    return parsed, f"cdk.json cluster_observability.{block}.persistence_size"


def observability_size_assertions(
    records: Sequence[Mapping[str, Any]],
    observations: Mapping[str, Mapping[str, Any]],
    *,
    cdk_context: Mapping[str, Any],
    components: Sequence[tuple[str, str, int]] = OBSERVABILITY_COMPONENTS,
) -> dict[str, Any]:
    """Assert each observability component's observed size on its own terms.

    Component membership comes from the PVC's live component label; the
    expected size is only ever compared against what was observed, so a
    renamed release or a resized PVC surfaces as a failed assertion rather than
    silently redefining which volume the run recorded.
    """
    assertions: dict[str, Any] = {}
    for component, block, documented_default_gib in components:
        expected_gib, expected_source = configured_component_size_gib(
            cdk_context,
            block=block,
            documented_default_gib=documented_default_gib,
        )
        matched = [record for record in records if record.get("component") == component]
        failures: list[str] = []
        observed: list[dict[str, Any]] = []
        for record in matched:
            volume_id = record.get("volume_id")
            observation = observations.get(str(volume_id)) if volume_id else None
            ebs_size = observation.get("size_gib") if isinstance(observation, Mapping) else None
            entry = {
                "namespace": record.get("namespace"),
                "name": record.get("name"),
                "uid": record.get("uid"),
                "requested_size_gib": record.get("requested_size_gib"),
                "volume_id": volume_id,
                "ebs_size_gib": ebs_size,
                "participating": bool(record.get("participating")),
            }
            observed.append(entry)
            if not record.get("participating"):
                failures.append(
                    f"{record.get('namespace')}/{record.get('name')} produced no EBS volume: "
                    f"{record.get('reason_code')}"
                )
                continue
            if record.get("requested_size_gib") != expected_gib:
                failures.append(
                    f"{record.get('namespace')}/{record.get('name')} requests "
                    f"{record.get('requested_size_gib')} GiB, expected {expected_gib} GiB"
                )
            if ebs_size is not None and ebs_size != expected_gib:
                failures.append(
                    f"{record.get('namespace')}/{record.get('name')} is backed by a "
                    f"{ebs_size} GiB EBS volume, expected {expected_gib} GiB"
                )
        if not matched:
            failures.append(f"No PVC declares the {component!r} component label")
        assertions[component] = {
            "expected_size_gib": expected_gib,
            "expected_source": expected_source,
            "pvcs": observed,
            "status": "failed" if failures else "verified",
            "failures": failures,
        }
    return assertions
