"""Documentation resources (docs:// scheme) for the GCO MCP server."""

import re
from pathlib import Path

from cli_runner import PROJECT_ROOT  # runtime-resolved checkout root (uvx-safe)
from server import mcp

DOCS_DIR = PROJECT_ROOT / "docs"
EXAMPLES_DIR = PROJECT_ROOT / "examples"
ADR_DIR = DOCS_DIR / "adr"

# ---------------------------------------------------------------------------
# Example metadata — used by both the index and the per-example resource to
# give the LLM rich context about what each manifest does and how to adapt it.
# ---------------------------------------------------------------------------

EXAMPLE_METADATA: dict[str, dict[str, str | list[str]]] = {
    "simple-job": {
        "category": "Jobs & Training",
        "summary": "Basic Kubernetes Job that runs a command and completes. Start here to verify your cluster.",
        "gpu": "no",
        "opt_in": "",
        "submission": "gco jobs submit-sqs examples/simple-job.yaml --region us-east-1",
        "keywords": ["simple", "hello", "starter", "basic", "smoke test"],
        "instance_types": [],
        "use_cases": [
            "verify cluster setup",
            "smoke test a new region",
            "minimal job example",
        ],
        "related": ["gpu-job", "sqs-job-submission"],
    },
    "gpu-job": {
        "category": "Jobs & Training",
        "summary": "Requests GPU resources and runs on GPU-enabled nodes.",
        "gpu": "NVIDIA",
        "opt_in": "",
        "submission": "gco jobs submit-sqs examples/gpu-job.yaml --region us-east-1",
        "keywords": [
            "gpu",
            "nvidia",
            "cuda",
            "single gpu",
            "nvidia.com/gpu",
            "g5",
            "g6",
            "g4dn",
            "tolerations",
        ],
        "instance_types": ["g5.xlarge", "g6.xlarge", "g4dn.xlarge"],
        "use_cases": [
            "run a single GPU workload",
            "test GPU node provisioning",
            "smoke test CUDA",
        ],
        "related": ["multi-gpu-training", "simple-job"],
    },
    "multi-gpu-training": {
        "category": "Jobs & Training",
        "summary": "PyTorch DistributedDataParallel (DDP) across multiple GPUs with indexed pods and headless service.",
        "gpu": "NVIDIA",
        "opt_in": "",
        "submission": "kubectl apply -f examples/multi-gpu-training.yaml",
        "keywords": [
            "ddp",
            "distributed",
            "pytorch",
            "multi gpu",
            "training",
            "torchrun",
            "nccl",
            "indexed pods",
            "headless service",
        ],
        "instance_types": ["g5.12xlarge", "g6.12xlarge", "p4d.24xlarge"],
        "use_cases": [
            "distributed PyTorch DDP training",
            "scale a training job across multiple GPUs",
        ],
        "related": ["gpu-job", "efa-distributed-training", "megatrain-sft-job"],
    },
    "efa-distributed-training": {
        "category": "Jobs & Training",
        "summary": "Elastic Fabric Adapter (EFA) for high-bandwidth inter-node communication (up to 3.2 Tbps on P5, 28.8 Tbps on P6e). For p4d/p5/p5e/p5en/p6-b200/p6-b300/p6e-gb200/trn instances.",
        "gpu": "NVIDIA + EFA",
        "opt_in": "",
        "submission": "gco jobs submit-direct examples/efa-distributed-training.yaml -r us-east-1",
        "keywords": ["efa", "elastic fabric adapter", "distributed", "nccl", "high bandwidth"],
        "instance_types": [
            "p4d.24xlarge",
            "p5.48xlarge",
            "trn1.32xlarge",
            "trn2.48xlarge",
        ],
        "use_cases": [
            "multi-node distributed training over EFA",
            "high-bandwidth NCCL all-reduce",
            "large-scale model pretraining",
        ],
        "related": ["multi-gpu-training", "trainium-job", "megatrain-sft-job"],
    },
    "megatrain-sft-job": {
        "category": "Jobs & Training",
        "summary": "SFT fine-tuning of Qwen2.5-1.5B on a single GPU using MegaTrain. Downloads weights to EFS.",
        "gpu": "NVIDIA",
        "opt_in": "",
        "submission": "gco jobs submit-direct examples/megatrain-sft-job.yaml -r us-east-1",
        "keywords": ["sft", "fine-tuning", "qwen", "megatrain", "llm training"],
        "instance_types": ["g5.xlarge", "g5.12xlarge", "g6.xlarge"],
        "use_cases": [
            "supervised fine-tuning of an LLM",
            "single-GPU SFT on Qwen",
            "fine-tune a small open-source model",
        ],
        "related": ["multi-gpu-training", "model-download-job", "efa-distributed-training"],
    },
    "model-download-job": {
        "category": "Jobs & Training",
        "summary": "Pre-downloads HuggingFace model weights to shared EFS for inference endpoints.",
        "gpu": "no",
        "opt_in": "",
        "submission": "kubectl apply -f examples/model-download-job.yaml",
        "keywords": ["huggingface", "download", "weights", "model cache", "efs"],
        "instance_types": [],
        "use_cases": [
            "stage HuggingFace weights on EFS",
            "warm a model cache before serving",
        ],
        "related": ["megatrain-sft-job", "inference-vllm", "efs-output-job"],
    },
    "sqs-job-submission": {
        "category": "Jobs & Training",
        "summary": "Demonstrates SQS-based submission (recommended). Contains CPU and GPU job examples.",
        "gpu": "optional",
        "opt_in": "",
        "submission": "gco jobs submit-sqs examples/sqs-job-submission.yaml --region us-east-1",
        "keywords": ["sqs", "submission", "queue", "broker"],
        "instance_types": [],
        "use_cases": [
            "submit jobs through the SQS queue",
            "queue-based job submission pattern",
        ],
        "related": ["simple-job", "gpu-job", "keda-scaled-job"],
    },
    "trainium-job": {
        "category": "Accelerator Jobs",
        "summary": "AWS Trainium instance with Neuron SDK. Lower cost than GPU for training.",
        "gpu": "Trainium",
        "opt_in": "",
        "submission": "gco jobs submit examples/trainium-job.yaml --region us-east-1",
        "keywords": ["trainium", "neuron", "trn1", "trn2", "training accelerator"],
        "instance_types": ["trn1.2xlarge", "trn1.32xlarge", "trn2.48xlarge"],
        "use_cases": [
            "lower-cost training on AWS silicon",
            "train with the Neuron SDK",
        ],
        "related": ["inferentia-job", "efa-distributed-training"],
    },
    "inferentia-job": {
        "category": "Accelerator Jobs",
        "summary": "AWS Inferentia2 with Neuron SDK. Optimized for low-cost, high-throughput inference.",
        "gpu": "Inferentia",
        "opt_in": "",
        "submission": "gco jobs submit examples/inferentia-job.yaml --region us-east-1",
        "keywords": ["inferentia", "neuron", "inf2", "inference accelerator"],
        "instance_types": ["inf2.xlarge", "inf2.8xlarge", "inf2.24xlarge", "inf2.48xlarge"],
        "use_cases": [
            "low-cost inference on AWS silicon",
            "high-throughput batch inference",
        ],
        "related": ["trainium-job", "inference-vllm"],
    },
    "inference-vllm": {
        "category": "Inference Serving",
        "summary": "vLLM OpenAI-compatible LLM serving with PagedAttention.",
        "gpu": "NVIDIA",
        "opt_in": "",
        "submission": "gco inference deploy my-llm -i vllm/vllm-openai:v0.26.0 --gpu-count 1",
        "keywords": [
            "vllm",
            "openai",
            "openai-compatible",
            "llm serving",
            "pagedattention",
            "inference",
            "completions",
            "chat completions",
            "v1/chat/completions",
            "model server",
            "llama",
            "qwen",
            "mistral",
        ],
        "instance_types": ["g5.xlarge", "g5.12xlarge", "g6.xlarge"],
        "use_cases": [
            "serve an LLM with an OpenAI-compatible API",
            "high-throughput LLM inference",
            "deploy a chat completions endpoint",
        ],
        "related": ["inference-tgi", "inference-sglang", "inference-triton", "model-download-job"],
    },
    "inference-tgi": {
        "category": "Inference Serving",
        "summary": "HuggingFace Text Generation Inference — optimized transformer serving.",
        "gpu": "NVIDIA",
        "opt_in": "",
        "submission": "gco jobs submit-direct examples/inference-tgi.yaml -r us-east-1",
        "keywords": ["tgi", "huggingface", "text generation", "llm serving"],
        "instance_types": ["g5.xlarge", "g5.12xlarge", "g6.xlarge"],
        "use_cases": [
            "serve HuggingFace LLMs with TGI",
            "transformer text-generation endpoint",
        ],
        "related": ["inference-vllm", "inference-sglang", "inference-torchserve"],
    },
    "inference-triton": {
        "category": "Inference Serving",
        "summary": "NVIDIA Triton Inference Server — multi-framework (PyTorch, TensorFlow, ONNX).",
        "gpu": "NVIDIA",
        "opt_in": "",
        "submission": "gco jobs submit-direct examples/inference-triton.yaml -r us-east-1",
        "keywords": ["triton", "nvidia", "multi-framework", "onnx", "tensorflow", "inference"],
        "instance_types": ["g5.xlarge", "g6.xlarge"],
        "use_cases": [
            "multi-framework inference serving",
            "serve ONNX or TensorFlow models",
        ],
        "related": ["inference-vllm", "inference-torchserve"],
    },
    "inference-torchserve": {
        "category": "Inference Serving",
        "summary": "PyTorch TorchServe model serving.",
        "gpu": "NVIDIA",
        "opt_in": "",
        "submission": "gco jobs submit-direct examples/inference-torchserve.yaml -r us-east-1",
        "keywords": ["torchserve", "pytorch", "model serving"],
        "instance_types": ["g5.xlarge", "g6.xlarge"],
        "use_cases": [
            "serve a PyTorch model with TorchServe",
        ],
        "related": ["inference-triton", "inference-vllm"],
    },
    "inference-sglang": {
        "category": "Inference Serving",
        "summary": "SGLang high-throughput serving with RadixAttention for prefix caching.",
        "gpu": "NVIDIA",
        "opt_in": "",
        "submission": "gco jobs submit-direct examples/inference-sglang.yaml -r us-east-1",
        "keywords": ["sglang", "radixattention", "prefix caching", "llm serving"],
        "instance_types": ["g5.xlarge", "g5.12xlarge"],
        "use_cases": [
            "high-throughput LLM serving with prefix caching",
            "serve LLMs with structured output",
        ],
        "related": ["inference-vllm", "inference-tgi"],
    },
    "efs-output-job": {
        "category": "Storage & Persistence",
        "summary": "Writes output to shared EFS storage. Results persist after pod termination.",
        "gpu": "no",
        "opt_in": "",
        "submission": "gco jobs submit-direct examples/efs-output-job.yaml --region us-east-1 -n gco-jobs",
        "keywords": ["efs", "shared storage", "persistent", "output"],
        "instance_types": [],
        "use_cases": [
            "persist job output to EFS",
            "share data between pods via EFS",
        ],
        "related": ["fsx-lustre-job", "model-download-job", "cluster-shared-bucket-upload-job"],
    },
    "fsx-lustre-job": {
        "category": "Storage & Persistence",
        "summary": "FSx for Lustre high-performance parallel storage (1000+ GB/s throughput).",
        "gpu": "no",
        "opt_in": "FSx (gco stacks fsx enable -y)",
        "submission": "gco jobs submit-direct examples/fsx-lustre-job.yaml --region us-east-1 -n gco-jobs",
        "keywords": ["fsx", "lustre", "parallel storage", "high throughput", "hpc"],
        "instance_types": [],
        "use_cases": [
            "high-throughput parallel storage for training",
            "stream large datasets to GPU nodes",
        ],
        "related": ["efs-output-job", "multi-gpu-training", "efa-distributed-training"],
    },
    "valkey-cache-job": {
        "category": "Caching & Databases",
        "summary": "Valkey Serverless cache for K/V caching, prompt caching, session state, feature stores.",
        "gpu": "no",
        "opt_in": 'Valkey ("valkey": {"enabled": true} in cdk.json)',
        "submission": "gco jobs submit-direct examples/valkey-cache-job.yaml -r us-east-1",
        "keywords": ["valkey", "redis", "cache", "kv store", "session state"],
        "instance_types": [],
        "use_cases": [
            "cache prompts or session state",
            "use Valkey from a job",
            "feature store backed by Valkey",
        ],
        "related": ["aurora-pgvector-job"],
    },
    "aurora-pgvector-job": {
        "category": "Caching & Databases",
        "summary": "Aurora Serverless v2 PostgreSQL with pgvector for RAG and semantic search.",
        "gpu": "no",
        "opt_in": 'Aurora ("aurora_pgvector": {"enabled": true} in cdk.json)',
        "submission": "gco jobs submit-direct examples/aurora-pgvector-job.yaml -r us-east-1",
        "keywords": ["aurora", "pgvector", "postgres", "rag", "vector database", "embeddings"],
        "instance_types": [],
        "use_cases": [
            "RAG with pgvector",
            "semantic search backed by Postgres",
            "store embeddings in Aurora",
        ],
        "related": ["valkey-cache-job", "analytics-database-export-job"],
    },
    "cluster-shared-bucket-upload-job": {
        "category": "Storage & Persistence",
        "summary": "Uploads a file to the always-on Cluster_Shared_Bucket using the gco-cluster-shared-bucket ConfigMap via envFrom. Works with analytics disabled.",
        "gpu": "no",
        "opt_in": "",
        "submission": "gco jobs submit-direct examples/cluster-shared-bucket-upload-job.yaml -r us-east-1",
        "keywords": ["s3", "shared bucket", "upload", "configmap"],
        "instance_types": [],
        "use_cases": [
            "upload artifacts to the shared S3 bucket",
            "share files across regions via S3",
        ],
        "related": ["efs-output-job", "analytics-s3-upload-job"],
    },
    "analytics-s3-upload-job": {
        "category": "Analytics",
        "summary": "Publishes a dataset snapshot plus schema manifest to Cluster_Shared_Bucket under analytics-data/ so a SageMaker Studio notebook can read it.",
        "gpu": "no",
        "opt_in": 'Analytics ("analytics_environment": {"enabled": true} in cdk.json)',
        "submission": "gco jobs submit-direct examples/analytics-s3-upload-job.yaml -r us-east-1",
        "keywords": ["analytics", "s3", "sagemaker", "dataset", "schema"],
        "instance_types": [],
        "use_cases": [
            "publish a dataset for a SageMaker Studio notebook",
            "share an analytics snapshot via S3",
        ],
        "related": ["analytics-database-export-job", "cluster-shared-bucket-upload-job"],
    },
    "analytics-database-export-job": {
        "category": "Analytics",
        "summary": "Exports rows from the regional Aurora pgvector cluster to Cluster_Shared_Bucket as CSV for a SageMaker Studio notebook to analyse.",
        "gpu": "no",
        "opt_in": 'Aurora + Analytics ("aurora_pgvector.enabled" and "analytics_environment.enabled" in cdk.json)',
        "submission": "gco jobs submit-direct examples/analytics-database-export-job.yaml -r us-east-1",
        "keywords": ["analytics", "aurora", "csv", "export", "sagemaker"],
        "instance_types": [],
        "use_cases": [
            "export Aurora rows to S3 as CSV",
            "feed a SageMaker Studio notebook from Postgres",
        ],
        "related": ["aurora-pgvector-job", "analytics-s3-upload-job"],
    },
    "volcano-gang-job": {
        "category": "Schedulers",
        "summary": "Volcano gang scheduling — all pods scheduled together or none. Master + workers topology.",
        "gpu": "no",
        "opt_in": "",
        "submission": "kubectl apply -f examples/volcano-gang-job.yaml",
        "keywords": ["volcano", "gang scheduling", "batch", "scheduler"],
        "instance_types": [],
        "use_cases": [
            "schedule all pods at once or none",
            "MPI-style master + workers topology",
        ],
        "related": ["kueue-job", "yunikorn-job", "slurm-cluster-job"],
    },
    "kueue-job": {
        "category": "Schedulers",
        "summary": "Kueue job queueing with ClusterQueue, LocalQueue, ResourceFlavors, and fair-sharing.",
        "gpu": "optional",
        "opt_in": "",
        "submission": "kubectl apply -f examples/kueue-job.yaml",
        "keywords": ["kueue", "queueing", "fair sharing", "scheduler", "clusterqueue"],
        "instance_types": [],
        "use_cases": [
            "queue jobs with quotas and fair sharing",
            "multi-tenant batch scheduling",
        ],
        "related": ["volcano-gang-job", "yunikorn-job"],
    },
    "yunikorn-job": {
        "category": "Schedulers",
        "summary": "Apache YuniKorn app-aware scheduling with hierarchical queues and gang scheduling.",
        "gpu": "no",
        "opt_in": 'YuniKorn ("helm": {"yunikorn": {"enabled": true}} in cdk.json)',
        "submission": "kubectl apply -f examples/yunikorn-job.yaml",
        "keywords": ["yunikorn", "scheduler", "hierarchical queues", "gang scheduling"],
        "instance_types": [],
        "use_cases": [
            "app-aware scheduling with hierarchical queues",
            "YuniKorn-style gang scheduling",
        ],
        "related": ["kueue-job", "volcano-gang-job"],
    },
    "keda-scaled-job": {
        "category": "Schedulers",
        "summary": "KEDA ScaledJob — custom SQS-triggered autoscaling. Template for custom consumers.",
        "gpu": "no",
        "opt_in": "",
        "submission": "kubectl apply -f examples/keda-scaled-job.yaml",
        "keywords": ["keda", "scaledjob", "autoscaling", "sqs", "event driven"],
        "instance_types": [],
        "use_cases": [
            "scale jobs from SQS queue depth",
            "event-driven job autoscaling",
        ],
        "related": ["sqs-job-submission"],
    },
    "slurm-cluster-job": {
        "category": "Schedulers",
        "summary": "Slinky Slurm Operator — sbatch submission on Kubernetes for HPC workloads.",
        "gpu": "no",
        "opt_in": 'Slurm ("helm": {"slurm": {"enabled": true}} in cdk.json)',
        "submission": "kubectl apply -f examples/slurm-cluster-job.yaml",
        "keywords": ["slurm", "hpc", "sbatch", "slinky"],
        "instance_types": [],
        "use_cases": [
            "submit sbatch jobs on Kubernetes",
            "run HPC workloads with Slurm",
        ],
        "related": ["volcano-gang-job"],
    },
    "ray-cluster": {
        "category": "Distributed Computing",
        "summary": "KubeRay RayCluster for distributed training, tuning, and serving. Auto-scaling workers.",
        "gpu": "no",
        "opt_in": "",
        "submission": "kubectl apply -f examples/ray-cluster.yaml",
        "keywords": ["ray", "kuberay", "distributed", "tune", "serve"],
        "instance_types": [],
        "use_cases": [
            "stand up a Ray cluster on EKS",
            "distributed training and tuning with Ray",
        ],
        "related": ["multi-gpu-training", "pipeline-dag"],
    },
    "pipeline-dag": {
        "category": "DAG Pipelines",
        "summary": "Multi-step pipeline with dependency ordering. Preprocess → Train via shared EFS.",
        "gpu": "no",
        "opt_in": "",
        "submission": "gco dag run examples/pipeline-dag.yaml -r us-east-1",
        "keywords": ["dag", "pipeline", "workflow", "dependencies"],
        "instance_types": [],
        "use_cases": [
            "run a multi-step ML pipeline",
            "chain preprocess and train jobs",
        ],
        "related": ["dag-step-preprocess", "dag-step-train", "ray-cluster"],
    },
    "dag-step-preprocess": {
        "category": "DAG Pipelines",
        "summary": "DAG step 1: generates training data on shared EFS.",
        "gpu": "no",
        "opt_in": "",
        "submission": "(used by pipeline-dag.yaml)",
        "keywords": ["dag", "preprocess", "step", "pipeline"],
        "instance_types": [],
        "use_cases": [
            "preprocessing step of a pipeline",
            "generate training data for a downstream step",
        ],
        "related": ["pipeline-dag", "dag-step-train"],
    },
    "dag-step-train": {
        "category": "DAG Pipelines",
        "summary": "DAG step 2: reads preprocess output, trains model, writes artifacts to EFS.",
        "gpu": "no",
        "opt_in": "",
        "submission": "(used by pipeline-dag.yaml)",
        "keywords": ["dag", "train", "step", "pipeline"],
        "instance_types": [],
        "use_cases": [
            "training step of a pipeline",
            "consume preprocess output and train",
        ],
        "related": ["pipeline-dag", "dag-step-preprocess"],
    },
}


# ---------------------------------------------------------------------------
# Doc metadata — used by ``find_docs`` and the docs:// discovery resources to
# describe every markdown file under ``docs/``. Indexed by basename without
# extension (e.g. ``ARCHITECTURE``). The vocabulary in ``topics`` is kept
# small and consistent so topic-based search across docs stays predictable.
# ---------------------------------------------------------------------------

DOC_METADATA: dict[str, dict[str, str | list[str]]] = {
    "ANALYTICS": {
        "summary": "Optional SageMaker Studio + EMR Serverless analytics environment, enabled via a single cdk.json toggle.",
        "topics": ["analytics", "storage", "customization", "gpu"],
        "keywords": [
            "sagemaker studio",
            "emr serverless",
            "cognito",
            "data science",
            "notebook",
            "presigned url",
            "studio domain",
            "analytics environment",
            "user pool",
        ],
        "related": ["CLUSTER_SHARED_BUCKET", "CUSTOMIZATION"],
    },
    "API": {
        "summary": (
            "Reference for every GCO HTTP surface: the control plane (manifests, "
            "jobs, queue, templates, webhooks, cost), cross-region aggregation, "
            "inference, health and observability, plus which paths each API "
            "Gateway exposes and the cluster-internal surfaces."
        ),
        "topics": ["api", "cli", "jobs", "inference", "webhooks", "templates"],
        "keywords": [
            "rest",
            "manifest processor",
            "inference proxy",
            "health monitor",
            "endpoints",
            "auth",
            "hmac request envelope",
            "api gateway",
            "sigv4",
            "openapi",
            "submit job",
            "api surface",
        ],
        "related": ["CLI", "ARCHITECTURE"],
    },
    "ARCHITECTURE": {
        "summary": "Deep dive into the multi-region infrastructure, security layers, data flow, and scale characteristics.",
        "topics": [
            "architecture",
            "concepts",
            "security",
            "multi-region",
            "eks",
            "capacity",
            "inference",
            "gpu",
            "monitoring",
            "deployment",
            "nodepools",
            "storage",
            "images",
            "cost",
            "networking",
        ],
        "keywords": [
            "multi-region",
            "eks",
            "vpc",
            "global accelerator",
            "data flow",
            "control plane",
            "data plane",
            "regional stack",
            "global stack",
            "iam",
            "kms",
            "high level design",
            "blast radius",
        ],
        "related": ["CONCEPTS", "CUSTOMIZATION", "API"],
    },
    "AUTOPILOT": {
        "summary": (
            "gco autopilot: one command to a fully configured Claude Code "
            "session on Amazon Bedrock with the GCO MCP server and the "
            "recommended companion MCP servers wired in."
        ),
        "topics": [
            "cli",
            "mcp",
            "agents",
            "bedrock",
            "getting-started",
            "deployment",
        ],
        "keywords": [
            "autopilot",
            "claude code",
            "bedrock",
            "mcp",
            "agent session",
            "companion servers",
            "feature flags",
            "session resume",
            "skills",
            "plugins",
            "dev container",
            "front door",
        ],
        "related": ["CLI", "CONCEPTS"],
    },
    "CLI": {
        "summary": "Complete command-line interface reference for the gco CLI across jobs, queues, stacks, capacity, inference, and more.",
        "topics": [
            "cli",
            "api",
            "jobs",
            "capacity",
            "inference",
            "cost",
            "gpu",
            "multi-region",
            "images",
            "nodepools",
            "deployment",
        ],
        "keywords": [
            "gco",
            "command-line",
            "subcommand",
            "submit job",
            "stacks deploy",
            "stacks destroy",
            "capacity status",
            "ai_recommend",
            "reserve_capacity",
            "images build",
            "models upload",
        ],
        "related": ["API", "RUNBOOKS"],
    },
    "CLUSTER_SHARED_BUCKET": {
        "summary": "Reference for the always-on Cluster_Shared_Bucket — the S3 bucket every regional cluster can read and write by default.",
        "topics": ["storage", "concepts", "multi-region", "security"],
        "keywords": [
            "s3",
            "shared bucket",
            "cross-region",
            "configmap",
            "envFrom",
            "kms",
            "iam grant",
            "bucket policy",
            "always-on",
        ],
        "related": ["ANALYTICS", "ARCHITECTURE"],
    },
    "CONCEPTS": {
        "summary": "Fundamental concepts behind GCO — what it is, the problems it solves, and how the key components fit together.",
        "topics": [
            "concepts",
            "architecture",
            "multi-region",
            "capacity",
            "gpu",
            "eks",
            "jobs",
            "inference",
        ],
        "keywords": [
            "what is gco",
            "fundamentals",
            "components",
            "global queue",
            "capacity orchestration",
            "ai/ml workloads",
            "gpu allocation",
            "regional clusters",
        ],
        "related": ["ARCHITECTURE", "README"],
    },
    "COST_MONITORING": {
        "summary": "Cost monitoring and cost-aware scheduling — per-region OpenCost with a Grafana cost dashboard, scheduled Parquet cost reports to S3, cross-region Athena analytics, the /api/v1/cost API, and spot price-gated central-queue dispatch.",
        "topics": [
            "cost",
            "monitoring",
            "observability",
            "queue",
            "customization",
            "api",
            "cli",
        ],
        "keywords": [
            "opencost",
            "cost allocation",
            "athena",
            "glue",
            "parquet",
            "cost reports",
            "cost dashboard",
            "spot price",
            "max spot price",
            "cost-aware scheduling",
            "namespace cost",
            "finops",
        ],
        "related": ["MONITORING", "CUSTOMIZATION", "API"],
    },
    "CUSTOMIZATION": {
        "summary": "How to customize GCO — deployment regions, EKS configuration, GPU nodepools, and more.",
        "topics": [
            "customization",
            "architecture",
            "gpu",
            "eks",
            "nodepools",
            "storage",
            "multi-region",
            "deployment",
        ],
        "keywords": [
            "cdk.json",
            "regions",
            "addons",
            "instance types",
            "fsx",
            "valkey",
            "aurora",
            "feature toggles",
            "queue processor",
            "helm charts",
            "image registry config",
        ],
        "related": ["ARCHITECTURE", "ANALYTICS"],
    },
    "FLOCI_TESTING": {
        "summary": (
            "Emulated-AWS test layer between in-process mocks and real-account "
            "live validation: production code issuing genuine SDK requests "
            "against a Floci emulator in CI, with zero AWS credentials."
        ),
        "topics": [
            "ci",
            "deployment",
            "security",
            "automation",
        ],
        "keywords": [
            "floci",
            "emulator",
            "localstack alternative",
            "integration tests",
            "aws endpoint url",
            "emulated aws",
            "gco release validate",
            "emulator endpoint",
            "wire protocol",
            "test layers",
            "known emulator gaps",
        ],
        "related": ["LIVE_RELEASE_VALIDATION", "CLI", "MAINTENANCE"],
    },
    "FORKING": {
        "summary": (
            "Take GCO into your own repository: repoint badges, clone URLs, the "
            "GitHub Pages site, and the OIDC trust-policy subject with "
            "scripts/migrate_fork.py, plus the follow-ups it cannot decide."
        ),
        "topics": ["forking", "migration", "repository", "ci", "oidc"],
        "keywords": [
            "fork",
            "migrate",
            "own repository",
            "rename repo",
            "badges",
            "oidc trust policy",
            "github pages",
            "codeowners",
            "solution id",
            "upstream remote",
        ],
        "related": ["CUSTOMIZATION", "MAINTENANCE"],
    },
    "IMAGE_MIRROR": {
        "summary": "Mirror third-party container images (chiefly Volcano's docker.io images) into the project's gco/* ECR so the cluster pulls from same-account ECR instead of a rate-limited upstream.",
        "topics": ["images", "customization", "deployment", "eks", "schedulers"],
        "keywords": [
            "ecr",
            "mirror",
            "docker hub",
            "docker.io",
            "volcano",
            "pull-through cache",
            "multi-arch",
            "image_registry",
            "rate limit",
            "skopeo",
            "buildx",
        ],
        "related": ["VOLCANO", "CUSTOMIZATION"],
    },
    "INFERENCE": {
        "summary": "Deploy and manage multi-region GPU inference endpoints, including model weight management and supported frameworks.",
        "topics": [
            "inference",
            "architecture",
            "gpu",
            "multi-region",
            "cost",
            "images",
            "monitoring",
        ],
        "keywords": [
            "vllm",
            "tgi",
            "triton",
            "torchserve",
            "sglang",
            "endpoints",
            "canary",
            "rolling update",
            "model weights",
            "global accelerator",
            "openai-compatible",
            "inference monitor",
        ],
        "related": ["ARCHITECTURE", "RUNBOOKS"],
    },
    "KEDA": {
        "summary": "KEDA event-driven autoscaling integration — scales workloads from external sources like SQS, Kafka, and Prometheus.",
        "topics": ["schedulers", "jobs", "autoscaling"],
        "keywords": [
            "keda",
            "scaledjob",
            "scaledobject",
            "sqs trigger",
            "event-driven",
            "kafka",
            "prometheus",
            "queue depth",
        ],
        "related": ["SCHEDULERS", "VOLCANO"],
    },
    "KUBERAY": {
        "summary": "KubeRay operator integration — runs Ray distributed computing workloads on Kubernetes for training, tuning, and serving.",
        "topics": ["schedulers", "jobs", "gpu", "training", "distributed"],
        "keywords": [
            "kuberay",
            "ray",
            "raycluster",
            "rayjob",
            "rayservice",
            "ray tune",
            "ray train",
            "ray serve",
            "distributed",
        ],
        "related": ["SCHEDULERS", "VOLCANO"],
    },
    "KUEUE": {
        "summary": "Kueue integration for Kubernetes-native job queueing with resource quotas, fair sharing, and priority scheduling.",
        "topics": ["schedulers", "jobs"],
        "keywords": [
            "kueue",
            "clusterqueue",
            "localqueue",
            "resourceflavor",
            "quota",
            "fair sharing",
            "priority",
            "preemption",
        ],
        "related": ["SCHEDULERS", "VOLCANO", "YUNIKORN"],
    },
    "LEARNING_PATH": {
        "summary": "Staged, hands-on onboarding path for users new to GCO or Kubernetes — a Kubernetes primer, cost-boundary milestones, and role-based tracks that sequence the other guides.",
        "topics": [
            "concepts",
            "quickstart",
            "cli",
            "jobs",
            "inference",
            "schedulers",
        ],
        "keywords": [
            "learning path",
            "onboarding",
            "getting started",
            "tutorial",
            "curriculum",
            "new user",
            "beginner",
            "kubernetes",
            "first job",
            "role based",
        ],
        "related": ["CONCEPTS", "CLI", "SCHEDULERS"],
    },
    "LIVE_RELEASE_VALIDATION": {
        "summary": "Local operator deploy-test-destroy harness for exact-commit live validation, guaranteed cleanup, and manually attached pull request reports.",
        "topics": [
            "deployment",
            "runbooks",
            "security",
            "automation",
            "multi-region",
        ],
        "keywords": [
            "live release validation",
            "local script",
            "dedicated validation account",
            "exact commit",
            "deploy test destroy",
            "checkpoint",
            "resume",
            "kms pending deletion",
            "manual pull request upload",
            "pull request comment",
        ],
        "related": ["RUNBOOKS", "MAINTENANCE", "CLI", "EXAMPLE_VALIDATION"],
    },
    "EXAMPLE_VALIDATION": {
        "summary": "Deploy-run-destroy validation of every shipped example manifest through its documented submission path, plus the offline static checks CI enforces on any examples/ change.",
        "topics": [
            "deployment",
            "runbooks",
            "jobs",
            "examples",
            "automation",
        ],
        "keywords": [
            "example validation",
            "examples",
            "gco examples validate",
            "static checks",
            "documented submission path",
            "kubectl tunnel",
            "per-example report",
            "disclosed mutations",
            "capacity skip",
            "feature enabled overrides",
        ],
        "related": ["LIVE_RELEASE_VALIDATION", "CLI", "KUBERAY", "KUEUE", "SLURM_OPERATOR"],
    },
    "MAINTENANCE": {
        "summary": "Routine upkeep — adding instance types to the nodepool lists, EKS Kubernetes version upgrades, base-image security-epoch refreshes, CVE-suppression renewals, and acting on the monthly dependency scan.",
        "topics": [
            "customization",
            "deployment",
            "eks",
            "nodepools",
            "images",
        ],
        "keywords": [
            "maintenance",
            "upkeep",
            "upgrade",
            "eks version",
            "kubernetes version",
            "instance types",
            "instance family",
            "dependency scan",
            "deps-scan",
            "security epoch",
            "cve suppression",
            "kubectl",
            "helm",
            "addon versions",
            "requirements-lock",
        ],
        "related": ["CUSTOMIZATION", "RUNBOOKS", "ARCHITECTURE"],
    },
    "MISSION": {
        "summary": "Goal-directed iteration loop — declare a directive, criteria, tool allowlist, and budget; runs deterministic five-phase iterations until a verdict.",
        "topics": [
            "concepts",
            "cli",
            "api",
            "automation",
            "feature-flags",
        ],
        "keywords": [
            "mission",
            "directive",
            "criteria",
            "verdict",
            "iteration",
            "goal directed",
            "autonomous loop",
            "sampling",
            "sandbox",
            "predicate",
            "checkpoint",
            "budget",
            "final report",
        ],
        "related": ["CLI", "ARCHITECTURE", "RUNBOOKS"],
    },
    "MONITORING": {
        "summary": "Self-hosted per-cluster observability (kube-prometheus-stack: Prometheus + Alertmanager + Grafana), on by default, with private port-forward access and the gco monitoring CLI.",
        "topics": [
            "monitoring",
            "observability",
            "gpu",
            "schedulers",
            "cli",
            "concepts",
            "cost",
        ],
        "keywords": [
            "prometheus",
            "grafana",
            "alertmanager",
            "kube-prometheus-stack",
            "dcgm",
            "servicemonitor",
            "podmonitor",
            "dashboards",
            "port-forward",
            "ssm tunnel",
            "gco monitoring",
            "credential rotation",
            "cluster observability",
        ],
        "related": ["ARCHITECTURE", "RUNBOOKS", "CLI", "INFERENCE"],
    },
    "README": {
        "summary": "Documentation index — the top-level guide map for the rest of the docs/ tree.",
        "topics": ["concepts", "multi-region", "gpu", "capacity", "inference", "quickstart"],
        "keywords": [
            "index",
            "overview",
            "guide map",
            "documentation",
            "table of contents",
            "getting started",
        ],
        "related": ["CONCEPTS", "ARCHITECTURE"],
    },
    "RUNBOOKS": {
        "summary": "Operational runbooks — step-by-step procedures for common operational scenarios with symptoms, diagnosis, and resolution.",
        "topics": [
            "runbooks",
            "troubleshooting",
            "jobs",
            "inference",
            "capacity",
            "monitoring",
            "deployment",
        ],
        "keywords": [
            "incident response",
            "operational procedures",
            "stuck job",
            "endpoint down",
            "capacity exhausted",
            "stack rollback",
            "playbook",
            "diagnose",
            "remediation",
        ],
        "related": ["TROUBLESHOOTING", "CLI"],
    },
    "SCHEDULERS": {
        "summary": "Comparison and overview of the six supported scheduling and orchestration tools — Volcano, Kueue, KubeRay, KEDA, Slurm, YuniKorn.",
        "topics": ["schedulers", "concepts", "jobs", "gpu"],
        "keywords": [
            "volcano",
            "kueue",
            "kuberay",
            "keda",
            "slurm",
            "yunikorn",
            "gang scheduling",
            "batch scheduler",
            "scheduler comparison",
            "queueing",
        ],
        "related": ["VOLCANO", "KUEUE", "KUBERAY"],
    },
    "SLURM_OPERATOR": {
        "summary": "Slinky Slurm Operator integration — runs sbatch, srun, and salloc inside an EKS cluster for HPC workflows.",
        "topics": ["schedulers", "jobs", "hpc"],
        "keywords": [
            "slurm",
            "slinky",
            "sbatch",
            "srun",
            "salloc",
            "hpc",
            "scientific computing",
            "mpi",
        ],
        "related": ["SCHEDULERS", "VOLCANO"],
    },
    "TROUBLESHOOTING": {
        "summary": "Troubleshooting guide — common installation, deployment, kubectl, and pod issues with their resolutions.",
        "topics": [
            "troubleshooting",
            "runbooks",
            "deployment",
            "eks",
            "jobs",
            "inference",
            "capacity",
        ],
        "keywords": [
            "kubectl",
            "pod crashloop",
            "imagepullbackoff",
            "stack rollback",
            "deployment failed",
            "credentials",
            "vpc",
            "nodepool not scaling",
            "common errors",
            "fix",
        ],
        "related": ["RUNBOOKS", "CLI"],
    },
    "VOLCANO": {
        "summary": "Volcano batch scheduler integration — gang scheduling, fair-share queuing, and job lifecycle management for AI/ML and HPC.",
        "topics": ["schedulers", "jobs", "gpu", "hpc"],
        "keywords": [
            "volcano",
            "gang scheduling",
            "vcjob",
            "podgroup",
            "queue",
            "fair share",
            "job lifecycle",
            "batch",
        ],
        "related": ["SCHEDULERS", "KUEUE", "YUNIKORN"],
    },
    "YUNIKORN": {
        "summary": "Apache YuniKorn integration — multi-tenant scheduler with hierarchical queues and gang scheduling.",
        "topics": ["schedulers", "jobs"],
        "keywords": [
            "yunikorn",
            "hierarchical queues",
            "multi-tenant",
            "gang scheduling",
            "app-aware scheduling",
            "fair share",
        ],
        "related": ["SCHEDULERS", "KUEUE", "VOLCANO"],
    },
}


# ---------------------------------------------------------------------------
# Root-doc metadata — searchable project-level guidance that deliberately lives
# at the repository root rather than under ``docs/``. Keep this separate from
# ``DOC_METADATA`` so the strict 1:1 metadata-to-``docs/*.md`` invariant remains
# meaningful. Root docs use static ``docs://gco/{name}`` resources.
# ---------------------------------------------------------------------------

ROOT_DOC_METADATA: dict[str, dict[str, str | list[str]]] = {
    "TENETS": {
        "path": "TENETS.md",
        "summary": (
            "Prioritized project tenets and north-star guidance for safety, truth, "
            "security, global capacity orchestration, automation, operations, cost, "
            "and maintainability."
        ),
        "topics": [
            "concepts",
            "architecture",
            "security",
            "automation",
            "multi-region",
            "deployment",
            "cost",
        ],
        "keywords": [
            "tenets",
            "north star",
            "principles",
            "decision framework",
            "project ethos",
            "safety",
            "truth",
            "reversibility",
            "least privilege",
            "capacity policy",
            "deterministic automation",
            "definition of done",
        ],
        "related": ["ARCHITECTURE", "MAINTENANCE", "LIVE_RELEASE_VALIDATION"],
    }
}


# ---------------------------------------------------------------------------
# Package-doc metadata — used by ``find_docs`` and the
# ``docs://gco/packages/...`` resources to describe the package-level READMEs
# that live next to the code (under ``gco_mcp/``) rather than in ``docs/``. These
# are developer-facing internals guides (how a package is structured and how to
# customize it), kept in a catalog separate from ``DOC_METADATA`` so the strict
# 1:1 ``docs/*.md`` invariant stays intact. Each entry is keyed by a stable
# slug and carries a ``path`` relative to the project root. A ``related`` entry
# may reference either another package slug or a ``DOC_METADATA`` key.
# ---------------------------------------------------------------------------

PACKAGE_DOC_METADATA: dict[str, dict[str, str | list[str]]] = {
    "mcp-server": {
        "path": "gco_mcp/README.md",
        "summary": "GCO MCP server guide — setup across MCP clients, feature-flag gating, and the full tool and resource catalog.",
        "topics": ["mcp", "concepts", "feature-flags", "customization"],
        "keywords": [
            "mcp server",
            "fastmcp",
            "stdio",
            "feature flags",
            "gco_enable",
            "kiro",
            "claude desktop",
            "cursor",
            "tool search",
            "available tools",
            "resources",
        ],
        "related": ["mcp-tools", "mcp-resources", "CLI", "MISSION"],
    },
    "mcp-tools": {
        "path": "gco_mcp/tools/README.md",
        "summary": "How MCP tools are defined — one module per domain, the @mcp.tool + audit_logged pattern, and how to add a new tool.",
        "topics": ["mcp", "customization"],
        "keywords": [
            "tool",
            "mcp.tool",
            "audit_logged",
            "cli_runner",
            "adding a tool",
            "tool module",
            "domain",
        ],
        "related": ["mcp-server", "mcp-resources"],
    },
    "mcp-resources": {
        "path": "gco_mcp/resources/README.md",
        "summary": "MCP resource modules by URI scheme (docs://, source://, k8s://, …) and how to add a new resource group.",
        "topics": ["mcp", "customization"],
        "keywords": [
            "resource",
            "mcp.resource",
            "uri scheme",
            "docs scheme",
            "source scheme",
            "resource index",
            "adding a resource",
        ],
        "related": ["mcp-server", "mcp-tools"],
    },
    "mcp-mission": {
        "path": "gco_mcp/mission/README.md",
        "summary": "Mission package internals — the five-phase goal-directed loop, deterministic verdict cascade, sandboxes, and how to extend each piece.",
        "topics": ["mcp", "automation", "customization", "concepts"],
        "keywords": [
            "mission",
            "engine",
            "verdict",
            "five-phase loop",
            "sandbox",
            "predicate",
            "sampling",
            "criteria",
            "customize mission",
            "module map",
        ],
        "related": ["MISSION", "mcp-metric-readers", "mcp-mission-judge", "mcp-server"],
    },
    "mcp-metric-readers": {
        "path": "gco_mcp/metric_readers/README.md",
        "summary": "Pure helpers behind the read-only metric-reader tools — and how to add an aggregation mode, file format, error code, or whole new reader source.",
        "topics": ["mcp", "metrics", "customization", "automation"],
        "keywords": [
            "metric reader",
            "cloudwatch",
            "aggregation mode",
            "file format",
            "parquet",
            "jsonl",
            "error code",
            "metrics_result",
            "customize metrics",
            "local root",
        ],
        "related": ["mcp-mission-judge", "mcp-mission", "MISSION", "mcp-server"],
    },
    "mcp-mission-judge": {
        "path": "gco_mcp/mission_judge/README.md",
        "summary": "LLM-as-judge progress scoring internals — the versioned rubric, deterministic prompt, score parsing, and how to customize the scoring for your use case.",
        "topics": ["mcp", "metrics", "customization", "automation"],
        "keywords": [
            "semantic progress",
            "judge",
            "rubric",
            "prompt",
            "score parsing",
            "progress_score",
            "gco_enable_semantic_progress",
            "llm as judge",
            "customize rubric",
        ],
        "related": ["mcp-metric-readers", "mcp-mission", "MISSION", "mcp-server"],
    },
}


@mcp.resource("docs://gco/index")
def docs_index() -> str:
    """List all available GCO documentation, examples, and configuration resources."""
    sections = ["# GCO Resource Index\n"]
    sections.append("## Project Overview")
    sections.append("- `docs://gco/README` — Project README and overview")
    sections.append("- `docs://gco/QUICKSTART` — Quick start guide (deploy in under 60 minutes)")
    sections.append("- `docs://gco/TENETS` — Prioritized project tenets and north-star guidance")
    sections.append("- `docs://gco/CONTRIBUTING` — Contributing guide\n")

    sections.append("## Documentation")
    sections.append(
        "- `find_docs(query=..., topic=..., limit=...)` tool — search the docs catalog by topic and free-text query"
    )
    sections.append(
        "- `docs://gco/docs/by-topic/{topic}` — list every doc tagged with a given topic phrase"
    )
    sections.append(
        "- `docs://gco/docs/by-related/{doc_name}` — list every doc related to the given doc"
    )
    for f in sorted(DOCS_DIR.glob("*.md")):
        sections.append(f"- `docs://gco/docs/{f.stem}` — {f.stem}")

    sections.append("\n## Architecture Decision Records")
    sections.append(
        "Append-only log of significant architectural decisions — the context, "
        "the decision, and its consequences."
    )
    sections.append("- `docs://gco/adr/index` — every ADR with its status (directory-driven)")
    sections.append("- `docs://gco/adr/README` — when to write an ADR and the authoring process")
    sections.append("- `docs://gco/adr/template` — the blank ADR template")
    sections.append("- `docs://gco/adr/{id}` — read one ADR by four-digit id (e.g. `0001`)")

    sections.append("\n## Package Internals")
    sections.append(
        "Developer-facing guides to the code packages under `gco_mcp/` — structure and "
        "how to customize each. Also searchable via `find_docs`."
    )
    for name, meta in PACKAGE_DOC_METADATA.items():
        sections.append(f"- `docs://gco/packages/{name}` — {meta.get('summary', '')}")

    sections.append("\n## Example Manifests")
    sections.append("- `docs://gco/examples/README` — Examples overview and usage guide")
    sections.append(
        "- `docs://gco/examples/guide` — How to create new job manifests (patterns & metadata)"
    )

    sections.append("\n### Discovery")
    sections.append(
        "- `find_examples(query=..., category=..., gpu=..., opt_in=..., limit=...)` tool — "
        "search the catalog by keyword and filters"
    )
    sections.append(
        "- `docs://gco/examples/by-category/{category}` — list every example in a given category"
    )
    sections.append(
        "- `docs://gco/examples/by-use-case/{use_case}` — list every example matching a use-case phrase"
    )
    sections.append(
        "- `docs://gco/examples/{name}` — full manifest plus metadata header for a single example\n"
    )

    # Categorize examples
    categories: dict[str, list[str]] = {}
    for f in sorted(EXAMPLES_DIR.glob("*.yaml")):
        name = f.stem
        meta = EXAMPLE_METADATA.get(name, {})
        cat_value = meta.get("category", "Other")
        cat = cat_value if isinstance(cat_value, str) else "Other"
        summary_value = meta.get("summary", name)
        summary = summary_value if isinstance(summary_value, str) else name
        entry = f"- `docs://gco/examples/{name}` — {summary}"
        categories.setdefault(cat, []).append(entry)

    for cat, entries in categories.items():
        sections.append(f"### {cat}")
        sections.extend(entries)
        sections.append("")

    sections.append("## Live State")
    sections.append(
        "- `gco://jobs/{region}/{job_name}` — live YAML for a Kubernetes Job in the "
        "explicitly selected regional EKS cluster"
    )
    sections.append(
        "- `gco://inference/{endpoint_name}` — desired-state record for an inference endpoint "
        "from the DynamoDB store"
    )
    sections.append(
        "- `gco://k8s/{region}/{namespace}/{kind}/{name}` — live YAML for any Kubernetes "
        "resource from the explicitly selected regional EKS cluster"
    )
    sections.append(
        "- `gco://cluster/{region}/topology` — Karpenter NodePools plus Pending pods snapshot for one region"
    )
    sections.append(
        "- `costs://gco/summary/{days_window}` — cost summary for the given day window (positive integer)"
    )
    sections.append("- `tasks://gco/{task_id}` — current status of a FastMCP background task by ID")
    sections.append(
        "- `mission://sessions/{session_id}` — Mission session state, report, or audit replay "
        "when `GCO_ENABLE_MISSION=true`"
    )
    sections.append("")

    sections.append("## Other Resource Groups")
    sections.append("- `k8s://gco/manifests/index` — Kubernetes manifests deployed to EKS")
    sections.append("- `iam://gco/policies/index` — IAM policy templates")
    sections.append("- `infra://gco/index` — Dockerfiles, Helm charts, CI/CD config")
    sections.append("- `ci://gco/index` — GitHub Actions workflows, composite actions, templates")
    sections.append(
        "- `images://gco/index` — ECR repositories, tags, images, and replication status"
    )
    sections.append("- `source://gco/index` — Source code browser")
    sections.append("- `demos://gco/index` — Demo walkthroughs and scripts")
    sections.append("- `clients://gco/index` — API client examples (Python, curl, AWS CLI)")
    sections.append("- `scripts://gco/index` — Utility scripts")
    sections.append("- `tests://gco/index` — Test suite documentation and patterns")
    sections.append(
        "- `config://gco/index` — authoritative CDK configuration, MCP feature flags, and environment variables"
    )
    sections.append("- `mcp://gco/tools/index` — live registered-tool catalog")
    sections.append("- `mcp://gco/resources/index` — live static-resource and template catalog")
    sections.append("- `mcp://gco/feature-flags` — authoritative feature-gate mapping")
    return "\n".join(sections)


@mcp.resource("docs://gco/README")
def readme_resource() -> str:
    """The main project README with overview and quickstart information."""
    return (PROJECT_ROOT / "README.md").read_text()


@mcp.resource("docs://gco/QUICKSTART")
def quickstart_resource() -> str:
    """Quick start guide — get running in under 60 minutes."""
    path = PROJECT_ROOT / "QUICKSTART.md"
    if not path.is_file():
        return "QUICKSTART.md not found."
    return path.read_text()


@mcp.resource("docs://gco/TENETS")
def tenets_resource() -> str:
    """Prioritized project tenets and north-star decision guidance."""
    meta = ROOT_DOC_METADATA["TENETS"]
    rel_path = str(meta["path"])
    path = PROJECT_ROOT / rel_path
    if not path.is_file():
        return f"{rel_path} not found."
    content = path.read_text()
    topics = meta.get("topics", [])
    related = meta.get("related", [])
    header_lines = []
    if isinstance(topics, list) and topics:
        header_lines.append(f"<!-- Topics: {', '.join(str(t) for t in topics)} -->")
    if isinstance(related, list) and related:
        header_lines.append(f"<!-- Related: {', '.join(str(r) for r in related)} -->")
    return "\n".join(header_lines) + "\n\n" + content


@mcp.resource("docs://gco/CONTRIBUTING")
def contributing_resource() -> str:
    """Contributing guide — how to contribute to the project."""
    path = PROJECT_ROOT / "CONTRIBUTING.md"
    if not path.is_file():
        return "CONTRIBUTING.md not found."
    return path.read_text()


@mcp.resource("docs://gco/docs/{doc_name}")
def doc_resource(doc_name: str) -> str:
    """Read a documentation file by name (e.g. ARCHITECTURE, CLI, INFERENCE).

    Prepends an HTML-comment header with ``Topics:`` and ``Related:`` lines
    pulled from ``DOC_METADATA`` so an LLM consuming the rendered markdown
    sees the doc's classification without it bleeding into the rendered
    output. HTML comments are used rather than ``#`` because docs are
    markdown — Python-style comments would render as text.
    """
    path = DOCS_DIR / f"{doc_name}.md"
    if not path.is_file():
        available = [f.stem for f in DOCS_DIR.glob("*.md")]
        return f"Document '{doc_name}' not found. Available: {', '.join(available)}"
    content = path.read_text()
    meta = DOC_METADATA.get(doc_name, {})
    header_lines = []
    topics = meta.get("topics", [])
    if isinstance(topics, list) and topics:
        header_lines.append(f"<!-- Topics: {', '.join(str(t) for t in topics)} -->")
    related = meta.get("related", [])
    if isinstance(related, list) and related:
        header_lines.append(f"<!-- Related: {', '.join(str(r) for r in related)} -->")
    if header_lines:
        return "\n".join(header_lines) + "\n\n" + content
    return content


@mcp.resource("docs://gco/packages/{package_name}")
def package_doc_resource(package_name: str) -> str:
    """Read a package-level README by slug (e.g. mcp-mission, mcp-metric-readers).

    Serves the developer-facing internals guides catalogued in
    ``PACKAGE_DOC_METADATA`` — the README files that live next to the code
    under ``gco_mcp/`` rather than in ``docs/``. Prepends an HTML-comment header
    with ``Topics:`` and ``Related:`` lines (mirroring :func:`doc_resource`)
    so a consuming LLM sees the classification without it bleeding into the
    rendered markdown. An unknown slug returns the literal ``Package doc 'X'
    not found. Available: ...`` string so callers can recover.
    """
    meta = PACKAGE_DOC_METADATA.get(package_name)
    if meta is None:
        available = ", ".join(sorted(PACKAGE_DOC_METADATA.keys()))
        return f"Package doc '{package_name}' not found. Available: {available}"
    rel_path = str(meta.get("path", ""))
    path = PROJECT_ROOT / rel_path
    if not path.is_file():
        return f"Package doc '{package_name}' file not found at '{rel_path}'."
    content = path.read_text()
    header_lines = []
    topics = meta.get("topics", [])
    if isinstance(topics, list) and topics:
        header_lines.append(f"<!-- Topics: {', '.join(str(t) for t in topics)} -->")
    related = meta.get("related", [])
    if isinstance(related, list) and related:
        header_lines.append(f"<!-- Related: {', '.join(str(r) for r in related)} -->")
    if header_lines:
        return "\n".join(header_lines) + "\n\n" + content
    return content


@mcp.resource("docs://gco/examples/README")
def examples_readme_resource() -> str:
    """Examples README — overview of all example manifests with usage instructions."""
    path = EXAMPLES_DIR / "README.md"
    if not path.is_file():
        return "Examples README.md not found."
    return path.read_text()


@mcp.resource("docs://gco/examples/guide")
def examples_guide_resource() -> str:
    """How to create new job manifests — patterns, metadata, and best practices.

    Use this resource when you need to write a new Kubernetes manifest for GCO.
    It provides the metadata for every existing example so you can pick the
    closest one as a starting point and adapt it.
    """
    lines = ["# GCO Example Manifest Guide\n"]
    lines.append("Use this guide to create new Kubernetes manifests for GCO. Pick the closest")
    lines.append("existing example as a starting point, then adapt it.\n")
    lines.append("## All Examples with Metadata\n")
    lines.append("| Example | Category | Keywords | GPU | Opt-in | How to Submit |")
    lines.append("|---------|----------|----------|-----|--------|---------------|")
    for name, meta in EXAMPLE_METADATA.items():
        gpu = meta.get("gpu", "no")
        opt_in = meta.get("opt_in", "—") or "—"
        submission = meta.get("submission", "")
        keywords = meta.get("keywords", [])
        keywords_cell = ", ".join(keywords) if isinstance(keywords, list) and keywords else "—"
        lines.append(
            f"| `{name}` | {meta['category']} | {keywords_cell} | {gpu} | {opt_in} | "
            f"`{submission}` |"
        )

    lines.append("\n## Common Patterns\n")
    lines.append("### Namespace")
    lines.append(
        "All GCO jobs use `namespace: gco-jobs`. Inference uses `namespace: gco-inference`.\n"
    )
    lines.append("### Security Context (required)")
    lines.append("```yaml")
    lines.append("securityContext:")
    lines.append("  runAsNonRoot: true")
    lines.append("  runAsUser: 1000")
    lines.append("  runAsGroup: 1000")
    lines.append("containers:")
    lines.append("- securityContext:")
    lines.append("    allowPrivilegeEscalation: false")
    lines.append("    capabilities:")
    lines.append('      drop: ["ALL"]')
    lines.append("```\n")
    lines.append("### GPU Resources")
    lines.append("```yaml")
    lines.append("resources:")
    lines.append("  requests:")
    lines.append('    nvidia.com/gpu: "1"')
    lines.append("  limits:")
    lines.append('    nvidia.com/gpu: "1"')
    lines.append("tolerations:")
    lines.append("- key: nvidia.com/gpu")
    lines.append("  operator: Equal")
    lines.append('  value: "true"')
    lines.append("  effect: NoSchedule")
    lines.append("```\n")
    lines.append("### EFS Shared Storage")
    lines.append("```yaml")
    lines.append("volumeMounts:")
    lines.append("- name: shared-storage")
    lines.append("  mountPath: /mnt/gco")
    lines.append("volumes:")
    lines.append("- name: shared-storage")
    lines.append("  persistentVolumeClaim:")
    lines.append("    claimName: gco-shared-storage")
    lines.append("```\n")
    lines.append("### Prevent Node Consolidation (long-running jobs)")
    lines.append("```yaml")
    lines.append("metadata:")
    lines.append("  annotations:")
    lines.append('    karpenter.sh/do-not-disrupt: "true"')
    lines.append("```\n")
    lines.append("### Submission Methods")
    lines.append("1. **SQS (recommended):** `gco jobs submit-sqs <manifest> --region <region>`")
    lines.append("2. **API Gateway:** `gco jobs submit <manifest>`")
    lines.append("3. **Direct kubectl:** `gco jobs submit-direct <manifest> -r <region>`")
    lines.append("4. **kubectl apply:** `kubectl apply -f <manifest>`")
    return "\n".join(lines)


@mcp.resource("docs://gco/examples/{example_name}")
def example_resource(example_name: str) -> str:
    """Read an example manifest by name, with metadata context for creating similar jobs.

    Returns the raw YAML manifest preceded by a metadata header that describes
    what the example does, its requirements, and how to submit it.
    """
    path = EXAMPLES_DIR / f"{example_name}.yaml"
    if not path.is_file():
        available = [f.stem for f in EXAMPLES_DIR.glob("*.yaml")]
        return f"Example '{example_name}' not found. Available: {', '.join(available)}"

    meta = EXAMPLE_METADATA.get(example_name, {})
    header_lines = []
    if meta:
        header_lines.append(f"# Example: {example_name}")
        header_lines.append(f"# Category: {meta.get('category', 'Unknown')}")
        header_lines.append(f"# Summary: {meta.get('summary', '')}")
        if meta.get("gpu", "no") != "no":
            header_lines.append(f"# GPU/Accelerator: {meta['gpu']}")
        if meta.get("opt_in"):
            header_lines.append(f"# Opt-in required: {meta['opt_in']}")
        header_lines.append(
            f"# Submit with: {meta.get('submission', 'kubectl apply -f examples/' + example_name + '.yaml')}"
        )
        keywords = meta.get("keywords", [])
        if isinstance(keywords, list) and keywords:
            header_lines.append(f"# Keywords: {', '.join(keywords)}")
        instance_types = meta.get("instance_types", [])
        if isinstance(instance_types, list) and instance_types:
            header_lines.append(f"# Instance Types: {', '.join(instance_types)}")
        use_cases = meta.get("use_cases", [])
        if isinstance(use_cases, list) and use_cases:
            header_lines.append(f"# Use Cases: {', '.join(use_cases)}")
        related = meta.get("related", [])
        if isinstance(related, list) and related:
            header_lines.append(f"# Related: {', '.join(related)}")
        header_lines.append("#")
        header_lines.append("# --- Manifest begins below ---\n")

    manifest = path.read_text()
    if header_lines:
        return "\n".join(header_lines) + manifest
    return manifest


@mcp.resource("docs://gco/examples/by-category/{category}")
def examples_by_category_resource(category: str) -> str:
    """List examples grouped by category.

    Returns a markdown listing of every example in the given category. Match
    is case-insensitive against the entry's ``category`` field. When the
    category is not recognised, returns the literal "Category 'X' not found.
    Available: ..." string so callers can recover.
    """
    matches = [
        (name, meta)
        for name, meta in EXAMPLE_METADATA.items()
        if str(meta.get("category", "")).lower() == category.lower()
    ]
    if not matches:
        available = sorted({str(m.get("category", "")) for m in EXAMPLE_METADATA.values()})
        return f"Category '{category}' not found. Available: {', '.join(available)}"
    lines = [f"# Examples in category: {category}\n"]
    for name, meta in sorted(matches):
        lines.append(f"- `docs://gco/examples/{name}` — {meta.get('summary', '')}")
    return "\n".join(lines)


@mcp.resource("docs://gco/examples/by-use-case/{use_case}")
def examples_by_use_case_resource(use_case: str) -> str:
    """List examples whose use_cases include the given phrase (case-insensitive).

    Substring match against every entry in each example's ``use_cases`` list.
    When nothing matches, returns the literal "No examples match use case
    'X'." string with a pointer to ``find_examples`` for broader search.
    """
    needle = use_case.lower()
    matches: list[tuple[str, dict[str, str | list[str]]]] = []
    for name, meta in EXAMPLE_METADATA.items():
        ucs = meta.get("use_cases", [])
        if isinstance(ucs, list) and any(needle in str(uc).lower() for uc in ucs):
            matches.append((name, meta))
    if not matches:
        return (
            f"No examples match use case '{use_case}'. "
            "Try `find_examples(query=...)` for broader search."
        )
    lines = [f"# Examples matching use case: {use_case}\n"]
    for name, meta in sorted(matches):
        lines.append(f"- `docs://gco/examples/{name}` — {meta.get('summary', '')}")
    return "\n".join(lines)


@mcp.resource("docs://gco/docs/by-topic/{topic}")
def docs_by_topic_resource(topic: str) -> str:
    """List docs whose topics include the given phrase (case-insensitive).

    Substring match against every entry in each doc's ``topics`` list. When
    nothing matches, returns the literal ``Topic 'X' not found. Available:
    ...`` string with the union of every known topic so callers can recover.
    """
    needle = topic.lower()
    matches: list[tuple[str, dict[str, str | list[str]]]] = []
    for name, meta in DOC_METADATA.items():
        topics = meta.get("topics", [])
        if isinstance(topics, list) and any(needle in str(t).lower() for t in topics):
            matches.append((name, meta))
    if not matches:
        available = sorted(
            {
                str(t)
                for meta in DOC_METADATA.values()
                for t in (meta.get("topics", []) if isinstance(meta.get("topics"), list) else [])
            }
        )
        return f"Topic '{topic}' not found. Available: {', '.join(available)}"
    lines = [f"# Docs matching topic: {topic}\n"]
    for name, meta in sorted(matches):
        lines.append(f"- `docs://gco/docs/{name}` — {meta.get('summary', '')}")
    return "\n".join(lines)


@mcp.resource("docs://gco/docs/by-related/{doc_name}")
def docs_by_related_resource(doc_name: str) -> str:
    """List docs related to ``doc_name``.

    Combines two views of the bidirectional relation: every doc that lists
    ``doc_name`` in its own ``related`` field (referenced by) and every doc
    ``doc_name`` itself lists (references). Unknown names return the literal
    ``Doc 'X' not found. Available: ...`` string.
    """
    if doc_name not in DOC_METADATA:
        available = sorted(DOC_METADATA.keys())
        return f"Doc '{doc_name}' not found. Available: {', '.join(available)}"

    referenced_by: list[str] = []
    for name, meta in DOC_METADATA.items():
        related = meta.get("related", [])
        if isinstance(related, list) and doc_name in related:
            referenced_by.append(name)

    references: list[str] = []
    referenced_self = DOC_METADATA[doc_name].get("related", [])
    if isinstance(referenced_self, list):
        references = [str(r) for r in referenced_self]

    lines = [f"# Docs related to {doc_name}\n"]
    if references:
        lines.append("## Referenced by this doc")
        for ref in sorted(set(references)):
            meta = DOC_METADATA.get(ref, {})
            lines.append(f"- `docs://gco/docs/{ref}` — {meta.get('summary', '')}")
        lines.append("")
    if referenced_by:
        lines.append("## Docs that reference this one")
        for ref in sorted(set(referenced_by)):
            meta = DOC_METADATA.get(ref, {})
            lines.append(f"- `docs://gco/docs/{ref}` — {meta.get('summary', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Architecture Decision Records (ADRs) — docs://gco/adr/*
#
# The ADR catalog under ``docs/adr/`` is directory-driven: both the index and
# the per-record resource derive everything from the files on disk, so
# recording a new decision needs no change here. ``NNNN-title.md`` files are the
# records; ``README.md`` (process guide) and ``template.md`` (blank form) are
# guides, not records, and are excluded from the record listing.
# ---------------------------------------------------------------------------

_ADR_ID_RE = re.compile(r"^\d{4}-")
_ADR_TITLE_PREFIX_RE = re.compile(r"^\d+\.\s*")


def _adr_record_files() -> list[Path]:
    """Return the numbered ADR record files (``NNNN-*.md``), sorted by id."""
    if not ADR_DIR.is_dir():
        return []
    return sorted(p for p in ADR_DIR.glob("*.md") if _ADR_ID_RE.match(p.name))


def _parse_adr(path: Path) -> dict[str, str]:
    """Extract ``id``, ``title``, and ``status`` from an ADR markdown file.

    Title is the first level-1 heading with any ``NNNN.`` numeric prefix
    stripped; status is read from the ``- **Status:** ...`` metadata line. Both
    fall back to sensible defaults so a malformed file still lists rather than
    breaking the index.
    """
    title = ""
    status = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not title and line.startswith("# "):
            title = _ADR_TITLE_PREFIX_RE.sub("", line[2:].strip())
            continue
        if not status:
            # Normalize "- **Status:** Accepted" to "status: accepted" so the
            # label is matched regardless of list marker or emphasis.
            plain = line.lstrip("-* ").replace("*", "")
            if plain.lower().startswith("status"):
                status = plain[len("status") :].lstrip(": ").strip().rstrip(".")
    return {
        "id": path.stem,
        "title": title or path.stem,
        "status": status or "Unknown",
    }


def _resolve_adr(adr_id: str) -> Path | None:
    """Resolve an ADR request to a file under ``docs/adr/``, or ``None``.

    Accepts a full filename stem (``0001-record-architecture-decisions``,
    ``README``, ``template``) or a numeric id in any zero-padding (``1`` ->
    ``0001``). Rejects anything containing a path separator or ``..`` so a
    crafted id cannot escape the directory; because the sanitized candidate has
    no separators, the joined path is always a direct child of ``docs/adr/``.
    """
    candidate = adr_id.strip()
    if not candidate or "/" in candidate or "\\" in candidate or ".." in candidate:
        return None
    if candidate.isdigit():
        want = candidate.zfill(4)
        return next((p for p in _adr_record_files() if p.name[:4] == want), None)
    path = ADR_DIR / f"{candidate}.md"
    return path if path.is_file() else None


@mcp.resource("docs://gco/adr/index")
def adr_index_resource() -> str:
    """List every Architecture Decision Record with its id, title, and status.

    Directory-driven: scans ``docs/adr/`` for numbered ``NNNN-title.md`` records
    at read time, so a newly added ADR appears here with no code change. The
    process guide and the blank template are available at
    ``docs://gco/adr/README`` and ``docs://gco/adr/template``.
    """
    lines = ["# Architecture Decision Records\n"]
    lines.append(
        "Append-only log of significant architectural decisions (context, "
        "decision, consequences). Read the process at `docs://gco/adr/README` "
        "and start a new record from `docs://gco/adr/template`.\n"
    )
    files = _adr_record_files()
    if not files:
        lines.append("_No ADRs have been recorded yet._")
        return "\n".join(lines)
    for path in files:
        meta = _parse_adr(path)
        lines.append(f"- `docs://gco/adr/{path.name[:4]}` — {meta['title']} ({meta['status']})")
    return "\n".join(lines)


@mcp.resource("docs://gco/adr/{adr_id}")
def adr_resource(adr_id: str) -> str:
    """Read a single ADR by id, filename stem, or guide name.

    Accepts a four-digit id (``0001``), a full stem
    (``0001-record-architecture-decisions``), or the ``README`` / ``template``
    guides. An unknown id returns the literal ``ADR 'X' not found. Available:
    ...`` string listing the numeric ids so callers can recover.
    """
    path = _resolve_adr(adr_id)
    if path is None:
        available = ", ".join(p.name[:4] for p in _adr_record_files()) or "none"
        return f"ADR '{adr_id}' not found. Available: {available}"
    return path.read_text(encoding="utf-8")
