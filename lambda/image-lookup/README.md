# Image Lookup

Lookup-or-create custom resource handler for [ECR](https://docs.aws.amazon.com/AmazonECR/latest/userguide/what-is-ecr.html) repositories under
`gco/<name>`. Implements the adopt-or-create pattern so a previously
retained repository (left over from a prior deploy with
`RemovalPolicy=RETAIN`) is rebound to the new stack rather than failing
the deploy with `RepositoryAlreadyExistsException`.

## Table of Contents

- [Trigger](#trigger)
- [How It Works](#how-it-works)
- [Input](#input)
- [Output](#output)
- [Environment Variables](#environment-variables)
- [IAM Permissions](#iam-permissions)
- [Tests](#tests)

## Trigger

[CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) custom resource — invoked by the [CDK](https://docs.aws.amazon.com/cdk/v2/guide/home.html) `Provider`
framework whenever the global stack creates, updates, or tears down an
image registry repository.

## How It Works

### Create / Update

1. `DescribeRepositories` against the requested repo name
2. If found, adopt the existing ARN/URI and skip creation
3. If absent, `CreateRepository` with `MUTABLE` tag mutability and
   `scanOnPush=true`
4. When `LifecyclePolicy` is supplied, validate it parses as JSON and
   call `PutLifecyclePolicy`

The adopt path returns `Adopted=true` in `Data` so CloudFormation
events visibly distinguish brand-new repos from rebinds.

### Delete

1. `DescribeRepositories` — if absent, return success (idempotent
   teardown)
2. `ListTagsForResource` — when `gco:retain=true` is present, return
   success without touching the repo. The retain tag wins regardless
   of stack-level `RemovalPolicy`
3. When `RemovalPolicy != "destroy"`, return success without deletion
4. When `RemovalPolicy == "destroy"` and `EmptyOnDelete == True`,
   paginate every image digest via `describe_images`, then
   `BatchDeleteImage` in chunks of 100, then `DeleteRepository(force=False)`
5. When `RemovalPolicy == "destroy"` and `EmptyOnDelete == False`,
   call `DeleteRepository(force=False)` directly (ECR will reject for
   non-empty repos — surfaces as a CloudFormation rollback)

## Input

CloudFormation custom resource event:

```json
{
  "RequestType": "Create | Update | Delete",
  "PhysicalResourceId": "arn:aws:ecr:...",
  "ResourceProperties": {
    "RepositoryName": "gco/my-app",
    "RemovalPolicy": "retain | destroy",
    "EmptyOnDelete": false,
    "LifecyclePolicy": "<JSON string, optional>"
  }
}
```

## Output

```json
{
  "PhysicalResourceId": "arn:aws:ecr:...",
  "Data": {
    "RepositoryArn": "arn:aws:ecr:us-east-1:123:repository/gco/my-app",
    "RepositoryUri": "123.dkr.ecr.us-east-1.amazonaws.com/gco/my-app",
    "RepositoryName": "gco/my-app",
    "Adopted": "true | false"
  }
}
```

For `Delete`, `Data` reports `{"Deleted": "true"}` or
`{"Deleted": "false", "Reason": "<retain-tag | removal-policy-retain>"}`.

The CDK `Provider` framework wraps this dict into the protocol-required
CloudFormation custom resource response envelope.

## Environment Variables

None. The handler uses the default boto3 region (the [Lambda](https://docs.aws.amazon.com/lambda/latest/dg/welcome.html)'s own
region) and inherits credentials from the execution role.

## IAM Permissions

- `ecr:DescribeRepositories` on `arn:aws:ecr:<region>:<account>:repository/gco/*`
- `ecr:CreateRepository` (cannot be scoped to a name prefix)
- `ecr:PutLifecyclePolicy` on `arn:aws:ecr:<region>:<account>:repository/gco/*`
- `ecr:ListTagsForResource` on `arn:aws:ecr:<region>:<account>:repository/gco/*`
- `ecr:DescribeImages` on `arn:aws:ecr:<region>:<account>:repository/gco/*`
- `ecr:BatchDeleteImage` on `arn:aws:ecr:<region>:<account>:repository/gco/*`
- `ecr:DeleteRepository` on `arn:aws:ecr:<region>:<account>:repository/gco/*`

## Tests

Unit tests live in [`tests/test_lambda_image_lookup.py`](../../tests/test_lambda_image_lookup.py)
and cover every branch:

- Adopt path when the repository already exists
- Create path when the repository is missing
- Lifecycle policy application — including the empty/whitespace no-op
  and the JSON-validation failure
- Delete: idempotent absence, retain-tag preservation, removal-policy
  retain, destroy with `EmptyOnDelete=True` (paginated digest
  collection + chunked deletion), destroy with `EmptyOnDelete=False`
- The `RepositoryNotFoundException` translation through both the
  typed exception and the generic `ClientError` shape
- Unsupported `RequestType` raising `ValueError`

Run with:

```bash
pytest tests/test_lambda_image_lookup.py -v
```
