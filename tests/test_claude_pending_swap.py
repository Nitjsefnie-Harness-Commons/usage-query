"""Pending switch state pauses Claude requests and serves cached readings; each reading stays its own account's."""
import json
import sys
import time
from pathlib import Path
from unittest import mock

import _util
from test_claude_accounts import credentials, install, run_json, setup
from test_claude_accounts_lock import SWITCHED, assert_new_pair, refreshing, replace_bytes, switch_error
from test_claude_backoff import throttled


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


def beta_cases(tmp, values):
    for index, value in enumerate(values):
        home = Path(tmp) / f"case{index}"
        home.mkdir()
        with beta_setup(str(home)) as (mod, accounts, http):
            assert Path(mod.claude_state.__file__).samefile(_util.script("claude_state.py")), "not this checkout's claude_state"
            install(accounts, beta_stale=True)
            yield value, mod, accounts, http


def test_pending_swap_uses_stale_cached_stash_without_refresh(tmp):
    with beta_setup(tmp) as (mod, accounts, http):
        install(accounts, beta_stale=True)
        seed_cache(Path(mod.CACHE), 10, age=0)
        pending_state(accounts, {"from": "alpha", "to": "beta"})
        seed_cache(accounts / ".beta.usage-cache", 42)
        old = (accounts / "beta.json").read_bytes()
        rc, raw, _, got = run_json(mod)
        assert rc == 0 and got["errors"] == {} and set(got["claude_accounts"]) == {"alpha", "beta"}, raw
        assert [got["claude_accounts"]["beta"][window]["pct"] for window in ("five_hour", "weekly")] == [42, 43], got
        assert (accounts / "beta.json").read_bytes() == old
        assert http.calls == [], "pending swap made a Claude request"


def test_pending_key_without_cache_records_error_and_skips_stash(tmp):
    for value, mod, accounts, http in beta_cases(tmp, (None, False, {})):
        seed_cache(Path(mod.CACHE), 10, age=0)
        pending_state(accounts, value)
        old = (accounts / "beta.json").read_bytes()
        rc, raw, _, got = run_json(mod)
        assert rc == 1 and set(got["errors"]) == {"claude:beta"} and "pending" in got["errors"]["claude:beta"].lower(), raw
        assert set(got["claude_accounts"]) == {"alpha"}
        assert (accounts / "beta.json").read_bytes() == old
        assert http.calls == []


def test_pending_swap_without_cache_does_not_query_a_fresh_stash(tmp):
    with beta_setup(tmp) as (mod, accounts, http):
        install(accounts)
        seed_cache(Path(mod.CACHE), 10, age=0)
        pending_state(accounts)
        rc, raw, _, got = run_json(mod)
        assert rc == 1 and set(got["errors"]) == {"claude:beta"}, raw
        assert http.calls == []


def test_unreadable_state_is_pending_and_serves_the_cache(tmp):
    for content, mod, accounts, http in beta_cases(tmp, (b'{"pending": ', b"\xff\xfe{", b"[" * 100000, None)):
        seed_cache(Path(mod.CACHE), 10, age=0)
        seed_cache(accounts / ".beta.usage-cache", 42)
        if content is None:
            (accounts / "state.json").mkdir()
        else:
            (accounts / "state.json").write_bytes(content)
        rc, raw, _, got = run_json(mod)
        assert rc == 0 and got["errors"] == {}, (content and content[:12], raw)
        assert got["claude_accounts"]["beta"]["five_hour"]["pct"] == 42 and http.calls == [], (content and content[:12], raw)


def test_state_without_pending_allows_refresh_and_is_not_an_account(tmp):
    for text, mod, accounts, http in beta_cases(tmp, ("{}", '"no pending swap"', "5", "[]", "null")):
        (accounts / "state.json").write_text(text, encoding="utf-8")
        rc, raw, _, got = run_json(mod)
        assert rc == 0 and got["errors"] == {} and set(got["claude_accounts"]) == {"alpha", "beta"}, raw
        assert got["claude_accounts"]["beta"]["five_hour"]["pct"] == 55
        assert http.calls == [("usage", "Bearer live-token"), ("refresh", "beta-token-refresh"), ("usage", "Bearer beta-new")]


def test_pending_swap_reports_error_for_uncached_live_reading(tmp):
    with refreshing(tmp) as (mod, accounts, http):
        pending_state(accounts)
        before = home_files(tmp)
        rc, raw, _, got = run_json(mod)
        assert rc == 1 and set(got["errors"]) == {"claude", "claude:beta"} and "pending" in got["errors"]["claude"].lower(), raw
        assert got["usage"] == {} and got["claude_accounts"] == {}
        assert http.calls == [], "uncached live reading refreshed during a pending swap"
        assert home_files(tmp) == before, "pending live query changed files"


def test_pending_swap_reports_stale_cached_live_reading(tmp):
    with refreshing(tmp) as (mod, accounts, http):
        pending_state(accounts)
        seed_cache(Path(mod.CACHE), 17)
        before = home_files(tmp)
        rc, raw, _, got = run_json(mod)
        assert rc == 1 and set(got["errors"]) == {"claude:beta"}, raw
        assert [got["usage"]["claude"][window]["pct"] for window in ("five_hour", "weekly")] == [17, 18], got
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
            rc, raw, _, got = run_json(mod)
        assert http.calls == [], "pending recovery race spent the shared refresh token"
        assert recovered == [], "recovery callback ran during a forbidden refresh"
        assert home_files(tmp) == before, "pending recovery race changed credentials or marker"
        assert rc == 1 and "pending" in got["errors"].get("claude", "").lower(), raw


def test_state_without_pending_allows_live_refresh(tmp):
    with refreshing(tmp) as (mod, accounts, http):
        (accounts / "state.json").write_text("{}", encoding="utf-8")
        rc, raw, _, got = run_json(mod)
        assert rc == 0 and got["errors"] == {} and got["usage"]["claude"]["five_hour"]["pct"] == 25, raw
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
        assert switch_error(mod.query_claude, errors=errors) == SWITCHED and errors == {}
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert state.read_text(encoding="utf-8") == "{}", "refresh overwrote switcher state"


def test_without_accounts_the_cache_follows_the_logged_in_account(tmp):
    with setup(tmp, {"live-token": 10, "other-token": 77}) as (mod, _, http):
        now = time.time()
        for uuid, token, later, pct, stale in (("u-1", "live-token", 0, 10, False), ("u-1", "refreshed", 0, 10, False),
                                               ("u-2", "other-token", 0, 77, False), ("u-2", "refreshed", 301, 77, True)):
            (Path(tmp) / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}), encoding="utf-8")
            credentials(Path(mod.CLAUDE_CRED), token)
            with mock.patch.object(mod.time, "time", return_value=now + later):
                reading = run_json(mod)[3]["usage"]["claude"]
            assert (reading["five_hour"]["pct"], "_stale_error" in reading) == (pct, stale), (uuid, token, reading)
        assert http.calls == [("usage", "Bearer live-token"), ("usage", "Bearer other-token"), ("usage", "Bearer refreshed")]


def test_a_switch_during_the_token_read_files_no_reading_under_the_wrong_account(tmp):
    with setup(tmp, {"live-token": 10, "beta-token": 40, "other-token": 77, "limited-token": throttled()}) as (mod, accounts, http):
        install(accounts)
        cache, probe = Path(mod.CACHE), mock.Mock(side_effect=lambda token: http.calls.append(("probe", token)) or {"seven_day": {"utilization": 33.0, "resets_at": None}})

        def switch(name, token, uuid=None):  # what the switcher, or a /login (`uuid`), leaves behind
            credentials(Path(mod.CLAUDE_CRED), token)
            (accounts / "live").write_text(name + "\n", encoding="utf-8")
            (Path(tmp) / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}), encoding="utf-8")
        mod.query_claude(errors={})
        fresh, until = json.loads(cache.read_text(encoding="utf-8")), time.time() + 60
        stale, token_read = fresh | {"fetched_at": 0}, (mod, "_claude_access_token")
        for row, (record, (where, what), name, token, uuid, kept, sent, held) in enumerate((  # where the switch lands; the account it leaves live; the token held before
                ({"account": fresh["account"], "backoff": {"streak": 1, "until": until}}, token_read, "beta", "beta-token", None, True, [], "live-token"),  # the 429-backoff probe, no stale reading to serve
                (stale | {"backoff": {"streak": 1, "until": until}}, token_read, "beta", "beta-token", None, True, [], "live-token"),  # the 429-backoff probe
                (stale, token_read, "alpha", "other-token", "u-2", True, [], "live-token"),  # a /login past the switcher keeps the marker
                (fresh, (mod.cache_store, "read"), "beta", "beta-token", None, True, [], "live-token"),  # a fresh hit reads no token: the end check
                (stale, (mod, "_get_retry"), "beta", "beta-token", None, True, [("usage", "Bearer live-token")], "live-token"),  # after the token check: the pre-write check
                (stale, (mod, "_get_retry"), "alpha", "other-token", "u-2", True, [("usage", "Bearer other-token")], "other-token"),  # a /login whose credentials beat the token read, ~/.claude.json not
                (stale | {"backoff": {"streak": 1, "until": until}}, (mod, "_claude_header_probe"), "alpha", "other-token", "u-2", True, [("probe", "other-token")], "other-token"),  # the same, around the backoff probe
                (stale, (mod, "_get_retry"), "alpha", "limited-token", "u-2", True, [("usage", "Bearer limited-token")], "limited-token"),  # the same, around a 429: no backoff written
                ({"account": fresh["account"]}, (mod, "_claude_header_probe"), "alpha", "limited-token", "u-2", False, [("usage", "Bearer limited-token"), ("probe", "limited-token")], "limited-token"),  # around its probe
                (stale, token_read, "beta", "beta-token", None, True, [], "live-token")), 1):  # the switcher, vs row 3's /login
            switch("alpha", held)
            cache.write_text(json.dumps(record), encoding="utf-8")
            before, calls = cache.read_bytes(), len(http.calls)
            with mock.patch.object(mod, "_claude_header_probe", probe), \
                    mock.patch.object(where, what, side_effect=lambda *a, r=getattr(where, what), n=name, t=token, u=uuid: switch(n, t, u) or r(*a)):
                assert switch_error(mod.query_claude, errors={}) == SWITCHED, (row, what, token)
            assert (cache.read_bytes() == before, json.loads(cache.read_bytes()).get("fetched_at"), http.calls[calls:]) == (kept, record.get("fetched_at"), sent), (row, what, token)
        switch("alpha", "live-token")  # the previous run filed nothing: alpha's next run reads its own
        assert run_json(mod)[3]["usage"]["claude"]["five_hour"]["pct"] == 10
        credentials(accounts / "alpha.json", "live-token")  # the switcher stashes alpha, installs beta: a switch inside the TTL
        switch("beta", "beta-token")
        got = run_json(mod)[3]
        assert (got["usage"]["claude"]["five_hour"]["pct"], got["claude_accounts"]["alpha"]["five_hour"]["pct"]) == (40, 10) and got["claude_accounts"]["beta"] == {"live": True, **got["usage"]["claude"]}, got
        switch("beta", "other-token", "u-2")  # a /login; then the switcher installs alpha between main's live reading and its accounts list
        with mock.patch.object(mod, "query_claude", side_effect=lambda *a, q=mod.query_claude, **k: q(*a, **k) if a else (q(**k), switch("alpha", "live-token"))[0]):
            got = run_json(mod)[3]
        assert (got["usage"]["claude"]["five_hour"]["pct"], got["claude_accounts"]["alpha"]["live"], got["claude_accounts"]["alpha"]["five_hour"]["pct"]) == (77, True, 10), got
        assert http.calls == [("usage", "Bearer live-token"), ("usage", "Bearer live-token"), ("usage", "Bearer other-token"), ("probe", "other-token"), ("usage", "Bearer limited-token"),
                              ("usage", "Bearer limited-token"), ("probe", "limited-token"), ("usage", "Bearer live-token"), ("usage", "Bearer beta-token"), ("usage", "Bearer beta-token"), ("usage", "Bearer live-token"), ("usage", "Bearer other-token")]


if __name__ == "__main__":
    sys.exit(_util.runner(_util.collect(globals()), "claudependingswap_"))
