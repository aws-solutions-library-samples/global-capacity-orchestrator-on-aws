# pcs-ami-lookup

[CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) custom resource (a CDK `cr.Provider` on-event handler) that resolves the AMI an [AWS PCS](https://docs.aws.amazon.com/pcs/latest/userguide/what-is-service.html) compute node group launches, for the managed-mode `pcs` block (`gco/stacks/pcs_cluster.py`).

## Why a Lambda

A node group's `ami` is `sample`, `dlami` or an `ami-` id. The two keywords resolve differently, and neither fits `cr.AwsCustomResource`:

| Kind | How AWS publishes it | How this handler finds it |
|------|----------------------|---------------------------|
| `sample` | Public images named `aws-pcs-sample_ami-al2023-<arch>*`, whose description lists the Slurm versions they support; no SSM parameter | `describe-images` owned by Amazon, filtered by name, `*<slurm version>*` description and `available` state, newest `CreationDate` ([the documented CLI recipe](https://docs.aws.amazon.com/pcs/latest/userguide/working-with_ami_samples.html)) |
| `dlami` | The SSM public parameter `/aws/service/pcs/ami/dlami-base-ubuntu2404/<arch>/latest/ami-id`, and public images named `aws-pcs-ready-dlami-base-ubuntu2404-<arch>-*` | The parameter first; the name search when the parameter is absent in the Region ([PCS-ready DLAMI](https://docs.aws.amazon.com/pcs/latest/userguide/working-with_ami_pcs-ready-dlami.html)) |

`cr.AwsCustomResource` cannot sort a `describe-images` response, and CI synthesizes without credentials, so a CDK context lookup is out too.

## Contract

| `ResourceProperties` | Meaning |
|---|---|
| `Kind` | `sample` or `dlami` |
| `Architecture` | `x86_64` or `arm64` (from the node group's instance types) |
| `SlurmVersion` | The cluster's Slurm major.minor; the sample AMI's description filter |
| `Region` | Where to look |
| `NodeGroup` | For the physical id and the log line |

Create and Update return `Data.ImageId`, `Data.Name` and `Data.Description`; Delete is a no-op. The physical id is stable per node group, so the resource is never replaced; a newer AMI is picked up on the next deploy. No match raises, which CloudFormation reports as the resource failing with the pattern that found nothing.

## Permissions

`ec2:DescribeImages` (no resource-level scoping) and `ssm:GetParameter` on `arn:<partition>:ssm:<region>::parameter/aws/service/pcs/*` (the public parameters have no account id).
