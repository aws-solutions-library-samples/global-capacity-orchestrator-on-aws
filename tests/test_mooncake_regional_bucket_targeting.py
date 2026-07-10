"""
Property-based test — a regional upload only ever touches its own region.

``RegionalBucketManager.upload`` takes a target region and writes local files
to that region's general-purpose bucket, named
``gco-regional-shared-<account>-<region>``. The bucket name is never built from
the account/region in code; it is read back from the *target region's own*
``/gco/regional-shared-bucket/name`` parameter so the manager can only ever
write where that region's stack published its bucket.

This test pins that targeting down. For an arbitrary region — set up alongside
several decoy regions (and a distinct "global" region), each advertising its
own distinctly-named bucket — an upload SHALL:

* read ``/gco/regional-shared-bucket/name`` from the target region and from no
  other region (never the global region's, never a decoy's), and
* put every object into the target region's
  ``gco-regional-shared-<account>-<region>`` bucket through an S3 client scoped
  to that same region — never into any decoy region's bucket.

The parameter store and S3 are both faked in-process: a small registry maps
each region to its own bucket name, so a manager that resolved the wrong
region would read a different bucket name and the write-target assertions would
trip.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from hypothesis import given, settings
from hypothesis import strategies as st

from cli.models import RegionalBucketManager
from gco.stacks.constants import (
    regional_shared_bucket_name_prefix,
    regional_shared_ssm_parameter_prefix,
)

# Physical-name prefixes derived from project_name (#139). This CLI test drives
# RegionalBucketManager with a config whose project_name is "gco", so scope the
# expected prefixes to "gco".
_PROJECT_NAME = "gco"
REGIONAL_SHARED_BUCKET_NAME_PREFIX = regional_shared_bucket_name_prefix(_PROJECT_NAME)
REGIONAL_SHARED_SSM_PARAMETER_PREFIX = regional_shared_ssm_parameter_prefix(_PROJECT_NAME)

_ACCOUNT = "123456789012"

# A distinct region the config treats as "global". An upload must never read or
# write through this region even though it, too, advertises a regional bucket.
_GLOBAL_REGION = "us-gov-1"

# Decoy regions that each publish their own bucket. None of them should ever be
# read or written by an upload aimed at a different target region.
_DECOY_REGIONS = ("eu-central-9", "ap-south-7", "sa-east-5")

# Region-like strings: two-letter area, a word, and a single-digit index. Kept
# in the realistic shape AWS uses while staying clear of the fixed decoy and
# global regions above.
_TARGET_REGIONS = st.from_regex(r"[a-z]{2}-[a-z]{4,9}-[1-9]", fullmatch=True).filter(
    lambda r: r not in _DECOY_REGIONS and r != _GLOBAL_REGION
)


def _bucket_for(region: str) -> str:
    """The canonical regional bucket name a region's stack would publish."""
    return f"{REGIONAL_SHARED_BUCKET_NAME_PREFIX}-{_ACCOUNT}-{region}"


@settings(max_examples=60, deadline=None)
@given(target_region=_TARGET_REGIONS, file_count=st.integers(min_value=1, max_value=5))
def test_upload_resolves_and_writes_only_the_target_region(
    target_region: str, file_count: int
) -> None:
    """An upload reads only the target region's bucket parameter and writes
    every object into only that region's bucket."""

    # Every known region advertises its own distinctly-named bucket, so a wrong
    # resolution would surface a different bucket name.
    registry = {region: _bucket_for(region) for region in _DECOY_REGIONS}
    registry[_GLOBAL_REGION] = _bucket_for(_GLOBAL_REGION)
    registry[target_region] = _bucket_for(target_region)

    # Records of which (parameter-name, region) pairs were resolved and which
    # (region, bucket, key) objects were written.
    ssm_lookups: list[tuple[str, str | None]] = []
    writes: list[tuple[str | None, str, str]] = []

    def fake_get_ssm_parameter_optional(name: str, *, region: str | None = None) -> str | None:
        ssm_lookups.append((name, region))
        return registry.get(region)

    def make_s3_client(region: str | None) -> MagicMock:
        client = MagicMock()

        def upload_file(filename: str, bucket: str, key: str) -> None:
            writes.append((region, bucket, key))

        client.upload_file.side_effect = upload_file
        return client

    def fake_boto_client(service: str, region_name: str | None = None, **_: object) -> MagicMock:
        assert service == "s3", f"unexpected client requested: {service!r}"
        return make_s3_client(region_name)

    config = MagicMock()
    config.global_region = _GLOBAL_REGION
    config.project_name = "gco"

    with TemporaryDirectory() as tmp:
        source = Path(tmp) / "payload"
        source.mkdir()
        for i in range(file_count):
            (source / f"object_{i}.bin").write_text(f"contents-{i}")

        with (
            patch("cli.models.get_config", return_value=config),
            patch(
                "gco.services.aws_ssm.get_ssm_parameter_optional",
                side_effect=fake_get_ssm_parameter_optional,
            ),
            patch("cli.models.boto3.client", side_effect=fake_boto_client),
        ):
            manager = RegionalBucketManager(config=config)
            result = manager.upload(str(source), target_region)

    expected_bucket = _bucket_for(target_region)
    expected_param = f"{REGIONAL_SHARED_SSM_PARAMETER_PREFIX}/name"

    # The bucket was resolved from the target region's own /name parameter, and
    # from no other region.
    assert ssm_lookups, "the manager never resolved a bucket parameter"
    assert all(name == expected_param for name, _ in ssm_lookups), (
        f"resolved unexpected parameter(s): {ssm_lookups}"
    )
    assert {region for _, region in ssm_lookups} == {target_region}, (
        f"target region {target_region!r} but parameters were read from "
        f"{[region for _, region in ssm_lookups]}"
    )

    # The result reports the target region and its bucket.
    assert result["region"] == target_region
    assert result["bucket"] == expected_bucket
    assert result["files_uploaded"] == file_count

    # Every object was written into the target region's bucket, through an S3
    # client scoped to the target region — never a decoy or the global region.
    assert len(writes) == file_count
    for write_region, bucket, _key in writes:
        assert bucket == expected_bucket, (
            f"object written to {bucket!r}, expected {expected_bucket!r}"
        )
        assert write_region == target_region, (
            f"object written through region {write_region!r}, expected {target_region!r}"
        )

    written_buckets = {bucket for _, bucket, _ in writes}
    decoy_buckets = {_bucket_for(r) for r in (*_DECOY_REGIONS, _GLOBAL_REGION)}
    assert written_buckets.isdisjoint(decoy_buckets), (
        f"upload leaked into non-target buckets: {written_buckets & decoy_buckets}"
    )
