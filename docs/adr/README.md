# Architecture Decision Records

This directory holds the Architecture Decision Records (ADRs) for GCO (Global
Capacity Orchestrator on AWS): an append-only log of the significant
architectural decisions behind the project. Each record captures the context
that forced a choice, the decision itself, and the consequences we accept by
making it.

The format is the lightweight one popularized by Michael Nygard.
[ADR-0001](0001-record-architecture-decisions.md) records the decision to adopt
ADRs and explains the full rationale.

## Table of Contents

- [Why ADRs](#why-adrs)
- [When to write one](#when-to-write-one)
- [How to add an ADR](#how-to-add-an-adr)
- [Status lifecycle](#status-lifecycle)
- [Discovering ADRs through the MCP server](#discovering-adrs-through-the-mcp-server)
- [Index](#index)

## Why ADRs

GCO documents *what* the system is (see [ARCHITECTURE.md](../ARCHITECTURE.md))
but the *why* behind non-obvious choices used to live only in pull request
history, release notes, and inline comments. ADRs give that rationale a durable
home next to the code, so reviewers and future maintainers can recover intent
without archaeology.

## When to write one

Write an ADR when a decision is architecturally significant — expensive to
reverse, or shaping of the system in a way future contributors will need to
understand. Rules of thumb:

- It changes a public contract, a security boundary, or a cross-cutting
  convention.
- It picks one option among several with real trade-offs (a scheduler, a
  storage layer, a dependency-pinning policy).
- You would otherwise capture the "why" only in a pull request comment that
  will be hard to find later.

You do not need an ADR for reversible, local choices — a variable name, a
behaviour-preserving refactor, or a routine bug fix. When in doubt, prefer a
short ADR over losing the rationale.

## How to add an ADR

1. Copy `template.md` to `NNNN-title-in-kebab-case.md`, using the next unused
   four-digit number (zero-padded) and a short, present-tense title.
2. Fill in the sections. Keep it concise — one decision per record.
3. Set the status: usually `Proposed` while under review, then `Accepted` when
   the pull request merges. Add the date and the deciders.
4. If this decision replaces an earlier one, set the older ADR's status to
   `Superseded by NNNN` and fill in this record's `Supersedes` field. Do not
   edit the substance of the superseded record.
5. Add a row to the [Index](#index) below.
6. Open a pull request as usual.

The MCP `docs://gco/adr/index` resource derives its listing from the files on
disk, so it reflects the new ADR with no extra step.

## Status lifecycle

- **Proposed** — under discussion; not yet agreed.
- **Accepted** — agreed and in effect. The normal state of a merged ADR.
- **Rejected** — considered and declined; kept for the record.
- **Deprecated** — no longer relevant, but not replaced by a specific decision.
- **Superseded by NNNN** — replaced by a later ADR, referenced by number.

## Discovering ADRs through the MCP server

The in-tree [MCP server](../../gco_mcp/) exposes ADRs to agents so that "why is
it built this way?" questions can be answered from the decision log:

- `docs://gco/adr/index` — every ADR with its id, title, and status, generated
  from the files in this directory.
- `docs://gco/adr/{id}` — a single record by four-digit id (for example,
  `0001`), by full filename stem, or the `README` / `template` guides.

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-cluster-observability-on-eks-auto-mode.md) | Cluster observability on EKS Auto Mode | Proposed |
