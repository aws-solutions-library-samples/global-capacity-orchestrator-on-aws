# Vector Ingest Lambda

The vector-ingest Lambda is the write half of the **globally replicated
vector store** - an **opt-in add-on to the GCO global stack** (not a
separate stack). It is **disabled by default** (`vector_store.enabled:
false` in `cdk.json`); set that flag to `true` to provision it.

When an object lands under the corpus prefix (default `vector-corpus/`)
of the always-on `Cluster_Shared_Bucket`, an [S3 event
notification](https://docs.aws.amazon.com/AmazonS3/latest/userguide/EventNotifications.html)
invokes this handler asynchronously. The handler chunks the document,
embeds each chunk with the configured
[Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html)
text-embedding model, and writes the vectors to the
`{project}-vector-store`
[DynamoDB global table](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GlobalTables.html)
in the global region; replication fans the items (and the
`corpus-embedding-index` vector index) out to every replica region so
workloads query locally.

## Table of Contents

- [How it fits in](#how-it-fits-in)
- [Supported formats and chunking](#supported-formats-and-chunking)
- [Item shape](#item-shape)
- [Idempotency and corpus lifecycle](#idempotency-and-corpus-lifecycle)
- [Failure handling](#failure-handling)
- [Configuration](#configuration)
- [IAM permissions](#iam-permissions)
- [Local testing](#local-testing)

## How it fits in

This [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html),
its S3 bucket notification (prefix-filtered to the corpus prefix), and
its async-failure dead-letter queue are created by
`GCOGlobalStack._create_vector_ingest()` (in
`gco/stacks/global_stack.py`) when `vector_store.enabled` is true; the
table and vector index come from `_create_vector_store()` in the same
stack. The handler is self-contained (boto3 + stdlib only) and is
packaged via `Code.from_asset("lambda/vector-ingest")`.

Uploading the corpus is a plain S3 write - `gco vector ingest` wraps it,
but `aws s3 cp docs/ s3://<cluster-shared-bucket>/vector-corpus/ --recursive`
works identically.

## Supported formats and chunking

| Suffix | Path |
|--------|------|
| `.txt`, `.md` | Paragraph chunker: blank-line-separated paragraphs packed greedily up to ~2000 characters; an oversized paragraph is hard-split at exactly 2000. The first Markdown ATX heading (when the document opens with one) becomes every chunk's `title`. |
| `.jsonl` | Pre-chunked path: each line is `{"text": "...", "title": "optional"}`; each `text` must fit one chunk (2000 chars). A malformed line fails the whole object. |
| anything else | Skipped (counted in the summary), so dropping a PDF or image into the prefix is harmless. |

Chunking is a pure function of the object bytes, which is what makes
re-delivery and re-upload idempotent (see below).

## Item shape

One item per chunk, keyed by a deterministic id:

```text
doc_id         = sha256(object_key)[:16] + "#" + chunk_index (zero-padded, e.g. "1a2b3c4d5e6f7a8b#0003")
text           = the chunk text
source         = the full S3 object key
chunk_index    = numeric position within the document
title          = first Markdown heading (when present)
embedding      = the vector, as a DynamoDB number list
embedding_model_id = model that produced the vector (provenance)
content_sha256 = digest of the source object bytes (re-ingest audits)
ingested_at    = ISO-8601 timestamp
```

The `corpus-embedding-index` vector index projects `text`, `source`,
`chunk_index`, `title`, and `embedding_model_id`, with `source` filterable
inline (`gco vector search --source ...`).

## Idempotency and corpus lifecycle

`doc_id` is deterministic, so S3's at-least-once event delivery and
re-uploads of the same key overwrite items in place. Two limits are
deliberate (documented, not handled):

- **Deleting an S3 object does not delete its items.** The store is
  additive; remove stale content by re-creating the corpus (empty the
  prefix, re-upload, and re-ingest) or by deleting items directly.
- **A shrinking object leaves tail chunks behind.** If a document goes
  from 10 chunks to 3, chunks 4-9 survive until the corpus is
  re-ingested.

Embedding-model drift has the same flavor: vectors are only comparable
to vectors from the same model, so adopting a newer
`vector_store.embedding_model_id` means re-ingesting the corpus. The
monthly dependency scan tracks the pin and says exactly this when it
flags a newer same-family model.

## Failure handling

Objects are isolated: one undecodable, oversized, or wrong-width object
never blocks the rest of the batch. Every invocation logs a single JSON
summary line (ingested/skipped/failed per object). If any object failed,
the handler re-raises after the batch so Lambda's async retry (twice)
and then the dead-letter queue see the failure; retries are safe because
succeeded objects overwrite idempotently.

A vector-width mismatch (model output vs. `vector_store.dimensions`) is
a hard per-object error with remediation text - the index width is a
one-way door, and a wrong-width vector must never reach the table.

## Configuration

The stack supplies everything through the environment; there is nothing
to configure on the function directly. All knobs live under the
`vector_store` block in `cdk.json`:

| Environment variable | Source |
|----------------------|--------|
| `VECTOR_STORE_TABLE_NAME` | the `{project}-vector-store` table |
| `EMBEDDING_MODEL_ID` | `vector_store.embedding_model_id` (default `amazon.titan-embed-text-v2:0`) |
| `EMBEDDING_DIMENSIONS` | `vector_store.dimensions` (default 1024) |
| `CORPUS_PREFIX` | `vector_store.corpus_prefix` (default `vector-corpus/`) |

The `dimensions` request key is sent only to model families known to
accept it (Titan Text Embeddings V2); other families get the bare
`inputText` body and the width verification arbitrates.

## IAM permissions

The dedicated execution role carries the write-only identity of the
pipeline:

- `s3:GetObject` on the cluster-shared bucket's corpus prefix (plus
  `kms:Decrypt` via the bucket's customer-managed key),
- `dynamodb:PutItem` on the vector-store table,
- `bedrock:InvokeModel` on the configured embedding model,
- CloudWatch Logs via `AWSLambdaBasicExecutionRole`.

It deliberately has **no** read path (`SearchVectors`, `GetItem`,
`Query`) - querying belongs to the regional workload role and the CLI.

## Local testing

`tests/test_vector_ingest_handler.py` covers the chunker, the Titan
request contract, width verification, per-object isolation, and the
summary/raise semantics with stubbed clients (loaded through
`tests/_lambda_imports.load_lambda_module`, so no packaging step is
needed). `tests/test_floci_vector_ingest.py` runs the same handler
against a local [Floci](https://github.com/floci-io/floci) emulator -
real S3 `get_object` and DynamoDB `put_item` over the wire with only the
Bedrock embedding stubbed (Floci does not emulate Bedrock); see
`docs/FLOCI_TESTING.md`.
