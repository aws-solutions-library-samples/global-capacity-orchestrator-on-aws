"""Deterministic accelerator catalog and Karpenter maintenance policy tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.accelerator_catalog import (
    Catalog,
    CatalogError,
    FamilyPolicy,
    NodePoolReference,
    ValidationReport,
    load_nodepools,
    validate_nodepools,
    validate_repository,
    validate_watch_instance_types,
)


def test_repository_accelerator_configuration_is_current() -> None:
    """The catalog, NodePools, and both capacity watch-list defaults agree offline."""
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


def test_config_loader_fallback_drift_is_reported_by_repository_validation(
    tmp_path: Path,
) -> None:
    """The standalone validator catches drift isolated to ConfigLoader's fallback."""
    catalog = Catalog.load()
    missing = catalog.instance_types[-1]
    config_loader_path = tmp_path / "config_loader.py"
    config_loader_path.write_text(
        "class ConfigLoader:\n"
        "    def get_capacity_history_config(self):\n"
        "        default_config = {\n"
        f"            'watch_instance_types': {list(catalog.instance_types[:-1])!r},\n"
        "        }\n"
        "        return default_config\n",
        encoding="utf-8",
    )

    report = validate_repository(config_loader_path=config_loader_path)
    finding = next(
        item for item in report.findings if item.code == "config-loader-watch-list-missing"
    )
    output = ValidationReport((finding,)).to_text()

    assert missing in output
    assert str(config_loader_path) in output
    assert "ConfigLoader.get_capacity_history_config()" in output
    assert "cdk.json context.historical.watch_instance_types" in output


def test_announced_family_requires_explicit_manifest_override() -> None:
    """Preview families cannot become schedulable through an omitted policy field."""
    with pytest.raises(CatalogError, match="manifest_allowed is required for announced families"):
        FamilyPolicy.from_mapping(
            "p7-preview",
            {
                "accelerator": "nvidia",
                "architectures": ["amd64"],
                "track": "nvidia-accelerated-x86",
                "generation": 7,
                "lifecycle": "announced",
            },
        )


def test_announced_p6e_override_is_explicit_and_not_observed() -> None:
    """Preview scheduling is reviewed explicitly and never invents exact sizes."""
    catalog = Catalog.load()
    policy = catalog.families["p6e-gb200"]

    assert policy.lifecycle == "announced"
    assert policy.manifest_allowed is True
    assert "p6e-gb200" not in catalog.live_families
