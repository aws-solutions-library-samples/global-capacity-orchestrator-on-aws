# Data Models

Pydantic data models used across the GCO codebase for request/response validation, serialization, and type safety.

## Table of Contents

- [Files](#files)
- [Usage](#usage)
- [Adding a New Model](#adding-a-new-model)

## Files

| File | Description |
|------|-------------|
| `__init__.py` | Re-exports all models for convenient `from gco.models import ...` |
| `cluster_models.py` | Cluster info, node details, pod/container status models |
| `health_models.py` | Health check responses, resource utilization metrics, threshold configuration |
| `inference_models.py` | Inference endpoint specs (image, GPU, replicas), per-region status, canary config |
| `manifest_models.py` | Manifest submission requests, validation results, resource limit models |

## Usage

```python
from gco.models import ManifestSubmission, HealthStatus, InferenceEndpointSpec
```

All models use Pydantic v2 with strict validation. Fields use `Field(...)` with descriptions for automatic API documentation.

## Adding a New Model

1. Create or extend a model file in this directory
2. Export it from `__init__.py`
3. Use it in the relevant service or API route
4. Add validation tests in `tests/test_models*.py`
