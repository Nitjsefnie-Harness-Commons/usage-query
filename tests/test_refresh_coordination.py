"""Concurrent refreshes of one single-use refresh token spend it once."""
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
from unittest import mock

import _util
from test_usage_query_oauth import _FakeResponse, _fake_jwt, _wham_payload, _write_codex_auth, _write_kimi_cred

USAGE_QUERY = os.path.join(_util.SCRIPTS, "query.py")
WAIT = 10  # failure guard only: every wait below ends on an event


class SingleUseEndpoint:
    """A token endpoint whose refresh token works once.

    The first request is held until the second contender either reaches the
    endpoint too or waits on the refresh lock, so both orders are forced
    without sleeping: an uncoordinated pair always races.
    """

    def __init__(self, valid, rotated, token_of):
        self.valid = {valid}
        self.rotated = rotated
        self.token_of = token_of
        self.sent = []
        self.contended = threading.Event()
        self.guard = threading.Lock()

    def __call__(self, req, timeout=None):
        token = self.token_of(req)
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


def test_a_refresh_lock_held_across_the_request_keeps_its_mtime_fresh(tmp):
    mod = _util.load(USAGE_QUERY, "usage_query_kimi_heartbeat")
    mod.KIMI_CRED = os.path.join(tmp, "kimi-code.json")
    mod.LOCK_HEARTBEAT = 0.01
    lock = mod.KIMI_CRED + ".usage-query.lock"
    _write_kimi_cred(mod.KIMI_CRED, {"access_token": "a", "refresh_token": "r",
                                     "expires_at": time.time() - 60})
    touched = threading.Event()

    def slow_endpoint(req, timeout=None):
        os.utime(lock, (0, 0))
        deadline = time.monotonic() + WAIT
        while os.stat(lock).st_mtime < 1000 and time.monotonic() < deadline:
            touched.wait(0.01)
        assert os.stat(lock).st_mtime > time.time() - 60, "held lock aged toward reclaim"
        return _FakeResponse({"access_token": "a2", "refresh_token": "r2", "expires_in": 900})

    with mock.patch("urllib.request.urlopen", side_effect=slow_endpoint), \
            mock.patch.object(mod, "_kimi_client_id", return_value="public-client"):
        assert mod._kimi_access_token() == "a2"
    assert not os.path.exists(lock)


if __name__ == "__main__":
    sys.exit(_util.runner(_util.collect(globals()), "refreshcoord_"))
