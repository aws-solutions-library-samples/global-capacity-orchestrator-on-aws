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

1. A release or pull-request recording starts only from a full commit SHA that
   has completed required CI. The operator explicitly opts in with
   `GCO_RECORDING_LIVE=1` and supplies that SHA through
   `GCO_EXPECTED_GIT_SHA`; each legacy recorder compares it with `HEAD` and
   rejects any unexpected source-tree change. The six generated legacy
   artifacts (`deploy`, `live_demo`, and `destroy`, each `.cast` + `.gif`) are
   the only paths allowed to differ so the full sequence can be captured from
   one checkout.
2. The operator must supply the authorized 12-digit account through
   `GCO_EXPECTED_ACCOUNT_ID`. The recorder resolves the active identity with
   `aws sts get-caller-identity` and stops before any infrastructure command if
   it does not match. Reusable scripts do not embed an account number.
3. Deploy, bounded live validation, and destroy use the same expected SHA and
   account. The live recorder first resolves the expected EKS endpoint through
   that AWS identity and requires the active minified kubectl context to match
   it exactly. After acquiring the recorder lock, it copies only that context
   with `kubectl config view --raw --minify --flatten` into a mode-`0600` file
   in its private staging directory, exports the single file as `KUBECONFIG`,
   and repeats endpoint and reachability checks. Repository CLI context
   refreshes can modify only this disposable copy, never the operator's
   kubeconfig. Guarded child execution cannot auto-configure access or force
   through a failed preflight, and it repeats the endpoint check immediately
   before `kubectl delete jobs --all`. The raw snapshot may contain credential
   material and is unlinked before publication rollback on every handled exit;
   if rollback fails, only noncredential staging is preserved for recovery.
   Accelerator use is opt-in, tightly bounded in duration and scale, and
   followed by teardown verification; a recording is not evidence of cleanup
   by itself.
4. Each cast is sanitized before GIF rendering. Every standalone 12-digit
   account-ID-shaped value is replaced with `000000000000`, AWS access-key-ID
   patterns are replaced with a non-secret marker, and a separate verification
   pass rejects any residual pattern before `agg` can render it into pixels.
   Longer numeric identifiers are not account IDs and are left intact rather
   than being partially rewritten.
5. Recorders invoke `asciinema rec --return`, so a failed deploy, live demo, or
   destroy command fails the recorder and cannot proceed to sanitization or
   publication.
6. A repository-wide fixed lock path beneath Git's common directory serializes
   every legacy recorder across linked worktrees from before child execution
   through rollback cleanup. A process writes a private owner file and acquires
   the fixed path with an atomic same-directory hard link. Cleanup compares file
   identity before unlinking, so a contender or handled signal cannot remove
   another process's lock. Raw casts, rendered GIFs, wrappers, the live
   kubeconfig, and prior-artifact backups are staged beside the tracked outputs.
   Final cast/GIF publication uses two individually atomic renames wrapped in a
   shared rollback transaction: an `EXIT` trap restores both prior artifacts
   (or removes both new artifacts) after an ordinary failure or handled `HUP`,
   `INT`, or `TERM`. Publication is complete only after both final-path
   operations succeed. `SIGKILL` cannot be trapped and leaves the lock
   fail-closed for operator inspection.
7. `SKIP_SANITIZE=1` remains a lower-level local debugging escape hatch. The
   three publishable legacy recorders reject it and never install bypassed
   artifacts.
8. The optional topology a recording shows is selected per run through
   `GCO_DEMO_ENABLE`, never by editing `cdk.json`. Every optional add-on ships
   disabled because each bills continuously, and rewriting the config would
   both change the shipped default for every user and violate guard 1's
   clean-source-tree rule. One variable drives all three recorders: the deploy
   and destroy recorders pass it to `gco stacks deploy-all|destroy-all
   --enable`, and the live recorder exports it so `detect_features` enters the
   matching sections. Each recorder validates the value against the canonical
   name sets during preflight and refuses to start on an unknown name.
   Because the value is read independently by three separate script runs, it is
   a convention rather than an enforced invariant; guard 9 is what keeps a
   mismatch from being published.
9. A demo section may not claim a feature it did not exercise. The optional
   feature sections and the inference lifecycle report their result from
   observed evidence, and under `GCO_DEMO_GUARDED_RECORDING=1` an unproven
   claim fails the recorder through guard 5 instead of publishing. A section
   whose infrastructure was never created is therefore a failed recording, not
   a green claim.

The live consent, SHA, and account guards are mandatory for all three legacy
recorders. `RENDER_EXISTING=1` is the non-mutating path for replaying a verified
cast through visual rendering without AWS or Kubernetes calls.

## Consequences

### Positive

- Published deploy/live-demo/destroy pairs are tied to the exact reviewed
  source and authorized account used for the live validation.
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
