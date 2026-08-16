# How it works

GCO is one CDK application that deploys a **global control plane** plus **one
regional stack per target region**, all in a single AWS partition. This page
is the high-level story; the authoritative deep dive is
[docs/ARCHITECTURE.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/ARCHITECTURE.md),
with the conceptual model in
[docs/CONCEPTS.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/CONCEPTS.md).

## The multi-region platform

![GCO multi-region reference architecture — global control plane and workload entry](assets/images/gco_ref_architecture_part1.png)

Platform engineers configure everything in `cdk.json` and drive deployment
from the `gco` CLI. The global side provides partition-wide state (SSM
parameters, DynamoDB tables, an S3 model bucket) and — in commercial `aws`
only — a Global Accelerator that gives workload traffic a single anycast
entry point with health-based cross-region routing and automatic failover.
API Gateway enforces IAM (SigV4) authentication on every exposed method, and
Lambda proxies wrap workload requests in a short-lived HMAC envelope before
they travel to a region. Other partitions (`aws-cn`, `aws-us-gov`) skip the
accelerator and route through IAM-authenticated regional APIs instead.

## Inside a region

![GCO regional reference architecture — EKS Auto Mode data plane and regional services](assets/images/gco_ref_architecture_part2.png)

Each regional stack is a VPC with an **EKS Auto Mode cluster** (private API
endpoint by default), an internal ALB provisioned from shared Gateway API
resources, Amazon EFS for persistent job outputs, and optional FSx for
Lustre. Capacity comes from NodePools provisioned on demand: the built-in
`system` and `general-purpose` pools plus project-managed GPU x86, GPU ARM,
inference, EFA, Mooncake EFA, Neuron, and CPU pools.

Platform services run in the `gco-system` namespace — a health monitor,
manifest processor, queue processor, inference monitor, and inference proxy —
while user workloads land in `gco-jobs` and `gco-inference`. Scheduler and
operator Helm charts (KEDA, Volcano, KubeRay, cert-manager, Kueue, and
opt-ins) are installed asynchronously by a Step Functions state machine with
per-chart retry, so a slow chart never rolls back the cluster.

## How a job flows

Manifests arrive by one of four submission paths — an SQS queue (recommended
for production), the IAM-authenticated REST API, a centralized DynamoDB job
queue with priority and audit trail, or direct kubectl. On the SQS path, a
KEDA-scaled queue processor consumes messages (zero pods when the queue is
empty), and the manifest processor validates each manifest against the
security policy before applying it. EKS Auto Mode then provisions a matching
node, the job runs, and outputs can persist to EFS or FSx after the pod
terminates. The step-by-step walk-through lives in
[Core Concepts — How Components Work Together](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/CONCEPTS.md#how-components-work-together).

Capacity decisions stay explicit: `gco capacity` exposes spot placement
scores, spot price history, capacity reservations, and Capacity Blocks, and
auto-region workflows use those signals to pick a target region. Routing is
transparent; placement is deliberate.

## The security posture

![GCO security model — layered controls and the authenticated request flow](assets/images/gco_ref_architecture_part3.png)

Six complementary controls protect every backend request: IAM authentication
at API Gateway, TLS trust separation (AWS-managed TLS at the edge, a
deployment-local private root behind it), a request-bound rotating HMAC,
private backend exposure (internal ALBs, private EKS endpoints),
freshness/integrity validation in backend middleware, and IRSA / EKS Pod
Identity for pod-level AWS access without static credentials. The README's
[Security Model](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/README.md#security-model)
section shows the exact request flow per partition.

## Keep reading

- [docs/ARCHITECTURE.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/ARCHITECTURE.md)
  — layers, data flows, and scale characteristics
- [docs/CONCEPTS.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/CONCEPTS.md)
  — the mental model behind the components
- [README — Architecture Overview](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/README.md#architecture-overview)
  — the generated CDK diagram and per-stack workflows
