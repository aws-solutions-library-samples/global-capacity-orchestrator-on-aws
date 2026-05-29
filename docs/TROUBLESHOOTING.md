# Troubleshooting Guide

Common issues and their solutions.

## Table of Contents

- [Installation Issues](#installation-issues)
  - [`pip install` fails with dependency conflicts](#pip-install-fails-with-dependency-conflicts)
  - [`gco: command not found`](#gco-command-not-found)
- [Deployment Issues](#deployment-issues)
  - [Stack Creation Fails](#stack-creation-fails)
  - [Stack Stuck in DELETE_FAILED](#stack-stuck-in-delete_failed)
  - [Lambda Custom Resource Timeout](#lambda-custom-resource-timeout)
- [kubectl Access Issues](#kubectl-access-issues)
  - [Unauthorized Error](#unauthorized-error)
  - [No cluster found Error](#no-cluster-found-error)
  - [kubectl Commands Hang](#kubectl-commands-hang)
- [Pod Issues](#pod-issues)
  - [Pods Stuck in Pending](#pods-stuck-in-pending)
  - [Pods CrashLoopBackOff](#pods-crashloopbackoff)
  - [Service Account Issues](#service-account-issues)
- [Lambda Issues](#lambda-issues)
  - [Lambda Timeout](#lambda-timeout)
  - [Lambda 401 Unauthorized](#lambda-401-unauthorized)
  - [Lambda Out of Memory](#lambda-out-of-memory)
- [Networking Issues](#networking-issues)
  - [API Gateway Timeout After Deployment](#api-gateway-timeout-after-deployment)
  - [Pods Can't Reach Internet](#pods-cant-reach-internet)
  - [Can't Access Services](#cant-access-services)
  - [ALB Not Routing Traffic](#alb-not-routing-traffic)
- [Performance Issues](#performance-issues)
  - [Slow Pod Startup](#slow-pod-startup)
  - [High CPU/Memory Usage](#high-cpumemory-usage)
  - [Slow API Responses](#slow-api-responses)
- [Storage Issues](#storage-issues)
  - [PVC Not Found](#pvc-not-found)
  - [EFS Mount Failures](#efs-mount-failures)
  - [FSx for Lustre Issues](#fsx-for-lustre-issues)
- [Getting Help](#getting-help)

## Installation Issues

### `pip install` fails with dependency conflicts

**Symptom**: `pip install -e .` (or `pip install -e ".[dev]"`) fails with one of:

- `ERROR: ResolutionImpossible: ...`
- `The conflict is caused by: ... requires X, but you'll have Y which is incompatible.`
- `Cannot install -e . because these package versions have conflicting dependencies.`

**Cause**: GCO pins exact versions of many Python packages (CDK, AWS SDKs, FastAPI, mypy, Ruff, pre-commit, etc.) so CI builds are reproducible. When you install on top of an existing Python environment that already has any of those packages at different versions, pip's resolver gives up.

**Recommended fix — use the dev container.** It ships every dependency at the exact version CI uses, so the resolver never has to do any work:

```bash
docker build -f Dockerfile.dev -t gco-dev .
docker run -it --rm \
  -v ~/.aws:/root/.aws:ro \
  -v $(pwd):/workspace \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w /workspace \
  gco-dev
```

The container has the `gco` CLI, AWS CLI, kubectl, CDK, and Node.js pre-installed. See [QUICKSTART.md](../QUICKSTART.md#step-1-clone-and-build-the-dev-container) for the full setup.

**If you really need a host install:**

1. Use a brand-new virtual environment — don't reuse one that already has CDK / boto3 / FastAPI installed at different versions.

   ```bash
   python3 -m venv .venv-fresh
   source .venv-fresh/bin/activate
   pip install -e ".[dev]"
   ```

2. Or use `pipx` to give the CLI its own isolated env:

   ```bash
   brew install pipx && pipx ensurepath
   pipx install -e .
   ```

3. Confirm Python ≥ 3.14 (`python3 --version`) — the codebase uses 3.14+ syntax.

Don't loosen the pins in `pyproject.toml` or `requirements-lock.txt` to make local install work — CI's lockfile-staleness check will reject the change.

### `gco: command not found`

**Symptom**: After installing, `gco` isn't on your `PATH`.

**Cause**: Either pip installed it into a venv that isn't currently active, or pipx's bin directory isn't on your `PATH`.

**Solution**:

```bash
# pipx users — make sure the bin dir is on PATH
pipx ensurepath
exec $SHELL  # reload your shell

# venv users — activate the env first
source .venv/bin/activate
which gco
```

If you're using the dev container, the CLI is always available — just exec into the container shell from the README and call `gco` directly.

## Deployment Issues

### Stack Creation Fails

**Symptom**: `cdk deploy` fails with CloudFormation errors

**Common Causes**:

1. **CDK not bootstrapped**

   This should resolve automatically — `deploy` and `deploy-all` auto-detect un-bootstrapped regions and bootstrap them. If auto-bootstrap fails:

   ```bash
   # Manual bootstrap
   gco stacks bootstrap -r REGION
   ```

2. **Insufficient IAM permissions**

   ```bash
   # Check your permissions
   aws sts get-caller-identity
   aws iam get-user --user-name YOUR-USER
   
   # You need permissions for: EKS, EC2, IAM, CloudFormation, Lambda, ECR
   ```

3. **Resource limits exceeded**

   ```bash
   # Check service quotas
   aws service-quotas list-service-quotas \
     --service-code eks \
     --query 'Quotas[?QuotaName==`Clusters`]'
   ```

4. **Docker/Finch not running**

   ```bash
   # Start Finch
   finch vm start
   
   # Or start Docker
   docker info
   ```

**Solution Steps**:

```bash
# 1. Check CloudFormation events
aws cloudformation describe-stack-events \
  --stack-name gco-REGION \
  --region REGION \
  --max-items 20

# 2. Check specific failed resource
aws cloudformation describe-stack-resources \
  --stack-name gco-REGION \
  --region REGION \
  --query 'StackResources[?ResourceStatus==`CREATE_FAILED`]'

# 3. Delete failed stack and retry
gco stacks destroy-all -y
gco stacks deploy-all -y
```

### Stack Stuck in REVIEW_IN_PROGRESS

**Symptom**: Deploy fails with `ResourceExistenceCheck` or stack shows `REVIEW_IN_PROGRESS`

This happens when a CloudFormation changeset fails early validation — typically because resources from a previous deployment still exist (e.g., log groups with retention policies that survived a stack delete).

**Solution**: GCO auto-detects and cleans up stuck stacks on the next deploy. If you need to fix it manually:

```bash
aws cloudformation delete-stack --stack-name gco-monitoring --region REGION
aws cloudformation wait stack-delete-complete --stack-name gco-monitoring --region REGION
gco stacks deploy gco-monitoring -y
```

### Stack Stuck in DELETE_FAILED

**Symptom**: Stack won't delete, stuck in DELETE_FAILED state

**Solution**:

```bash
# Option 1: Retain problematic resource
aws cloudformation delete-stack \
  --stack-name gco-REGION \
  --region REGION \
  --retain-resources RESOURCE-LOGICAL-ID

# Option 2: Force delete via Console
# Go to CloudFormation Console → Stack → Delete → Skip failing resources
```

### Lambda Custom Resource Timeout

**Symptom**: Stack fails with "Custom resource did not receive response"

**Causes**:

- Lambda timeout (5 minutes)
- VPC networking issues
- EKS authentication failures

**Solution**:

```bash
# 1. Check Lambda logs
aws logs tail /aws/lambda/gco-REGION-KubectlApplier* \
  --region REGION \
  --since 30m

# 2. Verify Lambda can reach EKS
aws eks describe-cluster \
  --name gco-REGION \
  --region REGION \
  --query 'cluster.endpoint'

# 3. Check Lambda security group
aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=*KubectlLambda*" \
  --region REGION

# 4. Verify EKS access entry
aws eks list-access-entries \
  --cluster-name gco-REGION \
  --region REGION
```

## kubectl Access Issues

### "Unauthorized" Error

**Symptom**: `kubectl get nodes` returns "Unauthorized"

**Cause**: Your IAM principal not added to cluster access entries

**Solution**:

```bash
# 1. Get your IAM principal ARN
PRINCIPAL_ARN=$(aws sts get-caller-identity --query Arn --output text)
echo "Your ARN: $PRINCIPAL_ARN"

# 2. If using assumed role, get the role ARN
# Extract role name from assumed-role ARN
ROLE_NAME=$(echo $PRINCIPAL_ARN | sed 's/.*:assumed-role\/\([^\/]*\)\/.*/\1/')
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# 3. Add access entry
aws eks create-access-entry \
  --cluster-name gco-REGION \
  --region REGION \
  --principal-arn "$ROLE_ARN"

# 4. Associate policy
aws eks associate-access-policy \
  --cluster-name gco-REGION \
  --region REGION \
  --principal-arn "$ROLE_ARN" \
  --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy \
  --access-scope type=cluster

# 5. Verify
aws eks list-access-entries \
  --cluster-name gco-REGION \
  --region REGION
```

### "No cluster found" Error

**Symptom**: `aws eks update-kubeconfig` fails

**Cause**: Wrong region or cluster name

**Solution**:

```bash
# List all clusters
aws eks list-clusters --region us-east-1
aws eks list-clusters --region us-west-2

# Update kubeconfig with correct name
aws eks update-kubeconfig \
  --name gco-us-east-1 \
  --region us-east-1
```

### kubectl Commands Hang

**Symptom**: kubectl commands timeout or hang

**Causes**:

- Network connectivity issues
- Cluster endpoint not accessible
- kubeconfig misconfigured

**Solution**:

```bash
# 1. Test cluster endpoint
ENDPOINT=$(aws eks describe-cluster \
  --name gco-REGION \
  --region REGION \
  --query 'cluster.endpoint' \
  --output text)

curl -k $ENDPOINT/healthz

# 2. Check kubeconfig
kubectl config view
kubectl config current-context

# 3. Verify credentials
aws eks get-token --cluster-name gco-REGION --region REGION

# 4. Update kubeconfig
aws eks update-kubeconfig \
  --name gco-REGION \
  --region REGION \
  --kubeconfig ~/.kube/config
```

## Pod Issues

### Pods Stuck in Pending

**Symptom**: Pods remain in `Pending` state

**Causes**:

- Insufficient resources
- Node selector mismatch
- Taints/tolerations mismatch
- Service account missing

**Diagnosis**:

```bash
# Check pod events
kubectl describe pod POD-NAME -n NAMESPACE

# Common issues and solutions:

# 1. "no nodes available"
kubectl get nodes
# Solution: Wait for nodes to provision or adjust nodepool limits

# 2. "Insufficient cpu/memory"
kubectl describe nodes
# Solution: Increase nodepool limits or reduce pod requests

# 3. "serviceaccount not found"
kubectl get sa -n gco-system
# Solution: Apply service account manifest
kubectl apply -f lambda/kubectl-applier-simple/manifests/01-serviceaccounts.yaml

**Symptom**: Pods stuck in `ContainerCreating` for > 2 minutes

**Causes**:
- Image pull errors
- Volume mount issues
- Network plugin issues

**Diagnosis**:

```bash
# Check pod events
kubectl describe pod POD-NAME -n NAMESPACE

# Common issues:

# 1. "ImagePullBackOff" or "ErrImagePull"
# Solution: Verify image exists and ECR permissions
aws ecr describe-images \
  --repository-name REPO-NAME \
  --region REGION

# 2. "FailedMount"
# Solution: Check PVC status
kubectl get pvc -n NAMESPACE

# 3. "CNI plugin not ready"
# Solution: Check VPC CNI pods
kubectl get pods -n kube-system -l k8s-app=aws-node
```

### Pods CrashLoopBackOff

**Symptom**: Pods repeatedly crash and restart

**Diagnosis**:

```bash
# Check pod logs
kubectl logs POD-NAME -n NAMESPACE --previous

# Check pod events
kubectl describe pod POD-NAME -n NAMESPACE

# Common causes:
# 1. Application error - fix code
# 2. Missing environment variables - check deployment
# 3. Liveness probe failing - adjust probe settings
# 4. OOMKilled - increase memory limits
```

### Service Account Issues

**Symptom**: Pods can't access Kubernetes API

**Solution**:

```bash
# 1. Verify service account exists
kubectl get sa gco-service-account -n gco-system

# 2. If missing, apply manifests
kubectl apply -f lambda/kubectl-applier-simple/manifests/01-serviceaccounts.yaml
kubectl apply -f lambda/kubectl-applier-simple/manifests/02-rbac.yaml

# 3. Restart pods to pick up service account
kubectl rollout restart deployment/health-monitor -n gco-system
kubectl rollout restart deployment/manifest-processor -n gco-system
```

## Lambda Issues

### Lambda Timeout

**Symptom**: Lambda function times out after 5 minutes

**Causes**:

- Can't connect to EKS
- Slow manifest application
- Network issues

**Solution**:

```bash
# 1. Check Lambda logs
aws logs tail /aws/lambda/gco-REGION-KubectlApplier* \
  --region REGION \
  --since 30m

# 2. Verify Lambda VPC configuration
aws lambda get-function-configuration \
  --function-name FUNCTION-NAME \
  --region REGION \
  --query 'VpcConfig'

# 3. Check security group rules
# Lambda SG should allow outbound to EKS cluster SG on port 443

# 4. Increase timeout (in regional_stack.py)
timeout=Duration.minutes(10)  # Increase from 5
```

### Lambda 401 Unauthorized

**Symptom**: Lambda logs show "401 Unauthorized" from Kubernetes API

**Cause**: Lambda role not in EKS access entries

**Solution**:

```bash
# 1. Get Lambda role ARN
LAMBDA_ROLE=$(aws lambda get-function \
  --function-name FUNCTION-NAME \
  --region REGION \
  --query 'Configuration.Role' \
  --output text)

# 2. Check if access entry exists
aws eks list-access-entries \
  --cluster-name gco-REGION \
  --region REGION

# 3. If missing, it should be created by CDK
# Redeploy stack to create access entry
gco stacks deploy-all -y
```

### Lambda Out of Memory

**Symptom**: Lambda fails with "Runtime exited with error: signal: killed"

**Solution**:

Edit `gco/stacks/regional_stack.py`:

```python
kubectl_lambda = lambda_.Function(
    ...
    memory_size=1024,  # Increase from 512
    ...
)
```

Redeploy:

```bash
gco stacks deploy-all -y
```

## Networking Issues

### Pods Can't Reach Internet

**Symptom**: Pods can't download images or access external services

**Causes**:

- NAT Gateway issues
- Route table misconfiguration
- Security group blocking outbound

**Solution**:

```bash
# 1. Check NAT Gateways
aws ec2 describe-nat-gateways \
  --filter "Name=vpc-id,Values=VPC-ID" \
  --region REGION

# 2. Check route tables
aws ec2 describe-route-tables \
  --filters "Name=vpc-id,Values=VPC-ID" \
  --region REGION

# 3. Test from pod
kubectl run test-pod --image=busybox --rm -it -- wget -O- https://www.google.com

# 4. Check VPC CNI
kubectl get pods -n kube-system -l k8s-app=aws-node
kubectl logs -n kube-system -l k8s-app=aws-node
```

### Can't Access Services

**Symptom**: Can't reach services via ClusterIP or LoadBalancer

**Solution**:

```bash
# 1. Check service
kubectl get svc -n NAMESPACE
kubectl describe svc SERVICE-NAME -n NAMESPACE

# 2. Check endpoints
kubectl get endpoints SERVICE-NAME -n NAMESPACE

# 3. Test from within cluster
kubectl run test-pod --image=busybox --rm -it -- wget -O- http://SERVICE-NAME.NAMESPACE

# 4. Check network policies
kubectl get networkpolicies -n NAMESPACE
```

### API Gateway Timeout After Deployment

**Symptom**: `gco jobs submit` returns "Endpoint request timed out" immediately after deploying a new stack

**Cause**: After a fresh deployment or stack recreation, the NLB target groups need 1-2 minutes for the AWS Load Balancer Controller to register the pod IPs and for health checks to pass.

**Solution**:

Wait 1-2 minutes after deployment completes, then retry:

```bash
# Check if targets are healthy
aws elbv2 describe-target-health \
  --target-group-arn $(aws elbv2 describe-target-groups \
    --region us-east-1 \
    --query 'TargetGroups[?contains(TargetGroupName, `gco-nlb`)].TargetGroupArn' \
    --output text) \
  --region us-east-1

# Once targets show "healthy", retry your command
gco jobs submit examples/simple-job.yaml --namespace gco-jobs
```

**Alternative**: Use `submit-direct` which bypasses the API Gateway and goes directly to kubectl:

```bash
gco jobs submit-direct examples/simple-job.yaml --region us-east-1 -n gco-jobs

# Or use SQS submission (recommended for production)
gco jobs submit-sqs examples/simple-job.yaml --region us-east-1
```

### ALB Not Routing Traffic

**Symptom**: Can't reach application via ALB

**Solution**:

```bash
# 1. Check ALB status
aws elbv2 describe-load-balancers \
  --names gco-alb-REGION \
  --region REGION

# 2. Check target groups
aws elbv2 describe-target-health \
  --target-group-arn TARGET-GROUP-ARN \
  --region REGION

# 3. Check security groups
# ALB SG should allow inbound from Global Accelerator IPs
# Target SG should allow inbound from ALB SG

# 4. Check listeners
aws elbv2 describe-listeners \
  --load-balancer-arn ALB-ARN \
  --region REGION
```

## Performance Issues

### Slow Pod Startup

**Symptom**: Pods take > 5 minutes to start

**Causes**:

- Large images
- Slow image pull
- Node provisioning delay

**Solution**:

```bash
# 1. Check image size
aws ecr describe-images \
  --repository-name REPO-NAME \
  --region REGION

# 2. Use smaller base images
# In your image build recipe: FROM python:3.14-slim instead of python:3.14

# 3. Pre-pull images
kubectl create daemonset image-puller \
  --image=YOUR-IMAGE \
  --namespace=kube-system

# 4. Use image pull secrets for faster auth
```

### High CPU/Memory Usage

**Symptom**: Nodes or pods using excessive resources, or `gco jobs health` reports "unhealthy" due to threshold violations

**Solution**:

```bash
# 1. Check resource usage
kubectl top nodes
kubectl top pods -n NAMESPACE

# 2. Identify resource hogs
kubectl get pods -n NAMESPACE \
  --sort-by='.status.containerStatuses[0].restartCount' \
  --output=wide

# 3. Adjust resource limits
# Edit deployment to increase limits or reduce requests

# 4. Enable HPA for auto-scaling
kubectl autoscale deployment DEPLOYMENT-NAME \
  --cpu-percent=70 \
  --min=2 \
  --max=10 \
  -n NAMESPACE
```

If the health monitor reports unhealthy due to expected GPU saturation (e.g., inference endpoints), disable the GPU threshold in `cdk.json`:

```json
"resource_thresholds": {
  "gpu_threshold": -1,
  "pending_requested_gpus": -1
}
```

Set any threshold to `-1` to disable that check. See [Customization Guide](CUSTOMIZATION.md#resource-thresholds) for all options.

### Slow API Responses

**Symptom**: API Gateway or Kubernetes API slow

**Solution**:

```bash
# 1. Check API Gateway metrics
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApiGateway \
  --metric-name Latency \
  --dimensions Name=ApiName,Value=gco-api \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300 \
  --statistics Average \
  --region REGION

# 2. Check EKS API server metrics
kubectl get --raw /metrics | grep apiserver_request_duration

# 3. Scale up services
kubectl scale deployment/manifest-processor --replicas=5 -n gco-system

# 4. Check for resource constraints
kubectl describe nodes | grep -A 5 "Allocated resources"
```

## Storage Issues

### PVC Not Found

**Symptom**: Pod stuck in Pending with "persistentvolumeclaim not found"

**Cause**: The PVC `gco-shared-storage` doesn't exist in the namespace

**Solution**:

```bash
# 1. Check if PVC exists
kubectl get pvc -n gco-jobs
kubectl get pvc -n gco-system

# 2. Check if StorageClass exists
kubectl get storageclass

# 3. If missing, the manifests may not have been applied
# Redeploy the stack to apply EFS storage manifests
gco stacks deploy gco-REGION -y

# 4. Or manually create the PVC
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: gco-shared-storage
  namespace: gco-jobs
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: efs-sc
  resources:
    requests:
      storage: 100Gi
EOF
```

### EFS Mount Failures

**Symptom**: Pod stuck in ContainerCreating with "FailedMount" error

**Causes**:

- EFS CSI driver not installed
- Security group blocking NFS traffic
- EFS file system not accessible from VPC

**Solution**:

```bash
# 1. Check EFS CSI driver pods
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-efs-csi-driver

# 2. Check EFS file system
aws efs describe-file-systems --region REGION

# 3. Check mount targets
aws efs describe-mount-targets \
  --file-system-id fs-XXXXX \
  --region REGION

# 4. Check security groups allow NFS (port 2049)
aws ec2 describe-security-groups \
  --group-ids sg-XXXXX \
  --region REGION

# 5. Test EFS connectivity from a pod
kubectl run efs-test --image=busybox --rm -it -- \
  nc -zv fs-XXXXX.efs.REGION.amazonaws.com 2049
```

### FSx for Lustre Issues

**Symptom**: FSx PVC not binding or mount failures

**Common Issues**:

1. **FSx not enabled**

   ```bash
   # Check if FSx is enabled
   gco stacks fsx status
   
   # Enable FSx and redeploy
   gco stacks fsx enable -y
   gco stacks deploy gco-REGION -y
   ```

2. **Mount fails with "Invalid argument" (Lustre Version Mismatch)**

   This error occurs when the FSx file system uses Lustre 2.10, which is
   **NOT compatible with kernel 6.x** (used by AL2023 and Bottlerocket 1.19+).

   **Check your FSx Lustre version**:

   ```bash
   aws fsx describe-file-systems --file-system-ids fs-XXXXX --region REGION \
     --query 'FileSystems[0].FileSystemTypeVersion'
   ```

   **Solution**: If the version is "2.10", you need to create a new FSx file system
   with version 2.12 or 2.15. GCO defaults to 2.15 for new deployments.

   | Lustre Version | Kernel 5.x (AL2) | Kernel 6.x (AL2023/Bottlerocket) |
   |----------------|------------------|----------------------------------|
   | 2.10           | ✅ Yes           | ❌ No                            |
   | 2.12           | ✅ Yes           | ✅ Yes                           |
   | 2.15           | ✅ Yes           | ✅ Yes                           |

   See [AWS Lustre Client Compatibility Matrix](https://docs.aws.amazon.com/fsx/latest/LustreGuide/lustre-client-matrix.html).

3. **Security group issues**

   ```bash
   # Check FSx security group allows Lustre traffic
   aws ec2 describe-security-groups \
     --group-ids sg-XXXXX \
     --region REGION
   
   # Lustre requires ports 988 (control) and 1021-1023 (data)
   ```

**Full Diagnosis**:

```bash
# 1. Check FSx file system status
aws fsx describe-file-systems --region REGION

# 2. Check FSx CSI driver
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-fsx-csi-driver

# 3. Check PVC status
kubectl get pvc gco-fsx-storage -n gco-jobs

# 4. Check pod events for mount errors
kubectl describe pod POD-NAME -n NAMESPACE
```

## Getting Help

### Collect Diagnostic Information

```bash
# Create diagnostic bundle
mkdir -p diagnostics

# Cluster info
kubectl cluster-info dump > diagnostics/cluster-info.txt

# Node info
kubectl get nodes -o wide > diagnostics/nodes.txt
kubectl describe nodes > diagnostics/nodes-describe.txt

# Pod info
kubectl get pods --all-namespaces -o wide > diagnostics/pods.txt
kubectl get events --all-namespaces > diagnostics/events.txt

# Job info via CLI
gco -o json jobs list --all-regions > diagnostics/jobs.json

# Logs
kubectl logs -n gco-system deployment/health-monitor > diagnostics/health-monitor.log
kubectl logs -n gco-system deployment/manifest-processor > diagnostics/manifest-processor.log

# AWS resources
aws eks describe-cluster --name gco-REGION --region REGION > diagnostics/eks-cluster.json
aws cloudformation describe-stacks --stack-name gco-REGION --region REGION > diagnostics/cfn-stack.json

# Create tarball
tar -czf diagnostics-$(date +%Y%m%d-%H%M%S).tar.gz diagnostics/
```

### Contact Support

Include:

- Diagnostic bundle
- Steps to reproduce
- Expected vs actual behavior
- CloudFormation stack events
- Lambda logs
- kubectl output

---

**Still stuck?** Check the [AWS EKS documentation](https://docs.aws.amazon.com/eks/) or open a [GitHub issue](https://github.com/awslabs/global-capacity-orchestrator-on-aws/issues).
