# CDK Configuration Package

This package loads, validates, and exposes the deployment context consumed by
GCO's [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) stacks.

## Table of Contents

- [Responsibilities](#responsibilities)
- [Usage](#usage)
- [Validation Model](#validation-model)
- [Source Files](#source-files)

## Responsibilities

`ConfigLoader` reads `cdk.json` context, merges documented defaults, validates
cross-field constraints, and returns typed configuration for regions, [EKS](https://docs.aws.amazon.com/eks/latest/userguide/what-is-eks.html),
[API Gateway](https://docs.aws.amazon.com/apigateway/latest/developerguide/welcome.html), private backend TLS, storage, analytics, observability, tracing, and
capacity history.

## Usage

```python
from gco.config import ConfigLoader

config = ConfigLoader(app)
regions = config.get_deployment_regions()
tls_policy = config.get_backend_tls_config()
```

## Validation Model

Invalid operator input fails during synthesis through `ConfigValidationError`.
Validation runs for every context, including one without `project_name`, which
then takes its default `gco`. It includes project naming, supported regions,
numeric bounds, feature sub-block types, and security-sensitive lifecycle
relationships such as TLS root overlap and trust-cache timing. Secrets and
credentials do not belong in CDK context.

A block that no longer takes effect (`DEPRECATED_CONTEXT_KEYS`, for example
`alb_config`) still validates, because `gco upgrade` keeps an operator's
`cdk.json` byte for byte: the loader ignores it and records a warning naming
the block and what to do instead. Warnings are logged at `WARNING`, which
`cdk synth` prints, and collected in `ConfigLoader.warnings`.

Before any getter reads a block, `_validate_block_shapes` checks every block in
`BLOCK_KEYS` (the blocks whose own validator does not already do so): a block
present in cdk.json must be an object, a key outside its set fails with the
full path, and `_comment*` keys are always allowed. `DEPRECATED_BLOCK_KEYS`
lists the keys inside blocks that warn and are ignored. A top-level key in
GCO's `snake_case` style that is neither a block nor in `OTHER_CONTEXT_KEYS`
draws a warning.

## Source Files

| File | Description |
|------|-------------|
| `__init__.py` | Exports the package's public configuration API. |
| `accelerator_catalog.json` | Reviewed NVIDIA GPU and AWS Neuron instance catalog consumed by configuration and capacity validation. |
| `config_loader.py` | Deployment defaults, typed accessors, cross-field validation, and schema rules. The opt-in `eks_capabilities` block (AWS-managed ACK and kro) is validated through the CDK-free `gco/eks_capabilities_config.py`, and the self-managed Argo CD block (`helm.argocd`) through `gco/argocd_config.py`; the CLI and live validation share both. The `tracing` block and its run-scoped `tracing_overrides` context resolve through `resolve_tracing_config`, which the regional stack also calls. The `gateway_health_check` block (the platform ALB target-group probe) is validated and rendered by the CDK-free `gco/gateway_health_check_config.py`; the FSx for Lustre tables (deployment types, per-type throughput and defaults, capacity increments) live in the CDK-free `gco/fsx_lustre_config.py`, which `gco stacks fsx` shares. `parse_block_flag` parses the boolean flags inside cdk.json blocks (JSON booleans; the string `"true"` with a deprecation warning; any other string is an error, because earlier releases read it as on and reading `"false"` as off would delete a running storage feature), and `parse_bool_flag` the top-level flags `cdk --context` passes as `"true"` / `"false"` strings. `STACK_BLOCK_FLAGS` lists the enabled flags the regional and global stacks read from the raw context (`queue_processor`, `drift_detection`, `mcp_server`, `volcano_image_mirror`, the optional `helm` charts, `images.replication`); the loader validates them and the stacks read them with `read_block_flag`, so both apply `parse_block_flag` the same way. `global_accelerator.health_check_path` must be one of the paths the services answer without authentication, the FastAPI-free `gco/unauthenticated_paths.py` set that the auth middleware applies. |
| `runai_api_contract.json` | The NVIDIA Run:ai API routes, parameters and fields `gco capacity runai` reads; `.github/scripts/check_runai_api_contract.py` checks it monthly against every published API reference version from its `min_version`. |
