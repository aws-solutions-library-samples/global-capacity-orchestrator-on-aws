"""Deterministic accelerator catalog and Karpenter maintenance policy tests."""

from __future__ import annotations

import argparse
import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from botocore.config import Config

from scripts import accelerator_catalog
from scripts.accelerator_catalog import (
    DEFAULT_CATALOG_PATH,
    Catalog,
    CatalogDrift,
    CatalogError,
    DiscoveredFamily,
    Discovery,
    FamilyPolicy,
    Finding,
    InstancePool,
    NodePoolReference,
    ValidationReport,
    _detect_accelerator,
    _instance_architectures,
    _utc_timestamp,
    compare_catalog,
    discover_accelerator_catalog,
    load_nodepools,
    main,
    refresh_catalog,
    validate_config_loader_watch_instance_types,
    validate_instance_pools,
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


# ---------------------------------------------------------------------------
# Shared fixtures for the malformed-input, drift, and CLI tests below
# ---------------------------------------------------------------------------

_BASE_FAMILY: dict[str, object] = {
    "accelerator": "nvidia",
    "architectures": ["amd64"],
    "track": "nvidia-general-x86",
    "generation": 5,
    "lifecycle": "active",
}

_BASE_CATALOG: dict[str, object] = {
    "schema_version": 1,
    "last_refreshed_at": "2000-01-01T00:00:00Z",
    "source": {"api": "ec2:DescribeInstanceTypes"},
    "families": {"g5": dict(_BASE_FAMILY)},
    "instance_types": ["g5.xlarge"],
}


def _policy(name: str, **overrides: Any) -> FamilyPolicy:
    values: dict[str, Any] = {
        "name": name,
        "accelerator": "nvidia",
        "architectures": ("amd64",),
        "track": "nvidia-general-x86",
        "generation": 5,
        "lifecycle": "active",
        "manifest_allowed": True,
        "reason": None,
        "replacements": (),
    }
    values.update(overrides)
    return FamilyPolicy(**values)


def _synthetic_catalog(
    families: dict[str, FamilyPolicy], instance_types: tuple[str, ...]
) -> Catalog:
    return Catalog(
        schema_version=1,
        last_refreshed_at="2000-01-01T00:00:00Z",
        source={"api": "ec2:DescribeInstanceTypes"},
        families=families,
        instance_types=instance_types,
    )


def _write_catalog(path: Path, **overrides: object) -> Path:
    """Write ``_BASE_CATALOG`` with top-level ``overrides`` applied."""
    payload = json.loads(json.dumps(_BASE_CATALOG))
    payload.update(overrides)
    path.write_text(json.dumps(payload))
    return path


def _write_family_catalog(path: Path, **family_overrides: object) -> Path:
    """Write ``_BASE_CATALOG`` with ``family_overrides`` applied to families.g5."""
    family = dict(_BASE_FAMILY)
    family.update(family_overrides)
    return _write_catalog(path, families={"g5": family})


def _nodepool_document(name: str, families: list[str], architectures: list[str] | None) -> str:
    requirements: list[dict[str, object]] = [
        {"key": "karpenter.sh/capacity-type", "operator": "In", "values": ["spot"]},
        {"key": "eks.amazonaws.com/instance-family", "operator": "In", "values": families},
    ]
    if architectures is not None:
        requirements.append(
            {"key": "kubernetes.io/arch", "operator": "In", "values": architectures}
        )
    return json.dumps(
        {
            "apiVersion": "karpenter.sh/v1",
            "kind": "NodePool",
            "metadata": {"name": name},
            "spec": {"template": {"spec": {"requirements": requirements}}},
        }
    )


def _write_cdk(path: Path, watch_instance_types: list[str]) -> Path:
    path.write_text(
        json.dumps({"context": {"historical": {"watch_instance_types": watch_instance_types}}})
    )
    return path


def _write_config_loader(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def _discovery_matching(catalog: Catalog, *extra_types: str, regions: int = 2) -> Discovery:
    """Return a discovery whose families mirror ``catalog`` exactly."""
    families = {
        name: DiscoveredFamily(accelerator=policy.accelerator, architectures=policy.architectures)
        for name, policy in catalog.families.items()
        if name in catalog.live_families
    }
    for extra in extra_types:
        family = extra.split(".", 1)[0]
        families[family] = DiscoveredFamily(accelerator="nvidia", architectures=("amd64",))
    return Discovery(
        regions=tuple(f"region-{index}" for index in range(regions)),
        instance_types=tuple(sorted((*catalog.instance_types, *extra_types))),
        families=families,
    )


# ---------------------------------------------------------------------------
# Catalog.load rejects every malformed shape with a located message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema_version": 2}, "schema_version must be 1"),
        (
            {"last_refreshed_at": "2000-01-01 00:00:00Z"},
            "last_refreshed_at must be an ISO 8601 UTC timestamp ending in Z",
        ),
        (
            {"last_refreshed_at": "2000-01-01T00:00:00+00:00"},
            "last_refreshed_at must be an ISO 8601 UTC timestamp ending in Z",
        ),
        (
            {"last_refreshed_at": "2000-13-01T00:00:00Z"},
            "last_refreshed_at must be an ISO 8601 UTC timestamp ending in Z",
        ),
        ({"last_refreshed_at": ""}, "last_refreshed_at must be a non-empty string"),
        ({"source": "ec2"}, "source must be a JSON/YAML object with string keys"),
        ({"families": ["g5"]}, "families must be a JSON/YAML object with string keys"),
        ({"instance_types": "g5.xlarge"}, "instance_types must be a list of non-empty strings"),
        ({"instance_types": ["g5.xlarge", ""]}, "instance_types must be a list of non-empty"),
        ({"instance_types": []}, "instance_types must not be empty"),
        (
            {"instance_types": ["g5.xlarge", "g5.2xlarge"]},
            "instance_types must be sorted lexicographically",
        ),
        ({"instance_types": ["g5.xlarge", "g5.xlarge"]}, "instance_types contains duplicates"),
        ({"instance_types": ["g5.xlarge", "g6.xlarge"]}, "g6.xlarge has no reviewed families.g6"),
        ({"instance_types": ["g5"]}, "invalid EC2 instance type in catalog: 'g5'"),
        ({"instance_types": [".xlarge"]}, "invalid EC2 instance type in catalog: '.xlarge'"),
    ],
)
def test_catalog_load_rejects_malformed_top_level_fields(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    """Each catalog-level schema violation is a CatalogError naming the field."""
    path = _write_catalog(tmp_path / "catalog.json", **overrides)
    with pytest.raises(CatalogError, match=message):
        Catalog.load(path)


@pytest.mark.parametrize(
    ("family_overrides", "message"),
    [
        ({"accelerator": "amd"}, r"families.g5.accelerator must be one of \['neuron', 'nvidia'\]"),
        ({"accelerator": 7}, "families.g5.accelerator must be a non-empty string"),
        ({"lifecycle": "retired"}, "families.g5.lifecycle must be one of"),
        ({"generation": -1}, "families.g5.generation must be a non-negative integer"),
        ({"generation": True}, "families.g5.generation must be a non-negative integer"),
        ({"generation": "5"}, "families.g5.generation must be a non-negative integer"),
        ({"manifest_allowed": "yes"}, "families.g5.manifest_allowed must be a boolean"),
        ({"reason": 5}, "families.g5.reason must be a string when present"),
        ({"replacements": "p5"}, "families.g5.replacements must be a list of non-empty strings"),
        ({"architectures": []}, "families.g5.architectures must not be empty"),
        ({"track": ""}, "families.g5.track must be a non-empty string"),
    ],
)
def test_catalog_load_rejects_malformed_family_policy(
    tmp_path: Path, family_overrides: dict[str, object], message: str
) -> None:
    """Each family-level policy violation is a CatalogError naming families.<name>."""
    path = _write_family_catalog(tmp_path / "catalog.json", **family_overrides)
    with pytest.raises(CatalogError, match=message):
        Catalog.load(path)


def test_catalog_load_accepts_optional_family_fields(tmp_path: Path) -> None:
    """Explicit overrides, reasons, and replacements survive a load."""
    path = _write_family_catalog(
        tmp_path / "catalog.json",
        lifecycle="deprecated",
        manifest_allowed=False,
        reason="Retired by policy.",
        replacements=["g6"],
    )
    catalog = Catalog.load(path)
    policy = catalog.families["g5"]

    assert policy.lifecycle == "deprecated"
    assert policy.manifest_allowed is False
    assert policy.reason == "Retired by policy."
    assert policy.replacements == ("g6",)
    assert catalog.live_families == frozenset({"g5"})


def test_catalog_load_reports_unreadable_or_invalid_json(tmp_path: Path) -> None:
    """A missing or non-JSON catalog surfaces the path and the underlying error."""
    missing = tmp_path / "missing.json"
    with pytest.raises(CatalogError, match=f"cannot read accelerator catalog {missing}"):
        Catalog.load(missing)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(CatalogError, match="cannot read accelerator catalog .*Expecting"):
        Catalog.load(broken)

    listed = tmp_path / "list.json"
    listed.write_text("[]")
    with pytest.raises(CatalogError, match="must be a JSON/YAML object with string keys"):
        Catalog.load(listed)


def test_checked_in_catalog_is_in_normalized_form() -> None:
    """The committed JSON equals its own normalization, so refreshes diff cleanly."""
    catalog = Catalog.load()

    assert catalog.to_mapping() == json.loads(DEFAULT_CATALOG_PATH.read_text())
    for name, policy in catalog.families.items():
        assert FamilyPolicy.from_mapping(name, policy.to_mapping()) == policy


def test_family_policy_to_mapping_emits_only_non_default_fields() -> None:
    """manifest_allowed, reason, and replacements appear only when they carry information."""
    assert _policy("g5").to_mapping() == {
        "accelerator": "nvidia",
        "architectures": ["amd64"],
        "track": "nvidia-general-x86",
        "generation": 5,
        "lifecycle": "active",
    }
    assert _policy("g5", manifest_allowed=False).to_mapping()["manifest_allowed"] is False
    deprecated = _policy(
        "p3", lifecycle="deprecated", manifest_allowed=False, reason="V100", replacements=("p5",)
    ).to_mapping()
    assert "manifest_allowed" not in deprecated
    assert deprecated["reason"] == "V100"
    assert deprecated["replacements"] == ["p5"]
    announced = _policy("p7", lifecycle="announced", manifest_allowed=False).to_mapping()
    assert announced["manifest_allowed"] is False


# ---------------------------------------------------------------------------
# ValidationReport rendering
# ---------------------------------------------------------------------------


def test_validation_report_renders_success_in_text_and_markdown() -> None:
    report = ValidationReport(())

    assert report.ok
    assert report.to_text() == (
        "Accelerator catalog validation passed: NodePools, both watch lists, "
        "and the instance pools are current.\n"
    )
    markdown = report.to_markdown()
    assert markdown.startswith("## Accelerator catalog and NodePool policy\n")
    assert "**Status: PASS.**" in markdown
    assert "are synchronized." in markdown


def test_validation_report_renders_findings_with_and_without_locations() -> None:
    located = Finding(
        code="watch-list-missing",
        title="watch list omits types",
        detail="Missing g9.xlarge.",
        recommendation="Add it.",
        locations=("cdk.json",),
    )
    unlocated = Finding(
        code="instance-pool-too-small",
        title="pool tiny is too small",
        detail="Two members.",
        recommendation="Add a third.",
    )
    report = ValidationReport((located, unlocated))

    text = report.to_text()
    assert text.startswith("Accelerator catalog validation failed with 2 finding(s):")
    assert "ERROR [watch-list-missing] watch list omits types" in text
    assert "  Location: cdk.json" in text
    assert text.count("Location:") == 1
    assert "  Why: Two members." in text
    assert "  Recommended change: Add a third." in text

    markdown = report.to_markdown()
    assert "**Status: ACTION REQUIRED.**" in markdown
    assert "2 actionable finding(s):" in markdown
    assert "### watch list omits types" in markdown
    assert "- **Location:** cdk.json" in markdown
    assert markdown.count("**Location:**") == 1
    assert "- **Why:** Two members." in markdown
    assert "- **Recommended change:** Add a third." in markdown


# ---------------------------------------------------------------------------
# NodePool manifest loading
# ---------------------------------------------------------------------------


def test_load_nodepools_skips_empty_and_non_nodepool_documents(tmp_path: Path) -> None:
    """Only the NodePool document counts, and pools without families are ignored."""
    (tmp_path / "10-nodepool-class-only.yaml").write_text(
        json.dumps({"kind": "NodeClass", "metadata": {"name": "default"}})
    )
    (tmp_path / "20-nodepool-multi.yaml").write_text(
        "---\n---\n"
        + json.dumps({"kind": "NodeClass", "metadata": {"name": "default"}})
        + "\n---\n"
        + _nodepool_document("gpu-pool", ["g5", "g6"], ["amd64"])
        + "\n"
    )
    (tmp_path / "30-nodepool-no-family.yaml").write_text(
        json.dumps(
            {
                "kind": "NodePool",
                "metadata": {"name": "cpu-pool"},
                "spec": {"template": {"spec": {}}},
            }
        )
    )
    (tmp_path / "40-nodepool-no-arch.yaml").write_text(_nodepool_document("neuron", ["inf2"], None))
    (tmp_path / "50-deployment.yaml").write_text(_nodepool_document("ignored", ["p5"], None))

    pools = load_nodepools(tmp_path)

    assert [(pool.name, pool.families, pool.architectures) for pool in pools] == [
        ("gpu-pool", ("g5", "g6"), ("amd64",)),
        ("neuron", ("inf2",), ()),
    ]
    assert pools[0].path == tmp_path / "20-nodepool-multi.yaml"
    assert pools[0].location == f"{tmp_path / '20-nodepool-multi.yaml'} (NodePool gpu-pool)"


def test_load_nodepools_reports_unparseable_yaml(tmp_path: Path) -> None:
    manifest = tmp_path / "10-nodepool-broken.yaml"
    manifest.write_text("kind: NodePool\nmetadata: [unclosed\n")

    with pytest.raises(CatalogError, match=f"cannot read NodePool manifest {manifest}"):
        load_nodepools(tmp_path)


def test_load_nodepools_rejects_non_list_requirements(tmp_path: Path) -> None:
    manifest = tmp_path / "10-nodepool-odd.yaml"
    manifest.write_text(
        json.dumps(
            {
                "kind": "NodePool",
                "metadata": {"name": "odd"},
                "spec": {"template": {"spec": {"requirements": "g5"}}},
            }
        )
    )

    with pytest.raises(CatalogError, match="spec.template.spec.requirements must be a list"):
        load_nodepools(tmp_path)


def test_load_nodepools_rejects_documents_with_non_string_keys(tmp_path: Path) -> None:
    manifest = tmp_path / "10-nodepool-keys.yaml"
    manifest.write_text("1: one\nkind: NodePool\n")

    with pytest.raises(
        CatalogError, match="document 1 must be a JSON/YAML object with string keys"
    ):
        load_nodepools(tmp_path)


# ---------------------------------------------------------------------------
# NodePool policy findings beyond the shipped manifests
# ---------------------------------------------------------------------------


def test_unknown_family_reference_is_reported_and_not_silently_allowed(tmp_path: Path) -> None:
    catalog = Catalog.load()
    pool = NodePoolReference(
        path=tmp_path / "47-nodepool-future.yaml",
        name="future-pool",
        families=("zz9",),
        architectures=("amd64",),
    )

    findings = validate_nodepools(catalog, (pool,))
    finding = next(item for item in findings if item.code == "unknown-family")

    assert finding.title == "future-pool references unreviewed family zz9"
    assert finding.locations == (pool.location,)
    assert "gco/config/accelerator_catalog.json" in finding.detail
    assert "Review zz9 against EC2" in finding.recommendation
    assert not any(item.code == "deprecated-family" for item in findings)


def test_architecture_mismatch_names_the_incompatible_pool(tmp_path: Path) -> None:
    catalog = Catalog.load()
    pool = NodePoolReference(
        path=tmp_path / "41-nodepool-gpu-arm.yaml",
        name="gpu-arm-pool",
        families=("g5",),
        architectures=("arm64",),
    )

    findings = validate_nodepools(catalog, (pool,))
    finding = next(item for item in findings if item.code == "architecture-mismatch")

    assert finding.title == "gpu-arm-pool cannot launch g5"
    assert "requires ['arm64'], but g5 is cataloged for ['amd64']" in finding.detail
    assert "architecture-compatible NodePool" in finding.recommendation


def test_newer_generation_without_a_compatible_pool_asks_for_a_new_pool() -> None:
    """When no pool shares the track, the recommendation is to create one."""
    catalog = _synthetic_catalog(
        {"g5": _policy("g5"), "g5g": _policy("g5g", architectures=("arm64",), generation=6)},
        ("g5.xlarge", "g5g.xlarge"),
    )
    pool = NodePoolReference(
        path=Path("/manifests/40-nodepool-gpu-x86.yaml"),
        name="gpu-x86-pool",
        families=("g5",),
        architectures=("amd64",),
    )

    findings = validate_nodepools(catalog, (pool,))
    finding = next(item for item in findings if item.code == "newer-generation-unreferenced")

    assert finding.locations == ()
    assert "generation 6 family/families [g5g]" in finding.detail
    assert "Create an architecture-compatible NodePool for [g5g]" in finding.recommendation


# ---------------------------------------------------------------------------
# Watch-list comparison (cdk.json and ConfigLoader fallback)
# ---------------------------------------------------------------------------


def test_watch_list_duplicates_are_reported_alongside_order(tmp_path: Path) -> None:
    catalog = Catalog.load()
    cdk_path = _write_cdk(
        tmp_path / "cdk.json", [*catalog.instance_types, catalog.instance_types[0]]
    )

    findings = validate_watch_instance_types(catalog, cdk_path)
    by_code = {finding.code: finding for finding in findings}

    assert set(by_code) == {"watch-list-duplicates", "watch-list-order"}
    assert by_code["watch-list-duplicates"].detail == (
        f"Duplicate values: {catalog.instance_types[0]}."
    )
    assert by_code["watch-list-duplicates"].locations == (str(cdk_path),)


def test_watch_list_unexpected_type_is_reported(tmp_path: Path) -> None:
    catalog = Catalog.load()
    cdk_path = _write_cdk(tmp_path / "cdk.json", [*catalog.instance_types, "z9.mega"])

    findings = validate_watch_instance_types(catalog, cdk_path)
    finding = next(item for item in findings if item.code == "watch-list-unexpected")

    assert finding.detail == "Unexpected 1 type(s): z9.mega."
    assert "remove them from context.historical.watch_instance_types" in finding.recommendation
    assert not any(item.code == "watch-list-order" for item in findings)


def test_watch_list_out_of_order_is_the_only_finding(tmp_path: Path) -> None:
    catalog = Catalog.load()
    cdk_path = _write_cdk(tmp_path / "cdk.json", list(reversed(catalog.instance_types)))

    findings = validate_watch_instance_types(catalog, cdk_path)

    assert [finding.code for finding in findings] == ["watch-list-order"]
    assert "gco/config/accelerator_catalog.json instance_types" in findings[0].recommendation


def test_watch_list_reports_unreadable_cdk_json(tmp_path: Path) -> None:
    catalog = Catalog.load()
    missing = tmp_path / "cdk.json"

    with pytest.raises(CatalogError, match=f"cannot read {missing}"):
        validate_watch_instance_types(catalog, missing)

    missing.write_text("{oops")
    with pytest.raises(CatalogError, match=f"cannot read {missing}"):
        validate_watch_instance_types(catalog, missing)


def test_config_loader_annotated_default_config_is_accepted(tmp_path: Path) -> None:
    """An annotated ``default_config: dict = {...}`` literal is parsed like a plain one."""
    catalog = Catalog.load()
    path = _write_config_loader(
        tmp_path / "config_loader.py",
        "class ConfigLoader:\n"
        "    def get_capacity_history_config(self):\n"
        "        default_config: dict[str, object] = {\n"
        "            'enabled': True,\n"
        f"            'watch_instance_types': {list(catalog.instance_types)!r},\n"
        "        }\n"
        "        return default_config\n",
    )

    assert validate_config_loader_watch_instance_types(catalog, path) == ()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("class ConfigLoader(:\n", "cannot parse"),
        ("class Other:\n    pass\n", "expected exactly one ConfigLoader class"),
        (
            "class ConfigLoader:\n    def other(self):\n        pass\n",
            "expected exactly one get_capacity_history_config method",
        ),
        (
            "class ConfigLoader:\n"
            "    def get_capacity_history_config(self):\n"
            "        return build_default()\n",
            "expected one literal default_config",
        ),
        (
            "class ConfigLoader:\n"
            "    def get_capacity_history_config(self):\n"
            "        default_config = {'enabled': True}\n"
            "        return default_config\n",
            "expected one default_config watch_instance_types value",
        ),
        (
            "class ConfigLoader:\n"
            "    def get_capacity_history_config(self):\n"
            "        default_config = {'watch_instance_types': load_types()}\n"
            "        return default_config\n",
            "default_config watch_instance_types must be a literal list",
        ),
        (
            "class ConfigLoader:\n"
            "    def get_capacity_history_config(self):\n"
            "        default_config = {'watch_instance_types': 'g5.xlarge'}\n"
            "        return default_config\n",
            "default_config.watch_instance_types must be a list of non-empty strings",
        ),
    ],
)
def test_config_loader_fallback_shape_violations_are_reported(
    tmp_path: Path, body: str, message: str
) -> None:
    catalog = Catalog.load()
    path = _write_config_loader(tmp_path / "config_loader.py", body)

    with pytest.raises(CatalogError, match=message):
        validate_config_loader_watch_instance_types(catalog, path)


def test_config_loader_missing_file_is_reported(tmp_path: Path) -> None:
    missing = tmp_path / "config_loader.py"

    with pytest.raises(CatalogError, match=f"cannot parse {missing}"):
        validate_config_loader_watch_instance_types(Catalog.load(), missing)


# ---------------------------------------------------------------------------
# Instance-pool policy: duplicate pool names
# ---------------------------------------------------------------------------


def test_duplicate_pool_names_are_reported_once() -> None:
    catalog = Catalog.load()
    pools = (
        InstancePool(name="dup", members=("g5.xlarge", "g5.2xlarge", "g5.4xlarge")),
        InstancePool(name="dup", members=("g6.xlarge", "g6.2xlarge", "g6.4xlarge")),
    )
    covered = {member for pool in pools for member in pool.members}
    rest = tuple(item for item in catalog.instance_types if item not in covered)

    findings = validate_instance_pools(catalog, pools, rest)

    assert [finding.code for finding in findings] == ["instance-pool-duplicate-name"]
    assert findings[0].detail == "Duplicated pool name(s): dup."


# ---------------------------------------------------------------------------
# EC2 response interpretation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("instance", "expected"),
    [
        ({"GpuInfo": {"Gpus": [{"Manufacturer": "NVIDIA", "Name": "L4"}]}}, "nvidia"),
        ({"GpuInfo": {"Gpus": [{"Manufacturer": "nvidia"}]}}, "nvidia"),
        ({"GpuInfo": {"Gpus": [{"Manufacturer": "AMD", "Name": "Radeon Pro V520"}]}}, None),
        ({"GpuInfo": {"Gpus": ["bogus", {"Manufacturer": 7}, {}]}}, None),
        ({"GpuInfo": {"Gpus": "nope"}}, None),
        ({"GpuInfo": {}}, None),
        ({"GpuInfo": "nope"}, None),
        ({"NeuronInfo": {"NeuronDevices": [{"Name": "Inferentia2", "Count": 1}]}}, "neuron"),
        ({"NeuronInfo": {"NeuronDevices": []}}, None),
        ({"NeuronInfo": {"NeuronDevices": "x"}}, None),
        ({"NeuronInfo": "x"}, None),
        (
            {
                "GpuInfo": {"Gpus": [{"Manufacturer": "AMD"}]},
                "NeuronInfo": {"NeuronDevices": [{"Name": "Trainium"}]},
            },
            "neuron",
        ),
        ({"InstanceType": "m5.large"}, None),
    ],
)
def test_detect_accelerator_classifies_describe_instance_types_records(
    instance: dict[str, object], expected: str | None
) -> None:
    assert _detect_accelerator(instance) == expected


def test_instance_architectures_map_to_kubernetes_labels() -> None:
    instance = {"ProcessorInfo": {"SupportedArchitectures": ["x86_64", "arm64", "i386"]}}

    assert _instance_architectures(instance) == ("amd64", "arm64", "i386")
    with pytest.raises(CatalogError, match="ProcessorInfo must be a JSON/YAML object"):
        _instance_architectures({})
    with pytest.raises(CatalogError, match="SupportedArchitectures must be a list"):
        _instance_architectures({"ProcessorInfo": {}})


# ---------------------------------------------------------------------------
# Online discovery against a fake boto3 session
# ---------------------------------------------------------------------------


def _region(name: str | None, status: str) -> dict[str, object]:
    """One ``DescribeRegions.Regions[]`` record; ``name=None`` omits RegionName."""
    record: dict[str, object] = {"OptInStatus": status}
    if name is not None:
        record["RegionName"] = name
        record["Endpoint"] = f"ec2.{name}.amazonaws.com"
    return record


def _instance(
    instance_type: str,
    *,
    gpu: str | None = None,
    neuron: int = 0,
    architectures: tuple[str, ...] = ("x86_64",),
) -> dict[str, object]:
    record: dict[str, object] = {
        "InstanceType": instance_type,
        "ProcessorInfo": {"SupportedArchitectures": list(architectures)},
    }
    if gpu is not None:
        record["GpuInfo"] = {"Gpus": [{"Manufacturer": gpu, "Name": "GPU", "Count": 1}]}
    if neuron:
        record["NeuronInfo"] = {"NeuronDevices": [{"Name": "Inferentia2", "Count": neuron}]}
    return record


class _FakePaginator:
    def __init__(self, fixture: _FakeEC2, region_name: str) -> None:
        self._fixture = fixture
        self._region_name = region_name

    def paginate(self, **kwargs: object) -> list[object]:
        self._fixture.calls.append(("paginate", self._region_name, kwargs))
        return list(self._fixture.pages_by_region.get(self._region_name, []))


class _FakeEC2Client:
    def __init__(self, fixture: _FakeEC2, region_name: str, config: object) -> None:
        self._fixture = fixture
        self.region_name = region_name
        self.config = config

    def describe_regions(self, **kwargs: object) -> object:
        self._fixture.calls.append(("describe_regions", self.region_name, kwargs))
        return self._fixture.regions_response

    def get_paginator(self, operation_name: str) -> _FakePaginator:
        self._fixture.calls.append(("get_paginator", self.region_name, operation_name))
        return _FakePaginator(self._fixture, self.region_name)


@dataclass
class _FakeEC2:
    """In-memory stand-in for ``boto3.Session`` and its EC2 clients."""

    regions_response: object
    pages_by_region: dict[str, list[object]] = field(default_factory=dict)
    calls: list[tuple[object, ...]] = field(default_factory=list)
    sessions: list[dict[str, object]] = field(default_factory=list)
    clients: list[_FakeEC2Client] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fixture = self

        class _Session:
            def __init__(self, **kwargs: object) -> None:
                fixture.sessions.append(kwargs)

            def client(self, service_name: str, *, region_name: str, config: object) -> object:
                assert service_name == "ec2"
                client = _FakeEC2Client(fixture, region_name, config)
                fixture.clients.append(client)
                return client

        module = types.ModuleType("boto3")
        module.Session = _Session
        monkeypatch.setitem(sys.modules, "boto3", module)


def test_discovery_unions_accelerator_types_across_enabled_regions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enabled Regions are scanned sequentially, deduplicated, and merged per family."""
    fake = _FakeEC2(
        regions_response={
            "Regions": [
                _region("us-east-1", "opt-in-not-required"),
                _region("eu-west-1", "opted-in"),
                _region("ap-east-1", "not-opted-in"),
                _region(None, "opted-in"),
                _region("us-east-1", "opt-in-not-required"),
            ]
        },
        pages_by_region={
            "us-east-1": [
                {
                    "InstanceTypes": [
                        _instance("g5.xlarge", gpu="NVIDIA"),
                        _instance("m5.large"),
                        _instance("g4ad.xlarge", gpu="AMD"),
                    ]
                },
                {"InstanceTypes": [_instance("inf2.xlarge", neuron=1)]},
            ],
            "eu-west-1": [
                {
                    "InstanceTypes": [
                        _instance("g5.2xlarge", gpu="NVIDIA"),
                        _instance("g5g.xlarge", gpu="NVIDIA", architectures=("arm64",)),
                        _instance("g5.xlarge", gpu="NVIDIA"),
                    ]
                },
                {},
            ],
        },
    )
    fake.install(monkeypatch)

    discovery = discover_accelerator_catalog()

    assert discovery.regions == ("eu-west-1", "us-east-1")
    assert discovery.instance_types == ("g5.2xlarge", "g5.xlarge", "g5g.xlarge", "inf2.xlarge")
    assert discovery.families == {
        "g5": DiscoveredFamily(accelerator="nvidia", architectures=("amd64",)),
        "g5g": DiscoveredFamily(accelerator="nvidia", architectures=("arm64",)),
        "inf2": DiscoveredFamily(accelerator="neuron", architectures=("amd64",)),
    }
    assert discovery.to_mapping() == {
        "regions_checked": ["eu-west-1", "us-east-1"],
        "instance_types": ["g5.2xlarge", "g5.xlarge", "g5g.xlarge", "inf2.xlarge"],
        "families": {
            "g5": {"accelerator": "nvidia", "architectures": ["amd64"]},
            "g5g": {"accelerator": "nvidia", "architectures": ["arm64"]},
            "inf2": {"accelerator": "neuron", "architectures": ["amd64"]},
        },
    }

    assert fake.sessions == [{}]
    assert [client.region_name for client in fake.clients] == [
        "us-east-1",
        "eu-west-1",
        "us-east-1",
    ]
    config = fake.clients[0].config
    assert isinstance(config, Config)
    assert config.retries == {"mode": "adaptive", "total_max_attempts": 10}
    assert config.connect_timeout == 10
    assert config.read_timeout == 60
    assert config.user_agent_extra == "gco-accelerator-catalog/1"
    assert fake.calls[0] == ("describe_regions", "us-east-1", {"AllRegions": True})
    assert [call for call in fake.calls if call[0] == "paginate"] == [
        ("paginate", "eu-west-1", {"PaginationConfig": {"PageSize": 100}}),
        ("paginate", "us-east-1", {"PaginationConfig": {"PageSize": 100}}),
    ]


def test_discovery_uses_the_named_profile_and_home_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeEC2(
        regions_response={"Regions": [_region("eu-central-1", "opt-in-not-required")]},
        pages_by_region={
            "eu-central-1": [{"InstanceTypes": [_instance("g6.xlarge", gpu="NVIDIA")]}]
        },
    )
    fake.install(monkeypatch)

    discovery = discover_accelerator_catalog(profile="ops", home_region="eu-central-1")

    assert fake.sessions == [{"profile_name": "ops"}]
    assert fake.clients[0].region_name == "eu-central-1"
    assert discovery.instance_types == ("g6.xlarge",)


def test_discovery_rejects_a_non_list_regions_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeEC2(regions_response={"Regions": {"RegionName": "us-east-1"}}).install(monkeypatch)

    with pytest.raises(CatalogError, match="DescribeRegions response Regions must be a list"):
        discover_accelerator_catalog()


def test_discovery_requires_at_least_one_enabled_region(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeEC2(
        regions_response={"Regions": [_region("ap-east-1", "not-opted-in"), _region("x", "")]}
    ).install(monkeypatch)

    with pytest.raises(CatalogError, match="no enabled commercial Regions"):
        discover_accelerator_catalog()


def test_discovery_rejects_a_non_list_instance_types_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeEC2(
        regions_response={"Regions": [_region("us-east-1", "opt-in-not-required")]},
        pages_by_region={"us-east-1": [{"InstanceTypes": "oops"}]},
    ).install(monkeypatch)

    with pytest.raises(CatalogError, match="InstanceTypes must be a list in us-east-1"):
        discover_accelerator_catalog()


def test_discovery_rejects_conflicting_accelerator_classes_for_one_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeEC2(
        regions_response={"Regions": [_region("us-east-1", "opt-in-not-required")]},
        pages_by_region={
            "us-east-1": [
                {
                    "InstanceTypes": [
                        _instance("x9.xlarge", gpu="NVIDIA"),
                        _instance("x9.2xlarge", neuron=2),
                    ]
                }
            ]
        },
    ).install(monkeypatch)

    with pytest.raises(CatalogError, match="conflicting accelerator classes for family x9"):
        discover_accelerator_catalog()


# ---------------------------------------------------------------------------
# Drift comparison, rendering, and refresh
# ---------------------------------------------------------------------------


def test_compare_catalog_reports_added_removed_and_metadata_drift() -> None:
    catalog = _synthetic_catalog(
        {"g5": _policy("g5"), "inf2": _policy("inf2", accelerator="neuron")},
        ("g5.xlarge", "inf2.xlarge"),
    )
    discovery = Discovery(
        regions=("eu-west-1", "us-east-1", "us-west-2"),
        instance_types=("g5.xlarge", "g8.xlarge"),
        families={
            "inf2": DiscoveredFamily(accelerator="nvidia", architectures=("amd64",)),
            "g8": DiscoveredFamily(accelerator="nvidia", architectures=("amd64",)),
            "g5": DiscoveredFamily(accelerator="nvidia", architectures=("amd64", "arm64")),
        },
    )

    drift = compare_catalog(catalog, discovery)

    assert drift.added == ("g8.xlarge",)
    assert drift.removed == ("inf2.xlarge",)
    assert drift.metadata_changes == (
        "`g5` architectures changed: catalog=['amd64'], EC2=['amd64', 'arm64'].",
        "New family `g8` requires reviewed track, generation, and lifecycle policy "
        "(accelerator=nvidia, architectures=['amd64']).",
        "`inf2` accelerator changed: catalog=neuron, EC2=nvidia.",
    )
    assert drift.regions_checked == 3
    assert drift.count == 5
    assert drift.has_drift
    assert drift.summary_mapping() == {
        "status": "drift",
        "drift_count": 5,
        "added_count": 1,
        "removed_count": 1,
        "metadata_change_count": 3,
        "regions_checked": 3,
    }


def test_compare_catalog_is_current_when_ec2_matches() -> None:
    catalog = Catalog.load()
    drift = compare_catalog(catalog, _discovery_matching(catalog, regions=4))

    assert not drift.has_drift
    assert drift.count == 0
    assert drift.summary_mapping()["status"] == "current"
    markdown = drift.to_markdown()
    assert "**Status: CURRENT.** Checked 4 enabled commercial Regions" in markdown
    assert "| New instance types | 0 |" in markdown
    assert "matches the EC2 union" in markdown
    assert "###" not in markdown


def test_drift_markdown_lists_only_the_sections_with_changes() -> None:
    added_only = CatalogDrift(
        added=("g8.xlarge",), removed=(), metadata_changes=(), regions_checked=2
    )
    markdown = added_only.to_markdown()
    assert "**Status: ACTION REQUIRED.**" in markdown
    assert "### New EC2 instance types" in markdown
    assert "- `g8.xlarge`" in markdown
    assert "python scripts/accelerator_catalog.py refresh" in markdown
    assert "### Instance types no longer returned" not in markdown
    assert "### Family metadata changes" not in markdown

    removed_and_metadata = CatalogDrift(
        added=(),
        removed=("p3dn.24xlarge",),
        metadata_changes=("`p3dn` accelerator changed: catalog=nvidia, EC2=neuron.",),
        regions_checked=2,
    )
    markdown = removed_and_metadata.to_markdown()
    assert "### New EC2 instance types" not in markdown
    assert "### Instance types no longer returned" in markdown
    assert "- `p3dn.24xlarge`" in markdown
    assert "mark the family deprecated or end-of-life" in markdown
    assert "### Family metadata changes" in markdown
    assert "- `p3dn` accelerator changed: catalog=nvidia, EC2=neuron." in markdown


def test_refresh_refuses_unreviewed_families(tmp_path: Path) -> None:
    catalog = Catalog.load()
    output = tmp_path / "catalog.json"

    with pytest.raises(CatalogError, match="refusing to refresh with unreviewed families: g8"):
        refresh_catalog(catalog, _discovery_matching(catalog, "g8.xlarge"), output)
    assert not output.exists()


def test_refresh_refuses_family_metadata_drift(tmp_path: Path) -> None:
    catalog = Catalog.load()
    discovery = _discovery_matching(catalog)
    discovery.families["g5"] = DiscoveredFamily(accelerator="nvidia", architectures=("arm64",))
    output = tmp_path / "catalog.json"

    with pytest.raises(CatalogError, match="refusing to refresh while family metadata differs"):
        refresh_catalog(catalog, discovery, output)
    assert not output.exists()


def test_refresh_writes_live_types_with_a_fresh_utc_timestamp(tmp_path: Path) -> None:
    catalog = _synthetic_catalog({"g5": _policy("g5")}, ("g5.xlarge",))
    discovery = Discovery(
        regions=("us-east-1",),
        instance_types=("g5.2xlarge", "g5.xlarge"),
        families={"g5": DiscoveredFamily(accelerator="nvidia", architectures=("amd64",))},
    )
    output = tmp_path / "catalog.json"

    refresh_catalog(catalog, discovery, output)
    refreshed = Catalog.load(output)

    assert refreshed.instance_types == ("g5.2xlarge", "g5.xlarge")
    assert refreshed.families == catalog.families
    assert refreshed.source == catalog.source
    assert refreshed.last_refreshed_at != catalog.last_refreshed_at
    assert _utc_timestamp(refreshed.last_refreshed_at, "refreshed") == refreshed.last_refreshed_at
    assert output.read_text().endswith("\n")


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------


def _install_discovery(
    monkeypatch: pytest.MonkeyPatch, discovery: Discovery
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def _fake(*, profile: str | None, home_region: str) -> Discovery:
        calls.append({"profile": profile, "home_region": home_region})
        return discovery

    monkeypatch.setattr(accelerator_catalog, "discover_accelerator_catalog", _fake)
    return calls


def test_main_validate_prints_text_for_the_current_repository(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["validate"]) == 0

    captured = capsys.readouterr()
    assert captured.out == ValidationReport(()).to_text()
    assert captured.err == ""


def test_main_validate_writes_markdown_to_the_requested_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "report.md"

    assert main(["validate", "--format", "markdown", "--output", str(output)]) == 0

    assert output.read_text() == ValidationReport(()).to_markdown()
    assert capsys.readouterr().out == ""


def test_main_validate_returns_one_and_names_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog.load()
    cdk_path = _write_cdk(tmp_path / "cdk.json", list(catalog.instance_types[:-1]))

    assert main(["validate", "--cdk-config", str(cdk_path)]) == 1

    out = capsys.readouterr().out
    assert "Accelerator catalog validation failed with 1 finding(s):" in out
    assert "ERROR [watch-list-missing]" in out
    assert catalog.instance_types[-1] in out


def test_main_reports_catalog_errors_on_stderr_with_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.json"

    assert main(["validate", "--catalog", str(missing)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("accelerator catalog error: cannot read accelerator catalog")


def test_main_capture_prints_the_discovery_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog.load()
    discovery = _discovery_matching(catalog)
    calls = _install_discovery(monkeypatch, discovery)

    assert main(["capture", "--profile", "ops", "--home-region", "eu-west-1"]) == 0

    assert calls == [{"profile": "ops", "home_region": "eu-west-1"}]
    assert json.loads(capsys.readouterr().out) == discovery.to_mapping()


def test_main_check_online_reports_drift_with_report_and_json_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog.load()
    calls = _install_discovery(monkeypatch, _discovery_matching(catalog, "g8.xlarge"))
    report = tmp_path / "drift.md"

    assert main(["check-online", "--report", str(report), "--json-summary"]) == 1

    assert calls == [{"profile": None, "home_region": "us-east-1"}]
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "added_count": 1,
        "drift_count": 2,
        "metadata_change_count": 1,
        "regions_checked": 2,
        "removed_count": 0,
        "status": "drift",
    }
    markdown = report.read_text()
    assert "**Status: ACTION REQUIRED.**" in markdown
    assert "- `g8.xlarge`" in markdown
    assert "New family `g8`" in markdown


def test_main_check_online_prints_a_status_line_when_current(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog.load()
    _install_discovery(monkeypatch, _discovery_matching(catalog, regions=3))

    assert main(["check-online"]) == 0

    assert capsys.readouterr().out == (
        "accelerator catalog: status=current drift_count=0 regions_checked=3\n"
    )


def test_main_refresh_writes_the_requested_output_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog.load()
    discovery = _discovery_matching(catalog, regions=5)
    _install_discovery(monkeypatch, discovery)
    output = tmp_path / "refreshed.json"
    before = DEFAULT_CATALOG_PATH.read_text()

    assert main(["refresh", "--output", str(output)]) == 0

    assert capsys.readouterr().out == (
        f"Refreshed {output} with {len(catalog.instance_types)} instance types "
        "from 5 enabled Regions.\n"
    )
    refreshed = Catalog.load(output)
    assert refreshed.instance_types == catalog.instance_types
    assert refreshed.families == catalog.families
    assert DEFAULT_CATALOG_PATH.read_text() == before


def test_main_refresh_failure_is_reported_as_a_catalog_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog.load()
    _install_discovery(monkeypatch, _discovery_matching(catalog, "g8.xlarge"))
    output = tmp_path / "refreshed.json"

    assert main(["refresh", "--output", str(output)]) == 2

    assert "refusing to refresh with unreviewed families: g8" in capsys.readouterr().err
    assert not output.exists()


def test_main_rejects_a_subcommand_without_a_handler(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A parser entry that gains no handler fails loudly instead of returning success."""

    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command", required=True)
        orphan = subparsers.add_parser("orphan")
        orphan.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG_PATH)
        orphan.set_defaults(profile=None, home_region="us-east-1")
        return parser

    monkeypatch.setattr(accelerator_catalog, "_build_parser", _parser)
    _install_discovery(monkeypatch, _discovery_matching(Catalog.load()))

    assert main(["orphan"]) == 2

    assert capsys.readouterr().err == "accelerator catalog error: unsupported command: orphan\n"


def test_main_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
