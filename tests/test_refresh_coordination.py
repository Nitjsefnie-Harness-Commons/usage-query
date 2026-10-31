"""Concurrent refreshes of one single-use refresh token spend it once."""
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.error
from datetime import datetime, timezone
import urllib.parse
from pathlib import Path
from unittest import mock

import _util
from test_claude_accounts import credentials, install, setup
from test_usage_query_oauth import _FakeResponse, _fake_jwt, _wham_payload, _write_codex_auth, _write_kimi_cred

USAGE_QUERY = os.path.join(_util.SCRIPTS, "query.py")
WAIT = 10  # failure guard only: every wait below ends on an event


class SingleUseEndpoint:
    """A token endpoint whose refresh token works once.

    The first request is held until the second contender either reaches the
    endpoint too or waits on the refresh lock, so both orders are forced
    without sleeping: an uncoordinated pair always races.
    """

    def __init__(self, valid, rotated, token_of, check=None):
        self.check = check
        self.valid = {valid}
        self.rotated = rotated
        self.token_of = token_of
        self.sent = []
        self.contended = threading.Event()
        self.guard = threading.Lock()

    def __call__(self, req, timeout=None):
        token = self.token_of(req)
        if self.check:
            self.check()
        with self.guard:
            self.sent.append(token)
            first = len(self.sent) == 1
        if first:
            assert self.contended.wait(WAIT), "the second contender never contended"
        else:
            self.contended.set()
        with self.guard:
            if token in self.valid:
                self.valid.discard(token)
                return _FakeResponse(self.rotated)
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(b'{"error": "invalid_grant"}'))

    def sleep_hook(self, real_sleep):
        def sleep(seconds):
            self.contended.set()
            real_sleep(min(seconds, 0.01))
        return sleep


def contend(call, count=2):
    """Run `call` in `count` threads released together; return results/errors."""
    start = threading.Barrier(count)
    results, errors = [], []
    out = threading.Lock()

    def worker():
        start.wait(WAIT)
        try:
            value = call()
        except Exception as exc:  # noqa: BLE001
            with out:
                errors.append(f"{type(exc).__name__}: {exc}")
        else:
            with out:
                results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT * 3)
        assert not thread.is_alive(), "a contender hung"
    return results, errors


def form_token(req):
    return dict(urllib.parse.parse_qsl(req.data.decode("utf-8")))["refresh_token"]


def json_token(req):
    return json.loads(req.data)["refresh_token"]


def test_kimi_contenders_spend_the_refresh_token_once(tmp):
    mod = _util.load(USAGE_QUERY, "usage_query_kimi_race")
    mod.KIMI_CRED = os.path.join(tmp, "kimi-code.json")
    _write_kimi_cred(mod.KIMI_CRED, {"access_token": "access-old", "refresh_token": "refresh-old",
                                     "expires_at": time.time() - 60})
    endpoint = SingleUseEndpoint("refresh-old", {"access_token": "access-new",
                                                 "refresh_token": "refresh-new",
                                                 "expires_in": 900}, form_token)
    with mock.patch("urllib.request.urlopen", side_effect=endpoint), \
            mock.patch.object(mod, "_kimi_client_id", return_value="public-client"), \
            mock.patch.object(mod.time, "sleep", side_effect=endpoint.sleep_hook(time.sleep)):
        results, errors = contend(mod._kimi_access_token)
    assert errors == [], errors
    assert results == ["access-new", "access-new"], results
    assert endpoint.sent == ["refresh-old"], endpoint.sent
    with open(mod.KIMI_CRED, encoding="utf-8") as f:
        assert json.load(f)["refresh_token"] == "refresh-new"
    assert os.listdir(tmp) == ["kimi-code.json"], "lock or temp file left behind"


def test_codex_contenders_spend_the_refresh_token_once(tmp):
    mod = _util.load(USAGE_QUERY, "usage_query_codex_race")
    mod.CODEX_AUTH = os.path.join(tmp, "auth.json")
    _write_codex_auth(mod.CODEX_AUTH, -60)
    rotated = _fake_jwt(time.time() + 86400)
    endpoint = SingleUseEndpoint("refresh-token-old", {"access_token": rotated,
                                                       "refresh_token": "refresh-token-new"},
                                 json_token)
    used = []

    def get(url, headers):
        used.append(headers["Authorization"])
        return _wham_payload()

    with mock.patch("urllib.request.urlopen", side_effect=endpoint), \
            mock.patch.object(mod, "_get_retry", side_effect=get), \
            mock.patch.object(mod.time, "sleep", side_effect=endpoint.sleep_hook(time.sleep)):
        results, errors = contend(mod._codex_rate_limits)
    assert errors == [], errors
    assert len(results) == 2
    assert endpoint.sent == ["refresh-token-old"], endpoint.sent
    assert set(used) == {f"Bearer {rotated}"}, used
    with open(mod.CODEX_AUTH, encoding="utf-8") as f:
        assert json.load(f)["tokens"]["refresh_token"] == "refresh-token-new"
    assert os.listdir(tmp) == ["auth.json"], "lock or temp file left behind"


def claude_endpoint(check=None):
    return SingleUseEndpoint("beta-token-refresh", {"access_token": "beta-new",
                                                    "refresh_token": "beta-refresh-new",
                                                    "expires_in": 3600}, json_token, check)


def test_claude_stash_contenders_spend_the_refresh_token_once(tmp):
    with setup(tmp) as (mod, accounts, _):
        install(accounts, beta_stale=True)
        stash = str(accounts / "beta.json")
        live = Path(mod.CLAUDE_CRED).read_bytes()

        def locked():
            assert (accounts / ".lock").exists(), "stash refresh POST ran without the accounts lock"

        endpoint = claude_endpoint(locked)
        # Both contenders pass the first (briefly locked) snapshot with the
        # stale pair before either takes the stash lock, so the loser's only
        # defence is the re-read under that lock.
        both_stale = threading.Barrier(2)
        real_route = mod._claude_refresh_stash

        def route(*args):
            both_stale.wait(WAIT)
            return real_route(*args)

        with mock.patch("urllib.request.urlopen", side_effect=endpoint), \
                mock.patch.object(mod, "_claude_refresh_stash", side_effect=route), \
                mock.patch.object(mod.time, "sleep", side_effect=endpoint.sleep_hook(time.sleep)):
            results, errors = contend(lambda: mod._claude_access_token(stash, {}))
        assert errors == [], errors
        assert results == ["beta-new", "beta-new"], results
        assert endpoint.sent == ["beta-token-refresh"], endpoint.sent
        stored = json.loads(Path(stash).read_text(encoding="utf-8"))["claudeAiOauth"]
        assert stored["refreshToken"] == "beta-refresh-new"
        assert Path(mod.CLAUDE_CRED).read_bytes() == live
        assert not (accounts / ".lock").exists()


def aged_then_touched(*locks):
    """Age every held lock to the epoch; True once the heartbeat renewed each."""
    for lock in locks:
        os.utime(lock, (0, 0))
    renewed = threading.Event()
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if all(os.stat(lock).st_mtime > time.time() - 60 for lock in locks):
            return True
        renewed.wait(0.01)
    return False


def test_a_kimi_refresh_lock_keeps_its_mtime_fresh_across_the_request(tmp):
    mod = _util.load(USAGE_QUERY, "usage_query_kimi_heartbeat")
    mod.KIMI_CRED = os.path.join(tmp, "kimi-code.json")
    mod.LOCK_HEARTBEAT = 0.01
    lock = mod.KIMI_CRED + ".usage-query.lock"
    _write_kimi_cred(mod.KIMI_CRED, {"access_token": "a", "refresh_token": "r",
                                     "expires_at": time.time() - 60})

    def slow_endpoint(req, timeout=None):
        assert aged_then_touched(lock), "held lock aged toward reclaim"
        return _FakeResponse({"access_token": "a2", "refresh_token": "r2", "expires_in": 900})

    with mock.patch("urllib.request.urlopen", side_effect=slow_endpoint), \
            mock.patch.object(mod, "_kimi_client_id", return_value="public-client"):
        assert mod._kimi_access_token() == "a2"
    assert not os.path.exists(lock)


def test_a_claude_stash_refresh_lock_keeps_its_mtime_fresh_across_the_request(tmp):
    with setup(tmp) as (mod, accounts, _):
        install(accounts, beta_stale=True)
        mod.LOCK_HEARTBEAT = 0.01

        endpoint = claude_endpoint()
        endpoint.contended.set()  # one caller: nothing to wait for

        def slow_endpoint(req, timeout=None):
            assert aged_then_touched(accounts / ".lock"), "held accounts lock aged toward reclaim"
            return endpoint(req)

        with mock.patch("urllib.request.urlopen", side_effect=slow_endpoint):
            assert mod._claude_access_token(str(accounts / "beta.json"), {}) == "beta-new"
        assert not (accounts / ".lock").exists()


def claude_code_locks(mod):
    config = Path(mod.CLAUDE_CRED).parent
    return config / ".oauth_refresh.lock", Path(os.path.realpath(config) + ".lock")


LIVE_REFRESH = {"live-token-refresh": {"access_token": "new-access",
                                       "refresh_token": "new-refresh", "expires_in": 3600}}


def posting(http, check):
    """Pass every request to the double, running `check` before a token POST."""
    def request(req, timeout=None):
        if req.full_url == http.mod.CLAUDE_OAUTH_TOKEN_URL:
            check()
        return http(req, timeout)
    return mock.patch("urllib.request.urlopen", side_effect=request)


def test_live_pair_refreshes_only_under_claude_codes_lock(tmp):
    # Every route to the live file: the live path itself, a stash path the
    # snapshot redirects because that stash is live, and no accounts directory.
    for case in ("live", "redirected-stash", "no-accounts-dir"):
        home = os.path.join(tmp, case)
        os.makedirs(home)
        with setup(home, {"new-access": 25}, LIVE_REFRESH) as (mod, accounts, http):
            if case != "no-accounts-dir":
                install(accounts)
            credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
            current, legacy = claude_code_locks(mod)
            path = str(accounts / "alpha.json") if case == "redirected-stash" else mod.CLAUDE_CRED

            def held(current=current, legacy=legacy, case=case):
                assert current.is_dir() and legacy.is_dir(), f"{case}: POST outside Claude Code's lock"

            with posting(http, held):
                assert mod._claude_access_token(path, {}) == "new-access", case
            assert http.calls == [("refresh", "live-token-refresh")], (case, http.calls)
            assert not current.exists() and not legacy.exists(), case
            stored = json.loads(Path(mod.CLAUDE_CRED).read_text(encoding="utf-8"))
            assert stored["claudeAiOauth"]["refreshToken"] == "new-refresh", case


def started(call):
    """Run `call` in a thread; its result or error lands in the returned dict."""
    out = {}

    def worker():
        try:
            out["token"] = call()
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=worker)
    thread.start()
    return thread, out


def waiting_on(mod, release):
    """A time.sleep that reports the first wait and blocks it until released."""
    waited = threading.Event()

    def sleep(seconds):
        if not waited.is_set():
            waited.set()
            assert release.wait(WAIT), "the test never released the lock"
    return waited, mock.patch.object(mod.time, "sleep", side_effect=sleep)


def test_live_refresh_waits_for_claude_code_and_adopts_its_rotation(tmp):
    with setup(tmp, {"cc-rotated": 30}, {}) as (mod, accounts, http):
        install(accounts)
        credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
        current, _ = claude_code_locks(mod)
        current.mkdir()  # Claude Code is mid-refresh
        release = threading.Event()
        waited, sleeping = waiting_on(mod, release)
        with sleeping:
            worker, out = started(lambda: mod._claude_access_token(mod.CLAUDE_CRED, {}))
            assert waited.wait(WAIT), "the live refresh did not wait on Claude Code's lock"
            assert http.calls == [], "POSTed while Claude Code held its lock"
            credentials(Path(mod.CLAUDE_CRED), "cc-rotated")
            current.rmdir()
            release.set()
            worker.join(WAIT)
        assert out == {"token": "cc-rotated"}, out
        assert http.calls == [], "spent the refresh token Claude Code had already rotated"
        assert not current.exists()


def test_live_refresh_waits_while_the_legacy_lock_is_held(tmp):
    with setup(tmp, {"new-access": 25}, LIVE_REFRESH) as (mod, accounts, http):
        install(accounts)
        credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
        current, legacy = claude_code_locks(mod)
        legacy.mkdir()  # an older Claude Code holds only the legacy lock
        release = threading.Event()
        waited, sleeping = waiting_on(mod, release)
        with sleeping:
            worker, out = started(lambda: mod._claude_access_token(mod.CLAUDE_CRED, {}))
            assert waited.wait(WAIT), "the live refresh did not wait on the legacy lock"
            assert http.calls == [] and not current.exists(), "held the new lock while the legacy one was busy"
            legacy.rmdir()
            release.set()
            worker.join(WAIT)
        assert out == {"token": "new-access"}, out
        assert http.calls == [("refresh", "live-token-refresh")]
        assert not current.exists() and not legacy.exists()


def test_a_stale_claude_code_lock_is_reclaimed_without_waiting(tmp):
    with setup(tmp, {"new-access": 25}, LIVE_REFRESH) as (mod, accounts, http):
        install(accounts)
        credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
        current, legacy = claude_code_locks(mod)
        for lock in (current, legacy):
            lock.mkdir()
            old = time.time() - mod.CLAUDE_CODE_LOCK_STALE - 1
            os.utime(lock, (old, old))
        with mock.patch.object(mod.time, "sleep", side_effect=AssertionError("waited on a stale lock")):
            assert mod._claude_access_token(mod.CLAUDE_CRED, {}) == "new-access"
        assert http.calls == [("refresh", "live-token-refresh")]
        assert not current.exists() and not legacy.exists()


def test_a_held_claude_code_lock_times_out_without_a_refresh(tmp):
    with setup(tmp, {}, {}) as (mod, accounts, http):
        install(accounts)
        live = credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
        current, _ = claude_code_locks(mod)
        current.mkdir()
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(mod.time, "sleep", side_effect=sleep):
            try:
                mod._claude_access_token(mod.CLAUDE_CRED, {})
            except TimeoutError as exc:
                assert "Claude Code" in str(exc), exc
            else:
                raise AssertionError("refreshed while Claude Code held its lock")
        assert clock[0] >= mod.CLAUDE_CODE_LOCK_WAIT
        assert http.calls == [] and current.is_dir()
        assert Path(mod.CLAUDE_CRED).read_bytes() == live


def test_claude_codes_lock_keeps_its_mtime_fresh_across_the_request(tmp):
    with setup(tmp, {"new-access": 25}, LIVE_REFRESH) as (mod, accounts, http):
        install(accounts)
        credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
        mod.LOCK_HEARTBEAT = 0.01
        locks = claude_code_locks(mod)

        def renewed():
            assert aged_then_touched(*locks), "Claude Code's lock aged toward its 60 s reclaim"

        with posting(http, renewed):
            assert mod._claude_access_token(mod.CLAUDE_CRED, {}) == "new-access"


LOG_LINE = re.compile(r"^(\S+) pid=(\d+) op=refresh account=(\S+) rt=(\S+) file=(.+) outcome=(.+)$")


def fp(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:10]


def log_lines(accounts, *secrets):
    """Parsed accounts.log lines; no token may appear in the file."""
    text = (accounts / "accounts.log").read_text(encoding="utf-8")
    for secret in secrets:
        assert secret not in text, "a token was logged"
    lines = []
    for raw in text.splitlines():
        match = LOG_LINE.match(raw)
        assert match, raw
        when, pid, account, sent, destination, outcome = match.groups()
        stamp = datetime.fromisoformat(when)
        assert stamp.utcoffset() == timezone.utc.utcoffset(None), when
        assert abs(stamp.timestamp() - time.time()) < 60, when
        assert int(pid) == os.getpid()
        lines.append((account, sent, destination, outcome))
    return lines


def test_every_claude_refresh_attempt_is_logged_by_fingerprint(tmp):
    timeout = TimeoutError("timed out")
    cases = (
        ("live-ok", "alpha", LIVE_REFRESH["live-token-refresh"], "ok rt_new=" + fp("new-refresh")),
        ("stash-ok", "beta", {"access_token": "new-access", "refresh_token": "new-refresh",
                              "expires_in": 3600}, "ok rt_new=" + fp("new-refresh")),
        ("live-http", "alpha", urllib.error.HTTPError("u", 400, "Bad Request", {},
                                                      io.BytesIO(b'{"error": "invalid_grant"}')), "http 400"),
        ("live-timeout", "alpha", timeout, "unknown"),
        ("live-reset", "alpha", urllib.error.URLError(ConnectionResetError("reset")), "unknown"),
    )
    for case, name, answer, outcome in cases:
        home = os.path.join(tmp, case)
        os.makedirs(home)
        sent = "live-token-refresh" if name == "alpha" else "beta-token-refresh"
        with setup(home, {"new-access": 25}, {sent: answer}) as (mod, accounts, _):
            install(accounts, beta_stale=True)
            credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
            path = mod.CLAUDE_CRED if name == "alpha" else str(accounts / "beta.json")
            try:
                mod._claude_access_token(path, {})
            except Exception:  # noqa: BLE001  the failing cases raise; the log is the subject
                assert not outcome.startswith("ok"), case
            got = log_lines(accounts, "live-token", "beta-token", "new-refresh", "new-access")
            assert got == [(name, fp(sent), path, outcome)], (case, got)


def test_no_accounts_directory_means_no_refresh_log(tmp):
    with setup(tmp, {"new-access": 25}, LIVE_REFRESH) as (mod, accounts, _):
        credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
        assert mod._claude_access_token(mod.CLAUDE_CRED, {}) == "new-access"
        assert not accounts.exists(), "the log created the accounts directory"


if __name__ == "__main__":
    sys.exit(_util.runner(_util.collect(globals()), "refreshcoord_"))
