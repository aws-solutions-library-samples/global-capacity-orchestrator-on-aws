# Autopilot

One command turns a plain terminal into a fully configured agent session:

```bash
gco autopilot                         # Claude Code (default)
gco autopilot --engine codex          # OpenAI Codex
```

Both engines use Amazon Bedrock with your AWS credentials, the GCO MCP server,
and the recommended companion MCP servers. Claude Code remains the default for
backward compatibility; selecting Codex is explicit and does not change an
existing workflow.

<details>
<summary>Claude Code recording (click to expand)</summary>

![GCO Autopilot with Claude Code](../demo/autopilot-claude-code.gif)

*A real Claude Code session grounded by the GCO MCP server
([re-record](../demo/record_autopilot.sh)).*

</details>

<details>
<summary>Codex recording (click to expand)</summary>

![GCO Autopilot with Codex](../demo/autopilot-codex.gif)

*A real Bedrock-backed Codex session grounded by only the GCO MCP
`find_docs` and `read_resource` tools. The recording disables companions and
shell access and fails on trust/approval prompts ([docs](#security-notes) ·
[re-record](../demo/record_autopilot.sh) with
`DEMO_ENGINE=codex DEMO_MODE=live`).*

</details>

## What Autopilot Provides

| Capability | Claude Code | Codex |
|---|---|---|
| Engine selection | Default | `--engine codex` or `GCO_AUTOPILOT_ENGINE=codex` |
| Bedrock default | `context.bedrock.claude_code_default_model_id` | `context.bedrock.codex_default_model_id` |
| Generated config | `~/.gco/autopilot/mcp.json` | `~/.gco/autopilot/codex/config.toml` |
| Isolation | `--strict-mcp-config` | Isolated `CODEX_HOME`, project config disabled per launch, and session-precedence Bedrock controls |
| Canonical reasoning | Model-native Claude configuration | `context.bedrock.codex.reasoning_effort` (`xhigh` for the shipped default) |
| Lazy npm package | `@anthropic-ai/claude-code` | `@openai/codex` |
| Imported context | Skills, agents, and plugins | Skills |
| Resume mapping | `--continue`, `--resume [ID]` | `codex resume --last`, `codex resume [ID]` |

The selected CLI is deliberately not baked into `gco-dev`. Autopilot detects
its binary and offers the exact pinned npm install on first use. The monthly
dependency scan reports drift for both pins.

## Table of Contents

- [The Front Door](#the-front-door)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Choosing an Engine](#choosing-an-engine)
- [Choosing a Model and Reasoning](#choosing-a-model-and-reasoning)
- [Region Resolution](#region-resolution)
- [Generated Configuration and Isolation](#generated-configuration-and-isolation)
- [Resuming Sessions](#resuming-sessions)
- [GCO MCP Feature Flags](#gco-mcp-feature-flags)
- [Bring Your Own Context](#bring-your-own-context)
- [Passing Native Engine Arguments](#passing-native-engine-arguments)
- [Dev-Container Persistence](#dev-container-persistence)
- [Security Notes](#security-notes)
- [Mission Compatibility](#mission-compatibility)
- [How It Is Tested and Kept Fresh](#how-it-is-tested-and-kept-fresh)
- [Troubleshooting](#troubleshooting)

## The Front Door

With Git and a container runtime installed, this is the whole journey:

```bash
git clone https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git
cd global-capacity-orchestrator-on-aws
./scripts/setup-dev-alias.sh
source ~/.zshrc                       # or ~/.bashrc; the script prints the target
gco autopilot                         # default Claude Code session
gco autopilot --engine codex          # or a Codex session
```

The setup script builds `gco-dev` and installs a shell function that runs each
`gco` command against the current checkout. It also supplies the persistent
mounts both Autopilot engines need; see [Dev-Container Persistence](#dev-container-persistence).

## Requirements

- AWS credentials with `bedrock:InvokeModel`. Claude requires the selected
  model/profile resource. Codex's Bedrock Responses path authorizes the same
  action against both the selected inference target and the account's default
  Bedrock project, so least-privilege policies must include both resources (see
  [Bedrock conversation inference](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html)).
  The normal AWS credential chain works: environment variables, `~/.aws`, SSO,
  web identity, and instance or task roles.
- Model access in the account and calling Region. Anthropic models have a
  first-time-use requirement; the OpenAI GPT profile does not use that form.
- The GCO CLI. The dev container is the recommended environment.
- Node.js 24 and npm 12.0.2 for the repository-pinned lazy installation. The
  dev container already supplies both.
- `uvx` and `npx` at session runtime for companion MCP servers. The dev
  container supplies these too.

## Quick Start

```bash
# Preview without writing config, installing a CLI, or launching it:
gco autopilot --dry-run
gco autopilot --engine codex --dry-run

# Launch; add -y to accept an absent selected engine's exact pinned install:
gco autopilot
gco autopilot --engine codex
```

Useful shared options:

```bash
gco autopilot --no-companions
gco autopilot -e mission -e infrastructure-deploy
gco autopilot --mcp-env GCO_MCP_TOOL_SEARCH=bm25
gco autopilot --skills ~/team-skills
gco -o json autopilot --engine codex --dry-run
gco autopilot --engine codex --print-config
```

On POSIX, the `gco` process is replaced by the selected engine, so terminal,
signal, and exit behavior are native rather than wrapped.

## Choosing an Engine

Engine resolution is deterministic:

1. `--engine claude-code|codex`
2. `GCO_AUTOPILOT_ENGINE`
3. `claude-code`

Examples:

```bash
gco autopilot                                  # Claude Code
GCO_AUTOPILOT_ENGINE=codex gco autopilot       # Codex by environment
gco autopilot --engine claude-code             # flag wins over environment
```

A malformed or blank environment value fails instead of silently falling back.

## Choosing a Model and Reasoning

### Claude Code

Claude model precedence is:

1. `--model` / `-m`
2. `GCO_AUTOPILOT_MODEL`
3. `context.bedrock.claude_code_default_model_id`

The shipped default is the global Claude Opus 5 inference profile. A non-Claude
ID is allowed with a warning because Claude Code is tuned for Claude models.
`--small-fast-model` and `GCO_AUTOPILOT_SMALL_FAST_MODEL` optionally select a
background model and apply only to Claude Code.

### Codex

Codex model precedence is:

1. `--model` / `-m`
2. `GCO_AUTOPILOT_CODEX_MODEL`
3. `GCO_AUTOPILOT_MODEL`
4. `context.bedrock.codex_default_model_id`

The shipped model is `global.openai.gpt-5.6-sol`. The generated TOML selects
`model_provider = "amazon-bedrock-runtime"` and the Responses wire API so the
cross-Region inference profile is sent to the Bedrock Runtime endpoint. The canonical
model receives `context.bedrock.codex.reasoning_effort`, currently `xhigh`.

Reasoning is deliberately omitted when the model comes from a CLI or
environment override: Autopilot cannot assume that an arbitrary replacement
accepts the canonical model's effort level. Put a new canonical model and its
reviewed effort in `cdk.json`; do not override provider/reasoning through native
Codex passthrough.

Blank model overrides fail closed for both engines.

## Region Resolution

`AWS_REGION` wins, then `AWS_DEFAULT_REGION`, then the GCO CLI's configured
default Region. Claude receives that Region in its Bedrock environment; Codex
receives it in `[model_providers.amazon-bedrock-runtime.aws]`. Global inference
profiles still route across their supported geography.

## Generated Configuration and Isolation

### Claude Code

Every launch regenerates `~/.gco/autopilot/mcp.json` and passes it with
`--mcp-config` and `--strict-mcp-config`. The generated file is the session's
only MCP config, so personal or project MCP entries cannot leak into the plan.
Autopilot also sets `DISABLE_AUTOUPDATER=1`; Claude Code upgrades happen only
when GCO's reviewed npm pin changes, not by mutating the launched installation.

### Codex

Every launch regenerates `~/.gco/autopilot/codex/config.toml` and sets
`CODEX_HOME=~/.gco/autopilot/codex`, so personal `~/.codex` state is neither
read nor modified. Codex 0.152.0 normally layers a trusted workspace's
`.codex/config.toml` above that user file, so Autopilot also:

- identifies Codex's Git project root (including linked worktrees);
- marks that project layer `untrusted` for this process only, disabling project
  config/hooks without persisting a trust decision; and
- repeats the selected model, provider, reasoning (when canonical), Region,
  Responses wire API, and update policy at session precedence.

The generated TOML still contains:

- the selected model and `amazon-bedrock-runtime` provider;
- canonical reasoning only when appropriate;
- the resolved AWS Region and `wire_api = "responses"`;
- update checks disabled; and
- the same GCO and companion MCP server registry as Claude.

Organization-managed Codex policy remains authoritative by design. Codex
0.152.0 has no Claude-equivalent strict replacement switch for system/managed
MCP layers; Autopilot's guarantee is isolation from personal and project
configuration, not bypassing administrator policy.

`--print-config` emits JSON for Claude and the generated user-layer TOML for
Codex; session-precedence safeguards are applied only when Codex launches.
`--dry-run` writes neither file. Set `GCO_AUTOPILOT_CONFIG_DIR` to relocate the
generated root. Hand edits do not survive the next launch.

## Resuming Sessions

Shared top-level controls map to each engine:

```bash
gco autopilot --continue
gco autopilot --resume
gco autopilot --resume SESSION_ID

gco autopilot --engine codex --continue
gco autopilot --engine codex --resume
gco autopilot --engine codex --resume SESSION_ID
```

Claude maps these to its native continue/session picker behavior and may offer
an interactive workspace-resume prompt when no explicit option was supplied.
Codex maps them to `codex resume --last`, `codex resume`, or
`codex resume SESSION_ID`. Codex does not use Claude's transcript probe or
prompt.

A resumed session still receives this launch's model, Region, MCP registry,
feature flags, and generated config.

## GCO MCP Feature Flags

Feature flags are shared across engines and apply only to the `gco` server:

```bash
gco autopilot                                     # read-only default tools
gco autopilot --engine codex -e mission
gco autopilot -e mission -e infrastructure-deploy
gco autopilot -e all-tools
gco autopilot --mcp-env GCO_MCP_TOOL_SEARCH=bm25
```

`--enable` accepts a short name or full `GCO_ENABLE_*` variable. Unknown names
and malformed `--mcp-env` values fail before launch. The resolved environment
appears in both dry-run plans and generated config formats.

## Bring Your Own Context

`--skills DIR` works with both engines. Each source must contain at least one
`<skill>/SKILL.md` or the launch fails:

```bash
gco autopilot --skills ~/team-skills
gco autopilot --engine codex --skills ~/team-skills
```

Claude packages imported skills and `--agents DIR` into a session plugin and
also supports `--plugin PATH` plus `GCO_AUTOPILOT_PLUGIN_DIRS`. Codex copies
skills into its isolated `CODEX_HOME/skills`; Claude plugins and agent files
are rejected before their paths are inspected because those formats are not
Codex concepts. Nothing is written into the project or personal `~/.codex`.

## Passing Native Engine Arguments

Everything after the first `--` goes to the selected CLI:

```bash
gco autopilot -- --permission-mode plan
gco autopilot --engine codex -- --no-alt-screen
gco autopilot --engine codex -- -c 'sandbox_mode="read-only"'
```

Codex passthrough may not override the isolated Bedrock plan. Autopilot rejects
native model/profile/provider/reasoning settings, project/trust roots, working-
directory or remote-session switches, in-place updates, and arbitrary MCP
process definitions—including attached short flags and quoted TOML dotted keys.
The only MCP config passthrough accepted is a fail-closed narrowing of the `gco`
server to `find_docs`/`read_resource`, as used by the reviewed live recorder.
Use top-level `--model`, `--no-companions`, or `cdk.json` instead. A second
native `--` ends option scanning, so prompt text after it is preserved.

For resumed Codex sessions, Autopilot places its owned root policy first, then
`resume [--last|ID]`, then native options/prompt text. This matches Codex's
resume grammar; resume-only flags no longer land at the root parser.

## Dev-Container Persistence

The `gco` function emitted by `scripts/setup-dev-alias.sh` mounts:

- `gco-dev-tools` at `/root/.npm-global`, preserving either lazily installed
  agent CLI across `--rm` containers;
- host `~/.gco` at `/root/.gco`, preserving generated config, Codex's isolated
  home, skills, and Codex session state; and
- host `~/.claude` at `/root/.claude`, preserving Claude onboarding and
  transcripts.

The generated function also forwards `GCO_AUTOPILOT_ENGINE`,
`GCO_AUTOPILOT_MODEL`, `GCO_AUTOPILOT_CODEX_MODEL`,
`GCO_AUTOPILOT_SMALL_FAST_MODEL`, and `GCO_AUTOPILOT_CONFIG_DIR` by name in
both TTY branches for Docker, Finch, and Podman. A config-dir override must be
a writable container path (normally under `/root/.gco` or `/workspace`), not
an unrelated absolute host path.

The image itself contains neither agent CLI. CI launches two separate Codex
containers with the same mounts and proves both the binary and
`/root/.gco/autopilot/codex/config.toml` survive.

## Security Notes

- Every MCP server is a separate process with the caller's credential reach.
  Review the companion registry before using broad AWS credentials.
- The EKS companion is read-only by default.
- The MCP shell companion has a narrow allowlist:
  `ls,cat,pwd,grep,wc,touch,find`. Codex also has a separate built-in shell;
  use native `--disable shell_tool` when that capability must be absent.
- Claude uses a strict MCP config. Codex isolates personal state, disables the
  workspace project-config layer for each launch, and repeats Bedrock controls
  at session precedence; organization-managed policy still applies.
- Bedrock traffic uses ordinary AWS IAM and CloudTrail. Autopilot does not send
  prompts to Anthropic's or OpenAI's direct API.
- Native Codex project/provider/profile/reasoning/update overrides are blocked
  so dry-run, generated config, and effective execution cannot disagree.
- The live Codex demo is intentionally narrower than default Autopilot:
  `--no-companions`, required GCO startup, only `find_docs`/`read_resource`,
  built-in shell disabled, read-only sandbox, and no trust/approval prompts.

## Mission Compatibility

`global.openai.gpt-5.6-sol` is also supported as an explicit Mission sampling
model. Mission uses Bedrock Converse rather than Codex's Responses API. The
shared provider-aware request builder removes `temperature`, which this GPT
profile rejects, while leaving unrelated explicit-model controls intact.

The repository includes a live-captured three-directive playback fixture at
`tests/fixtures/scaffold_responses/global_openai_gpt_5_6_sol.json`. Every
capture is replayed through Mission's parse, normalize, autofix, and validation
pipeline. This is compatibility evidence for Mission, not a change to Mission's
separate default model.

## How It Is Tested and Kept Fresh

- `tests/test_cli_autopilot.py` covers engine resolution, model precedence,
  generated JSON/TOML, isolation, skills, install flow, resume mapping, and
  native-override rejection.
- `tests/test_autopilot_ci_contract.py` derives both pins, defaults, provider,
  reasoning, and config schemas from production modules.
- `unit:cli:autopilot` installs both real npm pins and verifies both binaries.
- The live recorder contract requires Codex to start only GCO, successfully call
  `find_docs` and `read_resource`, expose no shell/companions, show no trust or
  approval dialog, and contain none of the caller's AWS credential values.
- GIF validation fully decodes every frame and requires both Autopilot assets to
  open on a nonblank banner frame for static previews.
- `integration:docker:dev-container` validates both engines on amd64 and arm64
  and proves Codex install/config persistence across separate containers.
- The monthly dependency scan checks both lazy npm pins, companion package
  health, and every Bedrock default. Dotted OpenAI versions are compared in one
  model family, so a newer GPT release is advisory drift rather than an
  automatic model change.
- Mission's GPT fixture is a real Bedrock capture and participates in the full
  cross-model replay suite.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Selected engine not found after installation | Ensure `$(npm prefix -g)/bin` is on `PATH`. In `gco-dev`, rebuild or rerun `setup-dev-alias.sh` so `/root/.npm-global/bin` is present. |
| `FTUFormNotFilled` | Submit Anthropic's one-time use-case form. This applies to Claude, not the OpenAI GPT profile. |
| `AccessDeniedException` on first message | Enable the selected profile and grant `bedrock:InvokeModel` in the calling Region. |
| Codex reports a different provider/profile than dry-run | Autopilot blocks native/project overrides; inspect organization-managed Codex policy, which remains authoritative. |
| You expected `xhigh` after `--model` | Canonical reasoning is intentionally omitted for explicit model overrides. Review and change `context.bedrock.codex_*` instead. |
| Personal `~/.codex` settings are missing | Expected: Autopilot uses an isolated `CODEX_HOME`. Run `codex` directly for personal configuration. |
| Claude plugin or `--agents` rejected under Codex | Codex supports imported skills, not Claude plugin/agent formats. |
| Companion server fails to start | Run the selected engine with its debug options after `--`; check registry/network status and the generated config. |
| You need a permanently different MCP set | Use `--no-companions` or run the engine directly with your own MCP configuration. |
