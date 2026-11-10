# NVIDIA Run:ai capacity

`gco capacity runai` reads GPU capacity from an
[NVIDIA Run:ai](https://run-ai-docs.nvidia.com/) control plane and reports it next
to GCO's own AWS capacity views. It is read-only and deliberately narrow: GCO
does not submit work to Run:ai, and Run:ai rows are not part of the AWS Region
ranking. [ADR 0006](adr/0006-runai-read-only-capacity-source.md) records why.

## Table of Contents

- [What it reports](#what-it-reports)
- [What it does not do](#what-it-does-not-do)
- [Configure it](#configure-it)
- [Read the output](#read-the-output)
- [Use it from an agent](#use-it-from-an-agent)
- [Supported versions](#supported-versions)
- [Network paths and certificates](#network-paths-and-certificates)
- [Keeping it working](#keeping-it-working)
- [What the tests prove](#what-the-tests-prove)

## What it reports

One row per Run:ai cluster and node pool the access key can see:

| Field | Meaning |
|---|---|
| `total_gpus`, `allocated_gpus`, `free_gpus` | The node pool's `TOTAL_GPUS`, `ALLOCATED_GPUS` and `FREE_GPUS` telemetry |
| `whole_gpus_upper_bound` | The whole GPUs a new workload could be given: the smaller of `free_gpus` and the whole GPUs left after `allocated_gpus`. Run:ai shares GPUs fractionally, so a 0.5-GPU allocation can leave `free_gpus` above what a whole-GPU request can use |
| `gpu_types` | Per GPU model on the pool's nodes: node count, ready nodes, GPUs, and `ec2_gpu_model` / `ec2_families`, the EC2 families that carry the same GPU model (from the [accelerator catalog's `gpu_model`](MAINTENANCE.md#sources-of-truth)) |
| `projects` | Per Run:ai project with quota on the pool: `deserved_gpus`, `limit_gpus`, `allocated_gpus` and `headroom_gpus` (deserved minus allocated; negative when the project runs over its quota) |
| `fits_gpu_count` | With `--gpu-count N`: whether `whole_gpus_upper_bound` is at least N |
| `project_headroom_gpus`, `within_project_quota` | With `--project`: that project's headroom, and with `--gpu-count` whether N fits inside it |
| `observed_at`, `valid_until` | When the telemetry was taken, and 90 seconds later |
| `source`, `submit_compatible` | Always `runai` and `false` |

```bash
gco capacity runai
gco capacity runai --gpu-count 4 --project team-a
gco --output json capacity runai --cluster prod --node-pool h100
```

The same numbers in the terminal, from `gco capacity runai --gpu-count 2`:

```text
  NVIDIA Run:ai capacity — https://runai.example.com (read-only; not ranked with AWS Regions)
  Observed 2026-11-09T12:00:00Z, valid until 2026-11-09T12:01:30Z
  ----------------------------------------------------------------------------------
  CLUSTER  NODE POOL  GPU TYPES                    TOTAL  ALLOC  FREE  WHOLE  FITS 2
  lab      h100       NVIDIA-H100-80GB-HBM3(H100)     16      0     8      8     yes
  prod     default    -                                ?      ?     ?      ?       ?
  prod     t4         Tesla-T4(T4)                     4    2.5     2      1      no

  note: project team-b: quotaStatus.allocated has no gpu value; read as 0
  note: prod/default: no TOTAL_GPUS value for this pool; reported as unknown, not 0
  note: prod/default: no ALLOCATED_GPUS value for this pool; reported as unknown, not 0
  note: prod/default: no FREE_GPUS value for this pool; reported as unknown, not 0

  WHOLE = whole GPUs a new workload could be given; ? = not reported (never 0).
```

## What it does not do

- **Submit or route work.** One token request and GET requests; nothing is
  created or changed in Run:ai, and no GCO job path calls the reader.
- **Rank Run:ai with AWS Regions.** A Run:ai row has no AWS Region,
  Availability Zone, price or reservation. `gco capacity recommend-region`
  ranks AWS Regions only.
- **Guess.** A value the control plane did not return is `null` (shown as `?`)
  with a note, never 0. A 403 stops the command with the reason rather than
  reporting no GPUs.

## Configure it

The reader takes its configuration from the environment only, so the client
secret never appears on a command line, in shell history, or in an MCP tool
argument:

| Variable | Required | Meaning |
|---|---|---|
| `GCO_RUNAI_URL` | yes | The control plane's origin, `https://host[:port]`: for a self-hosted control plane the Run:ai user interface URL, for SaaS `https://<tenant>.run.ai`. HTTPS only, no path |
| `GCO_RUNAI_CLIENT_ID` | yes | A [service account](https://run-ai-docs.nvidia.com/saas/infrastructure-setup/authentication/service-accounts) or user access key |
| `GCO_RUNAI_CLIENT_SECRET` | yes | Its secret |
| `GCO_RUNAI_CA_FILE` | no | A PEM bundle that signs the control plane's certificate, for a private CA. When set it is the only trust anchor |

The access key needs read access to clusters, nodes, node pools and projects
on every cluster it should report. Grant it through an
[access rule](https://run-ai-docs.nvidia.com/saas/infrastructure-setup/authentication/accessrules);
a cluster the key cannot read returns 403, which the reader reports as an
error naming the route.

The reader ignores `HTTPS_PROXY`, `REQUESTS_CA_BUNDLE` and `.netrc`, and never
follows a redirect, so the secret and the token only ever go to
`GCO_RUNAI_URL`.

## Read the output

- **Freshness.** Telemetry is a metrics snapshot that trails the scheduler, so
  a row is good until `valid_until`. Read again rather than reusing old output.
- **Unknown is not zero.** A pool the telemetry returns no value for shows
  `?` and a note.
- **Quota headroom.** The schema makes `quotaStatus.allocated.gpu` optional
  inside a present `allocated` object, so an object without the number reads
  as 0 (with a note), while a missing object stays unknown.
- **GPU models.** Run:ai reports a node's GPU product label (`Tesla-T4`,
  `NVIDIA-H100-80GB-HBM3`). The reader drops the vendor words and matches the
  longest EC2 GPU model the label begins with, so `NVIDIA-L40S` maps to `L40S`
  (`g6e`) and never to `L4`. A label with no EC2 counterpart has no
  `ec2_families`.

## Use it from an agent

The `runai_capacity` MCP tool runs the same command and returns its JSON. It
is off unless `GCO_ENABLE_RUNAI_CAPACITY=true` (or `GCO_ENABLE_ALL_TOOLS=true`)
is set on the MCP server, because it calls a non-AWS service with a secret
from the MCP host's environment. It takes `cluster`, `node_pool`, `gpu_count`
and `project`, and no URL or credential argument, so a prompt cannot redirect
the secret to another host.

## Supported versions

NVIDIA Run:ai **2.24 or later**. That is the first published API reference
with every route the reader uses: 2.23 has neither `POST /api/v2/token` nor
`GET /api/v1/org-unit/projects/node-pool-resources`, and 2.22 and older also
lack `GET /api/v1/nodes`. An older control plane answers 404 on one of them,
and the reader says it needs 2.24.

| Route | Used for |
|---|---|
| `POST /api/v2/token` | The OAuth 2.0 `client_credentials` exchange, form-encoded |
| `GET /api/v1/clusters` | Cluster names and versions |
| `GET /api/v1/node-pools` | Node pools, paged |
| `GET /api/v1/nodes` | GPU type and count per node, paged |
| `GET /api/v1/org-unit/projects/node-pool-resources` | Project quota per node pool, paged |
| `GET /api/v1/nodes/telemetry` | `TOTAL_GPUS`, `ALLOCATED_GPUS`, `FREE_GPUS` grouped by cluster and node pool |

The paged routes are read 500 items at a time (the API's maximum);
`--page-size` lowers it. A server that repeats a page instead of advancing
stops the read with an error rather than looping.

## Network paths and certificates

The reader runs wherever `gco` runs and talks to `GCO_RUNAI_URL` directly.
For an on-premises control plane that host needs a route to it (an
[AWS Site-to-Site VPN](https://docs.aws.amazon.com/vpn/latest/s2svpn/VPC_VPN.html)
or [AWS Direct Connect](https://docs.aws.amazon.com/directconnect/latest/UserGuide/Welcome.html)
connection, or the corporate network itself) and must resolve its host name.
A certificate signed by a corporate or private CA needs `GCO_RUNAI_CA_FILE`;
without it the command fails with TLS verification guidance instead of
connecting.

For a control plane only reachable inside a cluster, a `kubectl port-forward`
to its service plus a hosts-file entry mapping the certificate's host name to
`127.0.0.1` lets `GCO_RUNAI_URL` keep the name the certificate is issued for:

```bash
kubectl -n <control-plane-namespace> port-forward svc/<control-plane-service> 8443:443
echo "127.0.0.1 runai.example.internal" | sudo tee -a /etc/hosts
export GCO_RUNAI_URL=https://runai.example.internal:8443
export GCO_RUNAI_CA_FILE=$HOME/runai-ca.pem
```

## Keeping it working

The routes, parameters and fields the reader depends on are data:
`gco/config/runai_api_contract.json`. NVIDIA publishes the API reference per
version at <https://run-ai-docs.nvidia.com/api>, each page with its OpenAPI
document embedded, so the contract is checked without a Run:ai installation:

```bash
python3 .github/scripts/check_runai_api_contract.py
```

The monthly dependency scan runs it as the **NVIDIA Run:ai API** row of the
rolling dependency issue, against every published version from the
contract's `min_version` on, plus latest. Each row is one of:

| Finding | What to do |
|---|---|
| A route missing, or a parameter, value, request field or response field missing | The reader breaks on control planes at that version. Update `cli/capacity/runai.py` and the contract together; `tests/test_capacity_runai.py` checks that the contract declares exactly the routes the reader calls and that the test fixtures carry every declared field |
| A route marked deprecated | Move the reader to the replacement before NVIDIA removes it |
| `published; the contract holds` | A new version the reader already supports. Add it to `reviewed_versions` |

When the scan cannot read the reference, the row is reported as skipped,
never as current.

## What the tests prove

`tests/test_capacity_runai.py` runs the reader against a fake control plane
over real HTTPS with its own CA (`tests/runai_fake_control_plane.py`):
verification only through `GCO_RUNAI_CA_FILE`, the form-encoded token exchange,
bearer GETs, paging past 500 items and at page size 1, every error status,
redirects not followed, and redaction of the secret and token in logs. The
documents it serves are shaped by the public API reference, and the
contract's fields are asserted present in them. What the public reference does
not document, such as how quickly telemetry follows the scheduler, is handled
defensively (`valid_until`, `null` for unknowns) rather than proven: CI has no
live Run:ai control plane.
