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

import contextlib
import getpass
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Callable
from dataclasses import dataclass
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

    def test_unresolvable_runtime_identity_is_reported_not_raised(
        self,
        smoke_at: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """A distroless image can have no passwd entry for its uid.

        ``getpass.getuser`` then raises rather than returning a name, and the
        smoke check has to record that as a failure like any other. Letting the
        exception escape would abort the check before the remaining probes ran,
        so a broken trust store or missing extension would go unreported behind
        it.
        """
        _write_manifest(tmp_path)

        def _no_passwd_entry() -> str:
            raise OSError("no username found for uid 1000")

        monkeypatch.setattr(getpass, "getuser", _no_passwd_entry)

        assert _run_smoke(smoke_at, monkeypatch, "json") == 1
        err = capsys.readouterr().err
        assert "runtime identity lookup" in err
        assert "OSError" in err

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
# build_scratch_rootfs.py — the whole assembly against a synthetic builder
# ---------------------------------------------------------------------------
#
# The script reads the builder filesystem through ``host()``/``host_glob()``
# (rooted at ``SYSROOT``) and stages into ``ROOTFS``. Pointing both at
# ``tmp_path`` lets the real functions — ``main()`` included — run against a
# minimal Debian-like tree, while ``ldd``/``dpkg``/``ldconfig`` are answered at
# the ``subprocess.run`` seam. The stdlib import probe stays real: it only
# imports stdlib modules by name in an isolated interpreter.

_REAL_RUN = subprocess.run
_PY_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"
_PY = f"python{_PY_VERSION}"
_TRIPLET = "x86_64-linux-gnu"
# Debian's ldd prints the merged-/usr alias form; the files physically live
# under /usr, which is also the form dpkg's database records.
_LIBDIR_ALIAS = f"/lib/{_TRIPLET}"
_LIBDIR = f"/usr/lib/{_TRIPLET}"
_SHIPPED_PACKAGES = ("base-files", "ca-certificates", "libc6", "libgcc-s1", "tzdata", "zlib1g")
_PACKAGE_VERSIONS = {
    "base-files": "13.8",
    "ca-certificates": "20250419",
    "libc6": "2.41-12",
    "libgcc-s1": "14.2.0-19",
    "tzdata": "2025b-4",
    "zlib1g": "1:1.3.dfsg+really1.3.1-1+b1",
}
_DPKG_OWNERS = {
    f"{_LIBDIR}/libc.so.6": "libc6:amd64",
    f"{_LIBDIR}/libm.so.6": "libc6:amd64",
    f"{_LIBDIR}/ld-linux-x86-64.so.2": "libc6:amd64",
    f"{_LIBDIR}/libnss_files.so.2": "libc6:amd64",
    f"{_LIBDIR}/libgcc_s.so.1": "libgcc-s1:amd64",
    f"{_LIBDIR}/libz.so.1.3.1": "zlib1g:amd64",
}
_CA_TEXT = "-----BEGIN CERTIFICATE-----\nMIIBexampleRootCA\n-----END CERTIFICATE-----\n"
_OS_RELEASE = (
    'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n'
    'NAME="Debian GNU/Linux"\n'
    'VERSION_ID="13"\n'
    "ID=debian\n"
)


def _status_paragraph(package: str, version: str) -> str:
    return (
        f"Package: {package}\n"
        "Status: install ok installed\n"
        "Priority: required\n"
        "Section: libs\n"
        "Installed-Size: 1024\n"
        "Maintainer: Debian Maintainers <debian-devel@lists.debian.org>\n"
        "Architecture: amd64\n"
        f"Version: {version}\n"
        f"Description: synthetic {package}\n"
        " Test double for one dpkg status paragraph.\n"
    )


class _FakeBuilder:
    """A minimal Debian-like builder tree plus the ``subprocess.run`` seam.

    ``sysroot`` stands in for the builder's ``/`` (the script's ``SYSROOT``),
    ``rootfs`` for the staging target, ``build_dir`` for ``/opt/build`` where
    the script and ``runtime_smoke.py`` live. ``run`` answers ``ldd``,
    ``dpkg -S`` and ``ldconfig`` from the synthetic layout and records every
    call; the stdlib import probe is delegated to the real interpreter unless
    ``probe_result`` is set.
    """

    def __init__(self, tmp_path: Path, stdlib_extensions: list[str]) -> None:
        self.sysroot = tmp_path / "sysroot"
        self.rootfs = tmp_path / "rootfs"
        self.build_dir = tmp_path / "opt" / "build"
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.probe_result: subprocess.CompletedProcess[str] | None = None
        self.ldd_reports_interpreter = True
        self.stdlib_extensions = [*stdlib_extensions, "_tkinter"]
        self._lay_out()

    # -- synthetic builder layout -------------------------------------------

    def file(self, relative: str, content: str | bytes = "", mode: int | None = None) -> Path:
        path = self.sysroot / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        if mode is not None:
            path.chmod(mode)
        return path

    def link(self, relative: str, target: str) -> Path:
        path = self.sysroot / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        return path

    def _lay_out(self) -> None:
        # CPython as python:*-slim installs it: everything under /usr/local.
        self.file(f"usr/local/bin/{_PY}", b"\x7fELF interpreter", mode=0o755)
        self.link("usr/local/bin/python3", _PY)
        _fake_dynload(self.sysroot / "usr/local", self.stdlib_extensions)
        stdlib = next((self.sysroot / "usr/local/lib").glob("python3.*"))
        # Builder-absolute stdlib directory, e.g. /usr/local/lib/python3.14.
        self.stdlib = Path("/") / stdlib.relative_to(self.sysroot)
        stdlib_rel = stdlib.relative_to(self.sysroot)
        self.file(f"usr/local/lib/libpython{_PY_VERSION}.so.1.0", b"\x7fELF libpython")
        self.link(f"usr/local/lib/libpython{_PY_VERSION}.so", f"libpython{_PY_VERSION}.so.1.0")
        self.file(f"{stdlib_rel}/site-packages/pydantic_core/__init__.py")
        self.file(
            f"{stdlib_rel}/site-packages/pydantic_core/_pydantic_core.{_SO_SUFFIX}",
            b"\x7fELF rust extension",
        )
        # A directory whose *name* matches the ``*.so*`` seed glob (a dotted
        # distribution's dist-info); it must not become an ldd seed.
        self.file(f"{stdlib_rel}/site-packages/acme.sockets-1.0.dist-info/METADATA", "Name: x\n")
        self.file(f"{stdlib_rel}/ensurepip/__init__.py")
        self.file(f"{stdlib_rel}/ensurepip/_bundled/pip-25.1-py3-none-any.whl", b"PK")
        # The precompiled application tree.
        self.file("app/gco/__init__.py", '"""gco"""\n')
        self.file("app/gco/services/__init__.py")
        self.file("app/gco/services/__pycache__/manifest_api.cpython-314.pyc", b"\x00pyc")
        # Merged-/usr Debian: the aliases are symlinks, files live under /usr.
        for alias in ("lib", "lib64", "bin", "sbin"):
            self.link(alias, f"usr/{alias}")
        (self.sysroot / "usr/bin").mkdir()
        (self.sysroot / "usr/sbin").mkdir()
        libdir = f"usr/lib/{_TRIPLET}"
        for name in (
            "libc.so.6",
            "libm.so.6",
            "libgcc_s.so.1",
            "libnss_files.so.2",  # libnss_dns.so.2 deliberately absent
            "libz.so.1.3.1",
            "ld-linux-x86-64.so.2",
        ):
            self.file(f"{libdir}/{name}", b"\x7fELF " + name.encode())
        self.link(f"{libdir}/libz.so.1", "libz.so.1.3.1")
        # libc6 ships the PT_INTERP path as an absolute symlink into /lib.
        self.link("usr/lib64/ld-linux-x86-64.so.2", f"{_LIBDIR_ALIAS}/ld-linux-x86-64.so.2")
        # CA trust: the hashed-symlink farm points into /usr/share (relative
        # here so the dereference stays inside the synthetic builder).
        self.file("usr/share/ca-certificates/mozilla/Example_Root_CA.crt", _CA_TEXT)
        self.file("etc/ssl/certs/ca-certificates.crt", _CA_TEXT * 2)
        self.link(
            "etc/ssl/certs/Example_Root_CA.pem",
            "../../../usr/share/ca-certificates/mozilla/Example_Root_CA.crt",
        )
        self.link("etc/ssl/certs/abcd1234.0", "Example_Root_CA.pem")
        self.file("etc/ssl/openssl.cnf", "openssl_conf = openssl_init\n")
        # OPENSSLDIR: symlinks into /etc/ssl, one regular file, one directory.
        self.link("usr/lib/ssl/certs", "/etc/ssl/certs")
        self.link("usr/lib/ssl/openssl.cnf", "/etc/ssl/openssl.cnf")
        self.link("usr/lib/ssl/private", "/etc/ssl/private")
        self.file("usr/lib/ssl/openssl.cnf.dist", "# distributed default\n")
        (self.sysroot / "usr/lib/ssl/misc").mkdir()
        # tzdata, with its symlink aliases.
        self.file("usr/share/zoneinfo/Etc/UTC", b"TZif2\x00UTC")
        self.link("usr/share/zoneinfo/UTC", "Etc/UTC")
        # OS identity.
        self.file("etc/debian_version", "13.1\n")
        self.file("usr/lib/os-release", _OS_RELEASE)
        self.link("etc/os-release", "../usr/lib/os-release")
        # dpkg database: dpkg terminates every paragraph with a blank line, so
        # the file ends in "\n\n" and splitting yields a trailing empty
        # paragraph with no Package: field.
        paragraphs = [
            _status_paragraph(package, version) for package, version in _PACKAGE_VERSIONS.items()
        ]
        self.file("var/lib/dpkg/status", "\n".join(paragraphs) + "\n")
        for package in _PACKAGE_VERSIONS:
            self.file(f"usr/share/doc/{package}/copyright", f"{package} license text\n")
        # /opt/build: the script's own directory, where the manifest lands.
        self.build_dir.mkdir(parents=True)
        shutil.copy2(DOCKERFILES / "runtime_smoke.py", self.build_dir / "runtime_smoke.py")

    # -- seams ----------------------------------------------------------------

    def install(self, module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "SYSROOT", self.sysroot)
        monkeypatch.setattr(module, "ROOTFS", self.rootfs)
        monkeypatch.setattr(module, "subprocess", SimpleNamespace(run=self.run))
        monkeypatch.setattr(module, "sysconfig", SimpleNamespace(get_config_var=self.config_var))
        monkeypatch.setattr(module, "__file__", str(self.build_dir / "build_scratch_rootfs.py"))

    @staticmethod
    def config_var(name: str) -> object:
        # macOS reports MULTIARCH as "darwin" (or nothing); the image is Debian.
        return _TRIPLET if name == "MULTIARCH" else sysconfig.get_config_var(name)

    def run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        tool = argv[0]
        if tool == "ldd":
            return _completed(self._ldd(argv[1:]))
        if tool == "dpkg":
            assert argv[1] == "-S"
            return self._dpkg_search(argv)
        if tool == "ldconfig":
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        if tool == sys.executable:
            return self.probe_result or _REAL_RUN(argv, **kwargs)
        raise AssertionError(f"unexpected subprocess: {argv}")

    def calls_to(self, tool: str) -> list[tuple[list[str], dict[str, object]]]:
        return [(argv, kwargs) for argv, kwargs in self.calls if argv[0] == tool]

    def _ldd(self, files: list[str]) -> str:
        lines: list[str] = []
        for file in files:
            # ldd prefixes each section with "<path>:" only for several files.
            if len(files) > 1:
                lines.append(f"{file}:")
            lines.extend(self._ldd_section(Path(file).name))
        return "\n".join(lines) + "\n"

    def _ldd_section(self, name: str) -> list[str]:
        # linux-vdso has no path: it matches neither ldd regex and is skipped.
        lines = ["\tlinux-vdso.so.1 (0x00007ffd3b5f9000)"]
        if name == _PY:
            lines.append(
                f"\tlibpython{_PY_VERSION}.so.1.0 => /usr/local/lib/libpython{_PY_VERSION}.so.1.0"
                " (0x00007f1a8e400000)"
            )
            lines.append(f"\tlibm.so.6 => {_LIBDIR_ALIAS}/libm.so.6 (0x00007f1a8e300000)")
        elif name.startswith("libpython"):
            lines.append(f"\tlibm.so.6 => {_LIBDIR_ALIAS}/libm.so.6 (0x00007f1a8e300000)")
        elif name.startswith("_tkinter."):
            # python:*-slim ships _tkinter but never libtk/libtcl.
            lines.append("\tlibtk8.6.so => not found")
            lines.append("\tlibtcl8.6.so => not found")
        elif name.startswith("zlib."):
            lines.append(f"\tlibz.so.1 => {_LIBDIR_ALIAS}/libz.so.1 (0x00007f1a8e200000)")
        elif name.startswith("_pydantic_core."):
            # Rust extensions DT_NEED libgcc_s for unwinding.
            lines.append(f"\tlibgcc_s.so.1 => {_LIBDIR_ALIAS}/libgcc_s.so.1 (0x00007f1a8e100000)")
        lines.append(f"\tlibc.so.6 => {_LIBDIR_ALIAS}/libc.so.6 (0x00007f1a8e000000)")
        if self.ldd_reports_interpreter:
            lines.append("\t/lib64/ld-linux-x86-64.so.2 (0x00007f1a8e7c9000)")
        return lines

    @staticmethod
    def _dpkg_search(argv: list[str]) -> subprocess.CompletedProcess[str]:
        paths = argv[2:]
        owned = [f"{_DPKG_OWNERS[path]}: {path}\n" for path in paths if path in _DPKG_OWNERS]
        unknown = [
            f"dpkg-query: no path found matching pattern {path}\n"
            for path in paths
            if path not in _DPKG_OWNERS
        ]
        # dpkg -S exits 1 when any pattern is unmatched; the script ignores that.
        return subprocess.CompletedProcess(
            args=argv,
            returncode=1 if unknown else 0,
            stdout="".join(owned),
            stderr="".join(unknown),
        )


def _resolve_within(rootfs: Path, path: str) -> Path:
    """Follow ``path`` as the kernel would inside an image rooted at ``rootfs``.

    Absolute symlink targets are re-rooted under ``rootfs`` rather than the
    test host, so merged-/usr alias chains can be checked end to end.
    """
    current = Path(path)
    for _ in range(16):
        staged = rootfs / current.relative_to("/")
        if not staged.is_symlink():
            assert staged.is_file(), f"{path} resolves to nothing inside the rootfs"
            return staged
        target = Path(os.readlink(staged))
        current = Path(
            os.path.normpath(target if target.is_absolute() else current.parent / target)
        )
    raise AssertionError(f"symlink loop resolving {path} inside the rootfs")


def _assert_aborts(
    capsys: pytest.CaptureFixture[str], action: Callable[[], object], message: str
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        action()
    assert excinfo.value.code == 1
    assert f"build_scratch_rootfs: ERROR: {message}" in capsys.readouterr().err


@pytest.fixture
def builder(
    rootfs_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> _FakeBuilder:
    """A fresh synthetic builder wired into the script for one test."""
    fake = _FakeBuilder(tmp_path, sorted(rootfs_mod.CRITICAL_STDLIB_EXTENSIONS))
    fake.install(rootfs_mod, monkeypatch)
    return fake


@dataclass(frozen=True)
class _Assembled:
    builder: _FakeBuilder
    stdout: str


@pytest.fixture(scope="class")
def assembled(rootfs_mod: ModuleType, tmp_path_factory: pytest.TempPathFactory) -> _Assembled:
    """One real ``main()`` run against the synthetic builder, shared by a class.

    The stdlib probe is a real interpreter subprocess (about a second), so the
    assembly happens once and each test below inspects a different facet of
    the staged tree.
    """
    fake = _FakeBuilder(
        tmp_path_factory.mktemp("scratch-rootfs"), sorted(rootfs_mod.CRITICAL_STDLIB_EXTENSIONS)
    )
    stdout = io.StringIO()
    with pytest.MonkeyPatch.context() as monkeypatch:
        fake.install(rootfs_mod, monkeypatch)
        with contextlib.redirect_stdout(stdout):
            rootfs_mod.main()
    return _Assembled(fake, stdout.getvalue())


class TestSyntheticBuilderAssembly:
    """``main()`` end to end: the staged tree is what a scratch image needs."""

    def test_stages_interpreter_stdlib_and_app_tree_without_ensurepip(
        self, assembled: _Assembled, rootfs_mod: ModuleType
    ) -> None:
        """/usr/local and /app/gco are copied wholesale (symlinks kept); ensurepip is dropped."""
        rootfs = assembled.builder.rootfs
        interpreter = rootfs / "usr/local/bin" / _PY
        assert interpreter.is_file()
        assert interpreter.stat().st_mode & 0o777 == 0o755
        assert os.readlink(rootfs / "usr/local/bin/python3") == _PY
        stdlib = rootfs / assembled.builder.stdlib.relative_to("/")
        staged_extensions = sorted(p.name.split(".")[0] for p in (stdlib / "lib-dynload").iterdir())
        assert staged_extensions == sorted([*rootfs_mod.CRITICAL_STDLIB_EXTENSIONS, "_tkinter"])
        assert (rootfs / f"usr/local/lib/libpython{_PY_VERSION}.so.1.0").is_file()
        assert (
            os.readlink(rootfs / f"usr/local/lib/libpython{_PY_VERSION}.so")
            == f"libpython{_PY_VERSION}.so.1.0"
        )
        assert (stdlib / "site-packages/pydantic_core" / f"_pydantic_core.{_SO_SUFFIX}").is_file()
        assert not (stdlib / "ensurepip").exists()
        assert not list(rootfs.rglob("*.whl"))
        assert (rootfs / "app/gco/__init__.py").read_text(encoding="utf-8") == '"""gco"""\n'
        assert (rootfs / "app/gco/services/__pycache__/manifest_api.cpython-314.pyc").is_file()

    def test_seeds_every_elf_object_exactly_once(
        self, assembled: _Assembled, rootfs_mod: ModuleType
    ) -> None:
        """ldd sees the interpreter, every extension, libpython and the forced/NSS libraries.

        Directories matching the ``*.so*`` glob and absent optional NSS
        plugins are not seeds, and one chunk covers the whole set.
        """
        builder = assembled.builder
        ((argv, kwargs),) = builder.calls_to("ldd")
        expected = {
            f"/usr/local/bin/{_PY}",
            *(
                str(builder.stdlib / "lib-dynload" / f"{name}.{_SO_SUFFIX}")
                for name in builder.stdlib_extensions
            ),
            f"/usr/local/lib/libpython{_PY_VERSION}.so",
            f"/usr/local/lib/libpython{_PY_VERSION}.so.1.0",
            str(builder.stdlib / "site-packages/pydantic_core" / f"_pydantic_core.{_SO_SUFFIX}"),
            f"{_LIBDIR}/libgcc_s.so.1",
            f"{_LIBDIR}/libnss_files.so.2",
        }
        assert set(argv[1:]) == expected
        assert len(argv[1:]) == len(expected)
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        assert f"{_LIBDIR}/libnss_dns.so.2" not in argv
        assert len(rootfs_mod.CRITICAL_STDLIB_EXTENSIONS) + 1 == len(builder.stdlib_extensions)

    def test_replicates_the_shared_library_closure_under_usr(self, assembled: _Assembled) -> None:
        """Alias paths from ldd land under /usr; symlink hops are recreated, real files copied."""
        builder = assembled.builder
        rootfs = builder.rootfs
        libdir = rootfs / f"usr/lib/{_TRIPLET}"
        for name in (
            "libc.so.6",
            "libm.so.6",
            "libgcc_s.so.1",
            "ld-linux-x86-64.so.2",
            "libz.so.1.3.1",
        ):
            staged = libdir / name
            assert staged.is_file() and not staged.is_symlink(), name
            assert (
                staged.read_bytes() == (builder.sysroot / f"usr/lib/{_TRIPLET}" / name).read_bytes()
            )
        assert os.readlink(libdir / "libz.so.1") == "libz.so.1.3.1"
        # libc6's absolute PT_INTERP link is replicated verbatim ...
        assert (
            os.readlink(rootfs / "usr/lib64/ld-linux-x86-64.so.2")
            == f"{_LIBDIR_ALIAS}/ld-linux-x86-64.so.2"
        )
        # ... and the kernel's /lib64/ld-linux path resolves to a regular file
        # inside the image, through the recreated aliases.
        assert _resolve_within(rootfs, "/lib64/ld-linux-x86-64.so.2").samefile(
            libdir / "ld-linux-x86-64.so.2"
        )
        assert _resolve_within(rootfs, f"{_LIBDIR_ALIAS}/libz.so.1").samefile(
            libdir / "libz.so.1.3.1"
        )
        # Nothing is staged at the alias paths themselves.
        assert not (rootfs / "lib").is_dir() or (rootfs / "lib").is_symlink()

    def test_recreates_merged_usr_aliases_only_where_usr_is_populated(
        self, assembled: _Assembled
    ) -> None:
        """/lib and /lib64 point into /usr; /bin and /sbin are absent because nothing ships there."""
        rootfs = assembled.builder.rootfs
        assert os.readlink(rootfs / "lib") == "usr/lib"
        assert os.readlink(rootfs / "lib64") == "usr/lib64"
        for alias in ("bin", "sbin"):
            assert not (rootfs / alias).is_symlink()
            assert not (rootfs / alias).exists()
            assert not (rootfs / "usr" / alias).exists()

    def test_writes_runtime_identity_and_os_metadata(self, assembled: _Assembled) -> None:
        """passwd/group/nsswitch for uid 1000, Debian identity files, home and sticky tmp dirs."""
        builder = assembled.builder
        rootfs = builder.rootfs
        etc = rootfs / "etc"
        assert (etc / "passwd").read_text(encoding="utf-8") == (
            "root:x:0:0:root:/root:/usr/sbin/nologin\n"
            "gco:x:1000:1000:gco:/home/gco:/usr/sbin/nologin\n"
        )
        assert (etc / "group").read_text(encoding="utf-8") == "root:x:0:\ngco:x:1000:\n"
        assert (etc / "nsswitch.conf").read_text(encoding="utf-8") == (
            "passwd: files\ngroup: files\nhosts: files dns\n"
        )
        assert (etc / "debian_version").read_text(encoding="utf-8") == "13.1\n"
        for os_release in (etc / "os-release", rootfs / "usr/lib/os-release"):
            assert os_release.is_file() and not os_release.is_symlink()
            assert os_release.read_text(encoding="utf-8") == _OS_RELEASE
        assert (rootfs / "home/gco").is_dir()
        for scratch in ("tmp", "var/tmp"):
            assert (rootfs / scratch).is_dir()
            assert (rootfs / scratch).stat().st_mode & 0o7777 == 0o1777

    def test_materializes_trust_anchors_and_zoneinfo(self, assembled: _Assembled) -> None:
        """Cert symlinks become real files, OPENSSLDIR links stay links, tzdata keeps its links."""
        builder = assembled.builder
        rootfs = builder.rootfs
        certs = rootfs / "etc/ssl/certs"
        assert (certs / "ca-certificates.crt").read_text(encoding="utf-8") == _CA_TEXT * 2
        for entry in ("Example_Root_CA.pem", "abcd1234.0"):
            assert (certs / entry).is_file() and not (certs / entry).is_symlink(), entry
            assert (certs / entry).read_text(encoding="utf-8") == _CA_TEXT
        assert not (rootfs / "usr/share/ca-certificates").exists()
        assert (rootfs / "etc/ssl/openssl.cnf").read_text(encoding="utf-8") == (
            "openssl_conf = openssl_init\n"
        )
        ssl_dir = rootfs / "usr/lib/ssl"
        assert os.readlink(ssl_dir / "certs") == "/etc/ssl/certs"
        assert os.readlink(ssl_dir / "openssl.cnf") == "/etc/ssl/openssl.cnf"
        assert os.readlink(ssl_dir / "private") == "/etc/ssl/private"
        assert (ssl_dir / "openssl.cnf.dist").is_file() and not (
            ssl_dir / "openssl.cnf.dist"
        ).is_symlink()
        assert not (ssl_dir / "misc").exists()
        private = rootfs / "etc/ssl/private"
        assert private.is_dir()
        assert private.stat().st_mode & 0o777 == 0o700
        zoneinfo = rootfs / "usr/share/zoneinfo"
        assert (zoneinfo / "Etc/UTC").read_bytes() == b"TZif2\x00UTC"
        assert os.readlink(zoneinfo / "UTC") == "Etc/UTC"
        assert os.readlink(rootfs / "etc/localtime") == "/usr/share/zoneinfo/Etc/UTC"
        assert _resolve_within(rootfs, "/etc/localtime").samefile(zoneinfo / "Etc/UTC")
        assert (rootfs / "etc/timezone").read_text(encoding="utf-8") == "Etc/UTC\n"

    def test_attributes_shipped_files_to_packages_with_licenses(
        self, assembled: _Assembled
    ) -> None:
        """dpkg is asked about the real files in /usr form; status.d + copyright ship per package."""
        builder = assembled.builder
        rootfs = builder.rootfs
        ((argv, kwargs),) = builder.calls_to("dpkg")
        assert argv[:2] == ["dpkg", "-S"]
        assert kwargs == {"capture_output": True, "text": True, "check": False}
        queried = argv[2:]
        assert queried == sorted(queried)
        assert all(path.startswith("/usr/") for path in queried)
        assert f"{_LIBDIR}/libz.so.1.3.1" in queried
        assert f"{_LIBDIR}/libz.so.1" not in queried
        assert f"{_LIBDIR}/ld-linux-x86-64.so.2" in queried
        # CPython's own library is not dpkg-owned and is tolerated under /usr/local.
        assert f"/usr/local/lib/libpython{_PY_VERSION}.so.1.0" in queried
        status_d = rootfs / "var/lib/dpkg/status.d"
        assert sorted(entry.name for entry in status_d.iterdir()) == list(_SHIPPED_PACKAGES)
        for package in _SHIPPED_PACKAGES:
            paragraph = (status_d / package).read_text(encoding="utf-8")
            assert paragraph == _status_paragraph(package, _PACKAGE_VERSIONS[package])
            copyright_file = rootfs / "usr/share/doc" / package / "copyright"
            assert copyright_file.read_text(encoding="utf-8") == f"{package} license text\n"
        # The monolithic database itself does not ship.
        assert not (rootfs / "var/lib/dpkg/status").exists()

    def test_probes_stdlib_and_writes_the_parity_manifest_outside_the_rootfs(
        self, assembled: _Assembled, rootfs_mod: ModuleType
    ) -> None:
        """Every enumerated extension lands in exactly one manifest bucket; the file stays in /opt/build."""
        builder = assembled.builder
        manifest_path = builder.build_dir / "runtime_smoke_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = sys.version_info
        assert manifest["python"] == f"{version.major}.{version.minor}.{version.micro}"
        assert manifest["runtime_user"] == "gco"
        importable = manifest["stdlib_extensions"]
        broken = manifest["expected_broken"]
        assert importable == sorted(importable)
        assert set(importable) >= rootfs_mod.CRITICAL_STDLIB_EXTENSIONS
        assert set(importable) | set(broken) == set(builder.stdlib_extensions)
        assert set(importable).isdisjoint(broken)
        assert not list(builder.rootfs.rglob("runtime_smoke_manifest.json"))
        ((argv, kwargs),) = builder.calls_to(sys.executable)
        assert argv[:3] == [sys.executable, "-I", "-c"]
        assert json.loads(str(kwargs["input"])) == sorted(builder.stdlib_extensions)
        assert (
            f"runtime smoke manifest lists {len(importable)} builder-importable stdlib extensions"
            in assembled.stdout
        )
        # The builder-broken extension is excluded from the closure with a notice.
        assert "skipping stdlib extension already broken in the builder image: _tkinter" in (
            assembled.stdout
        )
        assert "(missing: libtk8.6.so => not found, libtcl8.6.so => not found)" in assembled.stdout

    def test_prewarms_the_linker_cache_and_reports_the_summary(self, assembled: _Assembled) -> None:
        """ldconfig runs against the staged tree (check=True) and the summary counts packages."""
        builder = assembled.builder
        assert builder.calls_to("ldconfig") == [
            (["ldconfig", "-r", str(builder.rootfs)], {"check": True})
        ]
        assert [argv[0] for argv, _ in builder.calls] == ["ldd", sys.executable, "dpkg", "ldconfig"]
        assert assembled.stdout.rstrip().splitlines()[-1] == (
            "build_scratch_rootfs: staged 6 shared objects from 6 Debian packages: "
            "base-files ca-certificates libc6 libgcc-s1 tzdata zlib1g"
        )

    def test_replicate_symlink_chain_stages_each_hop_once(
        self, builder: _FakeBuilder, rootfs_mod: ModuleType
    ) -> None:
        """Every hop of a SONAME chain is recreated; re-running is a no-op with the same real file."""
        alias = Path(f"{_LIBDIR_ALIAS}/libz.so.1")
        real = rootfs_mod.replicate_symlink_chain(alias)
        assert real == Path(f"{_LIBDIR_ALIAS}/libz.so.1.3.1")
        libdir = builder.rootfs / f"usr/lib/{_TRIPLET}"
        assert os.readlink(libdir / "libz.so.1") == "libz.so.1.3.1"
        assert (libdir / "libz.so.1.3.1").read_bytes() == b"\x7fELF libz.so.1.3.1"
        assert rootfs_mod.replicate_symlink_chain(alias) == real
        assert sorted(entry.name for entry in libdir.iterdir()) == ["libz.so.1", "libz.so.1.3.1"]


class TestAssemblyAborts:
    """Every hard failure exits 1 with a specific message on stderr."""

    def test_refuses_to_assemble_over_an_existing_rootfs(
        self, builder: _FakeBuilder, rootfs_mod: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A pre-existing ROOTFS aborts before anything is copied or executed."""
        builder.rootfs.mkdir()
        _assert_aborts(
            capsys, rootfs_mod.main, "/rootfs already exists; refusing to assemble over prior state"
        )
        assert not builder.calls
        assert list(builder.rootfs.iterdir()) == []

    def test_missing_multiarch_triplet(
        self,
        builder: _FakeBuilder,
        rootfs_mod: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No MULTIARCH from sysconfig means the library directory cannot be located."""
        monkeypatch.setattr(
            rootfs_mod, "sysconfig", SimpleNamespace(get_config_var=lambda name: None)
        )
        _assert_aborts(capsys, rootfs_mod.multiarch_dir, "sysconfig reports no MULTIARCH triplet")

    def test_missing_forced_library(
        self, builder: _FakeBuilder, rootfs_mod: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A FORCED_LIBS entry absent from the builder is a hard error, not a silent skip."""
        (builder.sysroot / f"usr/lib/{_TRIPLET}/libgcc_s.so.1").unlink()
        _assert_aborts(
            capsys,
            rootfs_mod.seed_binaries,
            f"forced library missing from builder: {_LIBDIR}/libgcc_s.so.1",
        )

    def test_symlink_loop_is_too_deep(
        self, builder: _FakeBuilder, rootfs_mod: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A self-referential SONAME chain exhausts the hop budget after staging each link once."""
        builder.link(f"usr/lib/{_TRIPLET}/libloop.so.1", "libloop.so.1.0")
        builder.link(f"usr/lib/{_TRIPLET}/libloop.so.1.0", "libloop.so.1")
        _assert_aborts(
            capsys,
            lambda: rootfs_mod.replicate_symlink_chain(Path(f"{_LIBDIR_ALIAS}/libloop.so.1")),
            f"symlink chain too deep at {_LIBDIR_ALIAS}/libloop.so.1",
        )
        libdir = builder.rootfs / f"usr/lib/{_TRIPLET}"
        assert os.readlink(libdir / "libloop.so.1") == "libloop.so.1.0"
        assert os.readlink(libdir / "libloop.so.1.0") == "libloop.so.1"

    def test_package_without_status_paragraph(
        self, builder: _FakeBuilder, rootfs_mod: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An owning package missing from /var/lib/dpkg/status aborts (after earlier ones are written)."""
        _assert_aborts(
            capsys,
            lambda: rootfs_mod.write_dpkg_metadata({"libc6", "libmystery1"}),
            "package libmystery1 owns shipped files but has no status paragraph",
        )
        assert (builder.rootfs / "var/lib/dpkg/status.d/libc6").is_file()
        assert not (builder.rootfs / "var/lib/dpkg/status.d/libmystery1").exists()

    def test_package_without_copyright(
        self, builder: _FakeBuilder, rootfs_mod: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Redistributing a package without its license text is refused."""
        (builder.sysroot / "usr/share/doc/zlib1g/copyright").unlink()
        _assert_aborts(
            capsys,
            lambda: rootfs_mod.write_dpkg_metadata({"zlib1g"}),
            "missing license text for redistributed package: /usr/share/doc/zlib1g/copyright",
        )
        assert (builder.rootfs / "var/lib/dpkg/status.d/zlib1g").is_file()

    @pytest.mark.parametrize("bundle_state", ["missing", "empty"])
    def test_ca_bundle_missing_or_empty(
        self,
        builder: _FakeBuilder,
        rootfs_mod: ModuleType,
        capsys: pytest.CaptureFixture[str],
        bundle_state: str,
    ) -> None:
        """No usable CA bundle after staging aborts before zoneinfo; a missing openssl.cnf is skipped."""
        bundle = builder.sysroot / "etc/ssl/certs/ca-certificates.crt"
        if bundle_state == "missing":
            bundle.unlink()
        else:
            bundle.write_text("", encoding="utf-8")
        (builder.sysroot / "etc/ssl/openssl.cnf").unlink()
        _assert_aborts(
            capsys, rootfs_mod.copy_trust_and_time, "CA bundle missing or empty after staging"
        )
        assert (builder.rootfs / "etc/ssl/certs/Example_Root_CA.pem").is_file()
        assert not (builder.rootfs / "etc/ssl/openssl.cnf").exists()
        assert not (builder.rootfs / "usr/share/zoneinfo").exists()

    def test_probe_crash_surfaces_its_stderr(
        self, builder: _FakeBuilder, rootfs_mod: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A non-zero probe interpreter aborts with the probe's stderr, not a JSON error."""
        builder.probe_result = subprocess.CompletedProcess(
            args=[sys.executable],
            returncode=1,
            stdout="",
            stderr="Fatal Python error: init_import_site: Failed to import the site module\n",
        )
        _assert_aborts(
            capsys,
            rootfs_mod.probe_stdlib_extensions,
            "stdlib import probe crashed: Fatal Python error: init_import_site: "
            "Failed to import the site module",
        )

    def test_closure_without_dynamic_linker(
        self, builder: _FakeBuilder, rootfs_mod: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """If ldd never reports ld-linux the fully staged tree is still rejected at the end."""
        builder.ldd_reports_interpreter = False
        _assert_aborts(capsys, rootfs_mod.main, "dynamic linker never entered the closure")
        # Everything else had already been staged: the check is the last gate.
        assert builder.calls_to("ldconfig")
        assert (builder.rootfs / f"usr/lib/{_TRIPLET}/libc.so.6").is_file()
        assert not (builder.rootfs / "usr/lib64").exists()
        assert not (builder.rootfs / "lib64").is_symlink()


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
