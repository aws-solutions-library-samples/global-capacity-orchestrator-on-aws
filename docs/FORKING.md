# Forking GCO Into Your Own Repository

GCO is designed to be taken and run with. This guide covers moving a copy into
your own repository — what to rewrite, what to leave alone, and the handful of
things a script cannot decide for you.

Everything here is about *repository identity*: the URLs, badges, and trust
policies that name `awslabs/global-capacity-orchestrator-on-aws`. Renaming the
*deployment* is a separate, orthogonal knob — see
[Renaming the deployment](#renaming-the-deployment).

## Table of Contents

- [Quick Start](#quick-start)
- [What the Tool Rewrites](#what-the-tool-rewrites)
- [What the Tool Preserves, and Why](#what-the-tool-preserves-and-why)
- [Manual Follow-Ups](#manual-follow-ups)
- [Verifying the Migration](#verifying-the-migration)
- [Renaming the Deployment](#renaming-the-deployment)
- [Staying in Sync With Upstream](#staying-in-sync-with-upstream)

## Quick Start

Create your repository first — either a GitHub fork, or an empty repository you
push a clone into:

```bash
git clone https://github.com/awslabs/global-capacity-orchestrator-on-aws.git my-gco
cd my-gco
git remote rename origin upstream
git remote add origin https://github.com/myorg/my-gco.git
```

Preview the reference changes. This writes nothing:

```bash
python scripts/migrate_fork.py --repo-url https://github.com/myorg/my-gco
```

The script is [`scripts/migrate_fork.py`](../scripts/migrate_fork.py); its module
docstring documents every flag and the rule table it applies.

The output lists every reference it would rewrite, every reference it will
deliberately leave alone, and a checklist of manual follow-ups. When it looks
right, apply it:

```bash
python scripts/migrate_fork.py --repo-url https://github.com/myorg/my-gco --apply
git diff                      # review
git commit -am "Repoint repository references at myorg/my-gco"
```

The tool refuses to run with `--apply` against a dirty working tree, so
`git diff` always shows exactly what it changed and `git checkout .` always
reverts it. Running it twice is a no-op.

`--owner myorg --repo my-gco` works if you would rather not pass a URL, and
`--json` emits the same report as machine-readable JSON.

## What the Tool Rewrites

Only git-tracked text files are considered, so ignored build output is never
touched. In a clean checkout the tool finds about 70 references across roughly
20 files:

| Reference | Example | Why it matters |
|-----------|---------|----------------|
| Repository URLs | `github.com/awslabs/global-capacity-orchestrator-on-aws` | CI badges, issue links, `tree`/`blob` links, `pyproject.toml` project URLs |
| SSH clone URLs | `git@github.com:awslabs/...` | The clone commands in `README.md` and `QUICKSTART.md` |
| GitHub Pages URL | `awslabs.github.io/global-capacity-orchestrator-on-aws` | The published site: orientation wiki at the root, coverage report at `/coverage/`, badge JSON at `/coverage-badge.json` (in `mkdocs.yml`, `wiki/*.md`, the README badge, and the wiki guard test) |
| Percent-encoded Pages URL | `awslabs.github.io%2Fglobal-capacity...` | The shields.io coverage badge embeds the Pages URL as a query parameter. Missing this leaves the badge reporting upstream's coverage while every other badge reports yours |
| Bare `owner/repo` slug | `"github_repo": "awslabs/global-capacity-orchestrator-on-aws"` | The **OIDC trust-policy subject**. Until this changes, your workflows cannot assume the deploy role |
| Bare repository name | `cd global-capacity-orchestrator-on-aws`, `/path/to/global-capacity-orchestrator-on-aws` | Clone directory names and the MCP server setup paths |

## What the Tool Preserves, and Why

The reason to use the tool rather than `sed -i 's/awslabs/myorg/g'` is that this
repository contains roughly twice as many occurrences of `awslabs` that must
survive as occurrences that should change. A blanket replacement silently breaks
two categories:

**Links to other AWS Labs projects.** Seven of them are referenced in the docs,
including [`aws-sigv4-proxy`](https://github.com/awslabs/aws-sigv4-proxy),
[`amazon-eks-ami`](https://github.com/awslabs/amazon-eks-ami), and the
[`ai-on-eks`](https://awslabs.github.io/ai-on-eks/) blueprints. Rewriting them
produces dead links to repositories under your org that do not exist.

**`awslabs.*` package names.** The MCP server names — `awslabs.eks-mcp-server`,
`awslabs.aws-documentation-mcp-server`, `awslabs.aws-pricing-mcp-server` — are
resolved from a package registry at runtime by `mcp.json`. Rewriting them means
`uvx` cannot find the package and the servers fail to start.

Three files keep their upstream references on purpose, because they define or
explain the upstream identity: `scripts/migrate_fork.py`,
`tests/test_migrate_fork.py`, and this guide.

Terminal recordings (`demo/*.cast`) are reported but not edited. They captured a
real session against the upstream URL; rewriting the captured output would
desynchronize the recording from what actually ran. Re-record with
[`demo/record_demo.sh`](../demo/record_demo.sh) if the URL on screen matters to
you.

A test named `test_every_upstream_reference_is_classified` walks every tracked
file and asserts that each occurrence of the upstream org or repository name is
claimed by exactly one rule. If a future change introduces a reference in a
shape the tool does not recognize, that test fails until a rule covers it — so
the classification cannot quietly fall behind the repository.

## Manual Follow-Ups

The tool reports these against your checkout. They are decisions or
out-of-repository actions, not string substitutions.

### Redeploy the OIDC provider stack

The most important one. `scripts/migrate_fork.py` updates the `github_repo`
context value in `.github/oidc_provider/cdk.json`, but the deployed IAM role
still trusts the old subject until you redeploy it in your AWS account:

```bash
cd .github/oidc_provider
cdk deploy --context github_repo=myorg/my-gco
```

Until then, workflows fail at the credential step with an STS error rather than
anything that mentions forking. See
[`.github/oidc_provider/README.md`](../.github/oidc_provider/README.md).

### Enable GitHub Pages

The project site is published by
[`.github/workflows/pages.yml`](../.github/workflows/pages.yml) to
`https://<owner>.github.io/<repo>/`: the orientation wiki (built with MkDocs
from [`wiki/`](../wiki)) is served at the site root, the HTML coverage report
at `/coverage/`, and the badge endpoint JSON at `/coverage-badge.json`. Enable
Pages on your repository — **Settings > Pages**, source **GitHub Actions** —
or the site and badge 404 even with correct URLs. The migration rewrites the
wiki's own URLs (`mkdocs.yml` `site_url`/`repo_url`, the nav's coverage link,
and every GitHub deep link in `wiki/*.md`), so your fork's wiki links to your
fork's files.

### Replace CODEOWNERS

[`.github/CODEOWNERS`](../.github/CODEOWNERS) assigns review to upstream's
maintainer. In your repository that handle either has no access or is simply not
who you want reviewing, and every pull request will request review from them.
Replace the owners or delete the file.

### Decide about the AWS Solutions ID

[`app.py`](../app.py) sets `SOLUTION_ID = "SO9707"`, which is prefixed onto the
global stack's CloudFormation description. It identifies deployments of the
*published* AWS guidance. A fork that has diverged generally should not keep
claiming it; remove it, or replace it with your own identifier. Nothing
functional depends on the value.

### Add your own security contact

[`.github/SECURITY.md`](../.github/SECURITY.md) describes AWS's vulnerability
disclosure process, which remains the right destination for issues in the
inherited code. Add how reporters should reach *you* about issues in your fork.

### Keep the attribution

`LICENSE` and `NOTICE` are deliberately untouched. Keep them, and add your own
copyright alongside rather than replacing what is there.

### Repository settings that do not live in files

- **Actions secrets and variables** the workflows expect, notably the AWS role
  ARN the OIDC login step assumes.
- **Branch protection** rules, which forks do not inherit.
- **Dependabot** — [`.github/dependabot.yml`](../.github/dependabot.yml) works
  as-is, but Dependabot must be enabled for the repository.
- **Actions permissions**, if your organization restricts which actions may run.

## Verifying the Migration

The migration is verifiable without deploying anything:

```bash
# No upstream references remain outside the intentional exclusions
python scripts/migrate_fork.py --owner myorg --repo my-gco   # expect "No references needed rewriting"

# The repository's own guards still pass
pytest tests/test_migrate_fork.py tests/test_oidc_stack.py tests/test_docs_coverage.py
npx markdownlint-cli2 --config .github/config/.markdownlint-cli2.yaml
```

`tests/test_oidc_stack.py` asserts the trust-policy subject, and
`tests/test_docs_coverage.py` checks documentation consistency, so both fail if
the rewrite was incomplete or malformed.

Then push and confirm CI is green on your own runners:

```bash
git push -u origin main
```

## Renaming the Deployment

Repository identity and *deployment* identity are separate. `project_name` in
[`cdk.json`](../cdk.json) is what scopes every AWS resource name, and changing it
yields a fully isolated deployment that can coexist with others in the same
account. If you want your stacks, ECR repositories, and DynamoDB tables to carry
your own name, change `project_name` — not the repository URL.

See [Customization — project_name](CUSTOMIZATION.md) for what it scopes and the
naming constraints. `scripts/migrate_fork.py` deliberately does not touch it:
renaming a deployment recreates infrastructure, which is not something a
documentation-and-URL migration should do as a side effect.

## Staying in Sync With Upstream

Keeping an `upstream` remote lets you pull fixes after diverging:

```bash
git remote add upstream https://github.com/awslabs/global-capacity-orchestrator-on-aws.git
git fetch upstream
git merge upstream/main
```

Merges will conflict on the files the migration rewrote — `README.md`,
`pyproject.toml`, `.github/oidc_provider/cdk.json` and friends — because both
sides changed the same lines. Resolve by keeping your identity and taking
upstream's substance. After any merge that brings in new upstream references,
re-run the tool to catch them:

```bash
python scripts/migrate_fork.py --owner myorg --repo my-gco
```

Because it is idempotent, running it after every upstream merge is safe and
cheap.
