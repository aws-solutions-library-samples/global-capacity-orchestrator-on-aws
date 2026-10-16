"""Umask-proof Lambda asset builds, staged copies, and the deploy-time readability preflight.

``pip install -t``, ``npm ci`` and ``shutil.copytree`` create a staged build
under the process umask (or copy a checkout's own modes), and CDK's asset hash
ignores modes. A deploy from a shell running ``umask 077`` therefore used to
upload Lambda code that the Lambda runtime cannot read, and the bootstrap
bucket then served that zip to every later deploy of the same content. CDK's
own copy of each asset in the cloud assembly takes the umask too: its
directories were owner-only, so the service images failed as their non-root
users. These tests pin the defences in ``cli/stacks.py``: staged builds are
given git's modes before their completion manifest is written, an owner-only
build published before that no longer counts as fresh, every copy a cloud
assembly publishes is given git's modes once ``app.py`` has synthesized it,
and ``deploy`` refuses a checkout whose deployable sources other users cannot
read. They also pin what the stacks package from a ``lambda/`` source
directory: never the caches a local test run leaves in it.
"""

from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cli.stacks as stacks

# Other suites reload cli.stacks, so classes are always looked up on the module
# at call time rather than bound once at import.

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")

REPO_ROOT = Path(__file__).resolve().parents[1]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _write(path: Path, content: str = "x\n", mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)
    return path


@pytest.fixture
def restrictive_umask() -> Iterator[None]:
    """Build under ``umask 077`` like the checkout that shipped unreadable zips."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


# ---------------------------------------------------------------------------
# Staged builds get git's modes
# ---------------------------------------------------------------------------


def test_mode_bits_gate_reads_everywhere_but_windows() -> None:
    with patch.object(stacks.os, "name", "nt"):
        on_windows = stacks._modes_gate_reads()
    with patch.object(stacks.os, "name", "posix"):
        on_posix = stacks._modes_gate_reads()
    assert (on_windows, on_posix) == (False, True)


def test_staged_builds_get_git_modes_whatever_the_umask(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    module = _write(staging / "pkg" / "module.py", mode=0o600)
    tool = _write(staging / "pkg" / "bin" / "tool", "#!/bin/sh\n", mode=0o700)
    already = _write(staging / "pkg" / "already.txt", mode=0o644)
    outside = _write(tmp_path / "outside.txt", mode=0o600)
    link = staging / "pkg" / "link"
    link.symlink_to(outside)
    # Owner-only directories, as a build under umask 077 leaves them. Semgrep's
    # permissions audit flags any mode above 0o644, owner-only 0o700 included.
    for directory in (staging / "pkg" / "bin", staging / "pkg", staging):
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(directory, 0o700)

    assert stacks._normalize_asset_modes(staging) is True
    assert stacks._normalize_asset_modes(staging) is False  # nothing left to correct

    assert _mode(staging) == 0o755
    assert _mode(staging / "pkg") == 0o755
    assert _mode(staging / "pkg" / "bin") == 0o755
    assert _mode(module) == 0o644
    assert _mode(tool) == 0o755
    assert _mode(already) == 0o644
    # The link keeps its own mode, and chmod never followed it out of the tree.
    assert link.is_symlink()
    assert _mode(outside) == 0o600


def test_a_default_umask_build_keeps_its_digest(tmp_path: Path) -> None:
    """Git's modes are what ``umask 022`` already produces: no rebuild churn."""
    build = tmp_path / "build"
    _write(build / "handler.py", mode=0o644)
    _write(build / "bin" / "tool", mode=0o755)
    for directory in (build, build / "bin"):  # what mkdir makes under umask 022
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(directory, 0o755)
    before = stacks._asset_tree_digest(build)

    assert stacks._normalize_asset_modes(build) is False

    assert before is not None
    assert stacks._asset_tree_digest(build) == before


def test_a_single_file_or_symlinked_root_is_normalized_without_following_links(
    tmp_path: Path,
) -> None:
    single = _write(tmp_path / "asset.single.py", mode=0o600)
    tool = _write(tmp_path / "asset.tool", mode=0o700)
    outside = _write(tmp_path / "outside" / "private.txt", mode=0o600)
    link = tmp_path / "asset.link"
    link.symlink_to(outside.parent, target_is_directory=True)

    assert stacks._normalize_asset_modes(single) is True
    assert stacks._normalize_asset_modes(tool) is True
    assert stacks._normalize_asset_modes(link) is False

    assert (_mode(single), _mode(tool)) == (0o644, 0o755)
    assert _mode(outside) == 0o600


def test_modes_are_left_alone_where_they_do_not_gate_reads(tmp_path: Path) -> None:
    private = _write(tmp_path / "lambda" / "fn" / "handler.py", mode=0o600)
    with (
        patch.object(stacks, "_modes_gate_reads", return_value=False),
        patch.object(stacks, "unreadable_asset_sources") as walk,
        patch.object(stacks, "_published_asset_copies") as copies,
    ):
        normalized = stacks._normalize_asset_modes(tmp_path)
        world_readable = stacks._asset_tree_is_world_readable(tmp_path)
        stacks.check_asset_sources_readable(tmp_path)
        corrected = stacks.normalize_staged_asset_modes(tmp_path)
    assert _mode(private) == 0o600
    assert (normalized, world_readable, corrected) == (False, True, [])
    walk.assert_not_called()
    copies.assert_not_called()


# ---------------------------------------------------------------------------
# The completion manifest and freshness
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("restrictive_umask")
def test_the_completion_manifest_is_published_world_readable(tmp_path: Path) -> None:
    _write(tmp_path / "handler.py")

    stacks._write_build_manifest(tmp_path, "source")

    assert _mode(tmp_path / stacks._LAMBDA_BUILD_MANIFEST) == 0o644


@pytest.mark.usefixtures("restrictive_umask")
def test_the_manifest_keeps_its_mode_where_modes_do_not_gate_reads(tmp_path: Path) -> None:
    _write(tmp_path / "handler.py")

    with patch.object(stacks, "_modes_gate_reads", return_value=False):
        stacks._write_build_manifest(tmp_path, "source")

    assert _mode(tmp_path / stacks._LAMBDA_BUILD_MANIFEST) == 0o600


def test_hidden_entries_are_files_others_cannot_read_or_directories_they_cannot_enter(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    readable = _write(tmp_path / "readable.txt", mode=0o644)
    private = _write(tmp_path / "private.txt", mode=0o640)
    link = tmp_path / "link"
    link.symlink_to(private)

    def hidden(path: Path) -> bool:
        return stacks._hidden_from_other_users(path.lstat())

    assert hidden(private) is True
    assert hidden(readable) is False
    assert hidden(link) is False  # a link is judged by its target, never its own mode
    for mode, expected in ((0o755, False), (0o751, True), (0o754, True), (0o700, True)):
        os.chmod(directory, mode)
        assert hidden(directory) is expected, oct(mode)


def test_world_readability_ignores_the_root_and_fails_closed(tmp_path: Path) -> None:
    build = tmp_path / "build"
    handler = _write(build / "pkg" / "handler.py")
    # tempfile.mkdtemp's owner-only mode; no asset carries it.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(build, 0o700)
    assert stacks._asset_tree_is_world_readable(build) is True

    os.chmod(handler, 0o600)
    assert stacks._asset_tree_is_world_readable(build) is False

    os.chmod(handler, 0o644)
    # An owner-only directory hides the readable file inside it.
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(build / "pkg", 0o700)
    assert stacks._asset_tree_is_world_readable(build) is False

    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(build / "pkg", 0o755)
    with patch.object(stacks.Path, "lstat", side_effect=PermissionError("denied")):
        assert stacks._asset_tree_is_world_readable(build) is False


def _prepare(source: Path, build: Path) -> bool:
    def builder(staging: Path) -> None:
        # What pip -t / npm ci / copytree leave behind under the process umask.
        (staging / "vendor").mkdir()
        (staging / "vendor" / "dep.py").write_text("dep = 1\n", encoding="utf-8")
        (staging / "handler.py").write_text("handler = 1\n", encoding="utf-8")

    return stacks._prepare_lambda_asset(
        source,
        build,
        source_inputs=None,
        display_name="demo",
        builder=builder,
    )


def test_a_build_made_under_a_restrictive_umask_publishes_git_modes(tmp_path: Path) -> None:
    source = tmp_path / "lambda" / "demo"
    build = tmp_path / "lambda" / "demo-build"
    _write(source / "handler.py")

    previous = os.umask(0o077)
    try:
        assert _prepare(source, build) is True
    finally:
        os.umask(previous)

    assert _mode(build / "handler.py") == 0o644
    assert _mode(build / "vendor") == 0o755
    assert _mode(build / "vendor" / "dep.py") == 0o644
    assert _mode(build / stacks._LAMBDA_BUILD_MANIFEST) == 0o644
    assert _prepare(source, build) is False  # and it is fresh from then on


def test_an_owner_only_build_from_an_older_cli_is_rebuilt_once(tmp_path: Path) -> None:
    source = tmp_path / "lambda" / "demo"
    build = tmp_path / "lambda" / "demo-build"
    _write(source / "handler.py")
    assert _prepare(source, build) is True

    # An older CLI published the owner-only tree and hashed it as it was, so
    # its own manifest still matches it.
    os.chmod(build / "vendor" / "dep.py", 0o600)
    (build / stacks._LAMBDA_BUILD_MANIFEST).unlink()
    source_digest = stacks._asset_tree_digest(source)
    assert source_digest is not None
    stacks._write_build_manifest(build, source_digest)
    manifest = stacks._read_build_manifest(build)
    assert manifest is not None
    assert manifest["build_digest"] == stacks._asset_tree_digest(build)

    assert stacks._asset_build_is_fresh_unlocked(source, build, source_inputs=None) is False
    assert _prepare(source, build) is True
    assert _mode(build / "vendor" / "dep.py") == 0o644
    assert _prepare(source, build) is False


# ---------------------------------------------------------------------------
# The copies CDK stages in a cloud assembly
# ---------------------------------------------------------------------------


def _owner_only_tree(root: Path) -> Path:
    """A staged directory as CDK leaves it under ``umask 077``, with one owner-only file."""
    _write(root / "gco" / "services" / "api.py")
    _write(root / "gco" / "services" / "private.py", mode=0o600)
    _write(root / "bin" / "tool", "#!/bin/sh\n", mode=0o700)
    for directory in (root / "gco" / "services", root / "gco", root / "bin", root):
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(directory, 0o700)
    return root


def _write_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _assembly(root: Path) -> Path:
    """A cloud assembly with every shape of published copy, plus things nothing publishes."""
    _write_json(
        root / "manifest.json",
        {
            "artifacts": {
                "S.assets": {"type": "cdk:asset-manifest", "properties": {"file": "S.assets.json"}},
                "S": {"type": "aws:cloudformation:stack", "properties": {}},
                "Nested": {
                    "type": "cdk:cloud-assembly",
                    "properties": {"directoryName": "assembly-Nested"},
                },
            }
        },
    )
    _write_json(
        root / "S.assets.json",
        {
            "files": {
                "zipped": {"source": {"path": "asset.zipped", "packaging": "zip"}},
                "single": {"source": {"path": "asset.single.json", "packaging": "file"}},
                "template": {"source": {"path": "S.template.json", "packaging": "file"}},
                "absent": {"source": {"path": "asset.absent", "packaging": "zip"}},
            },
            "dockerImages": {"image": {"source": {"directory": "asset.image"}}},
        },
    )
    _write_json(
        root / "assembly-Nested" / "manifest.json",
        {
            "artifacts": {
                "N.assets": {"type": "cdk:asset-manifest", "properties": {"file": "N.assets.json"}}
            }
        },
    )
    _write_json(
        root / "assembly-Nested" / "N.assets.json",
        {"files": {"nested": {"source": {"path": "../asset.nested", "packaging": "zip"}}}},
    )
    for name in ("asset.zipped", "asset.image", "asset.nested", "asset.stale"):
        _owner_only_tree(root / name)
    _write(root / "asset.single.json", mode=0o600)
    _write(root / "S.template.json", mode=0o600)
    _write(root / ".cache" / "zipped.zip", "packaged from the owner-only copy")
    _write(root / ".cache" / "other.zip", "packaged from a copy nothing corrects")
    return root


def test_every_published_copy_gets_git_modes(tmp_path: Path) -> None:
    root = _assembly(tmp_path / "cdk.out")
    outside = _write(tmp_path / "outside.txt", mode=0o600)
    (root / "asset.zipped" / "link").symlink_to(outside)

    corrected = stacks.normalize_staged_asset_modes(str(root))

    assert corrected == ["asset.image", "asset.nested", "asset.single.json", "asset.zipped"]
    for name in ("asset.zipped", "asset.image", "asset.nested"):
        copy = root / name
        modes = {
            path.relative_to(copy).as_posix(): _mode(path)
            for path in (copy, *copy.rglob("*"))
            if not path.is_symlink()
        }
        assert modes == {
            ".": 0o755,
            "bin": 0o755,
            "bin/tool": 0o755,
            "gco": 0o755,
            "gco/services": 0o755,
            "gco/services/api.py": 0o644,
            "gco/services/private.py": 0o644,
        }, name
    assert _mode(root / "asset.single.json") == 0o644
    # Nothing publishes these, so they keep the modes they were made with.
    assert _mode(root / "asset.stale") == 0o700
    assert _mode(root / "S.template.json") == 0o600
    assert _mode(outside) == 0o600  # the link was not followed
    # The zip cdk-assets cached from the corrected copy would publish the old modes.
    assert sorted(path.name for path in (root / ".cache").iterdir()) == ["other.zip"]


def test_copies_already_at_git_modes_keep_their_cached_zips(tmp_path: Path) -> None:
    root = _assembly(tmp_path / "cdk.out")
    stacks.normalize_staged_asset_modes(root)
    _write(root / ".cache" / "zipped.zip", "packaged from the corrected copy")

    assert stacks.normalize_staged_asset_modes(root) == []
    assert (root / ".cache" / "zipped.zip").exists()


@pytest.mark.parametrize(
    "corruption",
    [
        "no manifest.json",
        "unreadable manifest.json",
        "unreadable asset manifest",
        "sources without paths",
    ],
)
def test_assemblies_with_nothing_to_publish(tmp_path: Path, corruption: str) -> None:
    root = _assembly(tmp_path / "cdk.out")
    if corruption == "no manifest.json":
        (root / "manifest.json").unlink()
    elif corruption == "unreadable manifest.json":
        (root / "manifest.json").write_text("{", encoding="utf-8")
    elif corruption == "unreadable asset manifest":
        (root / "S.assets.json").write_text("not json", encoding="utf-8")
        (root / "assembly-Nested" / "N.assets.json").unlink()
    else:
        _write_json(
            root / "S.assets.json",
            {"files": {"x": {"source": {}}}, "dockerImages": {"y": {}}},
        )
        _write_json(root / "assembly-Nested" / "N.assets.json", {})

    assert stacks.normalize_staged_asset_modes(root) == []
    assert _mode(root / "asset.zipped") == 0o700


# What ``app.py`` does, in a child process whose umask CDK's own (jsii) process
# inherits: synthesize one Lambda whose source has a subdirectory, then hand
# the cloud assembly to ``normalize_staged_asset_modes`` unless told not to.
_SYNTH_SCRIPT = """\
import sys
import aws_cdk as cdk
from aws_cdk import aws_lambda as lambda_
import cli.stacks as stacks

source, outdir, normalize = sys.argv[1:]
app = cdk.App(outdir=outdir)
lambda_.Function(
    cdk.Stack(app, "S"),
    "F",
    runtime=lambda_.Runtime.PYTHON_3_14,
    handler="handler.handler",
    code=lambda_.Code.from_asset(source),
)
assembly = app.synth()
if normalize == "normalize":
    print(",".join(stacks.normalize_staged_asset_modes(assembly.directory)))
"""


def _synth_in_child(source: Path, outdir: Path, *, umask: int, normalize: bool) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _SYNTH_SCRIPT,
            str(source),
            str(outdir),
            "normalize" if normalize else "keep",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        umask=umask,  # the umask a shell would start CDK with
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip()


def _staged_copy(outdir: Path) -> Path:
    (copy,) = [path for path in outdir.glob("asset.*") if path.is_dir()]
    return copy


def test_cdk_stages_owner_only_copies_under_umask_077_and_reuses_them(tmp_path: Path) -> None:
    """Real CDK synths, in child processes started the way a ``umask 077`` shell starts them.

    CDK creates a staged copy's directories under its process umask and keeps
    the checkout's file modes, and a later synth of the same content reuses the
    copy as it is, whatever the umask is by then. ``normalize_staged_asset_modes``
    corrects the copy each time, which is what ``app.py`` runs after synthesis.
    """
    source = tmp_path / "source"
    _write(source / "handler.py", "def handler(event, context):\n    return event\n")
    _write(source / "pkg" / "module.py")
    for directory in (source, source / "pkg"):
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(directory, 0o755)
    outdir = tmp_path / "cdk.out"

    _synth_in_child(source, outdir, umask=0o077, normalize=False)
    copy = _staged_copy(outdir)
    assert (_mode(copy), _mode(copy / "pkg"), _mode(copy / "pkg" / "module.py")) == (
        0o700,
        0o700,
        0o644,
    )

    assert _synth_in_child(source, outdir, umask=0o077, normalize=True) == copy.name
    assert (_mode(copy), _mode(copy / "pkg")) == (0o755, 0o755)

    # A copy left owner-only by an earlier synth is reused as it is under umask 022 ...
    for directory in (copy / "pkg", copy):
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(directory, 0o700)
    _synth_in_child(source, outdir, umask=0o022, normalize=False)
    assert _staged_copy(outdir) == copy
    assert (_mode(copy), _mode(copy / "pkg")) == (0o700, 0o700)
    # ... and corrected by the next synth that normalizes.
    assert _synth_in_child(source, outdir, umask=0o022, normalize=True) == copy.name
    assert (_mode(copy), _mode(copy / "pkg")) == (0o755, 0o755)


# ---------------------------------------------------------------------------
# What the stacks package from a lambda/ source directory
# ---------------------------------------------------------------------------


def _calls(name: str) -> list[tuple[str, ast.Call]]:
    """Every call to ``name`` (a bare or attribute call) in ``gco/stacks``."""
    calls: list[tuple[str, ast.Call]] = []
    for module in sorted((REPO_ROOT / "gco" / "stacks").glob("*.py")):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                called = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if called == name:
                    calls.append((module.name, node))
    return calls


def _first_constant(module: str, call: ast.Call) -> str:
    assert call.args, f"{module}: asset path missing"
    value = call.args[0]
    assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
        f"{module}: dynamic asset path"
    )
    return value.value


def test_source_directories_are_zipped_without_local_caches() -> None:
    """A ``lambda/<name>`` directory goes through ``lambda_source_code``; a CLI build does not.

    ``Code.from_asset`` would package the bytecode a test run leaves in a source
    directory. Only the CLI's ``*-build`` directories may use it, since their
    bytecode is part of the build.
    """
    sources = [
        (module, _first_constant(module, call)) for module, call in _calls("lambda_source_code")
    ]
    direct = [
        (module, _first_constant(module, call))
        for module, call in _calls("from_asset")
        if module != "lambda_assets.py"
    ]
    assert len(sources) >= 10
    built = {f"lambda/{spec.build_directory}" for spec in stacks._CDK_ASSET_SPECS}
    assert [(module, path) for module, path in sources if path in built] == []
    assert all(path.startswith("lambda/") for _module, path in sources), sources
    assert [(module, path) for module, path in direct if path not in built] == []


def test_the_preflight_skips_only_caches_that_never_ship() -> None:
    """The checkout check may skip a cache only because no source asset packages it."""
    from gco.stacks.constants import LAMBDA_SOURCE_ASSET_EXCLUDE

    assert stacks._LAMBDA_SOURCE_COPY_IGNORE_PATTERNS == LAMBDA_SOURCE_ASSET_EXCLUDE
    skipped = stacks._LAMBDA_SOURCE_IGNORED_DIRECTORIES | stacks._LAMBDA_SOURCE_IGNORED_FILES
    assert skipped | {"*.pyc", "*.pyo"} == set(LAMBDA_SOURCE_ASSET_EXCLUDE)
    for name in stacks._LAMBDA_SOURCE_IGNORED_DIRECTORIES:
        assert stacks._skip_asset_source_directory("lambda/fn", name), name
    for name in (*stacks._LAMBDA_SOURCE_IGNORED_FILES, "handler.cpython-314.pyc", "x.pyo"):
        assert stacks._skip_asset_source_file("lambda/fn", name), name


def test_a_working_checkout_zips_what_a_clean_clone_zips(tmp_path: Path) -> None:
    """Same staged files and same asset hash with or without a test run's caches."""
    import aws_cdk as cdk
    from aws_cdk import aws_lambda as lambda_

    from gco.stacks.lambda_assets import lambda_source_code

    def synth(code: Callable[[str], lambda_.Code], label: str) -> tuple[str, set[str]]:
        outdir = tmp_path / f"cdk.out.{label}"
        app = cdk.App(outdir=str(outdir))
        stack = cdk.Stack(app, "S")
        lambda_.Function(
            stack,
            "F",
            runtime=lambda_.Runtime.PYTHON_3_14,
            handler="handler.handler",
            code=code(str(source)),
        )
        app.synth()
        manifest = json.loads((outdir / "S.assets.json").read_text(encoding="utf-8"))
        (asset_id,) = [
            key
            for key, asset in manifest["files"].items()
            if asset["source"].get("packaging") == "zip"
        ]
        copy = outdir / f"asset.{asset_id}"
        return asset_id, {path.relative_to(copy).as_posix() for path in copy.rglob("*")}

    source = tmp_path / "source"
    _write(source / "handler.py", "def handler(event, context):\n    return event\n")
    _write(source / "pkg" / "module.py")
    clean = synth(lambda_source_code, "clean")

    for cache in (source / "__pycache__", source / "pkg" / "__pycache__"):
        _write(cache / "module.cpython-314.pyc", "bytecode")
    _write(source / "stale.pyo", "bytecode")
    _write(source / ".mypy_cache" / "3.14" / "handler.data.json", "{}")
    _write(source / ".pytest_cache" / "v" / "cache" / "nodeids", "[]")
    _write(source / ".DS_Store", "finder")

    assert synth(lambda_source_code, "working") == clean
    assert clean[1] == {"handler.py", "pkg", "pkg/module.py"}
    # Without the exclusions the caches would ship and change the hash.
    unfiltered_id, unfiltered_files = synth(lambda_.Code.from_asset, "unfiltered")
    assert unfiltered_id != clean[0]
    assert "__pycache__/module.cpython-314.pyc" in unfiltered_files


# ---------------------------------------------------------------------------
# The checkout readability preflight
# ---------------------------------------------------------------------------


def _checkout(root: Path) -> Path:
    """A checkout where every deployable path is readable, plus owner-only noise."""
    _write(root / "lambda" / "fn" / "handler.py")
    _write(root / "lambda" / "tls-shared" / "backend_tls.py")
    _write(root / "gco" / "services" / "api.py")
    _write(root / "dockerfiles" / "Dockerfile.api")
    _write(root / "pyproject.toml")
    # Never packaged as they are: owner-only is fine.
    _write(root / "lambda" / "README.md", mode=0o600)
    _write(root / "lambda" / ".fn-build.lock", mode=0o600)
    _write(root / "lambda" / ".fn-build.staging-abc" / "handler.py", mode=0o600)
    _write(root / "lambda" / "fn-build" / "vendor" / "dep.py", mode=0o600)
    _write(root / "lambda" / "kubectl-applier-simple" / "handler.py", mode=0o600)
    _write(root / "lambda" / "inference-streaming-proxy" / "index.mjs", mode=0o600)
    _write(root / "lambda" / "fn" / "__pycache__" / "handler.cpython-314.pyc", mode=0o600)
    _write(root / "lambda" / "fn" / "stale.pyc", mode=0o600)
    _write(root / "lambda" / "fn" / ".DS_Store", mode=0o600)
    _write(root / "lambda" / "fn" / "node_modules" / "pkg" / "index.js", mode=0o600)
    _write(root / "gco" / "stacks" / "regional_stack.py", mode=0o600)
    _write(root / "dockerfiles" / "README.md", mode=0o600)
    _write(root / "cli" / "main.py", mode=0o600)  # excluded from every image
    _write(root / "docs" / "notes.md", mode=0o600)  # in no asset at all
    return root


def test_a_readable_checkout_passes(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    assert stacks.unreadable_asset_sources(root) == []
    stacks.check_asset_sources_readable(root)


def test_owner_only_deployable_paths_are_reported(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    os.chmod(root / "lambda" / "fn" / "handler.py", 0o600)
    (root / "lambda" / "fn" / "private").mkdir(mode=0o700)
    _write(root / "lambda" / "fn" / "private" / "inner.py")
    os.chmod(root / "gco" / "services" / "api.py", 0o640)
    os.chmod(root / "dockerfiles" / "Dockerfile.api", 0o600)
    os.chmod(root / "pyproject.toml", 0o600)

    assert stacks.unreadable_asset_sources(str(root)) == [
        "dockerfiles/Dockerfile.api",
        "gco/services/api.py",
        "lambda/fn/handler.py",
        "lambda/fn/private",
        "pyproject.toml",
    ]
    with pytest.raises(stacks.AssetPermissionError) as raised:
        stacks.check_asset_sources_readable(root)
    message = str(raised.value)
    assert isinstance(raised.value, RuntimeError)
    assert message.startswith("5 deployable path(s) in the checkout are not readable")
    assert "lambda/fn/handler.py" in message
    assert "(and" not in message
    assert message.endswith(
        "chmod -R a+rX lambda gco dockerfiles pyproject.toml requirements-lock.txt"
    )


def test_a_long_offender_list_is_truncated(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    for index in range(stacks._ASSET_SOURCE_REPORT_LIMIT + 3):
        _write(root / "gco" / "generated" / f"module_{index:02d}.py", mode=0o600)

    with pytest.raises(stacks.AssetPermissionError, match=r"^13 deployable .*\(and 3 more\)"):
        stacks.check_asset_sources_readable(root)


def test_entries_that_vanish_or_cannot_be_inspected_mid_walk(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    _write(root / "lambda" / "fn" / "scratch.py")
    _write(root / "lambda" / "fn" / "locked.py")
    real_lstat = Path.lstat

    def flaky_lstat(self: Path) -> os.stat_result:
        if self.name == "scratch.py":
            raise FileNotFoundError(self)  # an editor removed its scratch file
        if self.name == "locked.py":
            raise PermissionError(self)
        return real_lstat(self)

    with patch.object(Path, "lstat", flaky_lstat):
        assert stacks.unreadable_asset_sources(root) == ["lambda/fn/locked.py"]


def test_symlinked_and_missing_roots(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "module.py", mode=0o600)
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "gco").symlink_to(elsewhere, target_is_directory=True)
    # A linked root is not walked (CDK does not follow it either), and roots
    # that do not exist are skipped.
    assert stacks.unreadable_asset_sources(root) == []


def test_every_asset_directory_is_covered_by_the_preflight_or_the_cli_build() -> None:
    """Keep the preflight's scope in step with the assets the stacks declare.

    Plain Lambda directories are zipped (or built) as they sit in the checkout,
    so the preflight must walk them; ``*-build`` directories are CLI builds,
    whose modes ``_normalize_asset_modes`` sets; the service images build from
    the repository root, narrowed by ``.dockerignore`` and the regional stack's
    common excludes.
    """
    directories: set[str] = set()
    for module in sorted((REPO_ROOT / "gco" / "stacks").glob("*.py")):
        if module.name == "lambda_assets.py":
            continue  # lambda_source_code's own from_asset; its callers are counted below
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name not in {
                "from_asset",
                "lambda_source_code",
                "from_image_asset",
                "DockerImageAsset",
            }:
                continue
            candidates = [keyword.value for keyword in node.keywords if keyword.arg == "directory"]
            if name in {"from_asset", "lambda_source_code"} and node.args:
                candidates.append(node.args[0])
            for value in candidates:
                assert isinstance(value, ast.Constant), f"{module.name}: dynamic asset path"
                directories.add(str(value.value))

    assert "." in directories
    assert "lambda/helm-installer" in directories  # a CLI-built source can be an asset too
    built = {spec.build_directory: spec.source_directory for spec in stacks._CDK_ASSET_SPECS}
    for directory in sorted(directories - {"."}):
        parent, _, name = directory.partition("/")
        assert parent == "lambda", directory
        if name in built:
            continue
        assert not stacks._skip_asset_source_directory("lambda", name), directory
        assert name not in stacks._CLI_BUILT_LAMBDA_SOURCES, directory

    # Skipped sources are CLI-built and never deployed as they are.
    sources = set(built.values())
    assert sources >= stacks._CLI_BUILT_LAMBDA_SOURCES
    assert not {f"lambda/{name}" for name in stacks._CLI_BUILT_LAMBDA_SOURCES} & directories

    # The service image build context: what .dockerignore lets through, minus
    # what the regional stack excludes from every image.
    from gco.stacks.regional_stack import _SERVICE_IMAGE_COMMON_EXCLUDES

    allowed = {
        line[1:].strip().rstrip("/")
        for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.startswith("!")
    }
    fully_excluded = {
        pattern.removesuffix("/**")
        for pattern in _SERVICE_IMAGE_COMMON_EXCLUDES
        if pattern.endswith("/**") and "/" not in pattern.removesuffix("/**")
    }
    assert allowed - fully_excluded <= set(stacks._ASSET_SOURCE_ROOTS)
    partially_excluded = {
        pattern.removesuffix("/**")
        for pattern in _SERVICE_IMAGE_COMMON_EXCLUDES
        if pattern.removesuffix("/**") not in fully_excluded
    }
    assert partially_excluded == set(stacks._ASSET_SOURCE_EXCLUDED)


def test_the_real_checkout_is_deployable() -> None:
    """CI checks out under the default umask; a regression here names the path."""
    assert stacks.unreadable_asset_sources(REPO_ROOT) == []


# ---------------------------------------------------------------------------
# Where the preflight runs
# ---------------------------------------------------------------------------


def _manager(project_root: Path) -> stacks.StackManager:
    config = MagicMock()
    config.project_name = "gco"
    config.api_gateway_region = "us-east-2"
    return stacks.StackManager(config, project_root=project_root)


def _owner_only_checkout(root: Path) -> Path:
    _checkout(root)
    os.chmod(root / "lambda" / "fn" / "handler.py", 0o600)
    return root


def test_deploy_refuses_an_owner_only_checkout_before_packaging(tmp_path: Path) -> None:
    manager = _manager(_owner_only_checkout(tmp_path))
    with (
        patch.object(manager, "_sync_lambda_sources") as sync,
        patch.object(manager, "_ensure_lambda_build") as ensure,
        patch.object(manager, "_run_cdk") as run_cdk,
        pytest.raises(stacks.AssetPermissionError, match=r"lambda/fn/handler\.py"),
    ):
        manager.deploy("gco-us-east-1", require_approval=False)
    sync.assert_not_called()
    ensure.assert_not_called()
    run_cdk.assert_not_called()


def test_deploy_orchestrated_refuses_before_the_stack_listing(tmp_path: Path) -> None:
    manager = _manager(_owner_only_checkout(tmp_path))
    with (
        patch.object(manager, "list_stacks") as list_stacks,
        pytest.raises(stacks.AssetPermissionError),
    ):
        manager.deploy_orchestrated(require_approval=False)
    list_stacks.assert_not_called()


def test_the_checkout_is_walked_once_per_manager(tmp_path: Path) -> None:
    manager = _manager(_checkout(tmp_path))
    with patch.object(stacks, "check_asset_sources_readable") as check:
        manager.ensure_asset_sources_readable()
        manager.ensure_asset_sources_readable()
    check.assert_called_once_with(tmp_path)


def test_a_keep_control_plane_teardown_checks_before_deleting_anything(tmp_path: Path) -> None:
    """Its monitoring detach is a deploy: an owner-only checkout stops it first."""
    manager = _manager(_owner_only_checkout(tmp_path))
    with (
        patch.object(manager, "list_stacks", return_value=["gco-monitoring", "gco-us-east-1"]),
        patch.object(manager, "_stack_exists_in_cloudformation", return_value=True),
        patch.object(manager, "_run_cdk") as run_cdk,
        patch.object(manager, "destroy") as destroy,
        pytest.raises(stacks.AssetPermissionError),
    ):
        manager.destroy_orchestrated(force=True, keep_control_plane=True)
    run_cdk.assert_not_called()
    destroy.assert_not_called()


def test_destroy_never_checks_the_checkout(tmp_path: Path) -> None:
    """A teardown must not depend on the modes of the checkout it runs from."""
    manager = _manager(_owner_only_checkout(tmp_path))
    with (
        patch.object(manager, "_run_cdk", return_value=MagicMock(returncode=0)),
        patch.object(manager, "_stack_exists_in_cloudformation", return_value=False),
        patch.object(stacks, "check_asset_sources_readable") as check,
    ):
        assert manager.destroy("gco-us-east-1", force=True) is True
    check.assert_not_called()
