# GCO Core

The `gco` package contains the AWS CDK infrastructure, in-cluster services, shared data models, and validated configuration used by Global Capacity Orchestrator on AWS.

## Package Map

| Package | Responsibility | Detailed inventory |
|---------|----------------|--------------------|
| [`stacks/`](stacks/) | Global, regional, API, analytics, monitoring, and supporting AWS CDK stacks, plus shared constructs and cdk-nag policy. | [`stacks/README.md`](stacks/README.md) |
| [`services/`](services/) | FastAPI applications, Kubernetes controllers and workers, persistence helpers, authentication, metrics, and TLS support. | [`services/README.md`](services/README.md) |
| [`models/`](models/) | Typed health, cluster, manifest, and inference domain models shared across runtime layers. | [`models/README.md`](models/README.md) |
| [`config/`](config/) | `cdk.json` loading, defaults, schema validation, and managed configuration behavior. | [`config/README.md`](config/README.md) |

The child READMEs are authoritative for file-by-file inventories; this parent intentionally stays at subsystem level so a module is documented in one place rather than copied into multiple tables.

For system boundaries and request flows, see [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md). For contributor workflows and validation commands, see [`CONTRIBUTING.md`](../CONTRIBUTING.md).
