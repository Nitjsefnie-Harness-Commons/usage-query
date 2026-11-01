#!/usr/bin/env python3
"""A failed live fetch served from the cache says why, end to end.

Every provider that falls back to a cached reading when its live fetch raised
tags the entry with `_stale_error` ("<ExceptionType>: <message>", the form
`errors` uses) beside `_stale_age`, and the plain table appends it to the
account cell. A reading served with no fetch attempt carries neither.

Everything runs through `main()`: only the network call and the credential
lookup are replaced, so the real query function, cache read, table renderer
and exit-code logic are what the assertions see.
"""
import io
import json
import os
import sys
import time
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402
from test_usage_query_behavior import (  # noqa: E402
    _frozen_clock, _zai_envelope, _zai_sources)
from test_usage_query_oauth import _codex_payload  # noqa: E402


USAGE_QUERY = os.path.join(_util.SCRIPTS, "query.py")
# Inside the z.ai quota envelope's windows (the weekly one resets 2026-09-01).
MOMENT = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
NOW = MOMENT.timestamp()
STALE_FOR = 5000
# What the z.ai API answers for an account without a Coding Plan, as one site
# observed it; main() renders the raised error under `errors` the same way.
ZAI_NO_PLAN = {"code": 500, "msg": "当前用户不存在coding plan", "success": False}
ZAI_ERROR = "RuntimeError: z.ai quota API error: 当前用户不存在coding plan"
PROVIDERS = ("claude", "kimi", "codex", "zai")


def _load():
    return _util.load(USAGE_QUERY, "usage_query_stale_error")


class _Rig:
    """One provider wired to temp files and a scripted network.

    `fetch` is the mock standing in for the provider's one network call;
    `outcome` is what it does next: an exception to raise, or the payload to
    answer with. `seed(age)` writes the provider's cache `age` seconds old.
    """

    def __init__(self, mod, tmp, provider, stack):
        self.mod, self.provider, self.tmp = mod, provider, tmp
        self.display = {"claude": "Claude", "kimi": "Kimi", "codex": "Codex",
                        "zai": "z.ai"}[provider]
        self.error = "RuntimeError: refresh boom"
        self.outcome = RuntimeError("refresh boom")
        self.ttl = mod.CACHE_TTL
        cred = os.path.join(tmp, provider + "-cred.json")
        with open(cred, "w", encoding="utf-8") as fh:
            fh.write("{}")
        win = {"utilization": 7, "resets_at": None}
        if provider == "claude":
            self.cache, self.payload = os.path.join(tmp, "claude.cache"), {
                "five_hour": win, "seven_day": {"utilization": 8,
                                                "resets_at": None}}
            self.target = "_get_retry"
            stack.enter_context(mock.patch.object(mod, "CLAUDE_CRED", cred))
            stack.enter_context(mock.patch.object(mod, "CACHE", self.cache))
            stack.enter_context(mock.patch.object(
                mod, "_claude_access_token", return_value="token"))
        elif provider == "kimi":
            block = {"limit": "100", "remaining": "60",
                     "resetTime": "2026-09-01T00:00:00Z"}
            self.cache, self.payload = os.path.join(tmp, "kimi.cache"), {
                "limits": [{"window": {"duration": 300, "timeUnit": "MINUTE"},
                            "detail": block}], "usage": block}
            self.target = "_get"
            stack.enter_context(mock.patch.object(mod, "KIMI_CRED", cred))
            stack.enter_context(mock.patch.object(mod, "KIMI_CACHE",
                                                  self.cache))
            stack.enter_context(mock.patch.object(
                mod, "_kimi_access_token", return_value="token"))
        elif provider == "codex":
            self.cache = os.path.join(tmp, "codex.cache")
            self.payload = _codex_payload(now=NOW)
            self.target = "_codex_rate_limits"
            self.ttl = mod.CODEX_CACHE_TTL
            stack.enter_context(mock.patch.object(mod, "CODEX_CACHE",
                                                  self.cache))
        else:
            self.cache = os.path.join(tmp, "zai.cache")
            self.resets_cache = os.path.join(tmp, "zai-resets.cache")
            self.payload = _zai_envelope()
            self.target = "_get_retry"
            self.error, self.outcome = ZAI_ERROR, ZAI_NO_PLAN
            key_file = os.path.join(tmp, "zai-key")
            with open(key_file, "w", encoding="utf-8") as fh:
                fh.write("k" * 49 + "\n")
            stack.enter_context(_zai_sources(mod, files=(key_file,)))
            stack.enter_context(mock.patch.object(mod, "ZAI_CACHE",
                                                  self.cache))
            stack.enter_context(mock.patch.object(mod, "ZAI_RESETS_CACHE",
                                                  self.resets_cache))
        self.fetch = mock.Mock(side_effect=self._answer)
        stack.enter_context(mock.patch.object(mod, self.target, self.fetch))
        stack.enter_context(mock.patch.object(mod.time, "time",
                                              return_value=NOW))
        stack.enter_context(_frozen_clock(mod, MOMENT))

    def _answer(self, url=None, headers=None):
        if self.provider == "zai" and url == self.mod.ZAI_RESETS_URL:
            raise AssertionError("the reset list was fetched")
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def seed(self, age=STALE_FOR):
        with open(self.cache, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": NOW - age, "data": self.payload}, fh)

    def succeed(self):
        self.outcome = self.payload

    def main(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.mod.main(["--" + self.provider, *flags])
        return rc, out.getvalue(), err.getvalue()

    def entry(self):
        rc, raw, err = self.main("--json")
        got = json.loads(raw)
        return rc, got["usage"][self.provider], got["errors"], err

    def cell(self):
        """The account cell of the table's first row, as the plain run prints
        it (cells are joined by two spaces)."""
        rc, text, err = self.main()
        return rc, text.splitlines()[1].split("  ")[0], text, err


def _rig(tmp, provider, stack):
    return _Rig(_load(), tmp, provider, stack)


def _each(tmp, check):
    """Run `check(rig)` for every provider, each in its own directory."""
    for provider in PROVIDERS:
        home = os.path.join(tmp, provider)
        os.makedirs(home)
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            with ExitStack() as stack:
                try:
                    check(_rig(home, provider, stack))
                except AssertionError as exc:
                    raise AssertionError(f"{provider}: {exc}") from exc


def _assert_failed_fetch_is_labelled(tmp, provider):
    with mock.patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp}), \
            ExitStack() as stack:
        rig = _rig(tmp, provider, stack)
        # With nothing cached the same failure is an error: this pins the
        # "<ExceptionType>: <message>" form `_stale_error` has to repeat.
        rc, raw, _ = rig.main("--json")
        got = json.loads(raw)
        assert rc == 1 and got["errors"] == {provider: rig.error}, got
        assert got["usage"] == {}, got

        rig.seed()
        rc, entry, errors, err = rig.entry()
        assert entry["_stale_age"] == STALE_FOR, entry
        assert entry["_stale_error"] == rig.error, entry
        # A stale-served provider is still a reading, not a query failure.
        assert rc == 0 and errors == {} and err == "", (rc, errors, err)
        rc, cell, _, err = rig.cell()
        assert cell == (f"{rig.display} (cached {STALE_FOR}s; "
                        f"refresh failed: {rig.error})"), cell
        assert rc == 0 and err == "", (rc, err)


def test_claude_failed_fetch_served_from_cache_carries_the_error(tmp):
    _assert_failed_fetch_is_labelled(tmp, "claude")


def test_kimi_failed_fetch_served_from_cache_carries_the_error(tmp):
    _assert_failed_fetch_is_labelled(tmp, "kimi")


def test_codex_failed_fetch_served_from_cache_carries_the_error(tmp):
    _assert_failed_fetch_is_labelled(tmp, "codex")


def test_zai_failed_fetch_served_from_cache_carries_the_error(tmp):
    _assert_failed_fetch_is_labelled(tmp, "zai")


def test_a_fresh_live_read_carries_neither_stale_field(tmp):
    def check(rig):
        rig.seed(rig.ttl + 1)
        rig.succeed()
        rc, entry, errors, _ = rig.entry()
        assert rc == 0 and errors == {}, (rc, errors)
        assert rig.fetch.call_count >= 1
        assert "_stale_age" not in entry and "_stale_error" not in entry, entry
        rc, cell, text, _ = rig.cell()
        assert cell == rig.display, cell
        assert "refresh failed" not in text and "cached" not in text, text

    _each(tmp, check)


def test_a_cache_hit_inside_the_ttl_makes_no_fetch_and_no_stale_error(tmp):
    def check(rig):
        rig.seed(rig.ttl - 1)
        rc, entry, errors, _ = rig.entry()
        assert rc == 0 and errors == {}, (rc, errors)
        assert "_stale_age" not in entry and "_stale_error" not in entry, entry
        rc, cell, text, _ = rig.cell()
        assert cell == rig.display and "refresh failed" not in text, text
        assert rig.fetch.call_count == 0, rig.fetch.call_args_list

    _each(tmp, check)


def test_the_ttl_edge_decides_between_no_fetch_and_a_labelled_failure(tmp):
    """The other side of the previous boundary: one second past the TTL the
    fetch happens, fails, and the stale reading carries the error."""
    def check(rig):
        rig.seed(rig.ttl + 1)
        rc, entry, errors, _ = rig.entry()
        assert rc == 0 and errors == {}, (rc, errors)
        assert rig.fetch.call_count >= 1
        assert entry["_stale_age"] == rig.ttl + 1, entry
        assert entry["_stale_error"] == rig.error, entry

    _each(tmp, check)


def test_claude_lock_timeout_and_pending_swap_fallbacks_carry_no_error(tmp):
    for name, exc_name in (("lock", "_LockTimeout"),
                           ("swap", "_ClaudeSwapPending")):
        home = os.path.join(tmp, name)
        os.makedirs(home)
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}), \
                ExitStack() as stack:
            rig = _rig(home, "claude", stack)
            stack.enter_context(mock.patch.object(
                rig.mod, "_claude_access_token",
                side_effect=getattr(rig.mod, exc_name)("held")))
            rig.seed()
            rc, entry, errors, err = rig.entry()
            assert rc == 0 and errors == {} and err == "", (name, rc, errors)
            assert entry["_stale_age"] == STALE_FOR, (name, entry)
            assert "_stale_error" not in entry, (name, entry)
            rc, cell, text, _ = rig.cell()
            assert cell == f"Claude (cached {STALE_FOR}s)", (name, cell)
            assert "refresh failed" not in text, (name, text)
            assert rig.fetch.call_count == 0, (name, rig.fetch.call_args_list)


def test_zai_reset_list_read_without_live_carries_no_stale_error(tmp):
    """A failed quota fetch never asks for the reset list: its stale cache
    answers (live=False) and neither it nor the quota entry may report a
    failure that belongs to the quota call alone."""
    with mock.patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp}), \
            ExitStack() as stack:
        rig = _rig(tmp, "zai", stack)
        rig.seed()
        envelope = {"code": 200, "msg": "ok", "success": True,
                    "data": {"weekResets": [{
                        "recordId": 1, "grantType": "DIRECT",
                        "expireTime": "2026-10-29 00:13:36",
                        "available": True}]}}
        with open(rig.resets_cache, "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": NOW - STALE_FOR, "data": envelope}, fh)
        rc, entry, errors, _ = rig.entry()
        assert rc == 0 and errors == {}, (rc, errors)
        assert entry["_stale_error"] == ZAI_ERROR, entry
        assert entry["_weekly_resets"]["stale_age"] == STALE_FOR, entry
        assert "_stale_error" not in entry["_weekly_resets"], entry
        assert [c.args[0] for c in rig.fetch.call_args_list] == [
            rig.mod.ZAI_URL], rig.fetch.call_args_list


def test_cached_fetch_reports_an_error_only_for_a_fetch_that_raised(tmp):
    mod = _load()
    path = os.path.join(tmp, "cache.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": time.time() - 600, "data": {"v": 1}}, fh)
    never = mock.Mock(side_effect=AssertionError("fetched"))
    # live=False: no attempt, so no error to report.
    assert mod._cached_fetch(path, never, live=False)[0] == {"v": 1}
    assert mod._cached_fetch(path, never, live=False)[2:] == (None,)
    never.assert_not_called()
    # live: the attempt raised, so the error is reported in `errors` form.
    failing = mock.Mock(side_effect=ValueError("down"))
    data, age, error = mod._cached_fetch(path, failing)
    assert data == {"v": 1} and 599 <= age <= 700, (data, age)
    assert error == "ValueError: down", error
    # A success is neither stale nor in error.
    ok = mock.Mock(return_value={"v": 2})
    assert mod._cached_fetch(path, ok) == ({"v": 2}, 0, None)
    # Nothing cached and no fetch allowed still raises, as before.
    os.remove(path)
    try:
        mod._cached_fetch(path, never, live=False)
    except LookupError:
        pass
    else:
        raise AssertionError("no cache and no fetch did not raise")


def main():
    return _util.runner(_util.collect(globals()),
                        tmp_prefix="usagequerystaleerror_")


if __name__ == "__main__":
    raise SystemExit(main())
