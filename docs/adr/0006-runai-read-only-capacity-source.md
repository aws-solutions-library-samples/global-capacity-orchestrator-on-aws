# 0006. NVIDIA Run:ai as a read-only capacity source

- **Status:** Accepted
- **Date:** 2026-11-10
- **Deciders:** GCO maintainers
- **Supersedes:** none
- **Superseded by:** none

## Context

GCO finds accelerator capacity across AWS Regions: Spot placement scores and
prices, On-Demand Capacity Reservations, Capacity Blocks and Training Plans,
ranked by `gco capacity recommend-region` over the `eks`, `hyperpod` and `pcs`
backends. Many teams that run GPU work on AWS also run
[NVIDIA Run:ai](https://run-ai-docs.nvidia.com/) clusters, on premises or on
Amazon EKS, whose GPUs are scheduled by the Run:ai scheduler. GCO has no view
of that capacity, so an operator deciding where work should go compares two
tools by hand.

The [North Star](../../TENETS.md#north-star) scopes GCO to "the capacity AWS
actually has". A Run:ai cluster's GPUs may not be in AWS at all, and the
Run:ai scheduler, not GCO, decides what runs on them. The forces:

- **Truth** ([tenet 2](../../TENETS.md#2-tell-the-truth-about-state-and-capacity)):
  Run:ai capacity arrives as telemetry (a metrics snapshot), with no AWS
  Region, Availability Zone, price or reservation. Mixing it into the Region
  ranking would score capacity GCO cannot place work on, on signals it does
  not have.
- **One scheduler per GPU**: GCO placing work onto Run:ai GPUs would make it a
  second scheduler for capacity Run:ai already arbitrates (quotas,
  over-quota borrowing, preemption).
- **Security** ([tenet 3](../../TENETS.md#3-secure-every-boundary-by-default)):
  reading Run:ai needs a client secret for a non-AWS service, and the MCP
  server lets agents call tools.
- **Maintenance** ([tenet 10](../../TENETS.md#10-leave-the-project-easier-to-understand-and-maintain)):
  the Run:ai API is versioned by NVIDIA, not by AWS, and GCO has no Run:ai
  installation to test against.

## Decision

We will add NVIDIA Run:ai as an observation-only capacity source:
`gco capacity runai` and the `runai_capacity` MCP tool read it, and nothing
else in GCO does.

- **Read-only.** One token exchange (`POST /api/v2/token`) and GET requests on
  the routes declared in `gco/config/runai_api_contract.json`. GCO creates,
  changes and submits nothing in Run:ai.
- **Outside the Region ranking.** Every row says `source: runai` and
  `submit_compatible: false`. `recommend-region`, the capacity scoring and
  every job path (`jobs submit*`, `slurm submit`, the capacity poller) never
  import the reader. `tests/test_capacity_runai.py` enforces the import
  boundary, and `BACKENDS` has no `runai` entry.
- **Truthful rows.** Each row carries `observed_at` and `valid_until`. A value
  Run:ai did not report is `null` with a note, never 0. A 403 is an error,
  never "0 free GPUs". `whole_gpus_upper_bound` accounts for fractional GPU
  allocations. A GPU model is matched to EC2 families only through the
  accelerator catalog's reviewed `gpu_model`, and only as an annotation.
- **Credentials from the environment only.** `GCO_RUNAI_URL`,
  `GCO_RUNAI_CLIENT_ID`, `GCO_RUNAI_CLIENT_SECRET` and the optional
  `GCO_RUNAI_CA_FILE`. The CLI has no flag for the secret and the MCP tool has
  no URL or credential argument, so an agent cannot send the secret to another
  host. The reader does not follow redirects, ignores proxy and CA variables
  from the environment, and requires HTTPS. The MCP tool is off unless
  `GCO_ENABLE_RUNAI_CAPACITY` is set.
- **A checked contract.** The routes and fields the reader depends on are
  data, and the monthly dependency scan checks them against every NVIDIA
  Run:ai API reference version from 2.24 on, plus latest
  (`.github/scripts/check_runai_api_contract.py`). The supported floor is
  2.24, the first published version with every route.

Out of scope: submitting or routing work to Run:ai, ranking Run:ai capacity
with AWS Regions, storing Run:ai credentials in the deployed control plane,
and polling Run:ai from the cloud.

## Consequences

### Positive

- An operator sees Run:ai and AWS capacity from one CLI and one MCP server,
  each with its own provenance, without GCO taking scheduling authority over
  Run:ai GPUs.
- NVIDIA API changes surface in the monthly dependency issue before a
  customer's control plane is upgraded, without a Run:ai installation in CI.
- The accelerator catalog's `gpu_model` is reusable for any other source that
  names GPUs by model rather than instance type.

### Negative

- CI proves the reader against a fake control plane shaped by the public API
  reference, not against a live Run:ai. Behaviour the reference does not
  document (how fresh telemetry is, what an empty quota object means) rests on
  defensive handling, and the docs say so.
- The reader runs wherever `gco` runs and needs a network path to the control
  plane (for an on-premises one, a VPN or AWS Direct Connect route from that
  host). GCO adds no networking for it.
- One more environment-configured integration to document and keep current.

### Neutral

- The token is exchanged once per invocation and never cached, so token
  lifetime and refresh do not apply.

## Alternatives considered

### Option B — a Run:ai backend in `recommend-region`

- **Summary:** score Run:ai node pools alongside AWS Regions.
- **Why not:** Run:ai rows have no Region, price, Spot score or reservation to
  score on, and recommending a pool implies GCO can place work there, which
  needs submission to Run:ai and makes GCO a second scheduler.

### Option C — a deployed poller

- **Summary:** a Lambda or in-cluster service polls Run:ai and serves the
  numbers through the GCO API.
- **Why not:** it puts a third-party secret and a network route to customer
  infrastructure into every deployment, for numbers that expire in seconds.
  The CLI read keeps the secret and the network path with the operator. Revisit
  if operators ask for Run:ai rows in the API itself.

## References

- [docs/RUNAI.md](../RUNAI.md): configuration, output fields, and the
  maintenance loop
- [NVIDIA Run:ai REST API reference](https://run-ai-docs.nvidia.com/api)
- `gco/config/runai_api_contract.json`,
  `.github/scripts/check_runai_api_contract.py`, `cli/capacity/runai.py`
