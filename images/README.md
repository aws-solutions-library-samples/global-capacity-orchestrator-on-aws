# Images

Screenshots and visual assets for GCO (Global Capacity Orchestrator on AWS) documentation. Generated infrastructure diagrams live under [`diagrams/infra_diagrams/`](../diagrams/infra_diagrams/README.md) and generated code flowcharts live under [`diagrams/code_diagrams/`](../diagrams/code_diagrams/README.md); keeping generated diagrams beside their tooling prevents unsourced architecture images from drifting.

## Table of Contents

- [Reference Architecture Diagrams](#reference-architecture-diagrams)
- [MCP Server Screenshots](#mcp-server-screenshots)
- [SageMaker Studio Screenshots](#sagemaker-studio-screenshots)

## Reference Architecture Diagrams

These curated reference views complement the generated infrastructure diagrams and preserve the platform story at three levels:

- [Part 1 — Multi-region reference architecture](gco_ref_architecture_part1.png)
- [Part 2 — Regional EKS architecture](gco_ref_architecture_part2.png)
- [Part 3 — Security controls and request flow](gco_ref_architecture_part3.png)

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
