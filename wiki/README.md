# Wiki sources

This directory is the source of the project's orientation wiki, the site
published at
<https://aws-solutions-library-samples.github.io/global-capacity-orchestrator-on-aws/>.
The pages are plain Markdown, built into a static site by
[Zensical](https://zensical.org/) — the static site generator from the team
behind Material for MkDocs — and deployed to GitHub Pages by a workflow that
runs after every successful `Unit Tests` run on `main`.

The wiki is an **orientation and routing layer** above the reference
documentation: each page summarizes a topic and links to the authoritative
README, `docs/` guide or package README on GitHub. It never restates reference
detail (flags, configuration keys, procedures) because duplicated reference
content rots, and the deep docs are the ones the CI freshness guards protect.

This README is for contributors browsing the repository. It is deliberately
**not** part of the published site: the staging step described below leaves it
out of the tree Zensical builds, and [`index.md`](index.md) is the site's home
page.

## Table of Contents

- [What is in this directory](#what-is-in-this-directory)
- [What the site adds from elsewhere](#what-the-site-adds-from-elsewhere)
- [How the site is assembled](#how-the-site-is-assembled)
- [How the site is published](#how-the-site-is-published)
- [What keeps it honest](#what-keeps-it-honest)
- [Working on the wiki](#working-on-the-wiki)
- [Further reading](#further-reading)

## What is in this directory

Every file here is one published page, except this README. The order below is
the site's navigation order, which lives in the `nav` list of
[`zensical.toml`](../zensical.toml).

| File | Published at | What it covers |
|------|--------------|----------------|
| [`index.md`](index.md) | `/` (Home) | The one-paragraph pitch — one API, every accelerator, any region — with a two-minute "try it", who GCO is for, what it costs, and where to go next. |
| [`how-it-works.md`](how-it-works.md) | `/how-it-works/` | The architecture story at fly-over altitude: the global control plane and per-region stacks, what lives inside a region, how a job flows through the platform, and the security posture; illustrated with the three reference-architecture diagrams. |
| [`get-started.md`](get-started.md) | `/get-started/` (For users) | The fast path from a clean machine to a running job in well under an hour — clone and build the dev container, a first success with no AWS charges, deploy, run a job, optionally deploy an inference endpoint, tear down — with the point where billing starts marked. |
| [`evaluating-and-deploying.md`](evaluating-and-deploying.md) | `/evaluating-and-deploying/` (For users) | The questions before and after that fast path: what a deployment needs, the deploy/upgrade/destroy lifecycle, what it costs, and what can be customized. |
| [`what-you-can-run.md`](what-you-can-run.md) | `/what-you-can-run/` (For users) | The workload surface, one category at a time — schedulers, distributed training, inference serving, observability and cost, optional interactive analytics, and the goal-directed mission loop — each backed by a ready-to-submit example manifest. |
| [`repo-tour.md`](repo-tour.md) | `/repo-tour/` (For developers) | Which door to open: every top-level directory with a condensed description and a link to its own README, plus a suggested first hour. |
| [`build-and-test.md`](build-and-test.md) | `/build-and-test/` (For developers) | CI at a glance — the workflows, the 100% coverage gate across Python, Bash and Node.js, the quality signals beyond tests, and how to run the same checks locally. |
| [`contributing.md`](contributing.md) | `/contributing/` | The shape of a good change, how to report bugs and request features, running your own copy (forking), and the community standards; routes into `CONTRIBUTING.md`. |
| `README.md` | not published | This file. |

## What the site adds from elsewhere

Zensical builds one directory (`docs_dir`), but the published site is drawn
from several places in the repository, and nothing may be copied by hand from
one to another. [`scripts/build_wiki.py`](../scripts/build_wiki.py) therefore
**stages** the site's source tree into `build/wiki` (gitignored) before every
build, and `zensical.toml` points `docs_dir` at that tree:

| Site path | Source | How it gets there |
|-----------|--------|-------------------|
| `/`, `/<page>/` | this directory | Every `wiki/*.md` except this README is staged as-is. |
| `/assets/images/<name>` | [`images/`](../images/README.md) | Every tracked screenshot (not the directory's README) is staged as `assets/images/<name>`. Pages reference that path; the screenshots stay single-source, and the strict build fails if a referenced one is missing. |
| `/api/` and `/api/<document>/` | [`diagrams/api_specs/`](../diagrams/api_specs/README.md) | The generated API spec sheets (two API Gateways, the in-cluster Gateway, four services) and the interaction diagram they embed are staged as `api/`. They are rendered from `docs/openapi/*.json` by `diagrams/api_specs/generate.py`, which refuses to let a stale sheet through (`--check`); the generator itself is not staged. |
| `/swagger/` | [`docs/openapi/`](../docs/openapi/README.md) | A Swagger UI console per OpenAPI document, built by the deploy workflow with `diagrams/api_specs/generate.py --swagger-ui-dir` and the pinned `swagger-ui-dist` package served from `/swagger/assets/`, so the site still makes zero third-party asset requests. |
| `/python-coverage/`, `/bash-coverage/`, `/nodejs-coverage/` | Artifacts of the `Unit Tests` and `Inference Streaming Proxy` runs for the same commit | Copied into the site tree by the deploy workflow — no re-run, no regeneration. The nav links them as full external URLs because they are not part of the Zensical build (locally they 404, by design). |
| `/coverage/` | the deploy workflow | A redirect to `/python-coverage/`, the Python report's old address, so links published before the reports were split keep resolving. |
| `/python-coverage-badge.json`, `/bash-coverage-badge.json`, `/nodejs-coverage-badge.json` | [`.github/scripts/render_coverage_badges.py`](../.github/scripts/render_coverage_badges.py) | The shields.io endpoint documents behind the three coverage badges in the root README, rendered from the three reports above and kept at the site root so the badge URLs never change. |

The staging step is a sync, not a wholesale copy: files are copied when
missing or changed, files whose source disappeared are removed, and the tree is
never deleted, so the preview server keeps watching a directory that exists.
MkDocs did the first two jobs through a build hook and kept this README out
with `exclude_docs`; Zensical supports neither, and the explicit staging step
is the testable replacement.

## How the site is assembled

[`zensical.toml`](../zensical.toml) at the repository root is the whole
configuration; its header comment explains every constraint. The parts that
matter most:

- `docs_dir = "build/wiki"` — the staged tree above, never this directory
  directly. `watch` lists the three real sources (`wiki`, `images`,
  `diagrams/api_specs`) so the preview server rebuilds when one changes.
  `nav` is the only place page order and titles live.
- `strict = true` — every validation warning is an error. A link to a page that
  does not exist or an anchor that does not exist fails the build, locally and
  in CI.
- `[project.theme]` uses Zensical's `modern` variant with `font = false` and no
  analytics, external CSS or JavaScript — the published site is self-contained
  and loads no third-party assets. Search is Zensical's built-in client-side
  index. (The header's repository facts — stars, latest tag — are the one
  request that leaves the site; `repo_url` enables them.)
- Light and dark palettes with a toggle, both in the project's deep-orange.
- `[project.markdown_extensions]` names every Python-Markdown extension the
  pages use. Zensical enables none by default when the table is present, so
  `tables` and the fenced-code pair (`pymdownx.superfences` with
  `pymdownx.highlight` for Pygments-highlighted code blocks) are listed
  alongside `admonition`, `attr_list`, `md_in_html` and `toc` with permalinks.
- `site_url` and `repo_url` — declared once here and read by the spec-sheet
  generator (for the links it renders), the SVG content policy (for the link
  targets it allows) and the wiki guard tests. On a fork,
  [`scripts/migrate_fork.py`](../scripts/migrate_fork.py) rewrites both the
  repository URL and the `<owner>.github.io/<repo>` Pages host throughout the
  tree, this file included.
- The file stays plain TOML so `tests/test_wiki.py` can read the nav with the
  standard library's `tomllib` and no Zensical import.

The toolchain is the `docs` extra in [`pyproject.toml`](../pyproject.toml):
`zensical`, pinned there and in `requirements-lock.txt` together with what it
brings (Pygments, Python-Markdown, pymdown-extensions). Nothing else is needed
for the wiki itself; the Swagger consoles additionally need the repository's
locked npm tooling (`npm ci --ignore-scripts`).

## How the site is published

The site is one GitHub Pages deployment produced by
[`.github/workflows/pages.yml`](../.github/workflows/pages.yml) (workflow
`Deploy Pages`, job `pages:deploy`). GitHub Pages via Actions supports exactly
one deployment per repository, which is why the wiki, the API consoles and the
coverage reports ship together. From a change to a live page:

1. **A pull request changes `wiki/`, `zensical.toml` or the staging script.**
   The `lint:zensical:strict` job in
   [`.github/workflows/lint.yml`](../.github/workflows/lint.yml) installs the
   `docs` extra and runs `python scripts/build_wiki.py build` — the identical
   command the deploy runs — so a broken wiki fails the PR (through the
   required `gate:lint` check) instead of the post-merge deploy.
   `tests/test_wiki.py`, `tests/test_build_wiki.py` and markdownlint run on the
   same PR.
2. **The PR merges and `Unit Tests` runs on `main`.** That run measures Python
   and shell coverage and uploads them as the `pytest-coverage` and
   `bash-coverage-report` artifacts; the `Inference Streaming Proxy` workflow
   for the same commit uploads `node-inference-streaming-proxy-coverage`.
3. **`Deploy Pages` fires.** It is triggered by `workflow_run` when `Unit
   Tests` completes, and only proceeds when that run succeeded, was a `push`
   to the default branch, and came from this repository (not a fork's PR
   head). It checks out exactly `workflow_run.head_sha`, so the site describes
   the commit the tests measured.
4. **The site tree is assembled.** `python scripts/build_wiki.py build` stages
   `build/wiki` and runs `zensical build --clean --strict` into `site/`; the
   locked npm toolchain is installed and `diagrams/api_specs/generate.py
   --swagger-ui-dir site/swagger` builds the consoles; the three coverage
   artifacts are downloaded (the Node.js one by locating the sibling run for
   the same commit) and moved to `site/python-coverage`, `site/bash-coverage`
   and `site/nodejs-coverage`; the `/coverage/` redirect stub is written; and
   `render_coverage_badges.py` writes the three badge JSON files at the site
   root.
5. **The tree is deployed.** `actions/upload-pages-artifact` packages `site/`
   and `actions/deploy-pages` publishes it. A `pages` concurrency group
   serializes deployments and never cancels one in flight.

Two properties of this design are worth knowing. Publishing is its own
workflow rather than a job inside `Unit Tests` so that a Pages outage or a
wiki build failure surfaces on `Deploy Pages` and never turns a green test run
red. And because `workflow_run` always uses the workflow definition from the
default branch, edits to `pages.yml` only take effect after they merge; the
PR-side strict build is what makes a wiki change safe before that.

## What keeps it honest

The wiki's most likely failure is referential: a renamed file breaks a link,
a page falls out of the nav, a screenshot rename orphans an image. Each of
those is a PR-time failure:

- **`zensical build --clean --strict`** (locally through
  `scripts/build_wiki.py build`, in `lint:zensical:strict`, and in the deploy)
  fails on any link to a page or anchor that does not exist, and on a nav
  entry without a file.
- **[`tests/test_wiki.py`](../tests/test_wiki.py)** pins the structure: the
  configuration builds the staged tree (`docs_dir` equals the directory
  `scripts/build_wiki.py` writes, `watch` names the three sources, `strict` is
  on, fonts are not fetched, and the MkDocs-only settings Zensical ignores are
  absent); the nav and the pages are one-to-one (the staged `api/` sheets
  included); the nav's only external entries are the canonical Pages addresses
  of the Swagger consoles and the three coverage reports, and `pages.yml`
  places a tree at each; every GitHub deep link resolves to a path in the
  checkout; relative links resolve to sibling pages or staged assets; no image
  comes from an external host; no page links to `docs/` relatively (it would
  404 on the built site); and this README is left out of the staged tree,
  absent from the nav, and documents every page in the directory.
- **[`tests/test_build_wiki.py`](../tests/test_build_wiki.py)** pins the
  staging step itself: what is copied, what is left out (both READMEs, the
  generator), what is removed when a source disappears, that an unchanged tree
  is untouched, and that the preview loop re-stages on change and shuts the
  server down cleanly. Its last test checks the real repository's mapping
  against the published pages, the tracked images and the shipped sheets.
- **[`tests/test_docs_coverage.py`](../tests/test_docs_coverage.py)** checks
  that every `gco …` command inside a wiki shell block exists in the CLI and
  that every `examples/…` path the wiki names is a shipped file.
- **[`tests/test_markdown_links.py`](../tests/test_markdown_links.py)**
  validates this README's own links (it is the one file in `wiki/` that
  resolves against the source tree rather than the built site).
- **markdownlint** (`npm run lint:markdown`, configured in
  [`.github/config/.markdownlint-cli2.yaml`](../.github/config/.markdownlint-cli2.yaml))
  lints every Markdown file in the repository, these pages included.
- The staged content carries its own guards: the spec sheets and the
  interaction diagram are byte-exact under `diagrams/api_specs/generate.py
  --check`, and every tracked SVG is held to the content policy in
  [`.github/scripts/validate_svg_assets.py`](../.github/scripts/validate_svg_assets.py)
  because each one is also published as its own document on the Pages origin.

## Working on the wiki

Preview locally with the same strict build CI runs, then a live-reloading
server:

```bash
pip install -e ".[docs]"                       # once, in your venv (or use the dev container)
python scripts/build_wiki.py serve             # stage, strict build, then serve on :8000
python scripts/build_wiki.py serve --port 9000 # another port
python scripts/build_wiki.py build             # just the CI-equivalent strict build into site/
```

[`scripts/build_wiki.py`](../scripts/build_wiki.py) stages the sources and
runs `zensical build --clean --strict` first, so anything that would fail CI
fails before you look at a page. While the server runs, the script re-stages
whenever a page, screenshot or spec sheet changes, and Zensical reloads the
browser. The coverage-report paths 404 locally by design (they are merged in at
deploy time). Before pushing, run the guards too:

```bash
pytest tests/test_wiki.py tests/test_build_wiki.py tests/test_docs_coverage.py tests/test_markdown_links.py -q
npm run lint:markdown
```

To add a page:

1. Create `wiki/<slug>.md` with a single `#` heading; the file name becomes
   the URL (`/<slug>/`).
2. Add it to `nav` in [`zensical.toml`](../zensical.toml) under the right
   section; the strict build and `tests/test_wiki.py` both fail until you do.
3. Add its row to the table in this README; `tests/test_wiki.py` fails until
   you do.
4. Follow the content contract: summarize and link, never restate. Deep links
   to `README.md`, `docs/` and package READMEs are full
   `https://github.com/…/blob/main/…` URLs (those files are not on the site);
   images are `assets/images/<name>` and the file must exist in `images/`;
   sibling pages are linked by file name (`get-started.md`).

`CONTRIBUTING.md` has the same guidance in context under
[Developing the wiki](../CONTRIBUTING.md#developing-the-wiki).

## Further reading

- [Zensical documentation](https://zensical.org/docs/) — the
  [basics](https://zensical.org/docs/setup/basics/) (`docs_dir`, `site_url`,
  `watch`), [navigation](https://zensical.org/docs/setup/navigation/),
  [validation and strict mode](https://zensical.org/docs/setup/validation/),
  [colors](https://zensical.org/docs/setup/colors/) and
  [fonts](https://zensical.org/docs/setup/fonts/), the supported
  [Python-Markdown extensions](https://zensical.org/docs/compatibility/markdown/python-markdown/),
  and the [`build`](https://zensical.org/docs/usage/build/) and
  [`serve`](https://zensical.org/docs/usage/preview/) commands.
- [Migrating from MkDocs](https://zensical.org/docs/compatibility/mkdocs/migration/)
  — including the settings Zensical does not support (`hooks`, `exclude_docs`),
  which is why the staging step exists.
- [Publishing to GitHub Pages with a custom GitHub Actions workflow](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site#publishing-with-a-custom-github-actions-workflow)
  — the deployment model `pages.yml` uses.
- [`.github/CI.md`](../.github/CI.md) — the CI reference, including the
  `Deploy Pages` workflow and the `gate:lint` check that carries the strict
  build.
