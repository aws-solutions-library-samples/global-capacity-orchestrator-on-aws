import assert from "node:assert/strict";
import { Readable } from "node:stream";
import test from "node:test";

import {
  __test,
  CollectingWritable,
  responseMetadata,
} from "./support.mjs";

test("upstream responses preserve backpressure, bytes, and Lambda metadata", async () => {
  const chunks = Array.from({ length: 24 }, (_, index) =>
    Buffer.alloc(64, index),
  );
  const upstream = Readable.from(chunks, {
    highWaterMark: 16,
    objectMode: false,
  });
  upstream.statusCode = 206;
  upstream.headers = {
    "Content-Type": "application/octet-stream",
    "Content-Length": String(chunks.length * chunks[0].length),
    "Transfer-Encoding": "chunked",
    Connection: "keep-alive",
    "X-Upstream": ["one", "two"],
  };

  let cleanupCalls = 0;
  let destroyCalls = 0;
  const resource = {
    response: upstream,
    cleanup() {
      cleanupCalls += 1;
    },
    destroy(error) {
      destroyCalls += 1;
      upstream.destroy(error);
    },
  };
  const downstream = new CollectingWritable({
    delayMs: 1,
    highWaterMark: 16,
  });
  const responseState = { started: false };

  await __test.streamFinalResponse(
    resource,
    downstream,
    responseState,
    new AbortController().signal,
  );

  assert.equal(responseState.started, true);
  assert.deepEqual(downstream.buffer(), Buffer.concat(chunks));
  assert.ok(downstream.maxWritableLength > 0);
  assert.ok(downstream.maxWritableLength <= chunks[0].length);
  assert.deepEqual(responseMetadata.get(downstream), {
    statusCode: 206,
    headers: {
      "content-type": "application/octet-stream",
      "x-upstream": "one, two",
    },
  });
  assert.equal(cleanupCalls, 1);
  assert.equal(destroyCalls, 0);
});

test("JSON errors are finite, metadata-framed, and omit internal details", async () => {
  const downstream = new CollectingWritable();

  await __test.sendJsonError(
    downstream,
    429,
    "Try again later",
    { "retry-after": "3" },
    { requestId: "public-request-id" },
  );

  const body = downstream.text();
  assert.ok(Buffer.byteLength(body, "utf8") <= 256);
  assert.deepEqual(JSON.parse(body), {
    error: "Try again later",
    requestId: "public-request-id",
  });
  assert.equal(body.includes("stack"), false);
  assert.deepEqual(responseMetadata.get(downstream), {
    statusCode: 429,
    headers: {
      "content-type": "application/json",
      "retry-after": "3",
    },
  });
  assert.equal(downstream.listenerCount("close"), 0);
  assert.equal(downstream.listenerCount("error"), 0);

  const disconnected = new CollectingWritable();
  disconnected.destroy();
  await __test.sendJsonError(disconnected, 500, "Internal server error");
  assert.equal(responseMetadata.has(disconnected), false);
  assert.equal(disconnected.chunks.length, 0);
});
