"""Suite for the wheel's own usage-query package contract.

Nothing else here checks packaging, and packaging is exactly what the move into
a wheel put at risk. A renamed module, a typo in the console-script target, or
a ``main`` that stopped existing all produce a distribution that installs
cleanly and cannot run -- and the OAuth suite, which loads its target by path,
would not notice any of it.
"""
import os
import re
import subprocess
import sys
import tokenize
import tomllib
from pathlib import Path

import _util


ROOT = Path(_util.SCRIPTS).parent
PACKAGE = "usage_query_lib"
MODULES = ("query",)


_TMP = []


def tempdir_of():
    return _TMP[0]


def _run(args):
    """Run the package as a subprocess from a directory outside the checkout.

    ``PYTHONPATH`` names the root explicitly so this checks the package import
    path rather than relying on the working directory being importable.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([sys.executable, *args], cwd=tempdir_of(),
                          capture_output=True, text=True, env=env, check=False,
                          timeout=120)


def test_every_module_is_importable_by_name(tmp):
    _TMP[:] = [tmp]
    for name in MODULES:
        r = _run(["-m", f"{PACKAGE}.{name}", "--version"])
        assert r.returncode == 0, f"{name} --version exited {r.returncode}: {r.stderr}"


def test_the_module_reports_its_own_version(tmp):
    """Own version: what the package declares, not a literal to keep in step.

    Pinning the number here made a release bump fail a test that has nothing
    to say about which version is correct -- only that the CLI and the package
    agree, which is the thing that breaks silently."""
    _TMP[:] = [tmp]
    mod = _util.load(_util.script("query.py"), "contract_version")
    r = _run(["-m", f"{PACKAGE}.query", "--version"])
    out = (r.stdout + r.stderr).strip()
    assert out == f"usage_query {mod.__version__}", out


def test_the_module_has_a_main_the_entry_point_can_call(tmp):
    mod = _util.load(_util.script("query.py"), "contract_query")
    main = getattr(mod, "main", None)
    assert callable(main), "query has no callable main()"
    # setuptools console scripts call it with no arguments.
    params = main.__code__.co_argcount - len(main.__defaults__ or ())
    assert params == 0, f"query.main requires {params} positional argument(s)"


def test_declared_console_scripts_resolve(tmp):
    """A typo here ships a wheel whose command does not exist."""
    del tmp
    with open(ROOT / "pyproject.toml", "rb") as fh:
        cfg = tomllib.load(fh)
    scripts = cfg["project"]["scripts"]
    assert scripts, "the wheel declares no commands"
    for command, target in scripts.items():
        module, _, func = target.partition(":")
        path = ROOT / (module.replace(".", os.sep) + ".py")
        assert path.is_file(), f"{command} points at missing module {module}"
        mod = _util.load(str(path), f"entry_{command.replace('-', '_')}")
        assert callable(getattr(mod, func, None)), (
            f"{command} points at {target}, which is not callable")


def test_the_distribution_version_is_the_one_setuptools_reads(tmp):
    del tmp
    with open(ROOT / "pyproject.toml", "rb") as fh:
        cfg = tomllib.load(fh)
    attr = cfg["tool"]["setuptools"]["dynamic"]["version"]["attr"]
    assert attr == "usage_query_lib.__version__", attr
    init = (ROOT / PACKAGE / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'^__version__\s*=\s*"(\d+\.\d+\.\d+)"', init, re.MULTILINE)
    assert found, f"no SemVer __version__ in {PACKAGE}/__init__.py"


def _requires_python_floor():
    """``requires-python`` as a (major, minor) tuple, from pyproject itself."""
    with open(ROOT / "pyproject.toml", "rb") as fh:
        cfg = tomllib.load(fh)
    spec = cfg["project"]["requires-python"]
    found = re.search(r">=\s*(\d+)\.(\d+)", spec)
    assert found, f"cannot read a floor out of requires-python = {spec!r}"
    return int(found.group(1)), int(found.group(2))


def _pep701_uses(path):
    """PEP 701 f-string constructs in ``path``, as (line, what) pairs.

    Two of them arrived in 3.12 and are a hard SyntaxError before it: a
    newline inside the replacement field of a singly-quoted f-string, and
    reusing the f-string's own quote character inside it. ``ast.parse``'s
    ``feature_version`` gates neither, so this reads tokens instead.
    """
    found = []
    if not hasattr(tokenize, "FSTRING_START"):
        return found
    with open(path, "rb") as fh:
        toks = list(tokenize.tokenize(fh.readline))
    depth = 0
    opened_at = quote = None
    for tok in toks:
        if tok.type == tokenize.FSTRING_START:
            if depth == 0:
                opened_at, quote = tok.start[0], tok.string.lstrip("fFrRbB")
            depth += 1
            continue
        if tok.type == tokenize.FSTRING_END:
            depth -= 1
            if depth == 0:
                opened_at = quote = None
            continue
        if depth and quote and len(quote) < 3:
            if tok.start[0] != opened_at and tok.type != tokenize.NL:
                found.append((opened_at, "newline inside the replacement field"))
            if tok.type == tokenize.STRING and tok.string.startswith(quote[0]):
                found.append((opened_at, "the f-string quote reused inside it"))
    return sorted(set(found))


def test_no_module_uses_syntax_the_oldest_supported_python_cannot_parse(tmp):
    """``requires-python`` is a promise the syntax has to keep."""
    floor = _requires_python_floor()
    if floor >= (3, 12):
        _util.skip(f"requires-python is already {floor[0]}.{floor[1]}")
    problems = []
    for path in sorted(Path(_util.SCRIPTS).glob("*.py")):
        for line, what in _pep701_uses(path):
            problems.append(f"{path.name}:{line}: {what}")
    assert not problems, (
        f"PEP 701 syntax, a SyntaxError on {floor[0]}.{floor[1]}: "
        + "; ".join(problems))


def test_the_package_ships_the_query_module(tmp):
    del tmp
    with open(ROOT / "pyproject.toml", "rb") as fh:
        cfg = tomllib.load(fh)
    assert cfg["tool"]["setuptools"]["packages"] == [PACKAGE]
    assert (ROOT / PACKAGE / "query.py").is_file()
    assert (ROOT / PACKAGE / "__init__.py").is_file()


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="pkgcontract_")


if __name__ == "__main__":
    raise SystemExit(main())
