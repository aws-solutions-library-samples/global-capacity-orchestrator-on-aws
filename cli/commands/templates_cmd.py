"""Job template commands."""

import sys
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def templates(config: Any) -> None:
    """Manage job templates.

    Templates are reusable job configurations stored in DynamoDB.
    They support parameter substitution using {{parameter}} syntax.
    """
    pass


@templates.command("list")
@click.option("--region", "-r", help="Region to query (any region works)")
@pass_config
def templates_list(config: Any, region: Any) -> None:
    """List all job templates.

    Examples:
        gco templates list
    """
    formatter = get_output_formatter(config)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or (config.default_region if config.use_regional_api else None)
        result = aws_client.call_api(
            method="GET",
            path="/api/v1/templates",
            region=query_region,
        )

        if config.output_format == "table":
            templates_data = result.get("templates", [])
            if not templates_data:
                formatter.print_info("No templates found")
                return

            print(f"\n  Job Templates ({result.get('count', 0)} total)")
            print("  " + "-" * 70)
            print("  NAME                          DESCRIPTION                    CREATED")
            print("  " + "-" * 70)
            for t in templates_data:
                name = t.get("name", "")[:28]
                desc = (t.get("description") or "")[:28]
                created = (t.get("created_at") or "")[:19]
                print(f"  {name:<30} {desc:<30} {created}")
        else:
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to list templates: {e}")
        sys.exit(1)


@templates.command("get")
@click.argument("name")
@click.option("--region", "-r", help="Region to query (any region works)")
@pass_config
def templates_get(config: Any, name: Any, region: Any) -> None:
    """Get details of a specific template.

    Examples:
        gco templates get gpu-training-template
    """
    formatter = get_output_formatter(config)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or (config.default_region if config.use_regional_api else None)
        result = aws_client.call_api(
            method="GET",
            path=f"/api/v1/templates/{name}",
            region=query_region,
        )

        template = result.get("template", {})

        if config.output_format == "table":
            print(f"\n  Template: {template.get('name')}")
            print("  " + "-" * 50)
            print(f"  Description: {template.get('description') or 'N/A'}")
            print(f"  Created:     {template.get('created_at')}")
            print(f"  Updated:     {template.get('updated_at')}")

            params = template.get("parameters", {})
            if params:
                print("\n  Default Parameters:")
                for k, v in params.items():
                    print(f"    {k}: {v}")

            print("\n  Manifest:")
            import json

            print(json.dumps(template.get("manifest", {}), indent=4))
        else:
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to get template: {e}")
        sys.exit(1)


@templates.command("create")
@click.argument("manifest_path", type=click.Path(exists=True))
@click.option("--name", "-n", required=True, help="Template name")
@click.option("--description", "-d", help="Template description")
@click.option("--param", "-p", multiple=True, help="Default parameter (key=value)")
@click.option("--region", "-r", help="Region to use (any region works)")
@pass_config
def templates_create(
    config: Any, manifest_path: Any, name: Any, description: Any, param: Any, region: Any
) -> None:
    """Create a new job template from a manifest file.

    The manifest can contain {{parameter}} placeholders that will be
    substituted when creating jobs from the template.

    Examples:
        gco templates create job.yaml --name gpu-template -d "GPU training template"
        gco templates create job.yaml -n my-template -p image=pytorch:latest -p gpus=4
    """
    import yaml

    formatter = get_output_formatter(config)

    # Parse parameters
    parameters = {}
    for p in param:
        if "=" in p:
            k, v = p.split("=", 1)
            parameters[k] = v

    try:
        # Load manifest
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)

        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or (config.default_region if config.use_regional_api else None)
        result = aws_client.call_api(
            method="POST",
            path="/api/v1/templates",
            region=query_region,
            body={
                "name": name,
                "description": description,
                "manifest": manifest,
                "parameters": parameters if parameters else None,
            },
        )

        formatter.print_success(f"Template '{name}' created successfully")
        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to create template: {e}")
        sys.exit(1)


@templates.command("delete")
@click.argument("name")
@click.option("--region", "-r", help="Region to use (any region works)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def templates_delete(config: Any, name: Any, region: Any, yes: Any) -> None:
    """Delete a job template.

    Examples:
        gco templates delete old-template
        gco templates delete old-template -y
    """
    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(f"Delete template '{name}'?", abort=True)

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        query_region = region or (config.default_region if config.use_regional_api else None)
        result = aws_client.call_api(
            method="DELETE",
            path=f"/api/v1/templates/{name}",
            region=query_region,
        )

        formatter.print_success(f"Template '{name}' deleted")
        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to delete template: {e}")
        sys.exit(1)


@templates.command("run")
@click.argument("template_name")
@click.option("--name", "-n", required=True, help="Job name")
@click.option("--namespace", default="gco-jobs", help="Kubernetes namespace")
@click.option("--param", "-p", multiple=True, help="Parameter override (key=value)")
@click.option("--region", "-r", required=True, help="Region to run the job")
@pass_config
def templates_run(
    config: Any, template_name: Any, name: Any, namespace: Any, param: Any, region: Any
) -> None:
    """Create and run a job from a template.

    Examples:
        gco templates run gpu-template --name my-job --region us-east-1
        gco templates run gpu-template -n my-job -r us-east-1 -p image=custom:v1
    """
    formatter = get_output_formatter(config)

    # Parse parameters
    parameters = {}
    for p in param:
        if "=" in p:
            k, v = p.split("=", 1)
            parameters[k] = v

    try:
        from ..aws_client import get_aws_client

        aws_client = get_aws_client(config)

        result = aws_client.call_api(
            method="POST",
            path=f"/api/v1/jobs/from-template/{template_name}",
            region=region,
            body={
                "name": name,
                "namespace": namespace,
                "parameters": parameters if parameters else None,
            },
        )

        if result.get("success"):
            formatter.print_success(f"Job '{name}' created from template '{template_name}'")
        else:
            formatter.print_error("Failed to create job from template")

        formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to run template: {e}")
        sys.exit(1)
