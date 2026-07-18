"""
Tests for ``_augment_trusted_registries_with_project_ecr``.

This pure helper extends the operator-configured ``trusted_registries``
list with the project's own per-account ECR registry hostnames so jobs
built via ``gco images build`` aren't rejected by either of the two
submission-path validators (queue_processor on SQS, manifest_processor
on REST). The augmentation is order-stable and idempotent — re-running
synthesis must produce a deterministic ConfigMap so deploys don't
churn between runs that have no real config change.
"""

from __future__ import annotations

import pytest

from gco.stacks.regional_stack import _augment_trusted_registries_with_project_ecr


class TestProjectEcrAugmentation:
    """End-to-end behaviour of the augmentation helper."""

    def test_appends_global_then_each_regional_host(self) -> None:
        out = _augment_trusted_registries_with_project_ecr(
            ["docker.io", "public.ecr.aws"],
            account="123456789012",
            regions=["us-east-1", "eu-west-1"],
            global_region="us-east-2",
            url_suffix="amazonaws.com",
        )
        # Operator's entries come first, then the project ECRs starting
        # with the global region (where ``gco-global`` provisions the
        # source repo), then each deployed region in declaration order.
        assert out == [
            "docker.io",
            "public.ecr.aws",
            "123456789012.dkr.ecr.us-east-2.amazonaws.com",
            "123456789012.dkr.ecr.us-east-1.amazonaws.com",
            "123456789012.dkr.ecr.eu-west-1.amazonaws.com",
        ]

    def test_global_region_in_regions_list_only_appears_once(self) -> None:
        """When global_region is also a deployed region (single-region
        deploys are the common dev case), the helper must not emit the
        ECR host twice — that would bloat the rendered ConfigMap and
        force pointless rolling restarts on every deploy.
        """
        out = _augment_trusted_registries_with_project_ecr(
            [],
            account="123456789012",
            regions=["us-east-2"],
            global_region="us-east-2",
            url_suffix="amazonaws.com",
        )
        assert out == ["123456789012.dkr.ecr.us-east-2.amazonaws.com"]

    def test_existing_project_ecr_in_base_is_preserved_verbatim(self) -> None:
        """If the operator already added the project ECR to cdk.json by
        hand, the helper must not duplicate the host or reorder the
        operator's entries.
        """
        out = _augment_trusted_registries_with_project_ecr(
            ["docker.io", "123456789012.dkr.ecr.us-east-2.amazonaws.com"],
            account="123456789012",
            regions=["us-east-2"],
            global_region="us-east-2",
            url_suffix="amazonaws.com",
        )
        # Order preserved, no duplicate ECR host emitted.
        assert out == [
            "docker.io",
            "123456789012.dkr.ecr.us-east-2.amazonaws.com",
        ]

    def test_empty_account_skips_augmentation(self) -> None:
        """Synthesis sometimes runs without a resolved account ID (e.g.
        unit tests against a token environment). When the account is
        empty the helper must return the operator's list verbatim
        rather than emitting a malformed ``.dkr.ecr.<region>...`` host.
        """
        out = _augment_trusted_registries_with_project_ecr(
            ["docker.io"],
            account="",
            regions=["us-east-1"],
            global_region="us-east-2",
            url_suffix="amazonaws.com",
        )
        assert out == ["docker.io"]

    def test_empty_regions_still_emits_global_region_host(self) -> None:
        """Even with zero regional stacks deployed, the ``gco-global``
        stack provisions the source ECR repo in the global region, so
        the helper still emits that one host.
        """
        out = _augment_trusted_registries_with_project_ecr(
            ["docker.io"],
            account="123456789012",
            regions=[],
            global_region="us-east-2",
            url_suffix="amazonaws.com",
        )
        assert out == [
            "docker.io",
            "123456789012.dkr.ecr.us-east-2.amazonaws.com",
        ]

    def test_idempotent_on_repeat(self) -> None:
        """Running the helper on its own output is a no-op — important
        because some test harnesses re-augment on every synth pass.
        """
        first = _augment_trusted_registries_with_project_ecr(
            ["docker.io"],
            account="123456789012",
            regions=["us-east-1"],
            global_region="us-east-2",
            url_suffix="amazonaws.com",
        )
        second = _augment_trusted_registries_with_project_ecr(
            first,
            account="123456789012",
            regions=["us-east-1"],
            global_region="us-east-2",
            url_suffix="amazonaws.com",
        )
        assert first == second

    @pytest.mark.parametrize(
        "regions",
        [
            ["us-east-1", "us-west-2", "eu-west-1"],
            ["us-east-2"],
            ["ap-southeast-1", "ap-northeast-1"],
        ],
    )
    def test_every_region_gets_its_own_ecr_host(self, regions: list[str]) -> None:
        out = _augment_trusted_registries_with_project_ecr(
            [],
            account="999999999999",
            regions=regions,
            global_region="us-east-2",
            url_suffix="amazonaws.com",
        )
        for region in regions:
            assert f"999999999999.dkr.ecr.{region}.amazonaws.com" in out
        # Global region always present.
        assert "999999999999.dkr.ecr.us-east-2.amazonaws.com" in out

    def test_does_not_mutate_input_list(self) -> None:
        base = ["docker.io"]
        _ = _augment_trusted_registries_with_project_ecr(
            base,
            account="123456789012",
            regions=["us-east-1"],
            global_region="us-east-2",
            url_suffix="amazonaws.com",
        )
        # Caller's list must be untouched.
        assert base == ["docker.io"]
