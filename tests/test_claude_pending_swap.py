"""Pending switch state allows only cached stash readings and live refreshes."""
import json
import sys
import time
from pathlib import Path

import _util
from test_claude_accounts import install, run, setup
from test_claude_accounts_lock import assert_new_pair, refreshing


def pending_state(accounts, value=None):
    (accounts / "state.json").write_text(json.dumps({"pending": value}), encoding="utf-8")


def seed_cache(accounts, pct):
    path = accounts / ".beta.usage-cache"
    path.write_text(json.dumps({
        "fetched_at": time.time() - 1000,
        "data": {"five_hour": {"utilization": pct, "resets_at": None},
                 "seven_day": {"utilization": pct + 1, "resets_at": None}},
    }), encoding="utf-8")


def beta_setup(tmp):
    return setup(tmp, {"live-token": 10, "beta-token": 40, "beta-new": 55}, {
        "beta-token-refresh": {"access_token": "beta-new", "refresh_token": "beta-refresh-new", "expires_in": 3600},
    })


def test_pending_swap_uses_stale_cached_stash_without_refresh(tmp):
    with beta_setup(tmp) as (mod, accounts, http):
        install(accounts, beta_stale=True)
        pending_state(accounts, {"from": "alpha", "to": "beta"})
        seed_cache(accounts, 42)
        old = (accounts / "beta.json").read_bytes()
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and got["errors"] == {}, raw
        assert set(got["claude_accounts"]) == {"alpha", "beta"}
        assert got["claude_accounts"]["beta"]["five_hour"]["pct"] == 42
        assert got["claude_accounts"]["beta"]["weekly"]["pct"] == 43
        assert (accounts / "beta.json").read_bytes() == old
        assert http.calls == [("usage", "Bearer live-token")], "pending swap spent a stash refresh token"


def test_pending_key_without_cache_records_error_and_skips_stash(tmp):
    for index, value in enumerate((None, False, {})):
        home = Path(tmp) / f"case{index}"
        home.mkdir()
        with beta_setup(str(home)) as (mod, accounts, http):
            install(accounts, beta_stale=True)
            pending_state(accounts, value)
            old = (accounts / "beta.json").read_bytes()
            rc, raw, _ = run(mod, "--json")
            got = json.loads(raw)
            assert rc == 1 and set(got["errors"]) == {"claude:beta"}, raw
            assert "pending" in got["errors"]["claude:beta"].lower()
            assert set(got["claude_accounts"]) == {"alpha"}
            assert (accounts / "beta.json").read_bytes() == old
            assert http.calls == [("usage", "Bearer live-token")]


def test_pending_swap_without_cache_does_not_query_a_fresh_stash(tmp):
    with beta_setup(tmp) as (mod, accounts, http):
        install(accounts)
        pending_state(accounts)
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1 and set(got["errors"]) == {"claude:beta"}, raw
        assert http.calls == [("usage", "Bearer live-token")]


def test_state_without_pending_allows_refresh_and_is_not_an_account(tmp):
    with beta_setup(tmp) as (mod, accounts, http):
        install(accounts, beta_stale=True)
        (accounts / "state.json").write_text("{}", encoding="utf-8")
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and got["errors"] == {}, raw
        assert set(got["claude_accounts"]) == {"alpha", "beta"}
        assert got["claude_accounts"]["beta"]["five_hour"]["pct"] == 55
        assert http.calls == [("usage", "Bearer live-token"), ("refresh", "beta-token-refresh"),
                              ("usage", "Bearer beta-new")]


def test_pending_swap_leaves_live_refresh_unaffected(tmp):
    with refreshing(tmp) as (mod, accounts, http):
        pending_state(accounts)
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1 and set(got["errors"]) == {"claude:beta"}, raw
        assert got["usage"]["claude"]["five_hour"]["pct"] == 25
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert http.calls == [("refresh", "live-token-refresh"), ("usage", "Bearer new-access")]


def test_marker_naming_state_file_uses_live_destination(tmp):
    def repair_marker(accounts):
        (accounts / "live").write_text("beta\n", encoding="utf-8")

    with refreshing(tmp, change=repair_marker) as (mod, accounts, _):
        state = accounts / "state.json"
        state.write_text("{}", encoding="utf-8")
        (accounts / "live").write_text("state\n", encoding="utf-8")
        errors = {}
        got = mod.query_claude(errors=errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert state.read_text(encoding="utf-8") == "{}", "refresh overwrote switcher state"


if __name__ == "__main__":
    sys.exit(_util.runner(_util.collect(globals()), "claudependingswap_"))
