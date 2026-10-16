"""Lambda code assets packaged from the ``lambda/`` source directories.

A ``lambda/<name>`` source directory is zipped as it sits in the checkout, so
the caches a local test or type-check run leaves in it would ship with the
function and change its asset hash (see ``LAMBDA_SOURCE_ASSET_EXCLUDE``). The
service images and the TLS certificate manager image keep the same caches out
through their ``.dockerignore`` files. The CLI-built ``*-build`` directories
are packaged with ``lambda_.Code.from_asset`` directly: the bytecode
``pip install`` compiles there is part of the build, and the CLI already copies
no source caches into it.
"""

from __future__ import annotations

from aws_cdk import aws_lambda as lambda_

from gco.stacks.constants import LAMBDA_SOURCE_ASSET_EXCLUDE


def lambda_source_code(directory: str) -> lambda_.AssetCode:
    """Package the source directory ``directory`` (``lambda/<name>``) without local caches."""
    return lambda_.Code.from_asset(directory, exclude=list(LAMBDA_SOURCE_ASSET_EXCLUDE))
