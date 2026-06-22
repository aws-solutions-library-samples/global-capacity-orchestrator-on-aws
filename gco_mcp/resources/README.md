# MCP Resources

MCP resource definitions — one file per URI scheme. Each module registers resources against the shared FastMCP server instance via `@mcp.resource()` decorators.

## Table of Contents

- [Files](#files)
- [How Resources Work](#how-resources-work)
- [Adding a New Resource Group](#adding-a-new-resource-group)

## Files

| File | Scheme | Description |
|------|--------|-------------|
| `docs.py` | `docs://` | Documentation, examples (with metadata headers), quickstart, contributing guide, example creation guide |
| `source.py` | `source://` | Full source code browser — all `.py`, `.yaml`, `.json`, `.sh`, `.md` files across the project |
| `k8s.py` | `k8s://` | Kubernetes manifests applied to the EKS cluster during deployment |
| `iam_policies.py` | `iam://` | IAM policy templates (full-access, read-only, namespace-restricted) |
| `infra.py` | `infra://` | Dockerfiles, Helm chart config, security scanner configs |
| `ci.py` | `ci://` | GitHub Actions workflows, composite actions, scripts, issue/PR templates, CodeQL config |
| `demos.py` | `demos://` | Demo walkthroughs, live demo scripts, presentation materials |
| `clients.py` | `clients://` | API client examples (Python boto3, curl with SigV4, AWS CLI) |
| `scripts.py` | `scripts://` | Utility scripts (cluster access, versioning, CDK synthesis testing) |
| `tests.py` | `tests://` | Test suite README, infrastructure helpers, test files, BATS shell tests |
| `config.py` | `config://` | CDK configuration, parsed feature toggles, environment variable documentation |

## How Resources Work

Resources are read-only content that the LLM can fetch on demand. Each resource has:

1. A **URI** (e.g. `docs://gco/examples/simple-job`) that the LLM uses to request it
2. A **handler function** that reads the file from disk and returns its content
3. An **index resource** (e.g. `docs://gco/index`) that lists all available items in the group

The `docs.py` module is special — it enriches example manifests with metadata headers (category, GPU requirements, submission command) so the LLM has context for creating similar jobs.

## Adding a New Resource Group

1. Create a new file (e.g. `my_resources.py`) in this directory
2. Import `mcp` from `server.py` and define resources with `@mcp.resource("scheme://gco/...")`
3. Register the module in `resources/__init__.py`
4. Add the scheme to the `docs://gco/index` cross-reference list in `docs.py`
5. Add tests in `tests/test_mcp_resources_new.py` and `tests/test_mcp_integration.py`
