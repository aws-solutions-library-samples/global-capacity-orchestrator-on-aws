import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { Readable } from "node:stream";
import test, { afterEach } from "node:test";

import {
  __test,
  CollectingWritable,
  responseMetadata,
} from "./support.mjs";

const ENV_NAMES = ["GLOBAL_ACCELERATOR_ENDPOINT", "ROUTING_MODE"];
const ORIGINAL_ENV = new Map(
  ENV_NAMES.map((name) => [name, process.env[name]]),
);

function setEnvironment(values) {
  for (const [name, value] of Object.entries(values)) {
    if (value === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = value;
    }
  }
}

function codedError(code, message = code) {
  return Object.assign(new Error(message), { code });
}

class FakeRequest extends EventEmitter {
  constructor({ endError, onEnd } = {}) {
    super();
    this.destroyed = false;
    this.destroyError = null;
    this.endError = endError;
    this.endedBody = null;
    this.onEnd = onEnd;
    this.timeoutCalls = [];
    this.timeoutCallback = null;
  }

  setTimeout(milliseconds, callback) {
    this.timeoutCalls.push(milliseconds);
    this.timeoutCallback = milliseconds === 0 ? null : callback;
    return this;
  }

  end(body) {
    this.endedBody = body;
    if (this.endError) {
      throw this.endError;
    }
    this.onEnd?.();
  }

  destroy(error) {
    if (this.destroyed) {
      return;
    }
    this.destroyed = true;
    this.destroyError = error;
    if (error) {
      queueMicrotask(() => this.emit("error", error));
    }
  }
}

class FakeIncoming extends EventEmitter {
  constructor(statusCode = 200, headers = {}) {
    super();
    this.statusCode = statusCode;
    this.headers = headers;
    this.destroyed = false;
    this.destroyError = null;
    this.timeoutCalls = [];
    this.timeoutCallback = null;
  }

  setTimeout(milliseconds, callback) {
    this.timeoutCalls.push(milliseconds);
    this.timeoutCallback = milliseconds === 0 ? null : callback;
    return this;
  }

  destroy(error) {
    this.destroyed = true;
    this.destroyError = error;
  }
}

function upstreamArguments(signal = new AbortController().signal, overrides = {}) {
  return {
    target: {
      hostname: "backend.example.com",
      requestTarget: "/inference/v1/models?model=one",
    },
    method: "POST",
    headers: { "content-type": "application/json" },
    bodyBuffer: Buffer.from("{}"),
    transport: {
      agent: { name: "test-agent" },
      serverName: "backend.gco.internal",
      verifyServerIdentity() {},
    },
    idleTimeoutMs: 30_000,
    remainingMs: 60_000,
    signal,
    ...overrides,
  };
}

function fakeResource(statusCode = 200) {
  return {
    response: { statusCode, headers: { "content-type": "text/plain" } },
    cleanupCalls: 0,
    destroyCalls: 0,
    cleanup() {
      this.cleanupCalls += 1;
    },
    destroy() {
      this.destroyCalls += 1;
    },
  };
}

function forwardingArguments(overrides = {}) {
  return {
    target: {
      hostname: "backend.example.com",
      requestTarget: "/inference/v1/models",
    },
    method: "GET",
    headers: {},
    bodyBuffer: Buffer.alloc(0),
    transport: {},
    timeoutMs: 10_000,
    idleTimeoutMs: 30_000,
    responseStream: new CollectingWritable(),
    responseState: { started: false },
    signal: new AbortController().signal,
    ...overrides,
  };
}

function queuedOperations(items) {
  return {
    openCalls: [],
    sleepCalls: [],
    streamCalls: [],
    async openUpstream(args) {
      this.openCalls.push(args);
      const item = items.shift();
      if (item instanceof Error) {
        throw item;
      }
      return item;
    },
    async sleep(milliseconds, signal) {
      this.sleepCalls.push({ milliseconds, signal });
    },
    async streamFinalResponse(resource, responseStream, responseState, signal) {
      this.streamCalls.push({ resource, responseStream, responseState, signal });
    },
  };
}

function validEvent(overrides = {}) {
  return {
    httpMethod: "GET",
    path: "/inference/v1/models",
    headers: { Accept: "application/json", "X-Request-ID": "request-1" },
    multiValueHeaders: { Accept: ["application/json", "text/event-stream"] },
    queryStringParameters: { model: "alpha" },
    body: "",
    ...overrides,
  };
}

function baseDependencies(overrides = {}) {
  return {
    async getSecretToken() {
      return "test-signing-key";
    },
    async resolveRegionalEndpoint() {
      return "regional.example.com";
    },
    async getTlsTransport() {
      return {
        agent: { name: "agent" },
        serverName: "backend.gco.internal",
        verifyServerIdentity() {},
      };
    },
    async forwardRequest() {},
    ...overrides,
  };
}

async function assertHandlerError({
  environment,
  dependencies,
  expectedStatus,
  expectedBody,
  context = { getRemainingTimeInMillis: () => 30_000 },
  event = validEvent(),
}) {
  setEnvironment(environment);
  const downstream = new CollectingWritable();
  await __test.streamingHandler(event, downstream, context, dependencies);
  assert.equal(responseMetadata.get(downstream).statusCode, expectedStatus);
  assert.deepEqual(JSON.parse(downstream.text()), expectedBody);
  assert.equal(downstream.listenerCount("close"), 0);
  assert.equal(downstream.listenerCount("error"), 0);
}

afterEach(() => {
  for (const [name, value] of ORIGINAL_ENV) {
    if (value === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = value;
    }
  }
});

test("openUpstream pins TLS options and returns a cleanup-capable response", async () => {
  const incoming = new FakeIncoming(206, { "content-type": "application/json" });
  const state = {};
  const args = upstreamArguments();
  const factory = (options, callback) => {
    state.options = options;
    const request = new FakeRequest({
      onEnd() {
        queueMicrotask(() => callback(incoming));
      },
    });
    state.request = request;
    return request;
  };

  const resource = await __test.openUpstream(args, factory);
  assert.deepEqual(state.options, {
    protocol: "https:",
    hostname: "backend.example.com",
    port: 443,
    method: "POST",
    path: "/inference/v1/models?model=one",
    headers: { "content-type": "application/json" },
    agent: { name: "test-agent" },
    servername: "backend.gco.internal",
    rejectUnauthorized: true,
    minVersion: "TLSv1.2",
    checkServerIdentity: args.transport.verifyServerIdentity,
  });
  assert.deepEqual(state.request.endedBody, Buffer.from("{}"));
  assert.deepEqual(state.request.timeoutCalls, [30_000]);
  assert.deepEqual(incoming.timeoutCalls, [30_000]);

  resource.cleanup();
  assert.deepEqual(state.request.timeoutCalls, [30_000, 0]);
  assert.deepEqual(incoming.timeoutCalls, [30_000, 0]);
  resource.cleanup();
  assert.deepEqual(state.request.timeoutCalls, [30_000, 0]);
});

test("openUpstream rejects aborts, factory failures, protocol upgrades, and timeouts", async (t) => {
  await t.test("already aborted", async () => {
    const controller = new AbortController();
    controller.abort();
    let factoryCalled = false;
    await assert.rejects(
      __test.openUpstream(upstreamArguments(controller.signal), () => {
        factoryCalled = true;
      }),
      (error) => error instanceof __test.DownstreamAbortError,
    );
    assert.equal(factoryCalled, false);
  });

  await t.test("abort while pending", async () => {
    const controller = new AbortController();
    let request;
    const pending = __test.openUpstream(
      upstreamArguments(controller.signal),
      () => {
        request = new FakeRequest();
        return request;
      },
    );
    controller.abort();
    await assert.rejects(
      pending,
      (error) => error instanceof __test.DownstreamAbortError,
    );
    assert.equal(request.destroyed, true);
    assert.ok(request.destroyError instanceof __test.DownstreamAbortError);
  });

  await t.test("synchronous request factory error", async () => {
    await assert.rejects(
      __test.openUpstream(upstreamArguments(), () => {
        throw new Error("factory failed");
      }),
      /factory failed/,
    );
  });

  await t.test("request error", async () => {
    const failure = codedError("ECONNRESET");
    await assert.rejects(
      __test.openUpstream(upstreamArguments(), () =>
        new FakeRequest({
          onEnd() {
            queueMicrotask(() => this.emit("error", failure));
          },
        }),
      ),
      (error) => error === failure,
    );
  });

  await t.test("protocol upgrade", async () => {
    let socketDestroyed = false;
    await assert.rejects(
      __test.openUpstream(upstreamArguments(), () =>
        new FakeRequest({
          onEnd() {
            queueMicrotask(() =>
              this.emit("upgrade", {}, {
                destroy() {
                  socketDestroyed = true;
                },
              }),
            );
          },
        }),
      ),
      /Unexpected protocol upgrade/,
    );
    assert.equal(socketDestroyed, true);
  });

  await t.test("request end error", async () => {
    const failure = new Error("end failed");
    let request;
    await assert.rejects(
      __test.openUpstream(upstreamArguments(), () => {
        request = new FakeRequest({ endError: failure });
        return request;
      }),
      (error) => error === failure,
    );
    assert.equal(request.destroyed, true);
  });

  await t.test("idle timeout", async () => {
    let request;
    await assert.rejects(
      __test.openUpstream(upstreamArguments(), () => {
        request = new FakeRequest({
          onEnd() {
            queueMicrotask(() => this.timeoutCallback());
          },
        });
        return request;
      }),
      (error) => error instanceof __test.UpstreamTimeoutError,
    );
    assert.equal(request.destroyed, true);
  });

  await t.test("absolute deadline", async () => {
    await assert.rejects(
      __test.openUpstream(
        upstreamArguments(undefined, { remainingMs: 1 }),
        () => new FakeRequest(),
      ),
      (error) => error instanceof __test.UpstreamTimeoutError,
    );
  });

  await t.test("response idle timeout after headers", async () => {
    const incoming = new FakeIncoming();
    let request;
    const resource = await __test.openUpstream(
      upstreamArguments(),
      (_options, callback) => {
        request = new FakeRequest({
          onEnd() {
            queueMicrotask(() => callback(incoming));
          },
        });
        return request;
      },
    );
    incoming.timeoutCallback();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(incoming.destroyed, true);
    assert.ok(incoming.destroyError instanceof __test.UpstreamTimeoutError);
    assert.equal(request.destroyed, true);
    resource.cleanup();
  });

  await t.test("late response after rejection is destroyed", async () => {
    const failure = codedError("ECONNREFUSED");
    const incoming = new FakeIncoming();
    await assert.rejects(
      __test.openUpstream(upstreamArguments(), (_options, callback) =>
        new FakeRequest({
          onEnd() {
            queueMicrotask(() => this.emit("error", failure));
            queueMicrotask(() => callback(incoming));
          },
        }),
      ),
      (error) => error === failure,
    );
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(incoming.destroyed, true);
  });
});

test("sleep handles immediate completion, elapsed timers, and aborts", async () => {
  await __test.sleep(0, new AbortController().signal);
  await __test.sleep(1, new AbortController().signal);

  const alreadyAborted = new AbortController();
  alreadyAborted.abort();
  await assert.rejects(
    __test.sleep(10_000, alreadyAborted.signal),
    (error) => error instanceof __test.DownstreamAbortError,
  );

  const duringWait = new AbortController();
  const pending = __test.sleep(10_000, duringWait.signal);
  duringWait.abort();
  await assert.rejects(
    pending,
    (error) => error instanceof __test.DownstreamAbortError,
  );
});

test("forwardRequest retries connection errors and retryable GET responses", async () => {
  const connectionFailure = codedError("ECONNRESET");
  const connectionSuccess = fakeResource(200);
  const connectionOps = queuedOperations([
    connectionFailure,
    connectionSuccess,
  ]);
  await __test.forwardRequest(forwardingArguments(), connectionOps);
  assert.equal(connectionOps.openCalls.length, 2);
  assert.equal(connectionOps.sleepCalls.length, 1);
  assert.equal(connectionOps.sleepCalls[0].milliseconds, 300);
  assert.equal(connectionOps.streamCalls.length, 1);
  assert.equal(connectionOps.streamCalls[0].resource, connectionSuccess);

  const retryable = fakeResource(503);
  const statusSuccess = fakeResource(204);
  const statusOps = queuedOperations([retryable, statusSuccess]);
  await __test.forwardRequest(forwardingArguments(), statusOps);
  assert.equal(statusOps.openCalls.length, 2);
  assert.equal(statusOps.sleepCalls.length, 1);
  assert.equal(retryable.cleanupCalls, 1);
  assert.equal(retryable.destroyCalls, 1);
  assert.equal(statusOps.streamCalls[0].resource, statusSuccess);

  const postResponse = fakeResource(503);
  const postOps = queuedOperations([postResponse]);
  await __test.forwardRequest(
    forwardingArguments({ method: "POST" }),
    postOps,
  );
  assert.equal(postOps.openCalls.length, 1);
  assert.equal(postOps.sleepCalls.length, 0);
  assert.equal(postOps.streamCalls[0].resource, postResponse);

  const deadlineResponse = fakeResource(503);
  const deadlineOps = queuedOperations([deadlineResponse]);
  await __test.forwardRequest(
    forwardingArguments({ timeoutMs: 100 }),
    deadlineOps,
  );
  assert.equal(deadlineOps.openCalls.length, 1);
  assert.equal(deadlineOps.sleepCalls.length, 0);
  assert.equal(deadlineOps.streamCalls[0].resource, deadlineResponse);
});

test("forwardRequest maps terminal transport failures without unsafe retries", async (t) => {
  await t.test("downstream abort", async () => {
    const failure = new __test.DownstreamAbortError();
    const operations = queuedOperations([failure]);
    await assert.rejects(
      __test.forwardRequest(forwardingArguments(), operations),
      (error) => error === failure,
    );
    assert.equal(operations.sleepCalls.length, 0);
  });

  await t.test("TLS error", async () => {
    const operations = queuedOperations([codedError("EPROTO")]);
    await assert.rejects(
      __test.forwardRequest(forwardingArguments(), operations),
      (error) =>
        error instanceof __test.PublicError &&
        error.statusCode === 502 &&
        error.publicMessage === "Backend TLS verification failed",
    );
  });

  await t.test("unexpected error", async () => {
    const operations = queuedOperations([new Error("unexpected")]);
    await assert.rejects(
      __test.forwardRequest(forwardingArguments(), operations),
      (error) =>
        error instanceof __test.PublicError && error.statusCode === 500,
    );
  });

  await t.test("timeout retries exhausted", async () => {
    const operations = queuedOperations([
      new __test.UpstreamTimeoutError(),
      codedError("ETIMEDOUT"),
      codedError("GCO_UPSTREAM_TIMEOUT"),
    ]);
    await assert.rejects(
      __test.forwardRequest(forwardingArguments(), operations),
      (error) =>
        error.statusCode === 504 &&
        error.details.message === "Upstream failed after 3 attempt(s)",
    );
    assert.equal(operations.openCalls.length, 3);
    assert.equal(operations.sleepCalls.length, 2);
  });

  await t.test("connection retries exhausted", async () => {
    const operations = queuedOperations([
      codedError("ECONNREFUSED"),
      codedError("EHOSTUNREACH"),
      codedError("EAI_AGAIN"),
    ]);
    await assert.rejects(
      __test.forwardRequest(forwardingArguments(), operations),
      (error) =>
        error.statusCode === 503 &&
        error.details.message === "Upstream failed after 3 attempt(s)",
    );
  });

  await t.test("deadline cannot cover backoff", async () => {
    const operations = queuedOperations([codedError("ECONNRESET")]);
    await assert.rejects(
      __test.forwardRequest(
        forwardingArguments({ timeoutMs: 100 }),
        operations,
      ),
      (error) =>
        error.statusCode === 503 &&
        error.details.message === "Upstream failed after 1 attempt(s)",
    );
    assert.equal(operations.sleepCalls.length, 0);
  });

  await t.test("expired before first attempt", async () => {
    await assert.rejects(
      __test.forwardRequest(forwardingArguments({ timeoutMs: 0 })),
      (error) => error.statusCode === 504,
    );
  });
});

test("stream finalization fails safely before and after metadata framing", async () => {
  const metadataFailure = {
    response: Readable.from(["never-written"]),
    cleanupCalls: 0,
    destroyCalls: 0,
    cleanup() {
      this.cleanupCalls += 1;
    },
    destroy() {
      this.destroyCalls += 1;
    },
  };
  metadataFailure.response.statusCode = 200;
  metadataFailure.response.headers = {};
  await assert.rejects(
    __test.streamFinalResponse(
      metadataFailure,
      {
        write() {
          throw new Error("framing failed");
        },
      },
      { started: false },
      new AbortController().signal,
    ),
    (error) => error instanceof __test.PublicError && error.statusCode === 500,
  );
  assert.equal(metadataFailure.cleanupCalls, 1);
  assert.equal(metadataFailure.destroyCalls, 1);

  const streamFailure = {
    response: new Readable({
      read() {
        this.destroy(new Error("upstream stream failed"));
      },
    }),
    cleanupCalls: 0,
    destroyCalls: 0,
    cleanup() {
      this.cleanupCalls += 1;
    },
    destroy() {
      this.destroyCalls += 1;
    },
  };
  streamFailure.response.statusCode = 200;
  streamFailure.response.headers = {};
  const state = { started: false };
  await __test.streamFinalResponse(
    streamFailure,
    new CollectingWritable(),
    state,
    new AbortController().signal,
  );
  assert.equal(state.started, true);
  assert.equal(streamFailure.cleanupCalls, 1);
  assert.equal(streamFailure.destroyCalls, 1);
});

test("streamingHandler completes global and regional streaming requests", async () => {
  const globalCapture = {};
  const globalDependencies = baseDependencies({
    async forwardRequest(args) {
      globalCapture.args = args;
      const output = __test.beginStreamingResponse(
        args.responseStream,
        { statusCode: 200, headers: { "content-type": "text/plain" } },
      );
      args.responseState.started = true;
      await new Promise((resolve) => output.end("global-stream", resolve));
    },
  });
  setEnvironment({
    ROUTING_MODE: "global",
    GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com/base/",
  });
  const globalDownstream = new CollectingWritable();
  await __test.streamingHandler(
    validEvent(),
    globalDownstream,
    { getRemainingTimeInMillis: () => 20_000 },
    globalDependencies,
  );

  assert.equal(globalDownstream.text(), "global-stream");
  assert.deepEqual(responseMetadata.get(globalDownstream), {
    statusCode: 200,
    headers: { "content-type": "text/plain" },
  });
  assert.deepEqual(globalCapture.args.target, {
    hostname: "global.example.com",
    requestTarget: "/base/inference/v1/models?model=alpha",
  });
  assert.equal(globalCapture.args.timeoutMs, 19_000);
  assert.equal(globalCapture.args.idleTimeoutMs, 30_000);
  assert.deepEqual(globalCapture.args.headers.accept, [
    "application/json",
    "text/event-stream",
  ]);
  assert.match(globalCapture.args.headers["x-gco-signature"], /^[0-9a-f]{64}$/);
  assert.equal(globalDownstream.listenerCount("close"), 0);
  assert.equal(globalDownstream.listenerCount("error"), 0);

  const regionalCapture = {};
  let resolveCalls = 0;
  setEnvironment({
    ROUTING_MODE: "regional",
    GLOBAL_ACCELERATOR_ENDPOINT: undefined,
  });
  const regionalDownstream = new CollectingWritable();
  await __test.streamingHandler(
    validEvent({ rawQueryString: "model=beta", queryStringParameters: null }),
    regionalDownstream,
    { getRemainingTimeInMillis: () => 25_000 },
    baseDependencies({
      async resolveRegionalEndpoint() {
        resolveCalls += 1;
        return "regional.example.com";
      },
      async forwardRequest(args) {
        regionalCapture.args = args;
        const output = __test.beginStreamingResponse(
          args.responseStream,
          { statusCode: 201, headers: {} },
        );
        args.responseState.started = true;
        await new Promise((resolve) => output.end("regional-stream", resolve));
      },
    }),
  );
  assert.equal(resolveCalls, 1);
  assert.equal(regionalCapture.args.target.hostname, "regional.example.com");
  assert.equal(
    regionalCapture.args.target.requestTarget,
    "/inference/v1/models?model=beta",
  );
  assert.equal(regionalCapture.args.idleTimeoutMs, 300_000);
  assert.equal(regionalDownstream.text(), "regional-stream");
});

test("streamingHandler forwards a body between 8 KiB and 1 MiB intact", async () => {
  const body = JSON.stringify({ prompt: "x".repeat(16 * 1024) });
  const bodyBytes = Buffer.byteLength(body, "utf8");
  assert.ok(bodyBytes > 8 * 1024);
  assert.ok(bodyBytes < 1024 * 1024);

  let forwardedBody;
  setEnvironment({
    ROUTING_MODE: "global",
    GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
  });
  const downstream = new CollectingWritable();
  await __test.streamingHandler(
    validEvent({
      httpMethod: "POST",
      body,
      headers: { "Content-Type": "application/json" },
      multiValueHeaders: null,
    }),
    downstream,
    { getRemainingTimeInMillis: () => 30_000 },
    baseDependencies({
      async forwardRequest(args) {
        forwardedBody = args.bodyBuffer;
        const output = __test.beginStreamingResponse(
          args.responseStream,
          { statusCode: 200, headers: {} },
        );
        args.responseState.started = true;
        await new Promise((resolve) => output.end("accepted", resolve));
      },
    }),
  );

  assert.deepEqual(forwardedBody, Buffer.from(body, "utf8"));
  assert.equal(downstream.text(), "accepted");
});

test("streamingHandler maps authentication, routing, trust, timeout, and forwarding failures", async (t) => {
  await t.test("authentication unavailable", async () => {
    await assertHandlerError({
      environment: {
        ROUTING_MODE: "global",
        GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
      },
      dependencies: baseDependencies({
        async getSecretToken() {
          throw new Error("no secret");
        },
      }),
      expectedStatus: 503,
      expectedBody: {
        error: "Backend authentication is temporarily unavailable",
      },
    });
  });

  await t.test("global route unavailable", async () => {
    await assertHandlerError({
      environment: {
        ROUTING_MODE: "global",
        GLOBAL_ACCELERATOR_ENDPOINT: undefined,
      },
      dependencies: baseDependencies(),
      expectedStatus: 503,
      expectedBody: {
        error: "Global backend routing is temporarily unavailable",
      },
    });
  });

  await t.test("regional discovery unavailable", async () => {
    await assertHandlerError({
      environment: {
        ROUTING_MODE: "regional",
        GLOBAL_ACCELERATOR_ENDPOINT: undefined,
      },
      dependencies: baseDependencies({
        async resolveRegionalEndpoint() {
          throw new Error("registry unavailable");
        },
      }),
      expectedStatus: 502,
      expectedBody: {
        error: "Regional backend is temporarily unavailable",
      },
    });
  });

  await t.test("invalid global endpoint", async () => {
    await assertHandlerError({
      environment: {
        ROUTING_MODE: "global",
        GLOBAL_ACCELERATOR_ENDPOINT: "http://global.example.com",
      },
      dependencies: baseDependencies(),
      expectedStatus: 503,
      expectedBody: {
        error: "Global backend routing is temporarily unavailable",
      },
    });
  });

  await t.test("invalid regional endpoint", async () => {
    await assertHandlerError({
      environment: { ROUTING_MODE: "regional" },
      dependencies: baseDependencies({
        async resolveRegionalEndpoint() {
          return "http://regional.example.com";
        },
      }),
      expectedStatus: 502,
      expectedBody: {
        error: "Regional backend is temporarily unavailable",
      },
    });
  });

  await t.test("trust unavailable", async () => {
    await assertHandlerError({
      environment: {
        ROUTING_MODE: "global",
        GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
      },
      dependencies: baseDependencies({
        async getTlsTransport() {
          throw new Error("no trust");
        },
      }),
      expectedStatus: 503,
      expectedBody: { error: "Backend trust is temporarily unavailable" },
    });
  });

  await t.test("request budget exhausted", async () => {
    await assertHandlerError({
      environment: {
        ROUTING_MODE: "global",
        GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
      },
      dependencies: baseDependencies(),
      context: { getRemainingTimeInMillis: () => 500 },
      expectedStatus: 504,
      expectedBody: { error: "Gateway timeout" },
    });
  });

  await t.test("public forwarding error", async () => {
    await assertHandlerError({
      environment: {
        ROUTING_MODE: "global",
        GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
      },
      dependencies: baseDependencies({
        async forwardRequest() {
          throw new __test.PublicError(
            429,
            "Try again later",
            { "retry-after": "2" },
            { attempts: 3 },
          );
        },
      }),
      expectedStatus: 429,
      expectedBody: { error: "Try again later", attempts: 3 },
    });
  });

  await t.test("unexpected forwarding error", async () => {
    await assertHandlerError({
      environment: {
        ROUTING_MODE: "global",
        GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
      },
      dependencies: baseDependencies({
        async forwardRequest() {
          throw new Error("internal detail");
        },
      }),
      expectedStatus: 500,
      expectedBody: { error: "Internal server error" },
    });
  });
});

test("streamingHandler suppresses writes after downstream or started-response failures", async (t) => {
  await t.test("forwarding reports downstream abort", async () => {
    setEnvironment({
      ROUTING_MODE: "global",
      GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
    });
    const downstream = new CollectingWritable();
    await __test.streamingHandler(
      validEvent(),
      downstream,
      { getRemainingTimeInMillis: () => 30_000 },
      baseDependencies({
        async forwardRequest() {
          throw new __test.DownstreamAbortError();
        },
      }),
    );
    assert.equal(responseMetadata.has(downstream), false);
  });

  await t.test("response metadata was already started", async () => {
    setEnvironment({
      ROUTING_MODE: "global",
      GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
    });
    const downstream = new CollectingWritable();
    await __test.streamingHandler(
      validEvent(),
      downstream,
      { getRemainingTimeInMillis: () => 30_000 },
      baseDependencies({
        async forwardRequest({ responseState }) {
          responseState.started = true;
          throw new Error("stream failed after headers");
        },
      }),
    );
    assert.equal(responseMetadata.has(downstream), false);
  });

  await t.test("downstream close aborts in-flight forwarding", async () => {
    setEnvironment({
      ROUTING_MODE: "global",
      GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
    });
    const downstream = new CollectingWritable();
    await __test.streamingHandler(
      validEvent(),
      downstream,
      { getRemainingTimeInMillis: () => 30_000 },
      baseDependencies({
        async forwardRequest({ signal }) {
          downstream.emit("close");
          assert.equal(signal.aborted, true);
          throw new Error("cancelled");
        },
      }),
    );
    assert.equal(responseMetadata.has(downstream), false);
  });

  await t.test("downstream error aborts in-flight forwarding", async () => {
    setEnvironment({
      ROUTING_MODE: "global",
      GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
    });
    const downstream = new CollectingWritable();
    await __test.streamingHandler(
      validEvent(),
      downstream,
      { getRemainingTimeInMillis: () => 30_000 },
      baseDependencies({
        async forwardRequest({ signal }) {
          downstream.emit("error", new Error("caller disconnected"));
          assert.equal(signal.aborted, true);
          throw new Error("cancelled");
        },
      }),
    );
    assert.equal(responseMetadata.has(downstream), false);
  });
});

test("openUpstream handles empty bodies and failures after response headers", async (t) => {
  await t.test("empty request body is omitted", async () => {
    const incoming = new FakeIncoming();
    let request;
    const resource = await __test.openUpstream(
      upstreamArguments(undefined, { bodyBuffer: Buffer.alloc(0) }),
      (_options, callback) => {
        request = new FakeRequest({
          onEnd() {
            queueMicrotask(() => callback(incoming));
          },
        });
        return request;
      },
    );
    assert.equal(request.endedBody, undefined);
    resource.destroy();
    assert.match(request.destroyError.message, /cancelled/);
    resource.cleanup();
  });

  await t.test("late request error destroys the resolved response", async () => {
    const failure = codedError("ECONNRESET", "late request failure");
    const incoming = new FakeIncoming();
    let request;
    const resource = await __test.openUpstream(
      upstreamArguments(),
      (_options, callback) => {
        request = new FakeRequest({
          onEnd() {
            queueMicrotask(() => callback(incoming));
          },
        });
        return request;
      },
    );

    request.emit("error", failure);
    assert.equal(incoming.destroyed, true);
    assert.equal(incoming.destroyError, failure);
    resource.cleanup();
  });

  await t.test("absolute deadline remains active after headers", async () => {
    const incoming = new FakeIncoming();
    let request;
    const resource = await __test.openUpstream(
      upstreamArguments(undefined, { remainingMs: 1 }),
      (_options, callback) => {
        request = new FakeRequest({
          onEnd() {
            queueMicrotask(() => callback(incoming));
          },
        });
        return request;
      },
    );

    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(request.destroyed, true);
    assert.ok(request.destroyError instanceof __test.UpstreamTimeoutError);
    assert.equal(incoming.destroyed, true);
    assert.ok(incoming.destroyError instanceof __test.UpstreamTimeoutError);
    resource.cleanup();
  });
});

test("forwardRequest retries every retryable status and defaults missing status to 502", async () => {
  for (const method of ["GET", "HEAD"]) {
    for (const statusCode of [429, 502, 503, 504]) {
      const resources = [
        fakeResource(statusCode),
        fakeResource(statusCode),
        fakeResource(statusCode),
      ];
      const operations = queuedOperations([...resources]);
      await __test.forwardRequest(
        forwardingArguments({ method }),
        operations,
      );
      assert.equal(operations.openCalls.length, 3);
      assert.equal(operations.sleepCalls.length, 2);
      assert.equal(resources[0].cleanupCalls, 1);
      assert.equal(resources[0].destroyCalls, 1);
      assert.equal(resources[1].cleanupCalls, 1);
      assert.equal(resources[1].destroyCalls, 1);
      assert.equal(operations.streamCalls[0].resource, resources[2]);
    }
  }

  const missingStatus = fakeResource();
  delete missingStatus.response.statusCode;
  const success = fakeResource(200);
  const missingStatusOperations = queuedOperations([missingStatus, success]);
  await __test.forwardRequest(
    forwardingArguments({ method: "GET" }),
    missingStatusOperations,
  );
  assert.equal(missingStatusOperations.openCalls.length, 2);
  assert.equal(missingStatus.cleanupCalls, 1);
  assert.equal(missingStatus.destroyCalls, 1);
  assert.equal(missingStatusOperations.streamCalls[0].resource, success);

  const missingPostStatus = fakeResource();
  delete missingPostStatus.response.statusCode;
  const postOperations = queuedOperations([missingPostStatus]);
  await __test.forwardRequest(
    forwardingArguments({ method: "POST" }),
    postOperations,
  );
  assert.equal(postOperations.openCalls.length, 1);
  assert.equal(postOperations.sleepCalls.length, 0);
  assert.equal(postOperations.streamCalls[0].resource, missingPostStatus);
});

test("streamingHandler ignores close events once downstream ending has begun", async () => {
  setEnvironment({
    ROUTING_MODE: "global",
    GLOBAL_ACCELERATOR_ENDPOINT: "global.example.com",
  });

  for (const state of [
    { writableFinished: true, writableEnded: false },
    { writableFinished: false, writableEnded: true },
  ]) {
    const downstream = new EventEmitter();
    Object.assign(downstream, state);
    await __test.streamingHandler(
      validEvent(),
      downstream,
      { getRemainingTimeInMillis: () => 30_000 },
      baseDependencies({
        async forwardRequest({ signal }) {
          downstream.emit("close");
          assert.equal(signal.aborted, false);
        },
      }),
    );
    assert.equal(downstream.listenerCount("close"), 0);
    assert.equal(downstream.listenerCount("error"), 0);
  }
});
