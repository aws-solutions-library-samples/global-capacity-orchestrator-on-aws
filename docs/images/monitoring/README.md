# Monitoring dashboard screenshots

Screenshots of the curated GCO Grafana dashboards used by
[`docs/MONITORING.md`](../../MONITORING.md). They are **generated on demand**
against a live cluster rather than committed by hand, so this directory ships
only this README until an operator regenerates the images.

## Table of Contents

- [Regenerate](#regenerate)
- [Images](#images)

## Regenerate

Grafana is private (`ClusterIP`, no public endpoint), so first port-forward it,
then run the capture script:

```bash
# 1. Port-forward Grafana (tunnel through SSM if the API endpoint is private):
gco monitoring open --region us-east-1 --via-ssm i-0123456789abcdef0

# 2. In another shell, capture the dashboards (Chromium fetched once):
playwright install chromium
python scripts/capture_monitoring_screenshots.py \
    --username admin --password "$GCO_GRAFANA_ADMIN_PASSWORD"
```

## Images

The script writes one PNG per curated dashboard:

| File | Dashboard |
|------|-----------|
| `grafana-gpu-dcgm.png` | GCO GPU (DCGM) |
| `grafana-schedulers.png` | GCO Schedulers & Queues |
| `grafana-keda.png` | GCO KEDA Autoscaling |
| `grafana-services.png` | GCO Services |

The dashboard set is kept in sync with
`lambda/kubectl-applier-simple/manifests/post-helm-grafana-dashboards.yaml` by
`tests/test_cluster_observability_screenshots.py`.
