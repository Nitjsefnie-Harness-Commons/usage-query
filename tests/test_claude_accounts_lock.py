"""Claude refresh persistence follows the account marker under the shared lock."""
import json
import os
import stat
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

import _util
from test_claude_accounts import credentials, install, run, setup


def replace_bytes(path, data):
    temp = path.with_suffix(".swap-tmp")
    temp.write_bytes(data)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def swap(accounts, name):
    """Model the switcher's locked pair relocation while HTTP is in flight."""
    lock = accounts / ".lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()} {time.time()}\n")
        live = accounts.parent / ".credentials.json"
        old = (accounts / "live").read_text(encoding="utf-8").strip()
        replace_bytes(accounts / f"{old}.json", live.read_bytes())
        replace_bytes(live, (accounts / f"{name}.json").read_bytes())
        replace_bytes(accounts / "live", (name + "\n").encode())
    finally:
        lock.unlink()


@contextmanager
def refreshing(tmp, name="alpha", change=None, allow_held=False):
    old_token = "live-token" if name == "alpha" else "beta-token"
    with setup(tmp, {"new-access": 25, "beta-token": 40}, {
        old_token + "-refresh": {"access_token": "new-access",
                                 "refresh_token": "new-refresh", "expires_in": 3600},
    }) as (mod, accounts, http):
        install(accounts, beta_stale=name == "beta")
        if name == "alpha":
            credentials(Path(mod.CLAUDE_CRED), old_token, stale=True)
        lock = accounts / ".lock"

        def request(req, timeout=None):
            if not allow_held:
                assert not lock.exists(), "network request held the accounts lock"
            answer = http(req, timeout)
            if req.full_url == mod.CLAUDE_OAUTH_TOKEN_URL and change:
                change(accounts)
            return answer

        with mock.patch("urllib.request.urlopen", side_effect=request):
            yield mod, accounts, http


def query(mod, accounts, name, errors):
    if name == "alpha":
        return mod.query_claude(errors=errors)
    return mod.query_claude(str(accounts / f"{name}.json"),
                            str(accounts / f".{name}.usage-cache"), errors=errors)


def assert_new_pair(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["claudeAiOauth"]["accessToken"] == "new-access"
    assert data["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert data["claudeAiOauth"]["expiresAt"] > (time.time() + 3000) * 1000
    assert data["claudeAiOauth"]["keep"] == "value" and data["keep"] == "outer"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_live_stays_live_persists_under_lock_to_live_file(tmp):
    with refreshing(tmp) as (mod, accounts, http):
        stash = (accounts / "alpha.json").read_bytes()
        replacements = []
        real_replace = os.replace

        def replace(src, dst):
            if Path(dst) == Path(mod.CLAUDE_CRED):
                lock = accounts / ".lock"
                assert lock.exists(), "live write was not locked"
                pid, epoch = lock.read_text(encoding="utf-8").split()
                assert int(pid) == os.getpid() and abs(float(epoch) - time.time()) < 5
                assert Path(src).parent == Path(dst).parent
                replacements.append(Path(dst))
            return real_replace(src, dst)

        errors = {}
        with mock.patch.object(mod.os, "replace", side_effect=replace):
            got = query(mod, accounts, "alpha", errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert replacements == [Path(mod.CLAUDE_CRED)]
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert (accounts / "alpha.json").read_bytes() == stash
        assert not (accounts / ".lock").exists()
        assert http.calls == [("refresh", "live-token-refresh"), ("usage", "Bearer new-access")]


def test_inactive_stays_inactive_persists_under_lock_to_stash(tmp):
    with refreshing(tmp, "beta") as (mod, accounts, _):
        live = Path(mod.CLAUDE_CRED).read_bytes()
        real_replace = os.replace
        destinations = []

        def replace(src, dst):
            if Path(dst) == accounts / "beta.json":
                assert (accounts / ".lock").exists(), "stash write was not locked"
                assert Path(src).parent == accounts
                destinations.append(Path(dst))
            return real_replace(src, dst)

        errors = {}
        with mock.patch.object(mod.os, "replace", side_effect=replace):
            got = query(mod, accounts, "beta", errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert destinations == [accounts / "beta.json"]
        assert_new_pair(accounts / "beta.json")
        assert Path(mod.CLAUDE_CRED).read_bytes() == live
        assert not (accounts / ".lock").exists()


def test_inactive_becomes_live_during_refresh_writes_live_file(tmp):
    with refreshing(tmp, "beta", lambda accounts: swap(accounts, "beta")) as (mod, accounts, http):
        old_beta = (accounts / "beta.json").read_bytes()
        old_alpha = Path(mod.CLAUDE_CRED).read_bytes()
        errors = {}
        got = query(mod, accounts, "beta", errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert (accounts / "beta.json").read_bytes() == old_beta, "rotation went to inactive snapshot"
        assert (accounts / "alpha.json").read_bytes() == old_alpha
        assert (accounts / "live").read_text(encoding="utf-8").strip() == "beta"
        assert not (accounts / ".lock").exists()
        assert http.calls == [("refresh", "beta-token-refresh"), ("usage", "Bearer new-access")]


def test_inactive_becomes_live_without_stash_uses_live_file(tmp):
    def swap_and_remove_stash(accounts):
        swap(accounts, "beta")
        (accounts / "beta.json").unlink()

    with refreshing(tmp, "beta", swap_and_remove_stash) as (mod, accounts, _):
        errors = {}
        got = query(mod, accounts, "beta", errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert not (accounts / "beta.json").exists(), "live pair was written to a recreated stash"


def test_live_swapped_out_during_refresh_writes_original_accounts_stash(tmp):
    with refreshing(tmp, change=lambda accounts: swap(accounts, "beta")) as (mod, accounts, http):
        beta = (accounts / "beta.json").read_bytes()
        errors = {}
        got = query(mod, accounts, "alpha", errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert_new_pair(accounts / "alpha.json")
        assert Path(mod.CLAUDE_CRED).read_bytes() == beta, "rotation overwrote the new live account"
        assert (accounts / "beta.json").read_bytes() == beta
        assert (accounts / "live").read_text(encoding="utf-8").strip() == "beta"
        assert not (accounts / ".lock").exists()
        assert http.calls == [("refresh", "live-token-refresh"), ("usage", "Bearer new-access")]


def test_live_stash_removed_during_refresh_still_updates_live_file(tmp):
    with refreshing(tmp, change=lambda accounts: (accounts / "alpha.json").unlink()) as (mod, accounts, _):
        errors = {}
        got = query(mod, accounts, "alpha", errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert not (accounts / "alpha.json").exists(), "live rotation was redirected to a recreated stash"


def test_live_marker_lost_during_refresh_still_updates_live_file(tmp):
    for index, marker in enumerate((None, "", "../escape", b"\xff")):
        home = Path(tmp) / f"case{index}"
        home.mkdir()

        def lose_marker(accounts, value=marker):
            if value is None:
                (accounts / "live").unlink()
            elif isinstance(value, bytes):
                (accounts / "live").write_bytes(value)
            else:
                (accounts / "live").write_text(value + "\n", encoding="utf-8")

        with refreshing(str(home), change=lose_marker) as (mod, accounts, _):
            stash = (accounts / "alpha.json").read_bytes()
            errors = {}
            got = query(mod, accounts, "alpha", errors)
            assert errors == {} and got["five_hour"]["pct"] == 25
            assert_new_pair(Path(mod.CLAUDE_CRED))
            assert (accounts / "alpha.json").read_bytes() == stash


def test_swapped_in_account_without_stash_is_not_overwritten(tmp):
    def swap_and_remove_stash(accounts):
        swap(accounts, "beta")
        (accounts / "beta.json").unlink()

    with refreshing(tmp, change=swap_and_remove_stash) as (mod, accounts, _):
        beta = (accounts / "beta.json").read_bytes()
        errors = {}
        got = query(mod, accounts, "alpha", errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert Path(mod.CLAUDE_CRED).read_bytes() == beta, "missing stash invalidated the current live marker"
        assert_new_pair(accounts / "alpha.json")


def test_swap_between_marker_and_credential_read_cannot_misidentify_pair(tmp):
    with setup(tmp, {"alpha-new": 20, "beta-new": 50}, {
        "live-token-refresh": {"access_token": "alpha-new", "refresh_token": "alpha-refresh-new"},
        "beta-token-refresh": {"access_token": "beta-new", "refresh_token": "beta-refresh-new"},
    }) as (mod, accounts, http):
        install(accounts, beta_stale=True)
        credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
        beta = (accounts / "beta.json").read_bytes()
        original_open = open
        attempted = []
        deferred = []

        def opened(path, *args, **kwargs):
            if Path(path) == Path(mod.CLAUDE_CRED) and not attempted:
                attempted.append(True)
                if (accounts / ".lock").exists():
                    deferred.append(True)
                else:
                    swap(accounts, "beta")
            return original_open(path, *args, **kwargs)

        def request(req, timeout=None):
            assert not (accounts / ".lock").exists(), "snapshot lock extended across HTTP"
            if req.full_url == mod.CLAUDE_OAUTH_TOKEN_URL and deferred:
                deferred.clear()
                swap(accounts, "beta")
            return http(req, timeout)

        errors = {}
        with mock.patch("builtins.open", side_effect=opened), \
                mock.patch("urllib.request.urlopen", side_effect=request):
            got = query(mod, accounts, "alpha", errors)
        assert got["five_hour"]["pct"] == 20, "live query refreshed the wrong account"
        assert errors == {} and attempted
        assert json.loads((accounts / "alpha.json").read_text(encoding="utf-8"))["claudeAiOauth"]["refreshToken"] == "alpha-refresh-new"
        assert Path(mod.CLAUDE_CRED).read_bytes() == beta, "new live account was modified"
        assert http.calls == [("refresh", "live-token-refresh"), ("usage", "Bearer alpha-new")]


@contextmanager
def held_by_process(lock):
    source = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from test_claude_accounts_lock import replace_bytes\n"
        "path = sys.argv[1]\n"
        "fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)\n"
        "with os.fdopen(fd, 'w') as stream:\n"
        "    stream.write(f'{os.getpid()} {time.time()}\\n')\n"
        "try:\n"
        "    print('locked', flush=True)\n"
        "    command = sys.stdin.readline().strip()\n"
        "    if command.startswith('swap '):\n"
        "        accounts = Path(path).parent\n"
        "        name = command.split()[1]\n"
        "        live = accounts.parent / '.credentials.json'\n"
        "        old = (accounts / 'live').read_text(encoding='utf-8').strip()\n"
        "        replace_bytes(accounts / (old + '.json'), live.read_bytes())\n"
        "        replace_bytes(live, (accounts / (name + '.json')).read_bytes())\n"
        "        replace_bytes(accounts / 'live', (name + '\\n').encode())\n"
        "        print('swapped', flush=True)\n"
        "        sys.stdin.readline()\n"
        "finally:\n"
        "    os.remove(path)\n"
    )
    with subprocess.Popen([sys.executable, "-c", source, str(lock), str(Path(__file__).resolve().parent)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as child:
        try:
            assert child.stdout.readline().strip() == "locked"
            yield child
        finally:
            _, err = child.communicate("release\n", timeout=5)
            assert child.returncode == 0, err


def test_snapshot_lock_deadline_does_not_refresh(tmp):
    with refreshing(tmp, allow_held=True) as (mod, accounts, http):
        live = Path(mod.CLAUDE_CRED).read_bytes()
        stashes = {p.name: p.read_bytes() for p in accounts.glob("*.json")}
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            assert 0 < seconds <= 0.1
            clock[0] += seconds
            sleeps.append(seconds)

        with held_by_process(accounts / ".lock") as child:
            with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(mod.time, "sleep", side_effect=sleep):
                rc, raw, _ = run(mod, "--json")
            got = json.loads(raw)
            assert rc == 1 and "claude" in got["errors"], raw
            assert "lock" in got["errors"]["claude"].lower()
            assert "claude" not in got["usage"]
            assert clock[0] >= 30 and sleeps, "lock contention never waited to the deadline"
            assert child.poll() is None and (accounts / ".lock").exists()
            assert Path(mod.CLAUDE_CRED).read_bytes() == live, "snapshot timeout changed credentials"
            assert {p.name: p.read_bytes() for p in accounts.glob("*.json")} == stashes
            assert not list(accounts.glob("*.tmp"))
        assert http.calls == [], "snapshot timeout spent a refresh token"


def test_persist_lock_timeout_saves_refreshed_pair_without_lock(tmp):
    with ExitStack() as holders:
        children = []

        def hold(accounts):
            children.append(holders.enter_context(held_by_process(accounts / ".lock")))

        with refreshing(tmp, change=hold, allow_held=True) as (mod, accounts, http):
            stash = (accounts / "alpha.json").read_bytes()
            clock = [0.0]

            def sleep(seconds):
                assert 0 < seconds <= 0.1
                clock[0] += seconds

            errors = {}
            with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(mod.time, "sleep", side_effect=sleep):
                got = query(mod, accounts, "alpha", errors)
            assert got["five_hour"]["pct"] == 25
            assert set(errors) == {"claude:alpha"}
            assert "lock" in errors["claude:alpha"].lower()
            assert clock[0] >= 30
            assert children[0].poll() is None
            assert_new_pair(Path(mod.CLAUDE_CRED))
            assert (accounts / "alpha.json").read_bytes() == stash
            assert http.calls == [("refresh", "live-token-refresh"), ("usage", "Bearer new-access")]


def test_persist_lock_timeout_rereads_marker_and_saves_original_account(tmp):
    with ExitStack() as holders:
        children = []

        def hold_after_swap(accounts):
            children.append(holders.enter_context(held_by_process(accounts / ".lock")))

        with refreshing(tmp, change=hold_after_swap, allow_held=True) as (mod, accounts, _):
            beta = (accounts / "beta.json").read_bytes()
            clock = [0.0]
            swapped = []

            def sleep(seconds):
                assert 0 < seconds <= 0.1
                if not swapped:
                    children[0].stdin.write("swap beta\n")
                    children[0].stdin.flush()
                    assert children[0].stdout.readline().strip() == "swapped"
                    swapped.append(True)
                clock[0] += seconds

            errors = {}
            with mock.patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
                    mock.patch.object(mod.time, "sleep", side_effect=sleep):
                got = query(mod, accounts, "alpha", errors)
            assert got["five_hour"]["pct"] == 25
            assert set(errors) == {"claude:alpha"}
            assert clock[0] >= 30 and children[0].poll() is None and swapped
            assert_new_pair(accounts / "alpha.json")
            assert Path(mod.CLAUDE_CRED).read_bytes() == beta, "timeout fallback overwrote the new live account"
            assert (accounts / "live").read_text(encoding="utf-8").strip() == "beta"


def test_stale_lock_is_removed_and_refresh_persists(tmp):
    with refreshing(tmp, allow_held=True) as (mod, accounts, _):
        lock = accounts / ".lock"
        lock.write_text("123 0\n", encoding="utf-8")
        old = time.time() - 31
        os.utime(lock, (old, old))
        real_open = os.open
        acquired = []

        def opened(path, flags, mode=0o777):
            if Path(path) == lock:
                assert flags == os.O_CREAT | os.O_EXCL | os.O_WRONLY
                assert mode == 0o600
                acquired.append(path)
            return real_open(path, flags, mode)

        errors = {}
        with mock.patch.object(mod.os, "open", side_effect=opened), \
                mock.patch.object(mod.time, "sleep", side_effect=AssertionError("stale lock was not reclaimed immediately")):
            got = query(mod, accounts, "alpha", errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert len(acquired) >= 2, "stale lock was never retried"
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert not lock.exists()


def test_no_accounts_directory_refreshes_without_creating_lock(tmp):
    with setup(tmp, {"new-access": 25}, {
        "live-token-refresh": {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
    }) as (mod, accounts, _):
        credentials(Path(mod.CLAUDE_CRED), "live-token", stale=True)
        real_open = os.open
        locks = []

        def opened(path, flags, mode=0o777):
            if Path(path).name == ".lock":
                locks.append(path)
            return real_open(path, flags, mode)

        errors = {}
        with mock.patch.object(mod.os, "open", side_effect=opened):
            got = query(mod, accounts, "alpha", errors)
        assert errors == {} and got["five_hour"]["pct"] == 25
        assert_new_pair(Path(mod.CLAUDE_CRED))
        assert locks == [] and not accounts.exists(), "legacy refresh created an accounts lock"


def test_unidentified_starting_live_account_persists_live_pair(tmp):
    for index, marker in enumerate((None, "", "absent", "../escape", b"\xff")):
        home = Path(tmp) / f"case{index}"
        home.mkdir()
        with refreshing(str(home)) as (mod, accounts, _):
            if marker is None:
                (accounts / "live").unlink()
            elif isinstance(marker, bytes):
                (accounts / "live").write_bytes(marker)
            else:
                (accounts / "live").write_text(marker + "\n", encoding="utf-8")
            stashes = {p.name: p.read_bytes() for p in accounts.glob("*.json")}
            real_replace = os.replace

            def replace(src, dst, live_path=Path(mod.CLAUDE_CRED), directory=accounts, replace_fn=real_replace):
                if Path(dst) == live_path:
                    assert (directory / ".lock").exists(), "unidentified live write was not locked"
                    assert Path(src).parent == Path(dst).parent
                return replace_fn(src, dst)

            errors = {}
            with mock.patch.object(mod.os, "replace", side_effect=replace):
                got = query(mod, accounts, "alpha", errors)
            assert got["five_hour"]["pct"] == 25
            assert errors == {}, errors
            assert_new_pair(Path(mod.CLAUDE_CRED))
            assert {p.name: p.read_bytes() for p in accounts.glob("*.json")} == stashes
            assert not (accounts / ".lock").exists()


if __name__ == "__main__":
    sys.exit(_util.runner(_util.collect(globals()), "claudeaccountslock_"))
