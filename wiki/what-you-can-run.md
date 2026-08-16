# What you can run

GCO runs accelerated and CPU workloads of most shapes: batch jobs,
gang-scheduled distributed training, Ray clusters, Slurm workloads,
multi-region inference endpoints, and multi-step DAG pipelines. Every
category below ships with a ready-to-submit manifest in
[examples/](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/examples/README.md).

## Schedulers for every workload pattern

GCO ships six scheduling and orchestration tools; KEDA, Volcano, KubeRay,
Kueue, and cert-manager are enabled by default, while Slurm (Slinky) and
YuniKorn are opt-in:

- **Volcano** — gang scheduling for distributed training
- **Kueue** — resource quotas, fair sharing, priority admission
- **KubeRay** — Ray clusters for distributed computing and hyperparameter tuning
- **KEDA** — event-driven autoscaling and scale-to-zero (SQS and 60+ sources)
- **Slurm (Slinky)** — sbatch/srun workflows and HPC migration
- **YuniKorn** — multi-tenant queues and hierarchical quotas

They operate at different layers (admission, scaling, pod scheduling, node
provisioning) and can be combined — the
[Schedulers Overview](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/SCHEDULERS.md)
has the comparison table, the decision guide, and the GPU-quota coordination
warning you should read before enabling several at once.

![GCO Schedulers and Queues dashboard — pending pods, Kueue workloads, active Jobs](assets/images/grafana-schedulers.png)

## Distributed training

Kubeflow Trainer v2 is included and enabled by default: you submit a
`TrainJob`, and the trainer compiles it into a JobSet with the correct
`torchrun` rendezvous wiring, indexed pods, and restart semantics. TrainJobs
pass the same security validation pipeline as every other submission. Plain
Kubernetes Jobs, hand-rolled indexed Jobs, and EFA-enabled multi-node
training are all supported alternatives — see
[docs/DISTRIBUTED_TRAINING.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/DISTRIBUTED_TRAINING.md).

## Inference serving

Deploy endpoints to one or more regions with a single command: vLLM, TGI,
Triton, TorchServe, and SGLang work out of the box, with model weights
synced automatically from S3 to each region. Desired state lives in
DynamoDB with continuous reconciliation, so rolling updates, scaling, and
stop/start never lose configuration. Disaggregated prefill/decode serving
(Mooncake), streaming responses, canary deployments, and spot GPUs are all
covered in
[docs/INFERENCE.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/INFERENCE.md).

## Observability, cost, and experiment tracking

Per-cluster Prometheus + Grafana ships on by default (reached privately via
`gco monitoring open`), with dashboards for services, schedulers, KEDA,
GPU/DCGM telemetry, and cost:

![GCO GPU (DCGM) dashboard — per-GPU utilization, framebuffer, temperature, power](assets/images/grafana-gpu-dcgm.png)

Cost monitoring pairs per-cluster OpenCost with scheduled Parquet reports
and cross-region Athena analytics; MLflow experiment tracking is on by
default with observability. See
[docs/MONITORING.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/MONITORING.md)
and
[docs/COST_MONITORING.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/COST_MONITORING.md).

## Interactive analytics (optional)

An opt-in analytics environment bolts a SageMaker Studio domain, EMR
Serverless, and Cognito-authenticated presigned sessions onto an existing
deployment — off by default and zero cost until enabled:

![SageMaker Studio landing screen after login](assets/images/sagemaker_studio_landing_screen.png)

Details, sub-toggles (HyperPod, Canvas, managed MLflow), and cleanup
behavior are in
[docs/ANALYTICS.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/ANALYTICS.md).

## And a goal-directed loop

Mission is GCO's opt-in iteration loop: five-phase iterations
(propose → execute → observe → evaluate → decide) against machine-checkable
success criteria until a verdict is reached — see
[docs/MISSION.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/MISSION.md).
