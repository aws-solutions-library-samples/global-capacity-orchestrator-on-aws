"""Unit tests for the distroless image build scripts under ``dockerfiles/``.

``build_scratch_rootfs.py`` assembles the scratch rootfs in the builder stage
and derives the runtime verification manifest; ``runtime_smoke.py`` enforces
that manifest in the final stage. Both are stdlib-only scripts outside any
package, so they are loaded by file path (the same pattern
``test_ci_runtime_verifiers.py`` uses for ``.github/scripts``).

Everything here is hermetic: filesystem work happens under ``tmp_path``,
``ldd``/``dpkg`` interactions are faked at the ``subprocess.run`` boundary,
and the one real subprocess (the stdlib import probe) only imports stdlib
modules by name in an isolated interpreter. No Docker, no network, no root.
"""

from __future__ import annotations

import getpass
import importlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILES = REPO_ROOT / "dockerfiles"

SERVICE_DOCKERFILES = sorted(DOCKERFILES.glob("*-dockerfile"))


def _load_script(name: str) -> ModuleType:
    path = DOCKERFILES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_gco_test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rootfs_mod() -> ModuleType:
    return _load_script("build_scratch_rootfs")


@pytest.fixture(scope="module")
def smoke_mod() -> ModuleType:
    return _load_script("runtime_smoke")


# ---------------------------------------------------------------------------
# runtime_smoke.py
# ---------------------------------------------------------------------------

# Importable-everywhere stdlib extensions used as happy-path manifest content.
REAL_EXTENSIONS = ["_socket", "_ssl", "zlib"]


def _write_manifest(directory: Path, **overrides: object) -> None:
    manifest: dict[str, object] = {
        "python": "3.14.6",
        "runtime_user": getpass.getuser(),
        "stdlib_extensions": REAL_EXTENSIONS,
        "expected_broken": {},
    }
    manifest.update(overrides)
    (directory / "runtime_smoke_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def smoke_at(smoke_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The smoke module relocated so it reads a manifest under ``tmp_path``."""
    monkeypatch.setattr(smoke_mod, "__file__", str(tmp_path / "runtime_smoke.py"))
    return smoke_mod


def _run_smoke(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["runtime_smoke.py", *argv])
    result = smoke.main()
    assert isinstance(result, int)
    return result


class TestRuntimeSmoke:
    def test_usage_error_without_exactly_one_argument(
        self, smoke_at: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        assert _run_smoke(smoke_at, monkeypatch) == 2
        assert "usage:" in capsys.readouterr().err

    def test_all_green_returns_zero_and_reports_counts(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _write_manifest(tmp_path)
        assert _run_smoke(smoke_at, monkeypatch, "json") == 0
        out = capsys.readouterr().out
        assert "distroless runtime smoke OK" in out
        assert f"{len(REAL_EXTENSIONS)} stdlib extensions" in out
        assert "entry module json" in out
        assert getpass.getuser() in out

    def test_missing_stdlib_extension_fails_and_names_it(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _write_manifest(tmp_path, stdlib_extensions=[*REAL_EXTENSIONS, "_no_such_extension"])
        assert _run_smoke(smoke_at, monkeypatch, "json") == 1
        err = capsys.readouterr().err
        assert "stdlib extension _no_such_extension" in err
        assert "ModuleNotFoundError" in err

    def test_missing_entry_module_fails(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _write_manifest(tmp_path)
        assert _run_smoke(smoke_at, monkeypatch, "no.such.entry_module") == 1
        assert "entry module no.such.entry_module" in capsys.readouterr().err

    def test_wrong_runtime_user_fails_with_expected_and_actual(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _write_manifest(tmp_path, runtime_user="definitely-not-this-user")
        assert _run_smoke(smoke_at, monkeypatch, "json") == 1
        err = capsys.readouterr().err
        assert "definitely-not-this-user" in err
        assert getpass.getuser() in err

    def test_empty_extension_list_is_a_hard_failure(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # A gutted manifest must never produce a vacuously green smoke.
        _write_manifest(tmp_path, stdlib_extensions=[])
        assert _run_smoke(smoke_at, monkeypatch, "json") == 1
        assert "manifest lists no stdlib extensions" in capsys.readouterr().err

    def test_failures_aggregate_instead_of_stopping_at_the_first(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _write_manifest(
            tmp_path,
            stdlib_extensions=[*REAL_EXTENSIONS, "_no_such_extension"],
            runtime_user="definitely-not-this-user",
        )
        assert _run_smoke(smoke_at, monkeypatch, "no.such.entry_module") == 1
        err = capsys.readouterr().err
        assert "3 problem(s)" in err
        assert "stdlib extension _no_such_extension" in err
        assert "entry module no.such.entry_module" in err
        assert "runtime user" in err

    def test_zero_ca_certificates_fails(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _write_manifest(tmp_path)
        import ssl

        context = SimpleNamespace(cert_store_stats=lambda: {"x509_ca": 0})
        monkeypatch.setattr(ssl, "create_default_context", lambda: context)
        assert _run_smoke(smoke_at, monkeypatch, "json") == 1
        assert "zero CA certificates" in capsys.readouterr().err

    def test_broken_trust_store_is_reported_not_raised(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _write_manifest(tmp_path)
        import ssl

        def _boom() -> None:
            raise OSError("no trust anchors")

        monkeypatch.setattr(ssl, "create_default_context", _boom)
        assert _run_smoke(smoke_at, monkeypatch, "json") == 1
        err = capsys.readouterr().err
        assert "CA trust store" in err
        assert "no trust anchors" in err

    def test_missing_tzdata_is_reported(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _write_manifest(tmp_path)
        import zoneinfo

        def _boom(_key: str) -> None:
            raise zoneinfo.ZoneInfoNotFoundError("no tzdata")

        monkeypatch.setattr(zoneinfo, "ZoneInfo", _boom)
        assert _run_smoke(smoke_at, monkeypatch, "json") == 1
        assert "zoneinfo/tzdata" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# build_scratch_rootfs.py — pure path logic
# ---------------------------------------------------------------------------


class TestPathCanonicalization:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("/lib/x86_64-linux-gnu/libc.so.6", "/usr/lib/x86_64-linux-gnu/libc.so.6"),
            ("/lib64/ld-linux-x86-64.so.2", "/usr/lib64/ld-linux-x86-64.so.2"),
            ("/bin/true", "/usr/bin/true"),
            ("/sbin/nologin", "/usr/sbin/nologin"),
            ("/usr/lib/ssl/certs", "/usr/lib/ssl/certs"),
            ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs/ca-certificates.crt"),
        ],
    )
    def test_canonical_usr_path(self, rootfs_mod: ModuleType, source: str, expected: str) -> None:
        assert rootfs_mod.canonical_usr_path(Path(source)) == Path(expected)

    def test_stage_path_roots_canonical_form_under_rootfs(self, rootfs_mod: ModuleType) -> None:
        staged = rootfs_mod.stage_path(Path("/lib/x86_64-linux-gnu/libz.so.1"))
        assert staged == rootfs_mod.ROOTFS / "usr/lib/x86_64-linux-gnu/libz.so.1"
        assert rootfs_mod.stage_path(Path("/etc/passwd")) == rootfs_mod.ROOTFS / "etc/passwd"

    def test_fail_raises_systemexit_one(
        self, rootfs_mod: ModuleType, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            rootfs_mod.fail("boom")
        assert excinfo.value.code == 1
        assert "build_scratch_rootfs: ERROR: boom" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# build_scratch_rootfs.py — stdlib import probe + manifest
# ---------------------------------------------------------------------------

_SO_SUFFIX = "cpython-314-x86_64-linux-gnu.so"


def _fake_dynload(tmp_path: Path, module_names: list[str]) -> Path:
    dynload = tmp_path / "lib" / "python3.14" / "lib-dynload"
    dynload.mkdir(parents=True)
    for name in module_names:
        (dynload / f"{name}.{_SO_SUFFIX}").touch()
    return tmp_path


class TestStdlibProbe:
    def test_probe_splits_importable_from_broken(
        self, rootfs_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        critical = sorted(rootfs_mod.CRITICAL_STDLIB_EXTENSIONS)
        fake_root = _fake_dynload(tmp_path, [*critical, "_no_such_extension"])
        monkeypatch.setattr(rootfs_mod, "USR_LOCAL", fake_root)
        importable, broken = rootfs_mod.probe_stdlib_extensions()
        assert importable == critical
        assert set(broken) == {"_no_such_extension"}
        assert "ModuleNotFoundError" in broken["_no_such_extension"]

    def test_probe_enforces_the_critical_floor(
        self,
        rootfs_mod: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # A tree that lost _ssl must abort the build, not degrade the manifest.
        remaining = sorted(rootfs_mod.CRITICAL_STDLIB_EXTENSIONS - {"_ssl"})
        monkeypatch.setattr(rootfs_mod, "USR_LOCAL", _fake_dynload(tmp_path, remaining))
        with pytest.raises(SystemExit):
            rootfs_mod.probe_stdlib_extensions()
        err = capsys.readouterr().err
        assert "sanity floor" in err
        assert "_ssl" in err

    def test_probe_fails_on_empty_enumeration(
        self, rootfs_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "lib" / "python3.14" / "lib-dynload").mkdir(parents=True)
        monkeypatch.setattr(rootfs_mod, "USR_LOCAL", tmp_path)
        with pytest.raises(SystemExit):
            rootfs_mod.probe_stdlib_extensions()

    def test_critical_floor_names_are_real_stdlib_extensions(self, rootfs_mod: ModuleType) -> None:
        # Guards the floor itself against typos: every anchor must import in
        # the interpreter running this suite (the same pin CI uses). The
        # names are a frozen constant from the build script, not user input.
        for name in rootfs_mod.CRITICAL_STDLIB_EXTENSIONS:
            # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
            importlib.import_module(name)


class TestManifestWriter:
    def test_manifest_content_and_placement(
        self, rootfs_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shutil.copy2(DOCKERFILES / "runtime_smoke.py", tmp_path / "runtime_smoke.py")
        monkeypatch.setattr(rootfs_mod, "__file__", str(tmp_path / "build_scratch_rootfs.py"))
        rootfs_mod.write_runtime_smoke_manifest(["_ssl", "zlib"], {"_tkinter": "boom"})
        manifest = json.loads((tmp_path / "runtime_smoke_manifest.json").read_text())
        assert manifest["stdlib_extensions"] == ["_ssl", "zlib"]
        assert manifest["expected_broken"] == {"_tkinter": "boom"}
        assert manifest["runtime_user"] == rootfs_mod.RUNTIME_USER
        version = sys.version_info
        assert manifest["python"] == f"{version.major}.{version.minor}.{version.micro}"

    def test_manifest_refuses_to_write_without_the_smoke_script(
        self, rootfs_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rootfs_mod, "__file__", str(tmp_path / "build_scratch_rootfs.py"))
        with pytest.raises(SystemExit):
            rootfs_mod.write_runtime_smoke_manifest(["_ssl"], {})


# ---------------------------------------------------------------------------
# build_scratch_rootfs.py — ldd / dpkg output parsing at the subprocess seam
# ---------------------------------------------------------------------------


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["fake"], returncode=0, stdout=stdout, stderr="")


class TestResolveClosure:
    def test_parses_resolved_direct_and_skips_builder_broken_dynload(
        self,
        rootfs_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        ssl_seed = "/usr/local/lib/python3.14/lib-dynload/_ssl.cpython-314-x86_64-linux-gnu.so"
        tk_seed = "/usr/local/lib/python3.14/lib-dynload/_tkinter.cpython-314-x86_64-linux-gnu.so"
        ldd_output = (
            f"{ssl_seed}:\n"
            "\tlibssl.so.3 => /lib/x86_64-linux-gnu/libssl.so.3 (0x00007f0000000000)\n"
            "\tlibcrypto.so.3 => /lib/x86_64-linux-gnu/libcrypto.so.3 (0x00007f0000001000)\n"
            "\t/lib64/ld-linux-x86-64.so.2 (0x00007f0000002000)\n"
            f"{tk_seed}:\n"
            "\tlibtk8.6.so => not found\n"
            "\tlibX11.so.6 => /lib/x86_64-linux-gnu/libX11.so.6 (0x00007f0000003000)\n"
        )
        monkeypatch.setattr(
            rootfs_mod.subprocess, "run", lambda *args, **kwargs: _completed(ldd_output)
        )
        resolved = rootfs_mod.resolve_closure([Path(ssl_seed), Path(tk_seed)])
        assert resolved == {
            Path("/lib/x86_64-linux-gnu/libssl.so.3"),
            Path("/lib/x86_64-linux-gnu/libcrypto.so.3"),
            Path("/lib64/ld-linux-x86-64.so.2"),
        }
        # The broken extension's resolvable libs must NOT ride along.
        assert Path("/lib/x86_64-linux-gnu/libX11.so.6") not in resolved
        assert "_tkinter" in capsys.readouterr().out

    def test_unresolved_dependency_outside_dynload_aborts(
        self, rootfs_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seed = "/usr/local/lib/python3.14/site-packages/pkg/native.so"
        ldd_output = f"{seed}:\n\tlibmystery.so.1 => not found\n"
        monkeypatch.setattr(
            rootfs_mod.subprocess, "run", lambda *args, **kwargs: _completed(ldd_output)
        )
        with pytest.raises(SystemExit):
            rootfs_mod.resolve_closure([Path(seed)])

    def test_empty_resolution_aborts(
        self, rootfs_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            rootfs_mod.subprocess,
            "run",
            lambda *args, **kwargs: _completed("\tstatically linked\n"),
        )
        with pytest.raises(SystemExit):
            rootfs_mod.resolve_closure([Path("/usr/local/bin/python3.14")])


class TestOwningPackages:
    def test_maps_files_to_packages_and_tolerates_usr_local(
        self, rootfs_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dpkg_output = (
            "libc6:amd64: /usr/lib/x86_64-linux-gnu/libc.so.6\n"
            "libssl3t64:amd64: /usr/lib/x86_64-linux-gnu/libssl.so.3\n"
            "diversion by dash from: /usr/bin/sh\n"
        )
        monkeypatch.setattr(
            rootfs_mod.subprocess, "run", lambda *args, **kwargs: _completed(dpkg_output)
        )
        packages = rootfs_mod.owning_packages(
            {
                # Alias form on purpose: the query must canonicalize to /usr.
                Path("/lib/x86_64-linux-gnu/libc.so.6"),
                Path("/lib/x86_64-linux-gnu/libssl.so.3"),
                Path("/usr/local/lib/libpython3.14.so.1.0"),
            }
        )
        assert packages == {"libc6", "libssl3t64"}

    def test_unowned_file_outside_usr_local_aborts(
        self, rootfs_mod: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(rootfs_mod.subprocess, "run", lambda *args, **kwargs: _completed(""))
        with pytest.raises(SystemExit):
            rootfs_mod.owning_packages({Path("/usr/lib/x86_64-linux-gnu/libwho.so.1")})


# ---------------------------------------------------------------------------
# Dockerfile drift guards: the smoke's one per-service datum must stay true
# ---------------------------------------------------------------------------

_SMOKE_RUN = re.compile(
    r"^RUN --mount=type=bind,from=builder,source=/opt/build,target=/opt/build \\\n"
    r'    \["python", "/opt/build/runtime_smoke\.py", "(gco\.services\.[a-z_]+)"\]$',
    re.MULTILINE,
)
_CMD = re.compile(r'^CMD \["python", "-m", "(gco\.services\.[a-z_]+)"\]$', re.MULTILINE)
_COPY_BUILD_SCRIPTS = (
    "COPY dockerfiles/build_scratch_rootfs.py dockerfiles/runtime_smoke.py /opt/build/"
)


class TestDockerfileSmokeWiring:
    def test_service_dockerfiles_discovered(self) -> None:
        assert len(SERVICE_DOCKERFILES) >= 5, "dockerfile discovery broke"

    @pytest.mark.parametrize("dockerfile", SERVICE_DOCKERFILES, ids=lambda p: p.name)
    def test_smoke_entry_module_matches_cmd_and_exists(self, dockerfile: Path) -> None:
        text = dockerfile.read_text(encoding="utf-8")
        smoke = _SMOKE_RUN.search(text)
        assert smoke, f"{dockerfile.name}: bind-mounted runtime smoke RUN not found"
        cmd = _CMD.search(text)
        assert cmd, f"{dockerfile.name}: exec-form CMD not found"
        assert smoke.group(1) == cmd.group(1), (
            f"{dockerfile.name}: smoke verifies {smoke.group(1)} but CMD runs {cmd.group(1)}"
        )
        module_path = REPO_ROOT / (smoke.group(1).replace(".", "/") + ".py")
        assert module_path.is_file(), f"{dockerfile.name}: {module_path} does not exist"

    @pytest.mark.parametrize("dockerfile", SERVICE_DOCKERFILES, ids=lambda p: p.name)
    def test_builder_copies_both_build_scripts(self, dockerfile: Path) -> None:
        assert _COPY_BUILD_SCRIPTS in dockerfile.read_text(encoding="utf-8"), (
            f"{dockerfile.name}: builder must COPY build_scratch_rootfs.py and "
            "runtime_smoke.py together into /opt/build/"
        )


# ---------------------------------------------------------------------------
# Service images must never import synth-only code
# ---------------------------------------------------------------------------


class TestServiceEntryModulesShipWithoutStacks:
    """Every service image's smoke entry module must import without gco.stacks.

    The CDK service-image assets exclude ``gco/stacks/**`` from their build
    context (``_SERVICE_IMAGE_COMMON_EXCLUDES``: synth-only code must never
    rebuild service images), so any import of ``gco.stacks`` from a service
    module passes every offline test and then fails the distroless runtime
    smoke at deploy time — observed live when the resource-governance
    defaults briefly lived under ``gco.stacks.constants`` (run
    ex241-85d0ae2f). This walk is the offline mirror of that build gate:
    statically follow every gco-internal import reachable from each
    dockerfile's declared entry module and reject the walk if it reaches
    ``gco.stacks``. Runtime-shared values belong in top-level modules such
    as ``gco.resource_governance``.
    """

    _SMOKE_ENTRY = re.compile(r'runtime_smoke\.py",\s*"(gco\.[a-z_.]+)"')

    @staticmethod
    def _module_file(name: str) -> Path | None:
        relative = Path(*name.split("."))
        for candidate in (
            REPO_ROOT / relative.with_suffix(".py"),
            REPO_ROOT / relative / "__init__.py",
        ):
            if candidate.is_file():
                return candidate
        return None

    @classmethod
    def _gco_imports(cls, module: str, source: str) -> set[str]:
        """Every gco-internal module name ``module`` imports (runtime only)."""
        import ast

        tree = ast.parse(source)
        type_checking_nodes: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = node.test
                name = getattr(test, "id", getattr(test, "attr", ""))
                if name == "TYPE_CHECKING":
                    for child in ast.walk(node):
                        type_checking_nodes.add(id(child))
        found: set[str] = set()
        for node in ast.walk(tree):
            if id(node) in type_checking_nodes:
                continue
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package_parts = module.split(".")[: -node.level]
                    base = ".".join([*package_parts, node.module] if node.module else package_parts)
                else:
                    base = node.module or ""
                if base:
                    found.add(base)
                    # `from pkg import sub` may bind a submodule, not an attr.
                    for alias in node.names:
                        if cls._module_file(f"{base}.{alias.name}"):
                            found.add(f"{base}.{alias.name}")
        return {name for name in found if name == "gco" or name.startswith("gco.")}

    def _reachable_gco_modules(self, entry: str) -> set[str]:
        seen: set[str] = set()
        frontier = [entry]
        while frontier:
            module = frontier.pop()
            if module in seen:
                continue
            seen.add(module)
            path = self._module_file(module)
            if path is None:
                continue
            for imported in self._gco_imports(module, path.read_text(encoding="utf-8")):
                if imported not in seen:
                    frontier.append(imported)
                # Importing gco.a.b also imports packages gco and gco.a.
                parts = imported.split(".")
                for end in range(1, len(parts)):
                    parent = ".".join(parts[:end])
                    if parent not in seen:
                        frontier.append(parent)
        return seen

    def _entry_modules(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        for dockerfile in SERVICE_DOCKERFILES:
            match = self._SMOKE_ENTRY.search(dockerfile.read_text(encoding="utf-8"))
            if match:
                entries[dockerfile.name] = match.group(1)
        return entries

    def test_every_service_dockerfile_declares_a_smoke_entry(self) -> None:
        entries = self._entry_modules()
        assert sorted(entries) == [path.name for path in SERVICE_DOCKERFILES]
        for dockerfile, entry in entries.items():
            assert self._module_file(entry) is not None, (
                f"{dockerfile} smoke-tests {entry}, which does not exist"
            )

    def test_no_entry_module_reaches_gco_stacks(self) -> None:
        violations: dict[str, list[str]] = {}
        for dockerfile, entry in self._entry_modules().items():
            reached = self._reachable_gco_modules(entry)
            stacks = sorted(
                name for name in reached if name == "gco.stacks" or name.startswith("gco.stacks.")
            )
            if stacks:
                violations[f"{dockerfile} ({entry})"] = stacks
        assert not violations, (
            "service entry modules transitively import synth-only gco.stacks "
            f"(excluded from their image build context): {violations}"
        )

    def test_walk_actually_traverses_transitive_imports(self) -> None:
        # Sanity: the manifest-api entry must reach the processor module and
        # the shared runtime governance module through the walk; an
        # accidentally inert walker would make the guard above vacuous.
        reached = self._reachable_gco_modules("gco.services.manifest_api")
        assert "gco.services.manifest_processor" in reached
        assert "gco.resource_governance" in reached
