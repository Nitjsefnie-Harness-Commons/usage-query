#!/usr/bin/env python3
"""Formatting, pace arithmetic and command-line coverage for the tool."""
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402


USAGE_QUERY = os.path.join(_util.SCRIPTS, "query.py")


def _load():
    return _util.load(USAGE_QUERY, "usage_query_behavior")


def _frozen_clock(mod, frozen):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return frozen.replace(tzinfo=None)
            return frozen.astimezone(tz)

    return mock.patch.object(mod, "datetime", FrozenDateTime)


def test_pace_arithmetic_marks_over_pace_and_recovery_time(tmp):
    del tmp
    mod = _load()
    frozen = datetime(2026, 1, 1, tzinfo=timezone.utc)
    reset = "2026-01-01T03:00:00+00:00"
    with _frozen_clock(mod, frozen):
        assert mod._pace_pct(reset, 5 * 3600) == 40.0
        assert mod._recover_dur(reset, 5 * 3600, 60.0) == "1h00m"
        rows = mod._table_rows("Claude", {
            "five_hour": {"pct": 60.0, "pace_pct": 40.0,
                          "recover_in": "1h00m", "resets_at": "reset",
                          "resets_in": "3h"},
        })
    assert rows[0][2] == "60%"
    assert rows[0][3] == "40%"
    assert rows[0][6] == "OVER PACE (on pace in 1h00m)"


def test_reset_rendering_handles_inactive_and_unknown_length_windows(tmp):
    del tmp
    mod = _load()
    label, duration = mod._reset_info(None)
    assert label == "no reset scheduled (window inactive)"
    assert duration == "—"
    reset = "2026-01-01T03:00:00+00:00"
    assert mod._pace_pct(reset, None) is None
    assert mod._recover_dur(reset, None, 80.0) is None
    rows = mod._table_rows("Kimi", {
        "five_hour": {"pct": 80.0, "pace_pct": None, "recover_in": None,
                       "resets_at": label, "resets_in": duration},
    })
    assert rows[0][3] == "—"
    assert rows[0][4] == label
    assert rows[0][5] == "—"


def test_table_output_contains_rows_for_multiple_providers(tmp):
    del tmp
    mod = _load()
    rows = []
    for account, used in (("Claude", 20), ("Kimi", 40), ("Codex", 60)):
        rows.extend(mod._table_rows(account, {
            "five_hour": {"pct": used, "pace_pct": 50,
                          "recover_in": None, "resets_at": "reset",
                          "resets_in": "1h"},
        }))
    output = mod._render_table(rows)
    assert output.splitlines()[0].startswith("account")
    for account in ("Claude", "Kimi", "Codex"):
        assert account in output
    assert "OVER PACE" in output
    assert "* max = utilization ceiling" in output
    assert "reset times are machine-local" in output


def _assert_provider_selection(mod, flag, selected):
    calls = {}

    def result(name):
        calls[name] = calls.get(name, 0) + 1
        return {"five_hour": {"pct": 1}}

    with mock.patch.object(mod, "query_claude", side_effect=lambda: result("claude")), \
            mock.patch.object(mod, "query_kimi", side_effect=lambda: result("kimi")), \
            mock.patch.object(mod, "query_codex", side_effect=lambda: result("codex")):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = mod.main([flag, "--json"])
    assert rc == 0
    assert set(json.loads(output.getvalue())["usage"]) == {selected}
    assert calls == {selected: 1}


def test_provider_selection_flags_query_only_the_selected_account(tmp):
    del tmp
    mod = _load()
    for flag, selected in (("--claude", "claude"), ("--kimi", "kimi"),
                           ("--codex", "codex")):
        _assert_provider_selection(mod, flag, selected)


def test_an_uninstalled_provider_is_skipped_not_reported_as_an_error(tmp):
    """A default sweep asks what is on this box; a provider that is not on it
    is not a failure. Removing Kimi from a machine turned every query into
    `Kimi: ERROR - FileNotFoundError ...` on stderr with exit status 1."""
    del tmp
    mod = _load()
    absent = mod.ProviderNotConfigured("Kimi is not configured on this machine")
    with mock.patch.object(mod, "query_claude",
                           side_effect=lambda: {"five_hour": {"pct": 1}}), \
            mock.patch.object(mod, "query_kimi", side_effect=absent), \
            mock.patch.object(mod, "query_codex",
                              side_effect=lambda: {"five_hour": {"pct": 2}}):
        output, errs = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errs):
            rc = mod.main(["--json"])
    payload = json.loads(output.getvalue())
    assert rc == 0
    assert payload["errors"] == {}
    assert set(payload["usage"]) == {"claude", "codex"}
    assert "kimi" in payload["unconfigured"]
    assert errs.getvalue() == ""


def test_naming_an_uninstalled_provider_is_still_an_error(tmp):
    """Asking for it by name makes its absence the answer to the question."""
    del tmp
    mod = _load()
    absent = mod.ProviderNotConfigured("Kimi is not configured on this machine")
    with mock.patch.object(mod, "query_kimi", side_effect=absent):
        output, errs = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errs):
            rc = mod.main(["--kimi", "--json"])
    payload = json.loads(output.getvalue())
    assert rc == 1
    assert "kimi" in payload["errors"]
    assert payload["unconfigured"] == {}


def test_a_removed_provider_is_not_spoken_for_by_its_leftover_cache(tmp):
    """The credential check runs before the cache read, so a cache written
    while the provider was installed cannot answer for it afterwards."""
    mod = _load()
    cache = os.path.join(tmp, "kimi-cache.json")
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump({"data": {"limits": []}, "fetched_at": 2 ** 31}, fh)
    with mock.patch.object(mod, "KIMI_CACHE", cache), \
            mock.patch.object(mod, "KIMI_CRED",
                              os.path.join(tmp, "absent.json")):
        try:
            mod.query_kimi()
        except mod.ProviderNotConfigured:
            pass
        else:
            raise AssertionError("a leftover cache answered for a removed provider")


def test_version_flag_prints_the_tool_version_without_querying(tmp):
    del tmp
    mod = _load()
    output = io.StringIO()
    with redirect_stdout(output):
        try:
            mod.main(["--version"])
        except SystemExit as exc:
            assert exc.code == 0
    assert output.getvalue().strip() == "usage_query 1.1.0"


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="usagequerybehavior_")


if __name__ == "__main__":
    raise SystemExit(main())
