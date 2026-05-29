# Security Policy

## Reporting a vulnerability

If you believe you've discovered a potential security issue in Global Capacity Orchestrator (GCO), **please do not open a public GitHub issue**. Instead, notify AWS Security directly:

- **Email:** [aws-security@amazon.com](mailto:aws-security@amazon.com)
- **More information:** [https://aws.amazon.com/security/vulnerability-reporting/](https://aws.amazon.com/security/vulnerability-reporting/)

Please include:

- A description of the issue
- Steps to reproduce, or a proof-of-concept if possible
- The impact you believe the issue has
- Any mitigations you've identified

You should receive a response within one business day. If you don't, please follow up to make sure AWS Security received your original report.

We appreciate responsible disclosure and will credit reporters who request it once a fix has shipped.

## Supported versions

Security fixes are published on the `main` branch and tagged as patch releases (`x.y.z`). Older `x.y` lines do not receive backports.

## Scope

In scope for coordinated disclosure:

- `gco/` — CDK stacks, services, and models
- `cli/` — the `gco` command-line tool
- `lambda/` — Lambda handler code
- Published container images produced by this repository
- CI/CD pipeline configurations (`.github/workflows/`)

Out of scope:

- Third-party dependencies we consume (report those to their maintainers; our CVE scans and Dependabot will pick up fixes once released)
- Demo recordings, example manifests, and documentation
- Vulnerabilities that require a compromised AWS account, compromised local developer machine, or physical access to the cluster

## Security posture summary

This repository runs the following security checks on every push to `main` and every pull request:

- **Static analysis**: Bandit, Semgrep, CodeQL (Advanced Setup — `security-and-quality` pack, config pinned at `.github/codeql/codeql-config.yml`)
- **Dependency scanning**: pip-audit, Trivy (filesystem + each published container image), Dependabot
- **IaC scanning**: Checkov, KICS, cdk-nag (AWS Solutions + HIPAA + NIST 800-53 + PCI DSS + Serverless packs)
- **Secret scanning**: Gitleaks, TruffleHog
- **Weekly CVE re-scan**: Trivy against the latest vulnerability databases

See [`.github/CI.md`](CI.md) for workflow-level detail.
