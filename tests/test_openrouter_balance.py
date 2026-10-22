"""Optional OpenRouter helper execution and public balance output."""
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _util


BALANCE = {"total_credits": 170.0, "total_usage": 166.6, "balance": 3.36}


def setup(tmp, source=None):
    mod = _util.load(_util.script("query.py"))
    helper = Path(tmp) / ".agent-bundle" / "scripts" / "openrouter_key.py"
    if source is not None:
        helper.parent.mkdir(parents=True)
        helper.write_text(source, encoding="utf-8")
    return mod, helper


def run(mod, *flags):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(mod, "query_claude", return_value={}), \
            redirect_stdout(out), redirect_stderr(err):
        rc = mod.main(["--claude", *flags])
    return rc, out.getvalue(), err.getvalue()


def test_absent_script_is_silent_and_never_spawned(tmp):
    mod, helper = setup(tmp)
    assert not helper.exists()
    with mock.patch("subprocess.run", side_effect=AssertionError("absent script spawned")):
        rc, raw, err = run(mod, "--json")
        assert rc == 0 and err == ""
        assert json.loads(raw) == {"usage": {"claude": {}}, "errors": {}, "unconfigured": {}}
        assert run(mod) == (0, "", "")


def test_present_script_reports_exact_object_and_text_balance(tmp):
    mod, _ = setup(tmp, "import json, sys\n"
                   "assert sys.argv[1:] == ['credits', '--json']\n"
                   f"print(json.dumps({BALANCE!r}))\n")
    rc, raw, err = run(mod, "--json")
    got = json.loads(raw)
    assert rc == 0 and err == "", (rc, raw, err)
    assert got.get("openrouter") == BALANCE
    assert set(got["usage"]) == {"claude"}
    assert run(mod) == (0, "OpenRouter balance $3.36\n", "")


def test_present_failing_script_records_one_error(tmp):
    mod, _ = setup(tmp, "import sys\nsys.exit(7)\n")
    rc, raw, err = run(mod, "--json")
    got = json.loads(raw)
    assert rc == 1 and err == ""
    assert "openrouter" not in got
    assert set(got["errors"]) == {"openrouter"}
    assert "7" in got["errors"]["openrouter"]
    rc, text, err = run(mod)
    assert rc == 1 and text == ""
    assert "OpenRouter: ERROR" in err
    assert run(mod, "--quiet") == (1, "", "")


def test_script_is_invoked_with_argv_no_shell_and_timeout(tmp):
    mod, helper = setup(tmp, "")

    def execute(argv, **kwargs):
        assert argv[0] == sys.executable
        assert Path(argv[1]) == helper
        assert argv[2:] == ["credits", "--json"]
        assert kwargs.get("shell", False) is False
        assert 0 < kwargs["timeout"] <= 30
        assert kwargs["capture_output"] and kwargs["text"] and kwargs["check"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(BALANCE), "")

    with mock.patch("subprocess.run", side_effect=execute):
        rc, raw, _ = run(mod, "--json")
    assert rc == 0 and json.loads(raw)["openrouter"] == BALANCE, (rc, raw)


def test_timeout_records_an_error_and_keeps_usage(tmp):
    mod, _ = setup(tmp, "")
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("helper", 5)):
        rc, raw, _ = run(mod, "--json")
    got = json.loads(raw)
    assert rc == 1 and "openrouter" not in got
    assert set(got["errors"]) == {"openrouter"}
    assert got["usage"] == {"claude": {}}


def test_invalid_json_or_balance_is_an_error(tmp):
    for name, source in (("invalid", "print('not json')\n"),
                         ("missing", "print('{}')\n"),
                         ("wrong", "print('{\"balance\":\"invalid\"}')\n")):
        home = Path(tmp) / name
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            mod, _ = setup(str(home), source)
            rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1 and "openrouter" not in got, raw
        assert set(got["errors"]) == {"openrouter"}


if __name__ == "__main__":
    sys.exit(_util.runner(_util.collect(globals()), "openrouterbalance_"))
