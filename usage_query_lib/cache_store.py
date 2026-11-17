"""Cache files only this user can have written; others are never read."""
import json
import math
import os
import stat
import tempfile
import time
from contextlib import suppress

_OPEN_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _mine(info) -> bool:
    return not hasattr(os, "geteuid") or info.st_uid == os.geteuid()


def _trusted(folder) -> bool:
    if not hasattr(os, "geteuid"):
        return True
    info = os.stat(folder)
    return _mine(info) and not info.st_mode & stat.S_IWOTH


def read(path, ttl, account):
    """(record, fresh data, (stale data, age s)); a file not this user's own, or another account's, is ({}, None, None)."""
    try:
        if not _trusted(os.path.dirname(path) or "."):
            return {}, None, None
        with os.fdopen(os.open(path, os.O_RDONLY | _OPEN_FLAGS), encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            record = json.load(stream) if stat.S_ISREG(info.st_mode) and _mine(info) else None
    except (OSError, ValueError, RecursionError):
        return {}, None, None
    if not isinstance(record, dict) or record.get("account", account) != account:
        return {}, None, None
    fetched, data = record.get("fetched_at"), record.get("data")
    try:
        age = time.time() - float(fetched) if isinstance(fetched, (int, float)) else math.nan
    except OverflowError:
        age = math.nan
    if not isinstance(data, dict) or not 0 <= age < math.inf:
        return record, None, None
    return (record, data, None) if age < ttl else (record, None, (data, int(age)))


def write(path, record, account) -> None:
    """Replace `path` through a fresh 0600 temp file; a failure never fails a reading."""
    folder = os.path.dirname(path) or "."
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        if not _trusted(folder):
            return
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".", suffix=".tmp")
    except OSError:
        return
    try:
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except Exception:
            os.close(fd)
            raise
        with stream:
            json.dump({**record, "account": account}, stream)
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            os.remove(tmp)


def retire(paths) -> None:
    """Remove this user's own regular files at `paths` (POSIX)."""
    for path in paths if hasattr(os, "geteuid") else ():
        with suppress(OSError):
            info = os.lstat(path)
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
                os.remove(path)
