# Images

Screenshots and visual assets for GCO (Global Capacity Orchestrator on AWS) documentation.

## Table of Contents

- [Reference Architecture Diagrams](#reference-architecture-diagrams)
- [MCP Server Screenshots](#mcp-server-screenshots)
- [SageMaker Studio Screenshots](#sagemaker-studio-screenshots)

## Reference Architecture Diagrams

Reference architecture diagrams used in the [main README](../README.md#architecture-overview). Each diagram is annotated with numbered steps; the corresponding workflow descriptions live under the Architecture Overview section of the main README.

| Image | Description |
|-------|-------------|
| [gco_ref_architecture_part1.png](gco_ref_architecture_part1.png) | Multi-region reference architecture — DevOps/platform engineers and the CDK app deploy the stacks; user CLI requests flow through IAM-authenticated API Gateway, a Lambda secret-header proxy (Secrets Manager), and Global Accelerator out to per-region ALBs fronting EKS clusters with Karpenter GPU/Trainium/Inferentia/CPU node pools |
| [gco_ref_architecture_part2.png](gco_ref_architecture_part2.png) | Regional stack detail — public ALB, EKS cluster, Karpenter node pools (system, general-purpose, gpu-x86, gpu-arm, inference, gpu-efa), platform services and workloads across `gco-system`/`gco-jobs`/`gco-inference`, storage & data (EFS, FSx for Lustre, Valkey, Aurora pgvector, S3), regional API Gateway, internal NLB, and regional AWS services (SQS, DynamoDB, CloudWatch) |
| [gco_ref_architecture_part3.png](gco_ref_architecture_part3.png) | Security architecture — five defense-in-depth layers (IAM authentication, rotating secret header, Global Accelerator IP restriction, backend header validation, IRSA) mapped onto the end-to-end request flow from SigV4-signed user request to in-cluster service |

## MCP Server Screenshots

Screenshots demonstrating the GCO MCP server integration with Kiro.

| Image | Description |
|-------|-------------|
| [gco_mcp_kiro.png](gco_mcp_kiro.png) | GCO MCP server connected in Kiro showing 43 available tools |
| [gco_mcp_list_stacks.png](gco_mcp_list_stacks.png) | Listing deployed CDK stacks via natural language |
| [gco_mcp_check_capacity.png](gco_mcp_check_capacity.png) | Checking GPU capacity for g5.xlarge in us-east-1 |
| [gco_mcp_calculating_pi.png](gco_mcp_calculating_pi.png) | Using the MCP to write a PI calculation manifest, run it on available capacity, and print the logs |
| [pi_calculation_manifest.png](pi_calculation_manifest.png) | The PI calculation Kubernetes Job manifest |
| [gco_mcp_ai_recommend.png](gco_mcp_ai_recommend.png) | Using the MCP ai_recommend tool for AI-powered capacity recommendations |
| [gco_mcp_cost_summary.png](gco_mcp_cost_summary.png) | Viewing cost summary via natural language |

## SageMaker Studio Screenshots

Screenshots of the GCO analytics environment running in SageMaker Studio.

| Image | Description |
|-------|-------------|
| [sagemaker_studio_landing_screen.png](sagemaker_studio_landing_screen.png) | SageMaker Studio landing screen after login via `gco analytics studio login` |
| [sagemaker_studio_jupyterlab_app.png](sagemaker_studio_jupyterlab_app.png) | JupyterLab app running inside a Studio space |
| [sagemaker_studio_jupyterlab_landing_page.png](sagemaker_studio_jupyterlab_landing_page.png) | JupyterLab landing page with file browser and launcher |
| [sagemaker_studio_cloning_gco_in_jupyter.png](sagemaker_studio_cloning_gco_in_jupyter.png) | Cloning the GCO repository from a JupyterLab terminal |
| [sagemaker_studio_emr_serverless.png](sagemaker_studio_emr_serverless.png) | EMR Serverless application visible from the Studio Data panel |
| [sagemaker_studio_canvas_data_wrangler.png](sagemaker_studio_canvas_data_wrangler.png) | SageMaker Canvas Data Wrangler — no-code dataset import and feature engineering (shown when the `canvas` sub-toggle is enabled) |
| [sagemaker_studio_mlflow.png](sagemaker_studio_mlflow.png) | SageMaker Studio MLflow application visible from the MLflow panel |
