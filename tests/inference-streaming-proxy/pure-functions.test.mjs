import assert from "node:assert/strict";
import { createHash, createHmac } from "node:crypto";
import test from "node:test";

import { __test } from "./support.mjs";

function withEnvironment(values, operation) {
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
    return operation();
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

function validEvent(overrides = {}) {
  return {
    requestContext: { http: { method: "POST" } },
    rawPath: "/inference/v1/chat/completions",
    headers: { "content-type": "application/json" },
    body: "{\"prompt\":\"hello\"}",
    ...overrides,
  };
}

function assertPublicError(operation, statusCode, message) {
  assert.throws(operation, (error) => {
    assert.equal(error.name, "PublicError");
    assert.equal(error.statusCode, statusCode);
    assert.equal(error.publicMessage, message);
    return true;
  });
}

test("preflight validates the complete event before runtime dependencies", () => {
  withEnvironment({ ROUTING_MODE: "global" }, () => {
    const parsed = __test.preflightRequest(validEvent());
    assert.equal(parsed.method, "POST");
    assert.equal(parsed.path, "/inference/v1/chat/completions");
    assert.equal(parsed.query, "");
    assert.equal(parsed.bodyBuffer.toString("utf8"), "{\"prompt\":\"hello\"}");
    assert.equal(parsed.mode, "global");

    assertPublicError(
      () => __test.preflightRequest(validEvent({ isBase64Encoded: true })),
      415,
      "Base64-encoded request bodies are not supported",
    );
    assert.throws(
      () =>
        __test.preflightRequest(
          validEvent({ requestContext: { http: { method: "DELETE" } } }),
        ),
      (error) => {
        assert.equal(error.statusCode, 405);
        assert.equal(error.headers.allow, "GET, HEAD, POST");
        return true;
      },
    );
    assertPublicError(
      () => __test.preflightRequest(validEvent({ rawPath: "/health" })),
      404,
      "Not found",
    );
    assertPublicError(
      () => __test.preflightRequest(validEvent({ body: { prompt: "hello" } })),
      400,
      "Invalid request body",
    );
    assertPublicError(
      () =>
        __test.preflightRequest(
          validEvent({ rawQueryString: "model=a\r\nx-injected=1" }),
        ),
      400,
      "Invalid query string",
    );
    assertPublicError(
      () =>
        __test.preflightRequest(
          validEvent({ requestContext: {}, httpMethod: undefined }),
        ),
      400,
      "Invalid request method",
    );
  });
});

test("duplicate query values use the backend-compatible encoding", () => {
  const query = __test.requestQuery({
    multiValueQueryStringParameters: {
      model: ["alpha", "beta two"],
      flag: true,
      empty: null,
      punctuation: "!*",
    },
    queryStringParameters: { ignored: "yes" },
  });
  assert.equal(
    query,
    "model=alpha&model=beta+two&flag=True&empty=None&punctuation=%21%2A",
  );
  assert.equal(
    __test.requestQuery({
      rawQueryString: "model=alpha&model=beta",
      multiValueQueryStringParameters: { ignored: ["yes"] },
    }),
    "model=alpha&model=beta",
  );
});

test("target construction accepts only HTTPS port 443 and encodes paths", () => {
  const target = __test.buildTarget(
    "HTTPS://Proxy.Example.COM:443/root///",
    "/inference/v1/a b/%2F/雪",
    "model=alpha&model=beta+two",
  );
  assert.deepEqual(target, {
    hostname: "proxy.example.com",
    requestTarget:
      "/root/inference/v1/a%20b/%2F/%E9%9B%AA?model=alpha&model=beta+two",
  });

  for (const endpoint of [
    "http://proxy.example.com",
    "https://proxy.example.com:444",
    "https://user:pass@proxy.example.com",
    "https://proxy.example.com?redirect=evil",
    "https://proxy.example.com#fragment",
  ]) {
    assert.throws(
      () => __test.buildTarget(endpoint, "/inference/v1/models", ""),
      /HTTPS on port 443/,
    );
  }
});

test("request, signing, and response headers stay within their allowlists", () => {
  const requestHeaders = __test.sanitizeRequestHeaders({
    " Content-Type ": "application/json",
    Connection: "keep-alive",
    Authorization: "Bearer caller-secret",
    "X-GCO-Signature": "caller-forgery",
    "X-GCO-Target-Region": "us-east-1",
    "X-Request-ID": 1234,
    Range: null,
    "Accept-Encoding": "gzip",
  });
  assert.deepEqual(requestHeaders, {
    "content-type": "application/json",
    "x-request-id": "1234",
    "accept-encoding": "gzip",
  });

  const signedHeaders = __test.buildSignedHeaders(
    "test-key",
    "GET",
    "/inference/v1/models",
    Buffer.alloc(0),
  );
  const outbound = __test.outboundHeaders({
    ...requestHeaders,
    ...signedHeaders,
    host: "attacker.example",
    connection: "upgrade",
  });
  assert.equal(outbound.host, undefined);
  assert.equal(outbound.connection, undefined);
  assert.equal(outbound["x-gco-signature"], signedHeaders["x-gco-signature"]);
  assert.equal(outbound["content-type"], "application/json");

  assert.deepEqual(
    __test.sanitizeResponseHeaders({
      "Content-Type": "application/x-ndjson",
      "Content-Length": "999",
      "Transfer-Encoding": "chunked",
      Connection: "keep-alive",
      "Set-Cookie": ["a=1", "b=2"],
      "set-cookie": "c=3",
      "X-Unset": undefined,
    }),
    {
      "content-type": "application/x-ndjson",
    },
  );
});

test("duplicate request headers prefer REST multi-value values", () => {
  const incoming = __test.eventHeaders({
    headers: {
      Accept: "application/json",
      "X-Request-ID": "scalar-id",
      Authorization: "Bearer caller-secret",
    },
    multiValueHeaders: {
      accept: ["application/json", "text/event-stream"],
      "x-request-id": ["request-a", "request-b"],
      authorization: ["Bearer first", "Bearer second"],
    },
  });
  const sanitized = __test.sanitizeRequestHeaders(incoming);

  assert.deepEqual(sanitized, {
    accept: ["application/json", "text/event-stream"],
    "x-request-id": ["request-a", "request-b"],
  });
  assert.deepEqual(__test.outboundHeaders(sanitized), sanitized);
});

test("signing headers form a verifiable v1 HMAC envelope", () => {
  const signingKey = "unit-test-signing-key";
  const method = "post";
  const requestTarget = "/inference/v1/chat?model=a&model=b";
  const body = Buffer.from("{\"input\":\"λ\"}", "utf8");
  const earliestTimestamp = Math.floor(Date.now() / 1_000);
  const headers = __test.buildSignedHeaders(
    signingKey,
    method,
    requestTarget,
    body,
  );
  const latestTimestamp = Math.floor(Date.now() / 1_000);

  assert.deepEqual(Object.keys(headers).sort(), [
    "x-gco-content-sha256",
    "x-gco-nonce",
    "x-gco-signature",
    "x-gco-signature-version",
    "x-gco-timestamp",
  ]);
  assert.equal(headers["x-gco-signature-version"], "v1");
  assert.match(headers["x-gco-nonce"], /^[0-9a-f]{32}$/);
  assert.ok(Number(headers["x-gco-timestamp"]) >= earliestTimestamp);
  assert.ok(Number(headers["x-gco-timestamp"]) <= latestTimestamp);

  const expectedHash = createHash("sha256").update(body).digest("hex");
  assert.equal(headers["x-gco-content-sha256"], expectedHash);
  const canonical = [
    "v1",
    headers["x-gco-timestamp"],
    headers["x-gco-nonce"],
    "POST",
    requestTarget,
    expectedHash,
  ].join("\n");
  const expectedSignature = createHmac(
    "sha256",
    Buffer.from(signingKey, "utf8"),
  )
    .update(canonical, "utf8")
    .digest("hex");
  assert.equal(headers["x-gco-signature"], expectedSignature);
});

test("remaining-time budgets and transport failures map deterministically", () => {
  assert.equal(__test.requestBudgetMilliseconds(undefined), 899_000);
  assert.equal(
    __test.requestBudgetMilliseconds({
      getRemainingTimeInMillis: () => 12_345,
    }),
    11_345,
  );
  assert.equal(
    __test.requestBudgetMilliseconds({
      getRemainingTimeInMillis: () => 500,
    }),
    0,
  );
  assert.equal(
    __test.requestBudgetMilliseconds({
      getRemainingTimeInMillis: () => 999_999,
    }),
    899_000,
  );
  assert.equal(
    __test.requestBudgetMilliseconds({
      getRemainingTimeInMillis: () => {
        throw new Error("context unavailable");
      },
    }),
    899_000,
  );

  assert.equal(
    __test.transportFailureKind({ code: "GCO_DOWNSTREAM_ABORT" }),
    "downstream",
  );
  assert.equal(
    __test.transportFailureKind({ code: "ERR_TLS_CERT_ALTNAME_INVALID" }),
    "tls",
  );
  assert.equal(
    __test.transportFailureKind({ code: "GCO_UPSTREAM_TIMEOUT" }),
    "timeout",
  );
  assert.equal(
    __test.transportFailureKind({ code: "ECONNREFUSED" }),
    "connection",
  );
  assert.equal(__test.transportFailureKind(new Error("boom")), "unexpected");

  const connectionError = __test.publicErrorForTransportFailure(
    "connection",
    3,
  );
  assert.equal(connectionError.statusCode, 503);
  assert.equal(connectionError.publicMessage, "Service unavailable");
  assert.deepEqual(connectionError.details, {
    message: "Upstream failed after 3 attempt(s)",
  });

  const timeoutError = __test.publicErrorForTransportFailure("timeout", 2);
  assert.equal(timeoutError.statusCode, 504);
  assert.equal(timeoutError.publicMessage, "Gateway timeout");
  assert.deepEqual(timeoutError.details, {
    message: "Upstream failed after 2 attempt(s)",
  });
  assert.equal(
    __test.publicErrorForTransportFailure("tls", 1).statusCode,
    502,
  );
  assert.equal(
    __test.publicErrorForTransportFailure("unexpected", 1).statusCode,
    500,
  );
});

test("event extraction, routing modes, and small bodies cover API Gateway shapes", () => {
  assert.equal(
    __test.eventMethod({
      httpMethod: "head",
      requestContext: { http: { method: "DELETE" } },
    }),
    "HEAD",
  );
  assert.equal(
    __test.eventMethod({ requestContext: { http: { method: "get" } } }),
    "GET",
  );
  for (const event of [
    undefined,
    {},
    { httpMethod: 123 },
    { requestContext: { http: { method: null } } },
  ]) {
    assertPublicError(
      () => __test.eventMethod(event),
      400,
      "Invalid request method",
    );
  }

  assert.equal(
    __test.eventPath({
      rawPath: "/inference/v1/raw",
      path: "/inference/v1/rest",
    }),
    "/inference/v1/raw",
  );
  assert.equal(
    __test.eventPath({ path: "/inference/v1/rest" }),
    "/inference/v1/rest",
  );
  for (const value of [
    undefined,
    123,
    "/inference/",
    "/health",
    "/inference/v1\rmodels",
    "/inference/v1\nmodels",
    "/inference/v1\0models",
  ]) {
    assertPublicError(
      () => __test.eventPath({ rawPath: value }),
      404,
      "Not found",
    );
  }

  for (const [value, expected] of [
    ["global", "global"],
    [" regional ", "regional"],
  ]) {
    withEnvironment({ ROUTING_MODE: value }, () => {
      assert.equal(__test.routingMode(), expected);
    });
  }
  for (const value of [undefined, "", "   ", "maintenance"]) {
    withEnvironment({ ROUTING_MODE: value }, () => {
      assertPublicError(
        () => __test.routingMode(),
        503,
        "Backend routing is temporarily unavailable",
      );
    });
  }

  withEnvironment({ ROUTING_MODE: "global" }, () => {
    const explicitPlaintext = __test.preflightRequest(
      validEvent({ isBase64Encoded: false, body: null }),
    );
    assert.equal(explicitPlaintext.bodyBuffer.byteLength, 0);
    assert.equal(
      __test.preflightRequest(validEvent({ body: undefined })).bodyBuffer
        .byteLength,
      0,
    );

    for (const body of [false, 0, 123, {}, []]) {
      assertPublicError(
        () => __test.preflightRequest(validEvent({ body })),
        400,
        "Invalid request body",
      );
    }

    for (const name of [
      "x-gco-target-region",
      "X-GCO-TARGET-REGION",
      "X-GcO-TaRgEt-ReGiOn",
    ]) {
      assertPublicError(
        () =>
          __test.preflightRequest(
            validEvent({ headers: { [name]: "us-west-2" } }),
          ),
        400,
        "X-GCO-Target-Region is not supported by the global endpoint; use the target region's regional API endpoint if authorized for direct access",
      );
    }
  });
});

test("query classification and encoding preserve backend-compatible semantics", () => {
  assert.equal(__test.nonEmptyMapping({ model: "alpha" }), true);
  for (const value of [undefined, null, "model=alpha", 1, [], {}]) {
    assert.equal(__test.nonEmptyMapping(value), false);
  }

  assert.equal(__test.encodedQueryFromMapping({}), "");
  assert.equal(
    __test.encodedQueryFromMapping({
      falseFlag: false,
      scalar: "one",
      repeated: ["two words", "three"],
      punctuation: "!'()*",
      empty: null,
    }),
    "falseFlag=False&scalar=one&repeated=two+words&repeated=three&punctuation=%21%27%28%29%2A&empty=None",
  );
  assert.equal(
    __test.requestQuery({
      multiValueQueryStringParameters: {},
      queryStringParameters: { model: "fallback" },
    }),
    "model=fallback",
  );
  assert.equal(
    __test.requestQuery({
      multiValueQueryStringParameters: [],
      queryStringParameters: { model: "fallback" },
    }),
    "model=fallback",
  );
  assert.equal(
    __test.requestQuery({
      rawQueryString: "",
      queryStringParameters: { ignored: "yes" },
    }),
    "",
  );
  assert.equal(__test.requestQuery({}), "");
  for (const rawQueryString of ["model=a\rbroken", "model=a\nbroken"]) {
    assertPublicError(
      () => __test.requestQuery({ rawQueryString }),
      400,
      "Invalid query string",
    );
  }
});

test("target construction handles normalized bases and percent repair", () => {
  assert.deepEqual(
    __test.buildTarget(
      "Proxy.Example.COM",
      "/inference/v1/models",
      "",
    ),
    {
      hostname: "proxy.example.com",
      requestTarget: "/inference/v1/models",
    },
  );
  assert.deepEqual(
    __test.buildTarget(
      "proxy.example.com/root////",
      "/inference/v1/:@$&+,;=",
      "",
    ),
    {
      hostname: "proxy.example.com",
      requestTarget: "/root/inference/v1/:@$&+,;=",
    },
  );
  assert.deepEqual(
    __test.buildTarget(
      "proxy.example.com",
      "/inference/v1/%/x%2G/y%2F/z",
      "",
    ),
    {
      hostname: "proxy.example.com",
      requestTarget: "/inference/v1/%25/x%252G/y%2F/z",
    },
  );

  for (const endpoint of [undefined, "", "https://["]) {
    assert.throws(
      () => __test.buildTarget(endpoint, "/inference/v1/models", ""),
      /Invalid proxy endpoint/,
    );
  }
  assert.throws(
    () =>
      __test.buildTarget(
        "https://:password@proxy.example.com",
        "/inference/v1/models",
        "",
      ),
    /HTTPS on port 443/,
  );
});

test("optional and repeated header shapes remain bounded by allowlists", () => {
  assert.deepEqual(__test.eventHeaders({}), {});
  assert.deepEqual(__test.eventHeaders({ headers: {}, multiValueHeaders: {} }), {});
  assert.deepEqual(
    __test.eventHeaders({ multiValueHeaders: { Accept: "text/event-stream" } }),
    { Accept: ["text/event-stream"] },
  );
  assert.equal(__test.hasHeader({}, "accept"), false);
  assert.equal(__test.hasHeader({ AcCePt: "application/json" }, "accept"), true);

  assert.deepEqual(
    __test.sanitizeRequestHeaders({
      Accept: [null, "application/json", undefined],
      Range: [null, undefined],
      "User-Agent": undefined,
      "X-Request-ID": "request-1",
    }),
    {
      accept: ["application/json"],
      "x-request-id": "request-1",
    },
  );
  assert.deepEqual(
    __test.outboundHeaders({
      Accept: null,
      Range: undefined,
      "X-Request-ID": ["request-1", 2],
      Authorization: "Bearer secret",
    }),
    { "x-request-id": ["request-1", "2"] },
  );
  assert.deepEqual(
    __test.sanitizeResponseHeaders({
      "X-Values": ["one", 2],
      "X-Null": null,
      "X-Undefined": undefined,
    }),
    {
      "x-values": "one, 2",
      "x-null": "null",
    },
  );
});
