import assert from "node:assert/strict";
import { rootCertificates } from "node:tls";
import test, { afterEach, beforeEach } from "node:test";

import { __test } from "./support.mjs";

const ENV_NAMES = [
  "AWS_ACCOUNT_ID",
  "AWS_URL_SUFFIX",
  "BACKEND_TLS_CA_CACHE_TTL_SECONDS",
  "BACKEND_TLS_CA_MAX_STALE_SECONDS",
  "BACKEND_TLS_CA_RETRY_SECONDS",
  "BACKEND_TLS_ROOT_CA_PARAMETER",
  "BACKEND_TLS_ROOT_CA_REGION",
  "BACKEND_TLS_SERVER_NAME",
  "PROJECT_NAME",
  "REGIONAL_ENDPOINT_CACHE_TTL_SECONDS",
  "REGISTRY_REGION",
  "SECRET_ARN",
  "TARGET_REGION",
  "TEST_FLOAT",
  "TEST_INT",
];
const ORIGINAL_ENV = new Map(
  ENV_NAMES.map((name) => [name, process.env[name]]),
);
const TEST_CERTIFICATE = rootCertificates[0];

function setEnvironment(values) {
  for (const [name, value] of Object.entries(values)) {
    if (value === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = value;
    }
  }
}

function seedClient(clientMap, key, implementation) {
  const client = {
    calls: [],
    async send(command) {
      this.calls.push(command);
      return implementation(command, this.calls.length);
    },
  };
  clientMap.set(key, client);
  return client;
}

function validTlsEnvironment(overrides = {}) {
  return {
    BACKEND_TLS_SERVER_NAME: "backend.gco.internal.",
    BACKEND_TLS_ROOT_CA_PARAMETER: "/gco/backend/root-ca",
    BACKEND_TLS_ROOT_CA_REGION: "us-east-1",
    BACKEND_TLS_CA_CACHE_TTL_SECONDS: undefined,
    BACKEND_TLS_CA_MAX_STALE_SECONDS: undefined,
    BACKEND_TLS_CA_RETRY_SECONDS: undefined,
    ...overrides,
  };
}

function validRegionalEnvironment(overrides = {}) {
  return {
    REGISTRY_REGION: "us-east-1",
    TARGET_REGION: "us-west-2",
    PROJECT_NAME: "gco-test",
    AWS_ACCOUNT_ID: "123456789012",
    AWS_URL_SUFFIX: "amazonaws.com",
    REGIONAL_ENDPOINT_CACHE_TTL_SECONDS: "60",
    ...overrides,
  };
}

function loadBalancer(overrides = {}) {
  return {
    DNSName: "internal-gco.us-west-2.elb.amazonaws.com",
    Type: "application",
    Scheme: "internal",
    LoadBalancerArn:
      "arn:aws:elasticloadbalancing:us-west-2:123456789012:loadbalancer/app/gco/abc123",
    ...overrides,
  };
}

function ownershipClient(loadBalancers, tags = [], responses = {}) {
  return {
    calls: [],
    async send(command) {
      this.calls.push(command);
      if (command.constructor.name === "DescribeLoadBalancersCommand") {
        return responses.loadBalancers ?? { LoadBalancers: loadBalancers };
      }
      if (command.constructor.name === "DescribeTagsCommand") {
        return responses.tags ?? {
          TagDescriptions: [{ Tags: tags }],
        };
      }
      throw new Error(`Unexpected command ${command.constructor.name}`);
    },
  };
}

beforeEach(() => {
  __test.resetRuntimeStateForTest();
});

afterEach(() => {
  __test.resetRuntimeStateForTest();
  for (const [name, value] of ORIGINAL_ENV) {
    if (value === undefined) {
      delete process.env[name];
    } else {
      process.env[name] = value;
    }
  }
});

test("environment bounds, clocks, regions, and AWS client caches are deterministic", () => {
  setEnvironment({ TEST_FLOAT: undefined, TEST_INT: undefined });
  assert.equal(__test.boundedEnvFloat("TEST_FLOAT", 2.5, 1, 3), 2.5);
  assert.equal(__test.boundedEnvInt("TEST_INT", 7, 1, 10), 7);

  for (const [value, expected] of [
    [" 2.75 ", 2.75],
    ["1", 1],
    ["3", 3],
  ]) {
    setEnvironment({ TEST_FLOAT: value });
    assert.equal(__test.boundedEnvFloat("TEST_FLOAT", 2.5, 1, 3), expected);
  }
  for (const value of ["", "   ", "NaN", "Infinity", "0.5", "4"]) {
    setEnvironment({ TEST_FLOAT: value });
    assert.equal(__test.boundedEnvFloat("TEST_FLOAT", 2.5, 1, 3), 2.5);
  }

  for (const [value, expected] of [
    ["+8", 8],
    ["1", 1],
    ["10", 10],
  ]) {
    setEnvironment({ TEST_INT: value });
    assert.equal(__test.boundedEnvInt("TEST_INT", 7, 1, 10), expected);
  }
  for (const value of [
    "",
    "   ",
    "1.2",
    "text",
    "0",
    "11",
    "90071992547409999",
  ]) {
    setEnvironment({ TEST_INT: value });
    assert.equal(__test.boundedEnvInt("TEST_INT", 7, 1, 10), 7);
  }

  assert.ok(Number.isFinite(__test.monotonicSeconds()));
  assert.ok(Number.isFinite(__test.monotonicMilliseconds()));
  assert.equal(
    __test.secretRegion(
      "arn:aws:secretsmanager:us-west-2:123456789012:secret:gco-signing",
    ),
    "us-west-2",
  );
  for (const arn of [
    "not-an-arn",
    "notarn:aws:secretsmanager:us-west-2:123456789012:secret",
    "arn:aws:ssm:us-west-2:123456789012:parameter/gco",
    "arn:aws:secretsmanager::123456789012:secret:gco-signing",
    null,
  ]) {
    assert.equal(__test.secretRegion(arn), undefined);
  }

  const regionalSecret = __test.getSecretsClient(
    "arn:aws:secretsmanager:us-west-2:123456789012:secret:gco-signing",
  );
  assert.equal(
    __test.getSecretsClient(
      "arn:aws:secretsmanager:us-west-2:123456789012:secret:other",
    ),
    regionalSecret,
  );
  const defaultSecret = __test.getSecretsClient("not-an-arn");
  assert.notEqual(defaultSecret, regionalSecret);

  const ssm = __test.getSsmClient("us-east-1");
  assert.equal(__test.getSsmClient("us-east-1"), ssm);
  const elb = __test.getElbClient("us-west-2");
  assert.equal(__test.getElbClient("us-west-2"), elb);

  regionalSecret.destroy();
  defaultSecret.destroy();
  ssm.destroy();
  elb.destroy();
});

test("signing secrets refresh, cache, throttle, and fail closed after max-stale", async () => {
  const secretArn =
    "arn:aws:secretsmanager:us-west-2:123456789012:secret:gco-signing";
  setEnvironment({ SECRET_ARN: secretArn });
  const client = seedClient(
    __test.secretsClients,
    "us-west-2",
    (_command, callNumber) => {
      if (callNumber === 1) {
        return { SecretString: JSON.stringify({ token: "first-token" }) };
      }
      throw new Error("Secrets Manager unavailable");
    },
  );

  assert.equal(await __test.getSecretToken(1_000), "first-token");
  assert.equal(await __test.getSecretToken(1_100), "first-token");
  assert.equal(client.calls.length, 1);
  assert.deepEqual(client.calls[0].input, { SecretId: secretArn });

  assert.equal(await __test.getSecretToken(1_300), "first-token");
  assert.equal(client.calls.length, 2);
  assert.equal(await __test.getSecretToken(1_301), "first-token");
  assert.equal(client.calls.length, 2);

  await assert.rejects(
    __test.getSecretToken(1_901),
    /Authentication signing key is unavailable/,
  );
  assert.equal(client.calls.length, 3);
});

test("signing secret refreshes are shared and malformed values are rejected", async () => {
  const secretArn =
    "arn:aws:secretsmanager:us-east-1:123456789012:secret:gco-signing";
  setEnvironment({ SECRET_ARN: secretArn });

  let release;
  const client = seedClient(__test.secretsClients, "us-east-1", () =>
    new Promise((resolve) => {
      release = resolve;
    }),
  );
  const first = __test.getSecretToken(2_000);
  const second = __test.getSecretToken(2_000);
  assert.equal(client.calls.length, 1);
  release({ SecretString: JSON.stringify({ token: "shared-token" }) });
  assert.deepEqual(await Promise.all([first, second]), [
    "shared-token",
    "shared-token",
  ]);

  const invalidResponses = [
    undefined,
    null,
    {},
    { SecretString: null },
    { SecretString: "{" },
    { SecretString: "null" },
    { SecretString: "{}" },
    { SecretString: JSON.stringify({ token: "" }) },
    { SecretString: JSON.stringify({ token: 123 }) },
  ];
  for (const response of invalidResponses) {
    __test.resetRuntimeStateForTest();
    seedClient(__test.secretsClients, "us-east-1", () => response);
    await assert.rejects(
      __test.getSecretToken(),
      /Authentication signing key is unavailable/,
    );
  }

  __test.resetRuntimeStateForTest();
  setEnvironment({ SECRET_ARN: undefined });
  await assert.rejects(
    __test.getSecretToken(3_000),
    /Authentication signing key is unavailable/,
  );
});

test("TLS settings and public trust material are validated before agent creation", () => {
  assert.ok(TEST_CERTIFICATE);
  setEnvironment(
    validTlsEnvironment({
      BACKEND_TLS_CA_CACHE_TTL_SECONDS: "10",
      BACKEND_TLS_CA_MAX_STALE_SECONDS: "2",
      BACKEND_TLS_CA_RETRY_SECONDS: "0.5",
    }),
  );
  assert.deepEqual(__test.tlsSettings(), {
    serverName: "backend.gco.internal",
    parameterName: "/gco/backend/root-ca",
    parameterRegion: "us-east-1",
    ttl: 10,
    maxStale: 10,
    retry: 0.5,
  });

  const invalidSettings = [
    [{ BACKEND_TLS_SERVER_NAME: undefined }, /server identity is not configured/],
    [{ BACKEND_TLS_SERVER_NAME: "   " }, /server identity is not configured/],
    [{ BACKEND_TLS_SERVER_NAME: "not a dns name" }, /server identity is not configured/],
    [{ BACKEND_TLS_ROOT_CA_PARAMETER: undefined }, /trust parameter is not configured/],
    [{ BACKEND_TLS_ROOT_CA_PARAMETER: "   " }, /trust parameter is not configured/],
    [{ BACKEND_TLS_ROOT_CA_PARAMETER: "relative-name" }, /trust parameter is not configured/],
    [{ BACKEND_TLS_ROOT_CA_REGION: undefined }, /trust parameter is not configured/],
    [{ BACKEND_TLS_ROOT_CA_REGION: "   " }, /trust parameter is not configured/],
  ];
  for (const [overrides, pattern] of invalidSettings) {
    setEnvironment(validTlsEnvironment(overrides));
    assert.throws(__test.tlsSettings, pattern);
  }

  assert.throws(
    () => __test.validateTrustBundle("-----BEGIN PRIVATE KEY-----"),
    /invalid public material/,
  );
  assert.throws(
    () => __test.validateTrustBundle("ordinary public text"),
    /invalid public material/,
  );
  assert.throws(
    () => __test.validateTrustBundle("-----BEGIN CERTIFICATE-----"),
    /malformed certificates/,
  );
  assert.throws(
    () =>
      __test.validateTrustBundle(
        "-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----",
      ),
  );
  assert.doesNotThrow(() => __test.validateTrustBundle(TEST_CERTIFICATE));

  const transport = __test.newTlsTransport(
    "backend.gco.internal",
    TEST_CERTIFICATE,
  );
  assert.equal(transport.serverName, "backend.gco.internal");
  assert.equal(transport.agent.options.keepAlive, true);
  assert.equal(transport.agent.options.rejectUnauthorized, true);
  assert.equal(transport.agent.options.minVersion, "TLSv1.2");
  const identityError = transport.verifyServerIdentity("ignored.example", {
    subject: { CN: "different.example" },
  });
  assert.ok(identityError instanceof Error);
  transport.agent.destroy();
});

test("TLS trust refreshes are shared, bounded-stale, and fail closed", async () => {
  setEnvironment(validTlsEnvironment());
  const client = seedClient(
    __test.ssmClients,
    "us-east-1",
    (_command, callNumber) => {
      if (callNumber === 1) {
        return { Parameter: { Value: TEST_CERTIFICATE } };
      }
      throw new Error("SSM unavailable");
    },
  );

  const first = await __test.getTlsTransport(1_000);
  assert.equal((await __test.getTlsTransport(1_100)).agent, first.agent);
  assert.equal(client.calls.length, 1);
  assert.deepEqual(client.calls[0].input, { Name: "/gco/backend/root-ca" });

  assert.equal((await __test.getTlsTransport(1_300)).agent, first.agent);
  assert.equal(client.calls.length, 2);
  assert.equal((await __test.getTlsTransport(1_301)).agent, first.agent);
  assert.equal(client.calls.length, 2);
  await assert.rejects(
    __test.getTlsTransport(4_601),
    /Backend TLS trust bundle is unavailable/,
  );
  first.agent.destroy();

  __test.resetRuntimeStateForTest();
  let release;
  const concurrent = seedClient(__test.ssmClients, "us-east-1", () =>
    new Promise((resolve) => {
      release = resolve;
    }),
  );
  const pendingA = __test.getTlsTransport(5_000);
  const pendingB = __test.getTlsTransport(5_000);
  assert.equal(concurrent.calls.length, 1);
  release({ Parameter: { Value: TEST_CERTIFICATE } });
  const [transportA, transportB] = await Promise.all([pendingA, pendingB]);
  assert.equal(transportA.agent, transportB.agent);
  transportA.agent.destroy();

  for (const response of [
    undefined,
    null,
    {},
    { Parameter: null },
    { Parameter: {} },
    { Parameter: { Value: null } },
  ]) {
    __test.resetRuntimeStateForTest();
    seedClient(__test.ssmClients, "us-east-1", () => response);
    await assert.rejects(
      __test.getTlsTransport(),
      /Backend TLS trust bundle is unavailable/,
    );
  }
});

test("regional discovery validates SSM, paginated ALB ownership, tags, and cache", async () => {
  setEnvironment(validRegionalEnvironment());
  const endpoint = "internal-gco.us-west-2.elb.amazonaws.com";
  assert.equal(__test.awsUrlSuffix(), "amazonaws.com");
  setEnvironment({ AWS_URL_SUFFIX: undefined });
  assert.throws(__test.awsUrlSuffix, /not configured/);
  setEnvironment({ AWS_URL_SUFFIX: "amazonaws.com" });
  assert.equal(__test.validatedDnsName(`${endpoint}.`), endpoint);
  setEnvironment({ AWS_URL_SUFFIX: "amazonaws.com.cn" });
  assert.equal(
    __test.validatedDnsName("internal-gco.elb.amazonaws.com.cn"),
    "internal-gco.elb.amazonaws.com.cn",
  );
  setEnvironment({ AWS_URL_SUFFIX: "amazonaws.eu" });
  assert.equal(
    __test.validatedDnsName("internal-gco.eusc-de-east-1.elb.amazonaws.eu"),
    "internal-gco.eusc-de-east-1.elb.amazonaws.eu",
  );
  setEnvironment({ AWS_URL_SUFFIX: "amazonaws.com" });
  for (const invalid of [
    "",
    "not-a-dns-name",
    "public.example.com",
    "https://internal-gco.us-west-2.elb.amazonaws.com",
  ]) {
    assert.throws(() => __test.validatedDnsName(invalid), /Registered backend is invalid/);
  }

  const ssm = seedClient(__test.ssmClients, "us-east-1", () => ({
    Parameter: { Value: `${endpoint}.` },
  }));
  const alb = {
    calls: [],
    async send(command) {
      this.calls.push(command);
      if (command.constructor.name === "DescribeLoadBalancersCommand") {
        if (!command.input.Marker) {
          return {
            LoadBalancers: [loadBalancer({ DNSName: "other.elb.amazonaws.com" })],
            NextMarker: "page-2",
          };
        }
        assert.deepEqual(command.input, { Marker: "page-2" });
        return { LoadBalancers: [loadBalancer()] };
      }
      if (command.constructor.name === "DescribeTagsCommand") {
        assert.deepEqual(command.input, {
          ResourceArns: [loadBalancer().LoadBalancerArn],
        });
        return {
          TagDescriptions: [
            {
              Tags: [
                { Key: "elbv2.k8s.aws/cluster", Value: "gco-test-us-west-2" },
                {
                  Key: "gco.aws/gateway",
                  Value: "gco-system/gco-gateway",
                },
              ],
            },
          ],
        };
      }
      throw new Error(`Unexpected command ${command.constructor.name}`);
    },
  };
  __test.elbClients.set("us-west-2", alb);

  assert.equal(await __test.resolveRegionalEndpoint(100), endpoint);
  assert.equal(await __test.resolveRegionalEndpoint(101), endpoint);
  assert.equal(ssm.calls.length, 1);
  assert.equal(alb.calls.length, 3);
  assert.deepEqual(ssm.calls[0].input, {
    Name: "/gco-test/alb-hostname-us-west-2",
  });

  assert.equal(await __test.resolveRegionalEndpoint(160), endpoint);
  assert.equal(ssm.calls.length, 2);
  assert.equal(alb.calls.length, 6);

  setEnvironment({ REGIONAL_ENDPOINT_CACHE_TTL_SECONDS: "0" });
  assert.equal(__test.regionalEndpointCacheTtl(), 0);
  assert.equal(await __test.resolveRegionalEndpoint(161), endpoint);
  assert.equal(ssm.calls.length, 3);
  assert.equal(alb.calls.length, 9);
  assert.equal(__test.regionalEndpointCache.size, 1);
});

test("regional ownership rejects legacy Ingress ownership tags", async () => {
  const region = "us-west-2";
  const endpoint = "internal-gco.us-west-2.elb.amazonaws.com";
  const account = "123456789012";
  const project = "gco-test";
  const legacyTagSets = [
    [
      { Key: "eks:eks-cluster-name", Value: "gco-test-us-west-2" },
      { Key: "ingress.eks.amazonaws.com/stack", Value: "gco" },
    ],
    [
      { Key: "elbv2.k8s.aws/cluster", Value: "gco-test-us-west-2" },
      { Key: "ingress.k8s.aws/stack", Value: "gco-system/gco-ingress" },
    ],
  ];

  for (const tags of legacyTagSets) {
    __test.elbClients.set(region, ownershipClient([loadBalancer()], tags));
    await assert.rejects(
      __test.validateRegionalEndpointOwnership(
        endpoint,
        region,
        account,
        project,
      ),
      /not the GCO Gateway/,
    );
  }
});

test("regional ownership rejects missing, public, foreign, and untagged ALBs", async (t) => {
  const region = "us-west-2";
  const endpoint = "internal-gco.us-west-2.elb.amazonaws.com";
  const account = "123456789012";
  const project = "gco-test";
  const validTags = [
    { Key: "eks:eks-cluster-name", Value: "gco-test-us-west-2" },
    { Key: "gco.aws/gateway", Value: "gco-system/gco-gateway" },
  ];

  async function rejected(name, balancers, tags, pattern, responses) {
    await t.test(name, async () => {
      __test.elbClients.set(
        region,
        ownershipClient(balancers, tags, responses),
      );
      await assert.rejects(
        __test.validateRegionalEndpointOwnership(
          endpoint,
          region,
          account,
          project,
        ),
        pattern,
      );
    });
  }

  await rejected("missing", [], [], /does not exist/);
  await rejected(
    "omitted load balancers",
    [],
    [],
    /does not exist/,
    { loadBalancers: {} },
  );
  await rejected(
    "omitted DNS name",
    [loadBalancer({ DNSName: undefined })],
    [],
    /does not exist/,
  );
  await rejected(
    "non-application",
    [loadBalancer({ Type: "network" })],
    validTags,
    /not an internal ALB/,
  );
  await rejected(
    "internet-facing",
    [loadBalancer({ Scheme: "internet-facing" })],
    validTags,
    /not an internal ALB/,
  );
  await rejected(
    "malformed ARN",
    [loadBalancer({ LoadBalancerArn: "not-an-arn" })],
    validTags,
    /ownership is invalid/,
  );
  await rejected(
    "omitted ARN",
    [loadBalancer({ LoadBalancerArn: undefined })],
    validTags,
    /ownership is invalid/,
  );
  await rejected(
    "wrong ARN service",
    [
      loadBalancer({
        LoadBalancerArn:
          "arn:aws:ec2:us-west-2:123456789012:loadbalancer/app/gco/abc",
      }),
    ],
    validTags,
    /ownership is invalid/,
  );
  await rejected(
    "foreign region",
    [
      loadBalancer({
        LoadBalancerArn:
          "arn:aws:elasticloadbalancing:eu-west-1:123456789012:loadbalancer/app/gco/abc",
      }),
    ],
    validTags,
    /ownership is invalid/,
  );
  await rejected(
    "foreign account",
    [
      loadBalancer({
        LoadBalancerArn:
          "arn:aws:elasticloadbalancing:us-west-2:999999999999:loadbalancer/app/gco/abc",
      }),
    ],
    validTags,
    /ownership is invalid/,
  );
  await rejected(
    "wrong cluster tag",
    [loadBalancer()],
    [
      { Key: "eks:eks-cluster-name", Value: "someone-else" },
      { Key: "gco.aws/gateway", Value: "gco-system/gco-gateway" },
    ],
    /not owned by the GCO cluster/,
  );
  await rejected(
    "wrong platform tag",
    [loadBalancer()],
    [{ Key: "eks:eks-cluster-name", Value: "gco-test-us-west-2" }],
    /not the GCO Gateway/,
  );
  await rejected(
    "omitted tag descriptions",
    [loadBalancer()],
    [],
    /not owned by the GCO cluster/,
    { tags: {} },
  );
  await rejected(
    "omitted tags",
    [loadBalancer()],
    [],
    /not owned by the GCO cluster/,
    { tags: { TagDescriptions: [{}] } },
  );
});

test("regional resolution rejects incomplete registry configuration and bad values", async () => {
  const invalidConfigurations = [
    { REGISTRY_REGION: undefined },
    { REGISTRY_REGION: "invalid" },
    { TARGET_REGION: undefined },
    { TARGET_REGION: "invalid" },
    { PROJECT_NAME: undefined },
    { PROJECT_NAME: "" },
    { AWS_ACCOUNT_ID: undefined },
    { AWS_ACCOUNT_ID: "" },
  ];
  for (const overrides of invalidConfigurations) {
    setEnvironment(validRegionalEnvironment(overrides));
    await assert.rejects(
      __test.resolveRegionalEndpoint(100),
      /Regional endpoint registry is not configured/,
    );
  }

  setEnvironment(validRegionalEnvironment());
  for (const response of [undefined, null, {}, { Parameter: null }, { Parameter: {} }]) {
    __test.resetRuntimeStateForTest();
    seedClient(__test.ssmClients, "us-east-1", () => response);
    await assert.rejects(__test.resolveRegionalEndpoint(101));
  }

  __test.resetRuntimeStateForTest();
  seedClient(__test.ssmClients, "us-east-1", () => ({
    Parameter: { Value: "attacker.example.com" },
  }));
  await assert.rejects(
    __test.resolveRegionalEndpoint(101),
    /Registered backend is invalid/,
  );

  __test.resetRuntimeStateForTest();
  seedClient(__test.ssmClients, "us-east-1", () => ({
    Parameter: { Value: "internal-gco.us-west-2.elb.amazonaws.com" },
  }));
  __test.elbClients.set("us-west-2", ownershipClient([], []));
  await assert.rejects(
    __test.resolveRegionalEndpoint(),
    /Registered backend does not exist/,
  );
});
