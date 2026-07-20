# GCO Tenets

> **Status:** Normative project guidance for Global Capacity Orchestrator (GCO).
> These tenets govern product, architecture, implementation, operations, and
> maintenance decisions.
>
> **Priority rule:** The tenets are ordered. When two tenets genuinely conflict,
> the earlier tenet takes precedence. A deliberate exception must be narrow,
> reviewable, reversible where possible, and recorded in an
> [Architecture Decision Record](docs/adr/README.md).

## Table of Contents

- [North Star](#north-star)
- [How to Use These Tenets](#how-to-use-these-tenets)
- [Prioritized Tenets](#prioritized-tenets)
  - [1. Protect Workloads, Data, and Accounts](#1-protect-workloads-data-and-accounts)
  - [2. Tell the Truth About State and Capacity](#2-tell-the-truth-about-state-and-capacity)
  - [3. Secure Every Boundary by Default](#3-secure-every-boundary-by-default)
  - [4. Present One Coherent Experience Without Hiding Regional Reality](#4-present-one-coherent-experience-without-hiding-regional-reality)
  - [5. Treat Accelerator Capacity as Dynamic, Scarce, and Policy-Governed](#5-treat-accelerator-capacity-as-dynamic-scarce-and-policy-governed)
  - [6. Automate Mechanics; Keep Policy Reviewable](#6-automate-mechanics-keep-policy-reviewable)
  - [7. Design for Failure, Recovery, and Reversibility](#7-design-for-failure-recovery-and-reversibility)
  - [8. Make Operations Observable and Actionable](#8-make-operations-observable-and-actionable)
  - [9. Optimize for Useful Work, Not Infrastructure Activity](#9-optimize-for-useful-work-not-infrastructure-activity)
  - [10. Leave the Project Easier to Understand and Maintain](#10-leave-the-project-easier-to-understand-and-maintain)
- [Decision Framework](#decision-framework)
- [Definition of Done](#definition-of-done)
- [Stewardship](#stewardship)

## North Star

**One API. Every Accelerator. Any Region.**

GCO exists so a workload owner can express intent once and use accelerated
compute across AWS Regions without becoming an expert in every regional cluster,
capacity signal, or infrastructure lifecycle. The platform should make the safe
path easy while preserving the operator's ability to see, understand, and
control what actually happens.

The promise is intentionally ambitious, but each word has a precise boundary:

- **One API** means one coherent contract and operating model. It does not mean
  one opaque control plane that erases regional identity, failure domains, or
  authorization boundaries.
- **Every Accelerator** means the project continuously discovers, classifies,
  observes, and reasons about the accelerator catalog. It does not mean every
  device is automatically approved for new scheduling. Lifecycle, architecture,
  workload fit, networking, availability, and support policy still govern use.
- **Any Region** means the design is partition-aware and avoids arbitrary
  Region allowlists. It does not promise that every AWS service, accelerator,
  quota, or unit of capacity exists in every Region at every moment.

The north star is therefore not “hide complexity at any cost.” It is to absorb
repetitive complexity, expose material constraints, and help users place useful
work safely across the capacity AWS actually has.

## How to Use These Tenets

Use the tenets before implementation, during review, and when operating the
system under stress. They are a decision tool, not inspirational decoration and
not a substitute for evidence.

1. Start with the earliest tenet affected by a decision.
2. State the user or operator outcome, the relevant failure modes, and what is
   known versus assumed.
3. Compare alternatives in priority order. An option that violates an earlier
   tenet is not rescued merely because it performs well against a later one.
4. Prefer an implementation whose invariants can be checked automatically and
   whose policy choices remain visible to reviewers.
5. Record a durable ADR when a choice is expensive to reverse, changes a trust
   boundary, creates a long-lived exception, or resolves real tension between
   tenets.

“Follow the tenets” is not sufficient justification by itself. Reviews should
name the affected tenet, the evidence supporting the choice, and the operational
consequences. If an exception is temporary, record its owner, scope, expiration
or review trigger, and safe exit path.

## Prioritized Tenets

### 1. Protect Workloads, Data, and Accounts

A convenient orchestration path is never worth destroying the wrong resource,
losing user data, leaking authority, or disrupting unrelated workloads.

- Destructive operations must prove exact ownership from durable evidence.
  Names, prefixes, ambient credentials, and “looks like ours” heuristics are not
  sufficient authorization.
- Cleanup must be narrowly scoped, idempotent, and fail closed when identity is
  incomplete or contradictory. Preserving an uncertain resource is safer than
  deleting an unrelated one.
- Data retention and deletion behavior must be explicit. Stateful resources,
  model weights, job outputs, checkpoints, and audit evidence require lifecycle
  choices rather than incidental defaults.
- Production-affecting changes should be staged, bounded, and observable. Live
  validation requires explicit authorization, a known account and Region,
  documented cleanup authority, and a recovery path.
- Concurrency, retries, cancellation, and partial progress must not weaken
  ownership checks or expand the operation's authority.

When safety and speed conflict, choose safety and explain the delay. When safety
cannot be established, stop and ask rather than guessing.

### 2. Tell the Truth About State and Capacity

Operators must be able to trust what GCO says. A precise unknown is better than
a confident fiction.

- Distinguish desired state, submitted work, observed live state, cached state,
  inferred state, and unavailable evidence. Never silently substitute one for
  another.
- Success means the requested outcome was verified at the relevant boundary,
  not merely that a command exited zero or an API accepted a request.
- Capacity is a time- and Region-bound signal, not an inventory guarantee.
  Placement scores, prices, offerings, reservations, quotas, and historical
  observations must retain their scope and freshness.
- Aggregation must preserve partial failures. A global response should identify
  which Regions answered, which did not, and whether the result is complete.
- Skipped, degraded, stale, or credential-limited checks must be visible. They
  must not be rendered as “current,” “healthy,” or “no drift.”
- Tests and documentation must state what they prove and what remains outside
  their evidence, especially at mocked, synthesized, and live-AWS boundaries.

Truthful systems make recovery possible. False success spends operator trust,
and that trust is harder to restore than infrastructure.

### 3. Secure Every Boundary by Default

GCO crosses user, agent, API, account, Region, VPC, cluster, namespace, storage,
and third-party supply-chain boundaries. Each boundary must authenticate,
authorize, constrain, and audit the transition it permits.

- Use short-lived identities, least-privilege IAM, scoped Kubernetes RBAC, and
  explicit feature gates. Do not make long-lived credentials the normal path.
- Keep public exposure minimal. Prefer private endpoints and authenticated
  bridges; require TLS and verify the intended peer identity.
- Treat SigV4, TLS, request-bound HMAC, network policy, and encryption as
  complementary controls with different purposes. Do not describe one as a
  substitute for another.
- Validate all untrusted input, including manifests, paths, image references,
  model output, external catalogs, workflow data, and subprocess arguments.
- Keep secrets out of logs, reports, prompts, checkpoints, fixtures, and source
  control. Operational identifiers may be observable; reusable credentials may
  not.
- Default to no destructive, cost-incurring, host-filesystem, or autonomous
  authority. Operators opt in to the smallest capability needed.
- Review dependencies, images, IAM changes, and suppressions as changes to the
  trust model, not routine text edits.

Security controls should be usable and testable. A control that operators must
routinely bypass is a design problem to fix, not a habit to normalize.

### 4. Present One Coherent Experience Without Hiding Regional Reality

GCO should feel like one platform while remaining honest about the independent
systems underneath it.

- Keep commands, API shapes, resource models, and error semantics consistent
  across Regions and AWS partitions.
- Preserve Region in live-resource identity, authorization, status, logs, and
  failure reports. Never rely on an ambient kubectl context for a regional read
  or mutation.
- Global aggregation and routing are coordination mechanisms, not proof of
  compute capacity. Network health cannot answer whether a requested
  accelerator can be provisioned.
- Isolate regional failure. One unavailable Region should not corrupt another
  Region's state or erase useful partial results.
- Respect partition and service differences explicitly. Degrade to a documented
  regional path where a global AWS service is unavailable rather than claiming
  unsupported uniformity.
- Avoid both extremes: do not expose every infrastructure detail to every user,
  and do not flatten away constraints that change placement, security, cost, or
  recovery decisions.

The coherent experience is a stable contract over visible regional truth, not a
promise that all Regions are identical.

### 5. Treat Accelerator Capacity as Dynamic, Scarce, and Policy-Governed

Accelerators evolve quickly, are unevenly distributed, and often become scarce
exactly when workloads need them. GCO must keep discovery current without
turning mutable cloud inventory into unreviewed scheduling policy.

- Maintain a normalized, checked-in accelerator catalog as the reviewable
  baseline for NVIDIA GPU and AWS Neuron instance types the project observes.
- Compare that baseline with the live EC2 catalog on a regular cadence. Report
  additions, removals, and metadata changes with enough detail for a maintainer
  to act.
- Separate **observation eligibility** from **new scheduling eligibility**. A
  deprecated family can remain in historical monitoring while being prohibited
  from new NodePools.
- Treat lifecycle, generation, architecture, EFA capability, workload fit, and
  replacement families as reviewed policy. A new EC2 type does not silently
  enter a NodePool because an API returned it.
- Reject end-of-life or deprecated scheduling references with the exact
  manifest, family, and recommended replacement. Surface newer unreferenced
  generations as actionable advisory drift to the NodePools that should be
  reviewed.
- Keep the capacity-history watch list complete for every cataloged type so the
  project does not lose visibility while policy review is pending.
- Never equate catalog presence with regional availability, quota, price,
  reservation access, or an assurance that EKS can launch the type now.

“Every Accelerator” is a commitment to complete awareness and deliberate
support, not automatic endorsement.

### 6. Automate Mechanics; Keep Policy Reviewable

Automation should remove toil and catch drift, while consequential choices stay
legible to people accountable for them.

- Automate discovery, normalization, synchronization checks, schema validation,
  deterministic comparison, reporting, and repetitive remediation steps.
- Keep normal CI offline and reproducible. Mutable online discovery belongs in
  clearly identified scheduled or operator-invoked checks whose results become
  reviewable input.
- Make generated or synchronized data traceable to one source of truth. Guard
  every required copy so partial updates fail with an exact correction.
- Do not auto-merge lifecycle decisions, trust-boundary changes, replacement
  policy, architecture support, or scheduling eligibility merely because they
  can be inferred from a vendor catalog.
- Prefer idempotent tools with machine-readable output, bounded retries, stable
  ordering, and deterministic tests.
- Route routine drift through one durable reporting mechanism with clear
  ownership. A scheduled finding should create work, not noise or an endless
  stream of duplicate issues.
- Make automation failure distinguishable from real drift. A parser error,
  throttled API, or missing credential is an operational finding, not a catalog
  change and not a clean bill of health.

The ideal maintenance loop is: machines collect and compare; humans review the
policy delta; machines verify and apply the approved result.

### 7. Design for Failure, Recovery, and Reversibility

Distributed systems fail between steps. Deployments are interrupted, APIs
throttle, credentials expire, processes disappear, and cleanup runs after the
original context has been lost. These are normal design inputs.

- Make operations resumable and idempotent. Persist enough non-secret evidence
  to determine what completed, what remains, and what authority still applies.
- Bound retries by error class, time, and rate limits. Use backoff and jitter
  where appropriate; do not turn a regional outage into synchronized load.
- Define cancellation semantics. Long-running work must expose progress and
  leave an honest terminal state when the controlling client disconnects.
- Prefer reversible changes and staged rollout. Where rollback is unsafe or
  impossible, make that constraint explicit before execution and provide a
  forward-recovery plan.
- Treat deploy and destroy as equally important product paths. Retained
  resources, asynchronous controllers, and service-managed dependencies need
  intentional teardown behavior.
- Preserve unrelated and uncertain resources during recovery. Broad cleanup is
  not an acceptable substitute for missing ownership evidence.
- Exercise failure paths in deterministic tests, then use authorized live
  validation only for boundaries offline evidence cannot establish.

A recovery procedure that exists only in the memory of the original author is
not a recovery procedure.

### 8. Make Operations Observable and Actionable

Observability is the interface between a running system and the people
responsible for it. Signals should answer what happened, where, why it matters,
and what to do next.

- Emit structured, bounded logs and metrics with stable workload, project,
  Region, and request correlation where those dimensions are safe to expose.
- Report progress for long operations and preserve an independent source of
  truth, such as CloudFormation or durable task status, when a client session
  can disappear.
- Every alert should map to an owner and a runbook. An alarm without a delivery
  path or action is configuration, not operational readiness.
- Errors and drift findings must name the affected resource or file, the failed
  invariant, and a recommended next step. Prefer commands and exact locations
  over generic “update required” messages.
- Show skipped and degraded checks in summaries. Keep urgency proportional to
  impact so routine maintenance does not drown out workload or security risk.
- Bound retention, cardinality, payload size, and polling cost. Observability
  that can exhaust the system it watches is not safe.
- Measure the user-visible outcome in addition to infrastructure activity:
  workload acceptance, placement, readiness, completion, and recovery.

The goal is not maximum telemetry. It is the minimum complete evidence needed
to understand and act confidently.

### 9. Optimize for Useful Work, Not Infrastructure Activity

Clusters, APIs, queues, dashboards, and automation are means. The outcome is a
workload placed on suitable capacity, run safely, and completed with usable
results at an understood cost.

- Judge features by time-to-useful-work, successful workload completion,
  recovery behavior, and operator effort—not by the number of resources
  created or services integrated.
- Scale platform and workload components to demand where doing so preserves
  reliability. Avoid idle accelerator capacity and unnecessary always-on
  infrastructure.
- Make cost and capacity trade-offs visible. Spot, On-Demand, reservations, and
  Capacity Blocks serve different risk profiles; do not choose one invisibly
  for every workload.
- Prefer simple, composable control paths over feature breadth that increases
  failure modes without a clear user outcome.
- Keep advisory intelligence advisory. Deterministic policy, user intent, and
  verified state govern the final action.
- Include operational cost in design reviews: API volume, polling cadence,
  storage retention, data transfer, logging, and human response time.

The cheapest idle platform is still waste, and the busiest control plane can
still fail to deliver a single useful GPU-hour.

### 10. Leave the Project Easier to Understand and Maintain

Every change either compounds or reduces the cost of the next change. We choose
to leave clear sources of truth, enforceable invariants, and durable reasoning.

- Keep behavior, tests, documentation, examples, and MCP discovery aligned.
  User-facing changes are incomplete while any of those surfaces tells a
  different story.
- Minimize independent lists and copied constants. When duplication is required
  by a boundary, name the authority and add a guard that proves synchronization.
- Put rationale near the decision: focused comments for local constraints,
  maintenance guides for recurring work, runbooks for incidents, and ADRs for
  durable architectural choices.
- Prefer clear names, typed interfaces, small modules, and actionable failures
  over cleverness that requires tribal knowledge.
- Remove obsolete paths and stale claims instead of layering another exception
  on top. Preserve compatibility deliberately, not accidentally.
- Make onboarding possible from the repository. A maintainer should be able to
  discover how to validate, operate, recover, and extend a feature without
  private context.
- Improve the guardrail when a bug reveals a missing invariant. Fixing one
  occurrence without preventing the drift from returning leaves the work half
  done.

Maintainability is not polish applied after delivery. It is how GCO remains
safe and truthful as AWS, Kubernetes, accelerators, and the project evolve.

## Decision Framework

For a meaningful design or operational choice, evaluate the options in this
order. A “no” at an earlier step is a reason to reject or redesign an option
before optimizing later properties.

1. **Protection:** Can it affect workloads, data, accounts, or unrelated
   resources? Is exact authority established and bounded?
2. **Truth:** What evidence will prove the result? How are unknown, stale,
   partial, and skipped states represented?
3. **Security:** Which trust boundaries change? Are identity, authorization,
   input validation, confidentiality, and auditability least-privilege by
   default?
4. **Coherence:** Does it preserve one stable user contract while retaining the
   Region, partition, and failure-domain facts needed to operate safely?
5. **Capacity policy:** Does it distinguish mutable accelerator discovery from
   reviewed scheduling support and real-time capacity?
6. **Automation:** Which mechanical work can be deterministic? Which policy
   decisions need review, and where is that review recorded?
7. **Recovery:** What happens at every partial-failure and cancellation point?
   Can the change be resumed, reversed, or recovered forward?
8. **Operations:** What signals, runbook, ownership, and actionable diagnostics
   will exist in production?
9. **Useful work:** How does it improve workload outcomes, operator effort,
   latency, reliability, or cost?
10. **Maintenance:** Does it reduce synchronization surfaces, document the why,
    and leave an enforceable invariant for the next maintainer?

Record the evidence used, not only the selected option. If the best available
choice knowingly compromises a tenet, write an ADR that names the higher-priority
constraint, alternatives rejected, blast radius, compensating controls, and
revisit condition.

## Definition of Done

A change is done when the relevant claims below are demonstrated, not merely
when implementation code exists:

- The user or operator outcome is explicit, and acceptance is verified at the
  boundary where that outcome occurs.
- Workload, data, account, cleanup, and rollback impacts are understood; unsafe
  or destructive paths have exact ownership and confirmation controls.
- Desired, observed, inferred, stale, skipped, and failed states remain
  distinguishable in behavior and reporting.
- Trust boundaries, permissions, secrets, inputs, dependencies, and defaults
  have been reviewed for least privilege and fail-closed behavior.
- Region and partition behavior is explicit, including partial regional failure
  and unsupported-service behavior.
- Accelerator changes update the authoritative catalog or reviewed scheduling
  policy as appropriate, with complete observation coverage and actionable
  lifecycle guidance.
- Deterministic tests cover the invariant and failure paths. The relevant CI
  checks pass; any claim that still requires authorized live validation is
  named rather than implied.
- Operational status, logs, metrics, alarms, runbooks, cost effects, and
  cancellation/recovery behavior are updated where the runtime changes.
- Documentation, examples, maintenance guidance, MCP registration, and sources
  of truth agree with the shipped behavior.
- Any durable exception or expensive-to-reverse decision has an ADR, owner, and
  revisit trigger.

Not every bullet applies equally to every typo or refactor. Reviewers should use
judgment, but “not applicable” must be genuine—not a way to avoid evidence for a
changed boundary.

## Stewardship

These tenets belong to the project, not to a particular implementation or
maintainer. They should evolve when experience exposes a better principle, but
changes to them deserve the same care as changes to a public API.

- Propose substantive amendments in a focused pull request with rationale and
  concrete examples of decisions the new wording would change.
- Record priority changes, removals, and long-lived exceptions in an ADR.
- Keep the tenets concise enough to use, specific enough to resolve trade-offs,
  and aligned with the actual controls in code, CI, and operations.
- Revisit them after serious incidents, destructive near misses, recurring
  maintenance failures, major platform shifts, or evidence that the stated
  north star no longer serves workload owners.
- Do not weaken an earlier tenet merely to make a current implementation appear
  compliant. Improve the implementation or document the bounded exception.

For implementation practice, continue with [CONTRIBUTING.md](CONTRIBUTING.md).
For recurring upkeep, use the [Maintenance Guide](docs/MAINTENANCE.md). For
live-AWS evidence and cleanup controls, follow the
[Live Release Validation Guide](docs/LIVE_RELEASE_VALIDATION.md).
