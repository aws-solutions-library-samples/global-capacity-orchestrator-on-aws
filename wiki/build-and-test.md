# How we build & test

GCO treats CI as part of the product: every invariant worth keeping is
enforced by a check, and the checks themselves are documented in one
reference —
[.github/CI.md](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/.github/CI.md).
This page is the fly-over.

## CI at a glance

Six primary workflows run on every push and pull request:

- **Unit Tests** — the sharded pytest suite with a combined-coverage gate,
  plus CDK synth and a 34-combination config matrix, lockfile freshness,
  BATS, CLI and autopilot smoke checks, and MCP install/launch smoke.
- **Integration Tests** — per-Dockerfile build and functional container
  contracts, kind-based end-to-end clusters with real NetworkPolicy
  enforcement, Kubernetes manifest schema validation, the autopilot boot
  probe, and MCP server tests.
- **Floci Tests** — an emulated-AWS layer: production code issuing real SDK
  requests against a local emulator, with zero AWS credentials in CI.
- **Security** — bandit, pip-audit, npm audit, Trivy (filesystem and
  per-image), trufflehog, gitleaks, semgrep, checkov, KICS, and CodeQL.
- **Linting** — actionlint, hadolint, markdownlint, mypy (strict), ruff,
  shellcheck, and yamllint. The strict MkDocs build of this wiki runs here
  too.
- **Inference streaming proxy** — native Node.js tests for the production
  streaming Lambda, with their own exact 100% line/function/branch gate.

Eight satellite workflows cover the two-stage release path, weekly CVE
re-scans, the monthly dependency-drift issue, Pages publication, the pinned
Mooncake image contract, pull-request type labelling, and real-Grafana
dashboard provisioning. See the CI reference for their exact triggers and
permissions.

## The coverage gate

The unit suite enforces an **exact 100% line + branch coverage floor** —
shards each run a slice, a combining job merges their coverage, and the final
report applies the floor. The HTML report published from every `main` run is embedded in this
site:

**[Browse the live coverage report](https://aws-solutions-library-samples.github.io/global-capacity-orchestrator-on-aws/coverage/)**

The README's coverage badge reads a JSON endpoint generated from the same
run, so the badge, the report, and the gate can never tell three different
stories.

## Quality signals beyond tests

- **Single-source pins** — every version CI installs is declared in exactly
  one place; guard tests reject reintroduced copies.
- **Guard-test culture** — documentation indexes, workflow conventions,
  fork-migration URL coverage, and supply-chain properties (checksummed
  downloads, pinned actions) are all enforced by tests, not review memory.
- **Live release validation stays local** — the deploy-test-destroy harness
  against real AWS runs only as an explicitly authorized local process;
  ordinary CI stays mocked/offline by design.

## Developing locally

The dev container is the supported path for everything from running tests
to deploying stacks.
[CONTRIBUTING.md](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/CONTRIBUTING.md)
documents the pre-PR verification sequence (format, lint, types, catalog
validation, tests with the coverage floor) and the container-based lockfile
regeneration that is the only supported dependency workflow.
[tests/README.md](https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/blob/main/tests/README.md)
maps the suite's organization — every test module is registered there, and
a guard test keeps it that way.
