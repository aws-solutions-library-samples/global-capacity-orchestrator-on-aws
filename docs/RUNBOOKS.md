# Operational Runbooks

Step-by-step procedures for common operational scenarios. Each runbook includes symptoms, diagnosis steps, and resolution actions.

> **Prerequisites:** Before running kubectl commands, set up cluster access with `gco stacks access -r <region>`. This configures your kubeconfig and sets the current context to the target cluster.

## Table of Contents

- [Region Goes Unhealthy](#region-goes-unhealthy)
- [Secret Rotation Fails](#secret-rotation-fails)
- [Backend Certificate Rotation Fails](#backend-certificate-rotation-fails)
- [Global Accelerator Stops Routing to a Region](#global-accelerator-stops-routing-to-a-region)
- [SQS Dead Letter Queue Filling Up](#sqs-dead-letter-queue-filling-up)
- [Manifest Processor Rejecting Valid Jobs](#manifest-processor-rejecting-valid-jobs)
- [High API Gateway Latency](#high-api-gateway-latency)
- [EKS Cluster Unreachable](#eks-cluster-unreachable)
- [Inference Endpoint Not Serving Traffic](#inference-endpoint-not-serving-traffic)
- [Cost Spike Detection](#cost-spike-detection)

---

## Region Goes Unhealthy

**Symptoms:** `gco capacity status` shows a region as `unhealthy`. Global Accelerator stops routing traffic to the region. Cross-region aggregator returns errors for the affected region.

**Diagnosis:** Replace `<REGION>` with the affected AWS region (for example `us-east-1`):

```bash
# 1. Check health from the CLI
gco jobs list -r <REGION>

# 2. Check the health endpoint directly
gco capacity status

# 3. Check CloudWatch alarms in the monitoring dashboard
# Look for: EKS CPU/memory alarms, ALB unhealthy hosts, Lambda errors

# 4. Check EKS cluster status
aws eks describe-cluster --name gco-<REGION> --region <REGION> \
  --query 'cluster.status'

# 5. Check node health (if cluster is reachable)
kubectl get nodes
```

**Resolution:**

1. **If EKS API is unreachable:** Check VPC networking, security groups, and EKS control plane status in the AWS console. EKS Auto Mode manages nodes automatically — if the control plane is healthy, nodes should recover.

2. **If ALB health checks are failing:** Check the health monitor and manifest processor pods:

   ```bash
   kubectl get pods -n gco-system
   kubectl logs -n gco-system deployment/health-monitor
   ```

3. **If nodes are NotReady:** EKS Auto Mode should replace unhealthy nodes automatically. Check CloudWatch for node group scaling events. If stuck, check the nodepool configuration:

   ```bash
   kubectl get nodepools
   ```

4. **If the region is permanently degraded:** Traffic is automatically routed to healthy regions via Global Accelerator. No immediate action required for availability, but investigate root cause.

**Escalation:** If the cluster is completely unreachable and not recovering after 15 minutes, check the [AWS Health Dashboard](https://health.aws.amazon.com/health/status) for regional service issues.

---

## Secret Rotation Fails

**Symptoms:** The rotation alarm fires, the rotation function reports an error, or proxy/backend authentication begins returning 503/401 responses because no valid current or pending signing key can be loaded.

**Diagnosis:**

```bash
# Set these for your deployment
PROJECT_NAME=${PROJECT_NAME:-gco}
API_REGION=${API_REGION:-us-east-2}
API_STACK="${PROJECT_NAME}-api-gateway"
SECRET_ID="${PROJECT_NAME}/api-gateway-auth-token"

# 1. Discover the generated rotation function and its log group
ROTATION_FUNCTION=$(aws cloudformation list-stack-resources \
  --stack-name "$API_STACK" --region "$API_REGION" \
  --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && contains(LogicalResourceId, 'SecretRotationFunction')].PhysicalResourceId | [0]" \
  --output text)
ROTATION_LOG_GROUP=$(aws lambda get-function-configuration \
  --function-name "$ROTATION_FUNCTION" --region "$API_REGION" \
  --query 'LoggingConfig.LogGroup' --output text)
START_TIME_MS=$(python3 -c 'import time; print(int((time.time() - 3600) * 1000))')
aws logs filter-log-events \
  --log-group-name "$ROTATION_LOG_GROUP" \
  --filter-pattern "ERROR" \
  --start-time "$START_TIME_MS" \
  --region "$API_REGION"

# 2. Check the signing key's rotation stages
aws secretsmanager describe-secret \
  --secret-id "$SECRET_ID" --region "$API_REGION" \
  --query '{LastRotated: LastRotatedDate, NextRotation: NextRotationDate, Versions: VersionIdsToStages}'

# 3. Check whether AWSPENDING exists (a missing stage returns ResourceNotFoundException)
aws secretsmanager get-secret-value \
  --secret-id "$SECRET_ID" \
  --version-stage AWSPENDING \
  --region "$API_REGION" >/dev/null
```

**Resolution:**

1. **If rotation Lambda is failing:** Check IAM permissions on the rotation Lambda role. It needs `secretsmanager:GetSecretValue`, `secretsmanager:PutSecretValue`, and `secretsmanager:UpdateSecretVersionStage`.

2. **If rotation is stuck (AWSPENDING exists but never promoted):**

   ```bash
   # Cancel the stuck rotation
   aws secretsmanager cancel-rotate-secret \
     --secret-id "$SECRET_ID" --region "$API_REGION"

   # Trigger a fresh rotation
   aws secretsmanager rotate-secret \
     --secret-id "$SECRET_ID" --region "$API_REGION"
   ```

3. **If requests are failing now:** Trusted proxies and backend middleware use bounded key caches and accept the rotation stages needed for overlap. After restoring valid `AWSCURRENT`/`AWSPENDING` state, allow the cache window to elapse. If an urgent refresh is required, restart the manifest processor in each affected region; do not distribute the signing key to clients:

   ```bash
   kubectl rollout restart deployment/manifest-processor -n gco-system
   ```

**Prevention:** The monitoring stack includes a CloudWatch alarm for rotation failures. Ensure the SNS topic has subscribers.

---

## Backend Certificate Rotation Fails

**Symptoms:** A `BackendTls*` alarm enters `ALARM`, the certificate-manager dead-letter queue receives a message, the reconciliation heartbeat is absent for two schedule intervals, or regional backend requests begin failing TLS verification.

The normal path is fully automatic: the manager reconciles every 1–24 hours (12 by default), renews 30-day regional leaves 10 days before expiry, stages 10-year root replacements 180 days before expiry, confirms publication of the pending public root before starting the activation delay, and keeps the previous root trusted for 45 days. Customer action is needed only when the AWS reconciliation path remains unhealthy long enough to exhaust retries or trigger an alarm.

**Diagnosis:**

```bash
PROJECT_NAME=${PROJECT_NAME:-gco}
API_REGION=${API_REGION:-us-east-2}
REGISTRY_REGION=${REGISTRY_REGION:-us-east-2}
API_STACK="${PROJECT_NAME}-api-gateway"

# Discover the manager, schedule, and terminal DLQ from CloudFormation.
MANAGER_FUNCTION=$(aws cloudformation list-stack-resources \
  --stack-name "$API_STACK" --region "$API_REGION" \
  --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && contains(LogicalResourceId, 'BackendTlsCertificateManager')].PhysicalResourceId | [0]" \
  --output text)
ROTATION_RULE=$(aws cloudformation list-stack-resources \
  --stack-name "$API_STACK" --region "$API_REGION" \
  --query "StackResourceSummaries[?ResourceType=='AWS::Events::Rule' && contains(LogicalResourceId, 'BackendTlsRotationSchedule')].PhysicalResourceId | [0]" \
  --output text)
DLQ_URL=$(aws cloudformation list-stack-resources \
  --stack-name "$API_STACK" --region "$API_REGION" \
  --query "StackResourceSummaries[?ResourceType=='AWS::SQS::Queue' && contains(LogicalResourceId, 'BackendTlsRotationDlq')].PhysicalResourceId | [0]" \
  --output text)
MANAGER_LOG_GROUP=$(aws lambda get-function-configuration \
  --function-name "$MANAGER_FUNCTION" --region "$API_REGION" \
  --query 'LoggingConfig.LogGroup' --output text)

aws events describe-rule --name "$ROTATION_RULE" --region "$API_REGION"
aws logs tail "$MANAGER_LOG_GROUP" --since 24h --region "$API_REGION"
aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessages --region "$API_REGION"

# Inspect public trust only; never print the root secret or private keys.
aws ssm get-parameter --name "/${PROJECT_NAME}/backend-tls/root-ca.pem" \
  --region "$REGISTRY_REGION" --query 'Parameter.LastModifiedDate'
METRIC_START=$(python3 -c 'from datetime import UTC, datetime, timedelta; print((datetime.now(UTC) - timedelta(days=2)).isoformat())')
METRIC_END=$(python3 -c 'from datetime import UTC, datetime; print(datetime.now(UTC).isoformat())')
aws cloudwatch get-metric-data --region "$API_REGION" \
  --metric-data-queries "[{\"Id\":\"heartbeat\",\"MetricStat\":{\"Metric\":{\"Namespace\":\"GCO/BackendTLS\",\"MetricName\":\"ReconciliationSuccess\",\"Dimensions\":[{\"Name\":\"Project\",\"Value\":\"${PROJECT_NAME}\"}]},\"Period\":43200,\"Stat\":\"Sum\"}}]" \
  --start-time "$METRIC_START" \
  --end-time "$METRIC_END"
```

**Resolution:**

1. **If the schedule is disabled:** Re-enable it, then invoke one reconciliation. Determine why it was disabled before clearing the alarm.

   ```bash
   aws events enable-rule --name "$ROTATION_RULE" --region "$API_REGION"
   aws lambda invoke --function-name "$MANAGER_FUNCTION" \
     --cli-binary-format raw-in-base64-out --payload '{"Action":"Rotate"}' \
     --region "$API_REGION" /tmp/backend-tls-reconcile.json
   jq . /tmp/backend-tls-reconcile.json
   ```

2. **If reconciliation fails:** Fix the logged KMS, Secrets Manager, SSM, ACM, quota, or IAM error and invoke the same `Rotate` action again. The operation is idempotent: existing regional ACM ARNs are reimported in place, and an unregistered first import is compensated automatically.

3. **If a root is pending:** Do not edit the encrypted root state or force promotion. The activation clock starts only after SSM confirms that proxies can fetch the pending public root. Restore SSM access and let the manager complete the configured propagation delay.

4. **If an expiry alarm remains active after a successful invocation:** Confirm each configured region's ARN under `/${PROJECT_NAME}/backend-tls/certificate-arn/<region>` and inspect it with `aws acm describe-certificate --certificate-arn <ARN> --region <REGION>`. Escalate before manually importing or deleting certificates; ad hoc replacement changes the stable ARN consumed by the ALB listener.

5. **After recovery:** Delete terminal DLQ messages only after the heartbeat and expiry metrics are healthy. Warm proxy processes refresh public trust within the configured cache TTL (five minutes by default), so routine recovery does not require restarts.

### Root-State Recovery

The root secret contains the only durable signing key. Treat recovery as a
security incident: freeze PKI configuration changes, preserve CloudTrail and
Lambda logs, and never print or download `SecretString` into a terminal, ticket,
or log. The commands below inspect only secret metadata and public material.

1. **Restore a secret scheduled for deletion.** A normal stack must keep
   `${PROJECT_NAME}/backend-tls/root-ca` available. `restore-secret` is
   idempotent only while Secrets Manager still retains the secret:

   ```bash
   ROOT_SECRET_ID="${PROJECT_NAME}/backend-tls/root-ca"
   DELETED_DATE=$(aws secretsmanager describe-secret \
     --secret-id "$ROOT_SECRET_ID" --region "$API_REGION" \
     --query 'DeletedDate' --output text)
   if [ "$DELETED_DATE" != "None" ]; then
     aws secretsmanager restore-secret \
       --secret-id "$ROOT_SECRET_ID" --region "$API_REGION"
   fi
   ```

2. **Move `AWSCURRENT` to a known-good version.** List version IDs, stages, and
   dates only. Select the version associated with the last healthy
   reconciliation—often the newest `AWSPREVIOUS`—and have a second operator
   verify the ID before changing stages:

   ```bash
   aws secretsmanager list-secret-version-ids \
     --secret-id "$ROOT_SECRET_ID" --include-deprecated \
     --region "$API_REGION" \
     --query 'Versions[].{VersionId:VersionId,Stages:VersionStages,Created:CreatedDate}'

   KNOWN_GOOD_VERSION_ID=${KNOWN_GOOD_VERSION_ID:?Set the independently verified version ID}
   CURRENT_VERSION_ID=$(aws secretsmanager list-secret-version-ids \
     --secret-id "$ROOT_SECRET_ID" --region "$API_REGION" \
     --query 'Versions[?contains(VersionStages, `AWSCURRENT`)].VersionId | [0]' \
     --output text)
   if [ -z "$CURRENT_VERSION_ID" ] || [ "$CURRENT_VERSION_ID" = "None" ]; then
     aws secretsmanager update-secret-version-stage \
       --secret-id "$ROOT_SECRET_ID" --version-stage AWSCURRENT \
       --move-to-version-id "$KNOWN_GOOD_VERSION_ID" \
       --region "$API_REGION"
   elif [ "$CURRENT_VERSION_ID" != "$KNOWN_GOOD_VERSION_ID" ]; then
     aws secretsmanager update-secret-version-stage \
       --secret-id "$ROOT_SECRET_ID" --version-stage AWSCURRENT \
       --move-to-version-id "$KNOWN_GOOD_VERSION_ID" \
       --remove-from-version-id "$CURRENT_VERSION_ID" \
       --region "$API_REGION"
   fi
   ```

   Invoke one reconciliation; the manager validates the restored
   key/certificate pair and fails without replacing regional leaves if the
   version is malformed:

   ```bash
   aws lambda invoke --function-name "$MANAGER_FUNCTION" \
     --cli-binary-format raw-in-base64-out --payload '{"Action":"Rotate"}' \
     --region "$API_REGION" /tmp/backend-tls-reconcile.json
   jq . /tmp/backend-tls-reconcile.json
   ```

3. **Validate public trust and every regional association.** Set `REGIONS` to
   the exact space-separated workload-region list from `cdk.json`. The first
   loop works from CloudShell; the final TLS handshake also requires network
   reachability to each internal ALB (for example, a VPC-connected shell):

   ```bash
   REGIONS=${REGIONS:?Set the exact space-separated workload regions}
   BACKEND_TLS_SERVER_NAME="backend.${PROJECT_NAME}.gco.internal"
   TRUST_BUNDLE=$(mktemp)
   trap 'rm -f "$TRUST_BUNDLE"' EXIT
   aws ssm get-parameter --name "/${PROJECT_NAME}/backend-tls/root-ca.pem" \
     --region "$REGISTRY_REGION" --query 'Parameter.Value' --output text \
     >"$TRUST_BUNDLE"
   if grep -q 'PRIVATE KEY' "$TRUST_BUNDLE"; then
     echo "ERROR: public trust parameter contains private material" >&2
     exit 1
   fi
   openssl crl2pkcs7 -nocrl -certfile "$TRUST_BUNDLE" \
     | openssl pkcs7 -print_certs -noout

   for REGION in $REGIONS; do
     CERT_ARN=$(aws ssm get-parameter \
       --name "/${PROJECT_NAME}/backend-tls/certificate-arn/${REGION}" \
       --region "$REGISTRY_REGION" --query 'Parameter.Value' --output text)
     aws acm describe-certificate --certificate-arn "$CERT_ARN" \
       --region "$REGION" \
       --query 'Certificate.{Status:Status,NotAfter:NotAfter,Domain:DomainName,InUseBy:InUseBy}'
     LOAD_BALANCERS=$(aws acm describe-certificate --certificate-arn "$CERT_ARN" \
       --region "$REGION" --query 'Certificate.InUseBy[]' --output text)
     if [ -z "$LOAD_BALANCERS" ] || [ "$LOAD_BALANCERS" = "None" ]; then
       echo "ERROR: $REGION certificate is not attached to a load balancer" >&2
       exit 1
     fi
     CERTIFICATE_ATTACHED=false
     VERIFIED_LOAD_BALANCER_ARN=""
     for LOAD_BALANCER_ARN in $LOAD_BALANCERS; do
       LISTENER_ARNS=$(aws elbv2 describe-listeners \
         --load-balancer-arn "$LOAD_BALANCER_ARN" --region "$REGION" \
         --query 'Listeners[].ListenerArn' --output text)
       for LISTENER_ARN in $LISTENER_ARNS; do
         ATTACHED=$(aws elbv2 describe-listener-certificates \
           --listener-arn "$LISTENER_ARN" --region "$REGION" \
           --query "Certificates[?CertificateArn=='${CERT_ARN}'].CertificateArn | [0]" \
           --output text)
         if [ "$ATTACHED" = "$CERT_ARN" ]; then
           CERTIFICATE_ATTACHED=true
           VERIFIED_LOAD_BALANCER_ARN=$LOAD_BALANCER_ARN
           break
         fi
       done
       [ "$CERTIFICATE_ATTACHED" = "true" ] && break
     done
     if [ "$CERTIFICATE_ATTACHED" != "true" ]; then
       echo "ERROR: $REGION certificate is absent from every load-balancer listener" >&2
       exit 1
     fi

     ALB_HOSTNAME=$(aws ssm get-parameter \
       --name "/${PROJECT_NAME}/alb-hostname-${REGION}" \
       --region "$REGISTRY_REGION" --query 'Parameter.Value' --output text)
     VERIFIED_ALB_HOSTNAME=$(aws elbv2 describe-load-balancers \
       --load-balancer-arns "$VERIFIED_LOAD_BALANCER_ARN" \
       --region "$REGION" --query 'LoadBalancers[0].DNSName' --output text)
     if [ -z "$VERIFIED_ALB_HOSTNAME" ] \
       || [ "$VERIFIED_ALB_HOSTNAME" = "None" ] \
       || [ "$ALB_HOSTNAME" != "$VERIFIED_ALB_HOSTNAME" ]; then
       echo "ERROR: $REGION SSM hostname does not identify the certificate-bearing ALB" >&2
       exit 1
     fi
     openssl s_client -connect "${VERIFIED_ALB_HOSTNAME}:443" \
       -servername "$BACKEND_TLS_SERVER_NAME" \
       -verify_hostname "$BACKEND_TLS_SERVER_NAME" \
       -verify_return_error -CAfile "$TRUST_BUNDLE" \
       </dev/null >/dev/null
   done
   ```

4. **Perform an emergency root rollover through the normal staged path.** Once
   a valid `AWSCURRENT` is restored, increment
   `context.backend_tls.root_generation` by exactly one in `cdk.json`, review
   that change, and deploy the API stack normally. Do not manufacture a root or
   edit encrypted state manually. The manager publishes the pending public root,
   starts the activation delay only after that publication succeeds, promotes
   it after the configured delay, reimports leaves into stable ACM ARNs, and
   retains the previous public root for the overlap window. Monitor all
   `BackendTls*` alarms throughout the activation and overlap periods.

5. **Escalate when the secret or every root-state version is irrecoverable.**
   Private root material cannot be reconstructed from SSM or ACM. If
   `describe-secret` returns `ResourceNotFoundException`, do not manually create
   a same-named secret: CloudFormation would still reference the deleted
   physical resource and the deployment would remain drifted. Keep the schedule
   disabled, preserve the still-serving leaves, and engage the service
   owner/security incident process before they expire. Recovery requires an
   approved maintenance window and a controlled CloudFormation teardown/redeploy
   of the API PKI and dependent regional listener associations; the redeployed
   managed secret then starts from its generated `UNINITIALIZED` state. Do not
   delete or overwrite a surviving root secret in place: doing so can switch
   leaves before warm proxy trust caches contain the new root and create an
   avoidable outage. Re-run the full validation above before restoring traffic
   and the schedule.

**Prevention:** Keep the certificate manager schedule enabled and route the API stack's `BackendTls*` CloudWatch alarms to an on-call destination. The alarms exist automatically, but notification subscriptions are deployment-specific; an unsubscribed alarm is visible in CloudWatch but cannot page anyone.

---

## Global Accelerator Stops Routing to a Region

**Symptoms:** Traffic is not reaching a specific region even though the EKS cluster is healthy. `gco capacity status` shows the region as healthy but no jobs are landing there.

**Diagnosis:** Replace `<ENDPOINT_GROUP_ARN>` and `<TARGET_GROUP_ARN>` with the
standard Global Accelerator endpoint-group ARN and ALB target-group ARN, and
`<REGION>` with the affected AWS region:

```bash
# 1. Inspect registered endpoints and their health
aws globalaccelerator describe-endpoint-group \
  --endpoint-group-arn <ENDPOINT_GROUP_ARN> \
  --query 'EndpointGroup.{Region:EndpointGroupRegion,Endpoints:EndpointDescriptions[].{Id:EndpointId,Health:HealthState,Reason:HealthReason}}' \
  --region us-west-2

# 2. Check ALB target health in the workload region
aws elbv2 describe-target-health \
  --target-group-arn <TARGET_GROUP_ARN> \
  --region <REGION>

# 3. Discover the generated GA-registration function and inspect its logs
GA_FUNCTION=$(aws cloudformation list-stack-resources \
  --stack-name gco-<REGION> --region <REGION> \
  --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && contains(LogicalResourceId, 'GaRegistration')].PhysicalResourceId | [0]" \
  --output text)
GA_LOG_GROUP=$(aws lambda get-function-configuration \
  --function-name "$GA_FUNCTION" --region <REGION> \
  --query 'LoggingConfig.LogGroup' --output text)
aws logs tail "$GA_LOG_GROUP" --since 1h --region <REGION>
```

**Resolution:**

1. **If ALB is not registered:** The GA registration Lambda runs during stack deployment. Trigger a stack update to re-register:

   ```bash
   gco stacks deploy gco-<REGION> -y
   ```

2. **If ALB health checks are failing:** GA health checks hit `/api/v1/health` on the ALB. Check that the health monitor pod is running and the ALB target group has healthy targets.

3. **If GA endpoint is unhealthy:** Check the health check configuration in `cdk.json` under `global_accelerator`. The grace period and interval may need adjustment if the region takes longer to warm up.

---

## SQS Dead Letter Queue Filling Up

**Symptoms:** `gco queue stats` shows messages in the DLQ. Jobs submitted via SQS are not being processed. The queue processor logs show repeated failures.

**Diagnosis:** Replace `<DLQ_URL>` with the dead-letter queue URL (from
`gco queue stats` or the SQS console) and `<REGION>` with the affected AWS region:

```bash
# 1. Check queue status
gco queue stats

# 2. Check DLQ message count
aws sqs get-queue-attributes \
  --queue-url <DLQ_URL> \
  --attribute-names ApproximateNumberOfMessages \
  --region <REGION>

# 3. Sample a DLQ message to see the failure reason
aws sqs receive-message \
  --queue-url <DLQ_URL> \
  --max-number-of-messages 1 \
  --region <REGION>

# 4. Inspect KEDA consumer Jobs and recent logs
kubectl get scaledjob,pods -n gco-system -l app=queue-processor
kubectl logs -n gco-system -l app=queue-processor --all-containers \
  --prefix --tail=100
```

**Resolution:**

1. **If a message body or manifest is malformed:** The DLQ preserves the original message for inspection. Correct the submission and resubmit it with `gco jobs submit-sqs`; do not manually acknowledge the bad message from the main queue.

2. **If a queue-processor Job is crashing:** Inspect the built-in KEDA `ScaledJob` and its short-lived Job pods, fix the image or deployment configuration, and redeploy the regional stack:

   ```bash
   kubectl describe scaledjob/sqs-queue-processor -n gco-system
   kubectl get jobs,pods -n gco-system -l app=queue-processor
   gco stacks deploy gco-<REGION> -y
   ```

3. **If manifests are rejected:** Check `job_validation_policy` in `cdk.json`, including `allowed_kinds`, `allowed_namespaces`, resource quotas, image registries, and security controls. Rejected, unsupported, and apply-failed messages are deliberately left unacknowledged so SQS retries them and eventually moves them to the DLQ. Change policy/RBAC intentionally, redeploy, and only then replay the DLQ.

4. **To replay DLQ messages** (after fixing the root cause), where
   `<DLQ_ARN>` is the dead-letter queue ARN and `<MAIN_QUEUE_ARN>` is the main
   job queue ARN:

   ```bash
   # Move messages from DLQ back to main queue
   aws sqs start-message-move-task \
     --source-arn <DLQ_ARN> \
     --destination-arn <MAIN_QUEUE_ARN> \
     --region <REGION>
   ```

**Prevention:** The monitoring stack deploys a CloudWatch alarm on `ApproximateNumberOfMessagesVisible` for the DLQ. If the alarm fires, messages are accumulating — follow the diagnosis steps above.

---

## Manifest Processor Rejecting Valid Jobs

**Symptoms:** Job submissions return validation errors even though the manifest looks correct. Common errors: "CPU exceeds max", "Namespace not allowed", "Untrusted image source".

**Diagnosis:**

```bash
# 1. Dry-run the manifest to see the exact error
gco jobs submit my-job.yaml -n gco-jobs --dry-run

# 2. Inspect the deployed-policy source of truth
jq '.context.job_validation_policy.resource_quotas' cdk.json

# 3. Check allowed namespaces in the same deployment policy
jq '.context.job_validation_policy.allowed_namespaces' cdk.json
```

**Resolution:**

1. **Resource limit exceeded:** Update the shared quota in `cdk.json` and redeploy:

   ```json
   "job_validation_policy": {
     "resource_quotas": {
       "max_cpu_per_manifest": "32",
       "max_memory_per_manifest": "128Gi",
       "max_gpu_per_manifest": 8
     }
   }
   ```

   Then: `gco stacks deploy gco-<REGION> -y`

2. **Namespace not allowed:** The stock submission namespace is only `gco-jobs`, matching its namespace-scoped write Role. To opt into another namespace, first grant the manifest-processor service account an intentionally scoped Role/RoleBinding there, then add that namespace to the shared policy and redeploy. Do not add `default` merely to bypass validation.

   ```json
   "job_validation_policy": {
     "allowed_namespaces": ["gco-jobs", "my-namespace"]
   }
   ```

3. **Untrusted image source:** Add the registry to the shared image policy and redeploy:

   ```json
   "job_validation_policy": {
     "trusted_registries": ["docker.io", "gcr.io", "my-registry.example.com"]
   }
   ```

---

## High API Gateway Latency

**Symptoms:** API requests take >5 seconds. CloudWatch shows elevated `Latency` metric on the API Gateway. Users report slow `gco jobs submit` commands.

**Diagnosis:**

```bash
PROJECT_NAME=${PROJECT_NAME:-gco}
API_REGION=${API_REGION:-us-east-2}
API_STACK="${PROJECT_NAME}-api-gateway"
START_TIME=$(python3 -c 'from datetime import UTC, datetime, timedelta; print((datetime.now(UTC) - timedelta(hours=1)).isoformat())')
END_TIME=$(python3 -c 'from datetime import UTC, datetime; print(datetime.now(UTC).isoformat())')

# 1. Check average API Gateway latency (inspect p99 separately in CloudWatch)
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApiGateway \
  --metric-name Latency \
  --dimensions Name=ApiName,Value="${PROJECT_NAME}-global-api" \
  --start-time "$START_TIME" \
  --end-time "$END_TIME" \
  --period 300 --statistics Average \
  --region "$API_REGION"

# 2. Discover the generated proxy function and inspect cold-start records
PROXY_FUNCTION=$(aws cloudformation list-stack-resources \
  --stack-name "$API_STACK" --region "$API_REGION" \
  --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && contains(LogicalResourceId, 'ApiGatewayProxyFunction')].PhysicalResourceId | [0]" \
  --output text)
PROXY_LOG_GROUP=$(aws lambda get-function-configuration \
  --function-name "$PROXY_FUNCTION" --region "$API_REGION" \
  --query 'LoggingConfig.LogGroup' --output text)
aws logs tail "$PROXY_LOG_GROUP" \
  --since 1h --filter-pattern INIT_START --region "$API_REGION"

# 3. Use the X-Ray console or service map when tracing is enabled
```

**Resolution:**

1. **Lambda cold starts:** Confirm cold starts are a material share of latency before changing concurrency. API Gateway invokes the unqualified function today; provisioned concurrency requires a published Lambda version or alias and the integration must target that alias. Model that change in CDK and redeploy—`$LATEST` does not support provisioned concurrency, and an out-of-band CLI setting would create stack drift.

2. **Global Accelerator routing latency:** Check if traffic is being routed to the nearest region. Use `traceroute` to the GA endpoint to verify.

3. **ALB target response time:** Check the ALB `TargetResponseTime` metric. If the manifest processor is slow, check pod resource utilization and consider scaling replicas.

---

## EKS Cluster Unreachable

**Symptoms:** `kubectl` commands fail with connection errors. `gco stacks list` shows the cluster but `gco jobs list -r <region>` fails.

**Diagnosis:** Replace `<REGION>` with the affected AWS region and `<VPC_ID>`
with the cluster's VPC ID:

```bash
# 1. Check cluster status
aws eks describe-cluster --name gco-<REGION> --region <REGION> \
  --query 'cluster.{Status:status,Endpoint:endpoint,Access:resourcesVpcConfig.endpointPublicAccess}'

# 2. Check if your kubeconfig is current
gco stacks access -r <REGION>

# 3. Check VPC connectivity (if endpoint is private)
aws ec2 describe-vpc-endpoints \
  --filters Name=vpc-id,Values=<VPC_ID> \
  --region <REGION>
```

**Resolution:**

1. **If cluster status is not ACTIVE:** Wait for the cluster to finish updating. EKS updates can take 10-20 minutes.

2. **If kubeconfig is stale:** Refresh it (replace `<REGION>` with the affected AWS region):

   ```bash
   gco stacks access -r <REGION>
   ```

3. **If endpoint access mode is PRIVATE:** You need to be on the VPC or use the regional API Gateway:

   ```bash
   gco --regional-api jobs list -r <REGION>
   ```

---

## Inference Endpoint Not Serving Traffic

**Symptoms:** `gco inference status <ENDPOINT_NAME>` shows the endpoint but requests fail. Health checks return errors.

**Diagnosis:** Replace `<ENDPOINT_NAME>` with your inference endpoint name (from `gco inference list`):

```bash
# 1. Check endpoint status
gco inference status <ENDPOINT_NAME>

# 2. Check pod health
gco inference health <ENDPOINT_NAME>

# 3. Check pod logs
kubectl logs -n gco-inference deployment/<ENDPOINT_NAME> --tail=50

# 4. Check if the model loaded successfully
gco inference models <ENDPOINT_NAME>
```

**Resolution:**

1. **If pods are in CrashLoopBackOff:** Check logs for OOM errors or model loading failures. Increase memory/GPU resources.

2. **If pods are running but not ready:** The readiness probe may be failing. Check if the model finished loading (large models can take 5-10 minutes).

3. **If the service is unreachable:** Check the endpoint Service and the shared Gateway route:

   ```bash
   kubectl get svc -n gco-inference
   kubectl get gateway,httproute -n gco-system
   ```

---

## Cost Spike Detection

**Symptoms:** `gco costs summary` shows unexpected increase. AWS Cost Explorer shows higher-than-expected charges.

**Diagnosis:**

```bash
# 1. Check cost breakdown by region
gco costs regions

# 2. Check cost trend
gco costs trend --days 14

# 3. Check for forgotten inference endpoints
gco inference list

# 4. Check for stuck jobs consuming GPU resources
gco jobs list --all-regions --status running

# 5. Check node pool sizes
gco nodepools list -r us-east-1
```

**Resolution:**

1. **Forgotten inference endpoints:** Stop or delete unused endpoints (replace
   `<ENDPOINT_NAME>` with the endpoint name from `gco inference list`):

   ```bash
   gco inference stop <ENDPOINT_NAME>
   gco inference delete <ENDPOINT_NAME>
   ```

2. **Stuck jobs:** Delete completed/failed jobs that are still holding resources:

   ```bash
   gco jobs bulk-delete --status completed --older-than-days 7 --all-regions --execute -y
   gco jobs bulk-delete --status failed --older-than-days 3 --all-regions --execute -y
   ```

3. **Unexpected node scaling:** EKS Auto Mode scales nodes based on pending pods. Check if there are pods stuck in Pending that are triggering unnecessary scaling.

4. **For ongoing monitoring:** Set up AWS Budgets with alerts (replace
   `<ACCOUNT_ID>` with your AWS account ID):

   ```bash
   aws budgets create-budget \
     --account-id <ACCOUNT_ID> \
     --budget file://budget.json \
     --notifications-with-subscribers file://notifications.json
   ```
