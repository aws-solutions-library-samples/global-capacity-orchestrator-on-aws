# Inference Streaming Proxy Tests

Native Node.js tests for the production response-streaming Lambda in
[`lambda/inference-streaming-proxy/`](../../lambda/inference-streaming-proxy/).
The tests live in the repository-wide `tests/` tree, while dependency
installation and execution remain anchored to the deployable Lambda package.

## Table of contents

- [Overview](#overview)
- [Test map](#test-map)
- [Running the suite](#running-the-suite)
- [Coverage policy](#coverage-policy)
- [Test architecture](#test-architecture)
- [Adding or changing tests](#adding-or-changing-tests)

## Overview

The suite exercises request validation, routing, AWS client integration,
request forwarding, response streaming, retries, backpressure, TLS behavior,
cache handling, and downstream disconnects. It uses Node's built-in
`node:test` runner and V8 coverage; no separate JavaScript test framework is
required.

The production npm manifests remain beside
[`index.mjs`](../../lambda/inference-streaming-proxy/index.mjs) because they
define the exact dependency graph copied into the Lambda deployment artifact.
The package's `npm test` script discovers the test files in this directory.

## Test map

| File | Responsibility |
|------|----------------|
| `aws-runtime.test.mjs` | AWS client construction, caching, configuration, and runtime integration |
| `forwarding-handler.test.mjs` | Upstream request forwarding, retries, headers, backpressure, and disconnect handling |
| `handler-preflight.test.mjs` | Handler input validation and failures before an upstream request starts |
| `pure-functions.test.mjs` | Deterministic parsing, validation, routing, signing, and helper behavior |
| `streaming.test.mjs` | Streaming response metadata, body delivery, timeout, and error behavior |
| `support.mjs` | Shared fakes, stream collectors, fixtures, and access to test-only exports from production code |

## Running the suite

Use the Node version in [`.nvmrc`](../../.nvmrc) and the exact npm version in
the Lambda package's `packageManager` field.

```bash
bash .github/scripts/use-pinned-npm.sh \
  lambda/inference-streaming-proxy/package.json
npm ci --prefix lambda/inference-streaming-proxy \
  --ignore-scripts --no-audit --no-fund
npm --prefix lambda/inference-streaming-proxy test
```

The dedicated
[`Inference Streaming Proxy`](../../.github/workflows/inference-streaming-proxy.yml)
workflow runs these commands in CI.

## Coverage policy

The package script enforces at least 93% line, function, and branch coverage
with Node's built-in V8 coverage. This dedicated JavaScript gate is separate
from the repository-wide Python coverage configuration, whose enforced floor
remains 90% while the project targets ~92% measured coverage.

Do not weaken a threshold to accommodate new behavior. Add focused tests for
new success, failure, timeout, cancellation, and cleanup paths instead.

## Test architecture

`support.mjs` imports the production module after temporarily clearing
environment variables that are read at module initialization. Individual tests
import shared fakes from `support.mjs`, so they all exercise the same production
module instance and deterministic defaults.

Tests must not make live AWS or network calls. Inject fake AWS clients and
transport implementations through the production module's test-only exports.
Keep assertions on externally visible behavior—status, headers, bytes,
cleanup, retries, and errors—rather than private implementation ordering unless
the ordering is itself a safety contract.

## Adding or changing tests

1. Put new `*.test.mjs` files in this directory so the package glob discovers
   them automatically.
2. Reuse `support.mjs` for shared clients, streams, and transport doubles.
3. Restore mutated environment variables, clocks, clients, and caches in test
   cleanup hooks.
4. Cover both the successful path and relevant malformed-input, timeout,
   cancellation, stale-cache, or partial-stream failure paths.
5. Keep tests deterministic and offline; CI supplies no AWS credentials.
6. Update the test map above when adding or renaming a test file.
