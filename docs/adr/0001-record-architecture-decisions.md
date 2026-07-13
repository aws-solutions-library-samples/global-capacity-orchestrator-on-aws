# 0001. Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-07-13
- **Deciders:** GCO maintainers
- **Supersedes:** none
- **Superseded by:** none

## Context

GCO carries a large number of deliberate architectural decisions — the
multi-region active topology fronted by Global Accelerator, EKS Auto Mode,
exact-pinned dependencies with a committed lockfile, the fail-closed
authentication middleware, and many more. Today the *what* is documented well
(`../ARCHITECTURE.md`, the package READMEs, the workflow headers) but the *why*
is scattered. It lives in pull request history, the categorized GitHub Release
notes, and unusually thorough inline comments such as `../../.github/CI.md` and
the justification strings in `../../gco/stacks/nag_suppressions.py`. As the
maintainer guide itself notes, there has been no single place a contributor can
read to understand why a non-obvious choice was made.

That scattering has a cost. Rationale decays: a reviewer approves a change
whose motivation is only in a PR comment, and a year later nobody can say
whether a constraint is still load-bearing or safe to drop. New contributors
re-litigate settled questions because the trade-offs were never written down in
a durable, discoverable place.

## Decision

We will record architecturally significant decisions as Architecture Decision
Records (ADRs), following the lightweight format popularized by Michael Nygard.

- ADRs live in `docs/adr/` as Markdown files named
  `NNNN-title-in-kebab-case.md`, numbered sequentially from `0001` (this
  record).
- Each ADR starts from the shared `template.md` and captures the context, the
  decision, and its consequences.
- ADRs are immutable once accepted. We do not rewrite a decision's history:
  when a decision changes we add a new ADR and mark the old one `Superseded by`
  the new number, preserving an append-only log.
- The catalog is directory-driven. The index in `README.md` and the MCP
  `docs://gco/adr/index` resource both derive their listing from the files on
  disk, so recording a decision does not require touching a separate registry.

The authoring process and the guidance on when a decision is significant enough
to record live in `README.md`.

## Consequences

### Positive

- The reasoning behind a decision is durable and lives next to the code, so
  reviewers and future maintainers can recover intent without archaeology.
- Onboarding improves — a new contributor can read the decision log in order.
- A change of direction becomes explicit through a superseding ADR rather than
  a silent edit.

### Negative

- Authoring an ADR is a small, deliberate cost on the decisions that warrant
  one.
- The team must exercise judgment about what is "significant" enough to record;
  `README.md` gives rules of thumb but cannot remove the judgment call.

### Neutral

- ADRs complement, and do not replace, `../ARCHITECTURE.md` (which describes the
  current state) or the GitHub Release notes (which remain the changelog).

## References

- Michael Nygard, [Documenting Architecture Decisions](https://www.cognitect.com/blog/2011/11/15/documenting-architecture-decisions.html) (2011)
- [adr.github.io](https://adr.github.io/) — overview of ADRs and community templates
- `README.md` — the GCO ADR process and index
- `template.md` — the template used for new records
