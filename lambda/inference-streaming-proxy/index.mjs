import { createHash, createHmac, randomBytes, X509Certificate } from "node:crypto";
import * as https from "node:https";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import { checkServerIdentity } from "node:tls";
import { performance } from "node:perf_hooks";

import {
  GetSecretValueCommand,
  SecretsManagerClient,
} from "@aws-sdk/client-secrets-manager";
import { GetParameterCommand, SSMClient } from "@aws-sdk/client-ssm";
import {
  DescribeLoadBalancersCommand,
  DescribeTagsCommand,
  ElasticLoadBalancingV2Client,
} from "@aws-sdk/client-elastic-load-balancing-v2";

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);
const ALLOWED_REQUEST_HEADERS = new Set([
  "accept",
  "accept-encoding",
  "cache-control",
  "content-encoding",
  "content-type",
  "idempotency-key",
  "if-match",
  "if-none-match",
  "prefer",
  "range",
  "user-agent",
  "x-request-id",
]);
const INTERNAL_SIGNATURE_HEADERS = new Set([
  "x-gco-signature-version",
  "x-gco-signature",
  "x-gco-timestamp",
  "x-gco-nonce",
  "x-gco-content-sha256",
]);
const ALLOWED_METHODS = new Set(["GET", "HEAD", "POST"]);
const RETRYABLE_METHODS = new Set(["GET", "HEAD"]);
const RETRYABLE_STATUS_CODES = new Set([429, 502, 503, 504]);
const REGION_RE = /^[a-z]{2}(?:-[a-z]+)+-[0-9]+$/;
const DNS_NAME_RE = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i;
const GLOBAL_IDLE_TIMEOUT_MS = 30_000;
const REGIONAL_IDLE_TIMEOUT_MS = 300_000;
const LAMBDA_MAX_FORWARD_MS = 899_000;
const RESPONSE_HEADROOM_MS = 1_000;

function boundedEnvFloat(name, defaultValue, minimum, maximum) {
  const raw = process.env[name];
  if (raw === undefined || raw.trim() === "") {
    return defaultValue;
  }
  const value = Number(raw);
  return Number.isFinite(value) && value >= minimum && value <= maximum
    ? value
    : defaultValue;
}

function boundedEnvInt(name, defaultValue, minimum, maximum) {
  const raw = process.env[name];
  if (raw === undefined || !/^[+-]?\d+$/.test(raw.trim())) {
    return defaultValue;
  }
  const value = Number(raw);
  return Number.isSafeInteger(value) && value >= minimum && value <= maximum
    ? value
    : defaultValue;
}

function monotonicSeconds() {
  return performance.now() / 1_000;
}

function monotonicMilliseconds() {
  return performance.now();
}

class PublicError extends Error {
  constructor(statusCode, publicMessage, headers = {}, details = {}) {
    super(publicMessage);
    this.name = "PublicError";
    this.statusCode = statusCode;
    this.publicMessage = publicMessage;
    this.headers = headers;
    this.details = details;
  }
}

class UpstreamTimeoutError extends Error {
  constructor() {
    super("Upstream timeout");
    this.name = "UpstreamTimeoutError";
    this.code = "GCO_UPSTREAM_TIMEOUT";
  }
}

class DownstreamAbortError extends Error {
  constructor() {
    super("Downstream closed");
    this.name = "DownstreamAbortError";
    this.code = "GCO_DOWNSTREAM_ABORT";
  }
}

const SECRET_CACHE_TTL_SECONDS = boundedEnvFloat(
  "SECRET_CACHE_TTL_SECONDS",
  300,
  1,
  3_600,
);
const SECRET_CACHE_MAX_STALE_SECONDS = Math.max(
  SECRET_CACHE_TTL_SECONDS,
  boundedEnvFloat("SECRET_CACHE_MAX_STALE_SECONDS", 900, 1, 7_200),
);
const SECRET_CACHE_RETRY_SECONDS = boundedEnvFloat(
  "SECRET_CACHE_RETRY_SECONDS",
  5,
  0.1,
  60,
);
const MAX_RETRIES = boundedEnvInt("PROXY_MAX_RETRIES", 3, 1, 5);
const RETRY_BACKOFF_BASE_SECONDS = boundedEnvFloat(
  "PROXY_RETRY_BACKOFF_BASE",
  0.3,
  0,
  5,
);

const secretsClients = new Map();
const ssmClients = new Map();
const elbClients = new Map();

let cachedSecret = null;
let secretLastSuccessfulRefresh = 0;
let secretLastRefreshAttempt = 0;
let secretRefreshPromise = null;

let cachedTlsTransport = null;
let tlsLastSuccessfulRefresh = 0;
let tlsLastRefreshAttempt = 0;
let tlsRefreshPromise = null;

const regionalEndpointCache = new Map();

function secretRegion(secretArn) {
  const parts = String(secretArn || "").split(":");
  return parts.length >= 6 && parts[0] === "arn" && parts[2] === "secretsmanager"
    ? parts[3] || undefined
    : undefined;
}

function getSecretsClient(secretArn) {
  const region = secretRegion(secretArn);
  const key = region || "__default__";
  if (!secretsClients.has(key)) {
    secretsClients.set(
      key,
      new SecretsManagerClient(region ? { region } : {}),
    );
  }
  return secretsClients.get(key);
}

function getSsmClient(region) {
  if (!ssmClients.has(region)) {
    ssmClients.set(region, new SSMClient({ region }));
  }
  return ssmClients.get(region);
}

function getElbClient(region) {
  if (!elbClients.has(region)) {
    elbClients.set(region, new ElasticLoadBalancingV2Client({ region }));
  }
  return elbClients.get(region);
}

async function refreshSecret(now, ageAtAttempt) {
  secretLastRefreshAttempt = now;
  try {
    const secretArn = process.env.SECRET_ARN;
    if (!secretArn) {
      throw new Error("Signing secret is not configured");
    }
    const response = await getSecretsClient(secretArn).send(
      new GetSecretValueCommand({ SecretId: secretArn }),
    );
    if (typeof response.SecretString !== "string") {
      throw new Error("Signing secret has no string value");
    }
    const secretData = JSON.parse(response.SecretString);
    if (typeof secretData?.token !== "string" || secretData.token.length === 0) {
      throw new Error("Signing token is missing");
    }
    cachedSecret = secretData.token;
    secretLastSuccessfulRefresh = now;
    return cachedSecret;
  } catch {
    if (cachedSecret !== null && ageAtAttempt <= SECRET_CACHE_MAX_STALE_SECONDS) {
      console.warn("Secrets Manager refresh failed; using bounded stale signing key");
      return cachedSecret;
    }
    throw new Error("Authentication signing key is unavailable");
  }
}

async function getSecretToken() {
  const now = monotonicSeconds();
  const age = now - secretLastSuccessfulRefresh;
  if (cachedSecret !== null && age < SECRET_CACHE_TTL_SECONDS) {
    return cachedSecret;
  }
  if (
    cachedSecret !== null &&
    age <= SECRET_CACHE_MAX_STALE_SECONDS &&
    now - secretLastRefreshAttempt < SECRET_CACHE_RETRY_SECONDS
  ) {
    return cachedSecret;
  }
  if (secretRefreshPromise !== null) {
    return secretRefreshPromise;
  }

  const refresh = refreshSecret(now, age);
  secretRefreshPromise = refresh;
  try {
    return await refresh;
  } finally {
    if (secretRefreshPromise === refresh) {
      secretRefreshPromise = null;
    }
  }
}

function tlsSettings() {
  const serverName = String(process.env.BACKEND_TLS_SERVER_NAME || "")
    .trim()
    .replace(/\.+$/, "");
  const parameterName = String(process.env.BACKEND_TLS_ROOT_CA_PARAMETER || "").trim();
  const parameterRegion = String(process.env.BACKEND_TLS_ROOT_CA_REGION || "").trim();
  if (!DNS_NAME_RE.test(serverName)) {
    throw new Error("Backend TLS server identity is not configured");
  }
  if (!parameterName.startsWith("/") || !parameterRegion) {
    throw new Error("Backend TLS trust parameter is not configured");
  }

  const ttl = boundedEnvFloat("BACKEND_TLS_CA_CACHE_TTL_SECONDS", 300, 1, 3_600);
  const maxStale = Math.max(
    ttl,
    boundedEnvFloat("BACKEND_TLS_CA_MAX_STALE_SECONDS", 3_600, 1, 86_400),
  );
  const retry = boundedEnvFloat("BACKEND_TLS_CA_RETRY_SECONDS", 5, 0.1, 60);
  return { serverName, parameterName, parameterRegion, ttl, maxStale, retry };
}

function validateTrustBundle(trustBundle) {
  if (
    trustBundle.includes("PRIVATE KEY") ||
    !trustBundle.includes("-----BEGIN CERTIFICATE-----")
  ) {
    throw new Error("Backend TLS trust parameter contains invalid public material");
  }
  const certificates = trustBundle.match(
    /-----BEGIN CERTIFICATE-----[\s\S]*?-----END CERTIFICATE-----/g,
  );
  if (!certificates || certificates.length === 0) {
    throw new Error("Backend TLS trust parameter contains malformed certificates");
  }
  for (const certificate of certificates) {
    new X509Certificate(certificate);
  }
}

function newTlsTransport(serverName, trustBundle) {
  validateTrustBundle(trustBundle);
  const verifyServerIdentity = (_hostname, certificate) =>
    checkServerIdentity(serverName, certificate);
  const agent = new https.Agent({
    keepAlive: true,
    maxSockets: 10,
    maxFreeSockets: 4,
    rejectUnauthorized: true,
    ca: trustBundle,
    minVersion: "TLSv1.2",
    servername: serverName,
    checkServerIdentity: verifyServerIdentity,
  });
  return { agent, serverName, verifyServerIdentity };
}

async function refreshTlsTransport(settings, now, ageAtAttempt) {
  tlsLastRefreshAttempt = now;
  try {
    const response = await getSsmClient(settings.parameterRegion).send(
      new GetParameterCommand({ Name: settings.parameterName }),
    );
    const trustBundle = String(response.Parameter?.Value ?? "");
    const refreshed = newTlsTransport(settings.serverName, trustBundle);
    cachedTlsTransport = refreshed;
    tlsLastSuccessfulRefresh = now;
    return refreshed;
  } catch {
    if (cachedTlsTransport !== null && ageAtAttempt <= settings.maxStale) {
      console.warn("Backend TLS trust refresh failed; using bounded stale trust bundle");
      return cachedTlsTransport;
    }
    throw new Error("Backend TLS trust bundle is unavailable");
  }
}

async function getTlsTransport() {
  const settings = tlsSettings();
  const now = monotonicSeconds();
  const age = now - tlsLastSuccessfulRefresh;
  if (cachedTlsTransport !== null && age < settings.ttl) {
    return cachedTlsTransport;
  }
  if (
    cachedTlsTransport !== null &&
    age <= settings.maxStale &&
    now - tlsLastRefreshAttempt < settings.retry
  ) {
    return cachedTlsTransport;
  }
  if (tlsRefreshPromise !== null) {
    return tlsRefreshPromise;
  }

  const refresh = refreshTlsTransport(settings, now, age);
  tlsRefreshPromise = refresh;
  try {
    return await refresh;
  } finally {
    if (tlsRefreshPromise === refresh) {
      tlsRefreshPromise = null;
    }
  }
}

function regionalEndpointCacheTtl() {
  return boundedEnvFloat("REGIONAL_ENDPOINT_CACHE_TTL_SECONDS", 60, 0, 300);
}

function validatedDnsName(value) {
  const endpoint = String(value ?? "").trim().replace(/\.+$/, "");
  const lower = endpoint.toLowerCase();
  if (
    !DNS_NAME_RE.test(endpoint) ||
    (!lower.endsWith(".elb.amazonaws.com") &&
      !lower.endsWith(".elb.amazonaws.com.cn"))
  ) {
    throw new Error("Registered backend is invalid");
  }
  return endpoint;
}

async function validateRegionalEndpointOwnership(
  endpoint,
  region,
  expectedAccount,
  projectName,
) {
  const client = getElbClient(region);
  let marker;
  let matched = null;
  for (let page = 0; page < 20; page += 1) {
    const response = await client.send(
      new DescribeLoadBalancersCommand(marker ? { Marker: marker } : {}),
    );
    for (const loadBalancer of response.LoadBalancers || []) {
      const dnsName = String(loadBalancer.DNSName || "").replace(/\.+$/, "");
      if (dnsName.toLowerCase() === endpoint.toLowerCase()) {
        matched = loadBalancer;
        break;
      }
    }
    if (matched !== null) {
      break;
    }
    marker = response.NextMarker;
    if (!marker) {
      break;
    }
  }

  if (matched === null) {
    throw new Error("Registered backend does not exist");
  }
  if (matched.Type !== "application" || matched.Scheme !== "internal") {
    throw new Error("Registered backend is not an internal ALB");
  }

  const arn = String(matched.LoadBalancerArn || "");
  const arnParts = arn.split(":", 6);
  if (
    arnParts.length !== 6 ||
    arnParts[2] !== "elasticloadbalancing" ||
    arnParts[3] !== region ||
    arnParts[4] !== expectedAccount
  ) {
    throw new Error("Registered backend ownership is invalid");
  }

  const tagResponse = await client.send(
    new DescribeTagsCommand({ ResourceArns: [arn] }),
  );
  const tags = {};
  for (const description of tagResponse.TagDescriptions || []) {
    for (const tag of description.Tags || []) {
      tags[String(tag.Key)] = String(tag.Value);
    }
  }

  const expectedCluster = `${projectName}-${region}`;
  const clusterMatch =
    tags["eks:eks-cluster-name"] === expectedCluster ||
    tags["elbv2.k8s.aws/cluster"] === expectedCluster;
  if (!clusterMatch) {
    throw new Error("Registered backend is not owned by the GCO cluster");
  }

  const stackMatch =
    tags["ingress.eks.amazonaws.com/stack"] === "gco" ||
    tags["ingress.k8s.aws/stack"] === "gco-system/gco-ingress";
  if (!stackMatch) {
    throw new Error("Registered backend is not the GCO Ingress");
  }
}

async function resolveRegionalEndpoint() {
  const registryRegion = String(process.env.REGISTRY_REGION || "").trim();
  const targetRegion = String(process.env.TARGET_REGION || "").trim();
  const projectName = String(process.env.PROJECT_NAME || "").trim();
  const expectedAccount = String(process.env.AWS_ACCOUNT_ID || "").trim();
  if (
    !REGION_RE.test(registryRegion) ||
    !REGION_RE.test(targetRegion) ||
    !projectName ||
    !expectedAccount
  ) {
    throw new Error("Regional endpoint registry is not configured");
  }

  const cacheKey = JSON.stringify([
    registryRegion,
    targetRegion,
    projectName,
    expectedAccount,
  ]);
  const ttl = regionalEndpointCacheTtl();
  const now = monotonicSeconds();
  const cached = regionalEndpointCache.get(cacheKey);
  if (ttl > 0 && cached && now - cached.timestamp < ttl) {
    return cached.endpoint;
  }

  const parameterName = `/${projectName}/alb-hostname-${targetRegion}`;
  const response = await getSsmClient(registryRegion).send(
    new GetParameterCommand({ Name: parameterName }),
  );
  const endpoint = validatedDnsName(response.Parameter?.Value);
  await validateRegionalEndpointOwnership(
    endpoint,
    targetRegion,
    expectedAccount,
    projectName,
  );
  regionalEndpointCache.set(cacheKey, {
    timestamp: monotonicSeconds(),
    endpoint,
  });
  return endpoint;
}

function parseEndpoint(endpoint) {
  const value = String(endpoint || "").trim();
  const baseUrl = value.includes("://") ? value : `https://${value}`;
  let parsed;
  try {
    parsed = new URL(baseUrl);
  } catch {
    throw new Error("Invalid proxy endpoint");
  }
  if (
    parsed.protocol.toLowerCase() !== "https:" ||
    !parsed.hostname ||
    parsed.username ||
    parsed.password ||
    (parsed.port && parsed.port !== "443") ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("Proxy endpoint must use HTTPS on port 443");
  }
  return {
    hostname: parsed.hostname,
    endpointPath: parsed.pathname.replace(/\/+$/, ""),
  };
}

function encodeRequestPath(path) {
  const requestPath = path.startsWith("/") ? path : `/${path}`;
  const repaired = requestPath.replace(/%(?![0-9a-fA-F]{2})/g, "%25");
  return encodeURIComponent(repaired)
    .replace(/%2F/g, "/")
    .replace(/%3A/g, ":")
    .replace(/%40/g, "@")
    .replace(/%24/g, "$")
    .replace(/%26/g, "&")
    .replace(/%2B/g, "+")
    .replace(/%2C/g, ",")
    .replace(/%3B/g, ";")
    .replace(/%3D/g, "=")
    .replace(/%25/g, "%");
}

function pythonString(value) {
  if (value === null) {
    return "None";
  }
  if (value === true) {
    return "True";
  }
  if (value === false) {
    return "False";
  }
  return String(value);
}

function encodeQueryComponent(value) {
  return encodeURIComponent(pythonString(value))
    .replace(/[!'()*]/g, (character) =>
      `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
    )
    .replace(/%20/g, "+");
}

function nonEmptyMapping(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.keys(value).length > 0
  );
}

function encodedQueryFromMapping(queryParameters) {
  const pairs = [];
  for (const [name, rawValue] of Object.entries(queryParameters || {})) {
    const values = Array.isArray(rawValue) ? rawValue : [rawValue];
    for (const value of values) {
      pairs.push(`${encodeQueryComponent(name)}=${encodeQueryComponent(value)}`);
    }
  }
  return pairs.join("&");
}

function requestQuery(event) {
  if (typeof event?.rawQueryString === "string") {
    if (/[\r\n]/.test(event.rawQueryString)) {
      throw new PublicError(400, "Invalid query string");
    }
    return event.rawQueryString;
  }
  const parameters = nonEmptyMapping(event?.multiValueQueryStringParameters)
    ? event.multiValueQueryStringParameters
    : nonEmptyMapping(event?.queryStringParameters)
      ? event.queryStringParameters
      : {};
  return encodedQueryFromMapping(parameters);
}

function buildTarget(endpoint, requestPath, query) {
  const parsedEndpoint = parseEndpoint(endpoint);
  const encodedPath = encodeRequestPath(requestPath);
  const requestTarget = `${parsedEndpoint.endpointPath}${encodedPath}${
    query ? `?${query}` : ""
  }`;
  return {
    hostname: parsedEndpoint.hostname,
    requestTarget,
  };
}

function eventMethod(event) {
  const value = event?.httpMethod ?? event?.requestContext?.http?.method;
  if (typeof value !== "string") {
    throw new PublicError(400, "Invalid request method");
  }
  const method = value.toUpperCase();
  if (!ALLOWED_METHODS.has(method)) {
    throw new PublicError(405, "Method not allowed", {
      allow: "GET, HEAD, POST",
    });
  }
  return method;
}

function eventPath(event) {
  const value =
    typeof event?.rawPath === "string"
      ? event.rawPath
      : event?.path;
  if (
    typeof value !== "string" ||
    /[\r\n\0]/.test(value) ||
    !value.startsWith("/inference/") ||
    value.length <= "/inference/".length
  ) {
    throw new PublicError(404, "Not found");
  }
  return value;
}

function eventHeaders(event) {
  const headers = event?.headers;
  return headers !== null && typeof headers === "object" && !Array.isArray(headers)
    ? headers
    : {};
}

function hasHeader(headers, expectedName) {
  const lowerExpected = expectedName.toLowerCase();
  return Object.keys(headers).some(
    (name) => String(name).toLowerCase() === lowerExpected,
  );
}

function sanitizeRequestHeaders(headers) {
  const sanitized = {};
  for (const [name, value] of Object.entries(headers)) {
    const normalized = String(name).trim().toLowerCase();
    if (
      value === null ||
      value === undefined ||
      HOP_BY_HOP_HEADERS.has(normalized) ||
      !ALLOWED_REQUEST_HEADERS.has(normalized)
    ) {
      continue;
    }
    sanitized[normalized] = String(value);
  }
  return sanitized;
}

function buildSignedHeaders(signingKey, method, requestTarget, bodyBuffer) {
  const timestamp = String(Math.floor(Date.now() / 1_000));
  const nonce = randomBytes(16).toString("hex");
  const contentHash = createHash("sha256").update(bodyBuffer).digest("hex");
  const canonical = [
    "v1",
    timestamp,
    nonce,
    method.toUpperCase(),
    requestTarget,
    contentHash,
  ].join("\n");
  const signature = createHmac("sha256", Buffer.from(signingKey, "utf8"))
    .update(canonical, "utf8")
    .digest("hex");
  return {
    "x-gco-signature-version": "v1",
    "x-gco-signature": signature,
    "x-gco-timestamp": timestamp,
    "x-gco-nonce": nonce,
    "x-gco-content-sha256": contentHash,
  };
}

function outboundHeaders(headers) {
  const outbound = {};
  for (const [name, value] of Object.entries(headers)) {
    const normalized = String(name).toLowerCase();
    if (
      value !== null &&
      value !== undefined &&
      (ALLOWED_REQUEST_HEADERS.has(normalized) ||
        INTERNAL_SIGNATURE_HEADERS.has(normalized))
    ) {
      outbound[normalized] = String(value);
    }
  }
  return outbound;
}

function sanitizeResponseHeaders(headers) {
  const sanitized = {};
  for (const [name, value] of Object.entries(headers || {})) {
    const normalized = String(name).toLowerCase();
    if (
      HOP_BY_HOP_HEADERS.has(normalized) ||
      normalized === "content-length" ||
      value === undefined
    ) {
      continue;
    }
    sanitized[normalized] = Array.isArray(value)
      ? value.map(String).join(", ")
      : String(value);
  }
  return sanitized;
}

function requestBudgetMilliseconds(context) {
  let available = LAMBDA_MAX_FORWARD_MS;
  try {
    if (typeof context?.getRemainingTimeInMillis === "function") {
      const remaining = Number(context.getRemainingTimeInMillis());
      if (Number.isFinite(remaining)) {
        available = Math.max(0, remaining - RESPONSE_HEADROOM_MS);
      }
    }
  } catch {
    available = LAMBDA_MAX_FORWARD_MS;
  }
  return Math.min(LAMBDA_MAX_FORWARD_MS, available);
}

function isTlsError(error) {
  const code = String(error?.code || "");
  return (
    code === "EPROTO" ||
    code.startsWith("ERR_TLS_") ||
    code.startsWith("ERR_SSL_") ||
    code.startsWith("CERT_") ||
    [
      "UNABLE_TO_GET_ISSUER_CERT",
      "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
      "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
      "DEPTH_ZERO_SELF_SIGNED_CERT",
      "SELF_SIGNED_CERT_IN_CHAIN",
    ].includes(code)
  );
}

function transportFailureKind(error) {
  if (error instanceof DownstreamAbortError || error?.code === "GCO_DOWNSTREAM_ABORT") {
    return "downstream";
  }
  if (isTlsError(error)) {
    return "tls";
  }
  if (
    error instanceof UpstreamTimeoutError ||
    error?.code === "GCO_UPSTREAM_TIMEOUT" ||
    error?.code === "ETIMEDOUT"
  ) {
    return "timeout";
  }
  if (
    [
      "ECONNREFUSED",
      "ECONNRESET",
      "ECONNABORTED",
      "EHOSTUNREACH",
      "ENETUNREACH",
      "ENETDOWN",
      "ENOTFOUND",
      "EAI_AGAIN",
      "EPIPE",
    ].includes(String(error?.code || ""))
  ) {
    return "connection";
  }
  return "unexpected";
}

function openUpstream({
  target,
  method,
  headers,
  bodyBuffer,
  transport,
  idleTimeoutMs,
  remainingMs,
  signal,
}) {
  return new Promise((resolve, reject) => {
    let request;
    let response = null;
    let promiseSettled = false;
    let cleaned = false;
    let deadlineTimer;

    const cleanup = () => {
      if (cleaned) {
        return;
      }
      cleaned = true;
      clearTimeout(deadlineTimer);
      signal.removeEventListener("abort", onAbort);
      request?.setTimeout(0);
      response?.setTimeout(0);
    };

    const rejectBeforeResponse = (error) => {
      if (promiseSettled) {
        if (response && !response.destroyed) {
          response.destroy(error);
        }
        return;
      }
      promiseSettled = true;
      cleanup();
      reject(error);
    };

    const destroy = (error = new Error("Upstream request cancelled")) => {
      if (response && !response.destroyed) {
        response.destroy(error);
      }
      if (request && !request.destroyed) {
        request.destroy(error);
      }
    };

    const onAbort = () => {
      const error = new DownstreamAbortError();
      destroy(error);
      rejectBeforeResponse(error);
    };

    if (signal.aborted) {
      reject(new DownstreamAbortError());
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });

    try {
      request = https.request(
        {
          protocol: "https:",
          hostname: target.hostname,
          port: 443,
          method,
          path: target.requestTarget,
          headers,
          agent: transport.agent,
          servername: transport.serverName,
          rejectUnauthorized: true,
          minVersion: "TLSv1.2",
          checkServerIdentity: transport.verifyServerIdentity,
        },
        (incoming) => {
          if (promiseSettled) {
            incoming.destroy();
            return;
          }
          response = incoming;
          promiseSettled = true;
          response.setTimeout(idleTimeoutMs, () => {
            destroy(new UpstreamTimeoutError());
          });
          resolve({ request, response, cleanup, destroy });
        },
      );
    } catch (error) {
      rejectBeforeResponse(error);
      return;
    }

    request.on("error", (error) => {
      rejectBeforeResponse(error);
    });
    request.once("upgrade", (_incoming, socket) => {
      socket.destroy();
      rejectBeforeResponse(new Error("Unexpected protocol upgrade"));
    });
    request.setTimeout(idleTimeoutMs, () => {
      destroy(new UpstreamTimeoutError());
    });
    deadlineTimer = setTimeout(() => {
      destroy(new UpstreamTimeoutError());
    }, Math.max(1, Math.ceil(remainingMs)));

    try {
      request.end(bodyBuffer.length > 0 ? bodyBuffer : undefined);
    } catch (error) {
      destroy(error);
      rejectBeforeResponse(error);
    }
  });
}

function sleep(milliseconds, signal) {
  if (milliseconds <= 0) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DownstreamAbortError());
    };
    if (signal.aborted) {
      clearTimeout(timer);
      reject(new DownstreamAbortError());
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function streamFinalResponse(resource, responseStream, state, signal) {
  let output;
  try {
    output = awslambda.HttpResponseStream.from(responseStream, {
      statusCode: resource.response.statusCode || 502,
      headers: sanitizeResponseHeaders(resource.response.headers),
    });
    state.started = true;
  } catch {
    resource.cleanup();
    resource.destroy();
    throw new PublicError(500, "Internal server error");
  }

  try {
    await pipeline(resource.response, output, { signal });
  } catch {
    resource.destroy();
    if (!signal.aborted) {
      console.warn("Upstream response stream terminated before completion");
    }
  } finally {
    resource.cleanup();
  }
}

async function forwardRequest({
  target,
  method,
  headers,
  bodyBuffer,
  transport,
  timeoutMs,
  idleTimeoutMs,
  responseStream,
  responseState,
  signal,
}) {
  const maxAttempts = RETRYABLE_METHODS.has(method) ? MAX_RETRIES : 1;
  const deadline = monotonicMilliseconds() + Math.max(timeoutMs, 0);
  let lastFailureKind = null;
  let attemptsMade = 0;

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const remaining = deadline - monotonicMilliseconds();
    if (remaining <= 0) {
      break;
    }
    attemptsMade = attempt + 1;

    let resource;
    try {
      resource = await openUpstream({
        target,
        method,
        headers,
        bodyBuffer,
        transport,
        idleTimeoutMs,
        remainingMs: remaining,
        signal,
      });
    } catch (error) {
      const kind = transportFailureKind(error);
      if (kind === "downstream") {
        throw error;
      }
      if (kind === "tls") {
        throw new PublicError(502, "Backend TLS verification failed");
      }
      if (kind === "unexpected") {
        throw new PublicError(500, "Internal server error");
      }
      lastFailureKind = kind;
      console.warn(
        `Upstream ${method} failed on attempt ${attempt + 1}/${maxAttempts}`,
      );

      if (attempt >= maxAttempts - 1) {
        break;
      }
      const backoffMs = RETRY_BACKOFF_BASE_SECONDS * 1_000 * 2 ** attempt;
      if (deadline - monotonicMilliseconds() <= backoffMs) {
        break;
      }
      await sleep(backoffMs, signal);
      continue;
    }

    const statusCode = resource.response.statusCode || 502;
    if (RETRYABLE_STATUS_CODES.has(statusCode) && attempt < maxAttempts - 1) {
      const backoffMs = RETRY_BACKOFF_BASE_SECONDS * 1_000 * 2 ** attempt;
      if (deadline - monotonicMilliseconds() > backoffMs) {
        console.warn(
          `Retryable upstream status ${statusCode} on attempt ${attempt + 1}/${maxAttempts} for ${method}`,
        );
        resource.cleanup();
        resource.destroy();
        await sleep(backoffMs, signal);
        continue;
      }
    }

    await streamFinalResponse(
      resource,
      responseStream,
      responseState,
      signal,
    );
    return;
  }

  if (lastFailureKind === "connection") {
    throw new PublicError(503, "Service unavailable", {}, {
      message: `Upstream failed after ${attemptsMade} attempt(s)`,
    });
  }
  if (lastFailureKind === "timeout") {
    throw new PublicError(504, "Gateway timeout", {}, {
      message: `Upstream failed after ${attemptsMade} attempt(s)`,
    });
  }
  throw new PublicError(504, "Gateway timeout");
}

async function sendJsonError(
  responseStream,
  statusCode,
  message,
  headers = {},
  details = {},
) {
  if (responseStream.destroyed) {
    return;
  }
  const body = JSON.stringify({ error: message, ...details });
  try {
    const output = awslambda.HttpResponseStream.from(responseStream, {
      statusCode,
      headers: {
        "content-type": "application/json",
        ...headers,
      },
    });
    await pipeline(Readable.from([body]), output);
  } catch {
    // The caller may have disconnected before the bounded error could be sent.
  }
}

function routingMode() {
  const mode = String(process.env.ROUTING_MODE || "").trim();
  if (mode !== "global" && mode !== "regional") {
    throw new PublicError(503, "Backend routing is temporarily unavailable");
  }
  return mode;
}

async function streamingHandler(event, responseStream, context) {
  const responseState = { started: false };
  const downstreamAbort = new AbortController();
  const onDownstreamClose = () => {
    if (!responseStream.writableFinished && !responseStream.writableEnded) {
      downstreamAbort.abort(new DownstreamAbortError());
    }
  };
  const onDownstreamError = () => {
    downstreamAbort.abort(new DownstreamAbortError());
  };
  responseStream.once("close", onDownstreamClose);
  responseStream.once("error", onDownstreamError);

  try {
    if (event?.isBase64Encoded) {
      throw new PublicError(415, "Base64-encoded request bodies are not supported");
    }
    const method = eventMethod(event);
    const path = eventPath(event);
    const query = requestQuery(event);
    const incomingHeaders = eventHeaders(event);
    const body = event?.body ?? "";
    if (typeof body !== "string") {
      throw new PublicError(400, "Invalid request body");
    }
    const bodyBuffer = Buffer.from(body, "utf8");
    const mode = routingMode();

    if (mode === "global" && hasHeader(incomingHeaders, "x-gco-target-region")) {
      throw new PublicError(
        400,
        "X-GCO-Target-Region is not supported by the global endpoint; use the target region's regional API endpoint if authorized for direct access",
      );
    }

    let signingKey;
    try {
      signingKey = await getSecretToken();
    } catch {
      throw new PublicError(
        503,
        "Backend authentication is temporarily unavailable",
      );
    }

    let endpoint;
    try {
      if (mode === "global") {
        endpoint = process.env.GLOBAL_ACCELERATOR_ENDPOINT;
        if (!endpoint) {
          throw new Error("Global endpoint is not configured");
        }
      } else {
        endpoint = await resolveRegionalEndpoint();
      }
    } catch {
      if (mode === "regional") {
        console.warn("Regional backend resolution failed");
        throw new PublicError(502, "Regional backend is temporarily unavailable");
      }
      throw new PublicError(
        503,
        "Global backend routing is temporarily unavailable",
      );
    }

    let target;
    try {
      target = buildTarget(endpoint, path, query);
    } catch (error) {
      if (error instanceof PublicError) {
        throw error;
      }
      throw new PublicError(
        mode === "global" ? 503 : 502,
        mode === "global"
          ? "Global backend routing is temporarily unavailable"
          : "Regional backend is temporarily unavailable",
      );
    }

    let transport;
    try {
      transport = await getTlsTransport();
    } catch {
      throw new PublicError(503, "Backend trust is temporarily unavailable");
    }

    const requestHeaders = sanitizeRequestHeaders(incomingHeaders);
    Object.assign(
      requestHeaders,
      buildSignedHeaders(signingKey, method, target.requestTarget, bodyBuffer),
    );
    const timeoutMs = requestBudgetMilliseconds(context);
    if (timeoutMs <= 0) {
      throw new PublicError(504, "Gateway timeout");
    }

    await forwardRequest({
      target,
      method,
      headers: outboundHeaders(requestHeaders),
      bodyBuffer,
      transport,
      timeoutMs,
      idleTimeoutMs:
        mode === "global" ? GLOBAL_IDLE_TIMEOUT_MS : REGIONAL_IDLE_TIMEOUT_MS,
      responseStream,
      responseState,
      signal: downstreamAbort.signal,
    });
  } catch (error) {
    if (
      downstreamAbort.signal.aborted ||
      error instanceof DownstreamAbortError ||
      responseState.started
    ) {
      return;
    }
    if (error instanceof PublicError) {
      await sendJsonError(
        responseStream,
        error.statusCode,
        error.publicMessage,
        error.headers,
        error.details,
      );
      return;
    }
    await sendJsonError(responseStream, 500, "Internal server error");
  } finally {
    responseStream.removeListener("close", onDownstreamClose);
    responseStream.removeListener("error", onDownstreamError);
  }
}

export const handler = awslambda.streamifyResponse(streamingHandler);
