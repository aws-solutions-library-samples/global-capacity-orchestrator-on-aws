"""Schema, defaults and rendering for the self-managed Argo CD (``helm.argocd``).

GCO installs Argo CD into each regional cluster from the upstream ``argo-cd``
Helm chart when ``cdk.json`` ``helm.argocd.enabled`` is true (off by default;
``helm_enabled_overrides=argocd`` forces it on for one deploy). The chart runs
in namespaced mode: its controllers get no ClusterRoles, the in-cluster
destination is registered for the two tenant namespaces only, and the
post-Helm manifests ``post-helm-argocd-access.yaml`` /
``post-helm-argocd-gitops.yaml`` add the tenant RBAC, the fenced
``AppProject`` and — when ``gitops.repo_url`` is set — one root
``Application`` pointing Argo CD at an operator repository path. The
``repo_server`` sub-block sizes the manifest-generation tier and can hand its
replica count to a CPU HorizontalPodAutoscaler (:func:`argocd_chart_values`).

This module owns the block's shape so the config loader, the regional stack,
the ``gco gitops`` CLI and the kind CI job agree on names and defaults. Like
``gco.eks_capabilities_config`` it imports nothing from ``aws_cdk``: the CLI
runs without the CDK toolchain installed. It raises :class:`ArgoCdConfigError`
(a ``ValueError``); the loader re-raises with its own exception type.
"""

from __future__ import annotations

import copy
import fnmatch
import json
from collections.abc import Mapping
from typing import Any

#: ``cdk.json`` ``helm`` block key (and ``helm_enabled_overrides`` name).
ARGOCD_HELM_KEY = "argocd"

#: charts.yaml entry (the Helm release name, so resources are ``argocd-*``).
ARGOCD_CHART_NAME = "argocd"

#: Namespace the chart installs into; the fenced AppProject and the root
#: Application live here too.
ARGOCD_NAMESPACE = "argocd"

#: Fixed object names so the applier's prune inventory, the CLI and the kind
#: CI job can name them exactly.
GITOPS_PROJECT_NAME = "gco-tenants"
GITOPS_ROOT_APPLICATION_NAME = "gco-gitops-root"

#: Namespaces Argo CD may deploy into. These are the two tenant namespaces GCO
#: grants Argo CD write RBAC in; the platform namespace ``gco-system`` is
#: deliberately never a destination, so a compromised tenant repository cannot
#: touch platform resources. The chart's in-cluster cluster Secret lists the
#: same two namespaces (charts.yaml).
GITOPS_TENANT_NAMESPACES: tuple[str, ...] = ("gco-jobs", "gco-inference")

#: Namespace the root Application deploys manifests without a namespace into.
GITOPS_DEFAULT_NAMESPACE = "gco-jobs"

GITOPS_SYNC_POLICIES: tuple[str, ...] = ("manual", "automated")

#: Placeholders the GitOps ``path`` may carry; substituted per cluster so one
#: repository can hold a per-cluster overlay directory.
GITOPS_PATH_PLACEHOLDERS: tuple[str, ...] = ("{region}", "{cluster_name}")

#: The in-cluster Service the UI is served from, its port (``server.insecure``
#: makes it plain HTTP behind the kubectl port-forward) and the Secret the
#: server writes the generated ``admin`` password into on first start.
ARGOCD_SERVER_SERVICE = "argocd-server"
ARGOCD_SERVER_PORT = 80
ARGOCD_ADMIN_USERNAME = "admin"
ARGOCD_INITIAL_ADMIN_SECRET = "argocd-initial-admin-secret"  # nosec B105  # Kubernetes Secret name, not its contents

#: The repo server the chart renders for release ``argocd``: the Deployment
#: (and, with autoscaling on, the HorizontalPodAutoscaler of the same name)
#: and its one container, which the HPA's CPU metric is scoped to.
ARGOCD_REPO_SERVER_DEPLOYMENT = "argocd-repo-server"
ARGOCD_REPO_SERVER_CONTAINER = "repo-server"

#: Inclusive bounds for the repo-server scaling integers (the manifest
#: processor and inference proxy use the same ranges).
REPO_SERVER_REPLICAS_RANGE: tuple[int, int] = (1, 50)
REPO_SERVER_MAX_REPLICAS_RANGE: tuple[int, int] = (1, 100)
REPO_SERVER_CPU_TARGET_RANGE: tuple[int, int] = (1, 100)

#: HPA ``behavior`` for the repo server. A push renders every affected
#: Application at once and then goes quiet, so scale out only on a minute of
#: sustained load (at most two pods a minute) and scale in one pod every two
#: minutes after five quiet ones; a single burst of renders does not flap the
#: tier. The manifest-processor HPA damps its scaling the same way.
REPO_SERVER_HPA_BEHAVIOR: dict[str, Any] = {
    "scaleUp": {
        "stabilizationWindowSeconds": 60,
        "selectPolicy": "Max",
        "policies": [{"type": "Pods", "value": 2, "periodSeconds": 60}],
    },
    "scaleDown": {
        "stabilizationWindowSeconds": 300,
        "selectPolicy": "Min",
        "policies": [{"type": "Pods", "value": 1, "periodSeconds": 120}],
    },
}

#: The block with every knob at its default.
ARGOCD_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    # Repositories Applications in the gco-tenants project may read from
    # (Argo CD sourceRepos; glob patterns allowed). "*" admits any repository:
    # the fence is on destinations and kinds, and only cluster administrators
    # can create Applications in the argocd namespace.
    "source_repos": ["*"],
    "gitops": {
        # "" leaves the root Application out; set a Git URL to hand the
        # cluster to a repository path.
        "repo_url": "",
        "revision": "HEAD",
        "path": ".",
        "sync_policy": "manual",
    },
    # The manifest-generation tier. The application controller asks the repo
    # server to render every Application on each refresh and opens a new gRPC
    # connection per request, so extra replicas share that work. replicas is
    # the fixed size and, with autoscaling on, the HPA floor; the HPA then
    # owns the count up to max_replicas at the CPU target (percent of the
    # repo-server container's CPU request in charts.yaml).
    "repo_server": {
        "replicas": 1,
        "autoscaling": {
            "enabled": False,
            "max_replicas": 5,
            "cpu_target_utilization_percentage": 70,
        },
    },
}


class ArgoCdConfigError(ValueError):
    """Raised when the ``helm.argocd`` block is malformed."""


def _deep_merge(defaults: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = copy.deepcopy(dict(defaults))
    for key, value in overrides.items():
        if value is None and isinstance(merged.get(key), Mapping):
            # JSON null for a nested block reads as "absent": keep its defaults.
            continue
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _reject_unknown_keys(block: Mapping[str, Any], allowed: Any, path: str) -> None:
    unknown = sorted(str(key) for key in block if key not in set(allowed))
    if unknown:
        raise ArgoCdConfigError(
            f"{path} contains unknown key(s): {', '.join(unknown)}; "
            f"allowed keys: {', '.join(sorted(allowed))}"
        )


def _require_object(raw: Mapping[str, Any], key: str, path: str) -> Mapping[str, Any] | None:
    """``raw[key]`` when it is an object, ``None`` when absent or null; raise otherwise."""
    value = raw.get(key)
    if value is not None and not isinstance(value, Mapping):
        raise ArgoCdConfigError(f"{path} must be an object, got {value!r}")
    return value


def _require_int(value: object, bounds: tuple[int, int], path: str) -> int:
    """An exact integer (``bool`` rejected) inside the inclusive ``bounds``."""
    low, high = bounds
    if type(value) is not int or not low <= value <= high:
        raise ArgoCdConfigError(
            f"{path} must be an integer between {low} and {high}, got {value!r}"
        )
    return value


def _validate_repo_server(repo_server: Mapping[str, Any]) -> None:
    """Check the merged ``helm.argocd.repo_server`` block."""
    replicas = _require_int(
        repo_server["replicas"], REPO_SERVER_REPLICAS_RANGE, "helm.argocd.repo_server.replicas"
    )
    autoscaling = repo_server["autoscaling"]
    if type(autoscaling["enabled"]) is not bool:
        raise ArgoCdConfigError(
            "helm.argocd.repo_server.autoscaling.enabled must be a boolean, "
            f"got {autoscaling['enabled']!r}"
        )
    # Checked when off too, so a typo surfaces before someone turns it on.
    max_replicas = _require_int(
        autoscaling["max_replicas"],
        REPO_SERVER_MAX_REPLICAS_RANGE,
        "helm.argocd.repo_server.autoscaling.max_replicas",
    )
    _require_int(
        autoscaling["cpu_target_utilization_percentage"],
        REPO_SERVER_CPU_TARGET_RANGE,
        "helm.argocd.repo_server.autoscaling.cpu_target_utilization_percentage",
    )
    if max_replicas < replicas:
        raise ArgoCdConfigError(
            "helm.argocd.repo_server.autoscaling.max_replicas must be at least "
            f"helm.argocd.repo_server.replicas, got {max_replicas} < {replicas}"
        )


def is_git_repository_url(value: str) -> bool:
    """True for the Git URL shapes Argo CD clones (https://, ssh://, scp-style, ``.git``)."""
    url = value.strip()
    return url.startswith(("https://", "ssh://", "git@")) or url.endswith(".git")


def repository_allowed(repo_url: str, source_repos: list[str]) -> bool:
    """Whether Argo CD's ``sourceRepos`` glob list admits ``repo_url``."""
    return any(fnmatch.fnmatchcase(repo_url, pattern) for pattern in source_repos)


def validate_argocd_config(raw: object) -> dict[str, Any]:
    """Validate the raw ``helm.argocd`` block and return it with defaults filled.

    ``None`` means "absent" and yields the defaults (Argo CD off). Unknown keys
    fail by name, and a ``gitops.repo_url`` the project's ``source_repos``
    would refuse fails here rather than as a sync error after deploy.
    """
    if raw is None:
        return copy.deepcopy(ARGOCD_DEFAULTS)
    if not isinstance(raw, Mapping):
        raise ArgoCdConfigError(f"helm.argocd must be an object, got {type(raw).__name__}")
    _reject_unknown_keys(raw, ARGOCD_DEFAULTS.keys(), "helm.argocd")
    gitops_raw = _require_object(raw, "gitops", "helm.argocd.gitops")
    if gitops_raw is not None:
        _reject_unknown_keys(gitops_raw, ARGOCD_DEFAULTS["gitops"].keys(), "helm.argocd.gitops")
    repo_server_raw = _require_object(raw, "repo_server", "helm.argocd.repo_server")
    if repo_server_raw is not None:
        _reject_unknown_keys(
            repo_server_raw, ARGOCD_DEFAULTS["repo_server"].keys(), "helm.argocd.repo_server"
        )
        autoscaling_raw = _require_object(
            repo_server_raw, "autoscaling", "helm.argocd.repo_server.autoscaling"
        )
        if autoscaling_raw is not None:
            _reject_unknown_keys(
                autoscaling_raw,
                ARGOCD_DEFAULTS["repo_server"]["autoscaling"].keys(),
                "helm.argocd.repo_server.autoscaling",
            )
    config = _deep_merge(ARGOCD_DEFAULTS, raw)

    if type(config["enabled"]) is not bool:
        raise ArgoCdConfigError(f"helm.argocd.enabled must be a boolean, got {config['enabled']!r}")
    repos = config["source_repos"]
    if (
        not isinstance(repos, list)
        or not repos
        or not all(isinstance(item, str) and item.strip() for item in repos)
    ):
        raise ArgoCdConfigError(
            f"helm.argocd.source_repos must be a non-empty list of repository URLs or glob "
            f"patterns, got {repos!r}"
        )
    if len(set(repos)) != len(repos):
        raise ArgoCdConfigError("helm.argocd.source_repos lists a repository twice")

    gitops = config["gitops"]
    for key in ("repo_url", "revision", "path", "sync_policy"):
        if not isinstance(gitops[key], str):
            raise ArgoCdConfigError(
                f"helm.argocd.gitops.{key} must be a string, got {gitops[key]!r}"
            )
    if gitops["sync_policy"] not in GITOPS_SYNC_POLICIES:
        raise ArgoCdConfigError(
            f"helm.argocd.gitops.sync_policy must be one of {', '.join(GITOPS_SYNC_POLICIES)}, "
            f"got {gitops['sync_policy']!r}"
        )
    path = gitops["path"].strip()
    if path.startswith("/") or ".." in path.split("/"):
        raise ArgoCdConfigError(
            "helm.argocd.gitops.path must be a directory relative to the repository root "
            f"(no leading / and no ..), got {gitops['path']!r}"
        )
    repo_url = gitops["repo_url"].strip()
    if repo_url:
        if not is_git_repository_url(repo_url):
            raise ArgoCdConfigError(
                "helm.argocd.gitops.repo_url must be a Git repository URL (https://, ssh:// "
                f"or git@), got {gitops['repo_url']!r}"
            )
        if not gitops["revision"].strip():
            raise ArgoCdConfigError(
                "helm.argocd.gitops.revision must be a non-empty branch, tag or SHA"
            )
        if not repository_allowed(repo_url, repos):
            raise ArgoCdConfigError(
                f"helm.argocd.gitops.repo_url {repo_url!r} is not admitted by "
                f"helm.argocd.source_repos {repos}; add it (or a matching pattern)"
            )
    _validate_repo_server(config["repo_server"])
    return config


def gitops_enabled(config: Mapping[str, Any]) -> bool:
    """True when the block asks for the root Application (a repository is set)."""
    gitops = config.get("gitops")
    return isinstance(gitops, Mapping) and bool(str(gitops.get("repo_url") or "").strip())


def render_gitops_path(path_template: str, *, region: str, cluster_name: str) -> str:
    """Substitute the per-cluster placeholders in a GitOps ``path``.

    Plain ``str.replace`` rather than ``str.format`` so any other brace in the
    path (a Kustomize overlay dir named ``{prod}`` for instance) is left alone.
    An empty path means the repository root.
    """
    rendered = path_template.replace("{region}", region).replace("{cluster_name}", cluster_name)
    return rendered.strip() or "."


def sync_policy_document(policy: str) -> dict[str, Any]:
    """The Application ``syncPolicy`` for ``manual`` / ``automated``.

    Automated sync self-heals drift back to Git; prune stays off so a bad
    commit cannot delete tenant workloads — operators opt into that in-repo.
    """
    if policy == "automated":
        return {"automated": {"selfHeal": True, "prune": False}}
    return {}


def argocd_chart_values(config: Mapping[str, Any]) -> dict[str, Any]:
    """The argo-cd chart values GCO derives from a validated ``helm.argocd`` block.

    The regional stack hands them to the helm installer (deep-merged over the
    static ``charts.yaml`` values) and the kind CI job installs with them. They
    size the repo server: a fixed ``replicas`` count, or — with autoscaling on
    — the chart's own HorizontalPodAutoscaler between ``replicas`` and
    ``max_replicas`` (the chart then leaves the Deployment's replica count to
    it). The metric is CPU only and scoped to the repo-server container: the
    chart's default memory target never scales back in, because the repo
    server keeps its rendering memory once it has grown. The target is a
    percentage of that container's CPU request (``charts.yaml``), and
    :data:`REPO_SERVER_HPA_BEHAVIOR` damps the scaling.
    """
    repo_server = config["repo_server"]
    autoscaling = repo_server["autoscaling"]
    replicas = int(repo_server["replicas"])
    hpa: dict[str, Any] = {"enabled": bool(autoscaling["enabled"])}
    if hpa["enabled"]:
        hpa.update(
            {
                "minReplicas": replicas,
                "maxReplicas": int(autoscaling["max_replicas"]),
                "metrics": [
                    {
                        "type": "ContainerResource",
                        "containerResource": {
                            "name": "cpu",
                            "container": ARGOCD_REPO_SERVER_CONTAINER,
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": int(
                                    autoscaling["cpu_target_utilization_percentage"]
                                ),
                            },
                        },
                    }
                ],
                "behavior": copy.deepcopy(REPO_SERVER_HPA_BEHAVIOR),
            }
        )
    return {"repoServer": {"replicas": replicas, "autoscaling": hpa}}


#: kubectl-applier tokens rendered for the Argo CD post-Helm manifests.
#: ``post-helm-argocd-access.yaml`` is gated on ``{{ARGOCD_ENABLED}}`` (and
#: carries the structural ``{{ARGOCD_SOURCE_REPOS}}``);
#: ``post-helm-argocd-gitops.yaml`` is gated on the repository URL.
ARGOCD_MANIFEST_TOKENS: tuple[str, ...] = (
    "{{ARGOCD_ENABLED}}",
    "{{ARGOCD_SOURCE_REPOS}}",
    "{{ARGOCD_GITOPS_REPO_URL}}",
    "{{ARGOCD_GITOPS_REVISION}}",
    "{{ARGOCD_GITOPS_PATH}}",
    "{{ARGOCD_GITOPS_SYNC_POLICY}}",
)


def compute_argocd_replacements(
    config: Mapping[str, Any],
    *,
    enabled: bool,
    region: str,
    cluster_name: str,
) -> dict[str, str]:
    """Build the kubectl-applier replacements for the Argo CD post-Helm manifests.

    Shared by the regional stack (deploy) and the kind CI job so both render
    the two manifests identically. ``enabled`` is the effective chart
    enablement (cdk.json toggle or a run-scoped ``helm_enabled_overrides``);
    when it is false nothing is emitted, the applier skips both files and
    prunes what they applied. The two structural tokens render as single-line
    JSON (valid YAML flow style) so they are indentation-independent.
    """
    if not enabled:
        return {}
    replacements = {
        "{{ARGOCD_ENABLED}}": "true",
        "{{ARGOCD_SOURCE_REPOS}}": json.dumps([str(repo) for repo in config["source_repos"]]),
    }
    if not gitops_enabled(config):
        return replacements
    gitops = config["gitops"]
    replacements.update(
        {
            "{{ARGOCD_GITOPS_REPO_URL}}": str(gitops["repo_url"]).strip(),
            "{{ARGOCD_GITOPS_REVISION}}": str(gitops["revision"]).strip(),
            "{{ARGOCD_GITOPS_PATH}}": render_gitops_path(
                str(gitops["path"]), region=region, cluster_name=cluster_name
            ),
            "{{ARGOCD_GITOPS_SYNC_POLICY}}": json.dumps(
                sync_policy_document(str(gitops["sync_policy"]))
            ),
        }
    )
    return replacements
