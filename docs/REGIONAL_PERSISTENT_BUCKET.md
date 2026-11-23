# Regional Persistent Bucket

Each workload Region can have a persistent bucket: an S3 bucket in that Region
that `gco upgrade` leaves in place. Stage outputs there before an upgrade
deletes the regional stack, and read them back once the regional stack has
been rebuilt.

## Table of Contents

- [Overview](#overview)
- [Turning it on](#turning-it-on)
- [Expiry](#expiry)
- [What deletes it](#what-deletes-it)
- [Staging data before an upgrade](#staging-data-before-an-upgrade)
- [Discovery and permissions](#discovery-and-permissions)
- [Removing a Region or turning it off](#removing-a-region-or-turning-it-off)

## Overview

`gco upgrade` destroys and recreates every regional stack, and with it the
data that lives there: EFS, FSx for Lustre, the in-cluster volumes and the
[regional-shared bucket](REGIONAL_SHARED_BUCKET.md)
([UPGRADING.md → What is destroyed, what survives](UPGRADING.md#what-is-destroyed-what-survives)).
The persistent bucket lives in its own stack, `<project>-<region>-persistent`,
in the control-plane tier next to `<project>-global` and
`<project>-api-gateway`. `gco upgrade` and
`gco stacks destroy-all --keep-control-plane` keep that tier, so the bucket and
its objects survive both.

| | Persistent bucket | Regional-shared bucket | Cluster-shared bucket |
|---|---|---|---|
| Stack | `<project>-<region>-persistent` | `<project>-<region>` | `<project>-global` |
| Location | the Region's own S3 | the Region's own S3 | the global Region |
| Survives `gco upgrade` | yes | no | yes |
| `gco storage` alias | `regional-persistent:<region>` | `regional-shared:<region>` | `cluster-shared` |
| ConfigMap (namespaces) | `gco-regional-persistent-bucket` (`gco-jobs`) | `gco-regional-shared-bucket` (`gco-jobs`, `gco-system`, `gco-inference`) | `gco-cluster-shared-bucket` (all three) |

Use the persistent bucket for data a Region produces and needs again after an
upgrade. Copies stay in the Region, so they cost no cross-Region transfer and
take less time than the cluster-shared bucket from any Region other than the
global one.

The bucket is versioned, encrypted with its own customer-managed KMS key,
blocks public access, refuses requests without TLS, and logs requests to an
access-logs bucket in the same stack.

## Turning it on

The `regional_persistent_bucket` block in `cdk.json` controls it:

```json
"regional_persistent_bucket": {
  "enabled": true,
  "expiration_days": null
}
```

The shipped `cdk.json` sets `enabled` to `true`, so a new deployment gets a
persistent bucket in every Region in `deployment_regions.regional`.

`enabled` is `false` when the block or the key is absent, which is the case
for a `cdk.json` from v9.1.0 or earlier. That is on purpose: the upgrade into
this release runs the stack cycle with the previous release's `gco`, which
cannot deploy a stack type it does not know. Upgrade first, staging data the
way that release's guide describes, then add the block and run
`gco stacks deploy-all -y` with this release. That creates each Region's
persistent stack and updates the regional stacks in place to use it, without
a teardown, and the bucket is ready for the next upgrade. Adding the block
before that upgrade would make the previous release's `gco upgrade` fail when
it tries to deploy the new stacks.

## Expiry

By default objects stay until someone deletes them, so data staged for an
upgrade that stops halfway is still there when you resume it. Three lifecycle
rules apply either way:

- Overwritten and deleted object versions expire after 30 days.
- Delete markers that no longer have versions behind them are removed.
- Multipart uploads that never completed, such as an interrupted
  `gco storage sync`, are aborted after 7 days.

To expire objects automatically, set `expiration_days` to a whole number of
days, for example `14`, and run `gco stacks deploy-all -y`. S3 then expires
every object that many days after it was written, existing objects included,
and keeps the expired version for another 30 days as a noncurrent version.
Set it back to `null` to keep objects again.

## What deletes it

| Command | The persistent bucket |
|---------|-----------------------|
| `gco upgrade` | Kept, with its objects |
| `gco stacks destroy-all --keep-control-plane` | Kept, with its objects |
| `gco stacks destroy <project>-<region>` (the regional stack) | Kept, with its objects |
| `gco stacks destroy-all` | **Deleted, with every object and version.** The KMS key enters its 7-day pending-deletion window |
| `gco stacks destroy <project>-<region>-persistent` | **Deleted**, as above |

Copy out anything you need to keep before a full `destroy-all`.

## Staging data before an upgrade

From your machine, with `gco storage sync`:

```bash
# Before the upgrade: copy local outputs up.
gco storage sync regional-persistent:us-east-1 ./staged --direction upload --prefix upgrade/$(date +%F)/

# After the upgrade: bring them back.
gco storage sync regional-persistent:us-east-1 ./staged --prefix upgrade/$(date +%F)/
```

From the cluster, a Job in `gco-jobs` running as `gco-service-account` can
write to the bucket directly, which avoids a round trip through your machine.
This one copies the `outputs/` directory of the `gco-shared-storage` EFS
volume into the bucket:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: stage-efs-outputs
  namespace: gco-jobs
spec:
  backoffLimit: 1
  template:
    spec:
      serviceAccountName: gco-service-account
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
      restartPolicy: Never
      containers:
      - name: stage
        image: python:3.14.9-slim
        command:
        - python
        - -c
        - |
          import os, pathlib, subprocess, sys
          subprocess.check_call([sys.executable, "-m", "pip", "install", "--target", "/tmp/pylib", "boto3", "-q"])
          sys.path.insert(0, "/tmp/pylib")
          import boto3
          s3 = boto3.client("s3", region_name=os.environ["persistentBucketRegion"])
          root = pathlib.Path("/efs/outputs")
          for path in root.rglob("*"):
              if path.is_file():
                  key = f"upgrade/efs-outputs/{path.relative_to(root)}"
                  s3.upload_file(str(path), os.environ["persistentBucketName"], key)
                  print(f"staged {key}")
        envFrom:
        - configMapRef:
            name: gco-regional-persistent-bucket
        volumeMounts:
        - name: efs
          mountPath: /efs
          readOnly: true
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop:
            - ALL
        resources:
          requests:
            cpu: 250m
            memory: 256Mi
          limits:
            cpu: 500m
            memory: 512Mi
      volumes:
      - name: efs
        persistentVolumeClaim:
          claimName: gco-shared-storage
```

Submit it with `gco jobs submit-direct stage-efs-outputs.yaml -r us-east-1`
and wait for it to complete before you start the upgrade. After the upgrade, a
Job with the same mounts can copy the objects back onto the new EFS volume.

## Discovery and permissions

The persistent stack publishes the bucket's identity as SSM parameters in its
Region, under `/<project>/regional-persistent-bucket/`:

| Parameter | Value |
|-----------|-------|
| `name` | The bucket's name, which CloudFormation generates ([ADR-0005](adr/0005-cloudformation-generated-s3-bucket-names.md)) |
| `arn` | The bucket's ARN |
| `region` | The bucket's Region |
| `kms-key-arn` | The ARN of the bucket's KMS key |

The regional stack of the same Region reads them when it deploys, which is why
`gco stacks deploy-all` deploys the persistent stacks with the global stacks,
before the regional ones. From them it:

- grants `gco-service-account` (the job-pod role) `s3:GetObject`,
  `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` and
  `s3:GetBucketLocation` on the bucket, and `kms:Decrypt`, `kms:Encrypt`,
  `kms:GenerateDataKey` and `kms:DescribeKey` on its key, and nothing else;
- creates the `gco-regional-persistent-bucket` ConfigMap in `gco-jobs`, with
  `persistentBucketName`, `persistentBucketArn` and `persistentBucketRegion`.

The keys differ from those of the other bucket ConfigMaps, so one pod can use
`envFrom` with all three. A principal other than the job-pod role, such as the
one you run `gco storage sync` with, needs the same S3 permissions on the
bucket and the same KMS permissions on the key in its own IAM policy.
`gco storage s3-inventory` lists the bucket and its access-logs bucket for
every configured Region, deployed or not.

## Removing a Region or turning it off

Each persistent stack belongs to a Region in `deployment_regions.regional`.
Once the Region leaves `cdk.json`, or `enabled` is set to `false`, the stack is
no longer part of the CDK app, and `gco stacks destroy` cannot reach it, so
delete it first:

```bash
gco storage sync regional-persistent:us-west-2 ./keep   # copy out what you need
gco stacks destroy gco-us-west-2-persistent -y          # deletes the bucket and its objects
```

Then remove the Region (see
[CUSTOMIZATION.md → Removing a Region](CUSTOMIZATION.md#removing-a-region)) or
set `enabled` to `false` and run `gco stacks deploy-all -y`, which removes the
grant and the ConfigMap from the regional stacks.
