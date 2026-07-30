"""Inference endpoint commands."""

import codecs
import sys
from email.message import Message
from typing import Any

import click

from ..config import GCOConfig
from ..output import get_output_formatter

pass_config = click.make_pass_decorator(GCOConfig, ensure=True)


@click.group()
@pass_config
def inference(config: Any) -> None:
    """Manage multi-region inference endpoints."""
    pass


@inference.command("deploy")
@click.argument("endpoint_name")
@click.option(
    "--image",
    "-i",
    default=None,
    help="Container image (e.g. vllm/vllm-openai:v0.26.0). Optional with "
    "--mooncake-mode: falls back to the default upstream Mooncake-enabled vLLM image.",
)
@click.option(
    "--region",
    "-r",
    multiple=True,
    help="Target region(s). Repeatable. Default: all deployed regions",
)
@click.option("--replicas", default=1, help="Replicas per region (default: 1)")
@click.option("--gpu-count", default=1, help="GPUs per replica (default: 1)")
@click.option("--gpu-type", help="GPU instance type hint (e.g. g5.xlarge)")
@click.option("--port", default=8000, help="Container port (default: 8000)")
@click.option("--model-path", help="EFS path for model weights")
@click.option(
    "--model-source",
    help="S3 URI for model weights (e.g. s3://bucket/models/llama3). "
    "Auto-synced to each region via init container.",
)
@click.option("--health-path", default="/health", help="Health check path (default: /health)")
@click.option("--env", "-e", multiple=True, help="Environment variable (KEY=VALUE). Repeatable")
@click.option("--namespace", "-n", default="gco-inference", help="Kubernetes namespace")
@click.option("--label", "-l", multiple=True, help="Label (key=value). Repeatable")
@click.option("--min-replicas", type=int, default=None, help="Autoscaling: minimum replicas")
@click.option("--max-replicas", type=int, default=None, help="Autoscaling: maximum replicas")
@click.option(
    "--autoscale-metric",
    multiple=True,
    help="Autoscaling metric (cpu:70, memory:80, gpu:60). Repeatable. Enables "
    "autoscaling. CPU/memory scale via the native HPA; gpu (and gpu_memory) "
    "scale on CloudWatch GPU utilization via KEDA.",
)
@click.option(
    "--capacity-type",
    type=click.Choice(["on-demand", "spot"]),
    default=None,
    help="Node capacity type. 'spot' uses cheaper preemptible instances.",
)
@click.option(
    "--extra-args",
    multiple=True,
    help="Extra arguments passed to the container (e.g. '--kv-transfer-config {...}'). Repeatable.",
)
@click.option(
    "--accelerator",
    type=click.Choice(["nvidia", "neuron"]),
    default="nvidia",
    help="Accelerator type: 'nvidia' for GPU instances (default), 'neuron' for Trainium/Inferentia.",
)
@click.option(
    "--node-selector",
    multiple=True,
    help="Node selector (key=value). Repeatable. E.g. --node-selector eks.amazonaws.com/instance-family=inf2",
)
@click.option(
    "--no-rewrite-image",
    is_flag=True,
    default=False,
    help="Skip the per-region ECR URI rewrite. The image URI is sent verbatim "
    "to every target region (operator owns cross-region pulls).",
)
@click.option(
    "--mooncake-mode",
    type=click.Choice(["disaggregated", "store", "both"]),
    default=None,
    help="Enable Mooncake serving: 'disaggregated' splits prefill/decode, "
    "'store' runs a shared KV-cache store, 'both' composes the two. When set "
    "and -i is omitted, the default upstream Mooncake-enabled vLLM image is used.",
)
@click.option(
    "--prefill-replicas",
    type=int,
    default=1,
    help="Prefill instance count (X in an XpYd topology) for split modes.",
)
@click.option(
    "--decode-replicas",
    type=int,
    default=1,
    help="Decode instance count (Y in an XpYd topology) for split modes.",
)
@click.option(
    "--mooncake-protocol",
    type=click.Choice(["rdma", "tcp"]),
    default=None,
    help="Mooncake transfer intent. 'rdma' (the default) schedules role pods "
    "on EFA and configures vLLM's connector protocol as 'efa'; 'tcp' is the "
    "non-EFA fallback. Requires --mooncake-mode.",
)
@click.option(
    "--mooncake-device-name",
    default=None,
    help="Network device passed to Mooncake (for example efa_0 or eth0). "
    "Omit or pass an empty value for auto-detection. Requires --mooncake-mode.",
)
@click.option(
    "--mooncake-autoscale",
    multiple=True,
    help="Per-role Mooncake autoscaling as ROLE:MIN:MAX[:METRIC:TARGET ...], "
    "e.g. 'prefill:1:8' or 'decode:2:16:cpu:70:gpu:60'. Repeatable (one per "
    "role); append additional METRIC:TARGET pairs to scale a role on multiple "
    "metrics (cpu/memory via HPA, gpu/gpu_memory via KEDA CloudWatch). Requires "
    "--mooncake-mode disaggregated|both; populates spec.mooncake.autoscaling "
    "(distinct from the legacy --autoscale-metric/--min-replicas flags).",
)
@click.option(
    "--mooncake-cold-tier",
    is_flag=True,
    default=False,
    help="Enable the asynchronous per-region S3 cold tier for the shared "
    "KV-cache store (the cold tier extends the store). Pre-warm it with "
    "'gco inference populate-kv'. Requires --mooncake-mode store or both.",
)
@click.option(
    "--mooncake-proxy-image",
    default=None,
    help="Container image for the prefill-decode proxy (disaggregated/both). "
    "Defaults to the endpoint image, which bundles the reference proxy.",
)
@click.option(
    "--mooncake-admin-key-secret",
    default=None,
    help="Name of an existing Kubernetes Secret holding the prefill-decode "
    "proxy ADMIN_API_KEY. Optional: when omitted, each region's monitor "
    "auto-provisions a {name}-admin Secret with a generated key.",
)
@pass_config
def inference_deploy(
    config: Any,
    endpoint_name: Any,
    image: Any,
    region: Any,
    replicas: Any,
    gpu_count: Any,
    gpu_type: Any,
    port: Any,
    model_path: Any,
    model_source: Any,
    health_path: Any,
    env: Any,
    namespace: Any,
    label: Any,
    min_replicas: Any,
    max_replicas: Any,
    autoscale_metric: Any,
    capacity_type: Any,
    extra_args: Any,
    accelerator: Any,
    node_selector: Any,
    no_rewrite_image: Any,
    mooncake_mode: Any,
    prefill_replicas: Any,
    decode_replicas: Any,
    mooncake_protocol: Any,
    mooncake_device_name: Any,
    mooncake_autoscale: Any,
    mooncake_cold_tier: Any,
    mooncake_proxy_image: Any,
    mooncake_admin_key_secret: Any,
) -> None:
    """Deploy an inference endpoint to one or more regions.

    The endpoint is registered in DynamoDB and the inference_monitor
    in each target region creates the Kubernetes resources automatically.

    Examples:
        gco inference deploy my-llm -i vllm/vllm-openai:v0.26.0

        gco inference deploy llama3-70b \\
            -i vllm/vllm-openai:v0.26.0 \\
            -r us-east-1 -r eu-west-1 \\
            --replicas 2 --gpu-count 4 \\
            --model-path /mnt/gco/models/llama3-70b \\
            -e MODEL_NAME=meta-llama/Llama-3-70B
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    # Parse env vars and labels
    env_dict = {}
    for e_var in env:
        if "=" in e_var:
            k, v = e_var.split("=", 1)
            env_dict[k] = v

    labels_dict = {}
    for lbl in label:
        if "=" in lbl:
            k, v = lbl.split("=", 1)
            labels_dict[k] = v

    node_selector_dict = {}
    for ns in node_selector:
        if "=" in ns:
            k, v = ns.split("=", 1)
            node_selector_dict[k] = v

    # Build autoscaling config
    autoscaling_config = None
    if autoscale_metric:
        metrics = []
        for m in autoscale_metric:
            if ":" in m:
                mtype, mtarget = m.split(":", 1)
                metrics.append({"type": mtype, "target": int(mtarget)})
            else:
                metrics.append({"type": m, "target": 70})
        autoscaling_config = {
            "enabled": True,
            "min_replicas": min_replicas or 1,
            "max_replicas": max_replicas or 10,
            "metrics": metrics,
        }

    # Transfer overrides are meaningful only when a Mooncake block is being
    # authored. With no override, the monitor resolves the default RDMA intent
    # to vLLM's explicit EFA connector protocol and auto-detects the device.
    if (mooncake_protocol is not None or mooncake_device_name is not None) and not mooncake_mode:
        formatter.print_error(
            "--mooncake-protocol and --mooncake-device-name require --mooncake-mode."
        )
        sys.exit(1)

    mooncake_transfer_config: dict[str, Any] | None = None
    if mooncake_protocol is not None or mooncake_device_name is not None:
        mooncake_transfer_config = {}
        if mooncake_protocol is not None:
            mooncake_transfer_config["protocol"] = mooncake_protocol
        if mooncake_device_name is not None:
            mooncake_transfer_config["device_name"] = mooncake_device_name

    # Build per-role Mooncake autoscaling config (spec.mooncake.autoscaling).
    # This is distinct from the legacy single-Deployment autoscaling above:
    # each ROLE:MIN:MAX token sets a role's bounds, and any number of trailing
    # METRIC:TARGET pairs add scaling signals for that role. Bounds and metrics
    # are validated fail-fast in the deploy path before anything is persisted.
    mooncake_autoscaling_config: dict[str, Any] | None = None
    if mooncake_autoscale:
        if not mooncake_mode:
            formatter.print_error(
                "--mooncake-autoscale requires --mooncake-mode (disaggregated or both)."
            )
            sys.exit(1)
        mooncake_autoscaling_config = {"enabled": True}
        for entry in mooncake_autoscale:
            parts = entry.split(":")
            # ROLE:MIN:MAX, then zero or more METRIC:TARGET pairs.
            if len(parts) < 3 or (len(parts) - 3) % 2 != 0:
                formatter.print_error(
                    f"Invalid --mooncake-autoscale value '{entry}'. Expected "
                    "ROLE:MIN:MAX optionally followed by METRIC:TARGET pairs."
                )
                sys.exit(1)
            role = parts[0]
            if role not in ("prefill", "decode"):
                formatter.print_error(
                    f"Invalid --mooncake-autoscale role '{role}'. Expected 'prefill' or 'decode'."
                )
                sys.exit(1)
            try:
                role_block: dict[str, Any] = {
                    "min_replicas": int(parts[1]),
                    "max_replicas": int(parts[2]),
                }
                metric_tokens = parts[3:]
                metrics = [
                    {"type": metric_tokens[i], "target": int(metric_tokens[i + 1])}
                    for i in range(0, len(metric_tokens), 2)
                ]
                if metrics:
                    role_block["metrics"] = metrics
            except ValueError:
                formatter.print_error(
                    f"Invalid --mooncake-autoscale numbers in '{entry}'. MIN, MAX, "
                    "and each TARGET must be integers."
                )
                sys.exit(1)
            mooncake_autoscaling_config[role] = role_block

    # --mooncake-cold-tier opts into the async per-region S3 cold tier, which
    # extends the shared store, so it only applies to store/both modes.
    if mooncake_cold_tier and mooncake_mode not in ("store", "both"):
        formatter.print_error(
            "--mooncake-cold-tier requires --mooncake-mode store or both "
            "(the cold tier extends the shared KV-cache store)."
        )
        sys.exit(1)

    mooncake_store_config: dict[str, Any] | None = None
    if mooncake_cold_tier:
        mooncake_store_config = {"enabled": True, "cold_tier_enabled": True}

    # Configure the prefill-decode proxy that fronts split modes: an explicit
    # image (otherwise it defaults to the endpoint image) and the name of the
    # Kubernetes Secret holding its ADMIN_API_KEY.
    mooncake_proxy_config: dict[str, Any] | None = None
    if mooncake_proxy_image or mooncake_admin_key_secret:
        mooncake_proxy_config = {}
        if mooncake_proxy_image:
            mooncake_proxy_config["image"] = mooncake_proxy_image
        if mooncake_admin_key_secret:
            mooncake_proxy_config["admin_api_key_secret"] = mooncake_admin_key_secret

    # When no admin-key Secret is named, each region's monitor auto-provisions a
    # {name}-admin Secret with a generated key, so no manual step is needed.
    if mooncake_mode in ("disaggregated", "both") and not mooncake_admin_key_secret:
        formatter.print_info(
            "No --mooncake-admin-key-secret given; each region's inference "
            "monitor will auto-provision a '{name}-admin' Secret with a "
            "generated ADMIN_API_KEY. Pass --mooncake-admin-key-secret to use "
            "your own Secret instead."
        )

    try:
        manager = get_inference_manager(config)
        result = manager.deploy(
            endpoint_name=endpoint_name,
            image=image,
            target_regions=list(region) if region else None,
            replicas=replicas,
            gpu_count=gpu_count,
            gpu_type=gpu_type,
            port=port,
            model_path=model_path,
            model_source=model_source,
            health_check_path=health_path,
            env=env_dict if env_dict else None,
            namespace=namespace,
            labels=labels_dict if labels_dict else None,
            autoscaling=autoscaling_config,
            capacity_type=capacity_type,
            extra_args=list(extra_args) if extra_args else None,
            accelerator=accelerator,
            node_selector=node_selector_dict if node_selector_dict else None,
            rewrite_image=not no_rewrite_image,
            mooncake_mode=mooncake_mode,
            prefill_replicas=prefill_replicas,
            decode_replicas=decode_replicas,
            mooncake_store=mooncake_store_config,
            mooncake_transfer=mooncake_transfer_config,
            mooncake_proxy=mooncake_proxy_config,
            mooncake_autoscaling=mooncake_autoscaling_config,
        )

        formatter.print_success(f"Endpoint '{endpoint_name}' registered for deployment")
        regions_str = ", ".join(result.get("target_regions", []))
        formatter.print_info(f"Target regions: {regions_str}")
        formatter.print_info(f"Ingress path: {result.get('ingress_path', '')}")
        formatter.print_info(
            "The inference_monitor in each region will create the resources. "
            "Use 'gco inference status' to track progress."
        )

        # Warn if deploying to a subset of regions
        if region:
            from ..aws_client import get_aws_client as _get_client

            all_stacks = _get_client(config).discover_regional_stacks()
            all_regions = set(all_stacks.keys())
            target_set = set(result.get("target_regions", []))
            missing = all_regions - target_set
            if missing:
                formatter.print_warning(
                    f"Endpoint is NOT deployed to: {', '.join(sorted(missing))}. "
                    "Global Accelerator may route users to those regions where "
                    "the endpoint won't exist. Consider deploying to all regions "
                    "(omit -r) for consistent global routing."
                )

        if config.output_format != "table":
            formatter.print(result)

    except ValueError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to deploy endpoint: {e}")
        sys.exit(1)


@inference.command("list")
@click.option("--state", "-s", help="Filter by state (deploying, running, stopped, deleted)")
@click.option("--region", "-r", help="Filter by target region")
@pass_config
def inference_list(config: Any, state: Any, region: Any) -> None:
    """List inference endpoints.

    Examples:
        gco inference list
        gco inference list --state running
        gco inference list -r us-east-1
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        endpoints = manager.list_endpoints(desired_state=state, region=region)

        if config.output_format != "table":
            formatter.print(endpoints)
            return

        if not endpoints:
            formatter.print_info("No inference endpoints found")
            return

        print(f"\n  Inference Endpoints ({len(endpoints)} found)")
        print("  " + "-" * 85)
        print(f"  {'NAME':<25} {'STATE':<12} {'REGIONS':<25} {'REPLICAS':>8} {'IMAGE'}")
        print("  " + "-" * 85)
        for ep in endpoints:
            name = ep.get("endpoint_name", "")[:24]
            ep_state = ep.get("desired_state", "unknown")
            regions = ", ".join(ep.get("target_regions", []))[:24]
            spec = ep.get("spec", {})
            replicas = spec.get("replicas", 1) if isinstance(spec, dict) else 1
            image = spec.get("image", "")[:40] if isinstance(spec, dict) else ""
            print(f"  {name:<25} {ep_state:<12} {regions:<25} {replicas:>8} {image}")

        print()

    except Exception as e:
        formatter.print_error(f"Failed to list endpoints: {e}")
        sys.exit(1)


@inference.command("status")
@click.argument("endpoint_name")
@pass_config
def inference_status(config: Any, endpoint_name: Any) -> None:
    """Show detailed status of an inference endpoint.

    Examples:
        gco inference status my-llm
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        endpoint = manager.get_endpoint(endpoint_name)

        if not endpoint:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

        if config.output_format != "table":
            formatter.print(endpoint)
            return

        spec = endpoint.get("spec", {})
        print(f"\n  Endpoint: {endpoint_name}")
        print("  " + "-" * 60)
        print(f"  State:     {endpoint.get('desired_state', 'unknown')}")
        print(f"  Image:     {spec.get('image', 'N/A')}")
        print(f"  Replicas:  {spec.get('replicas', 1)}")
        print(f"  GPUs:      {spec.get('gpu_count', 0)}")
        print(f"  Port:      {spec.get('port', 8000)}")
        print(f"  Path:      {endpoint.get('ingress_path', 'N/A')}")
        print(f"  Namespace: {endpoint.get('namespace', 'N/A')}")
        print(f"  Created:   {endpoint.get('created_at', 'N/A')}")

        # Region status
        region_status = endpoint.get("region_status", {})
        if region_status:
            print("\n  Region Status:")
            print(f"  {'REGION':<18} {'STATE':<12} {'READY':>5} {'DESIRED':>7} {'LAST SYNC'}")
            print("  " + "-" * 65)
            for r, status in region_status.items():
                if isinstance(status, dict):
                    r_state = status.get("state", "unknown")
                    ready = status.get("replicas_ready", 0)
                    desired = status.get("replicas_desired", 0)
                    last_sync = status.get("last_sync", "N/A")
                    if last_sync and len(last_sync) > 19:
                        last_sync = last_sync[:19]
                    print(f"  {r:<18} {r_state:<12} {ready:>5} {desired:>7} {last_sync}")
        else:
            target_regions = endpoint.get("target_regions", [])
            print(f"\n  Target regions: {', '.join(target_regions)}")
            print("  (Waiting for inference_monitor to sync)")

        print()

    except Exception as e:
        formatter.print_error(f"Failed to get endpoint status: {e}")
        sys.exit(1)


@inference.command("scale")
@click.argument("endpoint_name")
@click.option("--replicas", "-r", required=True, type=int, help="New replica count")
@pass_config
def inference_scale(config: Any, endpoint_name: Any, replicas: Any) -> None:
    """Scale an inference endpoint.

    Examples:
        gco inference scale my-llm --replicas 4
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        result = manager.scale(endpoint_name, replicas)

        if result:
            formatter.print_success(f"Endpoint '{endpoint_name}' scaled to {replicas} replicas")
        else:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to scale endpoint: {e}")
        sys.exit(1)


@inference.command("stop")
@click.argument("endpoint_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def inference_stop(config: Any, endpoint_name: Any, yes: Any) -> None:
    """Stop an inference endpoint (scale to zero, keep config).

    Examples:
        gco inference stop my-llm -y
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(f"Stop endpoint '{endpoint_name}'?", abort=True)

    try:
        manager = get_inference_manager(config)
        result = manager.stop(endpoint_name)

        if result:
            formatter.print_success(f"Endpoint '{endpoint_name}' marked for stop")
        else:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to stop endpoint: {e}")
        sys.exit(1)


@inference.command("start")
@click.argument("endpoint_name")
@pass_config
def inference_start(config: Any, endpoint_name: Any) -> None:
    """Start a stopped inference endpoint.

    Examples:
        gco inference start my-llm
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        result = manager.start(endpoint_name)

        if result:
            formatter.print_success(f"Endpoint '{endpoint_name}' marked for start")
        else:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to start endpoint: {e}")
        sys.exit(1)


@inference.command("delete")
@click.argument("endpoint_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def inference_delete(config: Any, endpoint_name: Any, yes: Any) -> None:
    """Delete an inference endpoint from all regions.

    The inference_monitor in each region will clean up the K8s resources.

    Examples:
        gco inference delete my-llm -y
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    if not yes:
        click.confirm(f"Delete endpoint '{endpoint_name}' from all regions?", abort=True)

    try:
        manager = get_inference_manager(config)
        result = manager.delete(endpoint_name)

        if result:
            formatter.print_success(
                f"Endpoint '{endpoint_name}' marked for deletion. "
                "The inference_monitor will clean up resources in each region."
            )
        else:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to delete endpoint: {e}")
        sys.exit(1)


@inference.command("update-image")
@click.argument("endpoint_name")
@click.option("--image", "-i", required=True, help="New container image")
@pass_config
def inference_update_image(config: Any, endpoint_name: Any, image: Any) -> None:
    """Update the container image for an inference endpoint.

    Triggers a rolling update across all target regions.

    Examples:
        gco inference update-image my-llm -i vllm/vllm-openai:v0.26.0
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        result = manager.update_image(endpoint_name, image)

        if result:
            formatter.print_success(f"Endpoint '{endpoint_name}' image updated to {image}")
            formatter.print_info("Rolling update will be applied by inference_monitor")
        else:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to update image: {e}")
        sys.exit(1)


@inference.command("invoke")
@click.argument("endpoint_name")
@click.option("--prompt", "-p", help="Text prompt to send")
@click.option("--data", "-d", help="Raw JSON body to send")
@click.option(
    "--path", "api_path", default=None, help="API sub-path (default: auto-detect from framework)"
)
@click.option("--region", "-r", help="Target region for the request")
@click.option(
    "--max-tokens", type=int, default=100, help="Maximum tokens to generate (default: 100)"
)
@click.option(
    "--stream/--no-stream",
    default=None,
    help="Enable or disable incremental response streaming. Raw JSON with "
    "'stream': true enables streaming automatically.",
)
@pass_config
def inference_invoke(
    config: Any,
    endpoint_name: Any,
    prompt: Any,
    data: Any,
    api_path: Any,
    region: Any,
    max_tokens: Any,
    stream: Any,
) -> None:
    """Send a request to an inference endpoint and print the response.

    Automatically discovers the endpoint's stored API path (the legacy
    ``ingress_path`` record field) and routes the request through API Gateway
    with SigV4 authentication.

    Examples:
        gco inference invoke my-llm -p "What is GPU orchestration?"

        gco inference invoke my-llm -d '{"prompt": "Hello", "max_tokens": 50}'

        gco inference invoke my-llm -p "Explain K8s" --path /v1/completions
    """
    import json as _json

    from ..aws_client import get_aws_client
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    if not prompt and not data:
        formatter.print_error("Provide --prompt (-p) or --data (-d)")
        sys.exit(1)

    try:
        # Look up the endpoint's stored API prefix and serving spec. The record
        # retains the historical ``ingress_path`` field name for compatibility;
        # requests still traverse only the shared authenticated Ingress.
        manager = get_inference_manager(config)
        endpoint = manager.get_endpoint(endpoint_name)
        if not endpoint:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

        endpoint_path = endpoint.get("ingress_path", f"/inference/{endpoint_name}")
        spec = endpoint.get("spec", {})
        image = spec.get("image", "") if isinstance(spec, dict) else ""

        parsed_data: dict[str, Any] | None = None
        if data:
            parsed_json = _json.loads(data)
            if not isinstance(parsed_json, dict):
                raise ValueError("--data must contain a JSON object")
            parsed_data = parsed_json

        # An explicit flag wins over the body. Without a flag, raw OpenAI JSON
        # can opt into streamed transport by carrying its normal stream field.
        if stream is None:
            stream_response = parsed_data is not None and parsed_data.get("stream") is True
        else:
            stream_response = bool(stream)
        if parsed_data is not None and stream is not None:
            parsed_data["stream"] = stream_response

        # Auto-detect the API sub-path based on the container image. TGI uses a
        # distinct route for streamed token delivery; OpenAI-compatible servers
        # use the same route and select streaming in the JSON body.
        if api_path is None:
            if "vllm" in image:
                api_path = "/v1/completions"
            elif "text-generation-inference" in image or "tgi" in image:
                api_path = "/generate_stream" if stream_response else "/generate"
            elif "tritonserver" in image or "triton" in image:
                api_path = "/v2/models"
            else:
                api_path = "/v1/completions"

        full_path = f"{endpoint_path}{api_path}"

        # Build the request body.
        body: dict[str, Any]
        if parsed_data is not None:
            body = parsed_data
        else:
            assert prompt is not None
            if "generate" in api_path:
                # TGI format; /generate_stream controls response streaming.
                body = {"inputs": prompt, "parameters": {"max_new_tokens": max_tokens}}
            elif "/v2/" in api_path:
                # Triton — just list models, prompt not used for this path.
                body = {}
            else:
                # OpenAI-compatible (vLLM, etc.)
                # Determine model name for OpenAI-compatible request
                model_name = endpoint_name
                if isinstance(spec, dict):
                    # Check env vars first
                    model_name = spec.get("env", {}).get("MODEL", model_name)
                    # Check container args for --model (vLLM, etc.)
                    args_list = spec.get("args") or []
                    for i, arg in enumerate(args_list):
                        if arg == "--model" and i + 1 < len(args_list):
                            model_name = args_list[i + 1]
                            break
                    # Default for vLLM with no explicit model — auto-detect
                    # by querying /v1/models on the running endpoint
                    if model_name == endpoint_name and "vllm" in image:
                        try:
                            detect_client = get_aws_client(config)
                            models_path = f"/inference/{endpoint_name}/v1/models"
                            models_resp = detect_client.make_authenticated_request(
                                method="GET",
                                path=models_path,
                                target_region=region,
                            )
                            if models_resp.ok:
                                models_data = models_resp.json().get("data", [])
                                if models_data:
                                    model_name = models_data[0]["id"]
                        except Exception:
                            pass  # Fall through to endpoint_name as model
                body = {
                    "model": model_name,
                    "prompt": prompt,
                    "max_tokens": max_tokens,
                    "stream": stream_response,
                }

        if stream_response:
            # Keep streamed stdout byte-for-byte pipeline-friendly; request
            # metadata belongs on stderr when the response itself is streamed.
            print(f"ℹ POST {full_path}", file=sys.stderr)
        else:
            formatter.print_info(f"POST {full_path}")

        # Make the authenticated request. ``stream=True`` prevents requests
        # from preloading the body so chunks can reach stdout as they arrive.
        client = get_aws_client(config)
        response = client.make_authenticated_request(
            method="POST",
            path=full_path,
            body=body,
            target_region=region,
            stream=stream_response,
        )

        if stream_response:
            try:
                if not response.ok:
                    formatter.print_error(f"HTTP {response.status_code}: {response.text[:500]}")
                    sys.exit(1)

                # Requests assumes ISO-8859-1 for text/* without a declared
                # charset. Model token streams are UTF-8 in practice, so honor
                # only an explicit response charset and otherwise use UTF-8.
                content_type = response.headers.get("content-type", "")
                encoding = "utf-8"
                if isinstance(content_type, str):
                    parsed_content_type = Message()
                    parsed_content_type["content-type"] = content_type
                    declared_charset = parsed_content_type.get_content_charset()
                    if declared_charset is not None:
                        try:
                            codecs.lookup(declared_charset)
                        except LookupError:
                            pass
                        else:
                            encoding = declared_charset

                decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
                for chunk in response.iter_content(chunk_size=8192, decode_unicode=False):
                    if not chunk:
                        continue
                    output = chunk if isinstance(chunk, str) else decoder.decode(chunk)
                    if output:
                        sys.stdout.write(output)
                        sys.stdout.flush()
                remainder = decoder.decode(b"", final=True)
                if remainder:
                    sys.stdout.write(remainder)
                    sys.stdout.flush()
            finally:
                response.close()
            return

        # Buffered responses retain the friendly extraction used by the CLI.
        if response.ok:
            try:
                resp_json = response.json()
                # Extract the generated text for common formats
                text = None
                if "choices" in resp_json:
                    # OpenAI format
                    choices = resp_json["choices"]
                    if choices:
                        text = choices[0].get("text") or choices[0].get("message", {}).get(
                            "content"
                        )
                elif "generated_text" in resp_json:
                    # TGI format
                    text = resp_json["generated_text"]
                elif isinstance(resp_json, list) and resp_json and "generated_text" in resp_json[0]:
                    text = resp_json[0]["generated_text"]

                if text and config.output_format == "table":
                    print(f"\n{text.strip()}\n")
                else:
                    print(_json.dumps(resp_json, indent=2))
            except _json.JSONDecodeError:
                print(response.text)
        else:
            formatter.print_error(f"HTTP {response.status_code}: {response.text[:500]}")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to invoke endpoint: {e}")
        sys.exit(1)


@inference.command("canary")
@click.argument("endpoint_name")
@click.option("--image", "-i", required=True, help="New container image for canary")
@click.option(
    "--weight",
    "-w",
    default=10,
    type=int,
    help="Percentage of traffic to canary (1-99, default: 10)",
)
@click.option(
    "--replicas", "-r", default=1, type=int, help="Number of canary replicas (default: 1)"
)
@pass_config
def inference_canary(
    config: Any, endpoint_name: Any, image: Any, weight: Any, replicas: Any
) -> None:
    """Start a canary deployment with a new image.

    Routes a percentage of traffic to the canary while the primary
    continues serving the rest. Use 'promote' to make the canary
    the new primary, or 'rollback' to remove it.

    Examples:
        gco inference canary my-llm -i vllm/vllm-openai:v0.26.0 --weight 10
        gco inference canary my-llm -i new-image:latest -w 25 -r 2
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        result = manager.canary_deploy(endpoint_name, image, weight=weight, replicas=replicas)

        if not result:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

        formatter.print_success(
            f"Canary started: {weight}% traffic → {image} ({replicas} replica(s))"
        )
        formatter.print_info(f"Monitor with: gco inference status {endpoint_name}")
        formatter.print_info(f"Promote with: gco inference promote {endpoint_name}")
        formatter.print_info(f"Rollback with: gco inference rollback {endpoint_name}")

    except ValueError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to start canary: {e}")
        sys.exit(1)


@inference.command("promote")
@click.argument("endpoint_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def inference_promote(config: Any, endpoint_name: Any, yes: Any) -> None:
    """Promote the canary to primary.

    Replaces the primary image with the canary image and removes
    the canary deployment. All traffic goes to the new image.

    Examples:
        gco inference promote my-llm -y
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        endpoint = manager.get_endpoint(endpoint_name)

        if not endpoint:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

        canary = endpoint.get("spec", {}).get("canary")
        if not canary:
            formatter.print_error(f"Endpoint '{endpoint_name}' has no active canary")
            sys.exit(1)

        if not yes:
            current_image = endpoint.get("spec", {}).get("image", "unknown")
            click.echo(f"  Current primary: {current_image}")
            click.echo(f"  Canary image:    {canary.get('image', 'unknown')}")
            click.echo(f"  Canary weight:   {canary.get('weight', 0)}%")
            if not click.confirm("  Promote canary to primary?"):
                formatter.print_info("Cancelled")
                return

        result = manager.promote_canary(endpoint_name)
        if result:
            new_image = result.get("spec", {}).get("image", "unknown")
            formatter.print_success(f"Promoted: all traffic now serving {new_image}")
        else:
            formatter.print_error("Promotion failed")
            sys.exit(1)

    except ValueError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to promote canary: {e}")
        sys.exit(1)


@inference.command("rollback")
@click.argument("endpoint_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@pass_config
def inference_rollback(config: Any, endpoint_name: Any, yes: Any) -> None:
    """Remove the canary deployment, keeping the primary unchanged.

    All traffic returns to the primary deployment.

    Examples:
        gco inference rollback my-llm -y
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        endpoint = manager.get_endpoint(endpoint_name)

        if not endpoint:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

        canary = endpoint.get("spec", {}).get("canary")
        if not canary:
            formatter.print_error(f"Endpoint '{endpoint_name}' has no active canary")
            sys.exit(1)

        if not yes:
            click.echo(f"  Canary image:  {canary.get('image', 'unknown')}")
            click.echo(f"  Canary weight: {canary.get('weight', 0)}%")
            if not click.confirm("  Remove canary and restore full traffic to primary?"):
                formatter.print_info("Cancelled")
                return

        result = manager.rollback_canary(endpoint_name)
        if result:
            primary_image = result.get("spec", {}).get("image", "unknown")
            formatter.print_success(f"Rolled back: all traffic now serving {primary_image}")
        else:
            formatter.print_error("Rollback failed")
            sys.exit(1)

    except ValueError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to rollback canary: {e}")
        sys.exit(1)


@inference.command("health")
@click.argument("endpoint_name")
@click.option("--region", "-r", help="Target region to check")
@pass_config
def inference_health(config: Any, endpoint_name: Any, region: Any) -> None:
    """Check if an inference endpoint is healthy and ready to serve.

    Hits the endpoint's health check path and reports status and latency.

    Examples:
        gco inference health my-llm

        gco inference health my-llm -r us-east-1
    """
    import json as _json
    import time as _time

    from ..aws_client import get_aws_client
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        endpoint = manager.get_endpoint(endpoint_name)
        if not endpoint:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

        endpoint_path = endpoint.get("ingress_path", f"/inference/{endpoint_name}")
        spec = endpoint.get("spec", {})
        health_path = (
            spec.get("health_check_path", "/health") if isinstance(spec, dict) else "/health"
        )
        full_path = f"{endpoint_path}{health_path}"

        client = get_aws_client(config)
        start = _time.monotonic()
        response = client.make_authenticated_request(
            method="GET",
            path=full_path,
            target_region=region,
        )
        latency_ms = (_time.monotonic() - start) * 1000

        result = {
            "endpoint": endpoint_name,
            "status": "healthy" if response.ok else "unhealthy",
            "http_status": response.status_code,
            "latency_ms": round(latency_ms, 1),
            "path": full_path,
        }

        try:
            result["body"] = response.json()
        except Exception:
            result["body"] = response.text[:200] if response.text else None

        if config.output_format == "json":
            print(_json.dumps(result, indent=2))
        else:
            status_icon = "✓" if response.ok else "✗"
            formatter.print_info(
                f"{status_icon} {endpoint_name}: {result['status']} "
                f"(HTTP {response.status_code}, {result['latency_ms']}ms)"
            )

    except Exception as e:
        formatter.print_error(f"Health check failed: {e}")
        sys.exit(1)


@inference.command("models")
@click.argument("endpoint_name")
@click.option("--region", "-r", help="Target region to query")
@pass_config
def inference_models(config: Any, endpoint_name: Any, region: Any) -> None:
    """List models loaded on an inference endpoint.

    Queries the /v1/models path (OpenAI-compatible) to discover loaded models.

    Examples:
        gco inference models my-llm
    """
    import json as _json

    from ..aws_client import get_aws_client
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        endpoint = manager.get_endpoint(endpoint_name)
        if not endpoint:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

        ingress_path = endpoint.get("ingress_path", f"/inference/{endpoint_name}")
        full_path = f"{ingress_path}/v1/models"

        client = get_aws_client(config)
        response = client.make_authenticated_request(
            method="GET",
            path=full_path,
            target_region=region,
        )

        if response.ok:
            try:
                resp_json = response.json()
                print(_json.dumps(resp_json, indent=2))
            except _json.JSONDecodeError:
                print(response.text)
        else:
            formatter.print_error(f"HTTP {response.status_code}: {response.text[:500]}")
            sys.exit(1)

    except Exception as e:
        formatter.print_error(f"Failed to list models: {e}")
        sys.exit(1)


@inference.command("set-topology")
@click.argument("endpoint_name")
@click.option(
    "--prefill",
    required=True,
    type=int,
    help="Prefill (X) instance count for the XpYd topology.",
)
@click.option(
    "--decode",
    required=True,
    type=int,
    help="Decode (Y) instance count for the XpYd topology.",
)
@pass_config
def inference_set_topology(config: Any, endpoint_name: Any, prefill: Any, decode: Any) -> None:
    """Resize a disaggregated endpoint's prefill/decode topology.

    Updates the endpoint's prefill (X) and decode (Y) instance counts and
    re-triggers reconciliation so each region's monitor adjusts the role
    replica counts. Both counts must be integers in the range 1..1000.

    Examples:
        gco inference set-topology llama-pd --prefill 3 --decode 2
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        result = manager.set_topology(endpoint_name, prefill, decode)

        if result:
            formatter.print_success(
                f"Endpoint '{endpoint_name}' topology set to {prefill}p{decode}d"
            )
            formatter.print_info(
                "The inference_monitor will adjust prefill and decode "
                "replica counts in each region."
            )
            if config.output_format != "table":
                formatter.print(result)
        else:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

    except ValueError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to set topology: {e}")
        sys.exit(1)


@inference.command("configure-store")
@click.argument("endpoint_name")
@click.option(
    "--cold-tier/--no-cold-tier",
    "cold_tier",
    default=None,
    help="Opt the endpoint into (or out of) the asynchronous S3 cold tier. "
    "Enabling it also enables the shared store it extends.",
)
@click.option(
    "--offload",
    type=click.Choice(["cpu", "disk", "none"]),
    default=None,
    help="KV-store offload tier for spilling cache beyond GPU memory.",
)
@click.option(
    "--global-segment-size",
    type=int,
    default=None,
    help="Global segment size in bytes for the KV-cache store.",
)
@click.option(
    "--local-buffer-size",
    type=int,
    default=None,
    help="Local buffer size in bytes for the KV-cache store.",
)
@click.option(
    "--enable-store/--disable-store",
    "enabled",
    default=None,
    help="Enable or disable the shared KV-cache store.",
)
@pass_config
def inference_configure_store(
    config: Any,
    endpoint_name: Any,
    cold_tier: Any,
    offload: Any,
    global_segment_size: Any,
    local_buffer_size: Any,
    enabled: Any,
) -> None:
    """Update the shared KV-cache store on a Mooncake endpoint.

    Merges the given settings into the endpoint's existing KV-cache store
    configuration and re-triggers reconciliation so each region's monitor picks
    up the change. Enabling the cold tier also enables the shared store it
    extends. Use 'gco inference populate-kv' to pre-warm the cold tier.

    Examples:
        gco inference configure-store my-llm --cold-tier
        gco inference configure-store my-llm --offload cpu --local-buffer-size 2147483648
    """
    from ..inference import get_inference_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_inference_manager(config)
        endpoint = manager.get_endpoint(endpoint_name)
        if not endpoint:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

        # Merge onto the endpoint's current store block so changing one field
        # does not drop the others.
        spec = endpoint.get("spec", {}) if isinstance(endpoint, dict) else {}
        mooncake = spec.get("mooncake", {}) if isinstance(spec, dict) else {}
        store_config = dict(mooncake.get("store") or {})

        if enabled is not None:
            store_config["enabled"] = enabled
        if cold_tier is not None:
            store_config["cold_tier_enabled"] = cold_tier
            if cold_tier:
                # The cold tier extends the shared store, so enabling it enables
                # the store too.
                store_config["enabled"] = True
        if offload is not None:
            store_config["offload"] = offload
        if global_segment_size is not None:
            store_config["global_segment_size"] = global_segment_size
        if local_buffer_size is not None:
            store_config["local_buffer_size"] = local_buffer_size

        if not store_config:
            formatter.print_error(
                "No store settings given. Pass --cold-tier, --offload, "
                "--global-segment-size, --local-buffer-size, or --enable-store."
            )
            sys.exit(1)

        result = manager.configure_store(endpoint_name, store_config)
        if result:
            formatter.print_success(f"Endpoint '{endpoint_name}' store configuration updated")
            formatter.print_info(
                "The inference_monitor will re-render the KV-cache store "
                "configuration in each region."
            )
            if config.output_format != "table":
                formatter.print(result)
        else:
            formatter.print_error(f"Endpoint '{endpoint_name}' not found")
            sys.exit(1)

    except ValueError as e:
        formatter.print_error(str(e))
        sys.exit(1)
    except Exception as e:
        formatter.print_error(f"Failed to configure store: {e}")
        sys.exit(1)


@inference.command("populate-kv")
@click.argument("endpoint_name")
@click.argument("local_path")
@click.option(
    "--region",
    "-r",
    required=True,
    help="Region whose general-purpose bucket backs the endpoint's KV-cache cold tier.",
)
@pass_config
def inference_populate_kv(config: Any, endpoint_name: Any, local_path: Any, region: Any) -> None:
    """Upload data into an endpoint's Mooncake KV-cache cold tier.

    Uploads a local file or directory to the region's general-purpose bucket
    under the cold-tier key prefix the endpoint reads from
    (mooncake-kv/<endpoint>/). The endpoint must be deployed with the cold tier
    enabled (deploy with --mooncake-cold-tier, or run
    'gco inference configure-store <name> --cold-tier') for its pods to read the
    uploaded data.

    Examples:
        gco inference populate-kv my-llm ./kv-warm-set/ --region us-east-1
    """
    from ..models import get_regional_bucket_manager

    formatter = get_output_formatter(config)

    try:
        manager = get_regional_bucket_manager(config)
        formatter.print_info(
            f"Uploading {local_path} into the KV-cache cold tier for "
            f"'{endpoint_name}' in '{region}'..."
        )
        result = manager.populate_kv_cache(local_path, region, endpoint_name)

        formatter.print_success(
            f"Uploaded {result['files_uploaded']} file(s) to {result['s3_uri']}"
        )
        formatter.print_info(
            "Pods for this endpoint read the cold tier when it is enabled "
            "(deploy with --mooncake-cold-tier or 'gco inference configure-store')."
        )

        if config.output_format != "table":
            formatter.print(result)

    except Exception as e:
        formatter.print_error(f"Failed to populate KV cache: {e}")
        sys.exit(1)
