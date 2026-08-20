#!/usr/bin/env python3
"""Credential safety and Codex app-server coverage for usage_query.py."""
import io
import json
import os
import queue
import stat
import sys
import time
import urllib.parse
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402


USAGE_QUERY = os.path.join(_util.SCRIPTS, "query.py")


class _FakeResponse:
    """Minimal urlopen() context manager returning a canned JSON body."""

    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._raw


def _write_kimi_cred(path, cred):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cred, f)
    os.chmod(path, 0o600)


def test_stale_kimi_token_refreshes_and_persists_the_rotated_pair(tmp):
    """Kimi's access token lives ~15 min, so a stale one is the normal case; the
    refresh grant rotates the single-use refresh_token, so both halves of the new
    pair must land on disk or the next refresh (ours or kimi's) hits invalid_grant."""
    mod = _util.load(USAGE_QUERY, "usage_query_kimi_refresh")
    cred = os.path.join(tmp, "kimi-code.json")
    _write_kimi_cred(cred, {
        "access_token": "access-token-old",
        "refresh_token": "refresh-token-old",
        "expires_at": time.time() - 60,
        "keep_me": "untouched",
    })
    mod.KIMI_CRED = cred
    sent = {}

    def fake_urlopen(req, timeout=None):
        del timeout
        sent["url"] = req.full_url
        sent["body"] = dict(urllib.parse.parse_qsl(req.data.decode("utf-8")))
        return _FakeResponse({"access_token": "access-token-new",
                              "refresh_token": "refresh-token-new",
                              "expires_in": 900,
                              "token_type": "Bearer"})

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
            mock.patch.object(mod, "_kimi_client_id", return_value="public-client"):
        assert mod._kimi_access_token() == "access-token-new"

    assert sent["url"] == mod.KIMI_OAUTH_TOKEN_URL
    assert sent["body"] == {"client_id": "public-client",
                            "grant_type": "refresh_token",
                            "refresh_token": "refresh-token-old"}
    with open(cred, encoding="utf-8") as f:
        stored = json.load(f)
    assert stored["access_token"] == "access-token-new"
    assert stored["refresh_token"] == "refresh-token-new", (
        "the consumed refresh_token was left on disk")
    assert stored["expires_at"] > time.time() + 800
    assert stored["keep_me"] == "untouched"
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(cred).st_mode) == 0o600
    assert os.listdir(tmp) == ["kimi-code.json"], "temp file left beside the credential"


def test_valid_kimi_token_is_used_without_a_refresh(tmp):
    mod = _util.load(USAGE_QUERY, "usage_query_kimi_no_refresh")
    cred = os.path.join(tmp, "kimi-code.json")
    original = {
        "access_token": "access-token-live",
        "refresh_token": "refresh-token-unused",
        "expires_at": time.time() + 3600,
    }
    _write_kimi_cred(cred, original)
    mod.KIMI_CRED = cred
    with mock.patch("urllib.request.urlopen") as request:
        assert mod._kimi_access_token() == "access-token-live"
    request.assert_not_called()
    with open(cred, encoding="utf-8") as f:
        assert json.load(f) == original


def test_kimi_client_id_is_read_from_the_installed_cli(tmp):
    """The client id is public and read live from kimi_cli so it self-heals when
    upstream rotates it; an unreadable install falls back to the pinned literal."""
    mod = _util.load(USAGE_QUERY, "usage_query_kimi_client_id")
    venv = os.path.join(tmp, "venv")
    pkg = os.path.join(venv, "lib", "python3.13", "site-packages",
                       "kimi_cli", "auth")
    os.makedirs(pkg)
    os.makedirs(os.path.join(venv, "bin"))
    launcher = os.path.join(venv, "bin", "kimi")
    with open(launcher, "w", encoding="utf-8") as f:
        f.write("#!%s\nprint('kimi')\n" % os.path.join(venv, "bin", "python"))
    with open(os.path.join(pkg, "oauth.py"), "w", encoding="utf-8") as f:
        f.write('KIMI_CODE_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\n')
    with mock.patch.object(mod.shutil, "which", return_value=launcher):
        assert mod._kimi_client_id() == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with mock.patch.object(mod.shutil, "which", return_value=None), \
            mock.patch.object(mod.glob, "glob", return_value=[]):
        assert mod._kimi_client_id() == mod.KIMI_OAUTH_CLIENT_ID_FALLBACK


def _codex_payload(now=None):
    now = int(time.time() if now is None else now)
    default = {
        "limitId": "codex", "limitName": None, "planType": "pro",
        "primary": {"usedPercent": 20, "windowDurationMins": 300,
                    "resetsAt": now + 4 * 3600},
        "secondary": {"usedPercent": 27, "windowDurationMins": 10080,
                      "resetsAt": now + 6 * 86400},
    }
    spark = {
        "limitId": "codex_bengalfox", "limitName": "GPT-Codex-Spark",
        "primary": {"usedPercent": 3, "windowDurationMins": 10080,
                    "resetsAt": now + 6 * 86400},
        "secondary": None,
    }
    return {
        "rateLimits": default,
        "rateLimitsByLimitId": {"codex": default, "codex_bengalfox": spark},
        "rateLimitResetCredits": {"availableCount": 2, "credits": []},
    }


def test_codex_normalizes_default_and_model_scoped_windows(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_codex_normalize")
    got = mod._normalize_codex(_codex_payload())
    assert got["five_hour"]["pct"] == 20
    assert got["weekly"]["pct"] == 27
    assert got["_plan_type"] == "pro"
    assert got["_reset_credits_available"] == 2
    assert len(got["_scoped"]) == 1
    assert got["_scoped"][0]["label"] == "7d:GPT-Codex-Spark"
    assert got["_scoped"][0]["pct"] == 3


def test_codex_app_server_handshake_and_rate_limit_rpc(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_codex_rpc")
    payload = _codex_payload()
    incoming = queue.Queue()
    sent = []

    class FakeStdout:
        def __iter__(self):
            return self

        def __next__(self):
            item = incoming.get(timeout=1)
            if item is None:
                raise StopIteration
            return item

    class FakeStdin:
        def write(self, raw):
            message = json.loads(raw)
            sent.append(message)
            if message.get("id") == 0:
                incoming.put(json.dumps({"id": 0, "result": {}}) + "\n")
            elif message.get("id") == 1:
                incoming.put(json.dumps({"id": 1, "result": payload}) + "\n")

        def flush(self):
            pass

        def close(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            incoming.put(None)

        def kill(self):
            self.terminate()

        def wait(self, timeout=None):
            del timeout
            self.returncode = 0
            return 0

    fake = FakeProcess()
    with mock.patch.object(mod.subprocess, "Popen", return_value=fake) as popen:
        assert mod._codex_rpc_rate_limits() == payload
    assert popen.call_args.args[0] == [mod.CODEX_BIN, "app-server", "--stdio"]
    assert [message.get("method") for message in sent] == [
        "initialize", "initialized", "account/rateLimits/read"]


def test_codex_cache_avoids_rpc_and_stale_payload_survives_failure(tmp):
    mod = _util.load(USAGE_QUERY, "usage_query_codex_cache")
    mod.CODEX_CACHE = os.path.join(tmp, "codex-usage.json")
    payload = _codex_payload()
    with open(mod.CODEX_CACHE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time(), "data": payload}, f)
    with mock.patch.object(mod, "_codex_rpc_rate_limits") as rpc:
        assert mod.query_codex()["weekly"]["pct"] == 27
    rpc.assert_not_called()

    with open(mod.CODEX_CACHE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time() - 90, "data": payload}, f)
    with mock.patch.object(mod, "_codex_rpc_rate_limits",
                           side_effect=RuntimeError("offline")):
        got = mod.query_codex()
    assert got["weekly"]["pct"] == 27
    assert 89 <= got["_stale_age"] <= 91


def test_codex_flag_queries_only_codex(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_codex_cli")
    with mock.patch.object(mod, "query_claude") as claude, \
            mock.patch.object(mod, "query_kimi") as kimi, \
            mock.patch.object(mod, "query_codex",
                              return_value=mod._normalize_codex(_codex_payload())):
        output = io.StringIO()
        with redirect_stdout(output):
            rc = mod.main(["--codex", "--json"])
    assert rc == 0
    assert set(json.loads(output.getvalue())["usage"]) == {"codex"}
    claude.assert_not_called()
    kimi.assert_not_called()


def main():
    return _util.runner(_util.collect(globals()), "usagequeryoauth_")


if __name__ == "__main__":
    raise SystemExit(main())
