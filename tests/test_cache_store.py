"""Cache files are this user's own: others' files are never read, writes are private."""
import json
import os
import threading
import time
from pathlib import Path
from unittest import mock

import _util
from test_claude_backoff import PAYLOAD, query, rig

RECORD, NOTHING = {"fetched_at": 0, "data": {"v": 1}}, ({}, None, None)


def as_other(name):
    """os.<name> reporting another user as the owner."""
    real = getattr(os, name)
    return mock.patch.object(os, name, lambda *a: os.stat_result((*real(*a)[:4], real(*a).st_uid + 1, *real(*a)[5:])))


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
    assert cache.read(mod.CACHE, 60)[0] == RECORD and os.listdir(home) == [os.path.basename(mod.CACHE)]
    if not hasattr(os, "geteuid"):
        _util.skip("no uids to own files by on this platform")
    assert (os.stat(home).st_mode & 0o777, os.stat(mod.CACHE).st_mode & 0o777) == (0o700, 0o600)
    for name in ("fstat", "stat"):
        with as_other(name):
            assert cache.read(mod.CACHE, 60) == NOTHING, name
    with as_other("stat"):
        cache.write(mod.CACHE, {"fetched_at": 1, "data": {}})
    os.chmod(home, 0o777)
    cache.write(mod.CACHE, {"fetched_at": 2, "data": {}})
    assert cache.read(mod.CACHE, 60) == NOTHING and json.loads(Path(mod.CACHE).read_text(encoding="utf-8")) == RECORD
    os.chmod(home, 0o700)
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


def test_a_backoff_past_the_cap_or_not_finite_is_ignored(tmp):
    mod, cache, cred = rig(tmp, "uq_store_backoff", cached_age=600)
    for until, held in ((1e300, False), (float("inf"), False), (float("nan"), False), (time.time() + mod.BACKOFF_CAP + 60, False), (time.time() + mod.BACKOFF_CAP - 60, True)):
        Path(cache).write_text(json.dumps({"fetched_at": time.time() - 600, "data": PAYLOAD, "backoff": {"streak": 1, "until": until}}), encoding="utf-8")
        got, get = query(mod, cache, cred, lambda *_: PAYLOAD)
        assert get.call_count == (0 if held else 1) and ("backing off" in got.get("_stale_error", "")) is held, (until, got)


if __name__ == "__main__":
    raise SystemExit(_util.runner(_util.collect(globals()), "usagequerycachestore_"))
