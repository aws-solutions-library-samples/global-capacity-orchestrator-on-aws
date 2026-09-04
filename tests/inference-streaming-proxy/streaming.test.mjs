import assert from "node:assert/strict";
import { Readable } from "node:stream";
import test from "node:test";

import {
  __test,
  CollectingWritable,
  responseMetadata,
} from "./support.mjs";

test("upstream responses preserve backpressure, bytes, and Lambda metadata", async () => {
  const chunks = Array.from({ length: 128 }, (_, index) =>
    Buffer.alloc(1024, index),
  );
  const upstream = Readable.from(chunks, {
    highWaterMark: 1024,
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
    highWaterMark: 1024,
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
  const framingPrefixBytes = downstream.framing().delimiterIndex + 8;
  assert.ok(
    downstream.maxWritableLength <= framingPrefixBytes + chunks[0].length,
  );
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

test("empty upstream responses still emit metadata and the required delimiter", async () => {
  const upstream = Readable.from([]);
  upstream.statusCode = 200;
  upstream.headers = { "Content-Type": "text/plain", "Content-Length": "0" };
  const resource = {
    response: upstream,
    cleanup() {},
    destroy(error) {
      upstream.destroy(error);
    },
  };
  const downstream = new CollectingWritable();
  const responseState = { started: false };

  await __test.streamFinalResponse(
    resource,
    downstream,
    responseState,
    new AbortController().signal,
  );

  assert.equal(responseState.started, true);
  assert.deepEqual(responseMetadata.get(downstream), {
    statusCode: 200,
    headers: { "content-type": "text/plain" },
  });
  assert.equal(downstream.buffer().byteLength, 0);
  assert.ok(downstream.framing().delimiterIndex < 16 * 1024);
  assert.deepEqual(
    downstream
      .rawBuffer()
      .subarray(
        downstream.framing().delimiterIndex,
        downstream.framing().delimiterIndex + 8,
      ),
    Buffer.alloc(8),
  );
});

test("response metadata must place its delimiter within the first 16 KiB", async () => {
  const downstream = new CollectingWritable();
  const oversizedHeaders = { "x-oversized": "x".repeat(16 * 1024) };
  assert.throws(
    () =>
      __test.beginStreamingResponse(downstream, {
        statusCode: 200,
        headers: oversizedHeaders,
      }),
    (error) => error instanceof __test.PublicError && error.statusCode === 502,
  );
  assert.equal(downstream.rawBuffer().byteLength, 0);

  const upstream = Readable.from(["never-written"]);
  upstream.statusCode = 200;
  upstream.headers = oversizedHeaders;
  const resource = {
    response: upstream,
    cleanupCalls: 0,
    destroyCalls: 0,
    cleanup() {
      this.cleanupCalls += 1;
    },
    destroy() {
      this.destroyCalls += 1;
    },
  };
  await assert.rejects(
    __test.streamFinalResponse(
      resource,
      new CollectingWritable(),
      { started: false },
      new AbortController().signal,
    ),
    (error) => error instanceof __test.PublicError && error.statusCode === 502,
  );
  assert.equal(resource.cleanupCalls, 1);
  assert.equal(resource.destroyCalls, 1);
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

  const failing = new CollectingWritable();
  let writes = 0;
  failing._write = (chunk, _encoding, callback) => {
    writes += 1;
    if (writes === 1) {
      failing.chunks.push(Buffer.from(chunk));
      callback();
      return;
    }
    callback(new Error("downstream write failed"));
  };
  await __test.sendJsonError(failing, 500, "Internal server error");
  assert.equal(writes, 2);
  assert.equal(responseMetadata.has(failing), true);
  assert.equal(failing.listenerCount("close"), 0);
  assert.equal(failing.listenerCount("error"), 0);

  const disconnected = new CollectingWritable();
  disconnected.destroy();
  await __test.sendJsonError(disconnected, 500, "Internal server error");
  assert.equal(responseMetadata.has(disconnected), false);
  assert.equal(disconnected.chunks.length, 0);
});

test("missing upstream status defaults the final response to 502", async () => {
  const upstream = Readable.from(["upstream body"]);
  upstream.headers = {};
  const resource = {
    response: upstream,
    cleanup() {},
    destroy(error) {
      upstream.destroy(error);
    },
  };
  const downstream = new CollectingWritable();

  await __test.streamFinalResponse(
    resource,
    downstream,
    { started: false },
    new AbortController().signal,
  );

  assert.equal(responseMetadata.get(downstream).statusCode, 502);
  assert.equal(downstream.text(), "upstream body");
});

test("aborting an active upstream stream cleans up without a second response", async () => {
  const upstream = new Readable({
    read() {},
  });
  upstream.statusCode = 200;
  upstream.headers = { "Content-Type": "text/event-stream" };
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
  const downstream = new CollectingWritable();
  const state = { started: false };
  const controller = new AbortController();

  const pending = __test.streamFinalResponse(
    resource,
    downstream,
    state,
    controller.signal,
  );
  assert.equal(state.started, true);
  controller.abort();
  await pending;

  assert.equal(cleanupCalls, 1);
  assert.equal(destroyCalls, 1);
  assert.equal(responseMetadata.get(downstream).statusCode, 200);
  assert.equal(downstream.text(), "");
});

test("synchronous JSON error framing failures are swallowed", async () => {
  let writeCalls = 0;
  let endCalls = 0;
  const downstream = {
    destroyed: false,
    write() {
      writeCalls += 1;
      throw new Error("synchronous downstream write failure");
    },
    end() {
      endCalls += 1;
    },
  };

  await __test.sendJsonError(downstream, 500, "Internal server error");

  assert.equal(writeCalls, 1);
  assert.equal(endCalls, 0);
});
