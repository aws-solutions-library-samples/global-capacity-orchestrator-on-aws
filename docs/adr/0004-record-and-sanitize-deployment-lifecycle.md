# 0004. Record and sanitize the deployment lifecycle

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** GCO maintainers
- **Supersedes:** none
- **Superseded by:** none

## Context

A deployment recording is useful only when reviewers can trust what it depicts.
A cast produced from uncommitted source, a different commit than the one CI
validated, or the wrong AWS account is misleading even if the command succeeds.
Deploy and destroy recordings also contain CloudFormation output, ARNs, URLs,
and other values that routinely embed a 12-digit AWS account ID. Temporary AWS
access-key IDs can appear in diagnostic output as well and must never enter a
committed cast or GIF.

The recorder scripts are reusable project tooling, so they cannot hardcode one
account or one pull-request commit. A real lifecycle also changes the deploy
artifacts before teardown is recorded, which means a strict "no changed files"
check would reject the valid second half of the same recording session.

## Decision

We will make recorded deployment lifecycles guarded, reproducible, and
fail-closed for normal committed output.

1. A release or pull-request validation run records only a full commit SHA that
   has completed the required CI checks. The operator supplies that SHA through
   `GCO_EXPECTED_GIT_SHA`; the recorder compares it with `HEAD` and rejects any
   unexpected source-tree change. The four generated lifecycle artifacts
   (`deploy.cast`, `deploy.gif`, `destroy.cast`, and `destroy.gif`) are the only
   paths allowed to differ so teardown can follow a just-recorded deployment.
2. The operator supplies the authorized 12-digit account through
   `GCO_EXPECTED_ACCOUNT_ID`. The recorder resolves the active identity with
   `aws sts get-caller-identity` and stops before any infrastructure command if
   it does not match. Reusable scripts do not embed an account number.
3. Deploy, bounded live validation, and destroy use the same expected SHA and
   account. Accelerator use is opt-in, tightly bounded in duration and scale,
   and followed by teardown verification; a recording is not evidence of
   cleanup by itself.
4. Each cast is sanitized before GIF rendering. Every standalone 12-digit
   account-ID-shaped value is replaced with `000000000000`, AWS access-key-ID
   patterns are replaced with a non-secret marker, and a separate verification
   pass rejects any residual pattern before `agg` can render it into pixels.
   Longer numeric identifiers are not account IDs and are left intact rather
   than being partially rewritten.
5. Recorders invoke `asciinema rec --return`, so a failed deploy or destroy
   command fails the recorder and cannot proceed to sanitization or publication.
6. Raw casts, rendered GIFs, wrappers, and prior-artifact backups are staged
   beside the tracked outputs. Final cast/GIF publication uses two individually
   atomic renames wrapped in a shared rollback transaction: an `EXIT` trap
   restores both prior artifacts (or removes both new artifacts) after an
   ordinary failure or handled `HUP`, `INT`, or `TERM`. Publication is complete
   only after both final-path operations succeed. `SIGKILL` cannot be trapped.
7. `SKIP_SANITIZE=1` remains a local debugging escape hatch only. Output from a
   bypassed run must not be committed or distributed.

The SHA and account guards remain optional for casual local demonstrations so
the reusable scripts retain their existing ergonomics. They are mandatory for
an auditable PR, release, or published lifecycle recording.

## Consequences

### Positive

- A published deploy/destroy pair is tied to the exact reviewed source and
  authorized account used for the live validation.
- Wrong-account and dirty-source mistakes fail before an infrastructure
  mutation begins.
- Casts and derived GIFs have a machine-checked redaction boundary rather than
  relying on visual review.
- Failed recorded commands cannot be mistaken for successful lifecycle assets.
- Interrupted recording sessions do not leave executable temporary wrappers,
  and handled interruptions cannot leave a mixed-generation cast/GIF pair.

### Negative

- Auditable runs require operators to copy the exact green SHA and account into
  environment variables.
- Sanitization intentionally replaces any unrelated standalone 12-digit value
  because it is safer to over-redact than to miss an account ID.
- The allowlist for generated lifecycle artifacts must stay synchronized if the
  recorder output names change.
- POSIX filesystems cannot atomically switch two ordinary files together. The
  rollback transaction covers normal failures and trappable signals, but not a
  process terminated with `SIGKILL` or a host/filesystem failure.

### Neutral

- The scripts verify identity and provenance, but CI status is still checked by
  the release/PR operator before setting the expected SHA.
- Existing cast/GIF formats and rendering tools remain unchanged.

## Alternatives considered

### Hardcode the project test account

- **Summary:** embed one AWS account ID in both recorder scripts.
- **Why not:** it would make reusable open-source tooling account-specific and
  risks steering other operators toward an account they do not control.

### Trust the current checkout and ambient credentials

- **Summary:** record whatever `HEAD` and AWS identity happen to be active.
- **Why not:** neither the artifact nor the command result proves that CI tested
  that source or that the intended account was mutated.

### Sanitize only after GIF rendering

- **Summary:** redact the cast after `agg` has produced the GIF.
- **Why not:** the sensitive text would already be rasterized into image frames
  and could not be reliably removed.

### Rely on manual visual inspection

- **Summary:** have a reviewer watch the recording and look for identifiers.
- **Why not:** fast or dense terminal output makes this error-prone, and access
  key IDs can be visible for only a few frames.

### Publish the cast and GIF with unrelated renames

- **Summary:** replace the cast, then independently replace or remove the GIF.
- **Why not:** failure or interruption between operations can leave artifacts
  from different recording generations. Preserving and restoring the previous
  pair provides the strongest practical transaction available for two files.

## References

- PR #161
- [`../../demo/record_deploy.sh`](../../demo/record_deploy.sh)
- [`../../demo/record_destroy.sh`](../../demo/record_destroy.sh)
- [`../../demo/lib_demo.sh`](../../demo/lib_demo.sh)
- [`../../demo/README.md`](../../demo/README.md)
