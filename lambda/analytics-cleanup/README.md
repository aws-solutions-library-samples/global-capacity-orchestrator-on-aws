# Analytics Cleanup Lambda

[CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) custom resource handler that runs during `gco-analytics` stack deletion. Removes resources that CloudFormation can't delete on its own because they were created at runtime (not in the template).

## Table of Contents

- [What it does](#what-it-does)
- [Why it's needed](#why-its-needed)
- [Environment variables](#environment-variables)
- [IAM permissions required](#iam-permissions-required)
- [Error handling](#error-handling)
- [Dependencies](#dependencies)
- [Tests](#tests)

## What it does

On stack **Delete**, in order:

1. Deletes every [SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html) app in the Studio domain and waits for them to drain
2. Deletes every space, then every user profile, waiting for each list to drain
3. Removes the [EFS](https://docs.aws.amazon.com/efs/latest/ug/whatisefs.html) resource policies on the Studio file system and on the SageMaker-managed home file system; they block the EFS describe calls below
4. Deletes every access point on the Studio file system
5. Deletes the SageMaker-managed home file system: its mount targets, then the file system
6. Revokes the rules of the SageMaker-managed NFS security groups and deletes them, retrying while SageMaker still holds a reference

On **Create** or **Update**: no-op (returns SUCCESS immediately).

## Why it's needed

- **Apps, spaces and user profiles** are created at runtime, the profiles by the presigned-URL [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) at first login (not by CloudFormation). CloudFormation can't delete the SageMaker domain while profiles exist.
- **EFS access points** are created by the presigned-URL Lambda for per-user home directories. EFS requires a file system's access points and mount targets deleted before `DeleteFileSystem` ([Deleting EFS file systems](https://docs.aws.amazon.com/efs/latest/ug/delete-efs-fs.html)), and CloudFormation deletes the Studio file system after this custom resource.
- **The SageMaker-managed home file system and NFS security groups** are created by SageMaker, outside the template; the security groups would block the VPC delete.

Without this cleanup, `cdk destroy gco-analytics` would fail with "domain has active user profiles", on the Studio file system, or on the VPC.

## Environment variables

| Variable | Description |
|----------|-------------|
| `DOMAIN_ID` | SageMaker Studio domain ID (e.g. `d-abc123`) |
| `EFS_ID` | Studio EFS file system ID (e.g. `fs-abc123`); without it the resource-policy and access-point steps for that file system are skipped |
| `VPC_ID` | Analytics VPC ID; without it the security-group step is skipped |
| `REGION` | AWS region |

## IAM permissions required

The stack grants these through a customer-managed policy (`CleanupFunctionPolicy` in `gco/stacks/analytics_stack.py`), so the grant outlives the custom resource's run. Each statement names what the handler touches as far as the template knows it (`<region>`, `<account>` and `<domain-id>` are the stack's own):

| Actions | Resource |
|---------|----------|
| `sagemaker:ListApps`, `sagemaker:ListSpaces`, `sagemaker:ListUserProfiles`, `ec2:DescribeSecurityGroups` | `*` (these take no resource) |
| `sagemaker:DescribeDomain`, `sagemaker:DeleteApp`, `sagemaker:DeleteSpace`, `sagemaker:DeleteUserProfile` | The domain: `arn:aws:sagemaker:<region>:<account>:domain/<domain-id>`, and `app/<domain-id>/*`, `space/<domain-id>/*`, `user-profile/<domain-id>/*` under it |
| `elasticfilesystem:DescribeAccessPoints` | `Studio_EFS` |
| `elasticfilesystem:DeleteAccessPoint` | `arn:aws:elasticfilesystem:<region>:<account>:access-point/*` (the access points are created at run time) |
| `elasticfilesystem:DeleteFileSystemPolicy`, `elasticfilesystem:DescribeMountTargets`, `elasticfilesystem:DeleteMountTarget`, `elasticfilesystem:DeleteFileSystem` | `arn:aws:elasticfilesystem:<region>:<account>:file-system/*` (SageMaker creates the home file system at run time and reports its id only through `DescribeDomain`) |
| `ec2:RevokeSecurityGroupIngress`, `ec2:RevokeSecurityGroupEgress`, `ec2:DeleteSecurityGroup` | `arn:aws:ec2:<region>:<account>:security-group/*` with `ArnEquals` `ec2:Vpc` = the analytics VPC |

`tests/test_analytics_stack.py` reads the handler's API calls and fails when one is not granted.

## Error handling

- If an app, space or user profile fails to delete or does not drain in time, the handler raises: the custom resource fails and CloudFormation stops before the domain delete, which would fail on them. Retry the stack delete once the cause is fixed.
- Failures in the EFS policy, access-point, managed-EFS and security-group steps are logged and do not fail the custom resource.
- Every error is collected and logged as one warning for operator visibility.

## Dependencies

None beyond `boto3` (provided by the Lambda runtime).

## Tests

See `tests/test_analytics_cleanup_lambda.py`.
