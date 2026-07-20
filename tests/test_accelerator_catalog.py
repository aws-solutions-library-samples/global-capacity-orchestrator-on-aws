"""Deterministic accelerator catalog and Karpenter maintenance policy tests."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.accelerator_catalog import (
    Catalog,
    FamilyPolicy,
    NodePoolReference,
    ValidationReport,
    load_nodepools,
    validate_nodepools,
    validate_repository,
    validate_watch_instance_types,
)


def test_repository_accelerator_configuration_is_current() -> None:
    """The committed catalog, NodePools, and capacity watch list agree offline."""
    report = validate_repository()
    assert report.ok, report.to_text()


def test_deprecated_p3_reference_names_manifest_and_replacements(tmp_path: Path) -> None:
    """A V100 NodePool failure identifies the exact pool and migration choices."""
    catalog = Catalog.load()
    manifest = tmp_path / "40-nodepool-legacy-gpu.yaml"
    pool = NodePoolReference(
        path=manifest,
        name="legacy-gpu-pool",
        families=("p3",),
        architectures=("amd64",),
    )

    findings = validate_nodepools(catalog, (pool,))
    finding = next(item for item in findings if item.code == "deprecated-family")
    output = ValidationReport((finding,)).to_text()

    assert "legacy-gpu-pool references end-of-life family p3" in output
    assert "40-nodepool-legacy-gpu.yaml" in output
    assert "Remove p3 from this NodePool" in output
    assert "p4d, p5, p5e, p5en" in output


def test_unreferenced_p7_names_nodepools_that_need_updates() -> None:
    """A newly cataloged P generation points to both compatible EFA pools."""
    catalog = Catalog.load()
    p7 = FamilyPolicy(
        name="p7",
        accelerator="nvidia",
        architectures=("amd64",),
        track="nvidia-accelerated-x86",
        generation=7,
        lifecycle="active",
        manifest_allowed=True,
        reason=None,
        replacements=(),
    )
    synthetic = Catalog(
        schema_version=catalog.schema_version,
        last_refreshed_at=catalog.last_refreshed_at,
        source=catalog.source,
        families={**catalog.families, "p7": p7},
        instance_types=tuple(sorted((*catalog.instance_types, "p7.48xlarge"))),
    )

    findings = validate_nodepools(synthetic, load_nodepools())
    finding = next(item for item in findings if item.code == "newer-generation-unreferenced")
    output = ValidationReport((finding,)).to_text()

    assert "generation 7 family/families [p7]" in output
    assert "43-nodepool-efa.yaml" in output
    assert "NodePool gpu-efa-pool" in output
    assert "46-nodepool-mooncake-efa.yaml" in output
    assert "NodePool mooncake-efa-pool" in output
    assert "add a reviewed family from [p7]" in output


def test_incomplete_watch_list_names_every_missing_type(tmp_path: Path) -> None:
    """A stale cdk.json failure lists the missing type and synchronization step."""
    catalog = Catalog.load()
    missing = catalog.instance_types[-1]
    cdk_path = tmp_path / "cdk.json"
    cdk_path.write_text(
        json.dumps(
            {
                "context": {
                    "historical": {
                        "watch_instance_types": list(catalog.instance_types[:-1]),
                    }
                }
            }
        )
    )

    findings = validate_watch_instance_types(catalog, cdk_path)
    finding = next(item for item in findings if item.code == "watch-list-missing")
    output = ValidationReport((finding,)).to_text()

    assert missing in output
    assert "context.historical.watch_instance_types" in output
    assert "ConfigLoader.get_capacity_history_config()" in output


def test_announced_p6e_override_is_explicit_and_not_observed() -> None:
    """Preview scheduling is reviewed explicitly and never invents exact sizes."""
    catalog = Catalog.load()
    policy = catalog.families["p6e-gb200"]

    assert policy.lifecycle == "announced"
    assert policy.manifest_allowed is True
    assert "p6e-gb200" not in catalog.live_families
