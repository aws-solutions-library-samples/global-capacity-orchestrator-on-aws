"""Spot Placement Score instance-pool policy tests.

The pools in ``scripts/accelerator_catalog.py`` exist because AWS documents
that ``GetSpotPlacementScores`` needs at least three instance types to return
meaningful scores; a pool that shrinks below three members silently
reintroduces the depressed-score bug. These tests pin the shipped pool data
and the validator that guards it, alongside the NodePool and watch-list
checks in ``tests/test_accelerator_catalog.py``.
"""

from __future__ import annotations

import json

import pytest

from scripts.accelerator_catalog import (
    INSTANCE_POOLS,
    ROOT,
    UNPOOLED_INSTANCE_TYPES,
    Catalog,
    CatalogError,
    InstancePool,
    ValidationReport,
    pool_for_instance_type,
    validate_instance_pools,
    validate_repository,
)


def _watched_partition() -> tuple[Catalog, set[str], set[str]]:
    catalog = Catalog.load()
    pooled = {member for pool in INSTANCE_POOLS for member in pool.members}
    return catalog, pooled, set(UNPOOLED_INSTANCE_TYPES)


def test_shipped_pools_pass_validation() -> None:
    """The checked-in pool catalog validates cleanly against the real repository."""
    findings = validate_instance_pools(Catalog.load())
    assert findings == (), ValidationReport(findings).to_text()


def test_every_pool_has_at_least_three_distinct_members() -> None:
    """No shipped pool may shrink below the documented three-type minimum."""
    for pool in INSTANCE_POOLS:
        assert len(set(pool.members)) >= 3, (
            f"pool {pool.name} has fewer than three distinct members; "
            "GetSpotPlacementScores would return depressed scores"
        )


def test_every_pool_member_is_in_the_cdk_watch_list() -> None:
    """The subset rule holds directly against cdk.json, independent of the validator."""
    cdk = json.loads((ROOT / "cdk.json").read_text(encoding="utf-8"))
    watched = set(cdk["context"]["historical"]["watch_instance_types"])
    for pool in INSTANCE_POOLS:
        outside = sorted(set(pool.members) - watched)
        assert not outside, f"pool {pool.name} members missing from watch list: {outside}"


def test_pool_below_three_members_is_reported_by_name() -> None:
    """A two-member pool raises a finding that names the offending pool."""
    catalog = Catalog.load()
    pools = (InstancePool(name="tiny", members=("g5.xlarge", "g6.xlarge")),)

    findings = validate_instance_pools(catalog, pools, tuple(catalog.instance_types))
    finding = next(item for item in findings if item.code == "instance-pool-too-small")
    output = ValidationReport((finding,)).to_text()

    assert "tiny" in finding.title
    assert "2 distinct member type(s)" in output
    assert "at least three instance types" in output


def test_duplicate_members_do_not_satisfy_the_minimum() -> None:
    """Listing one type twice cannot smuggle a pool past the three-type rule."""
    catalog = Catalog.load()
    pools = (InstancePool(name="padded", members=("g5.xlarge", "g5.xlarge", "g6.xlarge")),)

    codes = {
        finding.code
        for finding in validate_instance_pools(catalog, pools, tuple(catalog.instance_types))
    }

    assert "instance-pool-duplicate-member" in codes
    assert "instance-pool-too-small" in codes


def test_member_outside_the_watch_list_is_reported() -> None:
    """Pool members must be observed capacity: unknown types are named findings."""
    catalog = Catalog.load()
    pools = (InstancePool(name="ghostly", members=("g5.xlarge", "g6.xlarge", "z9.mega")),)

    findings = validate_instance_pools(catalog, pools, tuple(catalog.instance_types))
    finding = next(item for item in findings if item.code == "instance-pool-unknown-member")
    output = ValidationReport((finding,)).to_text()

    assert "ghostly" in finding.title
    assert "z9.mega" in output


def test_overlapping_pools_are_permitted() -> None:
    """A type may belong to several pools without raising any finding."""
    catalog = Catalog.load()
    overlapping = (
        InstancePool(name="first", members=("g5.xlarge", "g5.2xlarge", "g5.4xlarge")),
        InstancePool(name="second", members=("g5.xlarge", "g6.xlarge", "g6.2xlarge")),
    )
    covered = {member for pool in overlapping for member in pool.members}
    rest = tuple(item for item in catalog.instance_types if item not in covered)

    assert validate_instance_pools(catalog, overlapping, rest) == ()


def test_first_pool_in_definition_order_wins_attribution() -> None:
    """Snapshot attribution is deterministic: definition order breaks overlaps."""
    overlapping = (
        InstancePool(name="first", members=("g5.xlarge", "g5.2xlarge", "g5.4xlarge")),
        InstancePool(name="second", members=("g5.xlarge", "g6.xlarge", "g6.2xlarge")),
    )

    winner = pool_for_instance_type("g5.xlarge", overlapping)

    assert winner is not None and winner.name == "first"
    assert pool_for_instance_type("g6.xlarge", overlapping) is not None
    assert pool_for_instance_type("p4d.24xlarge", overlapping) is None


def test_every_watched_type_has_a_reviewed_pooling_decision() -> None:
    """Watched types are either pooled or explicitly declared unpooled, never neither."""
    catalog, pooled, declared_unpooled = _watched_partition()
    watched = set(catalog.instance_types)

    assert watched == (pooled | declared_unpooled)
    assert not (pooled & declared_unpooled)


def test_uncovered_watch_type_is_reported() -> None:
    """Dropping the pools entirely names every type lacking a pooling decision."""
    catalog = Catalog.load()

    findings = validate_instance_pools(catalog, (), ())
    finding = next(item for item in findings if item.code == "instance-pool-uncovered-type")

    assert f"{len(catalog.instance_types)} watched type(s)" in finding.detail
    assert "UNPOOLED_INSTANCE_TYPES" in finding.recommendation


def test_stale_unpooled_declaration_is_reported() -> None:
    """A pooled type left in UNPOOLED_INSTANCE_TYPES is flagged as stale."""
    catalog = Catalog.load()

    findings = validate_instance_pools(
        catalog, INSTANCE_POOLS, (*UNPOOLED_INSTANCE_TYPES, "g5.xlarge")
    )
    finding = next(item for item in findings if item.code == "instance-pool-stale-unpooled")

    assert "g5.xlarge" in finding.detail


def test_pools_never_mix_cpu_architectures() -> None:
    """No pool mixes arm64 and x86_64 members; images are not portable across them."""
    catalog = Catalog.load()
    for pool in INSTANCE_POOLS:
        architectures = {
            architecture
            for member in pool.members
            for architecture in catalog.families[member.split(".", 1)[0]].architectures
        }
        assert len(architectures) == 1, (
            f"pool {pool.name} mixes CPU architectures {sorted(architectures)}"
        )


def test_validate_repository_reports_pool_findings() -> None:
    """Pool policy runs inside the catalog's standard validate path."""
    report = validate_repository(
        pools=(InstancePool(name="tiny", members=("g5.xlarge", "g6.xlarge")),),
        unpooled_instance_types=tuple(Catalog.load().instance_types),
    )

    assert not report.ok
    assert any(finding.code == "instance-pool-too-small" for finding in report.findings)


def test_empty_pool_is_rejected_at_construction() -> None:
    """A pool with no members is malformed input, not a finding."""
    with pytest.raises(CatalogError, match="has no members"):
        InstancePool(name="empty", members=())
