"""Installed Claude accounts, token routing, persistence and cache isolation."""
import io
import json
import os
import stat
import sys
import time
import urllib.error
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _util


class Response:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.raw


def credentials(path, token, stale=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": token,
                          "refreshToken": token + "-refresh",
                          "expiresAt": (time.time() + (-60 if stale else 3600)) * 1000,
                          "keep": "value"},
        "keep": "outer",
    }), encoding="utf-8")
    return path.read_bytes()


class HTTP:  # pylint: disable=too-few-public-methods
    """Allow only explicitly modelled endpoint/token pairs."""

    def __init__(self, mod, tokens=None, refreshes=None):
        self.mod = mod
        self.tokens = tokens if tokens is not None else {"live-token": 10}
        self.refreshes = refreshes or {}
        self.calls = []

    def __call__(self, req, timeout=None):
        assert timeout == self.mod.TIMEOUT
        if req.full_url == self.mod.CLAUDE_URL:
            assert req.get_method() == "GET"
            token = req.get_header("Authorization")
            self.calls.append(("usage", token))
            assert token.startswith("Bearer "), token
            token = token.removeprefix("Bearer ")
            assert token in self.tokens, "unexpected usage token: " + token
            pct = self.tokens[token]
            return Response({
                "five_hour": {"utilization": pct, "resets_at": None},
                "seven_day": {"utilization": pct + 1, "resets_at": None},
            })
        if req.full_url == self.mod.CLAUDE_OAUTH_TOKEN_URL:
            assert req.get_method() == "POST"
            body = json.loads(req.data)
            token = body["refresh_token"]
            self.calls.append(("refresh", token))
            assert body == {"grant_type": "refresh_token", "refresh_token": token,
                            "client_id": self.mod.CLAUDE_OAUTH_CLIENT_ID}
            assert token in self.refreshes, "unexpected refresh token: " + token
            result = self.refreshes[token]
            if isinstance(result, Exception):
                raise result
            return Response(result)
        raise AssertionError("unexpected URL: " + req.full_url)


@contextmanager
def setup(tmp, tokens=None, refreshes=None):
    with mock.patch.dict(os.environ, {"HOME": tmp, "USERPROFILE": tmp}):
        mod = _util.load(_util.script("query.py"))
        live = Path(mod.CLAUDE_CRED)
        credentials(live, "live-token")
        mod.CACHE = str(Path(tmp) / "live-cache")
        http = HTTP(mod, tokens, refreshes)
        with mock.patch("urllib.request.urlopen", side_effect=http):
            yield mod, live.parent / "accounts", http


def install(accounts, marker="alpha", beta_stale=False):
    credentials(accounts / "alpha.json", "unused-live-stash", stale=True)
    credentials(accounts / "beta.json", "beta-token", stale=beta_stale)
    if marker is not None:
        (accounts / "live").write_text(marker + "\n", encoding="utf-8")


def run(mod, *flags):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = mod.main(["--claude", *flags])
        except Exception as exc:
            raise AssertionError(f"CLI raised {type(exc).__name__}: {exc}") from exc
    return rc, out.getvalue(), err.getvalue()


def assert_legacy(mod):
    rc, raw, err = run(mod, "--json")
    got = json.loads(raw)
    assert rc == 0 and err == "", (rc, raw, err)
    assert set(got) == {"usage", "errors", "unconfigured"}, got
    assert set(got["usage"]) == {"claude"}
    assert got["usage"]["claude"]["five_hour"]["pct"] == 10
    assert got["errors"] == {}
    rc, text, err = run(mod)
    assert rc == 0 and err == ""
    assert "Claude alpha" not in text and "Claude beta" not in text
    assert text.splitlines()[1].startswith("Claude ")


def test_no_accounts_directory_keeps_legacy_output(tmp):
    with setup(tmp) as (mod, accounts, http):
        assert not accounts.exists()
        assert_legacy(mod)
        assert http.calls == [("usage", "Bearer live-token")]


def test_empty_accounts_directory_does_not_read_marker(tmp):
    with setup(tmp) as (mod, accounts, _):
        accounts.mkdir()
        real_open = open

        def opened(path, *args, **kwargs):
            assert Path(path) != accounts / "live", "empty accounts read live marker"
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=opened):
            assert_legacy(mod)


def test_missing_marker_keeps_legacy_output(tmp):
    with setup(tmp) as (mod, accounts, http):
        install(accounts, marker=None)
        assert_legacy(mod)
        assert http.calls == [("usage", "Bearer live-token")]


def test_unknown_marker_keeps_legacy_output(tmp):
    with setup(tmp) as (mod, accounts, http):
        install(accounts, marker="absent")
        assert_legacy(mod)
        assert http.calls == [("usage", "Bearer live-token")]


def test_fresh_inactive_account_uses_its_token_and_live_is_reused(tmp):
    with setup(tmp, {"live-token": 10, "beta-token": 40}) as (mod, accounts, http):
        install(accounts)
        (accounts / "ignored.txt").write_text("not json", encoding="utf-8")
        (accounts / "ignored.json").mkdir()
        rc, raw, err = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and err == "", (rc, raw, err)
        assert set(got["usage"]) == {"claude"}
        assert set(got["claude_accounts"]) == {"alpha", "beta"}
        assert got["claude_accounts"]["alpha"] == {
            "live": True, **got["usage"]["claude"]}
        beta = got["claude_accounts"]["beta"]
        assert set(beta) == {"live", "five_hour", "weekly"}
        assert beta["live"] is False
        assert beta["five_hour"] == {
            "pct": 40.0, "resets_at": "no reset scheduled (window inactive)",
            "resets_in": "—", "pace_pct": None, "recover_in": None}
        assert beta["weekly"]["pct"] == 41
        assert http.calls == [("usage", "Bearer live-token"),
                              ("usage", "Bearer beta-token")]


def test_null_window_on_inactive_account_reads_as_inactive(tmp):
    inactive = {"pct": 0.0, "resets_at": "no reset scheduled (window inactive)",
                "resets_in": "—", "pace_pct": None, "recover_in": None}
    for src, key, other, other_pct in (("five_hour", "five_hour", "weekly", 41),
                                       ("seven_day", "weekly", "five_hour", 40)):
        with setup(os.path.join(tmp, src),
                   {"live-token": 10, "beta-token": 40}) as (mod, accounts, http):
            install(accounts)

            def request(req, timeout=None, src=src, http=http):
                payload = json.loads(http(req, timeout).raw)
                if req.get_header("Authorization") == "Bearer beta-token":
                    payload[src] = None
                return Response(payload)

            with mock.patch("urllib.request.urlopen", side_effect=request):
                rc, raw, err = run(mod, "--json")
            got = json.loads(raw)
            assert rc == 0 and err == "" and got["errors"] == {}, (src, rc, raw, err)
            beta = got["claude_accounts"]["beta"]
            assert beta[key] == inactive, (src, beta)
            assert beta[other]["pct"] == other_pct, (src, beta)


@contextmanager
def scoped_accounts(tmp):
    with setup(tmp, {"live-token": 10, "beta-token": 40}) as (mod, accounts, http):
        install(accounts)

        def request(req, timeout=None):
            payload = json.loads(http(req, timeout).raw)
            scopes = (("Model One", 23), ("Model Two", 31))
            if req.get_header("Authorization") == "Bearer beta-token":
                scopes = (("Model Two", 73), ("Model One", 39))
            payload["limits"] = [
                {"group": "weekly", "scope": {"model": {"display_name": model}},
                 "percent": pct, "resets_at": None} for model, pct in scopes]
            return Response(payload)

        with mock.patch("urllib.request.urlopen", side_effect=request):
            yield mod, http


def test_inactive_scoped_weekly_limits_are_in_account_json(tmp):
    with scoped_accounts(tmp) as (mod, http):
        rc, raw, err = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and err == "", (rc, raw, err)
        beta = got["claude_accounts"]["beta"]
        assert beta.get("_scoped") == [
            {"label": "7d:Model Two", "pct": 73.0,
             "resets_at": "no reset scheduled (window inactive)", "resets_in": "—",
             "pace_pct": None, "recover_in": None},
            {"label": "7d:Model One", "pct": 39.0,
             "resets_at": "no reset scheduled (window inactive)", "resets_in": "—",
             "pace_pct": None, "recover_in": None},
        ], "inactive scoped limits were dropped or changed"
        assert got["claude_accounts"]["alpha"].get("_scoped") == got["usage"]["claude"]["_scoped"]
        assert [row["pct"] for row in got["usage"]["claude"]["_scoped"]] == [23, 31]
        assert set(got["usage"]) == {"claude"}
        assert http.calls == [("usage", "Bearer live-token"), ("usage", "Bearer beta-token")]


def test_inactive_scoped_rows_follow_account_windows_in_text(tmp):
    with scoped_accounts(tmp) as (mod, http):
        rc, text, err = run(mod)
        assert rc == 0 and err == "", (rc, text, err)
        lines = text.splitlines()
        scoped = [line for line in lines if "7d:Model " in line]
        assert [line.split()[:3] for line in scoped] == [
            ["7d:Model", "One", "23%"], ["7d:Model", "Two", "31%"],
            ["7d:Model", "Two", "73%"], ["7d:Model", "One", "39%"],
        ], "inactive scoped text rows were dropped or reordered"
        assert lines[5].startswith("Claude beta")
        assert lines[6].split()[0] == "7d"
        assert lines[7:9] == scoped[2:]
        assert scoped[0].index("7d:Model One") == scoped[3].index("7d:Model One")
        assert scoped[1].index("7d:Model Two") == scoped[2].index("7d:Model Two")
        assert scoped[0].index("23%") == scoped[2].index("73%")
        assert http.calls == [("usage", "Bearer live-token"), ("usage", "Bearer beta-token")]


def test_stale_inactive_account_refreshes_only_its_stash(tmp):
    with setup(tmp, {"live-token": 10, "beta-new": 50}, {
        "beta-token-refresh": {"access_token": "beta-new",
                               "refresh_token": "beta-refresh-new", "expires_in": 3600},
    }) as (mod, accounts, http):
        install(accounts, beta_stale=True)
        live = Path(mod.CLAUDE_CRED).read_bytes()
        alpha = (accounts / "alpha.json").read_bytes()
        rc, raw, err = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and err == "", (rc, raw, err)
        assert got["claude_accounts"]["beta"]["five_hour"]["pct"] == 50
        assert http.calls == [("usage", "Bearer live-token"),
                              ("refresh", "beta-token-refresh"), ("usage", "Bearer beta-new")]
        assert Path(mod.CLAUDE_CRED).read_bytes() == live, "live credentials changed"
        assert (accounts / "alpha.json").read_bytes() == alpha
        beta = json.loads((accounts / "beta.json").read_text(encoding="utf-8"))
        assert beta["claudeAiOauth"]["accessToken"] == "beta-new"
        assert beta["claudeAiOauth"]["refreshToken"] == "beta-refresh-new"
        assert beta["claudeAiOauth"]["expiresAt"] > (time.time() + 3000) * 1000
        assert beta["claudeAiOauth"]["keep"] == "value" and beta["keep"] == "outer"
        if os.name != "nt":
            assert stat.S_IMODE((accounts / "beta.json").stat().st_mode) == 0o600
        assert not list(accounts.glob("*.tmp"))


def test_refresh_failure_omits_only_failed_account(tmp):
    with setup(tmp, {"live-token": 10, "gamma-token": 60}, {
        "beta-token-refresh": urllib.error.HTTPError(
            "https://api.anthropic.com/v1/oauth/token", 400, "Bad Request", {},
            io.BytesIO(b'{"error":"invalid_grant"}')),
    }) as (mod, accounts, http):
        install(accounts, beta_stale=True)
        old = (accounts / "beta.json").read_bytes()
        credentials(accounts / "gamma.json", "gamma-token")
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1
        assert set(got["claude_accounts"]) == {"alpha", "gamma"}
        assert got["claude_accounts"]["gamma"]["five_hour"]["pct"] == 60
        assert set(got["errors"]) == {"claude:beta"}
        assert "HTTP 400" in got["errors"]["claude:beta"]
        assert (accounts / "beta.json").read_bytes() == old
        assert http.calls == [("usage", "Bearer live-token"),
                              ("refresh", "beta-token-refresh"), ("usage", "Bearer gamma-token")]


def test_unreadable_stash_is_named_and_other_accounts_report(tmp):
    with setup(tmp, {"live-token": 10, "gamma-token": 60}) as (mod, accounts, _):
        install(accounts)
        (accounts / "beta.json").write_text("broken json", encoding="utf-8")
        credentials(accounts / "gamma.json", "gamma-token")
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1
        assert set(got["claude_accounts"]) == {"alpha", "gamma"}
        assert set(got["errors"]) == {"claude:beta"}


def test_failed_stash_write_preserves_original(tmp):
    with setup(tmp, {"live-token": 10}, {
        "beta-token-refresh": {"access_token": "beta-new", "refresh_token": "new-refresh"},
    }) as (mod, accounts, _):
        install(accounts, beta_stale=True)
        old = (accounts / "beta.json").read_bytes()
        real_dump = json.dump

        def fail_dump(value, stream, *args, **kwargs):
            if "claudeAiOauth" in value:
                stream.write("partial")
                raise OSError("write failed")
            return real_dump(value, stream, *args, **kwargs)

        with mock.patch.object(mod.json, "dump", side_effect=fail_dump):
            rc, raw, _ = run(mod, "--json")
        assert rc == 1 and "write failed" in json.loads(raw)["errors"]["claude:beta"]
        assert (accounts / "beta.json").read_bytes() == old, "failed write destroyed stash"
        assert not list(accounts.glob("*.tmp"))


def test_stash_replace_uses_same_directory_and_preserves_old_on_failure(tmp):
    with setup(tmp, {"live-token": 10}, {
        "beta-token-refresh": {"access_token": "beta-new", "refresh_token": "new-refresh"},
    }) as (mod, accounts, _):
        install(accounts, beta_stale=True)
        old = (accounts / "beta.json").read_bytes()
        real_replace = os.replace
        replacements = []

        def fail_replace(src, dst):
            if Path(dst) == accounts / "beta.json":
                replacements.append((Path(src).parent, Path(dst)))
                assert Path(src).parent == accounts
                assert json.loads(Path(src).read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == "beta-new"
                raise OSError("replace failed")
            return real_replace(src, dst)

        with mock.patch.object(mod.os, "replace", side_effect=fail_replace):
            rc, raw, _ = run(mod, "--json")
        assert replacements == [(accounts, accounts / "beta.json")]
        assert rc == 1 and "replace failed" in json.loads(raw)["errors"]["claude:beta"]
        assert (accounts / "beta.json").read_bytes() == old
        assert not list(accounts.glob("*.tmp"))


def test_inactive_account_cache_survives_minute_polling_and_expires(tmp):
    with setup(tmp, {"live-token": 10, "beta-token": 40, "gamma-token": 70}) as (mod, accounts, http):
        install(accounts)
        credentials(accounts / "gamma.json", "gamma-token")
        start = time.time()
        with mock.patch.object(mod.time, "time", return_value=start):
            assert run(mod, "--json")[0] == 0
        http.tokens["beta-token"] = 55
        with mock.patch.object(mod.time, "time", return_value=start + 60):
            _, raw, _ = run(mod, "--json")
        assert json.loads(raw)["claude_accounts"]["beta"]["five_hour"]["pct"] == 40
        assert json.loads(raw)["claude_accounts"]["gamma"]["five_hour"]["pct"] == 70
        assert http.calls.count(("usage", "Bearer beta-token")) == 1
        assert http.calls.count(("usage", "Bearer gamma-token")) == 1
        with mock.patch.object(mod.time, "time", return_value=start + 301):
            _, raw, _ = run(mod, "--json")
        assert json.loads(raw)["claude_accounts"]["beta"]["five_hour"]["pct"] == 55
        assert http.calls.count(("usage", "Bearer beta-token")) == 2


def test_stale_live_token_still_refreshes_live_credentials(tmp):
    with setup(tmp, {"live-new": 20, "beta-token": 40}, {
        "live-token-refresh": {"access_token": "live-new",
                               "refresh_token": "live-refresh-new", "expires_in": 3600},
    }) as (mod, accounts, http):
        install(accounts)
        alpha = (accounts / "alpha.json").read_bytes()
        credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and got["usage"]["claude"]["five_hour"]["pct"] == 20
        assert got["claude_accounts"]["alpha"]["five_hour"]["pct"] == 20
        assert json.loads(Path(mod.CLAUDE_CRED).read_text(encoding="utf-8"))["claudeAiOauth"]["refreshToken"] == "live-refresh-new"
        assert (accounts / "alpha.json").read_bytes() == alpha
        assert http.calls == [("refresh", "live-token-refresh"),
                              ("usage", "Bearer live-new"), ("usage", "Bearer beta-token")]


def test_text_rows_follow_live_without_relabelling_it(tmp):
    with setup(tmp, {"live-token": 10, "beta-token": 40}) as (mod, accounts, _):
        install(accounts)
        rc, text, err = run(mod)
        assert rc == 0 and err == "", (rc, text, err)
        lines = text.splitlines()
        assert lines[1].split()[:2] == ["Claude", "5h"]
        assert lines[2].split()[0] == "7d"
        assert lines[3].split()[:3] == ["Claude", "beta", "5h"]
        assert lines[4].split()[0] == "7d"
        assert "Claude alpha" not in text


def test_live_failure_still_reports_inactive_account(tmp):
    with setup(tmp, {"beta-token": 40}) as (mod, accounts, _):
        install(accounts)
        Path(mod.CLAUDE_CRED).write_text("broken json", encoding="utf-8")
        rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1 and got["usage"] == {}
        assert set(got["claude_accounts"]) == {"beta"}
        assert set(got["errors"]) == {"claude"}


def test_account_errors_are_named_in_text_and_quiet_suppresses_them(tmp):
    with setup(tmp) as (mod, accounts, _):
        install(accounts)
        (accounts / "beta.json").write_text("broken json", encoding="utf-8")
        rc, text, err = run(mod)
        assert rc == 1 and "Claude" in text
        assert "Claude beta: ERROR" in err
        rc, text, err = run(mod, "--quiet")
        assert rc == 1 and "Claude" in text and err == ""


def test_other_provider_selection_does_not_read_stashes(tmp):
    with setup(tmp) as (mod, accounts, http):
        install(accounts)
        with mock.patch.object(mod, "query_kimi", return_value={}), \
                redirect_stdout(io.StringIO()) as out:
            rc = mod.main(["--kimi", "--json"])
        got = json.loads(out.getvalue())
        assert rc == 0 and "claude_accounts" not in got
        assert got["usage"] == {"kimi": {}} and http.calls == []


def test_live_entry_preserves_scoped_limits_and_omits_cache_metadata(tmp):
    with setup(tmp, {"beta-token": 40}) as (mod, accounts, http):
        install(accounts)
        window = {"pct": 10.0, "resets_at": "reset", "resets_in": "1h",
                  "pace_pct": 20.0, "recover_in": None}
        live = {"five_hour": window, "weekly": window,
                "_scoped": [{"label": "7d:scoped", **window}], "_stale_age": 60}
        original_query = mod.query_claude

        def query(*args, **kwargs):
            return original_query(*args, **kwargs) if args else live

        with mock.patch.object(mod, "query_claude", side_effect=query):
            rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 0 and got["usage"]["claude"] == live
        assert got["claude_accounts"]["alpha"] == {
            "live": True, "five_hour": window, "weekly": window,
            "_scoped": [{"label": "7d:scoped", **window}]}
        assert http.calls == [("usage", "Bearer beta-token")]


def test_accounts_directory_read_failure_preserves_live_output(tmp):
    with setup(tmp) as (mod, accounts, _):
        install(accounts)
        with mock.patch.object(mod.os, "listdir", side_effect=PermissionError("directory unreadable")):
            rc, raw, _ = run(mod, "--json")
        got = json.loads(raw)
        assert rc == 1 and "claude_accounts" not in got
        assert got["usage"]["claude"]["five_hour"]["pct"] == 10
        assert set(got["errors"]) == {"claude_accounts"}
        assert "directory unreadable" in got["errors"]["claude_accounts"]


def test_marker_read_failure_is_reported_in_json_and_text(tmp):
    with setup(tmp) as (mod, accounts, _):
        install(accounts)
        real_open = open

        def opened(path, *args, **kwargs):
            if Path(path) == accounts / "live":
                raise PermissionError("marker unreadable")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=opened):
            rc, raw, _ = run(mod, "--json")
            got = json.loads(raw)
            assert rc == 1 and "claude_accounts" not in got
            assert got["usage"]["claude"]["five_hour"]["pct"] == 10
            assert set(got["errors"]) == {"claude_accounts"}
            assert "marker unreadable" in got["errors"]["claude_accounts"]
            rc, text, err = run(mod)
            assert rc == 1 and "Claude" in text and "Claude accounts: ERROR" in err
            assert run(mod, "--quiet")[2] == ""


if __name__ == "__main__":
    sys.exit(_util.runner(_util.collect(globals()), "claudeaccounts_"))
