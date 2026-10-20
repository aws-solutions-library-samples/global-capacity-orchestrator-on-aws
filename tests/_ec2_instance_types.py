"""EC2 ``DescribeInstanceTypes`` records for the NodePool generator tests.

``cli/nodepools.py`` reads an instance type's vCPUs and accelerator class from
one ``DescribeInstanceTypes`` call. These records carry the fields that call
really returns for a few representative types (``VCpuInfo``, ``GpuInfo`` with
the GPU manufacturer, ``NeuronInfo``, ``ProcessorInfo``), so the generator
tests run the real classification over realistic shapes without credentials.
``fake_ec2_client`` stands in for ``boto3.client``: it answers from the table
and raises the ``ClientError`` EC2 raises for a type it does not know.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from botocore.exceptions import ClientError


def _record(
    instance_type: str,
    vcpus: int,
    *,
    gpu: tuple[str, str, int] | None = None,
    neuron: tuple[str, int] | None = None,
    architectures: tuple[str, ...] = ("x86_64",),
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "InstanceType": instance_type,
        "VCpuInfo": {"DefaultVCpus": vcpus},
        "ProcessorInfo": {"SupportedArchitectures": list(architectures)},
    }
    if gpu is not None:
        manufacturer, name, count = gpu
        record["GpuInfo"] = {
            "Gpus": [{"Name": name, "Manufacturer": manufacturer, "Count": count}],
            "TotalGpuMemoryInMiB": 16384 * count,
        }
    if neuron is not None:
        name, count = neuron
        record["NeuronInfo"] = {"NeuronDevices": [{"Name": name, "Count": count}]}
    return record


#: ``instance type -> DescribeInstanceTypes record``, with the vCPU counts EC2 reports.
EC2_INSTANCE_TYPES: dict[str, dict[str, Any]] = {
    record["InstanceType"]: record
    for record in (
        _record("p4d.24xlarge", 96, gpu=("NVIDIA", "A100", 8)),
        _record("p5.48xlarge", 192, gpu=("NVIDIA", "H100", 8)),
        _record("p6-b200.48xlarge", 192, gpu=("NVIDIA", "B200", 8)),
        _record("g4dn.xlarge", 4, gpu=("NVIDIA", "T4", 1)),
        _record("g5.xlarge", 4, gpu=("NVIDIA", "A10G", 1)),
        _record("g5.8xlarge", 32, gpu=("NVIDIA", "A10G", 1)),
        _record("g5g.xlarge", 4, gpu=("NVIDIA", "T4g", 1), architectures=("arm64",)),
        _record("gr6.4xlarge", 16, gpu=("NVIDIA", "L4", 1)),
        _record("g7e.2xlarge", 8, gpu=("NVIDIA", "RTX PRO 6000", 1)),
        # An AMD GPU: a GPU, but not one the nvidia.com/gpu taint is for.
        _record("g4ad.xlarge", 4, gpu=("AMD", "Radeon Pro V520", 1)),
        _record("trn2.3xlarge", 16, neuron=("Trainium2", 1)),
        _record("inf2.xlarge", 4, neuron=("Inferentia2", 1)),
        _record("m5.xlarge", 4),
        _record("m5.2xlarge", 8),
        _record("m6i.large", 2),
        _record("c5.xlarge", 4),
        _record("c5n.18xlarge", 72),
    )
}


def fake_ec2_client(*args: Any, **kwargs: Any) -> MagicMock:
    """A ``boto3.client`` stand-in whose ``describe_instance_types`` answers from the table."""
    client = MagicMock(name="ec2")

    def describe_instance_types(InstanceTypes: list[str]) -> dict[str, Any]:
        (instance_type,) = InstanceTypes
        record = EC2_INSTANCE_TYPES.get(instance_type)
        if record is None:
            raise ClientError(
                {
                    "Error": {
                        "Code": "InvalidInstanceType",
                        "Message": (
                            f"The following supplied instance types do not exist: [{instance_type}]"
                        ),
                    }
                },
                "DescribeInstanceTypes",
            )
        return {"InstanceTypes": [record]}

    client.describe_instance_types.side_effect = describe_instance_types
    return client
