"""Fail-closed AWS inventory collection and comparison helpers.

Split by concern so each module stays reviewable:

* ``_shared`` — ownership-matching primitives and the category lists
* ``stacks`` — CloudFormation discovery, description, fingerprinting
* ``ecr`` — repository, image, and manifest inventory
* ``scanners`` — one read-only ``_list_*`` helper per AWS service
* ``project`` — fan-out collection, baselines, and absence proofs

Import the public helpers from this package rather than from a submodule, so
internal regrouping stays a private detail.
"""

from .ecr import collect_ecr_inventory, describe_ecr_image_by_tag
from .project import (
    capture_baseline,
    collect_project_resources,
    compare_baseline,
    project_resources_are_absent,
    summarize_project_resources,
)
from .stacks import (
    collect_project_stacks,
    collect_stack_inventory,
    describe_stack,
    describe_stack_fingerprint,
    discover_enabled_regions,
    list_active_stacks,
)

__all__ = [
    "capture_baseline",
    "collect_ecr_inventory",
    "collect_project_resources",
    "collect_project_stacks",
    "collect_stack_inventory",
    "compare_baseline",
    "describe_ecr_image_by_tag",
    "describe_stack",
    "describe_stack_fingerprint",
    "discover_enabled_regions",
    "list_active_stacks",
    "project_resources_are_absent",
    "summarize_project_resources",
]
