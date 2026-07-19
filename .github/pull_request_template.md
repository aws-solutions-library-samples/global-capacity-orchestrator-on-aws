<!--
Thanks for contributing to Global Capacity Orchestrator (GCO)! Please fill out the sections below.
Delete any sections that don't apply.
-->

## Summary

<!-- Brief description of what this PR does and why -->

## Type of change

<!-- Check all that apply. The leading token (feat:, fix:, etc.) is used by
.github/release.yml to categorize this PR in the auto-generated release notes. -->

- [ ] `feat:` New feature (non-breaking)
- [ ] `fix:` Bug fix (non-breaking)
- [ ] `docs:` Documentation only
- [ ] `refactor:` Code refactor (no behavior change)
- [ ] `perf:` Performance improvement
- [ ] `test:` Test-only change
- [ ] `ci:` CI / tooling change
- [ ] `chore:` Maintenance (dep bumps, etc.)
- [ ] `breaking:` Breaking change (major version bump)

## Testing

<!-- How was this change verified? -->

- [ ] `pytest tests/` passes locally
- [ ] `cdk synth` succeeds (if CDK code changed)
- [ ] New tests added for new behavior
- [ ] Ran the change against a real AWS account (describe below)

<!-- If deployed to a real account, note what was verified. -->

## Live release validation

<!--
Select one decision and explain it. Live validation is normally required for
changes to deployed infrastructure/lifecycle or real AWS runtime integration.
It is usually not required for isolated, quickly validated CLI behavior;
CI/test-tooling-only changes; routine dependency bumps with no deployed effect;
docs/test-only changes; and behavior-preserving refactors. These are risk-based
categories: live-resource CLI changes and runtime-affecting dependency bumps can
still require validation. See ../docs/LIVE_RELEASE_VALIDATION.md.
-->

- [ ] Not required (explain the applicability decision below)
- [ ] Required and completed locally with `--actions all` for this exact SHA; the `PASSED` Markdown report was manually attached to the pull request
- [ ] Required but pending explicit validation-account and KMS-deletion authorization

**Applicability rationale:**

**Manual report attachment or comment link (never attach `checkpoint.json`):**

## Checklist

- [ ] Documentation updated (README, `docs/`, inline docstrings) as needed
- [ ] No secrets, credentials, or customer data committed
- [ ] `requirements-lock.txt` regenerated if `pyproject.toml` changed
- [ ] Changes align with the architecture described in `docs/ARCHITECTURE.md`

## Related issues

<!-- Link any related issues: "Fixes #123" or "Related to #456" -->
