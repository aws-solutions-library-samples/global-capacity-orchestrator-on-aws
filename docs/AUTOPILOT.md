# Autopilot

One command from a plain terminal to a fully configured agent session:

```bash
gco autopilot
```

<details>
<summary>🤖 Autopilot recording (click to expand)</summary>

![GCO Autopilot](../demo/autopilot.gif)

*`gco autopilot --dry-run` resolving the launch plan ([re-record](../demo/record_autopilot.sh))*

</details>

`gco autopilot` turns the current terminal into an opinionated [Claude Code](https://code.claude.com/docs/en/overview) session that is ready to operate GCO:

| What you get | Details |
|--------------|---------|
| **Amazon Bedrock backend** | The session uses your AWS credentials (`CLAUDE_CODE_USE_BEDROCK=1`) — no Anthropic account or API key. The model defaults to GCO's Claude Code default (`cdk.json` → `context.bedrock.claude_code_default_model_id`, currently the Claude Opus 5 global cross-Region inference profile) and can be any Claude model or inference profile enabled on Bedrock. |
| **GCO MCP server** | Wired in automatically. From a source checkout the session runs your working tree (`gco_mcp/run_mcp.py`); from an installed `gco-cli` it runs the matching release tag via `uvx`. |
| **Companion MCP servers** | Every server from the [Recommended Companion MCP Servers](../gco_mcp/README.md#recommended-companion-mcp-servers) list — AWS docs, pricing, EKS, filesystem, web search, memory, sequential thinking, and the rest — generated into a session-scoped config. |
| **Hermetic MCP config** | The generated config is passed with `--strict-mcp-config`, so every autopilot session starts from the same known-good server set regardless of personal or project MCP configs on the machine. |
| **Lazy, pinned install** | Claude Code is deliberately **not** baked into the dev container. When the `claude` binary is missing, autopilot offers to install the exact pinned release (`npm install -g @anthropic-ai/claude-code@<pin>`); the monthly deps-scan reports drift against npm's `latest`. |

## Table of Contents

- [The Front Door](#the-front-door)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Choosing a Model](#choosing-a-model)
- [Region Resolution](#region-resolution)
- [The Generated MCP Config](#the-generated-mcp-config)
- [Resuming Sessions](#resuming-sessions)
- [GCO MCP Feature Flags](#gco-mcp-feature-flags)
- [Bring Your Own Skills, Agents, and Plugins](#bring-your-own-skills-agents-and-plugins)
- [Passing Arguments to Claude Code](#passing-arguments-to-claude-code)
- [Security Notes](#security-notes)
- [How It's Tested and Kept Fresh](#how-its-tested-and-kept-fresh)
- [Troubleshooting](#troubleshooting)

## The Front Door

With git and a container runtime installed, this is the whole journey from nothing to a working Claude Code setup:

```bash
git clone https://github.com/awslabs/global-capacity-orchestrator-on-aws.git
cd global-capacity-orchestrator-on-aws
./scripts/setup-dev-alias.sh    # builds the dev container + installs the `gco` shell function
source ~/.zshrc                 # or ~/.bashrc — the script prints which file it updated
gco autopilot                   # offers the pinned Claude Code install, then launches
```

The dev-container `gco` function is autopilot-aware: it mounts a named volume over the container's npm prefix (so the pinned Claude Code install from the first launch **persists** across the otherwise-ephemeral containers), and it mounts `~/.claude` and `~/.gco` from your home directory (so onboarding state, session transcripts — and therefore [session resume](#resuming-sessions) — and the generated MCP config all survive too). The dev image ships `uv`/`uvx` and `node`/`npx` at pinned versions, which is everything the companion MCP servers need at launch.

## Requirements

- **AWS credentials** with `bedrock:InvokeModel` for the chosen model (the standard credential chain — env vars, `~/.aws`, SSO, instance roles — all work).
- **Model access enabled** in your account. Anthropic models on Bedrock are gated behind a one-time first-time-use form; see the remediation printed by any GCO Bedrock feature, or [docs/CUSTOMIZATION.md](CUSTOMIZATION.md) for the override mechanics.
- **The GCO CLI** (`gco`). The [dev container](../QUICKSTART.md#step-1-clone-and-build-the-dev-container) is the recommended way to get it.
- **npm** — only on first use, if Claude Code isn't already installed. The dev container ships Node.js + npm; on a bare host any Node 18+ works.
- **`uvx` and `npx` at session runtime** — the companion MCP servers launch through them (the dev container and any Python 3.14 + Node environment have both).

## Quick Start

```bash
# See exactly what would happen — model, region, servers, install plan:
gco autopilot --dry-run

# Launch. If Claude Code is missing you'll be offered the pinned install
# (pass -y to skip the prompt):
gco autopilot
```

The terminal becomes the Claude Code session (the `gco` process is replaced, not wrapped). Exit Claude Code and you're back in your shell.

Useful variants:

```bash
gco autopilot --continue                              # resume the last session
gco autopilot --resume                                # pick a session to resume
gco autopilot --skills ~/team-skills                  # import your own skills
gco autopilot --plugin ~/plugins/incident-response    # load a plugin for the session
gco autopilot -m global.anthropic.claude-sonnet-4-6   # different Claude model
gco autopilot --no-companions                         # GCO MCP server only
gco autopilot --print-config                          # dump the MCP config JSON
gco -o json autopilot --dry-run                       # machine-readable plan
```

## Choosing a Model

Resolution order:

1. `--model` / `-m` flag
2. `GCO_AUTOPILOT_MODEL` environment variable
3. `cdk.json` → `context.bedrock.claude_code_default_model_id`, resolved through `gco.bedrock` — autopilot's own default, deliberately separate from the advisory `default_model_id` that Mission sampling and the capacity advisor share. Repointing the interactive agent (`gco stacks bedrock set-claude-code-model`) never repoints the advisory features, and vice versa; future agent runners get their own sibling keys. Both keys ship the same profile today.

Any Claude model or inference profile available on Bedrock works, including application inference-profile ARNs. A model id that doesn't look like a Claude model produces a warning (Claude Code is tuned for Claude models) but is not refused.

Claude Code also uses a small/fast model for background tasks. Autopilot leaves that unset by default — the right haiku-class profile depends on what your account has enabled — but you can pin one with `--small-fast-model` or `GCO_AUTOPILOT_SMALL_FAST_MODEL`.

## Region Resolution

The Bedrock calls go to `AWS_REGION`. If it's already set in your environment it wins (least surprise). Otherwise autopilot sets it from the GCO CLI's configured default region (`gco --region …`, `GCO_DEFAULT_REGION`, or the first regional deployment in `cdk.json`). Cross-Region inference profiles like the default `global.…` profile route within their geography regardless of the calling Region.

## The Generated MCP Config

Every launch regenerates `~/.gco/autopilot/mcp.json` (override the directory with `GCO_AUTOPILOT_CONFIG_DIR`) and passes it to Claude Code with `--mcp-config … --strict-mcp-config`. Two consequences:

- **Hand edits don't survive** — the file is rewritten on the next launch. Persistent customization belongs in your own MCP config; see [gco_mcp/README.md](../gco_mcp/README.md) for per-client setup and the full companion list with rationale.
- **Nothing else leaks in** — `--strict-mcp-config` makes this the *only* MCP config for the session, so a broken or surprising server in `~/.claude` or a project `.mcp.json` can't affect an autopilot session.

Inspect what would be generated without launching:

```bash
gco autopilot --print-config
```

The `filesystem` companion is scoped to the directory you launch from, and the `eks` companion runs **read-only** (no `--allow-write`, no `--allow-sensitive-data-access`) — see [Security Notes](#security-notes).

## Resuming Sessions

Autopilot picks up where you left off:

- **Interactive prompt.** When Claude Code already has a session for this workspace, launching `gco autopilot` on a terminal asks `Resume your previous Claude Code session in this workspace?` — one keypress to continue, Enter to start fresh. The prompt never appears for `--yes` or non-interactive (piped/CI) runs, so scripts can't hang.
- **`--continue` / `-c`** resumes the most recent session directly, no prompt.
- **`--resume [SESSION_ID]`** resumes a specific session, or opens claude's interactive session picker when no id is given.

A resumed conversation keeps its history, but the MCP servers and Bedrock environment come from *this* launch — so model overrides and feature flags apply to resumed sessions too.

## GCO MCP Feature Flags

The GCO MCP server keeps its mutating and sensitive tool groups behind opt-in feature flags — deploys, destroys, capacity purchases, model uploads, Mission, and friends stay unmounted until you ask (see [gco_mcp/README.md](../gco_mcp/README.md#feature-flags) for what each flag gates). Autopilot exposes that directly:

```bash
gco autopilot                                     # default: read-only toolset
gco autopilot -e mission                          # one flag
gco autopilot -e mission -e infrastructure-deploy # several
gco autopilot -e all-tools                        # everything (umbrella flag)
gco autopilot --mcp-env GCO_MCP_TOOL_SEARCH=bm25  # any other server env var
```

Details worth knowing:

- `--enable` / `-e` accepts the short form (`mission`, `all-tools`, `infrastructure-deploy`) or the full `GCO_ENABLE_*` name. Unknown flags fail immediately with the valid list — a typo never silently launches a session missing the tools you wanted. The valid set comes from the server's own flag registry, so it can't drift.
- Flags apply to the **gco server only**; companions are unaffected.
- `--mcp-env KEY=VALUE` is the generic escape hatch for non-flag server settings, and it wins over `--enable` for the same key.
- The umbrella `all-tools` flag overrides per-flag values by design (that's the server's own semantics), so there is no `--disable`: to run with less, enable less.
- Enabled flags show up in `--dry-run` (`GCO MCP env:` line) and in the `env` of the `gco` entry in `--print-config`.

## Bring Your Own Skills, Agents, and Plugins

Claude Code's normal discovery still applies inside an autopilot session — skills and subagents in `~/.claude/` and in the workspace's `.claude/` load exactly as they always do (only the *MCP config* is strict). For everything that lives somewhere else — a team repo of skills, a scratch directory of agent definitions, a packaged plugin — autopilot adds three doors:

```bash
gco autopilot --skills ~/team-skills               # dirs of <skill>/SKILL.md
gco autopilot --agents ~/my-agents                 # dirs of *.md subagents
gco autopilot --plugin ~/plugins/incident-response # a full plugin dir or .zip
```

- **`--skills DIR` / `--agents DIR`** (repeatable) import loose directories. Autopilot packages them into a session-scoped plugin (`~/.gco/autopilot/gco-autopilot-imports/`, rebuilt from scratch every launch) and hands it to claude with `--plugin-dir` — nothing is copied into your project or `~/.claude`, and nothing persists beyond the staged copy. Sources are validated up front: a skills dir must contain at least one `*/SKILL.md`, an agents dir at least one `*.md`, and a typo'd path fails the launch instead of silently starting a session without your tools.
- **`--plugin PATH`** (repeatable) loads a ready-made [Claude Code plugin](https://code.claude.com/docs/en/plugins) directory or `.zip` for this session — plugins can bundle skills, agents, commands, and hooks together. Set `GCO_AUTOPILOT_PLUGIN_DIRS` (colon-separated) for plugins you want in *every* autopilot session.
- Imports show up in `--dry-run` (`Plugins:` / `Imports:` lines) so you can check what a launch would load.

## Passing Arguments to Claude Code

Everything after `--` goes to the `claude` CLI unchanged:

```bash
gco autopilot -- --continue          # resume the previous conversation
gco autopilot -- --permission-mode plan
```

## Security Notes

- **Each MCP server is a separate process with your credentials' reach.** The companion set is curated, but review [gco_mcp/README.md](../gco_mcp/README.md#recommended-companion-mcp-servers) before treating the session as low-privilege — the AWS-focused servers can read whatever your credentials can.
- **EKS server is read-only by default.** An auto-generated agent session should not silently hold cluster write access. If you want the mutating tools, add the server with `--allow-write` to your own MCP config instead of autopilot's.
- **Shell server allowlist is tight.** `ls,cat,pwd,grep,wc,touch,find` — no `rm`, no `git`. Widen it in your own config only with care.
- **The Bedrock session itself** is ordinary AWS API traffic under your IAM identity: CloudTrail applies, and no code or prompts leave AWS for Anthropic's API.

## How It's Tested and Kept Fresh

- `tests/test_cli_autopilot.py` covers plan resolution, config generation, the install flow, and — via lockstep guards — keeps the companion registry, the `gco_mcp/README.md` tables, and the deps-scan extraction regexes agreeing with each other.
- The `unit:cli:autopilot` CI job resolves the plan without Claude Code, validates the generated config, then installs the pinned release from npm and verifies detection — proving the pin is actually installable on every PR.
- The monthly [deps-scan](../.github/CI.md#dependency-scan-script) reports when the pinned Claude Code release falls behind npm's `latest`, and when any companion MCP package goes **missing, deprecated, or yanked** on its registry. Companions launch unpinned through `npx`/`uvx`, so registry health is their real dependency surface — exactly the failure mode that got `mcp-server-fetch` and `mcp-server-calculator` pruned in 2026-08.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `claude` not found right after a successful install | Your npm global bin dir isn't on `PATH` for this shell. Open a new shell, or add `$(npm prefix -g)/bin` to `PATH`. |
| Bedrock returns a 404 with `FTUFormNotFilled` | The account hasn't submitted Anthropic's one-time use-case form. Bedrock console → Model access → request the Anthropic model, or `aws bedrock put-use-case-for-model-access`. |
| `AccessDeniedException` on first message | The chosen model/profile isn't enabled in this account/Region, or the IAM identity lacks `bedrock:InvokeModel`. Try `-m` with a model you know is enabled. |
| Companion server fails to start inside the session | Run `claude --debug` via `gco autopilot -- --debug` and check the MCP logs; a registry outage or a newly-broken upstream package will show at launch. The monthly deps-scan flags dead companions — see the removals note in [gco_mcp/README.md](../gco_mcp/README.md#recommended-companion-mcp-servers). |
| You want a different companion set every time | Put your preferred servers in your own MCP config (see [gco_mcp/README.md](../gco_mcp/README.md)) and run `claude` directly, or launch with `--no-companions` and add servers with `claude mcp add`. |
