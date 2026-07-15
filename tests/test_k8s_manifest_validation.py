"""Tests for ``.github/scripts/validate_k8s_manifests.py``.

The validator gates a CI job (``integration:k8s:manifest-schema`` in
``integration-tests.yml``) that schema-validates every hand-authored
Kubernetes manifest — the kubectl-applier's own manifests and the
``examples/`` gallery — with kubeconform, not just a YAML-syntax check. These
tests pin the offline behavior (placeholder rendering, file selection, failure
parsing, the binary-missing exit path) so a refactor can't quietly relax the
rules, and add an opt-in online tier that exercises the real ``kubeconform``
binary against the committed manifests.

The script is loaded by file path because ``.github/scripts/`` isn't on
``sys.path`` and shouldn't be turned into a package just to support tests —
same posture as ``tests/test_helm_charts_validation.py`` and
``tests/test_pip_audit_ignore_validator.py``.

Two tiers:

* **Offline** (always run): placeholder rendering, target-file selection,
  ``format_failures`` parsing, and the ``main()`` binary-missing exit code —
  all against in-memory strings, temp files, and the committed manifests. No
  network, no ``kubeconform`` binary.
* **Online** (opt-in): gated behind ``GCO_KUBECONFORM_VALIDATION=1`` *and* a
  ``kubeconform`` binary on ``PATH``, so the network-bound schema pass never
  runs in the normal unit job. The dedicated CI job installs the pinned
  binary and sets the env var.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / ".github" / "scripts" / "validate_k8s_manifests.py"
MANIFESTS_DIR = PROJECT_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
EXAMPLES_DIR = PROJECT_ROOT / "examples"


def _load_validator():
    """Load the validator module by file path.

    ``.github/scripts`` is intentionally not a Python package, so import by
    path rather than adding an ``__init__.py`` — mirrors the helm-charts /
    pip-audit validator tests.
    """
    spec = importlib.util.spec_from_file_location("validate_k8s_manifests", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator()


# ── render_placeholders ───────────────────────────────────────────────────────


class TestRenderPlaceholders:
    def test_generic_token_becomes_string_stub(self) -> None:
        out = validator.render_placeholders("image: {{HEALTH_MONITOR_IMAGE}}")
        assert "{{" not in out
        assert validator._GENERIC_STUB in out

    def test_integer_token_becomes_bare_integer(self) -> None:
        # pollingInterval sits in a bare numeric scalar position — the stub
        # must be a bare int, not a quoted string, or the schema's
        # type: integer check would reject it.
        out = validator.render_placeholders("pollingInterval: {{QP_POLLING_INTERVAL}}")
        assert out == "pollingInterval: 1"

    def test_all_integer_tokens_covered(self) -> None:
        for token in validator._INTEGER_PLACEHOLDER_TOKENS:
            out = validator.render_placeholders(f"field: {token}")
            assert out == "field: 1", f"{token} did not render to a bare integer"

    def test_structural_cidr_token_becomes_ipblock_sequence(self) -> None:
        # {{VPC_ENDPOINT_CIDR_BLOCKS}} sits at the head of a YAML sequence, so
        # the stub must be a real list item, not a scalar.
        out = validator.render_placeholders("      to:\n        {{VPC_ENDPOINT_CIDR_BLOCKS}}")
        assert "ipBlock" in out
        assert "{{" not in out

    def test_text_without_placeholders_is_unchanged(self) -> None:
        text = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: gco-jobs\n"
        assert validator.render_placeholders(text) == text

    def test_multiple_distinct_tokens_all_replaced(self) -> None:
        out = validator.render_placeholders("a: {{ONE}}\nb: {{TWO}}\nc: {{THREE}}")
        assert "{{" not in out

    @pytest.mark.parametrize(
        "manifest_name",
        [
            "03-network-policies.yaml",  # the structural VPC CIDR placeholder
            "30-health-monitor.yaml",  # bare image: placeholder + quoted values
            "post-helm-sqs-consumer.yaml",  # the bare integer placeholders
            "04-resource-quotas.yaml",  # quoted-string placeholders only
        ],
    )
    def test_real_templated_manifest_renders_to_parseable_yaml(self, manifest_name: str) -> None:
        # The whole point of rendering (vs skipping templated files): the
        # result must parse so kubeconform can schema-check it.
        raw = (MANIFESTS_DIR / manifest_name).read_text(encoding="utf-8")
        assert "{{" in raw, "fixture drifted: expected a templated manifest"
        rendered = validator.render_placeholders(raw)
        assert "{{" not in rendered
        docs = list(yaml.safe_load_all(rendered))
        assert any(doc for doc in docs), "rendered manifest produced no YAML documents"


# ── iter_target_files (live, offline) ─────────────────────────────────────────


class TestIterTargetFiles:
    """Selection logic exercised against the real repo directories."""

    def test_returns_only_yaml_files(self) -> None:
        files = validator.iter_target_files()
        assert files, "expected to find manifests under the default target dirs"
        assert all(f.suffix in (".yaml", ".yml") for f in files)

    def test_excludes_non_manifest_pipeline_dag(self) -> None:
        names = {f.name for f in validator.iter_target_files()}
        assert "pipeline-dag.yaml" not in names

    def test_excludes_non_yaml_json_fixtures(self) -> None:
        # examples/ also ships *.json (metric criteria, trainer state) that are
        # not manifests; the *.yaml/*.yml glob must never pick them up.
        names = {f.name for f in validator.iter_target_files()}
        assert not any(n.endswith(".json") for n in names)
        assert "megatrain-trainer-state.json" not in names

    def test_includes_known_real_manifests(self) -> None:
        names = {f.name for f in validator.iter_target_files()}
        assert "simple-job.yaml" in names  # a real example manifest
        assert "00-namespaces.yaml" in names  # a real kubectl-applier manifest

    def test_real_dag_step_files_are_included(self) -> None:
        # dag-step-*.yaml ARE real Job manifests (only pipeline-dag.yaml is the
        # non-K8s DAG definition) — guard against over-broad exclusion.
        names = {f.name for f in validator.iter_target_files()}
        assert "dag-step-preprocess.yaml" in names

    def test_missing_directory_is_skipped(self) -> None:
        # A target dir that doesn't exist yields nothing rather than raising.
        assert validator.iter_target_files(("does/not/exist",)) == []


# ── render_tree ────────────────────────────────────────────────────────────────


class TestRenderTree:
    def test_non_templated_file_copied_verbatim(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        content = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: x\n"
        (src / "plain.yaml").write_text(content)
        dest = tmp_path / "out"
        validator.render_tree([src / "plain.yaml"], dest)
        assert (dest / "plain.yaml").read_text() == content

    def test_templated_file_is_rendered(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "tmpl.yaml").write_text("image: {{SOME_IMAGE}}\n")
        dest = tmp_path / "out"
        validator.render_tree([src / "tmpl.yaml"], dest)
        rendered = (dest / "tmpl.yaml").read_text()
        assert "{{" not in rendered

    def test_dest_created_if_absent(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.yaml").write_text("kind: Namespace\n")
        dest = tmp_path / "nested" / "out"
        validator.render_tree([src / "a.yaml"], dest)
        assert (dest / "a.yaml").exists()


# ── format_failures ────────────────────────────────────────────────────────────


class TestFormatFailures:
    def test_invalid_and_error_are_failures(self) -> None:
        result = {
            "resources": [
                {
                    "filename": "a.yaml",
                    "kind": "Pod",
                    "name": "p",
                    "status": "statusInvalid",
                    "msg": "bad",
                },
                {
                    "filename": "b.yaml",
                    "kind": "",
                    "name": "",
                    "status": "statusError",
                    "msg": "missing kind",
                },
            ]
        }
        failures = validator.format_failures(result)
        assert len(failures) == 2
        assert any("a.yaml" in f and "Pod" in f for f in failures)
        assert any("b.yaml" in f for f in failures)

    def test_valid_and_skipped_are_not_failures(self) -> None:
        result = {
            "resources": [
                {
                    "filename": "a.yaml",
                    "kind": "Pod",
                    "name": "p",
                    "status": "statusValid",
                    "msg": "",
                },
                {
                    "filename": "b.yaml",
                    "kind": "RayCluster",
                    "name": "r",
                    "status": "statusSkipped",
                    "msg": "",
                },
            ]
        }
        assert validator.format_failures(result) == []

    def test_empty_result_is_no_failures(self) -> None:
        assert validator.format_failures({}) == []
        assert validator.format_failures({"resources": []}) == []


# ── main(): binary-missing exit path (offline) ────────────────────────────────


class TestMainBinaryMissing:
    def test_missing_binary_returns_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        # main() must fail loudly (exit 2) when kubeconform isn't on PATH,
        # rather than silently reporting success.
        rc = validator.main(["--kubeconform-binary", "kubeconform-does-not-exist-xyz"])
        assert rc == 2
        assert "not found on PATH" in capsys.readouterr().err


# ── module-level constants (guard the design contract) ────────────────────────


class TestConstants:
    def test_pipeline_dag_is_excluded_by_name(self) -> None:
        assert "pipeline-dag.yaml" in validator.NON_MANIFEST_FILENAMES

    def test_ray_and_volcano_skipped_by_gvk(self) -> None:
        # GVK-qualified (not bare-Kind) so Volcano's Job doesn't also skip
        # every built-in batch/v1 Job.
        assert "ray.io/v1/RayCluster" in validator.SCHEMA_UNAVAILABLE_SKIPS
        assert "batch.volcano.sh/v1alpha1/Job" in validator.SCHEMA_UNAVAILABLE_SKIPS
        assert all("/" in gvk for gvk in validator.SCHEMA_UNAVAILABLE_SKIPS)

    def test_crd_catalog_is_datree_templated_url(self) -> None:
        loc = validator.CRD_CATALOG_SCHEMA_LOCATION
        assert loc.endswith(".json")
        assert "datreeio/CRDs-catalog" in loc


# ── live manifests (online, opt-in) ────────────────────────────────────────────

_KUBECONFORM = shutil.which("kubeconform")
_ONLINE_ENABLED = os.environ.get("GCO_KUBECONFORM_VALIDATION") == "1" and _KUBECONFORM is not None


@pytest.mark.kubeconform_online
@pytest.mark.skipif(
    not _ONLINE_ENABLED,
    reason=(
        "opt-in: set GCO_KUBECONFORM_VALIDATION=1 and install kubeconform to run "
        "the online schema checks"
    ),
)
class TestLiveManifestsOnline:
    """Real ``kubeconform`` schema validation of the committed manifests.

    Opt-in, network-bound (fetches upstream + CRD-catalog schemas).
    """

    def test_all_committed_manifests_are_schema_valid(self) -> None:
        rc = validator.main(["--verbose"])
        assert rc == 0

    def test_detects_a_schema_violation(self, tmp_path: Path) -> None:
        # Give kubeconform a manifest with an unknown field under -strict and
        # confirm it's reported — proves the online gate has teeth.
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "bad-pod.yaml").write_text(
            "apiVersion: v1\nkind: Pod\nmetadata:\n  name: bad\nspec:\n"
            "  containers:\n  - name: x\n    imageBogusField: nginx\n"
        )
        rc, result = validator.run_kubeconform(bad)
        assert rc != 0
        failures = validator.format_failures(result)
        assert failures
        assert any("bad-pod.yaml" in f for f in failures)
