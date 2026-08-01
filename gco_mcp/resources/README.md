# MCP Resources

MCP resource definitions and live-state adapters. Static modules use `@mcp.resource()` decorators; live and flag-gated modules expose `register(mcp)` functions. `resources.register_all_resources()` imports and registers the complete catalog against the shared FastMCP server.

## Table of Contents

- [Files](#files)
- [Resource Families](#resource-families)
- [How Resources Work](#how-resources-work)
- [Adding a New Resource Group](#adding-a-new-resource-group)

## Files

| File | Scheme or role | Description |
|------|----------------|-------------|
| `_eks.py` | shared helper | Builds configured-project, account-qualified, partition-aware [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html) contexts for region-pinned live reads. |
| `ci.py` | `ci://` | GitHub Actions workflows, composite actions, scripts, templates, and policy files. |
| `clients.py` | `clients://` | API client examples for Python, curl, and the AWS CLI. |
| `cluster.py` | `gco://cluster/` | Live regional NodePool and pending-pod topology. |
| `config.py` | `config://` | Raw [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) configuration and environment-variable reference. |
| `costs.py` | `costs://` | Live cost-summary views for a bounded day window. |
| `demos.py` | `demos://` | Demo walkthroughs and scripts. |
| `docs.py` | `docs://` | Documentation, package guides, ADRs, and examples enriched with metadata. |
| `iam_policies.py` | `iam://` | [IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/introduction.html) policy templates. |
| `images.py` | `images://` | [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) repository, tag, image, and replication views. |
| `inference.py` | `gco://inference/` | Live inference endpoint records from the desired-state store. |
| `infra.py` | `infra://` | Dockerfiles, Helm chart configuration, and infrastructure metadata. |
| `jobs.py` | `gco://jobs/` | Live, explicitly regional Kubernetes Job YAML. |
| `k8s.py` | `k8s://`, `gco://k8s/` | Deployed manifests and explicitly regional live Kubernetes objects. |
| `mission.py` | `mission://` | Mission sessions, reports, and audit replay; gated by `GCO_ENABLE_MISSION`. |
| `scripts.py` | `scripts://` | Utility scripts for access, versioning, and operations. |
| `self.py` | `mcp://` | Live tool/resource indexes and the authoritative feature-flag-to-tool map. |
| `source.py` | `source://` | Allowlisted source and project-configuration browser. |
| `tasks.py` | `tasks://` | FastMCP background-task state. |
| `tests.py` | `tests://` | Test-suite documentation, helpers, Python tests, and BATS files. |

## Resource Families

The live catalog is available from `mcp://gco/resources/index`. Important entry points include:

- `docs://gco/index` — documentation, package guides, ADRs, and examples.
- `k8s://gco/manifests/index` — manifests applied during deployment.
- `images://gco/index` — ECR repositories and image metadata.
- `gco://jobs/{region}/{job_name}` and `gco://k8s/{region}/{namespace}/{kind}/{name}` — region-pinned EKS reads. Legacy regionless forms remain registered only to return a structured `eks_region_required` error; they never use the ambient kubectl context.
- `gco://inference/{endpoint_name}` and `gco://cluster/{region}/topology` — live endpoint and cluster state.
- `costs://gco/summary/{days_window}` and `tasks://gco/{task_id}` — windowed operational views.
- `mission://sessions/{session_id}` — enabled only with `GCO_ENABLE_MISSION=true` (or the umbrella flag).
- `mcp://gco/tools/index`, `mcp://gco/resources/index`, and `mcp://gco/feature-flags` — self-introspection.

## How Resources Work

Each resource has a URI, a read-only handler, and a description. Static resources are concrete URIs; parameterized resources are templates. FastMCP's Resources As Tools transform also exposes `list_resources` and `read_resource`, so tool-only clients can reach the same catalog.

Handlers that read project files use allowlists and resolved-path checks. Kubernetes
handlers require an explicit region and an account-qualified EKS context whose
cluster prefix comes from the merged CLI ``project_name`` configuration, including
``GCO_PROJECT_NAME`` overrides. Resource responses are read-only, but live handlers
can still call AWS, the GCO CLI, or ``kubectl``, so their descriptions identify the
backing system.

## Adding a New Resource Group

1. Create the resource module and define decorated resources or a `register(mcp)` function.
2. Add the module to `resources/__init__.py`; call its `register` function there when needed.
3. Add the family to `docs://gco/index`, the server instructions, this file, and the main MCP guide.
4. Ensure every handler has a useful description and validates any path, identifier, region, or bounded numeric input before external access.
5. Update the existing MCP resource coverage and catalog-count expectations when the live registry intentionally changes.
