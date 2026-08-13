"""``gco vector`` — operate the globally replicated vector store.

Thin click veneer over :class:`cli.vector_store.VectorStoreClient`:
``status`` (table/replica/index state), ``ingest`` (upload documents to
the corpus prefix; the S3-triggered Lambda does the chunking and
embedding), and ``search`` (similarity query, optionally against a
specific replica region). Requires the opt-in vector-store add-on
deployed with the global stack (``vector_store.enabled`` in cdk.json).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

_VECTOR_UNAVAILABLE_HINT = (
    "The vector store is not available. The table, index, and ingest pipeline "
    "ship with the global stack when vector_store.enabled is true in cdk.json "
    "(off by default): enable it and run 'gco stacks deploy gco-global', or "
    "wait a few minutes for the vector index to finish building after the "
    "first deployment. 'gco vector status' shows where things stand."
)


def _emit_json(payload: Any, *, err: bool = False) -> None:
    """Emit ``payload`` as a single JSON line (datetime/Path safe)."""
    click.echo(json.dumps(payload, default=str), err=err)


def _emit_error(code: str, details: dict[str, Any] | None = None) -> None:
    """Emit a structured error envelope to stderr."""
    payload: dict[str, Any] = {"code": code}
    if details is not None:
        payload["details"] = details
    _emit_json(payload, err=True)


def _exit_unavailable(err: Exception) -> None:
    """Print the deployment hint and a structured envelope, then exit 1."""
    click.echo(_VECTOR_UNAVAILABLE_HINT, err=True)
    _emit_error("vector_store_unavailable", {"message": str(err)})
    raise SystemExit(1)


def _build_client(query_region: str | None = None) -> Any:
    """Construct the store client (SSM-lazy; free until first use)."""
    from ..vector_store import VectorStoreClient  # noqa: PLC0415

    return VectorStoreClient(query_region=query_region)


@click.group("vector")
def vector() -> None:
    """Semantic search over an S3-ingested document corpus.

    The corpus lives in the ``{project}-vector-store`` DynamoDB global
    table (opt-in: vector_store.enabled), replicated to every deployment
    region so workloads and searches read locally. Drop .txt/.md/.jsonl
    files under the corpus prefix of the cluster-shared bucket — or use
    'gco vector ingest' — and the ingest Lambda chunks, embeds, and
    stores them.
    """


@vector.command("status")
@click.option(
    "--region",
    default=None,
    help="Region whose replica to describe (default: the global region).",
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def vector_status_cmd(region: str | None, output: str) -> None:
    """Show table, replica, and vector-index state."""
    from ..vector_store import VectorStoreUnavailableError  # noqa: PLC0415

    try:
        status = _build_client(query_region=region).status()
    except VectorStoreUnavailableError as err:
        _exit_unavailable(err)
    except Exception as err:  # noqa: BLE001 — CLI boundary: envelope, don't traceback
        _emit_error("vector_status_failed", {"message": str(err)})
        raise SystemExit(1) from None

    if output == "table":
        click.echo(f"  table:        {status['table_name']} ({status['table_status']})")
        click.echo(f"  index:        {status['index_name']} ({status['index_status']})")
        click.echo(f"  region:       {status['region']}")
        click.echo(f"  items:        {status['item_count']}")
        for replica in status["replicas"]:
            click.echo(f"  replica:      {replica['region']} ({replica['status']})")
        if status["index_status"] not in ("ACTIVE",):
            click.echo(
                "  note:         searches answer ValidationException until the "
                "index is ACTIVE (several minutes after first deploy)"
            )
    else:
        _emit_json(status)


@vector.command("ingest")
@click.argument("files", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--demo",
    is_flag=True,
    help="Ingest the checkout's docs/*.md as a self-contained demo corpus.",
)
@click.option(
    "--wait",
    is_flag=True,
    help="Wait until every uploaded document is searchable (up to 5 minutes).",
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def vector_ingest_cmd(files: tuple[Path, ...], demo: bool, wait: bool, output: str) -> None:
    """Upload FILES (.txt/.md/.jsonl) to the corpus for ingestion.

    Uploading IS ingestion: the S3 event notification invokes the ingest
    Lambda, which chunks, embeds, and writes the items; global-table
    replication then fans them out to every replica region. Re-uploading
    a file overwrites its chunks in place.
    """
    from ..vector_store import (  # noqa: PLC0415
        VectorStoreUnavailableError,
        demo_corpus_paths,
    )

    if demo and files:
        _emit_error("vector_ingest_invalid_args", {"message": "--demo takes no FILES"})
        raise SystemExit(2)
    if not demo and not files:
        _emit_error(
            "vector_ingest_invalid_args",
            {"message": "give at least one file, or use --demo"},
        )
        raise SystemExit(2)

    try:
        paths = demo_corpus_paths() if demo else list(files)
        summary = _build_client().ingest(paths, wait_timeout_seconds=300 if wait else 0)
    except VectorStoreUnavailableError as err:
        _exit_unavailable(err)
    except Exception as err:  # noqa: BLE001 — CLI boundary: envelope, don't traceback
        _emit_error("vector_ingest_failed", {"message": str(err)})
        raise SystemExit(1) from None

    if output == "table":
        click.echo(f"  bucket:   {summary['bucket']}")
        for key in summary["uploaded"]:
            count = (summary.get("chunks_by_source") or {}).get(key)
            suffix = f"  ({count} chunks)" if count is not None else ""
            click.echo(f"  uploaded: {key}{suffix}")
        if summary.get("timed_out"):
            click.echo(
                "  note:     wait timed out before every document became "
                "searchable; ingestion continues in the background "
                "('gco vector status' to check)"
            )
        elif not wait:
            click.echo("  note:     ingestion continues asynchronously (--wait to block)")
    else:
        _emit_json(summary)
    if summary.get("timed_out"):
        raise SystemExit(1)


@vector.command("search")
@click.argument("query")
@click.option(
    "--top-k",
    type=int,
    default=5,
    show_default=True,
    help="Number of similar chunks to return.",
)
@click.option(
    "--source",
    default=None,
    help="Only return chunks from this source document (full S3 key).",
)
@click.option(
    "--region",
    default=None,
    help="Region whose replica to query (default: the global region).",
)
@click.option(
    "--output",
    type=click.Choice(["json", "table"]),
    default="json",
    show_default=True,
)
def vector_search_cmd(
    query: str, top_k: int, source: str | None, region: str | None, output: str
) -> None:
    """Search the corpus for chunks similar to QUERY."""
    from ..vector_store import VectorStoreUnavailableError  # noqa: PLC0415

    try:
        results = _build_client(query_region=region).search(query, top_k=top_k, source=source)
    except VectorStoreUnavailableError as err:
        _exit_unavailable(err)
    except Exception as err:  # noqa: BLE001 — CLI boundary: envelope, don't traceback
        _emit_error("vector_search_failed", {"message": str(err)})
        raise SystemExit(1) from None

    if output == "table":
        header = f"  {'SCORE':>6}  {'SOURCE':<44}  {'CHUNK':>5}  TEXT"
        click.echo(header)
        click.echo("  " + "-" * (len(header) - 2))
        for entry in results:
            score = entry.get("score")
            score_text = f"{score:.3f}" if isinstance(score, (int, float)) else "-"
            source_text = (entry.get("source") or "")[:44]
            chunk = entry.get("chunk_index", "-")
            text = " ".join(str(entry.get("text") or "").split())[:120]
            click.echo(f"  {score_text:>6}  {source_text:<44}  {chunk:>5}  {text}")
    else:
        _emit_json({"results": results})
