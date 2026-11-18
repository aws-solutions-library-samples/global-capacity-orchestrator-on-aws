"""Dependency-light FSx for Lustre facts shared by the CLI, the loader and the stack.

The values come from the CloudFormation reference for
``AWS::FSx::FileSystem`` and its ``LustreConfiguration``. GCO creates SSD
file systems (it never sets ``StorageType``), so only the SSD values apply.
No AWS CDK import, so ``gco stacks fsx`` can use the same tables as
``ConfigLoader`` and the regional stack.
"""

from __future__ import annotations

#: cdk.json context keys of the global block and its per-Region overlays.
FSX_LUSTRE_CONTEXT_KEY = "fsx_lustre"
FSX_LUSTRE_REGIONS_CONTEXT_KEY = "fsx_lustre_regions"

#: ``LustreConfiguration.DeploymentType`` values.
FSX_DEPLOYMENT_TYPES: tuple[str, ...] = ("SCRATCH_1", "SCRATCH_2", "PERSISTENT_1", "PERSISTENT_2")

#: ``PerUnitStorageThroughput`` (MB/s per TiB) each PERSISTENT type accepts on
#: SSD storage. SCRATCH types take no throughput setting at all.
FSX_THROUGHPUT_CHOICES: dict[str, tuple[int, ...]] = {
    "PERSISTENT_1": (50, 100, 200),
    "PERSISTENT_2": (125, 250, 500, 1000),
}

#: The throughput a PERSISTENT file system gets when cdk.json names none:
#: PERSISTENT_1 keeps its historical 200; PERSISTENT_2 has no 200, so 250.
FSX_DEFAULT_THROUGHPUT: dict[str, int] = {"PERSISTENT_1": 200, "PERSISTENT_2": 250}

#: ``FileSystemTypeVersion`` values. PERSISTENT_2 does not support 2.10.
FSX_FILE_SYSTEM_TYPE_VERSIONS: tuple[str, ...] = ("2.10", "2.12", "2.15")

#: ``DataCompressionType`` and ``AutoImportPolicy`` values.
FSX_DATA_COMPRESSION_TYPES: tuple[str, ...] = ("NONE", "LZ4")
FSX_AUTO_IMPORT_POLICIES: tuple[str, ...] = ("NONE", "NEW", "NEW_CHANGED", "NEW_CHANGED_DELETED")

#: The ``fsx_lustre`` block's defaults. ``per_unit_storage_throughput`` of
#: ``None`` means the deployment type's default (and nothing for SCRATCH).
FSX_LUSTRE_DEFAULTS: dict[str, object] = {
    "enabled": False,
    "storage_capacity_gib": 1200,
    "deployment_type": "SCRATCH_2",
    "file_system_type_version": "2.15",
    "per_unit_storage_throughput": None,
    "data_compression_type": "LZ4",
    "import_path": None,
    "export_path": None,
    "auto_import_policy": "NEW_CHANGED_DELETED",
}

#: Storage capacity (GiB) increments: every type accepts 1200 and 2400, then
#: multiples of 3600 for SCRATCH_1 and of 2400 for the SSD types.
FSX_SMALL_CAPACITIES_GIB: tuple[int, ...] = (1200, 2400)
FSX_CAPACITY_INCREMENT_GIB: dict[str, int] = {
    "SCRATCH_1": 3600,
    "SCRATCH_2": 2400,
    "PERSISTENT_1": 2400,
    "PERSISTENT_2": 2400,
}


def default_per_unit_storage_throughput(deployment_type: str) -> int | None:
    """The throughput a ``deployment_type`` gets by default (``None`` for SCRATCH)."""
    return FSX_DEFAULT_THROUGHPUT.get(deployment_type)


def valid_storage_capacity(deployment_type: str, capacity_gib: int) -> bool:
    """Whether FSx accepts ``capacity_gib`` for ``deployment_type``."""
    if capacity_gib in FSX_SMALL_CAPACITIES_GIB:
        return True
    increment = FSX_CAPACITY_INCREMENT_GIB[deployment_type]
    return capacity_gib > 0 and capacity_gib % increment == 0
