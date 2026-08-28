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
            mock.patch.object(mod, "query_codex", side_effect=lambda: result("codex")), \
            mock.patch.object(mod, "query_zai", side_effect=lambda: result("zai")):
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
                           ("--codex", "codex"), ("--zai", "zai")):
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
                              side_effect=lambda: {"five_hour": {"pct": 2}}), \
            mock.patch.object(mod, "query_zai", side_effect=absent):
        output, errs = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errs):
            rc = mod.main(["--json"])
    payload = json.loads(output.getvalue())
    assert rc == 0
    assert payload["errors"] == {}
    assert set(payload["usage"]) == {"claude", "codex"}
    assert "kimi" in payload["unconfigured"]
    assert "zai" in payload["unconfigured"]
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
    with mock.patch.object(mod, "ZAI_CACHE", os.path.join(tmp, "zai-cache.json")), \
            mock.patch.object(mod, "ZAI_CRED",
                              os.path.join(tmp, "zai-absent.json")):
        try:
            mod.query_zai()
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
    assert output.getvalue().strip() == "usage_query 1.2.0"


def _zai_envelope():
    """A realistic /api/monitor/usage/quota/limit payload, captured live
    2026-08-28 from a lite-plan account: unit 3 = hours, 6 = weeks, the
    allowance confusingly named `usage`, plus an unobserved unit-4 window so
    the scoped-row fallback has something to fall back to."""
    def ms(dt):
        return int(dt.timestamp() * 1000)

    return {"code": 200, "msg": "Operation successful", "success": True,
            "data": {"level": "lite", "limits": [
                {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
                 "usage": 2000, "currentValue": 26, "remaining": 1973,
                 "percentage": 1,
                 "nextResetTime": ms(datetime(2026, 8, 28, 15, 0,
                                              tzinfo=timezone.utc))},
                {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
                 "usage": 10000, "currentValue": 52, "remaining": 9948,
                 "percentage": 2,
                 "nextResetTime": ms(datetime(2026, 9, 1, 12, 0,
                                              tzinfo=timezone.utc))},
                {"type": "CREDIT_LIMIT", "unit": 4, "number": 30,
                 "usage": 1000, "currentValue": 5, "remaining": 995,
                 "percentage": 1,
                 "nextResetTime": ms(datetime(2026, 8, 28, 18, 0,
                                              tzinfo=timezone.utc))},
            ]}}


def test_zai_normalization_keys_windows_plan_and_scoped_extras(tmp):
    del tmp
    mod = _load()
    frozen = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
    with _frozen_clock(mod, frozen):
        out = mod._normalize_zai(_zai_envelope(), "peak 1x")
    assert out["five_hour"]["pct"] == 1.0
    assert out["five_hour"]["pace_pct"] == 40.0  # 3h of the 5h window left
    assert out["five_hour"]["peak_note"] == "peak 1x"
    assert round(out["weekly"]["pace_pct"], 1) == 42.9  # 4d of 7d elapsed
    assert out["_plan_type"] == "lite"
    # The unobserved unit degrades to a visibly-labelled scoped row.
    assert [s["label"] for s in out["_scoped"]] == ["unit4x30"]


def test_zai_peak_note_follows_the_published_window(tmp):
    del tmp
    mod = _load()
    cases = [
        (datetime(2026, 1, 5, 7, 0, tzinfo=timezone.utc), "peak 1x"),
        (datetime(2026, 1, 5, 9, 59, tzinfo=timezone.utc), "peak 1x"),
        (datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc), "off-peak 0.5x"),
        (datetime(2026, 1, 5, 5, 59, tzinfo=timezone.utc), "off-peak 0.5x"),
        (datetime(2026, 1, 10, 15, 0, tzinfo=timezone.utc), "off-peak 0.5x"),
    ]
    for moment, expected in cases:
        with _frozen_clock(mod, moment):
            assert mod._zai_peak_note() == expected, moment.isoformat()


def test_zai_query_reads_the_harness_auth_file_and_caches(tmp):
    mod = _load()
    cred = os.path.join(tmp, "auth.json")
    with open(cred, "w", encoding="utf-8") as fh:
        json.dump({"zai": {"type": "api", "key": "k" * 49}}, fh)
    cache = os.path.join(tmp, "zai-cache.json")
    with mock.patch.object(mod, "ZAI_CRED", cred), \
            mock.patch.object(mod, "ZAI_CACHE", cache), \
            mock.patch.object(mod, "_get_retry",
                              side_effect=lambda *a, **k: _zai_envelope()):
        out = mod.query_zai()
    assert out["_plan_type"] == "lite"
    with open(cache, encoding="utf-8") as fh:
        assert json.load(fh)["data"]["code"] == 200


def test_zai_query_rejects_a_keyless_entry_and_an_error_envelope(tmp):
    mod = _load()
    cred = os.path.join(tmp, "auth.json")
    with open(cred, "w", encoding="utf-8") as fh:
        json.dump({"zai": {"type": "api"}}, fh)
    with mock.patch.object(mod, "ZAI_CRED", cred):
        try:
            mod.query_zai()
        except RuntimeError as exc:
            assert "no API key" in str(exc)
        else:
            raise AssertionError("a keyless zai entry authenticated")
    with open(cred, "w", encoding="utf-8") as fh:
        json.dump({"zai": {"type": "api", "key": "k"}}, fh)
    bad = _zai_envelope()
    bad["code"] = 500
    bad["msg"] = "boom"
    with mock.patch.object(mod, "ZAI_CRED", cred), \
            mock.patch.object(mod, "ZAI_CACHE", os.path.join(tmp, "c.json")), \
            mock.patch.object(mod, "_get_retry",
                              side_effect=lambda *a, **k: bad):
        try:
            mod.query_zai()
        except RuntimeError as exc:
            assert "boom" in str(exc)
        else:
            raise AssertionError("an error envelope was accepted")


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="usagequerybehavior_")


if __name__ == "__main__":
    raise SystemExit(main())
