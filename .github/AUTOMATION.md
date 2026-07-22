# GitHub Project Automation

This directory contains the repository's GitHub governance, CI/CD workflows,
security configuration, issue templates, and reusable automation.

> **Note:** this file is deliberately *not* named `README.md`. GitHub renders
> `.github/README.md` as the repository front page in preference to the root
> `README.md`, which would hide the project README.

## Table of Contents

- [Directory Map](#directory-map)
- [CI and Security](#ci-and-security)
- [Governance](#governance)
- [Maintenance](#maintenance)

## Directory Map

| Path | Purpose |
|---|---|
| [`actions/`](actions/README.md) | Repository-local composite actions shared by workflows |
| [`codeql/`](codeql/README.md) | CodeQL query and path configuration |
| [`config/`](config/README.md) | Shared linter and security-scanner configuration |
| [`ISSUE_TEMPLATE/`](ISSUE_TEMPLATE/README.md) | Bug, feature, and support intake templates |
| [`kind/`](kind/README.md) | Local Kubernetes-in-Docker CI configuration |
| [`legacy/`](legacy/README.md) | Inactive historical CI material |
| [`oidc_provider/`](oidc_provider/README.md) | Optional GitHub OIDC bootstrap stack |
| [`scripts/`](scripts/README.md) | CI validation and security helper scripts |
| [`workflows/`](workflows/README.md) | GitHub Actions workflow definitions |

## CI and Security

[`CI.md`](CI.md) documents the workflow topology and required checks. Security
policy and disclosure guidance live in [`SECURITY.md`](SECURITY.md). Workflow
permissions should remain least-privilege, third-party actions should remain
pinned, and scanner exceptions must carry a reviewable rationale.

## Governance

- [`CODEOWNERS`](CODEOWNERS) defines required reviewers.
- [`pull_request_template.md`](pull_request_template.md) defines PR evidence.
- [`release.yml`](release.yml) controls generated release-note categories.
- [`dependabot.yml`](dependabot.yml) configures dependency update proposals.

## Maintenance

Keep reusable behavior in `actions/` or `scripts/` rather than duplicating it
across workflows. Update the nearest README whenever a workflow, action input,
scanner configuration, or operator procedure changes.
