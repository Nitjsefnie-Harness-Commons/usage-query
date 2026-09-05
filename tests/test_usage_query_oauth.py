#!/usr/bin/env python3
"""Credential safety and Codex rate-limit coverage for usage_query.py."""
import base64
import io
import json
import os
import stat
import sys
import time
import urllib.error
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
    """The account/rateLimits/read response shape, which is what the cache holds
    and what _normalize_codex consumes."""
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


def _wham_payload(now=None):
    """The backend usage payload, in the snake_case shape the endpoint really
    returns (field-for-field as observed live, with the timestamps relative)."""
    now = int(time.time() if now is None else now)
    return {
        "plan_type": "pro",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {"used_percent": 95,
                               "limit_window_seconds": 604800,
                               "reset_after_seconds": 401797,
                               "reset_at": now + 401797},
            "secondary_window": None,
        },
        "additional_rate_limits": [{
            "limit_name": "GPT-Codex-Spark",
            "metered_feature": "codex_bengalfox",
            "rate_limit": {
                "primary_window": {"used_percent": 0,
                                   "limit_window_seconds": 18000,
                                   "reset_at": now + 18000},
                "secondary_window": {"used_percent": 4,
                                     "limit_window_seconds": 604800,
                                     "reset_at": now + 604800},
            },
        }],
        "rate_limit_reset_credits": {"available_count": 1,
                                     "applicable_available_count": 1},
    }


def _fake_jwt(exp):
    """A syntactically real, cryptographically meaningless JWT. Nothing here is
    a credential: the only claim the tool reads is `exp`."""
    def seg(obj):
        raw = json.dumps(obj).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.%s" % (seg({"alg": "RS256", "typ": "JWT"}),
                         seg({"exp": int(exp)}), "signature-placeholder")


def _write_codex_auth(path, exp_delta, **overrides):
    """A ~/.codex/auth.json in the shape codex writes (read off a live file)."""
    auth = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": _fake_jwt(time.time() + 86400),
            "access_token": _fake_jwt(time.time() + exp_delta),
            "refresh_token": "refresh-token-old",
            "account_id": "account-123",
        },
        "last_refresh": "2026-01-01T00:00:00.000000Z",
        "keep_me": "untouched",
    }
    auth.update(overrides)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(auth, f, indent=2)
    os.chmod(path, 0o600)
    return auth


def _capture_get(seen, payload):
    def fake(url, headers):
        seen["url"] = url
        seen["headers"] = headers
        return payload
    return fake


def test_codex_never_reaches_for_the_codex_cli(tmp):
    """The whole point of reading auth.json directly: no `codex app-server` is
    started, so the status line stops paying a CLI boot (and, on Windows, a
    console window) once a minute."""
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_codex_no_subprocess")
    assert not hasattr(mod, "subprocess"), "the module can still spawn processes"
    with open(USAGE_QUERY, encoding="utf-8") as f:
        source = f.read()
    # Prose about the app-server is fine; a way to start one is not.
    assert "Popen" not in source
    assert "--stdio" not in source
    assert "CODEX_BIN" not in source


def test_codex_maps_the_backend_payload_into_the_rpc_shape(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_codex_shape")
    now = int(time.time())
    got = mod._codex_rpc_shape(_wham_payload(now))
    assert got["rateLimits"]["limitId"] == "codex"
    assert got["rateLimits"]["limitName"] is None
    assert got["rateLimits"]["planType"] == "pro"
    assert got["rateLimits"]["primary"] == {"usedPercent": 95.0,
                                            "windowDurationMins": 10080,
                                            "resetsAt": now + 401797}
    assert got["rateLimits"]["secondary"] is None
    spark = got["rateLimitsByLimitId"]["codex_bengalfox"]
    assert spark["limitName"] == "GPT-Codex-Spark"
    assert spark["primary"]["windowDurationMins"] == 300
    assert spark["secondary"]["windowDurationMins"] == 10080
    assert got["rateLimitResetCredits"]["availableCount"] == 1
    # ... and the normalizer downstream still reads it, unchanged.
    out = mod._normalize_codex(got)
    assert out["weekly"]["pct"] == 95
    assert out["_plan_type"] == "pro"
    assert out["_reset_credits_available"] == 1
    assert [row["label"] for row in out["_scoped"]] == [
        "5h:GPT-Codex-Spark", "7d:GPT-Codex-Spark"]


def test_codex_valid_token_is_used_without_a_refresh(tmp):
    mod = _util.load(USAGE_QUERY, "usage_query_codex_live_token")
    auth = os.path.join(tmp, "auth.json")
    original = _write_codex_auth(auth, 86400)
    mod.CODEX_AUTH = auth
    seen = {}
    with mock.patch("urllib.request.urlopen") as opened, \
            mock.patch.object(mod, "_get_retry",
                              side_effect=_capture_get(seen, _wham_payload())):
        got = mod._codex_rate_limits()
    opened.assert_not_called()
    assert seen["url"] == "https://chatgpt.com/backend-api/wham/usage"
    assert seen["headers"]["Authorization"] == (
        "Bearer " + original["tokens"]["access_token"])
    assert seen["headers"]["ChatGPT-Account-Id"] == "account-123"
    assert got["rateLimits"]["primary"]["usedPercent"] == 95.0
    with open(auth, encoding="utf-8") as f:
        assert json.load(f) == original, "a live token was rotated for nothing"


def test_codex_stale_token_refreshes_and_persists_the_rotated_set(tmp):
    """The Codex access token lives ~10 days, so a machine that has not run
    codex for a fortnight has a stale one. OpenAI rotates the refresh_token, so
    the rotated set must land on disk or codex's own next refresh fails."""
    mod = _util.load(USAGE_QUERY, "usage_query_codex_refresh")
    auth = os.path.join(tmp, "auth.json")
    _write_codex_auth(auth, -60)
    mod.CODEX_AUTH = auth
    sent = {}

    def fake_urlopen(req, timeout=None):
        del timeout
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"id_token": "id-token-new",
                              "access_token": "access-token-new",
                              "refresh_token": "refresh-token-new"})

    seen = {}
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
            mock.patch.object(mod, "_get_retry",
                              side_effect=_capture_get(seen, _wham_payload())):
        got = mod._codex_rate_limits()

    assert sent["url"] == mod.CODEX_OAUTH_TOKEN_URL
    assert sent["body"] == {"client_id": mod.CODEX_OAUTH_CLIENT_ID,
                            "grant_type": "refresh_token",
                            "refresh_token": "refresh-token-old"}
    assert seen["headers"]["Authorization"] == "Bearer access-token-new"
    assert got["rateLimits"]["planType"] == "pro"
    with open(auth, encoding="utf-8") as f:
        stored = json.load(f)
    assert stored["tokens"]["access_token"] == "access-token-new"
    assert stored["tokens"]["refresh_token"] == "refresh-token-new", (
        "the consumed refresh_token was left on disk")
    assert stored["tokens"]["id_token"] == "id-token-new"
    assert stored["tokens"]["account_id"] == "account-123"
    assert stored["auth_mode"] == "chatgpt"
    assert stored["keep_me"] == "untouched"
    assert stored["last_refresh"] > "2026-01-01T00:00:00.000000Z"
    assert stored["last_refresh"].endswith("Z")
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(auth).st_mode) == 0o600
    assert os.listdir(tmp) == ["auth.json"], "temp file left beside the credential"


def test_codex_401_on_a_token_that_looked_live_refreshes_once(tmp):
    """`exp` says live, the backend says 401 -- a revoked or server-rotated
    token. One refresh, one retry, and no loop."""
    mod = _util.load(USAGE_QUERY, "usage_query_codex_401")
    auth = os.path.join(tmp, "auth.json")
    _write_codex_auth(auth, 86400)
    mod.CODEX_AUTH = auth
    calls = []

    def fake_urlopen(req, timeout=None):
        del timeout, req
        return _FakeResponse({"access_token": "access-token-new",
                              "refresh_token": "refresh-token-new"})

    def flaky(url, headers):
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        return _wham_payload()

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
            mock.patch.object(mod, "_get_retry", side_effect=flaky):
        got = mod._codex_rate_limits()
    assert len(calls) == 2
    assert calls[1] == "Bearer access-token-new"
    assert got["rateLimits"]["limitId"] == "codex"

    # A second 401, after the refresh, is a real failure and is not retried.
    _write_codex_auth(auth, 86400)
    always_401 = mock.Mock(side_effect=urllib.error.HTTPError(
        "u", 401, "Unauthorized", {}, None))
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
            mock.patch.object(mod, "_get_retry", always_401):
        try:
            mod._codex_rate_limits()
        except RuntimeError as exc:
            assert "HTTP 401" in str(exc)
        else:
            raise AssertionError("an endlessly-401 backend was accepted")
    assert always_401.call_count == 2


def test_codex_failed_refresh_reports_and_leaves_the_credential_alone(tmp):
    mod = _util.load(USAGE_QUERY, "usage_query_codex_refresh_fail")
    auth = os.path.join(tmp, "auth.json")
    original = _write_codex_auth(auth, -60)
    mod.CODEX_AUTH = auth
    body = io.BytesIO(b'{"error": {"code": "refresh_token_expired"}}')
    with mock.patch("urllib.request.urlopen",
                    side_effect=urllib.error.HTTPError(
                        mod.CODEX_OAUTH_TOKEN_URL, 400, "Bad Request", {}, body)), \
            mock.patch.object(mod, "_get_retry") as fetch:
        try:
            mod._codex_rate_limits()
        except RuntimeError as exc:
            assert "HTTP 400" in str(exc)
            assert "codex login" in str(exc)
        else:
            raise AssertionError("a dead refresh token produced a result")
    fetch.assert_not_called()
    with open(auth, encoding="utf-8") as f:
        assert json.load(f) == original, "a failed refresh rewrote auth.json"
    assert os.listdir(tmp) == ["auth.json"]


def test_codex_without_credentials_is_unconfigured_not_broken(tmp):
    mod = _util.load(USAGE_QUERY, "usage_query_codex_absent")
    mod.CODEX_AUTH = os.path.join(tmp, "nothing", "auth.json")
    try:
        mod._codex_rate_limits()
    except mod.ProviderNotConfigured as exc:
        assert "not configured on this machine" in str(exc)
    else:
        raise AssertionError("a machine without Codex reported a Codex failure")

    # An API-key login HAS credentials, but no subscription quota windows.
    auth = os.path.join(tmp, "auth.json")
    with open(auth, "w", encoding="utf-8") as f:
        json.dump({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-placeholder",
                   "tokens": None}, f)
    mod.CODEX_AUTH = auth
    try:
        mod._codex_rate_limits()
    except RuntimeError as exc:
        assert "API-key logins" in str(exc)
    else:
        raise AssertionError("an API-key login reported quota windows")


def test_codex_usage_url_follows_the_configured_base(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_codex_url")
    assert mod._codex_usage_url() == (
        "https://chatgpt.com/backend-api/wham/usage")
    mod.CODEX_BASE_URL = "https://chatgpt.com/"
    assert mod._codex_usage_url() == (
        "https://chatgpt.com/backend-api/wham/usage")
    mod.CODEX_BASE_URL = "https://codex.example.test"
    assert mod._codex_usage_url() == (
        "https://codex.example.test/api/codex/usage")


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


def test_codex_cache_avoids_the_fetch_and_stale_payload_survives_failure(tmp):
    """Codex holds its own, much longer TTL: a 30 s one expired in lockstep with
    the status line's 30 s refresh, so essentially every refresh was a miss."""
    mod = _util.load(USAGE_QUERY, "usage_query_codex_cache")
    mod.CODEX_CACHE = os.path.join(tmp, "codex-usage.json")
    assert mod.CODEX_CACHE_TTL >= 600, "a Codex miss is too expensive to court"
    payload = _codex_payload()
    fresh = mod.CODEX_CACHE_TTL - 60
    with open(mod.CODEX_CACHE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time() - fresh, "data": payload}, f)
    with mock.patch.object(mod, "_codex_rate_limits") as fetch:
        assert mod.query_codex()["weekly"]["pct"] == 27
    fetch.assert_not_called()

    stale = mod.CODEX_CACHE_TTL + 90
    with open(mod.CODEX_CACHE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time() - stale, "data": payload}, f)
    with mock.patch.object(mod, "_codex_rate_limits",
                           side_effect=RuntimeError("offline")):
        got = mod.query_codex()
    assert got["weekly"]["pct"] == 27
    assert stale - 1 <= got["_stale_age"] <= stale + 1


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


def test_codex_keeps_the_detail_of_every_spendable_banked_reset(tmp):
    """A banked reset is redeemed BY HAND, so the row a human acts on has to
    name which credit it is and when it dies. Only `available` credits are
    offered -- a redeemed one cannot be spent again -- and they are ordered by
    expiry so the most perishable is read first."""
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_codex_credits")
    now = int(time.time())
    payload = _codex_payload(now)
    payload["rateLimitResetCredits"] = {
        "availableCount": 2,
        "credits": [
            {"id": "cred_late", "status": "available", "title": "Full reset",
             "expiresAt": now + 30 * 86400},
            {"id": "cred_spent", "status": "redeemed", "title": "Full reset",
             "expiresAt": now + 86400},
            {"id": "cred_soon", "status": "available", "title": "Half reset",
             # Two days plus a margin: the duration is computed against a
             # clock that has moved on since `now`, and a flat 2d would
             # render as 1d23h on any run slow enough to cross a second.
             "expiresAt": now + 2 * 86400 + 300},
        ],
    }
    got = mod._normalize_codex(payload)
    assert got["_reset_credits_available"] == 2
    assert [c["id"] for c in got["_reset_credits"]] == ["cred_soon", "cred_late"]
    assert got["_reset_credits"][0]["title"] == "Half reset"
    assert got["_reset_credits"][0]["expires_in"] == "2d0h"


def test_codex_survives_a_reset_credit_summary_with_no_detail(tmp):
    """The app-server omits per-credit detail on its periodic refresh, so the
    count alone must still normalize rather than raise."""
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_codex_credits_bare")
    payload = _codex_payload()
    payload["rateLimitResetCredits"] = {"availableCount": 1}
    got = mod._normalize_codex(payload)
    assert got["_reset_credits_available"] == 1
    assert got.get("_reset_credits") in (None, [])


def main():
    return _util.runner(_util.collect(globals()), "usagequeryoauth_")


if __name__ == "__main__":
    raise SystemExit(main())
