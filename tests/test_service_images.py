"""The image catalog is the filesystem, and every consumer reads it from there.

``gco.service_images`` replaced three hand-kept inventories (the CLI's shipped
image list, the regional stack's asset-hash excludes, the CI container-scan
matrix) that could — and did — drift: the CLI's copy was missing cost-monitor
entirely. These tests pin both halves of that arrangement.

The first half is the discovery itself, exercised against synthetic trees under
``tmp_path``: the naming convention, sorted deterministic output, the three
image families (platform services, container Lambdas, root images), the
directories and staging copies it must ignore, the loud failure for a filename
that cannot become an image name, and the module's ``--github-matrix`` CLI.

The second half is the wiring, checked against the real repository, so a
consumer cannot quietly go back to a literal list: the CLI inventory and the
stack's build inputs must equal discovery, every discovered service must
actually be built by ``_create_container_images`` (a Dockerfile nobody builds
is dead weight; a build with no Dockerfile fails at synth), the security
workflow must derive its scan matrix by running this module, and every service
image must be built by some job in ``integration-tests.yml``. Filenames are
compared against the shipped set as well, so a rename has to be deliberate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from gco.service_images import (
    REPO_ROOT,
    ImageBuild,
    discover_image_builds,
    discover_service_dockerfiles,
    main,
    service_dockerfile,
)

#: The service images this release ships. Discovery is authoritative at
#: runtime; this literal exists so a rename or an accidental deletion shows up
#: as a failing assertion rather than as silently smaller inventories
#: everywhere at once.
SHIPPED_SERVICES = {
    "cost-monitor",
    "health-monitor",
    "inference-monitor",
    "inference-proxy",
    "manifest-processor",
    "queue-processor",
}


def _write(path: Path, text: str = "FROM scratch\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A synthetic repository with one of every discoverable shape."""
    _write(tmp_path / "dockerfiles" / "Dockerfile.zeta-service")
    _write(tmp_path / "dockerfiles" / "Dockerfile.alpha-service")
    _write(tmp_path / "dockerfiles" / "README.md", "# not a Dockerfile\n")
    _write(tmp_path / "lambda" / "some-lambda" / "Dockerfile")
    _write(tmp_path / "lambda" / "some-lambda-build" / "Dockerfile")
    _write(tmp_path / "lambda" / "zip-packaged-lambda" / "handler.py", "")
    _write(tmp_path / "Dockerfile.dev")
    return tmp_path


class TestDiscovery:
    def test_service_dockerfile_is_the_naming_convention(self) -> None:
        assert service_dockerfile("health-monitor") == "dockerfiles/Dockerfile.health-monitor"

    def test_services_are_discovered_sorted_and_repo_relative(self, tree: Path) -> None:
        assert discover_service_dockerfiles(tree) == {
            "alpha-service": "dockerfiles/Dockerfile.alpha-service",
            "zeta-service": "dockerfiles/Dockerfile.zeta-service",
        }

    def test_a_directory_named_like_a_dockerfile_is_not_a_service(self, tree: Path) -> None:
        """Only files are build recipes, and a stray directory must not raise."""
        (tree / "dockerfiles" / "Dockerfile.Not_A_Service").mkdir()
        assert set(discover_service_dockerfiles(tree)) == {"alpha-service", "zeta-service"}

    def test_an_unusable_service_filename_fails_loudly(self, tree: Path) -> None:
        """A stray editor backup must not silently become (or shadow) a service."""
        _write(tree / "dockerfiles" / "Dockerfile.health-monitor.bak")
        with pytest.raises(ValueError, match=r"is not a valid image name"):
            discover_service_dockerfiles(tree)

    def test_builds_cover_the_three_families_with_their_contexts(self, tree: Path) -> None:
        assert discover_image_builds(tree) == [
            ImageBuild("alpha-service", "dockerfiles/Dockerfile.alpha-service", "."),
            ImageBuild("zeta-service", "dockerfiles/Dockerfile.zeta-service", "."),
            ImageBuild("some-lambda", "lambda/some-lambda/Dockerfile", "lambda/some-lambda"),
            ImageBuild("dev", "Dockerfile.dev", "."),
        ]

    def test_generated_lambda_staging_copies_are_skipped(self, tree: Path) -> None:
        """``lambda/<name>-build/`` is build output, not a source image."""
        images = [build.image for build in discover_image_builds(tree)]
        assert "some-lambda-build" not in images

    def test_a_root_directory_named_like_a_dockerfile_is_not_an_image(self, tree: Path) -> None:
        (tree / "Dockerfile.d").mkdir()
        assert [b.image for b in discover_image_builds(tree)][-1] == "dev"

    def test_an_unusable_lambda_directory_name_fails_loudly(self, tree: Path) -> None:
        _write(tree / "lambda" / "Bad Name" / "Dockerfile")
        with pytest.raises(ValueError, match=r"is not a valid image name"):
            discover_image_builds(tree)


class TestCli:
    def test_github_matrix_emits_one_compact_object_per_image(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--github-matrix", "--repo-root", str(tree)]) == 0
        stdout = capsys.readouterr().out
        assert "\n" not in stdout.strip(), "matrix output must be a single line for $GITHUB_OUTPUT"
        assert json.loads(stdout) == [
            {"image": b.image, "dockerfile": b.dockerfile, "context": b.context}
            for b in discover_image_builds(tree)
        ]

    def test_human_output_is_indented(self, tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--repo-root", str(tree)]) == 0
        stdout = capsys.readouterr().out
        assert '\n  {\n    "image"' in stdout
        assert len(json.loads(stdout)) == 4

    def test_an_empty_tree_is_an_error_not_an_empty_matrix(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty matrix would silently scan nothing, so discovery must fail."""
        assert main(["--github-matrix", "--repo-root", str(tmp_path)]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no Dockerfiles discovered" in captured.err

    def test_the_default_repo_root_is_this_checkout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--github-matrix"]) == 0
        images = {row["image"] for row in json.loads(capsys.readouterr().out)}
        assert images >= SHIPPED_SERVICES


class TestShippedRepository:
    def test_discovery_finds_exactly_the_shipped_services(self) -> None:
        assert set(discover_service_dockerfiles()) == SHIPPED_SERVICES

    def test_every_discovered_dockerfile_exists(self) -> None:
        for dockerfile in discover_service_dockerfiles().values():
            assert (REPO_ROOT / dockerfile).is_file(), dockerfile

    def test_the_cli_inventory_is_discovery(self) -> None:
        """``gco images`` must not carry its own copy of the catalog (it drifted)."""
        from cli import images

        assert discover_service_dockerfiles() == images._MAINTAINED_IMAGES

    def test_the_stacks_asset_excludes_are_discovery(self) -> None:
        from gco.stacks import regional_stack

        assert set(regional_stack._SERVICE_IMAGE_BUILD_INPUTS) == set(
            discover_service_dockerfiles().values()
        )

    def test_each_service_image_excludes_every_other_services_dockerfile(self) -> None:
        """One service's asset hash must not move when another's recipe changes."""
        from gco.stacks import regional_stack

        services = discover_service_dockerfiles()
        for dockerfile in services.values():
            excludes = regional_stack._service_image_asset_excludes(dockerfile)
            assert dockerfile not in excludes
            assert set(services.values()) - {dockerfile} <= set(excludes)

    def test_every_discovered_service_is_built_by_the_regional_stack(self) -> None:
        """A Dockerfile with no asset is dead weight; an asset with no file fails synth."""
        source = (REPO_ROOT / "gco" / "stacks" / "regional_stack.py").read_text(encoding="utf-8")
        built = set(re.findall(r'_service_image_asset\(\s*"\w+",\s*"([a-z0-9-]+)"', source))
        assert built == set(discover_service_dockerfiles())


class TestWorkflowWiring:
    @staticmethod
    def _workflow(name: str) -> dict:
        return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text())

    def test_the_container_scan_matrix_is_discovered_not_listed(self) -> None:
        workflow = self._workflow("security.yml")
        discover = workflow["jobs"]["security-discover-images"]
        commands = " ".join(step.get("run", "") for step in discover["steps"])
        assert "gco.service_images --github-matrix" in commands
        assert discover["outputs"]["matrix"] == "${{ steps.discover.outputs.matrix }}"

        scan = workflow["jobs"]["security-trivy-container-scan"]
        assert scan["needs"] == "security-discover-images"
        assert scan["strategy"]["matrix"] == (
            "${{ fromJSON(needs.security-discover-images.outputs.matrix) }}"
        )

    def test_the_container_scan_consumes_every_discovered_field(self) -> None:
        """The build step must read image/dockerfile/context — the keys we emit."""
        scan = self._workflow("security.yml")["jobs"]["security-trivy-container-scan"]
        build = next(step for step in scan["steps"] if step.get("name") == "Build image")
        assert build["with"]["context"] == "${{ matrix.context }}"
        assert build["with"]["file"] == "${{ matrix.dockerfile }}"
        assert build["with"]["tags"] == "${{ matrix.image }}:scan"

    def test_every_service_image_is_built_by_an_integration_job(self) -> None:
        """Building an image in CI is how its runtime contract gets exercised."""
        workflow = self._workflow("integration-tests.yml")
        built = {
            (step.get("with") or {}).get("file")
            for job in workflow["jobs"].values()
            for step in job.get("steps") or []
        }
        missing = set(discover_service_dockerfiles().values()) - built
        assert not missing, f"service images built by no integration job: {sorted(missing)}"

    def test_every_workflow_image_build_names_a_real_dockerfile(self) -> None:
        for name in ("integration-tests.yml", "security.yml"):
            for job in self._workflow(name)["jobs"].values():
                for step in job.get("steps") or []:
                    referenced = (step.get("with") or {}).get("file")
                    if referenced and "${{" not in referenced:
                        assert (REPO_ROOT / referenced).is_file(), f"{name}: {referenced}"
