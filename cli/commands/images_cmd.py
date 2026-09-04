"""Container image registry commands.

Subcommands wrap :class:`cli.images.ImageManager`. Read-only commands
(`list`, `tags`, `describe`, `uri`, replication get/status) need no
confirmation; administrative commands (`init`, `lifecycle`, replication
sync) are idempotent; destructive commands (`delete-tag`, `delete-repo`,
`cleanup`, `prune`) require ``-y`` / ``--yes``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


def _discard_mirror_log(_message: str) -> None:
    """Suppress human progress logs when machine-readable output is requested."""


@click.group()
@pass_config
def images(config: Any) -> None:
    """Manage container images in the project ECR registry (gco/* repos)."""
    pass


# ---------------------------------------------------------------------------
# Administrative
# ---------------------------------------------------------------------------


@images.command("init")
@click.argument("name")
@click.option("--retain/--no-retain", default=False, help="Apply gco:retain=true tag")
@pass_config
def images_init(config: Any, name: Any, retain: Any) -> None:
    """Create a project repository with the default lifecycle policy.

    Examples:
        gco images init my-app
        gco images init my-app --retain
    """
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        manager = get_image_manager(config)
        result = manager.init(name, retain=retain)
        if config.output_format == "table":
            if result.get("created"):
                formatter.print_success(f"Created repository {result['name']}")
            else:
                formatter.print_info(f"Repository {result['name']} already existed")
        else:
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to init repository: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


@images.command("list")
@pass_config
def images_list(config: Any) -> None:
    """List every repository under the project's gco/ prefix."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        repos = get_image_manager(config).list_repos()
        if not repos:
            formatter.print_info("No repositories found.")
            return
        formatter.print(repos)
    except Exception as e:
        formatter.print_error(f"Failed to list repositories: {e}")
        sys.exit(1)


@images.command("tags")
@click.argument("name")
@pass_config
def images_tags(config: Any, name: Any) -> None:
    """List tags within a repository."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        rows = get_image_manager(config).list_tags(name)
        if not rows:
            formatter.print_info("No tags found.")
            return
        formatter.print(rows)
    except Exception as e:
        formatter.print_error(f"Failed to list tags: {e}")
        sys.exit(1)


@images.command("describe")
@click.argument("name")
@click.argument("tag")
@pass_config
def images_describe(config: Any, name: Any, tag: Any) -> None:
    """Print the full ECR details for a single image tag."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        result = get_image_manager(config).describe(name, tag)
        if not result:
            formatter.print_info(f"Tag '{tag}' not found in {name}")
            return
        formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to describe image: {e}")
        sys.exit(1)


@images.command("uri")
@click.argument("name")
@click.option("--tag", "-t", default="latest", help="Image tag (default: latest)")
@pass_config
def images_uri(config: Any, name: Any, tag: Any) -> None:
    """Print the registry URI for an image without making any AWS calls."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        uri = get_image_manager(config).get_uri(name, tag=tag)
        print(uri)
    except Exception as e:
        formatter.print_error(f"Failed to compute URI: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Build / push
# ---------------------------------------------------------------------------


@images.command("build")
@click.argument("context")
@click.option("--name", "-n", required=True, help="Image name")
@click.option("--tag", "-t", default=None, help="Image tag (default: git SHA or 'latest')")
@click.option("--dockerfile", "-f", default="Dockerfile", help="Path to Dockerfile")
@click.option("--build-arg", "build_args", multiple=True, help="Build arg KEY=VALUE")
@click.option("--platform", default="linux/amd64", help="Target platform")
@click.option("--retain/--no-retain", default=False, help="Apply gco:retain=true tag")
@pass_config
def images_build(
    config: Any,
    context: Any,
    name: Any,
    tag: Any,
    dockerfile: Any,
    build_args: Any,
    platform: Any,
    retain: Any,
) -> None:
    """Build a container image and push it to the project's ECR repo.

    Examples:
        gco images build ./my-app --name my-app --tag v1
        gco images build ./svc --name svc --build-arg VERSION=1.2.3
    """
    from ..images import get_image_manager

    formatter = get_output_formatter(config)

    args_dict: dict[str, str] = {}
    for arg in build_args or ():
        if "=" not in arg:
            formatter.print_error(f"Invalid --build-arg (missing '='): {arg}")
            sys.exit(1)
        key, value = arg.split("=", 1)
        args_dict[key] = value

    try:
        manager = get_image_manager(config)
        result = manager.build(
            context=context,
            name=name,
            tag=tag,
            dockerfile=dockerfile,
            build_args=args_dict or None,
            platform=platform,
            retain=retain,
            quiet=config.output_format != "table",
        )
        if config.output_format == "table":
            formatter.print_success(f"Built and pushed {result['image_uri']}")
            if result.get("digest"):
                formatter.print_info(f"Digest: {result['digest']}")
        else:
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to build image: {e}")
        sys.exit(1)


@images.command("push")
@click.argument("name")
@click.option("--tag", "-t", required=True, help="Image tag")
@click.option("--local-image", required=True, help="Existing local image reference")
@click.option("--retain/--no-retain", default=False, help="Apply gco:retain=true tag")
@pass_config
def images_push(
    config: Any,
    name: Any,
    tag: Any,
    local_image: Any,
    retain: Any,
) -> None:
    """Push an already-built local image to the project's ECR repo."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        result = get_image_manager(config).push(
            name=name,
            tag=tag,
            local_image=local_image,
            retain=retain,
            quiet=config.output_format != "table",
        )
        if config.output_format == "table":
            formatter.print_success(f"Pushed {result['image_uri']}")
            if result.get("digest"):
                formatter.print_info(f"Digest: {result['digest']}")
        else:
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to push image: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Mirror third-party images into the project ECR
# ---------------------------------------------------------------------------


@images.command("mirror")
@click.option(
    "--region",
    "-r",
    required=True,
    help="Target AWS region (must match the regional stack, e.g. us-east-1).",
)
@click.option(
    "--ecr-namespace",
    default=None,
    help=(
        "Destination ECR namespace. Defaults to cdk.json "
        "volcano_image_mirror.ecr_namespace (gco/dockerhub); must match it so the "
        "consumer's image override resolves to the mirror."
    ),
)
@click.option(
    "--no-skip-existing",
    is_flag=True,
    default=False,
    help="Re-copy images even if the tag already exists in ECR.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the copy plan without creating repositories or copying images.",
)
@pass_config
def images_mirror(
    config: Any,
    region: Any,
    ecr_namespace: Any,
    no_skip_existing: Any,
    dry_run: Any,
) -> None:
    """Mirror third-party images (e.g. Volcano's docker.io images) into the ECR.

    This is the same multi-arch copy ``gco stacks deploy`` runs automatically when
    ``volcano_image_mirror.enabled`` is set. Run it directly to pre-seed a region
    before enabling the toggle, or to re-mirror after bumping a mirrored image's
    version. Wraps the shared ``cli._image_mirror`` core (also used by the deploy
    auto-mirror and the ``images_mirror`` MCP tool).

    Examples:
        gco images mirror --region us-east-1
        gco images mirror --region us-east-1 --dry-run
        gco images mirror --region us-east-1 --ecr-namespace gco/dockerhub
    """
    from .. import _image_mirror as mirror

    formatter = get_output_formatter(config)
    namespace = (ecr_namespace or mirror.cdk_default_namespace()).strip("/")
    table_output = config.output_format == "table"
    try:
        if dry_run:
            # Partition metadata is local, so this remains free of AWS API calls.
            registry_host = mirror._registry_host("<account>", region)
            plan = mirror.plan_from_sources(mirror.collect_source_refs(), registry_host, namespace)
            result = {
                "region": region,
                "ecr_namespace": namespace,
                "images": [
                    {"source_ref": item.source_ref, "dest_ref": item.dest_ref} for item in plan
                ],
            }
            if table_output:
                formatter.print_info(
                    f"[dry-run] would mirror {len(plan)} image(s) into namespace {namespace!r}:"
                )
                for item in plan:
                    formatter.print_info(f"  {item.source_ref}  ->  {item.dest_ref}")
            else:
                formatter.print(result)
            return

        result = mirror.mirror_images(
            region,
            ecr_namespace=namespace,
            skip_existing=not no_skip_existing,
            log=print if table_output else _discard_mirror_log,
        )
        if table_output:
            formatter.print_success(
                f"Mirrored {len(result['mirrored'])}, skipped {len(result['skipped'])} "
                f"into {result['registry']} (strategy: {result['strategy']})."
            )
        else:
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to mirror images: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Destructive
# ---------------------------------------------------------------------------


@images.command("delete-tag")
@click.argument("name")
@click.argument("tag")
@click.option("--yes", "-y", is_flag=True, required=True, help="Required confirmation")
@pass_config
def images_delete_tag(config: Any, name: Any, tag: Any, yes: Any) -> None:
    """Delete a single tag from a repository (irreversible)."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        result = get_image_manager(config).delete_tag(name, tag)
        formatter.print_success(
            f"Deleted {len(result.get('deleted', []))} image(s) from {result['name']}"
        )
        if config.output_format != "table":
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to delete tag: {e}")
        sys.exit(1)


@images.command("delete-repo")
@click.argument("name")
@click.option("--force/--no-force", default=False, help="Delete even if non-empty")
@click.option("--yes", "-y", is_flag=True, required=True, help="Required confirmation")
@pass_config
def images_delete_repo(config: Any, name: Any, force: Any, yes: Any) -> None:
    """Delete a whole repository (irreversible)."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        result = get_image_manager(config).delete_repo(name, force=force)
        formatter.print_success(f"Deleted repository {result['name']}")
        if config.output_format != "table":
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to delete repository: {e}")
        sys.exit(1)


@images.command("cleanup")
@click.option("--name", "-n", default=None, help="Single repository to clean up")
@click.option("--all", "all_repos", is_flag=True, help="Clean up every project repo")
@click.option("--yes", "-y", is_flag=True, required=True, help="Required confirmation")
@pass_config
def images_cleanup(config: Any, name: Any, all_repos: Any, yes: Any) -> None:
    """Remove untagged images across one or all project repos."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    if not name and not all_repos:
        formatter.print_error("Provide --name <repo> or --all")
        sys.exit(1)
    try:
        result = get_image_manager(config).cleanup(name=name, all=all_repos)
        formatter.print_success(
            f"Cleaned up: repos_touched={result['repos_touched']} "
            f"tags_deleted={result['tags_deleted']} "
            f"bytes_freed={result['bytes_freed']}"
        )
        if config.output_format != "table":
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to clean up: {e}")
        sys.exit(1)


@images.command("prune")
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Dry run by default; pass --no-dry-run to actually delete",
)
@click.option("--yes", "-y", is_flag=True, required=True, help="Required confirmation")
@pass_config
def images_prune(config: Any, dry_run: Any, yes: Any) -> None:
    """Remove untagged images older than 30 days (dry-run by default)."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        result = get_image_manager(config).prune(dry_run=dry_run)
        verb = "Would delete" if dry_run else "Deleted"
        formatter.print_success(
            f"{verb}: repos_touched={result['repos_touched']} "
            f"tags_deleted={result['tags_deleted']} "
            f"bytes_freed={result['bytes_freed']}"
        )
        if config.output_format != "table":
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to prune: {e}")
        sys.exit(1)


@images.command("orphans")
@click.option(
    "--threshold-days",
    default=30,
    type=int,
    help="Only report tags older than this many days",
)
@pass_config
def images_orphans(config: Any, threshold_days: Any) -> None:
    """List tags older than threshold_days that are not referenced anywhere."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        rows = get_image_manager(config).orphans(threshold_days=threshold_days)
        if not rows:
            formatter.print_info("No orphans found.")
            return
        formatter.print(rows)
    except Exception as e:
        formatter.print_error(f"Failed to detect orphans: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@images.group("lifecycle")
def lifecycle() -> None:
    """Lifecycle policy management."""
    pass


@lifecycle.command("get")
@click.argument("name")
@pass_config
def lifecycle_get(config: Any, name: Any) -> None:
    """Print the lifecycle policy on a repository."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        result = get_image_manager(config).lifecycle_get(name)
        if not result:
            formatter.print_info(f"No lifecycle policy on {name}.")
            return
        formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to read lifecycle policy: {e}")
        sys.exit(1)


@lifecycle.command("set")
@click.argument("name")
@click.option("--file", "-f", "policy_file", required=True, help="Path to lifecycle JSON")
@pass_config
def lifecycle_set(config: Any, name: Any, policy_file: Any) -> None:
    """Replace the lifecycle policy on a repository from a JSON file."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        with open(policy_file, encoding="utf-8") as f:
            policy = json.load(f)
        result = get_image_manager(config).lifecycle_set(name, policy)
        formatter.print_success(f"Updated lifecycle policy on {result['name']}")
        if config.output_format != "table":
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to set lifecycle policy: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Replication
# ---------------------------------------------------------------------------


@images.group("replication")
def replication() -> None:
    """Replication management."""
    pass


@replication.command("get")
@pass_config
def replication_get(config: Any) -> None:
    """Print the current ECR replication configuration."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        result = get_image_manager(config).replication_get()
        if not result:
            formatter.print_info("No replication policy configured.")
            return
        formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to read replication policy: {e}")
        sys.exit(1)


@replication.command("status")
@pass_config
def replication_status(config: Any) -> None:
    """Print per-image replication status across project repos."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        rows = get_image_manager(config).replication_status()
        if not rows:
            formatter.print_info("No replication status entries.")
            return
        formatter.print(rows)
    except Exception as e:
        formatter.print_error(f"Failed to read replication status: {e}")
        sys.exit(1)


@replication.command("sync")
@pass_config
def replication_sync(config: Any) -> None:
    """Apply the project's standard replication rule (gco/* to all regions)."""
    from ..images import get_image_manager

    formatter = get_output_formatter(config)
    try:
        result = get_image_manager(config).replication_sync()
        dests = result.get("destinations") or []
        formatter.print_success(
            f"Replication rule synced: destinations={', '.join(dests) or 'none'}"
        )
        if config.output_format != "table":
            formatter.print(result)
    except Exception as e:
        formatter.print_error(f"Failed to sync replication rule: {e}")
        sys.exit(1)
