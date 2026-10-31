"""A Claude plan without a 5h limit reports no 5h window, not a 0% one."""
import json
import os
import sys
from unittest import mock

import _util
from test_claude_accounts import Response, install, run, setup


def serving(http, five_hour=None, limits=None, tokens=("live-token", "beta-token")):
    """Rewrite the double's usage payload for the named tokens."""
    def request(req, timeout=None):
        payload = json.loads(http(req, timeout).raw)
        if req.get_header("Authorization") in {f"Bearer {t}" for t in tokens}:
            payload["five_hour"] = five_hour
            if limits is not None:
                payload["limits"] = limits
        return Response(payload)
    return mock.patch("urllib.request.urlopen", side_effect=request)


def test_null_five_hour_without_session_limit_is_absent_everywhere(tmp):
    with setup(tmp, {"live-token": 10, "beta-token": 40}) as (mod, accounts, http):
        install(accounts)
        weekly_limit = {"group": "weekly", "percent": 11}
        with serving(http, limits=[weekly_limit]):
            rc, raw, err = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and err == "" and got["errors"] == {}, (rc, raw, err)
        assert "five_hour" not in got["usage"]["claude"], got["usage"]["claude"]
        assert got["usage"]["claude"]["weekly"]["pct"] == 11
        for name in ("alpha", "beta"):
            assert "five_hour" not in got["claude_accounts"][name], got["claude_accounts"][name]
            assert "weekly" in got["claude_accounts"][name]
        os.remove(mod.CACHE)
        for cache in accounts.glob(".*.usage-cache"):
            cache.unlink()
        with serving(http, limits=[weekly_limit]):
            rc, text, err = run(mod)
        assert rc == 0 and err == "", (rc, text, err)
        rows = [line for line in text.splitlines() if line.startswith("Claude") or line.startswith(" ")]
        assert rows and not any(" 5h " in f" {line} " for line in text.splitlines()), text
        assert any("7d" in line for line in rows), text


def test_session_window_from_either_source_keeps_five_hour(tmp):
    cases = (
        ("null-with-session-limit", None, [{"group": "session", "percent": 0}], 0.0),
        ("present-without-limits", {"utilization": 33, "resets_at": None}, [], 33.0),
    )
    for label, five_hour, limits, pct in cases:
        home = os.path.join(tmp, label)
        os.makedirs(home)
        with setup(home, {"live-token": 10}) as (mod, _, http):
            with serving(http, five_hour, limits, tokens=("live-token",)):
                rc, raw, err = run(mod, "--json")
            got = json.loads(raw)
            assert rc == 0 and err == "", (label, rc, raw, err)
            assert got["usage"]["claude"]["five_hour"]["pct"] == pct, (label, got)
            with serving(http, five_hour, limits, tokens=("live-token",)):
                rc, text, _ = run(mod)
            assert any(line.split()[1:2] == ["5h"] for line in text.splitlines()
                       if line.startswith("Claude")), (label, text)


if __name__ == "__main__":
    sys.exit(_util.runner(_util.collect(globals()), "claudeabsent5h_"))
