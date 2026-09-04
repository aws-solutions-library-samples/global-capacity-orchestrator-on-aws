import assert from "node:assert/strict";
import test from "node:test";

import {
  __test,
  CollectingWritable,
  responseMetadata,
} from "./support.mjs";

async function withEnvironment(values, operation) {
  const previous = new Map();
  for (const [name, value] of Object.entries(values)) {
    previous.set(name, process.env[name]);
    if (value === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = value;
    }
  }
  try {
    return await operation();
  } finally {
    for (const [name, value] of previous) {
      if (value === undefined) {
        delete process.env[name];
      } else {
        process.env[name] = value;
      }
    }
  }
}

function validEvent(headers = {}) {
  return {
    requestContext: { http: { method: "GET" } },
    rawPath: "/inference/v1/models",
    headers,
    body: "",
  };
}

function requestEvent(method, body) {
  return {
    requestContext: { http: { method } },
    rawPath: "/inference/v1/models",
    headers: {},
    body,
  };
}

function dependencyTraps(calls) {
  const trap = (name) => async () => {
    calls.push(name);
    throw new Error(`${name} must not run during preflight denial`);
  };
  return {
    getSecretToken: trap("getSecretToken"),
    resolveRegionalEndpoint: trap("resolveRegionalEndpoint"),
    getTlsTransport: trap("getTlsTransport"),
    forwardRequest: trap("forwardRequest"),
  };
}

test("request bodies are byte-bounded for every supported method", async () => {
  await withEnvironment({ ROUTING_MODE: "global" }, async () => {
    const limit = __test.MAX_REQUEST_BODY_BYTES;
    for (const method of ["GET", "HEAD", "POST"]) {
      const exactBody = "x".repeat(limit);
      const parsed = __test.preflightRequest(requestEvent(method, exactBody));
      assert.equal(parsed.bodyBuffer.byteLength, limit);

      assert.throws(
        () => __test.preflightRequest(requestEvent(method, `${exactBody}x`)),
        (error) => {
          assert.equal(error.name, "PublicError");
          assert.equal(error.statusCode, 413);
          assert.equal(
            error.publicMessage,
            `Request body exceeds maximum size of ${limit} bytes`,
          );
          return true;
        },
      );
    }

    const exactUtf8Body = "é".repeat(limit / 2);
    assert.equal(
      __test.preflightRequest(requestEvent("POST", exactUtf8Body)).bodyBuffer
        .byteLength,
      limit,
    );
    assert.throws(
      () =>
        __test.preflightRequest(requestEvent("POST", `${exactUtf8Body}é`)),
      (error) => error.statusCode === 413,
    );
  });
});

test("oversized bodies are denied before any AWS-backed dependency", async () => {
  await withEnvironment({ ROUTING_MODE: "global" }, async () => {
    const calls = [];
    const downstream = new CollectingWritable();
    await __test.streamingHandler(
      requestEvent("POST", "x".repeat(__test.MAX_REQUEST_BODY_BYTES + 1)),
      downstream,
      { getRemainingTimeInMillis: () => 30_000 },
      dependencyTraps(calls),
    );

    assert.deepEqual(calls, []);
    assert.equal(responseMetadata.get(downstream).statusCode, 413);
  });
});

test("global region pinning is denied before any AWS-backed dependency", async () => {
  await withEnvironment({ ROUTING_MODE: "global" }, async () => {
    const calls = [];
    const downstream = new CollectingWritable();

    await __test.streamingHandler(
      validEvent({ "X-GCO-Target-Region": "us-west-2" }),
      downstream,
      { getRemainingTimeInMillis: () => 30_000 },
      dependencyTraps(calls),
    );

    assert.deepEqual(calls, []);
    assert.equal(responseMetadata.get(downstream).statusCode, 400);
    const body = downstream.text();
    assert.ok(Buffer.byteLength(body, "utf8") <= 512);
    assert.deepEqual(JSON.parse(body), {
      error:
        "X-GCO-Target-Region is not supported by the global endpoint; use the target region's regional API endpoint if authorized for direct access",
    });
  });
});

test("invalid routing mode is denied before authentication or discovery", async () => {
  await withEnvironment({ ROUTING_MODE: "maintenance" }, async () => {
    const calls = [];
    const downstream = new CollectingWritable();

    await __test.streamingHandler(
      validEvent(),
      downstream,
      { getRemainingTimeInMillis: () => 30_000 },
      dependencyTraps(calls),
    );

    assert.deepEqual(calls, []);
    assert.deepEqual(responseMetadata.get(downstream), {
      statusCode: 503,
      headers: { "content-type": "application/json" },
    });
    assert.deepEqual(JSON.parse(downstream.text()), {
      error: "Backend routing is temporarily unavailable",
    });
  });
});

test("the target-region header denial is global-mode specific", async () => {
  await withEnvironment({ ROUTING_MODE: "regional" }, async () => {
    const parsed = __test.preflightRequest(
      validEvent({ "X-GCO-Target-Region": "us-west-2" }),
    );
    assert.equal(parsed.mode, "regional");
    assert.equal(parsed.incomingHeaders["X-GCO-Target-Region"], "us-west-2");
  });
});

test("production dependency defaults remain behind preflight validation", async () => {
  const downstream = new CollectingWritable();

  await __test.streamingHandler(undefined, downstream, {
    getRemainingTimeInMillis: () => 30_000,
  });

  assert.deepEqual(responseMetadata.get(downstream), {
    statusCode: 400,
    headers: { "content-type": "application/json" },
  });
  assert.deepEqual(JSON.parse(downstream.text()), {
    error: "Invalid request method",
  });
  assert.equal(downstream.listenerCount("close"), 0);
  assert.equal(downstream.listenerCount("error"), 0);
});
