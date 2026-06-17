#!/usr/bin/env python3
"""Mirror the project's third-party container images into its own ECR.

Thin CLI wrapper around :mod:`cli._image_mirror`, which holds the shared core
used by both this script and the auto-mirror step in ``gco stacks deploy``
(``cli/stacks.py``). See that module's docstring for the full rationale and —
importantly — **"HOW TO ADD AN IMAGE TO THE MIRROR"**.

Some upstream registries (chiefly Docker Hub, ``docker.io``) rate-limit
anonymous pulls and have no credential-free ECR pull-through cache, which can
stall image pulls on a cold cluster (most visibly Volcano's ``volcanosh/vc-*``).
Mirroring those images into a ``gco/*`` ECR namespace lets the cluster pull from
same-account ECR (fast, no credential) once the consumer points at the mirror.
The copy preserves the full multi-arch manifest list (Buildx / Finch
``--all-platforms`` / skopeo, chosen at runtime). The image set is produced by
``cli._image_mirror.collect_source_refs`` (Volcano's images are derived from
``lambda/helm-installer/charts.yaml`` so they never drift from the chart).

Usage::

    # Mirror every configured image into
    #   <account>.dkr.ecr.us-east-1.amazonaws.com/<ecr_namespace>/<repo>:<tag>
    python scripts/mirror_images.py --region us-east-1

    # Preview the plan without creating repos or copying anything
    python scripts/mirror_images.py --region us-east-1 --dry-run

    # Override the destination namespace (must match cdk.json
    # volcano_image_mirror.ecr_namespace)
    python scripts/mirror_images.py --region us-east-1 --ecr-namespace gco/dockerhub

Requirements: a container runtime with a multi-arch copy path (Docker Buildx,
Finch/nerdctl, or skopeo) and AWS credentials with ECR create/push permissions
for the destination account/region. The source pulls are anonymous.

Note: ``gco stacks deploy`` runs this same mirror automatically (per region)
when ``volcano_image_mirror.enabled`` is set, so this script is mainly for
out-of-band re-mirrors (e.g. after bumping a mirrored image's version, or to
seed a region before enabling the toggle).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repo root importable so ``cli`` resolves when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli import _image_mirror as mirror  # noqa: E402 - sys.path set above


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mirror the project's third-party images into ECR."
    )
    parser.add_argument(
        "--region",
        required=True,
        help="Target AWS region (must match the regional stack, e.g. us-east-1).",
    )
    parser.add_argument(
        "--ecr-namespace",
        default=None,
        help=(
            "Destination ECR namespace under which the images are stored. "
            "Defaults to cdk.json volcano_image_mirror.ecr_namespace (gco/dockerhub). "
            "Must match the cdk.json value so the consumer's image override "
            "resolves to the mirrored images."
        ),
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Re-copy images even if the tag already exists in ECR.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the copy plan without creating repositories or copying images.",
    )
    args = parser.parse_args(argv)

    ecr_namespace = (args.ecr_namespace or mirror.cdk_default_namespace()).strip("/")

    if args.dry_run:
        # Placeholder host so the plan is printable without AWS calls.
        registry_host = f"<account>.dkr.ecr.{args.region}.amazonaws.com"
        plan = mirror.plan_from_sources(mirror.collect_source_refs(), registry_host, ecr_namespace)
        print(f"[dry-run] would mirror {len(plan)} image(s) into namespace {ecr_namespace!r}:")
        for item in plan:
            print(f"  {item.source_ref}  ->  {item.dest_ref}")
        return 0

    result = mirror.mirror_images(
        args.region, ecr_namespace=ecr_namespace, skip_existing=not args.no_skip_existing
    )
    print(
        f"\nDone. mirrored={len(result['mirrored'])} skipped={len(result['skipped'])} "
        f"into {result['registry']} (strategy: {result['strategy']}).\n"
        "Set volcano_image_mirror.enabled=true in cdk.json (matching ecr_namespace), "
        "deploy the regional stack, then re-converge add-ons with "
        "'gco stacks addons install -r <region>'."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
