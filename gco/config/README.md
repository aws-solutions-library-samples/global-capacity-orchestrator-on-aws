# CDK Configuration Package

This package loads, validates, and exposes the deployment context consumed by
GCO's CDK stacks.

## Table of Contents

- [Responsibilities](#responsibilities)
- [Usage](#usage)
- [Validation Model](#validation-model)
- [Source Files](#source-files)

## Responsibilities

`ConfigLoader` reads `cdk.json` context, merges documented defaults, validates
cross-field constraints, and returns typed configuration for regions, EKS,
API Gateway, private backend TLS, storage, analytics, observability, and
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
Validation includes project naming, supported regions, numeric bounds, feature
sub-block types, and security-sensitive lifecycle relationships such as TLS
root overlap and trust-cache timing. Secrets and credentials do not belong in
CDK context.

## Source Files

- [`__init__.py`](__init__.py) exports the package's public API.
- [`config_loader.py`](config_loader.py) contains defaults, accessors, and
  validation rules.
