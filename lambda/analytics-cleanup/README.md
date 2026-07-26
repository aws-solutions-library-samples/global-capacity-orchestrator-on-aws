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

On stack **Delete**:

1. Lists and deletes all [SageMaker](https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html) user profiles in the Studio domain
2. Lists and deletes all [EFS](https://docs.aws.amazon.com/efs/latest/ug/whatisefs.html) access points on the Studio file system

On **Create** or **Update**: no-op (returns SUCCESS immediately).

## Why it's needed

- **User profiles** are created by the presigned-URL [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html) at first login (not by CloudFormation). CloudFormation can't delete the SageMaker domain while profiles exist.
- **EFS access points** are created by the presigned-URL Lambda for per-user home directories. CloudFormation can't delete the EFS file system while access points exist.

Without this cleanup, `cdk destroy gco-analytics` would fail with "domain has active user profiles" or "file system has access points".

## Environment variables

| Variable | Description |
|----------|-------------|
| `DOMAIN_ID` | SageMaker Studio domain ID (e.g. `d-abc123`) |
| `EFS_ID` | EFS file system ID (e.g. `fs-abc123`) |
| `REGION` | AWS region |

## IAM permissions required

```json
{
  "Effect": "Allow",
  "Action": [
    "sagemaker:ListUserProfiles",
    "sagemaker:DeleteUserProfile",
    "efs:DescribeAccessPoints",
    "efs:DeleteAccessPoint"
  ],
  "Resource": "*"
}
```

## Error handling

- Individual deletion failures are logged but don't block the overall cleanup
- The handler always returns `SUCCESS` so CloudFormation can proceed with stack deletion
- Errors are collected and logged as warnings for operator visibility

## Dependencies

None beyond `boto3` (provided by the Lambda runtime).

## Tests

See `tests/test_analytics_cleanup_lambda.py`.
