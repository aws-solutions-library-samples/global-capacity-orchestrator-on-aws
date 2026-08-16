# Contributing

Contributions are welcome — and the project makes its expectations explicit
rather than tribal.
[CONTRIBUTING.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/CONTRIBUTING.md)
is the authoritative guide; this page routes you to the right parts of it.

## The shape of a good change

- **Keep PRs focused and reasonably sized**, with clear descriptions, tests,
  and documentation updates — the review guidelines apply the prioritized
  [project tenets](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/TENETS.md),
  starting with the earliest tenet a change affects.
- **Run the pre-PR verification sequence** before pushing: formatting, lint,
  types, accelerator-catalog validation, and the test suite with its
  coverage floor. The exact commands live in
  [CONTRIBUTING.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/CONTRIBUTING.md).
- **Significant decisions get an ADR** — when a choice is expensive to
  reverse, changes a trust boundary, or resolves a real tenet conflict, it
  is recorded in the append-only
  [Architecture Decision Records](https://github.com/awslabs/global-capacity-orchestrator-on-aws/tree/main/docs/adr).
- **Some changes need live validation** — deployed-infrastructure changes
  may require the local deploy-test-destroy harness, run only with explicit
  authorization; CONTRIBUTING.md's applicability table says when.

## Reporting bugs and requesting features

Use the structured
[issue templates](https://github.com/awslabs/global-capacity-orchestrator-on-aws/tree/main/.github/ISSUE_TEMPLATE):
a bug report with environment and reproduction steps, and a feature request
with problem/solution framing. Support questions are routed to the
troubleshooting and quick-start docs first.

## Running your own copy

Forking is a designed-for path, not an afterthought: *"GCO is designed to be
taken and run with."*
[docs/FORKING.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/docs/FORKING.md)
documents the supported migration — a script repoints badges, clone URLs,
the Pages site (this wiki and the embedded coverage report), and the OIDC
trust-policy subject to your fork, dry-run by default and idempotent, plus
the manual follow-ups it cannot decide for you.

## Community standards

The project ships a
[Code of Conduct](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/CODE_OF_CONDUCT.md)
and records its debts of gratitude in
[ACKNOWLEDGMENTS.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/ACKNOWLEDGMENTS.md).
Releases follow semantic versioning and are cut from the Actions tab — the
process is described at the end of
[CONTRIBUTING.md](https://github.com/awslabs/global-capacity-orchestrator-on-aws/blob/main/CONTRIBUTING.md).
