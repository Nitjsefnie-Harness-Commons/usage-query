"""A 429 that outlives the retries backs every process off that account, doubling to a cap."""
import io
import json
import os
import sys
import time
import urllib.error
from unittest import mock

import _util

USAGE_QUERY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "usage_query_lib", "query.py")
PAYLOAD = {"five_hour": {"utilization": 7, "resets_at": None},
           "seven_day": {"utilization": 8, "resets_at": None}}


def rig(tmp, name, cached_age):
    mod = _util.load(USAGE_QUERY, name)
    cache = os.path.join(tmp, "claude-usage.json")
    cred = os.path.join(tmp, "credentials.json")
    with open(cred, "w", encoding="utf-8") as f:
        f.write("{}")
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time() - cached_age, "data": PAYLOAD}, f)
    return mod, cache, cred


def throttled(retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return urllib.error.HTTPError("u", 429, "Too Many Requests", headers, io.BytesIO(b""))


def query(mod, cache, cred, fetch, probe=RuntimeError("no probe")):
    with mock.patch.object(mod, "_claude_access_token", return_value="t"), \
            mock.patch.object(mod, "_claude_header_probe", side_effect=probe), \
            mock.patch.object(mod, "_get_retry", side_effect=fetch) as get:
        return mod.query_claude(cred, cache), get


def test_a_429_backs_off_every_later_fetch_until_it_expires(tmp):
    mod, cache, cred = rig(tmp, "uq_backoff_hold", cached_age=600)
    got, get = query(mod, cache, cred, throttled())
    assert get.call_count == 1 and got["weekly"]["pct"] == 8 and "429" in got["_stale_error"]
    backoff = json.load(open(cache, encoding="utf-8"))["backoff"]
    assert backoff["streak"] == 1
    assert mod.BACKOFF_BASE - 5 <= backoff["until"] - time.time() <= mod.BACKOFF_BASE
    got, get = query(mod, cache, cred, AssertionError("fetched inside the backoff"))
    assert get.call_count == 0 and "backing off" in got["_stale_error"]


def test_consecutive_429s_double_the_backoff_up_to_the_cap(tmp):
    mod, cache, cred = rig(tmp, "uq_backoff_double", cached_age=600)
    delays = []
    for _ in range(6):
        record = json.load(open(cache, encoding="utf-8"))
        if "backoff" in record:
            record["backoff"]["until"] = 0  # let the next attempt through
            json.dump(record, open(cache, "w", encoding="utf-8"))
        query(mod, cache, cred, throttled())
        delays.append(round(json.load(open(cache, encoding="utf-8"))["backoff"]["until"] - time.time(), -1))
    base, cap = mod.BACKOFF_BASE, mod.BACKOFF_CAP
    assert delays == [min(cap, base * 2 ** n) for n in range(6)], delays
    query(mod, cache, cred, throttled(retry_after=cap * 10))
    assert json.load(open(cache, encoding="utf-8"))["backoff"]["until"] - time.time() <= cap


def test_a_success_clears_the_backoff(tmp):
    mod, cache, cred = rig(tmp, "uq_backoff_clear", cached_age=600)
    query(mod, cache, cred, throttled())
    record = json.load(open(cache, encoding="utf-8"))
    record["backoff"]["until"] = 0
    json.dump(record, open(cache, "w", encoding="utf-8"))
    got, get = query(mod, cache, cred, lambda *_: PAYLOAD)
    assert get.call_count == 1 and "_stale_error" not in got
    assert "backoff" not in json.load(open(cache, encoding="utf-8"))


def test_a_throttled_endpoint_is_read_from_the_inference_headers(tmp):
    mod, cache, cred = rig(tmp, "uq_backoff_probe", cached_age=600)
    probed = {"seven_day": {"utilization": 49.0, "resets_at": "2026-11-11T21:00:00Z"}}
    got, get = query(mod, cache, cred, throttled(), probe=lambda _t: probed)
    assert get.call_count == 1 and got["weekly"]["pct"] == 49.0 and "_stale_error" not in got
    record = json.load(open(cache, encoding="utf-8"))
    assert record["backoff"]["streak"] == 1 and record["data"]["five_hour"] == PAYLOAD["five_hour"]
    record["fetched_at"] = 0  # past the TTL, still inside the backoff
    json.dump(record, open(cache, "w", encoding="utf-8"))
    probed["seven_day"]["utilization"] = 51.0
    got, get = query(mod, cache, cred, AssertionError("fetched inside the backoff"),
                     probe=lambda _t: probed)
    assert get.call_count == 0 and got["weekly"]["pct"] == 51.0 and "_stale_error" not in got


if __name__ == "__main__":
    sys.exit(_util.runner(_util.collect(globals()), "claudebackoff_"))
