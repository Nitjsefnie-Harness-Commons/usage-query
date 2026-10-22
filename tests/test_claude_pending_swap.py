"""Pending switch state pauses Claude requests and serves cached readings."""
import json
import sys
import time
from pathlib import Path
from unittest import mock

import _util
from test_claude_accounts import install, run, setup
from test_claude_accounts_lock import assert_new_pair, refreshing, replace_bytes


def pending_state(accounts, value=None):
    (accounts / "state.json").write_text(json.dumps({"pending": value}), encoding="utf-8")


def seed_cache(path, pct, age=1000):
    path.write_text(json.dumps({
        "fetched_at": time.time() - age,
        "data": {"five_hour": {"utilization": pct, "resets_at": None},
                 "seven_day": {"utilization": pct + 1, "resets_at": None}},
    }), encoding="utf-8")


def home_files(home):
    return {str(path.relative_to(home)): path.read_bytes()
            for path in Path(home).rglob("*") if path.is_file()}


def beta_setup(tmp):
    return setup(tmp, {"live-token": 10, "beta-token": 40, "beta-new": 55}, {
        "beta-token-refresh": {"access_token": "beta-new", "refresh_token": "beta-refresh-new", "expires_in": 3600},
    })


def test_pending_swap_uses_stale_cached_stash_without_refresh(tmp):
    with beta_setup(tmp) as (mod, accounts, http):
        install(accounts, beta_stale=True)
        seed_cache(Path(mod.CACHE), 10, age=0)
        pending_state(accounts, {"from": "alpha", "to": "beta"})
        seed_cache(accounts / ".beta.usage-cache", 42)
        old = (accounts / "beta.json").read_bytes()
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and got["errors"] == {}, raw
        assert set(got["claude_accounts"]) == {"alpha", "beta"}
        assert got["claude_accounts"]["beta"]["five_hour"]["pct"] == 42
        assert got["claude_accounts"]["beta"]["weekly"]["pct"] == 43
        assert (accounts / "beta.json").read_bytes() == old
        assert http.calls == [], "pending swap made a Claude request"


def test_pending_key_without_cache_records_error_and_skips_stash(tmp):
    for index, value in enumerate((None, False, {})):
        home = Path(tmp) / f"case{index}"
        home.mkdir()
        with beta_setup(str(home)) as (mod, accounts, http):
            install(accounts, beta_stale=True)
            seed_cache(Path(mod.CACHE), 10, age=0)
            pending_state(accounts, value)
            old = (accounts / "beta.json").read_bytes()
            rc, raw, _ = run(mod, "--json")
            got = json.loads(raw)
            assert rc == 1 and set(got["errors"]) == {"claude:beta"}, raw
            assert "pending" in got["errors"]["claude:beta"].lower()
            assert set(got["claude_accounts"]) == {"alpha"}
            assert (accounts / "beta.json").read_bytes() == old
            assert http.calls == []


def test_pending_swap_without_cache_does_not_query_a_fresh_stash(tmp):
    with beta_setup(tmp) as (mod, accounts, http):
        install(accounts)
        seed_cache(Path(mod.CACHE), 10, age=0)
        pending_state(accounts)
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1 and set(got["errors"]) == {"claude:beta"}, raw
        assert http.calls == []


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


def test_pending_swap_reports_error_for_uncached_live_reading(tmp):
    with refreshing(tmp) as (mod, accounts, http):
        pending_state(accounts)
        before = home_files(tmp)
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1 and set(got["errors"]) == {"claude", "claude:beta"}, raw
        assert "pending" in got["errors"]["claude"].lower()
        assert got["usage"] == {} and got["claude_accounts"] == {}
        assert http.calls == [], "uncached live reading refreshed during a pending swap"
        assert home_files(tmp) == before, "pending live query changed files"


def test_pending_swap_reports_stale_cached_live_reading(tmp):
    with refreshing(tmp) as (mod, accounts, http):
        pending_state(accounts)
        seed_cache(Path(mod.CACHE), 17)
        before = home_files(tmp)
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1 and set(got["errors"]) == {"claude:beta"}, raw
        assert got["usage"]["claude"]["five_hour"]["pct"] == 17
        assert got["usage"]["claude"]["weekly"]["pct"] == 18
        assert got["usage"]["claude"]["_stale_age"] >= 1000
        assert got["claude_accounts"]["alpha"]["five_hour"]["pct"] == 17
        assert http.calls == [], "stale cached live reading refreshed during a pending swap"
        assert home_files(tmp) == before


def test_pending_recovery_during_live_refresh_cannot_spend_shared_pair(tmp):
    with setup(tmp, {"beta-new": 55}, {
        "beta-token-refresh": {"access_token": "beta-new", "refresh_token": "beta-refresh-new", "expires_in": 3600},
    }) as (mod, accounts, http):
        install(accounts, beta_stale=True)
        Path(mod.CLAUDE_CRED).write_bytes((accounts / "beta.json").read_bytes())
        pending_state(accounts, {"from": "alpha", "to": "beta"})
        before = home_files(tmp)
        recovered = []

        def request(req, timeout=None):
            answer = http(req, timeout)
            if req.full_url == mod.CLAUDE_OAUTH_TOKEN_URL:
                with mod._claude_accounts_lock(str(accounts)):
                    assert Path(mod.CLAUDE_CRED).read_bytes() == (accounts / "beta.json").read_bytes()
                    replace_bytes(accounts / "live", b"beta\n")
                    replace_bytes(accounts / "state.json", b"{}")
                recovered.append(True)
            return answer

        with mock.patch("urllib.request.urlopen", side_effect=request):
            rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert http.calls == [], "pending recovery race spent the shared refresh token"
        assert recovered == [], "recovery callback ran during a forbidden refresh"
        assert home_files(tmp) == before, "pending recovery race changed credentials or marker"
        assert rc == 1 and "pending" in got["errors"].get("claude", "").lower(), raw


def test_state_without_pending_allows_live_refresh(tmp):
    with refreshing(tmp) as (mod, accounts, http):
        (accounts / "state.json").write_text("{}", encoding="utf-8")
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and got["errors"] == {}, raw
        assert got["usage"]["claude"]["five_hour"]["pct"] == 25
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert http.calls == [("refresh", "live-token-refresh"), ("usage", "Bearer new-access"),
                              ("usage", "Bearer beta-token")]


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
