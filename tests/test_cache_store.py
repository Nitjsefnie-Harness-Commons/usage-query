"""Cache files are this user's own: others' files are never read, writes are private."""
import io
import json
import os
import stat
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import _util
from test_claude_backoff import PAYLOAD, query, rig

RECORD, NOTHING = {"fetched_at": 0, "data": {"v": 1}}, ({}, None, None)


def patched(name, uid=0, mode=0):
    """os.<name> reporting the owner `uid` users on and `mode` bits added."""
    real = getattr(os, name)
    return mock.patch.object(os, name, lambda *a: os.stat_result((real(*a).st_mode | mode, *real(*a)[1:4], real(*a).st_uid + uid, *real(*a)[5:])))


def test_the_caches_are_private_files_in_this_users_home(tmp):
    mod = _util.load(_util.script("query.py"), "usage_query_cache_store")
    cache, home = mod.cache_store, os.path.join(tmp, ".cache", "usage-query")
    assert Path(cache.__file__).samefile(_util.script("cache_store.py")), "not this checkout's cache_store"
    assert {os.path.dirname(getattr(mod, name)) for name in ("CACHE", "KIMI_CACHE", "CODEX_CACHE", "ZAI_CACHE", "ZAI_RESETS_CACHE")} == {home}
    old = os.umask(0o022)
    try:
        cache.write(mod.CACHE, RECORD)
    finally:
        os.umask(old)
    with mock.patch.object(os, "replace", side_effect=OSError("busy")):
        cache.write(mod.CACHE, {"fetched_at": 1, "data": {}})
    with mock.patch.object(os, "fdopen", side_effect=OSError("no stream")), mock.patch.object(os, "close", wraps=os.close) as close:
        cache.write(mod.CACHE, {"fetched_at": 1, "data": {}})
    assert close.call_count == 1, "the temp file's descriptor was left open"
    assert cache.read(mod.CACHE, 60)[0] == RECORD and os.listdir(home) == [os.path.basename(mod.CACHE)]
    if not hasattr(os, "geteuid"):
        _util.skip("no uids to own files by on this platform")
    assert (os.stat(home).st_mode & 0o777, os.stat(mod.CACHE).st_mode & 0o777) == (0o700, 0o600)
    with patched("fstat", uid=1):
        assert cache.read(mod.CACHE, 60) == NOTHING, "another user's file was read"
    for uid, mode in ((1, 0), (0, stat.S_IWOTH)):  # the folder another user's, or world-writable
        with patched("stat", uid, mode):
            assert cache.read(mod.CACHE, 60) == NOTHING, (uid, mode)
            cache.write(mod.CACHE, {"fetched_at": 2, "data": {}})
        assert json.loads(Path(mod.CACHE).read_text(encoding="utf-8")) == RECORD, (uid, mode)
    link, fifo, got = os.path.join(home, "link"), os.path.join(home, "fifo"), []
    os.symlink(mod.CACHE, link)
    os.mkfifo(fifo)

    def promptly(*paths):
        reader = threading.Thread(target=lambda: got.extend(cache.read(path, 60) for path in paths), daemon=True)
        reader.start()
        reader.join(10)
    promptly(fifo)
    assert got == [NOTHING], "a FIFO without a writer blocked the open"
    writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)  # holds the pipe open: a blocking read never ends
    try:
        os.write(writer, json.dumps(RECORD).encode())
        promptly(fifo, link)
    finally:
        os.close(writer)
    assert got == [NOTHING] * 3, got
    # Earlier versions' temp-dir caches (the runner's temp dir is `tmp`): only this user's own regular files go.
    own, other, link, pid = (os.path.join(tmp, name) for name in (*mod.LEGACY_CACHES[:3], mod.LEGACY_CACHES[0] + ".123"))
    for path in (own, other, pid):
        Path(path).write_text("{}", encoding="utf-8")
    os.symlink(pid, link)
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        with patched("lstat", uid=1):
            mod.main(["--kimi"])
        assert all(os.path.lexists(path) for path in (own, other, link, pid)), "another user's file was removed"
        mod.main(["--kimi"])
    assert [os.path.lexists(path) for path in (own, other, link, pid)] == [False, False, True, True]


def test_a_backoff_past_the_cap_or_not_finite_is_ignored(tmp):
    mod, cache, cred = rig(tmp, "uq_store_backoff", cached_age=600)
    for until, held in ((1e300, False), (10 ** 400, False), (float("inf"), False), (float("nan"), False), (time.time() + mod.BACKOFF_CAP + 60, False), (time.time() + mod.BACKOFF_CAP - 60, True)):
        Path(cache).write_text(json.dumps({"fetched_at": time.time() - 600, "data": PAYLOAD, "backoff": {"streak": 1, "until": until}}), encoding="utf-8")
        got, get = query(mod, cache, cred, lambda *_: PAYLOAD)
        assert get.call_count == (0 if held else 1) and ("backing off" in got.get("_stale_error", "")) is held, (until, got)


if __name__ == "__main__":
    raise SystemExit(_util.runner(_util.collect(globals()), "usagequerycachestore_"))
