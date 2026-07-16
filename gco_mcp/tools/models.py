"""Model weight management MCP tools."""

import asyncio
import contextlib
import json

import cli_runner
from audit import audit_logged
from feature_flags import FLAG_DESTRUCTIVE_OPERATIONS, FLAG_MODEL_UPLOAD, is_enabled
from local_data import resolve_local_path, stage_upload_path
from server import mcp


@mcp.tool(tags={"safe", "models"})
@audit_logged
def list_models() -> str:
    """List all uploaded model weights in the S3 bucket."""
    return cli_runner._run_cli("models", "list")


@mcp.tool(tags={"safe", "models"})
@audit_logged
def get_model_uri(model_name: str) -> str:
    """Get the S3 URI for a model (for use with --model-source).

    Args:
        model_name: Name of the model.
    """
    return cli_runner._run_cli("models", "uri", model_name)


async def _ctx_warning(message: str) -> None:
    """Emit ``ctx.warning(...)`` from inside a tool body, no-op when no Context."""
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
    except Exception:
        return
    with contextlib.suppress(Exception):
        await ctx.warning(message)


# =============================================================================
# Model upload — gated by GCO_ENABLE_MODEL_UPLOAD
# =============================================================================


if is_enabled(FLAG_MODEL_UPLOAD):

    @mcp.tool(tags={"data-upload", "models"})
    @audit_logged
    async def models_upload(
        model_name: str,
        source_path: str,
    ) -> str:
        """[gated by GCO_ENABLE_MODEL_UPLOAD] data-upload.

        Upload model weights from a source confined beneath
        ``GCO_STORAGE_LOCAL_ROOT``. Relative paths are resolved beneath that
        root rather than the MCP process's working directory. The source is
        exposed to the CLI through a private descriptor-backed snapshot;
        descendant links, special files, hard links, and filesystem crossings
        fail closed.

        Args:
            model_name: Model name in the registry.
            source_path: Root-relative local file or directory to upload.
        """
        try:
            local_contract = resolve_local_path(
                source_path,
                require_exists=True,
                purpose="Model upload",
            )
        except (OSError, ValueError) as exc:
            return json.dumps({"error": str(exc), "code": "local_data_path_rejected"})

        def _upload_from_staged_path() -> str:
            with stage_upload_path(local_contract) as staged:
                return cli_runner._run_cli(
                    "models",
                    "upload",
                    staged.argument,
                    "--name",
                    model_name,
                    pass_fds=(staged.directory_fd,),
                )

        try:
            # Keep staging, descriptor inheritance, subprocess execution, and
            # cleanup in one worker. If the awaiting MCP request is cancelled,
            # the worker retains the private snapshot until the CLI exits.
            return await asyncio.to_thread(_upload_from_staged_path)
        except (OSError, ValueError) as exc:
            return json.dumps({"error": str(exc), "code": "local_data_path_rejected"})


# =============================================================================
# Destructive tools — gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS
# =============================================================================


if is_enabled(FLAG_DESTRUCTIVE_OPERATIONS):

    @mcp.tool(tags={"destructive", "models"})
    @audit_logged
    async def delete_model(model_name: str) -> str:
        """[gated by GCO_ENABLE_DESTRUCTIVE_OPERATIONS] destructive.

        `gco models delete` — delete a model from the central S3 bucket.
        Cannot be undone — every file under the model's S3 prefix is
        permanently removed.

        Args:
            model_name: Name of the model to delete.
        """
        await _ctx_warning(f"Deleting model {model_name!r} — this cannot be undone.")
        return await asyncio.to_thread(cli_runner._run_cli, "models", "delete", model_name, "-y")
