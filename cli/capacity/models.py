"""Data classes for capacity checking.

Instance-type characteristics are resolved live from
``ec2:DescribeInstanceTypes`` — see :func:`instance_type_info_from_ec2`. This
module intentionally holds no checked-in instance specification table: one used
to live here (``GPU_INSTANCE_SPECS``, 25 GPU types) and short-circuited the API,
which meant a new accelerator family was invisible until someone remembered to
hand-edit it, and a wrong number propagated silently into NodePool sizing and
capacity scores. One API call per lookup is cheaper than that failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class CapacityCheckError(Exception):
    """A primary capacity availability check failed at the AWS API level.

    Raised when an availability lookup (e.g. DescribeInstanceTypeOfferings)
    fails due to throttling, expired/invalid credentials, denied permissions,
    or a region that isn't opted in. This is distinct from a *successful* lookup
    that reports an instance type as genuinely not offered — masking such
    failures as "unavailable" hides real, actionable errors from the caller.
    """


@dataclass
class InstanceTypeInfo:
    """Compute characteristics of an EC2 instance type, as EC2 reports them.

    Every field is resolved live from ``ec2:DescribeInstanceTypes``. There is
    deliberately no checked-in specification table behind this: a hand-maintained
    catalog silently goes stale, and a stale accelerator count feeds directly
    into NodePool sizing and capacity scoring. Asking EC2 costs one API call and
    is always right.

    The first seven fields keep their historical names and order — the capacity
    heuristics and the recommender read them positionally — and everything added
    since carries a default, so a partial record from an older API shape still
    constructs.

    Memory conventions, which are easy to get wrong:

    * ``memory_gib`` is host RAM.
    * ``gpu_memory_gib`` is the **total** across all accelerators, taken from
      ``GpuInfo.TotalGpuMemoryInMiB``. It is not per-device: a heterogeneous
      ``Gpus[]`` list would make ``count x per_device`` wrong, and the
      on-demand availability heuristics read this field as a total.
    * ``gpu_devices`` carries the per-model breakdown when you need it.
    """

    instance_type: str
    vcpus: int
    memory_gib: float
    gpu_count: int = 0
    gpu_type: str | None = None
    gpu_memory_gib: float = 0
    architecture: str = "x86_64"

    # --- Region the description came from -------------------------------
    # DescribeInstanceTypes is region-scoped: a type absent here may exist
    # elsewhere, so the answer is only meaningful alongside its region.
    region: str | None = None

    # --- Processor ------------------------------------------------------
    cores: int | None = None
    threads_per_core: int | None = None
    architectures: list[str] = field(default_factory=list)
    sustained_clock_speed_ghz: float | None = None
    processor_manufacturer: str | None = None
    current_generation: bool | None = None
    bare_metal: bool | None = None
    hypervisor: str | None = None
    burstable: bool | None = None
    free_tier_eligible: bool | None = None

    # --- Accelerators ---------------------------------------------------
    #: Per-model GPU breakdown: name, manufacturer, count, memory_gib.
    gpu_devices: list[dict[str, Any]] = field(default_factory=list)
    gpu_manufacturer: str | None = None
    #: AWS Neuron (Trainium / Inferentia2) devices, same shape as gpu_devices.
    neuron_devices: list[dict[str, Any]] = field(default_factory=list)
    neuron_count: int = 0
    neuron_memory_gib: float = 0
    #: First-generation Inferentia, reported separately by EC2.
    inference_accelerators: list[dict[str, Any]] = field(default_factory=list)
    inference_accelerator_count: int = 0
    media_accelerators: list[dict[str, Any]] = field(default_factory=list)
    fpgas: list[dict[str, Any]] = field(default_factory=list)

    # --- Network --------------------------------------------------------
    efa_supported: bool | None = None
    efa_max_interfaces: int | None = None
    network_performance: str | None = None
    maximum_network_interfaces: int | None = None
    maximum_network_cards: int | None = None
    ipv6_supported: bool | None = None
    ena_support: str | None = None
    encryption_in_transit_supported: bool | None = None

    # --- Storage --------------------------------------------------------
    instance_storage_supported: bool | None = None
    instance_storage_total_gb: int | None = None
    instance_storage_disks: list[dict[str, Any]] = field(default_factory=list)
    instance_storage_nvme: str | None = None
    ebs_optimized_support: str | None = None
    ebs_encryption_support: str | None = None
    ebs_nvme_support: str | None = None
    ebs_baseline_iops: int | None = None
    ebs_maximum_iops: int | None = None
    ebs_baseline_throughput_mbps: float | None = None
    ebs_maximum_throughput_mbps: float | None = None

    # --- Purchasing and placement ---------------------------------------
    supported_usage_classes: list[str] = field(default_factory=list)
    supported_placement_strategies: list[str] = field(default_factory=list)
    dedicated_hosts_supported: bool | None = None

    # --- Platform capabilities ------------------------------------------
    supported_root_device_types: list[str] = field(default_factory=list)
    supported_virtualization_types: list[str] = field(default_factory=list)
    supported_boot_modes: list[str] = field(default_factory=list)
    hibernation_supported: bool | None = None
    auto_recovery_supported: bool | None = None
    nitro_enclaves_support: str | None = None
    nitro_tpm_support: str | None = None

    @property
    def is_gpu(self) -> bool:
        return self.gpu_count > 0

    @property
    def is_accelerated(self) -> bool:
        """True when the type carries any accelerator, not just an NVIDIA GPU.

        ``is_gpu`` deliberately stays GPU-only because the capacity heuristics
        use it to reason about GPU scarcity specifically; a Trainium node is a
        different supply pool.
        """
        return bool(
            self.gpu_count
            or self.neuron_count
            or self.inference_accelerator_count
            or self.media_accelerators
            or self.fpgas
        )

    @property
    def spot_supported(self) -> bool:
        return "spot" in self.supported_usage_classes

    @property
    def capacity_block_supported(self) -> bool:
        return "capacity-block" in self.supported_usage_classes


@dataclass
class SpotPriceInfo:
    """Spot price information for an instance type."""

    instance_type: str
    availability_zone: str
    current_price: float
    avg_price_7d: float
    min_price_7d: float
    max_price_7d: float
    price_stability: float  # 0-1, higher is more stable


@dataclass
class CapacityEstimate:
    """Capacity availability estimate."""

    instance_type: str
    region: str
    availability_zone: str | None
    capacity_type: str  # "spot" or "on-demand"
    availability: str  # "high", "medium", "low", "unavailable", "unknown"
    confidence: float  # 0-1
    estimated_wait_time: str | None = None
    price_per_hour: float | None = None
    recommendation: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None  # Explicit error string for partial/degraded results


def _mib_to_gib(value: Any) -> float | None:
    """Convert an EC2 MiB quantity to GiB, or None when absent."""
    if value is None:
        return None
    try:
        return round(float(value) / 1024, 2)
    except TypeError, ValueError:
        return None


def instance_type_info_from_ec2(
    record: dict[str, Any], region: str | None = None
) -> InstanceTypeInfo:
    """Build an :class:`InstanceTypeInfo` from one ``DescribeInstanceTypes`` entry.

    A pure function so the mapping can be tested against recorded API shapes
    without AWS credentials, and so the CLI and any future caller derive the
    same fields from the same payload.

    Every read is defensive. EC2 omits whole field groups depending on the
    instance family (``GpuInfo`` only on GPU types, ``NeuronInfo`` only on
    Trainium/Inferentia2, ``InstanceStorageInfo`` only when local disks exist),
    and it adds new groups over time, so a missing key is normal and must not
    raise. Lists are read in full rather than indexed at ``[0]``: a family with
    two accelerator models or two supported architectures would otherwise be
    silently misreported.
    """
    vcpu_info = record.get("VCpuInfo") or {}
    memory_info = record.get("MemoryInfo") or {}
    processor_info = record.get("ProcessorInfo") or {}
    gpu_info = record.get("GpuInfo") or {}
    neuron_info = record.get("NeuronInfo") or {}
    inference_info = record.get("InferenceAcceleratorInfo") or {}
    media_info = record.get("MediaAcceleratorInfo") or {}
    fpga_info = record.get("FpgaInfo") or {}
    network_info = record.get("NetworkInfo") or {}
    efa_info = network_info.get("EfaInfo") or {}
    storage_info = record.get("InstanceStorageInfo") or {}
    ebs_info = record.get("EbsInfo") or {}
    ebs_throughput = ebs_info.get("EbsOptimizedInfo") or {}
    placement_info = record.get("PlacementGroupInfo") or {}

    architectures = list(processor_info.get("SupportedArchitectures") or [])

    gpu_devices = [
        {
            "name": device.get("Name"),
            "manufacturer": device.get("Manufacturer"),
            "count": device.get("Count"),
            "memory_gib": _mib_to_gib((device.get("MemoryInfo") or {}).get("SizeInMiB")),
        }
        for device in (gpu_info.get("Gpus") or [])
    ]
    # Sum across models rather than trusting Gpus[0].Count — a heterogeneous
    # list would otherwise undercount the node's real accelerator budget.
    gpu_count = sum(int(device.get("count") or 0) for device in gpu_devices)

    # Prefer EC2's own total. Fall back to count x per-device across models when
    # the field is absent, because reporting 0 GiB for a node that plainly has
    # GPUs is worse than a derived figure — the on-demand availability
    # heuristics read this as a total and would score the node as CPU-only.
    total_gpu_memory_gib = _mib_to_gib(gpu_info.get("TotalGpuMemoryInMiB"))
    if total_gpu_memory_gib is None and gpu_devices:
        derived = sum(
            float(device.get("memory_gib") or 0) * int(device.get("count") or 0)
            for device in gpu_devices
        )
        total_gpu_memory_gib = round(derived, 2) if derived else None

    neuron_devices = [
        {
            "name": device.get("Name"),
            "count": device.get("Count"),
            "core_count": (device.get("CoreInfo") or {}).get("Count"),
            "core_version": (device.get("CoreInfo") or {}).get("Version"),
            "memory_gib": _mib_to_gib((device.get("MemoryInfo") or {}).get("SizeInMiB")),
        }
        for device in (neuron_info.get("NeuronDevices") or [])
    ]

    inference_accelerators = [
        {
            "name": device.get("Name"),
            "manufacturer": device.get("Manufacturer"),
            "count": device.get("Count"),
            "memory_gib": _mib_to_gib((device.get("MemoryInfo") or {}).get("SizeInMiB")),
        }
        for device in (inference_info.get("Accelerators") or [])
    ]

    return InstanceTypeInfo(
        instance_type=str(record.get("InstanceType") or ""),
        vcpus=int(vcpu_info.get("DefaultVCpus") or 0),
        memory_gib=_mib_to_gib(memory_info.get("SizeInMiB")) or 0.0,
        gpu_count=gpu_count,
        # Join multiple models so the scalar field stays informative on a
        # heterogeneous type instead of naming only the first.
        gpu_type=(
            "+".join(str(device["name"]) for device in gpu_devices if device.get("name")) or None
        ),
        # Total, not per-device — see the InstanceTypeInfo docstring.
        gpu_memory_gib=total_gpu_memory_gib or 0.0,
        architecture=architectures[0] if architectures else "x86_64",
        region=region,
        cores=vcpu_info.get("DefaultCores"),
        threads_per_core=vcpu_info.get("DefaultThreadsPerCore"),
        architectures=architectures,
        sustained_clock_speed_ghz=processor_info.get("SustainedClockSpeedInGhz"),
        processor_manufacturer=processor_info.get("Manufacturer"),
        current_generation=record.get("CurrentGeneration"),
        bare_metal=record.get("BareMetal"),
        hypervisor=record.get("Hypervisor"),
        burstable=record.get("BurstablePerformanceSupported"),
        free_tier_eligible=record.get("FreeTierEligible"),
        gpu_devices=gpu_devices,
        gpu_manufacturer=next(
            (str(device["manufacturer"]) for device in gpu_devices if device.get("manufacturer")),
            None,
        ),
        neuron_devices=neuron_devices,
        neuron_count=sum(int(device.get("count") or 0) for device in neuron_devices),
        neuron_memory_gib=_mib_to_gib(neuron_info.get("TotalNeuronDeviceMemoryInMiB")) or 0.0,
        inference_accelerators=inference_accelerators,
        inference_accelerator_count=sum(
            int(device.get("count") or 0) for device in inference_accelerators
        ),
        media_accelerators=[
            {
                "name": device.get("Name"),
                "manufacturer": device.get("Manufacturer"),
                "count": device.get("Count"),
                "memory_gib": _mib_to_gib((device.get("MemoryInfo") or {}).get("SizeInMiB")),
            }
            for device in (media_info.get("Accelerators") or [])
        ],
        fpgas=[
            {
                "name": device.get("Name"),
                "manufacturer": device.get("Manufacturer"),
                "count": device.get("Count"),
                "memory_gib": _mib_to_gib((device.get("MemoryInfo") or {}).get("SizeInMiB")),
            }
            for device in (fpga_info.get("Fpgas") or [])
        ],
        efa_supported=network_info.get("EfaSupported"),
        efa_max_interfaces=efa_info.get("MaximumEfaInterfaces"),
        network_performance=network_info.get("NetworkPerformance"),
        maximum_network_interfaces=network_info.get("MaximumNetworkInterfaces"),
        maximum_network_cards=network_info.get("MaximumNetworkCards"),
        ipv6_supported=network_info.get("Ipv6Supported"),
        ena_support=network_info.get("EnaSupport"),
        encryption_in_transit_supported=network_info.get("EncryptionInTransitSupported"),
        instance_storage_supported=record.get("InstanceStorageSupported"),
        instance_storage_total_gb=storage_info.get("TotalSizeInGB"),
        instance_storage_disks=[
            {
                "size_gb": disk.get("SizeInGB"),
                "count": disk.get("Count"),
                "type": disk.get("Type"),
            }
            for disk in (storage_info.get("Disks") or [])
        ],
        instance_storage_nvme=storage_info.get("NvmeSupport"),
        ebs_optimized_support=ebs_info.get("EbsOptimizedSupport"),
        ebs_encryption_support=ebs_info.get("EncryptionSupport"),
        ebs_nvme_support=ebs_info.get("NvmeSupport"),
        ebs_baseline_iops=ebs_throughput.get("BaselineIops"),
        ebs_maximum_iops=ebs_throughput.get("MaximumIops"),
        ebs_baseline_throughput_mbps=ebs_throughput.get("BaselineThroughputInMBps"),
        ebs_maximum_throughput_mbps=ebs_throughput.get("MaximumThroughputInMBps"),
        supported_usage_classes=list(record.get("SupportedUsageClasses") or []),
        supported_placement_strategies=list(placement_info.get("SupportedStrategies") or []),
        dedicated_hosts_supported=record.get("DedicatedHostsSupported"),
        supported_root_device_types=list(record.get("SupportedRootDeviceTypes") or []),
        supported_virtualization_types=list(record.get("SupportedVirtualizationTypes") or []),
        supported_boot_modes=list(record.get("SupportedBootModes") or []),
        hibernation_supported=record.get("HibernationSupported"),
        auto_recovery_supported=record.get("AutoRecoverySupported"),
        nitro_enclaves_support=record.get("NitroEnclavesSupport"),
        nitro_tpm_support=record.get("NitroTpmSupport"),
    )
