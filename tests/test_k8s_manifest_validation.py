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


class TestBuiltInNodePoolContracts:
    def test_cpu_general_pool_does_not_mislabel_arm_nodes_as_x86(self) -> None:
        manifest = yaml.safe_load(
            (MANIFESTS_DIR / "45-nodepool-cpu-general.yaml").read_text(encoding="utf-8")
        )
        template = manifest["spec"]["template"]
        labels = template["metadata"]["labels"]
        arch_requirement = next(
            requirement
            for requirement in template["spec"]["requirements"]
            if requirement["key"] == "kubernetes.io/arch"
        )

        assert set(arch_requirement["values"]) == {"amd64", "arm64"}
        assert "arch" not in labels


class TestStaticPodTokenBoundaries:
    @staticmethod
    def _documents(filename: str) -> list[dict]:
        text = (MANIFESTS_DIR / filename).read_text(encoding="utf-8")
        rendered = validator.render_placeholders(text)
        return [document for document in yaml.safe_load_all(rendered) if document]

    def test_shared_inference_service_account_disables_api_token(self) -> None:
        accounts = {
            document["metadata"]["namespace"]: document
            for document in self._documents("01-serviceaccounts.yaml")
            if document["kind"] == "ServiceAccount"
        }
        assert accounts["gco-jobs"]["automountServiceAccountToken"] is False
        assert accounts["gco-inference"]["automountServiceAccountToken"] is False
        assert accounts["gco-inference"]["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]

    def test_cost_monitor_disables_api_token_but_keeps_sts_projection(self) -> None:
        documents = self._documents("34-cost-monitor.yaml")
        account = next(document for document in documents if document["kind"] == "ServiceAccount")
        deployment = next(document for document in documents if document["kind"] == "Deployment")
        pod = deployment["spec"]["template"]["spec"]

        assert account["automountServiceAccountToken"] is False
        assert pod["automountServiceAccountToken"] is False
        assert pod["serviceAccountName"] == "gco-cost-monitor-sa"

        container = pod["containers"][0]
        environment = {entry["name"]: entry["value"] for entry in container["env"]}
        role_arn = account["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]
        token_directory = "/var/run/secrets/eks.amazonaws.com/serviceaccount"
        assert environment["AWS_ROLE_ARN"] == role_arn
        assert environment["AWS_WEB_IDENTITY_TOKEN_FILE"] == f"{token_directory}/token"

        token_mount = next(
            mount for mount in container["volumeMounts"] if mount["name"] == "aws-iam-token"
        )
        assert token_mount["mountPath"] == token_directory
        assert token_mount["readOnly"] is True
        token_source = next(
            volume["projected"]["sources"][0]["serviceAccountToken"]
            for volume in pod["volumes"]
            if volume["name"] == "aws-iam-token"
        )
        assert token_source == {
            "audience": "sts.amazonaws.com",
            "expirationSeconds": 86400,
            "path": "token",
        }


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

    def test_all_quantity_tokens_render_as_strings_when_quoted(self) -> None:
        for token in validator._QUANTITY_PLACEHOLDER_TOKENS:
            out = validator.render_placeholders(f'field: "{token}"')
            assert yaml.safe_load(out)["field"] == "1", (
                f"{token} did not render to a quoted Kubernetes quantity"
            )

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
            "31-manifest-processor.yaml",  # REST policy env wiring
            "33-inference-proxy.yaml",  # quoted Quantity + bare integer HPA target
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

    def test_inference_tls_cpu_placeholders_render_with_kubernetes_types(self) -> None:
        raw = (MANIFESTS_DIR / "33-inference-proxy.yaml").read_text(encoding="utf-8")
        documents = list(yaml.safe_load_all(validator.render_placeholders(raw)))
        deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
        hpa = next(doc for doc in documents if doc["kind"] == "HorizontalPodAutoscaler")

        containers = {
            container["name"]: container
            for container in deployment["spec"]["template"]["spec"]["containers"]
        }
        request = containers["api-tls-proxy"]["resources"]["requests"]["cpu"]
        tls_metric = next(
            metric
            for metric in hpa["spec"]["metrics"]
            if metric["containerResource"]["container"] == "api-tls-proxy"
        )
        target = tls_metric["containerResource"]["target"]["averageUtilization"]

        assert request == "1"
        assert type(request) is str
        assert target == 1
        assert type(target) is int

    def test_manifest_processor_security_policy_env_is_fully_wired(self) -> None:
        raw = (MANIFESTS_DIR / "31-manifest-processor.yaml").read_text(encoding="utf-8")
        # Only the image token occupies an unquoted YAML scalar. Preserve the
        # quoted env tokens so this test can assert their exact wiring.
        docs = list(
            yaml.safe_load_all(
                raw.replace("{{MANIFEST_PROCESSOR_IMAGE}}", "example.invalid/manifest:test")
            )
        )
        deployment = next(doc for doc in docs if doc and doc.get("kind") == "Deployment")
        env = {
            item["name"]: item.get("value")
            for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
        }

        expected = {
            "VALIDATION_ENABLED": "{{MP_VALIDATION_ENABLED}}",
            "YAML_MAX_DEPTH": "{{MP_YAML_MAX_DEPTH}}",
            "BLOCK_PRIVILEGED": "{{MP_BLOCK_PRIVILEGED}}",
            "BLOCK_PRIVILEGE_ESCALATION": "{{MP_BLOCK_PRIVILEGE_ESCALATION}}",
            "BLOCK_HOST_NETWORK": "{{MP_BLOCK_HOST_NETWORK}}",
            "BLOCK_HOST_PID": "{{MP_BLOCK_HOST_PID}}",
            "BLOCK_HOST_IPC": "{{MP_BLOCK_HOST_IPC}}",
            "BLOCK_HOST_PATH": "{{MP_BLOCK_HOST_PATH}}",
            "BLOCK_ADDED_CAPABILITIES": "{{MP_BLOCK_ADDED_CAPABILITIES}}",
            "BLOCK_RUN_AS_ROOT": "{{MP_BLOCK_RUN_AS_ROOT}}",
        }
        assert {name: env.get(name) for name in expected} == expected


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
        # Compatibility wrapper still returns valid files only; detailed callers
        # use collect_target_files() to surface the missing-input error.
        assert validator.iter_target_files(("does/not/exist",)) == []


class TestCollectTargetFiles:
    def test_existing_file_is_kept_when_another_input_is_missing(self, tmp_path: Path) -> None:
        manifest = tmp_path / "valid.yaml"
        manifest.write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: valid\n")

        files, errors = validator.collect_target_files(
            (str(manifest), str(tmp_path / "missing.yaml"))
        )

        assert files == [manifest.resolve()]
        assert len(errors) == 1
        assert "missing.yaml: path does not exist" in errors[0]

    def test_explicit_non_yaml_file_is_reported(self, tmp_path: Path) -> None:
        text_file = tmp_path / "notes.txt"
        text_file.write_text("not yaml")
        files, errors = validator.collect_target_files((str(text_file),))
        assert files == []
        assert errors == [f"{text_file}: explicit file is not .yaml or .yml"]

    def test_empty_explicit_directory_is_reported(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        files, errors = validator.collect_target_files((str(empty),))
        assert files == []
        assert errors == [f"{empty}: directory contains no Kubernetes YAML manifests"]

    def test_quoted_recursive_glob_is_supported(self, tmp_path: Path) -> None:
        nested = tmp_path / "nested"
        nested.mkdir()
        manifest = nested / "job.yaml"
        manifest.write_text("apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: x\n")
        files, errors = validator.collect_target_files((str(tmp_path / "**" / "*.yaml"),))
        assert files == [manifest.resolve()]
        assert errors == []

    def test_duplicate_inputs_preserve_first_seen_order(self, tmp_path: Path) -> None:
        manifest = tmp_path / "a.yaml"
        manifest.write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: a\n")
        files, errors = validator.collect_target_files((str(manifest), str(manifest)))
        assert files == [manifest.resolve()]
        assert errors == []


# ── render_tree ────────────────────────────────────────────────────────────────


class TestRenderTree:
    def test_non_templated_file_copied_verbatim(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        content = "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: x\n"
        (src / "plain.yaml").write_text(content)
        dest = tmp_path / "out"
        rendered_paths = validator.render_tree([src / "plain.yaml"], dest)
        assert rendered_paths[0].read_text() == content

    def test_templated_file_is_rendered(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "tmpl.yaml").write_text("image: {{SOME_IMAGE}}\n")
        dest = tmp_path / "out"
        rendered_path = validator.render_tree([src / "tmpl.yaml"], dest)[0]
        rendered = rendered_path.read_text()
        assert "{{" not in rendered

    def test_dest_created_if_absent(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.yaml").write_text("kind: Namespace\n")
        dest = tmp_path / "nested" / "out"
        rendered_paths = validator.render_tree([src / "a.yaml"], dest)
        assert rendered_paths[0].exists()

    def test_repo_relative_paths_are_preserved(self, tmp_path: Path) -> None:
        dest = tmp_path / "out"
        source = EXAMPLES_DIR / "simple-job.yaml"
        rendered_path = validator.render_tree([source], dest)[0]
        assert rendered_path == dest / "examples" / "simple-job.yaml"


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


class TestKubeconformOutputIntegrity:
    @pytest.mark.parametrize(
        "result",
        [
            pytest.param({}, id="blank-or-malformed-json"),
            pytest.param([], id="non-object-json"),
            pytest.param(
                {
                    "resources": [],
                    "summary": {"valid": 0, "invalid": 0, "errors": 0, "skipped": 0},
                },
                id="empty-results",
            ),
            pytest.param(
                {
                    "resources": [
                        {
                            "filename": "one.yaml",
                            "kind": "Namespace",
                            "name": "one",
                            "status": "statusValid",
                            "msg": "",
                        }
                    ],
                    "summary": {"valid": 1, "invalid": 0, "errors": 0, "skipped": 0},
                },
                id="partial-results",
            ),
        ],
    )
    def test_zero_exit_with_unusable_json_fails_closed(
        self,
        result: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for name in ("one", "two"):
            (tmp_path / f"{name}.yaml").write_text(
                f"apiVersion: v1\nkind: Namespace\nmetadata:\n  name: {name}\n",
                encoding="utf-8",
            )
        monkeypatch.setattr(validator.shutil, "which", lambda _binary: "/usr/bin/kubeconform")
        monkeypatch.setattr(validator, "run_kubeconform", lambda *_args, **_kwargs: (0, result))

        rc = validator.main(["--path", str(tmp_path), "--kubeconform-binary", "fake"])

        assert rc == 2
        assert "unusable JSON output" in capsys.readouterr().err

    def test_v080_separator_pseudo_record_is_ignored(self) -> None:
        result = {
            "resources": [
                {
                    "filename": "one.yaml",
                    "kind": "",
                    "name": "",
                    "version": "",
                    "status": "",
                    "msg": "",
                },
                {
                    "filename": "one.yaml",
                    "kind": "Namespace",
                    "name": "one",
                    "version": "v1",
                    "status": "statusValid",
                    "msg": "",
                },
            ],
            "summary": {"valid": 1, "invalid": 0, "errors": 0, "skipped": 0},
        }

        assert (
            validator.validate_kubeconform_output(
                result,
                expected_filenames={"one.yaml"},
            )
            == []
        )

    def test_multidocument_file_cannot_mask_a_missing_input(self) -> None:
        result = {
            "resources": [
                {
                    "filename": "one.yaml",
                    "kind": "Namespace",
                    "name": name,
                    "status": "statusValid",
                    "msg": "",
                }
                for name in ("one-a", "one-b")
            ],
            "summary": {"valid": 2, "invalid": 0, "errors": 0, "skipped": 0},
        }

        errors = validator.validate_kubeconform_output(
            result,
            expected_filenames={"one.yaml", "two.yaml"},
        )

        assert any("two.yaml" in error and "no resource result" in error for error in errors)

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param(
                {
                    "filename": "one.yaml",
                    "kind": "Namespace",
                    "name": "",
                    "version": "",
                    "status": "",
                    "msg": "",
                },
                id="partially-populated",
            ),
            pytest.param(
                {
                    "filename": "one.yaml",
                    "kind": "",
                    "name": "",
                    "version": "",
                    "status": "",
                    "msg": "",
                    "futureField": "",
                },
                id="unknown-field",
            ),
            pytest.param(
                {
                    "filename": "one.yaml",
                    "kind": "",
                    "name": "",
                    "status": "",
                    "msg": "",
                },
                id="missing-field",
            ),
            pytest.param(
                {
                    "filename": "",
                    "kind": "",
                    "name": "",
                    "version": "",
                    "status": "",
                    "msg": "",
                },
                id="empty-filename",
            ),
            pytest.param(
                {
                    "filename": 7,
                    "kind": "",
                    "name": "",
                    "version": "",
                    "status": "",
                    "msg": "",
                },
                id="non-string-filename",
            ),
        ],
    )
    def test_non_separator_blank_records_still_fail_closed(self, record: dict) -> None:
        result = {
            "resources": [record],
            "summary": {"valid": 0, "invalid": 0, "errors": 0, "skipped": 0},
        }

        errors = validator.validate_kubeconform_output(
            result,
            expected_filenames={"one.yaml"},
        )
        assert "resources[0] has unknown status ''" in errors

    def test_complete_accounted_output_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        manifest = tmp_path / "one.yaml"
        manifest.write_text(
            "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: one\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(validator.shutil, "which", lambda _binary: "/usr/bin/kubeconform")

        def fake_run(directory: Path, **_kwargs):
            rendered = next(directory.rglob("*.yaml"))
            return 0, {
                "resources": [
                    {
                        "filename": str(rendered),
                        "kind": "Namespace",
                        "name": "one",
                        "status": "statusValid",
                        "msg": "",
                    }
                ],
                "summary": {"valid": 1, "invalid": 0, "errors": 0, "skipped": 0},
            }

        monkeypatch.setattr(validator, "run_kubeconform", fake_run)

        rc = validator.main(["--path", str(manifest), "--kubeconform-binary", "fake"])

        captured = capsys.readouterr()
        assert rc == 0
        assert "OK: 1 manifest(s)" in captured.out
        assert captured.err == ""


class TestMainExplicitInputs:
    def test_missing_input_does_not_mask_existing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = tmp_path / "valid.yaml"
        manifest.write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: valid\n")
        missing = tmp_path / "missing.yaml"
        calls: list[Path] = []

        monkeypatch.setattr(validator.shutil, "which", lambda _binary: "/usr/bin/kubeconform")

        def fake_run(directory: Path, **_kwargs):
            rendered = list(directory.rglob("*.yaml"))
            calls.extend(rendered)
            return 0, {
                "resources": [
                    {
                        "filename": str(rendered[0]),
                        "kind": "Namespace",
                        "name": "valid",
                        "status": "statusValid",
                        "msg": "",
                    }
                ],
                "summary": {"valid": 1, "invalid": 0, "errors": 0, "skipped": 0},
            }

        monkeypatch.setattr(validator, "run_kubeconform", fake_run)
        rc = validator.main(
            ["--path", str(manifest), "--path", str(missing), "--kubeconform-binary", "fake"]
        )

        captured = capsys.readouterr()
        assert rc == 2
        assert calls, "the existing explicit file must still be validated"
        assert str(missing) in captured.err

    def test_schema_and_input_failures_are_both_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manifest = tmp_path / "bad.yaml"
        manifest.write_text("apiVersion: v1\nkind: Pod\nmetadata:\n  name: bad\n")
        missing = tmp_path / "missing.yaml"
        monkeypatch.setattr(validator.shutil, "which", lambda _binary: "/usr/bin/kubeconform")

        def fake_run(directory: Path, **_kwargs):
            rendered = next(directory.rglob("*.yaml"))
            return 1, {
                "resources": [
                    {
                        "filename": str(rendered),
                        "kind": "Pod",
                        "name": "bad",
                        "status": "statusInvalid",
                        "msg": "schema error",
                    }
                ],
                "summary": {"valid": 0, "invalid": 1, "errors": 0, "skipped": 0},
            }

        monkeypatch.setattr(validator, "run_kubeconform", fake_run)

        rc = validator.main(
            ["--path", str(manifest), "--path", str(missing), "--kubeconform-binary", "fake"]
        )
        captured = capsys.readouterr()
        assert rc == 2
        assert "bad.yaml" in captured.out
        assert str(missing) in captured.err


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

    def test_trainer_gvks_skipped_by_gvk(self) -> None:
        # Kubeflow Trainer v2 has no datreeio catalog schema; the shipped
        # runtime is covered by the (stronger) trainer runtime lockstep in
        # validate_helm_charts.py instead. Removing these skips without a
        # catalog schema appearing upstream re-breaks the schema job on
        # every trainer manifest.
        assert "trainer.kubeflow.org/v1alpha1/TrainJob" in validator.SCHEMA_UNAVAILABLE_SKIPS
        assert (
            "trainer.kubeflow.org/v1alpha1/ClusterTrainingRuntime"
            in validator.SCHEMA_UNAVAILABLE_SKIPS
        )

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


# ── main() offline, against a scripted kubeconform ───────────────────────────
#
# The real binary is network-bound and opt-in above. Everything main() does
# around it -- input collection, rendering, envelope validation, exit-code
# policy -- is deterministic and worth pinning without the network. So these
# drive main() with a stand-in ``kubeconform`` executable that walks the
# rendered directory and emits exactly the JSON shape the pinned v0.8.0 emits,
# with a per-run behaviour chosen through an environment variable.


_FAKE_KUBECONFORM = r'''#!/usr/bin/env python3
"""Stand-in kubeconform: emit v0.8.0-shaped JSON for every rendered manifest."""
import json, os, sys
mode = os.environ.get("FAKE_KUBECONFORM_MODE", "valid")
directory = sys.argv[-1]
files = sorted(
    os.path.join(root, name)
    for root, _dirs, names in os.walk(directory)
    for name in names
    if name.endswith((".yaml", ".yml"))
)
if mode == "silent-crash":
    sys.exit(3)
if mode == "garbage":
    sys.stdout.write("this is not json")
    sys.exit(0)
resources = []
for path in files:
    if mode == "invalid-first" and path == files[0]:
        resources.append({"filename": path, "kind": "Pod", "name": "bad", "version": "v1",
                          "status": "statusInvalid", "msg": "unknown field imageBogusField"})
    else:
        resources.append({"filename": path, "kind": "ConfigMap", "name": "ok", "version": "v1",
                          "status": "statusValid", "msg": ""})
if mode == "partial":
    resources = resources[:-1]  # drop one file's record: incomplete accounting
counts = {"valid": 0, "invalid": 0, "errors": 0, "skipped": 0}
for r in resources:
    counts[{"statusValid": "valid", "statusInvalid": "invalid"}[r["status"]]] += 1
json.dump({"resources": resources, "summary": counts}, sys.stdout)
sys.exit(1 if counts["invalid"] else (0 if mode != "nonzero-clean" else 4))
'''


@pytest.fixture
def fake_kubeconform(tmp_path: Path) -> Path:
    binary = tmp_path / "bin" / "kubeconform"
    binary.parent.mkdir()
    binary.write_text(_FAKE_KUBECONFORM, encoding="utf-8")
    binary.chmod(0o755)
    return binary


@pytest.fixture
def manifest_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "manifests"
    directory.mkdir()
    (directory / "a.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\n", encoding="utf-8"
    )
    (directory / "b.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: b\n", encoding="utf-8"
    )
    return directory


def _run_main(binary: Path, *args: str, mode: str = "valid", monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_KUBECONFORM_MODE", mode)
    return validator.main(["--kubeconform-binary", str(binary), *args])


class TestMainOffline:
    def test_a_clean_run_reports_ok_and_exits_zero(
        self,
        fake_kubeconform: Path,
        manifest_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = _run_main(fake_kubeconform, "--path", str(manifest_dir), monkeypatch=monkeypatch)

        assert rc == 0
        assert "OK: 2 manifest(s) are schema-valid" in capsys.readouterr().out

    def test_verbose_lists_each_valid_resource(
        self,
        fake_kubeconform: Path,
        manifest_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        rc = _run_main(
            fake_kubeconform, "--path", str(manifest_dir), "--verbose", monkeypatch=monkeypatch
        )

        assert rc == 0
        out = capsys.readouterr().out
        assert out.count("ok    ") == 2
        assert "ConfigMap ok" in out

    def test_verbose_lists_only_the_valid_resources_of_a_mixed_result(
        self,
        fake_kubeconform: Path,
        manifest_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The invalid record belongs in the failure list, not the ``ok`` list."""
        rc = _run_main(
            fake_kubeconform,
            "--path",
            str(manifest_dir),
            "--verbose",
            mode="invalid-first",
            monkeypatch=monkeypatch,
        )

        assert rc == 1
        out = capsys.readouterr().out
        assert out.count("ok    ") == 1
        assert "Pod bad" not in [line for line in out.splitlines() if line.startswith("ok")]

    def test_a_schema_violation_is_listed_and_exits_one(
        self,
        fake_kubeconform: Path,
        manifest_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Exit 1 is reserved for real validation failures."""
        rc = _run_main(
            fake_kubeconform,
            "--path",
            str(manifest_dir),
            mode="invalid-first",
            monkeypatch=monkeypatch,
        )

        assert rc == 1
        out = capsys.readouterr().out
        assert "1 Kubernetes manifest validation problem(s) found" in out
        assert "unknown field imageBogusField" in out
        assert "SCHEMA_UNAVAILABLE_SKIPS" in out, "the remediation hint must be printed"

    def test_a_missing_binary_is_exit_two_with_a_pointer_to_the_docs(
        self,
        manifest_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Exit 2 keeps 'could not run' distinguishable from 'manifests are wrong'."""
        rc = validator.main(
            ["--kubeconform-binary", "definitely-not-on-path-xyz", "--path", str(manifest_dir)]
        )

        assert rc == 2
        err = capsys.readouterr().err
        assert "not found on PATH" in err
        assert "docs/MAINTENANCE.md" in err

    def test_no_files_at_all_is_exit_two(
        self,
        fake_kubeconform: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        rc = _run_main(fake_kubeconform, "--path", str(empty), monkeypatch=monkeypatch)

        assert rc == 2
        err = capsys.readouterr().err
        # The directory-with-no-manifests case is reported as an input error,
        # which takes the place of the generic "no files" message.
        assert "contains no Kubernetes YAML manifests" in err

    def test_a_bad_input_is_reported_but_good_inputs_are_still_validated(
        self,
        fake_kubeconform: Path,
        manifest_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """One missing path must not mask the report for the paths that exist."""
        rc = _run_main(
            fake_kubeconform,
            "--path",
            str(manifest_dir),
            "--path",
            str(tmp_path / "does-not-exist"),
            "--verbose",
            monkeypatch=monkeypatch,
        )

        assert rc == 2, "an input error takes precedence in the exit code"
        captured = capsys.readouterr()
        assert "path does not exist" in captured.err
        assert captured.out.count("ok    ") == 2, "the valid files were still validated"

    def test_unusable_json_is_exit_two_not_a_pass(
        self,
        fake_kubeconform: Path,
        manifest_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Garbage on stdout with exit 0 must fail closed, never read as OK."""
        rc = _run_main(
            fake_kubeconform, "--path", str(manifest_dir), mode="garbage", monkeypatch=monkeypatch
        )

        assert rc == 2
        assert "unusable JSON output" in capsys.readouterr().err

    def test_a_record_missing_for_one_file_is_exit_two(
        self,
        fake_kubeconform: Path,
        manifest_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Completeness is per rendered filename; a dropped record is not 'valid'."""
        rc = _run_main(
            fake_kubeconform, "--path", str(manifest_dir), mode="partial", monkeypatch=monkeypatch
        )

        assert rc == 2
        assert "no resource result was returned for input file(s)" in capsys.readouterr().err

    def test_a_silent_nonzero_exit_is_a_runtime_failure(
        self,
        fake_kubeconform: Path,
        manifest_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Non-zero with no JSON is an invocation error, reported as such."""
        rc = _run_main(
            fake_kubeconform,
            "--path",
            str(manifest_dir),
            mode="silent-crash",
            monkeypatch=monkeypatch,
        )

        assert rc == 2
        # No JSON at all is caught by the envelope check first.
        assert "unusable JSON output" in capsys.readouterr().err

    def test_a_nonzero_exit_with_clean_json_is_still_a_runtime_failure(
        self,
        fake_kubeconform: Path,
        manifest_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Valid-looking JSON does not excuse a non-zero process result."""
        rc = _run_main(
            fake_kubeconform,
            "--path",
            str(manifest_dir),
            mode="nonzero-clean",
            monkeypatch=monkeypatch,
        )

        assert rc == 2
        assert "exited non-zero without validation failures" in capsys.readouterr().err

    def test_no_strict_is_forwarded(
        self, fake_kubeconform: Path, manifest_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, list[str]] = {}
        real_run = validator.subprocess.run

        def spy(cmd, **kwargs):  # noqa: ANN001, ANN202
            seen["cmd"] = list(cmd)
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(validator.subprocess, "run", spy)
        _run_main(
            fake_kubeconform, "--path", str(manifest_dir), "--no-strict", monkeypatch=monkeypatch
        )

        assert "-strict" not in seen["cmd"]
        assert "-skip" in seen["cmd"], "the CRD skips must still be passed"


class TestInputCollectionEdges:
    """The ``collect_target_files`` branches the default targets never hit."""

    def test_a_glob_that_matches_directories_expands_them(self, tmp_path: Path) -> None:
        for name in ("one", "two"):
            sub = tmp_path / name
            sub.mkdir()
            (sub / "m.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")
        (tmp_path / "loose.txt").write_text("not yaml", encoding="utf-8")

        files, errors = validator.collect_target_files((str(tmp_path / "*"),))

        assert errors == []
        assert sorted(path.parent.name for path in files) == ["one", "two"]

    def test_a_glob_matching_no_manifests_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

        files, errors = validator.collect_target_files((str(tmp_path / "*.txt"),))

        assert files == []
        assert errors == [f"{tmp_path / '*.txt'}: glob matched no Kubernetes YAML manifests"]

    def test_an_explicit_non_manifest_yaml_is_refused(self, tmp_path: Path) -> None:
        """``pipeline-dag.yaml`` is GCO's own format, not a Kubernetes document."""
        dag = tmp_path / "pipeline-dag.yaml"
        dag.write_text("steps: []\n", encoding="utf-8")

        files, errors = validator.collect_target_files((str(dag),))

        assert files == []
        assert errors == [f"{dag}: explicit file is not a Kubernetes manifest"]


class TestEnvelopeValidationEdges:
    """The remaining shapes ``validate_kubeconform_output`` must fail closed on."""

    @staticmethod
    def _valid(filename: str = "one.yaml") -> dict:
        return {
            "filename": filename,
            "kind": "Namespace",
            "name": "one",
            "version": "v1",
            "status": "statusValid",
            "msg": "",
        }

    def test_a_non_object_resource_record_is_reported_by_index(self) -> None:
        result = {
            "resources": [self._valid(), "not-a-record"],
            "summary": {"valid": 1, "invalid": 0, "errors": 0, "skipped": 0},
        }

        errors = validator.validate_kubeconform_output(result, expected_filenames={"one.yaml"})

        assert "resources[1] is not an object" in errors

    def test_a_missing_summary_is_reported_and_stops_further_checks(self) -> None:
        errors = validator.validate_kubeconform_output(
            {"resources": [self._valid()]}, expected_filenames={"one.yaml"}
        )

        assert errors == ["'summary' is missing or is not an object"]

    @pytest.mark.parametrize(
        ("valid_count", "expected"),
        [
            pytest.param(True, "is missing or is not a non-negative integer", id="bool-is-not-int"),
            pytest.param(-1, "is missing or is not a non-negative integer", id="negative"),
            pytest.param("1", "is missing or is not a non-negative integer", id="string"),
            pytest.param(3, "summary.valid reports 3, but resources contain 1", id="disagrees"),
        ],
    )
    def test_summary_counts_are_type_checked_and_reconciled(
        self, valid_count: object, expected: str
    ) -> None:
        """``True`` is an ``int`` in Python; the check must not be fooled by it."""
        result = {
            "resources": [self._valid()],
            "summary": {"valid": valid_count, "invalid": 0, "errors": 0, "skipped": 0},
        }

        errors = validator.validate_kubeconform_output(result, expected_filenames={"one.yaml"})

        assert any(expected in error for error in errors)


class TestRunKubeconformArguments:
    def test_no_skips_omits_the_skip_flag_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty ``-skip`` would be passed as a literal empty string."""
        seen: dict[str, list[str]] = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            seen["cmd"] = list(cmd)

            class _Done:
                returncode = 0
                stdout = "{}"

            return _Done()

        monkeypatch.setattr(validator.subprocess, "run", fake_run)

        validator.run_kubeconform(tmp_path, skip_gvks=())

        assert "-skip" not in seen["cmd"]


def test_main_reports_the_generic_no_files_message_for_a_bare_glob_miss(
    fake_kubeconform: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When collection yields nothing *and* no input error explains why."""
    # An input that is neither a glob, a file nor a directory can only be reached
    # through a path object that exists but is something else (a FIFO). Rather
    # than depend on that, exercise the branch the same way main() would see it:
    # no files, no errors.
    monkeypatch.setattr(validator, "collect_target_files", lambda targets: ([], []))
    monkeypatch.setenv("FAKE_KUBECONFORM_MODE", "valid")

    rc = validator.main(["--kubeconform-binary", str(fake_kubeconform), "--path", str(tmp_path)])

    assert rc == 2
    assert "no *.yaml/*.yml files found" in capsys.readouterr().err


def test_collect_target_files_reports_an_unsupported_input_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that exists but is neither file nor directory (e.g. a FIFO)."""
    fifo = tmp_path / "pipe.yaml"
    os.mkfifo(fifo)

    files, errors = validator.collect_target_files((str(fifo),))

    assert files == []
    assert errors == [f"{fifo}: unsupported input type"]
