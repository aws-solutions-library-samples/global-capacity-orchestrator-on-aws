# Images

Screenshots and visual assets for GCO (Global Capacity Orchestrator on AWS) documentation. Generated infrastructure diagrams live under [`diagrams/infra_diagrams/`](../diagrams/infra_diagrams/README.md) and generated code flowcharts live under [`diagrams/code_diagrams/`](../diagrams/code_diagrams/README.md); keeping generated diagrams beside their tooling prevents unsourced architecture images from drifting.

## Table of Contents

- [Reference Architecture Diagrams](#reference-architecture-diagrams)
- [MCP Server Screenshots](#mcp-server-screenshots)
- [In-Cluster Monitoring Screenshots](#in-cluster-monitoring-screenshots)
- [Platform Add-on Screenshots](#platform-add-on-screenshots)
- [SageMaker Studio Screenshots](#sagemaker-studio-screenshots)

## Reference Architecture Diagrams

These curated reference views complement the generated infrastructure diagrams
and preserve the platform story at three levels. They are retained rendered
artifacts: **their editable source is not present in this repository**, and no
in-repository generator is claimed for them. Do not substitute the generated
CDK views or invent a draw.io/PowerPoint/SVG provenance when refreshing them.

| Asset | View | Provenance / update path |
|-------|------|--------------------------|
| [gco_ref_architecture_part1.png](gco_ref_architecture_part1.png) | Multi-region reference architecture | Curated rendered PNG; editable source unavailable in this repository |
| [gco_ref_architecture_part2.png](gco_ref_architecture_part2.png) | Regional EKS architecture | Curated rendered PNG; editable source unavailable in this repository |
| [gco_ref_architecture_part3.png](gco_ref_architecture_part3.png) | Security controls and request flow | Curated rendered PNG; editable source unavailable in this repository |
| [`diagrams/infra_diagrams/`](../diagrams/infra_diagrams/README.md) | CDK-derived stack and aggregate topology | Reproducible structure via `python diagrams/generate.py --infra-only` |
| [`diagrams/code_diagrams/`](../diagrams/code_diagrams/README.md) | Per-function control flow | Regenerate with fixed `SOURCE_DATE_EPOCH` and exact `GCO_DIAGRAM_SOURCE_COMMIT` through `diagrams/generate.py` |

## MCP Server Screenshots

Screenshots demonstrating the GCO MCP server integration with Kiro. Tool counts shown inside historical screenshots are illustrative; the live, feature-flag-aware counts are documented in [`gco_mcp/README.md`](../gco_mcp/README.md).

| Image | Description |
|-------|-------------|
| [gco_mcp_kiro.png](gco_mcp_kiro.png) | GCO MCP server connected in Kiro |
| [gco_mcp_list_stacks.png](gco_mcp_list_stacks.png) | Listing deployed [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) stacks via natural language |
| [gco_mcp_check_capacity.png](gco_mcp_check_capacity.png) | Checking GPU capacity for g5.xlarge in us-east-1 |
| [gco_mcp_calculating_pi.png](gco_mcp_calculating_pi.png) | Using the MCP server to write a PI calculation manifest, run it on available capacity, and print the logs |
| [pi_calculation_manifest.png](pi_calculation_manifest.png) | The PI calculation Kubernetes Job manifest |
| [gco_mcp_ai_recommend.png](gco_mcp_ai_recommend.png) | Using the MCP capacity recommendation tool |
| [gco_mcp_cost_summary.png](gco_mcp_cost_summary.png) | Viewing a cost summary via natural language |

## In-Cluster Monitoring Screenshots

The self-hosted observability UIs, all reached through
`gco monitoring open` (ClusterIP services, no public endpoints — see
[`docs/MONITORING.md`](../docs/MONITORING.md)). The Grafana dashboards and the
OpenCost UI are captured by
[`scripts/capture_monitoring_screenshots.py`](../scripts/capture_monitoring_screenshots.py).

| Image | Description |
|-------|-------------|
| [mlflow-ui.png](mlflow-ui.png) | MLflow tracking server run view — the run logged by [`examples/mlflow-tracking-job.yaml`](../examples/mlflow-tracking-job.yaml), with its metric, parameters, and Finished status |
| [grafana-services.png](grafana-services.png) | GCO Services dashboard — per-service request rate and p95 latency |
| [grafana-schedulers.png](grafana-schedulers.png) | GCO Schedulers and Queues dashboard — pending pods, Kueue workloads, active Jobs |
| [grafana-keda.png](grafana-keda.png) | GCO KEDA Autoscaling dashboard — active scalers and scaler errors |
| [grafana-gpu-dcgm.png](grafana-gpu-dcgm.png) | GCO GPU (DCGM) dashboard — per-GPU utilization, framebuffer, temperature, power |
| [grafana-cost.png](grafana-cost.png) | GCO Cost dashboard — cluster and projected monthly cost, node and namespace splits |
| [opencost-ui.png](opencost-ui.png) | Native OpenCost UI — cost allocation table with per-namespace efficiency |

## Platform Add-on Screenshots

The dashboards of the opt-in platform add-ons, both reached through
`gco gitops open` and `gco crossplane open` (ClusterIP services, no public
endpoints). The kind CI job `integration:kind:platform-addons` captures them
with the code behind `gco gitops screenshot` and `gco crossplane screenshot`,
once the example workloads have run, and uploads them as the
`platform-addon-dashboards` artifact; these copies come from that artifact
(see [`.github/CI.md`](../.github/CI.md#the-platform-add-ons-against-the-real-charts)).

| Image | Description |
|-------|-------------|
| [argocd-ui.png](argocd-ui.png) | Argo CD Applications view — the `gco-gitops-hello` Application from [`examples/argocd-gitops-job.yaml`](../examples/argocd-gitops-job.yaml), repointed at the commit under test, Synced and Healthy in the `gco-tenants` project ([`docs/GITOPS.md`](../docs/GITOPS.md)) |
| [crossview-dashboard.png](crossview-dashboard.png) | Crossview dashboard — the BatchJob API from [`examples/crossplane-batch-api.yaml`](../examples/crossplane-batch-api.yaml) (one XRD, one Composition, the go-templating Function) and the `gco-crossplane-hello` composite resource from [`examples/crossplane-batch-job.yaml`](../examples/crossplane-batch-job.yaml), Synced and Ready ([`docs/CROSSPLANE.md`](../docs/CROSSPLANE.md)) |

## SageMaker Studio Screenshots

Screenshots of the GCO analytics environment running in [SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html) Studio.

| Image | Description |
|-------|-------------|
| [sagemaker_studio_landing_screen.png](sagemaker_studio_landing_screen.png) | SageMaker Studio landing screen after login via `gco analytics studio login` |
| [sagemaker_studio_jupyterlab_app.png](sagemaker_studio_jupyterlab_app.png) | JupyterLab app running inside a Studio space |
| [sagemaker_studio_jupyterlab_landing_page.png](sagemaker_studio_jupyterlab_landing_page.png) | JupyterLab landing page with file browser and launcher |
| [sagemaker_studio_cloning_gco_in_jupyter.png](sagemaker_studio_cloning_gco_in_jupyter.png) | Cloning the GCO repository from a JupyterLab terminal |
| [sagemaker_studio_emr_serverless.png](sagemaker_studio_emr_serverless.png) | [EMR Serverless](https://docs.aws.amazon.com/emr/latest/EMR-Serverless-UserGuide/emr-serverless.html) application visible from the Studio Data panel |
| [sagemaker_studio_canvas_data_wrangler.png](sagemaker_studio_canvas_data_wrangler.png) | SageMaker Canvas Data Wrangler when the `canvas` sub-toggle is enabled |
| [sagemaker_studio_mlflow.png](sagemaker_studio_mlflow.png) | SageMaker Studio MLflow application visible from the MLflow panel |
