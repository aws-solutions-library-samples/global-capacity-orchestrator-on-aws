"""Examples whose image is built for one CPU architecture pin that architecture.

The CPU NodePool (45-nodepool-cpu-general.yaml) launches amd64 and arm64
nodes, and the GPU pools include an arm64 (Graviton g5g) pool
(41-nodepool-gpu-arm.yaml), so a pod whose image exists for one
architecture only must say so, or the scheduler may place it on a node where
the image cannot start. Multi-arch images (nvidia/cuda, vllm/vllm-openai,
lmsysorg/sglang, the NGC pytorch and tritonserver images) need no pin.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"
MANIFESTS_DIR = REPO_ROOT / "lambda" / "kubectl-applier-simple" / "manifests"
ARCH_LABEL = "kubernetes.io/arch"

#: Image repositories published for linux/amd64 only. Docker Hub lists a
#: single linux/amd64 image for the pytorch/pytorch CUDA runtime tags the
#: examples use (2.13.0-cuda13.0-cudnn9-runtime, 2.14.1-cuda12.6-cudnn9-runtime).
AMD64_ONLY_IMAGE_REPOSITORIES = frozenset({"pytorch/pytorch"})


def _repository(image: str) -> str:
    return image.split("@", 1)[0].rsplit(":", 1)[0]


def _pod_spec(document: dict[str, Any]) -> dict[str, Any] | None:
    spec = document.get("spec") or {}
    kind = document.get("kind")
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template", {}).get("spec")
    if isinstance(spec.get("template"), dict):
        return spec["template"].get("spec")
    return None


def _trainjob_node_selectors(document: dict[str, Any]) -> list[dict[str, str]]:
    """The nodeSelectors a TrainJob's runtime patches set on the ``node`` pods."""
    selectors = []
    for patch in (document.get("spec") or {}).get("runtimePatches") or []:
        template = (patch.get("trainingRuntimeSpec") or {}).get("template") or {}
        for job in (template.get("spec") or {}).get("replicatedJobs") or []:
            if job.get("name") != "node":
                continue
            pod = (((job.get("template") or {}).get("spec") or {}).get("template") or {}).get(
                "spec"
            ) or {}
            selectors.append(pod.get("nodeSelector") or {})
    return selectors


def _single_architecture_workloads() -> list[tuple[str, str, list[dict[str, str]]]]:
    """``(workload, image, nodeSelectors that apply)`` for each amd64-only image."""
    found = []
    for path in sorted(EXAMPLES_DIR.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if not isinstance(document, dict):
                continue
            name = (
                f"{path.name}:{document.get('kind')}/{(document.get('metadata') or {}).get('name')}"
            )
            if document.get("kind") == "TrainJob":
                image = ((document.get("spec") or {}).get("trainer") or {}).get("image", "")
                if _repository(image) in AMD64_ONLY_IMAGE_REPOSITORIES:
                    found.append((name, image, _trainjob_node_selectors(document)))
                continue
            pod = _pod_spec(document)
            if not isinstance(pod, dict):
                continue
            for container in [*(pod.get("initContainers") or []), *(pod.get("containers") or [])]:
                image = container.get("image", "")
                if _repository(image) in AMD64_ONLY_IMAGE_REPOSITORIES:
                    found.append((name, image, [pod.get("nodeSelector") or {}]))
    return found


def test_the_shipped_pools_offer_both_architectures() -> None:
    """The reason the pin matters: arm64 nodes exist in the CPU and GPU pools."""
    offered: dict[str, set[str]] = {}
    for path in sorted(MANIFESTS_DIR.glob("4*-nodepool-*.yaml")):
        text = re.sub(r"\{\{[A-Z0-9_]+\}\}", "placeholder", path.read_text(encoding="utf-8"))
        for document in yaml.safe_load_all(text):
            if not isinstance(document, dict) or document.get("kind") != "NodePool":
                continue
            for requirement in document["spec"]["template"]["spec"]["requirements"]:
                if requirement["key"] == ARCH_LABEL:
                    offered[document["metadata"]["name"]] = set(requirement["values"])
    assert offered["cpu-general-pool"] == {"amd64", "arm64"}
    assert offered["gpu-arm-pool"] == {"arm64"}
    assert offered["gpu-x86-pool"] == {"amd64"}


def test_single_architecture_examples_exist() -> None:
    """The check below would pass vacuously if no example used such an image."""
    names = {
        workload.split(":", 1)[0]
        for workload, _image, _selectors in _single_architecture_workloads()
    }
    assert {"multi-gpu-training.yaml", "kubeflow-trainjob.yaml"} <= names


@pytest.mark.parametrize(
    ("workload", "image", "selectors"),
    _single_architecture_workloads(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_amd64_only_image_is_pinned_to_amd64(
    workload: str, image: str, selectors: list[dict[str, str]]
) -> None:
    assert any(selector.get(ARCH_LABEL) == "amd64" for selector in selectors), (
        f"{workload} runs {image} (linux/amd64 only) without a {ARCH_LABEL}: amd64 nodeSelector"
    )
