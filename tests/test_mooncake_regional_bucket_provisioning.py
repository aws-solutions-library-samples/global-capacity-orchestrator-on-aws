"""
Property-based test — the general-purpose regional bucket is always-on.

The regional stack provisions one general-purpose S3 bucket named
``gco-regional-shared-<account>-<region>`` per region together with its three
discovery parameters under ``/gco/regional-shared-bucket`` (``/name``, ``/arn``,
``/region``). That bucket is unconditional: there is no ``cdk.json`` context
key and no feature flag whose value can remove it, and its existence does not
depend on whether any inference endpoint opts into cold-tier KV storage.

This test pins that guarantee down. For an arbitrary set of regions, and for
arbitrary combinations of context toggles — including made-up keys named as if
they could gate the bucket (``regional_shared_bucket``, ``cold_tier``,
``mooncake``) plus the real ``queue_processor`` toggle and the FSx switch — the
synthesized regional template SHALL contain exactly one bucket whose name
carries the ``gco-regional-shared-`` prefix and exactly three discovery
parameters under the ``/gco/regional-shared-bucket`` namespace. No toggle
combination can drive either count to zero.

The synth fixture reuses the Docker + helm-installer patching pattern from
``tests/test_regional_stack.py`` so the hot loop needs no Docker daemon, and a
small cache keyed by ``(region, fsx, toggles)`` keeps repeated synths of the
same configuration out of each example's budget.
"""

from __future__ import annotations

from functools import cache
from typing import Any
from unittest.mock import MagicMock, patch

import aws_cdk as cdk
from aws_cdk import assertions
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gco.stacks.constants import (
    REGIONAL_SHARED_BUCKET_NAME_PREFIX,
    REGIONAL_SHARED_SSM_PARAMETER_PREFIX,
)
from gco.stacks.regional_stack import GCORegionalStack

# Reuse the battle-tested MockConfigLoader + helm-installer patch pattern from
# tests/test_regional_stack.py rather than re-implementing a synth fixture. We
# import MockConfigLoader by name and pull the helm-installer mock staticmethod
# off a module-level reference; we do NOT import the synthesis fixture class
# under its canonical name, because pytest would otherwise re-collect it here.
from tests.test_regional_stack import MockConfigLoader
from tests.test_regional_stack import TestRegionalStackSynthesis as _RegionalStackSynthesisFixtures

_ACCOUNT = "123456789012"

# Regions the regional_stack fixture is known to synthesize under
# MockConfigLoader.
_CANDIDATE_REGIONS = [
    "us-east-1",
    "us-west-2",
    "eu-west-1",
    "ap-southeast-1",
]

# Context keys fed to the App to probe toggle-independence. ``queue_processor``
# is a real switch the stack consults; the remaining keys are deliberately
# named as if they might gate the regional bucket or react to a cold-tier
# choice, but the stack never reads them — which is the whole point: the
# bucket's existence cannot be suppressed by any context value.
_TOGGLE_KEYS = (
    "queue_processor",
    "regional_shared_bucket",
    "cold_tier",
    "mooncake",
)


class _RegionalMockConfig(MockConfigLoader):
    """MockConfigLoader whose region-sensitive methods return a chosen region.

    The base hard-codes ``"us-east-1"``; we override only the two methods the
    regional stack consults at synth time so the fixture keeps working for
    every region we sample.
    """

    def __init__(self, app: cdk.App | None, region: str, fsx_enabled: bool) -> None:
        super().__init__(app, fsx_enabled=fsx_enabled)
        self._region = region

    def get_regions(self) -> list[str]:
        return [self._region]

    def get_cluster_config(self, region: str) -> Any:
        from gco.models import ClusterConfig

        return ClusterConfig(
            region=region,
            cluster_name=f"gco-test-{region}",
            kubernetes_version="1.36",
            addons=["metrics-server"],
            resource_thresholds=self.get_resource_thresholds(),
        )


def _synth_regional_template(
    region: str,
    fsx_enabled: bool,
    toggles: tuple[tuple[str, bool], ...],
    logical_name: str,
) -> assertions.Template:
    """Synthesize a regional stack for the given region + toggle combination.

    Mirrors the Docker + helm-installer patching from
    ``tests/test_regional_stack.py`` so no real Docker daemon is needed.
    """
    context: dict[str, Any] = {key: {"enabled": value} for key, value in toggles}
    app = cdk.App(context=context)
    config = _RegionalMockConfig(app, region=region, fsx_enabled=fsx_enabled)

    with (
        patch("gco.stacks.regional_stack.ecr_assets.DockerImageAsset") as mock_docker,
        patch.object(
            GCORegionalStack,
            "_create_helm_installer_lambda",
            _RegionalStackSynthesisFixtures._mock_helm_installer,
        ),
    ):
        mock_image = MagicMock()
        mock_image.image_uri = f"{_ACCOUNT}.dkr.ecr.{region}.amazonaws.com/test:latest"
        mock_docker.return_value = mock_image

        stack = GCORegionalStack(
            app,
            logical_name,
            config=config,
            region=region,
            auth_secret_arn=f"arn:aws:secretsmanager:{region}:{_ACCOUNT}:secret:test-secret",  # nosec B106
            env=cdk.Environment(account=_ACCOUNT, region=region),
        )
        return assertions.Template.from_stack(stack)


@cache
def _regional_shared_surface(
    region: str,
    fsx_enabled: bool,
    toggles: tuple[tuple[str, bool], ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the regional bucket names and parameter names for a config.

    The result is ``(bucket_names, parameter_names)`` where each element is a
    sorted tuple. Synth output is a pure function of the inputs, so caching
    keeps repeated synths of the same ``(region, fsx, toggles)`` out of the
    per-example budget. Returned as tuples so the cache entry is hashable.
    """
    template = _synth_regional_template(
        region=region,
        fsx_enabled=fsx_enabled,
        toggles=toggles,
        logical_name=f"mc75-{region}-{int(fsx_enabled)}-{abs(hash(toggles)) % 100000}",
    )
    resources = template.to_json().get("Resources", {})

    bucket_names: list[str] = []
    parameter_names: list[str] = []
    for res in resources.values():
        rtype = res.get("Type")
        props = res.get("Properties", {})
        if rtype == "AWS::S3::Bucket":
            name = props.get("BucketName")
            if isinstance(name, str) and name.startswith(f"{REGIONAL_SHARED_BUCKET_NAME_PREFIX}-"):
                bucket_names.append(name)
        elif rtype == "AWS::SSM::Parameter":
            name = props.get("Name")
            if isinstance(name, str) and name.startswith(REGIONAL_SHARED_SSM_PARAMETER_PREFIX):
                parameter_names.append(name)

    return tuple(sorted(bucket_names)), tuple(sorted(parameter_names))


class TestRegionalSharedBucketAlwaysProvisioned:
    """The general-purpose regional bucket and its discovery parameters are
    synthesized for every region under every toggle combination — no context
    value can remove them.
    """

    @settings(
        max_examples=24,
        deadline=None,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
        ],
    )
    @given(
        regions=st.lists(
            st.sampled_from(_CANDIDATE_REGIONS),
            min_size=1,
            max_size=2,
            unique=True,
        ),
        fsx_enabled=st.booleans(),
        toggle_values=st.lists(
            st.booleans(), min_size=len(_TOGGLE_KEYS), max_size=len(_TOGGLE_KEYS)
        ),
    )
    def test_bucket_and_parameters_present_for_every_region(
        self,
        regions: list[str],
        fsx_enabled: bool,
        toggle_values: list[bool],
    ) -> None:
        """Each region's template carries exactly one ``gco-regional-shared-``
        bucket and exactly three discovery parameters, regardless of toggles.
        """
        toggles = tuple(zip(_TOGGLE_KEYS, toggle_values, strict=False))

        for region in regions:
            bucket_names, parameter_names = _regional_shared_surface(region, fsx_enabled, toggles)

            assert len(bucket_names) == 1, (
                f"Region={region!r}, fsx={fsx_enabled}, toggles={dict(toggles)}: "
                f"expected exactly one general-purpose regional bucket, found "
                f"{len(bucket_names)}: {list(bucket_names)}"
            )
            assert bucket_names[0] == (
                f"{REGIONAL_SHARED_BUCKET_NAME_PREFIX}-{_ACCOUNT}-{region}"
            ), (
                f"Region={region!r}: the regional bucket name must embed the "
                f"account and region. Got {bucket_names[0]!r}"
            )

            expected_params = {
                f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/name",
                f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/arn",
                f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/region",
            }
            assert set(parameter_names) == expected_params, (
                f"Region={region!r}, fsx={fsx_enabled}, toggles={dict(toggles)}: "
                f"expected exactly the three regional-bucket discovery "
                f"parameters {sorted(expected_params)}, got {list(parameter_names)}"
            )
