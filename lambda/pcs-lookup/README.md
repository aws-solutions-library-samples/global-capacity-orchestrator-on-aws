# pcs-lookup

[CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/Welcome.html) custom resources (one CDK `cr.Provider` on-event handler) that look up the [AWS PCS](https://docs.aws.amazon.com/pcs/latest/userguide/what-is-service.html) facts a managed-mode `pcs` block needs at deploy time and `cr.AwsCustomResource` cannot shape (`gco/stacks/pcs_cluster.py` `PcsLookup`). The event's `ResourceType` selects the lookup.

## `Custom::PcsAmiLookup`

Resolves the AMI a compute node group launches. A node group's `ami` is `sample`, `dlami` or an `ami-` id. The two keywords resolve differently, and neither fits `cr.AwsCustomResource`:

| Kind | How AWS publishes it | How this handler finds it |
|------|----------------------|---------------------------|
| `sample` | Public images named `aws-pcs-sample_ami-al2023-<arch>*`, whose description lists the Slurm versions they support; no SSM parameter | `describe-images` owned by Amazon, filtered by name, `*<slurm version>*` description and `available` state, newest `CreationDate` ([the documented CLI recipe](https://docs.aws.amazon.com/pcs/latest/userguide/working-with_ami_samples.html)) |
| `dlami` | The SSM public parameter `/aws/service/pcs/ami/dlami-base-ubuntu2404/<arch>/latest/ami-id`, and public images named `aws-pcs-ready-dlami-base-ubuntu2404-<arch>-*` | The parameter first; the name search when the parameter is absent in the Region ([PCS-ready DLAMI](https://docs.aws.amazon.com/pcs/latest/userguide/working-with_ami_pcs-ready-dlami.html)) |

`cr.AwsCustomResource` cannot sort a `describe-images` response, and CI synthesizes without credentials, so a CDK context lookup is out too.

| `ResourceProperties` | Meaning |
|---|---|
| `Kind` | `sample` or `dlami` |
| `Architecture` | `x86_64` or `arm64` (from the node group's instance types) |
| `SlurmVersion` | The cluster's Slurm major.minor; the sample AMI's description filter |
| `Region` | Where to look |
| `NodeGroup` | For the physical id and the log line |

Create and Update return `Data.ImageId`, `Data.Name` and `Data.Description`; Delete is a no-op. The physical id is stable per node group, so the resource is never replaced; a newer AMI is picked up on the next deploy. No match raises, which CloudFormation reports as the resource failing with the pattern that found nothing.

## `Custom::PcsControllerLookup`

Resolves the cluster controller's address for the Prometheus scrape of its Slurm metrics (`pcs.managed.metrics.enabled`). `GetCluster` lists the controller's endpoints (`SLURMCTLD`, `SLURMDBD`, `SLURMRESTD`) in no guaranteed order and `cr.AwsCustomResource` can only address a list by index, so the handler picks the `SLURMCTLD` entry by type.

| `ResourceProperties` | Meaning |
|---|---|
| `ClusterId` | The cluster to describe (`pcs_...`) |
| `Region` | Where it lives |

Create and Update return `Data.PrivateIpAddress`, `Data.Port` (6817) and `Data.Address` (`ip:port`, the `{{PCS_CONTROLLER_ADDRESS}}` the kubectl-applier writes into the `ScrapeConfig`); Delete is a no-op. A cluster without a `SLURMCTLD` endpoint raises, which CloudFormation reports with the cluster's status.

## Permissions

`ec2:DescribeImages` (no resource-level scoping), `ssm:GetParameter` on `arn:<partition>:ssm:<region>::parameter/aws/service/pcs/*` (the public parameters have no account id), and `pcs:GetCluster` on the managed cluster's ARN.
