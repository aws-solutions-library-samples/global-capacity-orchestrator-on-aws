"""
ECR image URI helpers backed by local AWS partition metadata.

Lives in its own small module so both ``cli.images`` (which builds and
manages images) and ``cli.inference`` (which has to rewrite URIs to
target the local region of each deployed endpoint) can depend on it
without forming an import cycle.

Static-analysis tools (CodeQL, pyright) flag deferred-import cycles
even when both imports happen inside method bodies, because the
resulting module-level dependency graph still has a cycle. Splitting
the helper out keeps the dependency graph a DAG: ``cli.images`` and
``cli.inference`` both depend on ``cli._image_uri``, and neither
depends on the other. Partition and DNS suffix resolution uses
botocore's bundled endpoint metadata and makes no AWS API calls.
"""

from __future__ import annotations

import re
from functools import cache

import botocore.session

# ECR registry host shape in any AWS partition:
#   <account-id>.dkr.ecr.<region>.<partition-url-suffix>
_ECR_HOST_RE = re.compile(
    r"^(?P<account>\d+)\.dkr\.ecr\."
    r"(?P<region>[a-z]{2,4}(?:-[a-z0-9]+)+-[0-9]+)\."
    r"(?P<suffix>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)$"
)


@cache
def _partition_metadata(region: str) -> tuple[str, str]:
    """Return ``(partition, URL suffix)`` from botocore's local metadata."""
    if not region:
        raise ValueError("AWS region must not be empty")

    resolver = botocore.session.get_session().get_component("endpoint_resolver")
    partition = resolver.get_partition_for_region(region)
    if not partition:
        raise ValueError(f"Could not resolve an AWS partition for region {region!r}")
    url_suffix = resolver.get_partition_dns_suffix(partition)
    if not url_suffix:
        raise ValueError(f"Could not resolve the URL suffix for AWS partition {partition!r}")
    return str(partition), str(url_suffix)


def aws_partition(region: str) -> str:
    """Return the ARN partition for ``region`` using botocore metadata."""
    return _partition_metadata(region)[0]


def aws_url_suffix(region: str) -> str:
    """Return the AWS DNS suffix for ``region`` using botocore metadata."""
    return _partition_metadata(region)[1]


def ecr_registry_host(account_id: str, region: str) -> str:
    """Return the partition-correct private ECR registry hostname."""
    return f"{account_id}.dkr.ecr.{region}.{aws_url_suffix(region)}"


def rewrite_image_uri_for_region(uri: str, region: str) -> str:
    """Rewrite an ECR image URI to target a specific region's replica.

    Pure helper — no AWS calls. Detects ECR URIs by matching the
    ``<account>.dkr.ecr.<region>.<partition-url-suffix>`` host shape and
    validating that suffix against botocore's bundled endpoint metadata.
    It then replaces both the region and suffix for the target region, so
    same-partition and cross-partition rewrites cannot retain a stale DNS
    suffix. Non-ECR refs (Docker Hub, GHCR, etc.) are returned unchanged.

    Args:
        uri: The image URI (with optional ``host/path:tag`` shape).
        region: Target AWS region for the rewrite.

    Returns:
        The rewritten URI when the input is an ECR URI; otherwise the
        original input.
    """
    if "://" in uri:
        # Not a bare image ref (looks like a URL with a scheme).
        return uri
    parts = uri.split("/", 1)
    host = parts[0]
    match = _ECR_HOST_RE.match(host)
    if match is None:
        return uri

    source_region = match.group("region")
    if match.group("suffix") != aws_url_suffix(source_region):
        # The hostname has ECR-like labels but is not an AWS ECR endpoint.
        return uri

    new_host = ecr_registry_host(match.group("account"), region)
    if len(parts) > 1:
        return f"{new_host}/{parts[1]}"
    return new_host
