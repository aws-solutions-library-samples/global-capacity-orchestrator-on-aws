"""Tests for gco/argocd_config.py — the self-managed Argo CD block (``helm.argocd``).

The module is the single source of the block's shape: the config loader, the
regional stack, the ``gco gitops`` CLI and the kind CI job all call it, so
these tests pin the defaults, every validation branch, the GitOps helpers and
the kubectl-applier replacements the stack renders into the post-Helm
manifests, plus the repo-server sizing / autoscaler chart values.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import patch

import aws_cdk as cdk
import pytest

from gco import argocd_config as ac
from gco.config.config_loader import ConfigLoader, ConfigValidationError


def _block(**overrides: object) -> dict[str, object]:
    block: dict[str, object] = {"enabled": True, **overrides}
    return block


def _gitops(**overrides: object) -> dict[str, object]:
    gitops: dict[str, object] = {
        "repo_url": "https://github.com/example/gco-tenants.git",
        "revision": "main",
        "path": "clusters/{region}",
        "sync_policy": "automated",
    }
    gitops.update(overrides)
    return gitops


class TestDefaults:
    def test_absent_block_is_off_with_every_default(self) -> None:
        config = ac.validate_argocd_config(None)
        assert config == ac.ARGOCD_DEFAULTS
        assert config["enabled"] is False
        assert config["source_repos"] == ["*"]
        assert config["gitops"] == {
            "repo_url": "",
            "revision": "HEAD",
            "path": ".",
            "sync_policy": "manual",
        }
        assert config["repo_server"] == {
            "replicas": 1,
            "autoscaling": {
                "enabled": False,
                "max_replicas": 5,
                "cpu_target_utilization_percentage": 70,
            },
        }
        assert ac.gitops_enabled(config) is False

    @pytest.mark.parametrize(
        "raw",
        [
            {"enabled": True, "gitops": None},
            {"enabled": True, "repo_server": None},
            {"enabled": True, "repo_server": {"autoscaling": None}},
        ],
    )
    def test_json_null_nested_blocks_keep_their_defaults(self, raw: dict[str, object]) -> None:
        config = ac.validate_argocd_config(raw)
        assert config["gitops"] == ac.ARGOCD_DEFAULTS["gitops"]
        assert config["repo_server"] == ac.ARGOCD_DEFAULTS["repo_server"]

    def test_defaults_are_copied_not_shared(self) -> None:
        config = ac.validate_argocd_config(None)
        config["source_repos"].append("mutated")
        config["gitops"]["path"] = "mutated"
        assert ac.ARGOCD_DEFAULTS["source_repos"] == ["*"]
        assert ac.ARGOCD_DEFAULTS["gitops"]["path"] == "."

    def test_partial_block_merges_nested_defaults(self) -> None:
        config = ac.validate_argocd_config({"enabled": True, "gitops": {"revision": "v1"}})
        assert config["enabled"] is True
        assert config["gitops"] == {
            "repo_url": "",
            "revision": "v1",
            "path": ".",
            "sync_policy": "manual",
        }

    def test_merge_does_not_alias_the_caller_block(self) -> None:
        raw = {"enabled": True, "source_repos": ["https://github.com/example/*"]}
        config = ac.validate_argocd_config(raw)
        config["source_repos"].append("x")
        assert raw["source_repos"] == ["https://github.com/example/*"]

    def test_fixed_names_the_manifests_and_cli_share(self) -> None:
        assert ac.ARGOCD_HELM_KEY == "argocd"
        assert ac.ARGOCD_CHART_NAME == "argocd"
        assert ac.ARGOCD_NAMESPACE == "argocd"
        assert ac.GITOPS_PROJECT_NAME == "gco-tenants"
        assert ac.GITOPS_ROOT_APPLICATION_NAME == "gco-gitops-root"
        assert ac.GITOPS_TENANT_NAMESPACES == ("gco-jobs", "gco-inference")
        assert "gco-system" not in ac.GITOPS_TENANT_NAMESPACES
        assert ac.GITOPS_DEFAULT_NAMESPACE in ac.GITOPS_TENANT_NAMESPACES
        assert ac.ARGOCD_SERVER_SERVICE == "argocd-server"
        assert ac.ARGOCD_INITIAL_ADMIN_SECRET == "argocd-initial-admin-secret"


class TestValidationErrors:
    @pytest.mark.parametrize("raw", [[], "on", 1, True])
    def test_block_must_be_an_object(self, raw: object) -> None:
        with pytest.raises(ac.ArgoCdConfigError, match=r"helm\.argocd must be an object"):
            ac.validate_argocd_config(raw)

    def test_errors_are_value_errors(self) -> None:
        assert issubclass(ac.ArgoCdConfigError, ValueError)

    def test_unknown_top_level_key_is_named(self) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError, match=r"helm\.argocd contains unknown key\(s\): vpce_ids"
        ):
            ac.validate_argocd_config({"enabled": True, "vpce_ids": []})

    def test_unknown_gitops_key_is_named(self) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError, match=r"helm\.argocd\.gitops contains unknown key\(s\): source"
        ):
            ac.validate_argocd_config({"gitops": {"source": "codecommit"}})

    @pytest.mark.parametrize("gitops", [[], "https://github.com/x/y.git", 3])
    def test_gitops_must_be_an_object(self, gitops: object) -> None:
        with pytest.raises(ac.ArgoCdConfigError, match=r"helm\.argocd\.gitops must be an object"):
            ac.validate_argocd_config({"gitops": gitops})

    @pytest.mark.parametrize("enabled", ["true", 1, None])
    def test_enabled_must_be_a_boolean(self, enabled: object) -> None:
        with pytest.raises(ac.ArgoCdConfigError, match="enabled must be a boolean"):
            ac.validate_argocd_config({"enabled": enabled})

    @pytest.mark.parametrize("repos", [[], "*", ["*", ""], ["  "], [1], None])
    def test_source_repos_must_be_a_non_empty_list_of_strings(self, repos: object) -> None:
        with pytest.raises(ac.ArgoCdConfigError, match="source_repos must be a non-empty list"):
            ac.validate_argocd_config({"source_repos": repos})

    def test_source_repos_must_not_repeat(self) -> None:
        with pytest.raises(ac.ArgoCdConfigError, match="lists a repository twice"):
            ac.validate_argocd_config({"source_repos": ["*", "*"]})

    @pytest.mark.parametrize("key", ["repo_url", "revision", "path", "sync_policy"])
    def test_gitops_fields_must_be_strings(self, key: str) -> None:
        with pytest.raises(ac.ArgoCdConfigError, match=f"gitops.{key} must be a string"):
            ac.validate_argocd_config({"gitops": {key: 7}})

    def test_sync_policy_is_manual_or_automated(self) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError, match="sync_policy must be one of manual, automated"
        ):
            ac.validate_argocd_config({"gitops": {"sync_policy": "auto"}})

    @pytest.mark.parametrize("path", ["/abs", "../up", "a/../../b", "a/.."])
    def test_path_must_stay_inside_the_repository(self, path: str) -> None:
        with pytest.raises(ac.ArgoCdConfigError, match="relative to the repository root"):
            ac.validate_argocd_config({"gitops": {"path": path}})

    @pytest.mark.parametrize(
        "repo_url", ["example.com/repo", "http://example.com/repo", "s3://b/k"]
    )
    def test_repo_url_must_be_a_git_url(self, repo_url: str) -> None:
        with pytest.raises(ac.ArgoCdConfigError, match="must be a Git repository URL"):
            ac.validate_argocd_config({"gitops": {"repo_url": repo_url}})

    def test_repo_url_needs_a_revision(self) -> None:
        with pytest.raises(ac.ArgoCdConfigError, match="revision must be a non-empty"):
            ac.validate_argocd_config({"gitops": _gitops(revision="  ")})

    def test_repo_url_must_be_admitted_by_source_repos(self) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError, match=r"is not admitted by helm\.argocd\.source_repos"
        ):
            ac.validate_argocd_config(
                _block(source_repos=["https://github.com/other/*"], gitops=_gitops())
            )


class TestRepoServerValidation:
    @pytest.mark.parametrize("repo_server", [[], "big", 3])
    def test_repo_server_must_be_an_object(self, repo_server: object) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError, match=r"helm\.argocd\.repo_server must be an object"
        ):
            ac.validate_argocd_config({"repo_server": repo_server})

    def test_unknown_repo_server_key_is_named(self) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError,
            match=r"helm\.argocd\.repo_server contains unknown key\(s\): min_replicas",
        ):
            ac.validate_argocd_config({"repo_server": {"min_replicas": 2}})

    @pytest.mark.parametrize("autoscaling", [[], True, "on"])
    def test_autoscaling_must_be_an_object(self, autoscaling: object) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError,
            match=r"helm\.argocd\.repo_server\.autoscaling must be an object",
        ):
            ac.validate_argocd_config({"repo_server": {"autoscaling": autoscaling}})

    def test_unknown_autoscaling_key_is_named(self) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError,
            match=(
                r"helm\.argocd\.repo_server\.autoscaling contains unknown key\(s\): "
                r"memory_target_utilization_percentage"
            ),
        ):
            ac.validate_argocd_config(
                {"repo_server": {"autoscaling": {"memory_target_utilization_percentage": 80}}}
            )

    @pytest.mark.parametrize("replicas", [0, 51, True, 2.0, "2", None])
    def test_replicas_is_an_exact_integer_in_range(self, replicas: object) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError,
            match=r"helm\.argocd\.repo_server\.replicas must be an integer between 1 and 50",
        ):
            ac.validate_argocd_config({"repo_server": {"replicas": replicas}})

    @pytest.mark.parametrize("enabled", ["true", 1, None])
    def test_autoscaling_enabled_must_be_a_boolean(self, enabled: object) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError,
            match=r"helm\.argocd\.repo_server\.autoscaling\.enabled must be a boolean",
        ):
            ac.validate_argocd_config({"repo_server": {"autoscaling": {"enabled": enabled}}})

    @pytest.mark.parametrize(
        ("key", "value", "bounds"),
        [
            ("max_replicas", 0, "1 and 100"),
            ("max_replicas", 101, "1 and 100"),
            ("max_replicas", False, "1 and 100"),
            ("cpu_target_utilization_percentage", 0, "1 and 100"),
            ("cpu_target_utilization_percentage", 150, "1 and 100"),
            ("cpu_target_utilization_percentage", "70", "1 and 100"),
        ],
    )
    def test_autoscaling_integers_are_checked_even_when_off(
        self, key: str, value: object, bounds: str
    ) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError,
            match=rf"helm\.argocd\.repo_server\.autoscaling\.{key} must be an integer "
            rf"between {bounds}",
        ):
            ac.validate_argocd_config({"repo_server": {"autoscaling": {key: value}}})

    def test_ceiling_may_not_undercut_the_floor(self) -> None:
        with pytest.raises(
            ac.ArgoCdConfigError,
            match=r"max_replicas must be at least helm\.argocd\.repo_server\.replicas, got 3 < 4",
        ):
            ac.validate_argocd_config(
                {"repo_server": {"replicas": 4, "autoscaling": {"max_replicas": 3}}}
            )

    def test_floor_equal_to_ceiling_is_valid(self) -> None:
        config = ac.validate_argocd_config(
            {"repo_server": {"replicas": 5, "autoscaling": {"enabled": True}}}
        )
        assert config["repo_server"]["autoscaling"]["max_replicas"] == 5


class TestChartValues:
    def test_default_is_one_fixed_replica_without_an_autoscaler(self) -> None:
        assert ac.argocd_chart_values(ac.validate_argocd_config({"enabled": True})) == {
            "repoServer": {"replicas": 1, "autoscaling": {"enabled": False}}
        }

    def test_fixed_size_without_autoscaling(self) -> None:
        config = ac.validate_argocd_config(
            {"repo_server": {"replicas": 3, "autoscaling": {"max_replicas": 9}}}
        )
        # The ceiling only matters to an HPA, so none is rendered.
        assert ac.argocd_chart_values(config) == {
            "repoServer": {"replicas": 3, "autoscaling": {"enabled": False}}
        }

    def test_autoscaling_renders_the_chart_hpa(self) -> None:
        config = ac.validate_argocd_config(
            {
                "enabled": True,
                "repo_server": {
                    "replicas": 2,
                    "autoscaling": {
                        "enabled": True,
                        "max_replicas": 6,
                        "cpu_target_utilization_percentage": 60,
                    },
                },
            }
        )
        values = ac.argocd_chart_values(config)
        assert values == {
            "repoServer": {
                "replicas": 2,
                "autoscaling": {
                    "enabled": True,
                    "minReplicas": 2,
                    "maxReplicas": 6,
                    "metrics": [
                        {
                            "type": "ContainerResource",
                            "containerResource": {
                                "name": "cpu",
                                "container": "repo-server",
                                "target": {"type": "Utilization", "averageUtilization": 60},
                            },
                        }
                    ],
                    "behavior": ac.REPO_SERVER_HPA_BEHAVIOR,
                },
            }
        }
        # A copy: mutating the rendered values never edits the shared constant.
        values["repoServer"]["autoscaling"]["behavior"]["scaleUp"]["policies"].clear()
        assert ac.REPO_SERVER_HPA_BEHAVIOR["scaleUp"]["policies"]

    def test_behavior_damps_both_directions(self) -> None:
        behavior = ac.REPO_SERVER_HPA_BEHAVIOR
        assert behavior["scaleUp"]["stabilizationWindowSeconds"] == 60
        assert behavior["scaleDown"]["stabilizationWindowSeconds"] == 300
        assert behavior["scaleDown"]["policies"] == [
            {"type": "Pods", "value": 1, "periodSeconds": 120}
        ]

    def test_names_match_the_chart_release(self) -> None:
        assert f"{ac.ARGOCD_CHART_NAME}-repo-server" == ac.ARGOCD_REPO_SERVER_DEPLOYMENT
        assert ac.ARGOCD_REPO_SERVER_CONTAINER == "repo-server"


class TestValidConfigurations:
    def test_full_block_round_trips(self) -> None:
        raw = _block(
            source_repos=["https://github.com/example/*"],
            gitops=_gitops(),
            repo_server={
                "replicas": 2,
                "autoscaling": {
                    "enabled": True,
                    "max_replicas": 10,
                    "cpu_target_utilization_percentage": 80,
                },
            },
        )
        config = ac.validate_argocd_config(copy.deepcopy(raw))
        assert config == raw
        assert ac.gitops_enabled(config) is True

    @pytest.mark.parametrize(
        "repo_url",
        [
            "https://github.com/example/gco-tenants",
            "ssh://git@example.com/tenants.git",
            "git@github.com:example/gco-tenants.git",
            "file-share/tenants.git",
        ],
    )
    def test_git_url_shapes(self, repo_url: str) -> None:
        assert ac.is_git_repository_url(repo_url)
        config = ac.validate_argocd_config({"gitops": {"repo_url": repo_url}})
        assert ac.gitops_enabled(config)

    def test_repo_url_padding_is_tolerated(self) -> None:
        config = ac.validate_argocd_config(
            {"gitops": {"repo_url": "  https://github.com/example/t.git  "}}
        )
        assert ac.gitops_enabled(config)

    def test_repository_root_path(self) -> None:
        # "" and "." both mean the repository root.
        for path in ("", "."):
            assert ac.validate_argocd_config({"gitops": {"path": path}})["gitops"]["path"] == path

    def test_repository_allowed_uses_glob_patterns(self) -> None:
        repos = ["https://github.com/example/*", "git@github.com:org/exact.git"]
        assert ac.repository_allowed("https://github.com/example/anything.git", repos)
        assert ac.repository_allowed("git@github.com:org/exact.git", repos)
        assert not ac.repository_allowed("https://github.com/other/x.git", repos)
        assert ac.repository_allowed("anything", ["*"])


class TestGitOpsHelpers:
    def test_gitops_enabled_tolerates_odd_shapes(self) -> None:
        assert ac.gitops_enabled({}) is False
        assert ac.gitops_enabled({"gitops": "x"}) is False
        assert ac.gitops_enabled({"gitops": {"repo_url": None}}) is False
        assert ac.gitops_enabled({"gitops": {"repo_url": "   "}}) is False
        assert ac.gitops_enabled({"gitops": {"repo_url": "https://x/y.git"}}) is True

    def test_render_gitops_path_substitutes_only_the_known_placeholders(self) -> None:
        assert (
            ac.render_gitops_path(
                "clusters/{region}/{cluster_name}/{prod}",
                region="us-east-1",
                cluster_name="gco-us-east-1",
            )
            == "clusters/us-east-1/gco-us-east-1/{prod}"
        )

    @pytest.mark.parametrize("path", ["", "   "])
    def test_empty_path_is_the_repository_root(self, path: str) -> None:
        assert ac.render_gitops_path(path, region="r", cluster_name="c") == "."

    def test_sync_policy_documents(self) -> None:
        assert ac.sync_policy_document("automated") == {
            "automated": {"selfHeal": True, "prune": False}
        }
        assert ac.sync_policy_document("manual") == {}


class TestReplacements:
    def test_disabled_emits_nothing(self) -> None:
        config = ac.validate_argocd_config(_block(gitops=_gitops()))
        assert (
            ac.compute_argocd_replacements(
                config, enabled=False, region="us-east-1", cluster_name="gco-us-east-1"
            )
            == {}
        )

    def test_enabled_without_gitops_gates_the_fence_only(self) -> None:
        config = ac.validate_argocd_config(_block(source_repos=["https://github.com/example/*"]))
        replacements = ac.compute_argocd_replacements(
            config, enabled=True, region="us-east-1", cluster_name="gco-us-east-1"
        )
        assert replacements == {
            "{{ARGOCD_ENABLED}}": "true",
            "{{ARGOCD_SOURCE_REPOS}}": '["https://github.com/example/*"]',
        }
        assert "{{ARGOCD_GITOPS_REPO_URL}}" not in replacements

    def test_enabled_with_gitops_renders_the_root_application(self) -> None:
        config = ac.validate_argocd_config(
            _block(gitops=_gitops(repo_url=" https://github.com/example/t.git "))
        )
        replacements = ac.compute_argocd_replacements(
            config, enabled=True, region="eu-west-1", cluster_name="gco-eu-west-1"
        )
        assert replacements == {
            "{{ARGOCD_ENABLED}}": "true",
            "{{ARGOCD_SOURCE_REPOS}}": '["*"]',
            "{{ARGOCD_GITOPS_REPO_URL}}": "https://github.com/example/t.git",
            "{{ARGOCD_GITOPS_REVISION}}": "main",
            "{{ARGOCD_GITOPS_PATH}}": "clusters/eu-west-1",
            "{{ARGOCD_GITOPS_SYNC_POLICY}}": '{"automated": {"selfHeal": true, "prune": false}}',
        }
        # The structural tokens are single-line JSON (YAML flow style).
        for token in ("{{ARGOCD_SOURCE_REPOS}}", "{{ARGOCD_GITOPS_SYNC_POLICY}}"):
            assert "\n" not in replacements[token]
            json.loads(replacements[token])

    def test_manual_sync_renders_an_empty_policy(self) -> None:
        config = ac.validate_argocd_config(_block(gitops=_gitops(sync_policy="manual")))
        replacements = ac.compute_argocd_replacements(
            config, enabled=True, region="r", cluster_name="c"
        )
        assert replacements["{{ARGOCD_GITOPS_SYNC_POLICY}}"] == "{}"

    def test_every_emitted_token_is_a_declared_manifest_token(self) -> None:
        config = ac.validate_argocd_config(_block(gitops=_gitops()))
        replacements = ac.compute_argocd_replacements(
            config, enabled=True, region="r", cluster_name="c"
        )
        assert set(replacements) == set(ac.ARGOCD_MANIFEST_TOKENS)


def _loader(valid_cdk_context: dict[str, Any], helm: object = None) -> ConfigLoader:
    context = dict(valid_cdk_context)
    if helm is not None:
        context["helm"] = helm
    return ConfigLoader(cdk.App(context=context))


class TestConfigLoaderWiring:
    def test_absent_block_reads_as_off(self, valid_cdk_context: dict[str, Any]) -> None:
        assert _loader(valid_cdk_context).get_argocd_config() == ac.ARGOCD_DEFAULTS
        # A helm block without argocd (or a non-object helm) is the same.
        assert _loader(valid_cdk_context, {"volcano": {}}).get_argocd_config()["enabled"] is False
        assert _loader(valid_cdk_context, []).get_argocd_config()["enabled"] is False

    def test_valid_block_is_returned_with_defaults(self, valid_cdk_context: dict[str, Any]) -> None:
        loader = _loader(valid_cdk_context, {"argocd": {"enabled": True}})
        config = loader.get_argocd_config()
        assert config["enabled"] is True
        assert config["gitops"]["sync_policy"] == "manual"

    def test_malformed_block_fails_at_construction(self, valid_cdk_context: dict[str, Any]) -> None:
        with pytest.raises(ConfigValidationError, match=r"helm\.argocd\.gitops\.sync_policy"):
            _loader(valid_cdk_context, {"argocd": {"gitops": {"sync_policy": "sometimes"}}})

    def test_malformed_repo_server_fails_at_construction(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        with pytest.raises(ConfigValidationError, match=r"helm\.argocd\.repo_server\.replicas"):
            _loader(valid_cdk_context, {"argocd": {"repo_server": {"replicas": 0}}})

    def test_getter_revalidates_and_wraps_the_module_error(
        self, valid_cdk_context: dict[str, Any]
    ) -> None:
        loader = _loader(valid_cdk_context)
        with (
            patch.object(loader, "_raw_argocd_config", return_value={"enabled": "yes"}),
            pytest.raises(ConfigValidationError, match=r"helm\.argocd\.enabled must be a boolean"),
        ):
            loader.get_argocd_config()
