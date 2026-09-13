# 0005. CloudFormation-generated S3 bucket names

- **Status:** Accepted
- **Date:** 2026-09-12
- **Deciders:** GCO maintainers
- **Supersedes:** none
- **Superseded by:** none

## Context

Four of the project's ten S3 buckets carried explicit, deterministic physical
names derived from `project_name`, the account, and a Region:
`<project>-cost-reports-<account>-<monitoring-region>` (monitoring stack),
`<project>-cluster-shared-<account>-<global-region>` (global stack),
`<project>-regional-shared-<account>-<region>` (each regional stack), and
`<project>-analytics-studio-<account>-<region>` (analytics stack). The other
six — the model-weights bucket and every access-logs bucket — have always used
the name CloudFormation generates (`<stack>-<construct>-<random>`).

The deterministic names existed for two reasons. The cost-report bucket's name
was *load-bearing*: the regional stacks deploy before the monitoring stack that
owns the bucket, so they granted their cost-monitor role `s3:PutObject` by a
literal ARN they could compute at synth time and injected the same name into
the cost-monitor container environment. The other three names were merely
*convenient*: they made cdk-nag allow-list strings and documentation
readable. Nothing consumed them — every consumer already resolved those
buckets through the SSM parameters (`/<project>/cluster-shared-bucket/{name,arn,region}`,
`/<project>/regional-shared-bucket/...`) or the stack's own construct tokens.

S3 bucket names live in a single global namespace, and S3 does not guarantee
that a deleted name becomes reusable promptly. On 2026-09-12 a live release
validation run — which deploys and destroys the whole topology in one account —
failed to create `gco-monitoring`: `CreateBucket` on the deterministic
cost-report name returned `BucketAlreadyExists` although the previous
incarnation had been deleted three days earlier and `HeadBucket` reported the
name free minutes later. Any fixed name that is destroyed and recreated is
exposed to this, and the regional-shared bucket's optional `retain` removal
policy makes the exposure structural: a retained bucket from an earlier
deployment occupies the only name the next deployment can compute.

A fixed name also has a security edge. Consumers that reconstruct a bucket
name trust a *name*, not an *owner*: if another account claimed the name during
the window after deletion, a deploy would fail closed, but the already-deployed
regional grants would point at a bucket the project does not own.

## Decision

We will let CloudFormation generate the physical name of every GCO S3 bucket,
and we will treat the published identity — not a reconstructable name — as
the only way to find one.

1. No GCO stack sets `bucket_name`. `tests/test_bucket_naming_contract.py`
   enforces this on the stack sources, and the per-stack tests assert that no
   synthesized `AWS::S3::Bucket` carries a `BucketName`.
2. The owning stack publishes each bucket's `name`, `arn`, and `region` as SSM
   parameters under a `project_name`-derived prefix in the bucket's home
   Region. The cost-report bucket joins the existing contract at
   `/<project>/cost-report-bucket/{name,arn,region}` in the monitoring Region.
3. Access to the cost-report bucket is granted in the direction the deploy
   order allows. The monitoring stack, which deploys last and receives every
   regional stack, admits each regional cost-monitor role through the bucket
   policy and the KMS key policy (principal based, via the same cross-Region
   reference mechanism its dashboards already use). The regional stack grants
   its cost-monitor role exactly one permission — `ssm:GetParameter` on the
   `/name` parameter, by literal ARN — and no longer names the bucket or key.
4. The cost-monitor service discovers its bucket at runtime from that
   parameter (`COST_REPORT_BUCKET_PARAMETER` / `COST_REPORT_BUCKET_PARAMETER_REGION`),
   caches it, and treats "not published yet" as a wait rather than a failure:
   the scheduled pass skips, the report endpoints answer 503, and
   `/internal/status` shows the wait. An explicit `COST_REPORT_BUCKET` remains
   as a kind/CI override.
5. The CLI (`gco storage`) and release validation read the published
   parameters instead of computing names, and release validation compares the
   bucket the cost API reports against the published one — proving the
   runtime discovery and the operator surface agree.

Out of scope: other resource types with explicit physical names (DynamoDB
tables, SQS queues, the auth secret, the aggregator role). Their namespaces are
per account and Region, their names are load-bearing for cross-Region literal
ARNs, and none has shown a reuse hazard.

## Consequences

### Positive

- A destroy-and-redeploy, a retained bucket from an earlier deployment, or two
  deployments in one account can never collide on a bucket name.
- Consumers trust the identity the deployment published in its own account,
  never a name that another party could claim.
- The regional cost-monitor role loses its `Resource::*` KMS statement and its
  object-key wildcard; the remaining grant is a single literal SSM parameter
  ARN with no cdk-nag acknowledgement.

### Negative

- **Upgrading an existing deployment replaces the four buckets.** `BucketName`
  is immutable, so removing it makes CloudFormation create a new bucket and
  delete the old one; with the default `DESTROY` removal policy the old
  bucket's objects are emptied and lost, and with `retain` the old bucket is
  orphaned under its previous name. Operators upgrading a deployment whose
  cluster-shared, regional-shared, cost-report, or Studio bucket holds data
  they need must copy it aside first (`gco storage s3-inventory` lists every
  bucket and its home Region) and back afterwards. Fresh deployments and
  release validation are unaffected.
- Bucket names are no longer readable at a glance (`gco-global-clustersharedbucket1a2b3c4d-…`).
  `gco storage s3-inventory` and the SSM parameters are the lookup surface.
- The cost-monitor service depends on SSM at runtime (one `GetParameter` per
  15-minute refresh) where it previously depended only on S3.

### Neutral

- The cost-report pipeline's first-boot behaviour is unchanged in effect —
  before, the bucket did not exist until the monitoring stack deployed and
  writes failed; now the parameter does not exist and writes are skipped —
  but the wait is now explicit in the service status.
- Release validation gains an accepted-residue class for Tagging API records
  of deleted VPC endpoints (an unrelated lag observed in the same run) and a
  free-disk preflight; neither changes what a run proves.

## Alternatives considered

### Option A — keep deterministic names and retry on `BucketAlreadyExists`

- **Summary:** teach the deploy orchestrator to delete the rolled-back stack
  and retry when S3 reports the deterministic name unavailable.
- **Why not:** it papers over a namespace hazard that S3 documents, leaves the
  `retain` policy structurally broken, keeps the name-trust edge, and would
  mask genuine conflicts behind automatic retries.

### Option B — deterministic prefix plus a per-stack nonce

- **Summary:** keep a readable prefix and append a fragment of `AWS::StackId`
  so each stack instance mints a unique name, granting by prefix wildcard.
- **Why not:** a prefix wildcard on an identity policy is exactly the
  name-squatting exposure the change removes, the 63-character limit shrinks
  the usable `project_name` budget, and it invents a naming scheme where
  CloudFormation already has one.

### Option C — move the cost-report bucket into the global stack

- **Summary:** create the bucket in the first-deployed stack so regional
  stacks can resolve it through the existing cross-Region SSM read.
- **Why not:** Athena requires its results location in the workgroup's
  Region, so the monitoring Region would need a second bucket whenever it
  differs from the global Region; the principal-based grant in the monitoring
  stack achieves the same decoupling without moving data.

## References

- Pull request #375 (platform services hosting contract), which carries this
  change after the live validation failure it responds to.
- `gco/stacks/constants.py` — the S3 bucket naming policy and the
  `cost_report_ssm_parameter_prefix` contract.
- `docs/COST_MONITORING.md`, `docs/CLUSTER_SHARED_BUCKET.md`,
  `docs/REGIONAL_SHARED_BUCKET.md` — the operator-facing lookup surfaces.
