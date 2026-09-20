# Wiki sources

This directory is the source of the project's orientation wiki, the site
published at
<https://aws-solutions-library-samples.github.io/global-capacity-orchestrator-on-aws/>.
The pages are plain Markdown, built into a static site by
[MkDocs](https://www.mkdocs.org/) with the
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) theme, and
deployed to GitHub Pages by a workflow that runs after every successful
`Unit Tests` run on `main`.

The wiki is an **orientation and routing layer** above the reference
documentation: each page summarizes a topic and links to the authoritative
README, `docs/` guide or package README on GitHub. It never restates reference
detail (flags, configuration keys, procedures) because duplicated reference
content rots, and the deep docs are the ones the CI freshness guards protect.

This README is for contributors browsing the repository. It is deliberately
**not** part of the published site: [`mkdocs.yml`](../mkdocs.yml) excludes it,
and [`index.md`](index.md) is the site's home page.

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
the site's navigation order, which lives in the `nav:` block of
[`mkdocs.yml`](../mkdocs.yml).

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

MkDocs only serves files under `docs_dir`, which is this directory. Several
parts of the published site come from other places in the repository, or are
merged in at deploy time, so that nothing is copied by hand and nothing can
drift from its source:

| Site path | Source | How it gets there |
|-----------|--------|-------------------|
| `/assets/images/<name>` | [`images/`](../images/README.md) | Injected into the MkDocs build by [`scripts/mkdocs_hooks.py`](../scripts/mkdocs_hooks.py). Pages reference `assets/images/<name>`; the screenshots stay single-source, and the strict build verifies each one exists. |
| `/api/` and `/api/<document>/` | [`diagrams/api_specs/`](../diagrams/api_specs/README.md) | The generated API spec sheets (two API Gateways, the in-cluster Gateway, four services) and the interaction diagram they embed, injected by the same hook. Rendered from `docs/openapi/*.json` by `diagrams/api_specs/generate.py`, which refuses to let a stale sheet through (`--check`). |
| `/swagger/` | [`docs/openapi/`](../docs/openapi/README.md) | A Swagger UI console per OpenAPI document, built by the deploy workflow with `diagrams/api_specs/generate.py --swagger-ui-dir` and the pinned `swagger-ui-dist` package served from `/swagger/assets/`, so the site still makes zero third-party requests. |
| `/python-coverage/`, `/bash-coverage/`, `/nodejs-coverage/` | Artifacts of the `Unit Tests` and `Inference Streaming Proxy` runs for the same commit | Copied into the site tree by the deploy workflow — no re-run, no regeneration. The nav links them as full external URLs because they are not part of the MkDocs build (locally they 404, by design). |
| `/coverage/` | the deploy workflow | A redirect to `/python-coverage/`, the Python report's old address, so links published before the reports were split keep resolving. |
| `/python-coverage-badge.json`, `/bash-coverage-badge.json`, `/nodejs-coverage-badge.json` | [`.github/scripts/render_coverage_badges.py`](../.github/scripts/render_coverage_badges.py) | The shields.io endpoint documents behind the three coverage badges in the root README, rendered from the three reports above and kept at the site root so the badge URLs never change. |

## How the site is assembled

[`mkdocs.yml`](../mkdocs.yml) at the repository root is the whole
configuration; its header comment explains every constraint. The parts that
matter most:

- `docs_dir: wiki` — this directory is the source; `nav:` is the only place
  page order and titles live, and `exclude_docs` keeps this README out of the
  build (MkDocs would otherwise treat it as a second home page and refuse the
  conflict with `index.md`).
- `strict: true` — every warning is an error. A broken link, an image that
  does not exist, or a nav entry without a file fails the build, locally and
  in CI.
- `theme: material` with `font: false` and no analytics, external CSS or
  JavaScript — the published site is self-contained and makes no third-party
  requests. Search is Material's built-in client-side index.
- `hooks: [scripts/mkdocs_hooks.py]` — the
  [hook](https://www.mkdocs.org/user-guide/configuration/#hooks) whose
  `on_files` registers the `images/` tree as `assets/images/` and the API spec
  sheets as `api/` (see the table above), using `File.generated` so the build
  copies the real on-disk files instead of duplicates.
- `site_url` and `repo_url` — declared once here and read by the spec-sheet
  generator (for the links it renders) and by the wiki guard tests. On a
  fork, [`scripts/migrate_fork.py`](../scripts/migrate_fork.py) rewrites both
  the repository URL and the `<owner>.github.io/<repo>` Pages host throughout
  the tree, this file included.
- The Markdown extensions are the stock set (`admonition`, `attr_list`,
  `md_in_html`, `toc` with permalinks). The file stays plain YAML — no
  `!!python/name:` tags — so `tests/test_wiki.py` can read the nav with
  `yaml.safe_load` and no MkDocs import.

The toolchain is the `docs` extra in [`pyproject.toml`](../pyproject.toml):
`mkdocs` and `mkdocs-material`, pinned there and in `requirements-lock.txt`.
Nothing else is needed for the wiki itself; the Swagger consoles additionally
need the repository's locked npm tooling (`npm ci --ignore-scripts`).

## How the site is published

The site is one GitHub Pages deployment produced by
[`.github/workflows/pages.yml`](../.github/workflows/pages.yml) (workflow
`Deploy Pages`, job `pages:deploy`). GitHub Pages via Actions supports exactly
one deployment per repository, which is why the wiki, the API consoles and the
coverage reports ship together. From a change to a live page:

1. **A pull request changes `wiki/`, `mkdocs.yml` or the hook.** The
   `lint:mkdocs:strict` job in
   [`.github/workflows/lint.yml`](../.github/workflows/lint.yml) installs the
   `docs` extra and runs `mkdocs build --strict` — the identical build the
   deploy runs — so a broken wiki fails the PR (through the required
   `gate:lint` check) instead of the post-merge deploy. `tests/test_wiki.py`
   and markdownlint run on the same PR.
2. **The PR merges and `Unit Tests` runs on `main`.** That run measures Python
   and shell coverage and uploads them as the `pytest-coverage` and
   `bash-coverage-report` artifacts; the `Inference Streaming Proxy` workflow
   for the same commit uploads `node-inference-streaming-proxy-coverage`.
3. **`Deploy Pages` fires.** It is triggered by `workflow_run` when `Unit
   Tests` completes, and only proceeds when that run succeeded, was a `push`
   to the default branch, and came from this repository (not a fork's PR
   head). It checks out exactly `workflow_run.head_sha`, so the site describes
   the commit the tests measured.
4. **The site tree is assembled.** `mkdocs build --strict` builds the wiki
   (with the injected images and spec sheets) into `site/`; the locked npm
   toolchain is installed and `diagrams/api_specs/generate.py --swagger-ui-dir
   site/swagger` builds the consoles; the three coverage artifacts are
   downloaded (the Node.js one by locating the sibling run for the same
   commit) and moved to `site/python-coverage`, `site/bash-coverage` and
   `site/nodejs-coverage`; the `/coverage/` redirect stub is written; and
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

- **`mkdocs build --strict`** (locally, in `lint:mkdocs:strict`, and in the
  deploy) fails on any broken relative link, missing image, or nav entry
  without a file.
- **[`tests/test_wiki.py`](../tests/test_wiki.py)** pins the structure: the
  nav and the pages are one-to-one (the injected `api/` sheets included);
  the nav's only external entries are the canonical Pages addresses of the
  Swagger consoles and the three coverage reports, and `pages.yml` places a
  tree at each; every GitHub deep link resolves to a path in the checkout;
  relative links resolve to sibling pages or injected assets; no image comes
  from an external host; no page links to `docs/` relatively (it would 404 on
  the built site); and this README is excluded from the build, absent from the
  nav, and documents every page in the directory.
- **[`tests/test_docs_coverage.py`](../tests/test_docs_coverage.py)** checks
  that every `gco …` command inside a wiki shell block exists in the CLI and
  that every `examples/…` path the wiki names is a shipped file.
- **[`tests/test_markdown_links.py`](../tests/test_markdown_links.py)**
  validates this README's own links (it is the one file in `wiki/` that
  resolves against the source tree rather than the built site).
- **markdownlint** (`npm run lint:markdown`, configured in
  [`.github/config/.markdownlint-cli2.yaml`](../.github/config/.markdownlint-cli2.yaml))
  lints every Markdown file in the repository, these pages included.
- The injected content carries its own guards: the spec sheets and the
  interaction diagram are byte-exact under `diagrams/api_specs/generate.py
  --check`, and every tracked SVG is held to the content policy in
  [`.github/scripts/validate_svg_assets.py`](../.github/scripts/validate_svg_assets.py)
  because each one is also published as its own document on the Pages origin.

## Working on the wiki

Preview locally with the same strict build CI runs, then a live-reloading
server:

```bash
pip install -e ".[docs]"                  # once, in your venv (or use the dev container)
./scripts/preview_wiki.sh                 # strict build, then serve on :8000
./scripts/preview_wiki.sh --build-only    # just the CI-equivalent strict build
```

[`scripts/preview_wiki.sh`](../scripts/preview_wiki.sh) runs `mkdocs build
--strict` first, so anything that would fail CI fails before you look at a
page. The coverage-report paths 404 locally by design (they are merged in at
deploy time). Before pushing, run the guards too:

```bash
pytest tests/test_wiki.py tests/test_docs_coverage.py tests/test_markdown_links.py -q
npm run lint:markdown
```

To add a page:

1. Create `wiki/<slug>.md` with a single `#` heading; the file name becomes
   the URL (`/<slug>/`).
2. Add it to `nav:` in [`mkdocs.yml`](../mkdocs.yml) under the right section;
   the strict build and `tests/test_wiki.py` both fail until you do.
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

- [MkDocs user guide](https://www.mkdocs.org/user-guide/) — writing pages,
  the [configuration reference](https://www.mkdocs.org/user-guide/configuration/)
  (`docs_dir`, `nav`, `strict`, `exclude_docs`,
  [hooks](https://www.mkdocs.org/user-guide/configuration/#hooks)), and the
  [`build` and `serve` commands](https://www.mkdocs.org/user-guide/cli/).
- [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/) — the
  theme; the [setup pages](https://squidfunk.github.io/mkdocs-material/setup/)
  document the `palette` and `features` keys used in `mkdocs.yml`.
- [Publishing to GitHub Pages with a custom GitHub Actions workflow](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site#publishing-with-a-custom-github-actions-workflow)
  — the deployment model `pages.yml` uses.
- [`.github/CI.md`](../.github/CI.md) — the CI reference, including the
  `Deploy Pages` workflow and the `gate:lint` check that carries the strict
  build.
