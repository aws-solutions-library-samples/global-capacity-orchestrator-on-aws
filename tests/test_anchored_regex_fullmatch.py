"""Guardrail: a ``^...$`` pattern must check a whole value with ``fullmatch``.

In Python ``$`` matches at the end of the string and also just before a
trailing newline, so ``re.compile(r"^[a-z]+$").match("abc\\n")`` succeeds. A
validator written as ``^...$`` with ``match`` therefore accepts a name, id or
path that carries one trailing newline, and the value goes on into an argv, a
URL or an API call. ``fullmatch`` refuses it, with or without the anchors.

The guard parses the Python under ``gco/``, ``cli/``, ``gco_mcp/``,
``scripts/``, ``.github/scripts/`` and ``dockerfiles/`` with :mod:`ast` and
fails on each ``.match(`` call whose pattern starts with ``^`` and ends with a
``$`` that no backslash escapes. It finds the pattern when it is:

* compiled into a name in the same module (at module level, in a class body
  or in a function), from a string, an f-string, or a module-level string
  constant;
* imported from another scanned module, or read as an attribute of one;
* passed to a module-level function of the same module that calls ``.match``
  on the parameter receiving it (``_validate(value, pattern, what)``);
* given as a literal to ``re.match`` or to ``re.compile(...).match``.

A parser of text in which a trailing newline is legitimate or cannot occur (a
line from ``str.splitlines()``, say) may keep ``match``: add the call site to
:data:`ALLOWED_MATCH_SITES` with the reason. An entry that no longer names a
call site fails the guard, so the list cannot go stale.

The same module tests the AWS Region name rule in ``gco/aws_names.py``, which
the CLI's Region validators share for the same reason.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gco.aws_names import AWS_REGION_NAME_PATTERN, AWS_REGION_NAME_RE, is_aws_region_name

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: ``re.match`` spelled in two parts, so the Python 3.15 deprecation guard,
#: which greps the tests for bare calls of it, does not read this module's
#: messages and fixtures as calls.
_RE_MATCH = "re." + "match"

#: The trees the guard parses.
SCANNED_ROOTS = ("gco", "cli", "gco_mcp", "scripts", ".github/scripts", "dockerfiles")

#: Directories skipped wherever they occur. Any ``*-build`` directory is
#: skipped too: those hold vendored Lambda dependencies, not our code.
EXCLUDED_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".hypothesis",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "build",
        "cdk.out",
        "dist",
        "htmlcov",
        "node_modules",
    }
)

#: Call sites that keep ``match``, keyed by (path, enclosing function or
#: class, pattern), each with the reason a trailing newline does no harm
#: there. The pattern is the compiled name the call is made on; for a helper
#: it is ``<parameter>=<pattern passed in>``.
ALLOWED_MATCH_SITES: dict[tuple[str, str, str], str] = {
    (
        "gco/services/api_shared.py",
        "_collect_job_events",
        "_JOB_POD_EVENT_RE",
    ): "parses a Kubernetes event message, and the (\\S+) capture cannot take a newline",
    (
        "gco/services/api_shared.py",
        "_collect_job_events",
        "_SCHEDULED_EVENT_RE",
    ): "parses a Kubernetes event message, and the (\\S+) capture cannot take a newline",
    (
        "cli/slurm.py",
        "sbatch_directives",
        "_SBATCH_RE",
    ): "parses one stripped line from str.splitlines(), which holds no newline",
    (
        ".github/scripts/check_bash_coverage.py",
        "untraceable_lines",
        "_TERMINATOR_WITH_REDIRECTIONS",
    ): "parses one line from str.splitlines(), which holds no newline",
    (
        ".github/scripts/check_bash_coverage.py",
        "untraceable_lines",
        "_EMPTY_CASE_ARM",
    ): "parses one line from str.splitlines(), which holds no newline",
    (
        ".github/scripts/disambiguate_lcov_functions.py",
        "parse_records",
        "FN_RE",
    ): "parses one tracefile line; main() splits the file with str.splitlines()",
    (
        ".github/scripts/disambiguate_lcov_functions.py",
        "parse_records",
        "FNDA_RE",
    ): "parses one tracefile line; main() splits the file with str.splitlines()",
    (
        ".github/scripts/verify_action_pins.py",
        "collect_pins",
        "USES_LINE_RE",
    ): "parses one workflow line from str.splitlines(), which holds no newline",
    (
        ".github/scripts/verify_action_pins.py",
        "collect_pins",
        "TRAILER_RE",
    ): "parses the trailer group of one line from str.splitlines(), which holds no newline",
    (
        "dockerfiles/build_scratch_rootfs.py",
        "resolve_closure",
        "_LDD_RESOLVED",
    ): "parses one line of ldd output from str.splitlines(), which holds no newline",
    (
        "dockerfiles/build_scratch_rootfs.py",
        "resolve_closure",
        "_LDD_DIRECT",
    ): "parses one line of ldd output from str.splitlines(), which holds no newline",
}


@dataclass(frozen=True)
class MatchSite:
    """One ``.match(`` call on a ``^...$`` pattern."""

    path: str
    line: int
    scope: str
    pattern: str
    text: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.path, self.scope, self.pattern)

    def describe(self) -> str:
        if self.pattern == _RE_MATCH:
            call = f"{_RE_MATCH}()"
        elif self.pattern == "re.compile":
            call = "re.compile(...).match()"
        else:
            receiver, _, passed = self.pattern.partition("=")
            call = f"{receiver}.match()" + (f" given {passed}" if passed else "")
        return f"{self.path}:{self.line}: {self.scope}: {call} on {self.text!r}"


def _is_anchored(text: str) -> bool:
    """Whether ``text`` starts with ``^`` and ends with a ``$`` no backslash escapes."""
    if not (text.startswith("^") and text.endswith("$")):
        return False
    body = text[:-1]
    return (len(body) - len(body.rstrip("\\"))) % 2 == 0


def _pattern_text(node: ast.expr | None, strings: Mapping[str, str]) -> str | None:
    """The pattern a literal spells; an f-string field or unknown operand reads ``{}``."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    if isinstance(node, ast.Name):
        return strings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _pattern_text(node.left, strings)
        right = _pattern_text(node.right, strings)
        if left is None and right is None:
            return None
        return f"{'{}' if left is None else left}{'{}' if right is None else right}"
    return None


def _re_call(node: ast.expr, name: str) -> ast.Call | None:
    """``node`` when it is a call of ``re.<name>``, else None."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
    ):
        return node
    return None


def _pattern_argument(call: ast.Call) -> ast.expr | None:
    """The ``pattern`` argument of an ``re`` function call."""
    if call.args:
        return call.args[0]
    return next((kw.value for kw in call.keywords if kw.arg == "pattern"), None)


def _assigned_names(node: ast.stmt) -> tuple[list[str], ast.expr | None]:
    """The plain names a simple assignment binds, and the value it binds them to."""
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id], node.value
    return [], None


def _anchored_compiles(
    statements: Iterable[ast.stmt], strings: Mapping[str, str]
) -> dict[str, str]:
    """``name -> pattern`` for each ``name = re.compile(<^...$ pattern>)`` in ``statements``."""
    found: dict[str, str] = {}
    for statement in statements:
        names, value = _assigned_names(statement)
        call = _re_call(value, "compile") if value is not None else None
        if call is None:
            continue
        text = _pattern_text(_pattern_argument(call), strings)
        if text is not None and _is_anchored(text):
            found.update(dict.fromkeys(names, text))
    return found


def _module_key(relative: Path) -> str:
    """``gco/pcs_config.py`` -> ``gco/pcs_config``; a package's ``__init__`` names the package."""
    key = relative.with_suffix("").as_posix()
    return key.removesuffix("/__init__")


@dataclass
class _Module:
    path: str
    key: str
    tree: ast.Module
    strings: dict[str, str] = field(default_factory=dict)
    patterns: dict[str, str] = field(default_factory=dict)


def _load_module(root: Path, path: Path) -> _Module:
    relative = path.relative_to(root)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(relative))
    module = _Module(path=relative.as_posix(), key=_module_key(relative), tree=tree)
    for statement in tree.body:
        names, value = _assigned_names(statement)
        text = _pattern_text(value, module.strings)
        if text is not None:
            module.strings.update(dict.fromkeys(names, text))
    module.patterns = _anchored_compiles(tree.body, module.strings)
    return module


class _Index:
    """Every scanned module, so an imported pattern resolves to its definition."""

    def __init__(self, modules: Iterable[_Module], roots: Iterable[str]) -> None:
        self.modules = {module.key: module for module in modules}
        self.roots = tuple(roots)

    def resolve(self, importer: _Module, module: str | None, level: int) -> str | None:
        """The key of the module an ``import`` statement in ``importer`` names, if scanned."""
        dotted = (module or "").replace(".", "/")
        if level:
            package = importer.key.split("/")
            # A package's __init__ is its own package; any other module sits in one.
            is_package = importer.path.endswith("/__init__.py")
            base = package if is_package else package[:-1]
            base = base[: len(base) - (level - 1)] if level > 1 else base
            candidates = ["/".join([*base, dotted]) if dotted else "/".join(base)]
        else:
            directory = importer.key.rpartition("/")[0]
            candidates = [dotted, *(f"{root}/{dotted}" for root in self.roots)]
            candidates.append(f"{directory}/{dotted}")
        return next((key for key in candidates if key in self.modules), None)


class _SiteFinder(ast.NodeVisitor):
    """Collect the ``.match(`` calls one module makes on ``^...$`` patterns."""

    def __init__(self, module: _Module, index: _Index) -> None:
        self.module = module
        self.index = index
        self.sites: list[MatchSite] = []
        self.scope: list[str] = []
        self.function_patterns: list[dict[str, str]] = []
        self.function_params: list[list[str]] = []
        self.class_patterns: dict[str, str] = {}
        self.imported: dict[str, str] = {}
        self.module_aliases: dict[str, str] = {}
        # Function qualname -> (parameter, its position, line) for each
        # ``<parameter>.match(`` call the function makes.
        self.helpers: dict[str, list[tuple[str, int, int]]] = {}
        self._collect_imports_and_classes()

    def _collect_imports_and_classes(self) -> None:
        for node in ast.walk(self.module.tree):
            if isinstance(node, ast.ClassDef):
                self.class_patterns.update(_anchored_compiles(node.body, self.module.strings))
            elif isinstance(node, ast.ImportFrom):
                source = self.index.resolve(self.module, node.module, node.level)
                target = self.index.modules.get(source) if source else None
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if target is not None and alias.name in target.patterns:
                        self.imported[bound] = target.patterns[alias.name]
                        continue
                    dotted = f"{node.module}.{alias.name}" if node.module else alias.name
                    submodule = self.index.resolve(self.module, dotted, node.level)
                    if submodule is not None:
                        self.module_aliases[bound] = submodule
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    resolved = self.index.resolve(self.module, alias.name, 0)
                    if resolved is not None and alias.asname:
                        self.module_aliases[alias.asname] = resolved

    # -- scopes -----------------------------------------------------------

    def _qualname(self) -> str:
        return ".".join(self.scope) or "<module>"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        local = _anchored_compiles(
            (n for n in ast.walk(node) if isinstance(n, ast.Assign | ast.AnnAssign)),
            self.module.strings,
        )
        self.function_patterns.append(local)
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        self.function_params.append([argument.arg for argument in arguments])
        self.generic_visit(node)
        self.function_params.pop()
        self.function_patterns.pop()
        self.scope.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    # -- calls ------------------------------------------------------------

    def _named_pattern(self, name: str) -> str | None:
        for local in reversed(self.function_patterns):
            if name in local:
                return local[name]
        return self.module.patterns.get(name) or self.imported.get(name)

    def _record(self, node: ast.Call, pattern: str, text: str) -> None:
        site = MatchSite(self.module.path, node.lineno, self._qualname(), pattern, text)
        self.sites.append(site)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "match":
            self._match_call(node, func.value)
        self.generic_visit(node)

    def _match_call(self, node: ast.Call, receiver: ast.expr) -> None:
        strings = self.module.strings
        if isinstance(receiver, ast.Name):
            if receiver.id == "re":
                text = _pattern_text(_pattern_argument(node), strings)
                if text is not None and _is_anchored(text):
                    self._record(node, _RE_MATCH, text)
                return
            text = self._named_pattern(receiver.id)
            if text is not None:
                self._record(node, receiver.id, text)
                return
            if self.function_params and receiver.id in self.function_params[-1]:
                position = self.function_params[-1].index(receiver.id)
                self.helpers.setdefault(self._qualname(), []).append(
                    (receiver.id, position, node.lineno)
                )
            return
        if isinstance(receiver, ast.Attribute):
            owner = receiver.value
            if isinstance(owner, ast.Name) and owner.id in self.module_aliases:
                target = self.index.modules[self.module_aliases[owner.id]]
                text = target.patterns.get(receiver.attr)
            else:
                text = self.class_patterns.get(receiver.attr)
            if text is not None:
                self._record(node, ast.unparse(receiver), text)
            return
        compiled = _re_call(receiver, "compile")
        if compiled is not None:
            text = _pattern_text(_pattern_argument(compiled), strings)
            if text is not None and _is_anchored(text):
                self._record(node, "re.compile", text)

    def helper_sites(self) -> Iterator[MatchSite]:
        """The ``.match`` calls module-level helpers make on a ``^...$`` pattern passed in."""
        for node in ast.walk(self.module.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            # A module-level function's qualname is its bare name.
            helper = node.func.id
            for parameter, position, line in self.helpers.get(helper, ()):
                argument = next(
                    (kw.value for kw in node.keywords if kw.arg == parameter),
                    node.args[position] if position < len(node.args) else None,
                )
                if isinstance(argument, ast.Name):
                    text = self.module.patterns.get(argument.id) or self.imported.get(argument.id)
                    if text is not None:
                        yield MatchSite(
                            self.module.path, line, helper, f"{parameter}={argument.id}", text
                        )


def _python_files(root: Path, roots: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for top in roots:
        for path in sorted((root / top).rglob("*.py")):
            directories = path.relative_to(root).parts[:-1]
            if any(p in EXCLUDED_DIR_NAMES or p.endswith("-build") for p in directories):
                continue
            files.append(path)
    return files


def find_match_sites(root: Path, roots: Iterable[str] = SCANNED_ROOTS) -> list[MatchSite]:
    """Every ``.match(`` call on a ``^...$`` pattern under ``roots``, deduplicated and sorted."""
    roots = tuple(roots)
    modules = [_load_module(root, path) for path in _python_files(root, roots)]
    index = _Index(modules, roots)
    sites: set[MatchSite] = set()
    for module in modules:
        finder = _SiteFinder(module, index)
        finder.visit(module.tree)
        sites.update(finder.sites)
        sites.update(finder.helper_sites())
    return sorted(sites, key=lambda site: (site.path, site.line, site.pattern))


@pytest.fixture(scope="module")
def repository_sites() -> list[MatchSite]:
    return find_match_sites(PROJECT_ROOT)


# ---------------------------------------------------------------------------
# The repository guard
# ---------------------------------------------------------------------------


def test_scanned_roots_exist_and_hold_python() -> None:
    """A renamed root must fail here rather than leave the guard scanning nothing."""
    for top in SCANNED_ROOTS:
        assert (PROJECT_ROOT / top).is_dir(), f"{top}/ no longer exists; update SCANNED_ROOTS"
    files = _python_files(PROJECT_ROOT, SCANNED_ROOTS)
    assert len(files) >= 200, f"only {len(files)} Python files found under {SCANNED_ROOTS}"
    anchored = sum(len(_load_module(PROJECT_ROOT, path).patterns) for path in files)
    assert anchored >= 40, f"only {anchored} module-level ^...$ patterns found; is the walk broken?"


def test_anchored_patterns_check_whole_values_with_fullmatch(
    repository_sites: list[MatchSite],
) -> None:
    """Every ``^...$`` pattern used with ``match`` is a reviewed parser, not a validator."""
    unreviewed = [site for site in repository_sites if site.key not in ALLOWED_MATCH_SITES]
    assert not unreviewed, (
        "These calls check a ^...$ pattern with .match(), which also accepts the value "
        "followed by one newline. Use .fullmatch(); if the call parses text in which a "
        "trailing newline is legitimate or impossible, add its (path, scope, pattern) key "
        "to ALLOWED_MATCH_SITES with the reason:\n  "
        + "\n  ".join(site.describe() for site in unreviewed)
    )


def test_every_allowed_site_is_still_a_match_call(repository_sites: list[MatchSite]) -> None:
    """An entry whose call site is gone or now uses fullmatch must be deleted."""
    present = {site.key for site in repository_sites}
    stale = sorted(key for key in ALLOWED_MATCH_SITES if key not in present)
    assert not stale, f"ALLOWED_MATCH_SITES entries that name no .match() call: {stale}"
    assert all(reason.strip() for reason in ALLOWED_MATCH_SITES.values())


# ---------------------------------------------------------------------------
# The detector itself, on small modules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'import re\nNAME = re.compile(r"^[a-z]+$")\n\ndef ok(v):\n    return NAME.match(v)\n',
            [("ok", "NAME")],
            id="module-constant",
        ),
        pytest.param(
            'import re\nNAME = re.compile(r"^[a-z]+$")\n\ndef ok(v):\n'
            "    return NAME.fullmatch(v)\n",
            [],
            id="fullmatch-is-fine",
        ),
        pytest.param(
            'import re\nNAME = re.compile(r"^[a-z]+")\n\ndef ok(v):\n    return NAME.match(v)\n',
            [],
            id="prefix-pattern-is-fine",
        ),
        pytest.param(
            'import re\nNAME = re.compile(r"^cost: 5\\$")\n\ndef ok(v):\n'
            "    return NAME.match(v)\n",
            [],
            id="escaped-dollar-is-literal",
        ),
        pytest.param(
            'import re\nNAME = re.compile(r"^a\\\\$")\n\ndef ok(v):\n    return NAME.match(v)\n',
            [("ok", "NAME")],
            id="escaped-backslash-then-anchor",
        ),
        pytest.param(
            'import re\nPART = "[a-z]+"\nNAME = re.compile(rf"^{PART}-[0-9]+$")\n\n'
            "def ok(v):\n    return NAME.match(v)\n",
            [("ok", "NAME")],
            id="f-string",
        ),
        pytest.param(
            'import re\nTEXT = r"^[a-z]+$"\nNAME = re.compile(TEXT)\n\n'
            "def ok(v):\n    return NAME.match(v)\n",
            [("ok", "NAME")],
            id="string-constant",
        ),
        pytest.param(
            'import re\nNAME = re.compile("^" + "[a-z]+" + "$")\n\n'
            "def ok(v):\n    return NAME.match(v)\n",
            [("ok", "NAME")],
            id="concatenation",
        ),
        pytest.param(
            f'import re\n\ndef ok(v):\n    return {_RE_MATCH}(r"^[a-z]+$", v)\n',
            [("ok", _RE_MATCH)],
            id="inline-literal",
        ),
        pytest.param(
            'import re\n\ndef ok(v):\n    return re.compile(r"^[a-z]+$").match(v)\n',
            [("ok", "re.compile")],
            id="compiled-inline",
        ),
        pytest.param(
            'import re\n\ndef ok(v):\n    name = re.compile(r"^[a-z]+$")\n'
            "    return name.match(v)\n",
            [("ok", "name")],
            id="function-local",
        ),
        pytest.param(
            'import re\n\nclass Check:\n    NAME = re.compile(r"^[a-z]+$")\n\n'
            "    def ok(self, v):\n        return self.NAME.match(v)\n",
            [("Check.ok", "self.NAME")],
            id="class-attribute",
        ),
        pytest.param(
            'import re\nNAME = re.compile(r"^[a-z]+$")\n\n'
            "def _validate(value, pattern):\n    return pattern.match(value)\n\n"
            "def ok(v):\n    return _validate(v, NAME)\n",
            [("_validate", "pattern=NAME")],
            id="helper-parameter",
        ),
        pytest.param(
            'import re\nNAME = re.compile(r"^[a-z]+$")\n\n'
            "def _validate(value, pattern):\n    return pattern.match(value)\n\n"
            "def ok(v):\n    return _validate(v, pattern=NAME)\n",
            [("_validate", "pattern=NAME")],
            id="helper-keyword",
        ),
        pytest.param(
            'import re\nNAME = re.compile(r"^[a-z]+")\n\n'
            "def _validate(value, pattern):\n    return pattern.match(value)\n\n"
            "def ok(v):\n    return _validate(v, NAME)\n",
            [],
            id="helper-with-prefix-pattern",
        ),
    ],
)
def test_detector_finds_each_shape(
    tmp_path: Path, source: str, expected: list[tuple[str, str]]
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(source, encoding="utf-8")
    sites = find_match_sites(tmp_path, ("pkg",))
    assert [(site.scope, site.pattern) for site in sites] == expected


@pytest.mark.parametrize(
    ("importer", "expected"),
    [
        pytest.param(
            "from pkg.names import NAME\n\ndef ok(v):\n    return NAME.match(v)\n",
            [("ok", "NAME")],
            id="absolute-from-import",
        ),
        pytest.param(
            "from .names import NAME as ALIAS\n\ndef ok(v):\n    return ALIAS.match(v)\n",
            [("ok", "ALIAS")],
            id="relative-import-alias",
        ),
        pytest.param(
            "from pkg import names\n\ndef ok(v):\n    return names.NAME.match(v)\n",
            [("ok", "names.NAME")],
            id="module-attribute",
        ),
        pytest.param(
            "import pkg.names as n\n\ndef ok(v):\n    return n.NAME.match(v)\n",
            [("ok", "n.NAME")],
            id="import-as",
        ),
        pytest.param(
            "from names import NAME\n\ndef ok(v):\n    return NAME.match(v)\n",
            [("ok", "NAME")],
            id="sibling-on-sys-path",
        ),
        pytest.param(
            "from pkg.names import PREFIX\n\ndef ok(v):\n    return PREFIX.match(v)\n",
            [],
            id="imported-prefix-pattern",
        ),
    ],
)
def test_detector_follows_imports(
    tmp_path: Path, importer: str, expected: list[tuple[str, str]]
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "names.py").write_text(
        'import re\nNAME = re.compile(r"^[a-z]+$")\nPREFIX = re.compile(r"^[a-z]+")\n',
        encoding="utf-8",
    )
    (package / "user.py").write_text(importer, encoding="utf-8")
    sites = find_match_sites(tmp_path, ("pkg",))
    assert [(site.scope, site.pattern) for site in sites] == expected
    assert all(site.path == "pkg/user.py" for site in sites)


def test_detector_skips_caches_and_vendored_build_trees(tmp_path: Path) -> None:
    source = 'import re\nNAME = re.compile(r"^[a-z]+$")\nNAME.match("a")\n'
    for directory in ("pkg", "pkg/__pycache__", "pkg/helm-installer-build"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
        (tmp_path / directory / "mod.py").write_text(source, encoding="utf-8")
    sites = find_match_sites(tmp_path, ("pkg",))
    assert [(site.path, site.scope) for site in sites] == [("pkg/mod.py", "<module>")]


# ---------------------------------------------------------------------------
# The shared AWS Region name rule (gco/aws_names.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "us-east-1",
        "ap-southeast-5",
        "eu-central-2",
        "mx-central-1",
        "us-gov-west-1",
        "cn-north-1",
        "us-isob-east-1",
        "eu-isoe-west-1",
    ],
)
def test_region_rule_accepts_region_names(name: str) -> None:
    assert is_aws_region_name(name)
    assert AWS_REGION_NAME_RE.fullmatch(name) is not None


@pytest.mark.parametrize(
    "value",
    [
        "us-east-1\n",
        "us-east-1\r\n",
        "\nus-east-1",
        " us-east-1",
        "us-east-1 ",
        "us-east-1\nus-west-2",
        "US-EAST-1",
        "us-east-1a",
        "us-east",
        "useast1",
        "us-east-1/x",
        "",
    ],
)
def test_region_rule_refuses_anything_else(value: str) -> None:
    assert not is_aws_region_name(value)


def test_region_pattern_is_unanchored_so_larger_patterns_can_embed_it() -> None:
    assert not AWS_REGION_NAME_PATTERN.startswith("^")
    assert not AWS_REGION_NAME_PATTERN.endswith("$")
    assert AWS_REGION_NAME_RE.pattern == AWS_REGION_NAME_PATTERN
