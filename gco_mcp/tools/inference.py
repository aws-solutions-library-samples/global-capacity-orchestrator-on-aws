"""Inference endpoint management MCP tools."""

import asyncio
import contextlib
import json
from typing import Any

import cli_runner
from audit import audit_logged
from feature_flags import FLAG_DESTRUCTIVE_OPERATIONS, is_enabled
from server import mcp


async def _ctx_warning(message: str) -> None:
    """Emit ``ctx.warning(...)`` from inside a tool body, no-op when no Context.

    The destructive ``delete_inference`` tool runs short — we don't need the
    full long-task progress stack, just an audited warning back to the
    operator (and the audit log via the middleware spy).
    """
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
    except Exception:
        return
    with contextlib.suppress(Exception):
        await ctx.warning(message)


#: Serving modes that split an endpoint into separate prefill and decode roles.
#: These endpoints carry extra Kubernetes resources (per-role Deployments, a
#: proxy, and a shared store dependency), so removing one is held behind the
#: destructive-operations flag.
_DISAGGREGATED_MODES = frozenset({"disaggregated", "both"})


def _endpoint_is_disaggregated(name: str) -> bool:
    """Return True when the named endpoint runs a split prefill/decode topology.

    Reads the endpoint's persisted spec (read-only) and inspects its
    ``mooncake`` block. A ``mode`` of ``disaggregated`` or ``both`` runs
    separate prefill and decode roles. Anything else — a plain
    single-Deployment endpoint, an endpoint without a ``mooncake`` block, or a
    lookup that cannot be parsed — is treated as not disaggregated.
    """
    raw = cli_runner._run_cli("inference", "status", name)
    try:
        data = json.loads(raw)
    except TypeError, ValueError:
        return False
    if not isinstance(data, dict):
        return False
    spec = data.get("spec")
    if not isinstance(spec, dict):
        return False
    mooncake = spec.get("mooncake")
    if not isinstance(mooncake, dict):
        return False
    return mooncake.get("mode") in _DISAGGREGATED_MODES


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def deploy_inference(
    name: str,
    image: str,
    gpu_count: int = 1,
    replicas: int = 1,
    port: int = 8000,
    region: str | None = None,
    env_vars: list[str] | None = None,
) -> str:
    """Deploy an inference endpoint across regions.

    Args:
        name: Endpoint name (e.g. my-llm).
        image: Container image (e.g. vllm/vllm-openai:v0.22.0).
        gpu_count: GPUs per replica.
        replicas: Number of replicas per region.
        port: Container port.
        region: Target region(s). Omit for all deployed regions.
        env_vars: Environment variables as KEY=VALUE strings.
    """
    args = [
        "inference",
        "deploy",
        name,
        "-i",
        image,
        "--gpu-count",
        str(gpu_count),
        "--replicas",
        str(replicas),
        "--port",
        str(port),
    ]
    if region:
        args += ["-r", region]
    for env in env_vars or []:
        args += ["-e", env]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "inference"})
@audit_logged
def list_inference_endpoints(state: str | None = None, region: str | None = None) -> str:
    """List all inference endpoints.

    Args:
        state: Filter by state (deploying, running, stopped, deleted).
        region: Filter by region.
    """
    args = ["inference", "list"]
    if state:
        args += ["--state", state]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "inference"})
@audit_logged
def inference_status(name: str) -> str:
    """Get detailed status of an inference endpoint including per-region breakdown.

    Args:
        name: Endpoint name.
    """
    return cli_runner._run_cli("inference", "status", name)


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def scale_inference(name: str, replicas: int) -> str:
    """Scale an inference endpoint.

    Args:
        name: Endpoint name.
        replicas: Target replica count.
    """
    return cli_runner._run_cli("inference", "scale", name, "--replicas", str(replicas))


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def update_inference_image(name: str, image: str) -> str:
    """Rolling update of an inference endpoint's container image.

    Args:
        name: Endpoint name.
        image: New container image.
    """
    return cli_runner._run_cli("inference", "update-image", name, "-i", image)


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def stop_inference(name: str) -> str:
    """Stop an inference endpoint (scales to zero, keeps config).

    Args:
        name: Endpoint name.
    """
    return cli_runner._run_cli("inference", "stop", name, "-y")


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def start_inference(name: str) -> str:
    """Start a stopped inference endpoint.

    Args:
        name: Endpoint name.
    """
    return cli_runner._run_cli("inference", "start", name)


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def deploy_disaggregated_inference(
    name: str,
    image: str | None = None,
    prefill: int = 1,
    decode: int = 1,
    mooncake_mode: str = "disaggregated",
    gpu_count: int = 1,
    port: int = 8000,
    region: str | None = None,
    env_vars: list[str] | None = None,
) -> str:
    """Deploy a split prefill/decode (XpYd) inference endpoint.

    Registers an endpoint that serves the prefill and decode phases on
    separate roles backed by a shared KV-cache store. When no image is
    supplied the deploy falls back to the default upstream Mooncake-enabled
    vLLM image.

    Args:
        name: Endpoint name (e.g. llama-pd).
        image: Container image. Omit to use the default upstream
            Mooncake-enabled vLLM image.
        prefill: Prefill (X) instance count for the topology.
        decode: Decode (Y) instance count for the topology.
        mooncake_mode: Serving mode: disaggregated, store, or both.
        gpu_count: GPUs per replica.
        port: Container port.
        region: Target region(s). Omit for all deployed regions.
        env_vars: Environment variables as KEY=VALUE strings.
    """
    args = [
        "inference",
        "deploy",
        name,
        "--mooncake-mode",
        mooncake_mode,
        "--prefill-replicas",
        str(prefill),
        "--decode-replicas",
        str(decode),
        "--gpu-count",
        str(gpu_count),
        "--port",
        str(port),
    ]
    if image:
        args += ["-i", image]
    if region:
        args += ["-r", region]
    for env in env_vars or []:
        args += ["-e", env]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def set_mooncake_topology(name: str, prefill: int, decode: int) -> str:
    """Resize a disaggregated endpoint's prefill/decode topology.

    Updates the prefill (X) and decode (Y) instance counts and re-triggers
    reconciliation so each region adjusts its role replica counts. Both
    counts must be integers in the range 1..1000.

    Args:
        name: Endpoint name.
        prefill: New prefill (X) instance count.
        decode: New decode (Y) instance count.
    """
    return cli_runner._run_cli(
        "inference",
        "set-topology",
        name,
        "--prefill",
        str(prefill),
        "--decode",
        str(decode),
    )


@mcp.tool(tags={"safe", "inference"})
@audit_logged
def mooncake_topology_status(name: str) -> str:
    """Show a disaggregated endpoint's per-role topology status.

    Returns the endpoint record including its role-keyed region status, so
    the prefill and decode roles can be inspected per region.

    Args:
        name: Endpoint name.
    """
    return cli_runner._run_cli("inference", "status", name)


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
async def populate_kv_cache(endpoint_name: str, local_path: str, region: str) -> str:
    """`gco inference populate-kv` — upload data into an endpoint's KV-cache cold tier.

    Uploads a local file or directory to the region's general-purpose bucket
    under the cold-tier key prefix the endpoint reads from, pre-warming its
    Mooncake KV-cache prefix cache. The endpoint must be deployed with the cold
    tier enabled (deploy with --mooncake-cold-tier, or configure it via
    configure-store) for its pods to read the uploaded data.

    Args:
        endpoint_name: Endpoint whose KV-cache cold tier receives the data.
        local_path: Local file or directory to upload.
        region: Region whose general-purpose bucket backs the cold tier.
    """
    return await asyncio.to_thread(
        cli_runner._run_cli,
        "inference",
        "populate-kv",
        endpoint_name,
        local_path,
        "-r",
        region,
    )


if is_enabled(FLAG_DESTRUCTIVE_OPERATIONS):

    @mcp.tool(tags={"destructive", "inference"})
    @audit_logged
    async def delete_inference(name: str) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        Delete an inference endpoint. Cannot be undone — the endpoint, its
        DynamoDB record, and the underlying Kubernetes resources are removed.

        Deleting a disaggregated (split prefill/decode) endpoint is refused at
        invocation when GCO_ENABLE_DESTRUCTIVE_OPERATIONS is not enabled: the
        request is rejected and no endpoint resource is removed.

        Args:
            name: Endpoint name.
        """
        if not is_enabled(FLAG_DESTRUCTIVE_OPERATIONS) and await asyncio.to_thread(
            _endpoint_is_disaggregated, name
        ):
            message = (
                f"Refusing to delete disaggregated endpoint {name!r}: destructive "
                "operations are disabled. Set GCO_ENABLE_DESTRUCTIVE_OPERATIONS=true "
                "to allow this deletion."
            )
            await _ctx_warning(message)
            return json.dumps({"error": message, "destructive_operations_disabled": True})
        await _ctx_warning(f"Deleting inference endpoint {name!r} — this cannot be undone.")
        return await asyncio.to_thread(cli_runner._run_cli, "inference", "delete", name, "-y")


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def canary_deploy(name: str, image: str, weight: int = 10) -> str:
    """Start a canary deployment (A/B test a new image version).

    Args:
        name: Endpoint name.
        image: New image to canary.
        weight: Percentage of traffic to send to canary (1-99).
    """
    return cli_runner._run_cli("inference", "canary", name, "-i", image, "--weight", str(weight))


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def promote_canary(name: str) -> str:
    """Promote canary to primary (100% traffic to new version).

    Args:
        name: Endpoint name.
    """
    return cli_runner._run_cli("inference", "promote", name, "-y")


@mcp.tool(tags={"low-risk", "inference"})
@audit_logged
def rollback_canary(name: str) -> str:
    """Rollback canary (remove canary, 100% traffic to primary).

    Args:
        name: Endpoint name.
    """
    return cli_runner._run_cli("inference", "rollback", name, "-y")


@mcp.tool(tags={"safe", "inference"})
@audit_logged
def invoke_inference(
    name: str,
    prompt: str,
    max_tokens: int = 100,
    api_path: str | None = None,
    stream: bool = False,
    region: str | None = None,
) -> str:
    """Send a prompt to an inference endpoint and return the generated text.

    Automatically discovers the endpoint's ingress path, detects the serving
    framework (vLLM, TGI, Triton), and routes the request through the API
    Gateway with SigV4 authentication.

    Use this for single-turn text completions. For multi-turn conversations
    with chat models, use chat_inference instead.

    Args:
        name: Endpoint name (e.g. my-llm).
        prompt: Text prompt to send to the model.
        max_tokens: Maximum tokens to generate (default: 100).
        api_path: Override the API sub-path (default: auto-detect from framework).
        stream: Enable streaming for lower time-to-first-token (default: false).
        region: Target region for the request (default: nearest via Global Accelerator).
    """
    args = ["inference", "invoke", name, "-p", prompt, "--max-tokens", str(max_tokens)]
    if api_path:
        args += ["--path", api_path]
    if stream:
        args.append("--stream")
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "inference"})
@audit_logged
def chat_inference(
    name: str,
    messages: list[dict[str, str]],
    max_tokens: int = 256,
    temperature: float | None = None,
    stream: bool = False,
    region: str | None = None,
) -> str:
    """Send a multi-turn chat conversation to an inference endpoint.

    Sends an OpenAI-compatible /v1/chat/completions request. Works with
    vLLM, TGI (with --api-protocol openai), and any OpenAI-compatible server.

    Each message in the list should have 'role' (system/user/assistant) and
    'content' keys.

    Args:
        name: Endpoint name (e.g. my-llm).
        messages: List of chat messages, e.g. [{"role": "user", "content": "Hello"}].
        max_tokens: Maximum tokens to generate (default: 256).
        temperature: Sampling temperature (optional, server default if omitted).
        stream: Enable streaming for lower time-to-first-token (default: false).
        region: Target region for the request.
    """
    body: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens, "stream": stream}
    if temperature is not None:
        body["temperature"] = temperature
    data_str = json.dumps(body)
    args = ["inference", "invoke", name, "-d", data_str, "--path", "/v1/chat/completions"]
    if stream:
        args.append("--stream")
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "inference"})
@audit_logged
def inference_health(name: str, region: str | None = None) -> str:
    """Check if an inference endpoint is healthy and ready to serve requests.

    Hits the endpoint's health check path and returns status and latency.
    Useful to verify readiness before sending inference requests.

    Args:
        name: Endpoint name.
        region: Target region to check (default: nearest via Global Accelerator).
    """
    args = ["inference", "health", name]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)


@mcp.tool(tags={"safe", "inference"})
@audit_logged
def list_endpoint_models(name: str, region: str | None = None) -> str:
    """List models loaded on an inference endpoint.

    Queries the endpoint's /v1/models path (OpenAI-compatible) to discover
    which models are loaded, their context length, and other metadata.
    Works with vLLM and other OpenAI-compatible servers.

    Args:
        name: Endpoint name.
        region: Target region to query.
    """
    args = ["inference", "models", name]
    if region:
        args += ["-r", region]
    return cli_runner._run_cli(*args)
