#!/usr/bin/env python3
"""z.ai weekly quota resets: fetched beside the quota, shown on the 7d row.

The reset list is a second, optional call. Its data is an annotation on the
weekly window; every way it can fail has to leave the quota rows exactly as
they were before the list was read at all.
"""
import io
import json
import os
import sys
import time
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402
from test_usage_query_behavior import (  # noqa: E402
    _frozen_clock, _zai_envelope, _zai_sources)


USAGE_QUERY = os.path.join(_util.SCRIPTS, "query.py")
# The keys query_zai returned before the reset list existed. A failed reset
# fetch must hand back exactly this shape.
QUOTA_ONLY_KEYS = {"five_hour", "weekly", "_scoped", "_plan_type", "_billing"}
# Inside the quota envelope's weekly window (resets 2026-09-01 12:00 UTC).
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _load():
    return _util.load(USAGE_QUERY, "usage_query_weekly_resets")


def _entry(record_id, expire, available=True):
    """One reset-list record. Ids are synthetic: this repository is public."""
    return {"recordId": record_id, "grantType": "DIRECT",
            "expireTime": expire, "available": available}


def _reset_envelope(week, five_hour=()):
    """The /api/biz/customer-package-reset/list shape, synthetic ids only.
    Timestamps are UTC+8 wall-clock strings, as the endpoint sends them."""
    return {"code": 200, "msg": "Operation successful", "success": True,
            "data": {"customerId": 1234, "targetType": "PERSONAL",
                     "organizationId": None, "projectId": None,
                     "lastFiveHourResetTime": None,
                     "lastWeekResetTime": "2026-08-22 00:15:42",
                     "fiveHourResets": list(five_hour),
                     "weekResets": list(week)}}


def _local(wall):
    """A UTC+8 wall-clock string rendered the way the table renders resets."""
    utc8 = timezone(timedelta(hours=8))
    moment = datetime.strptime(wall, "%Y-%m-%d %H:%M:%S").replace(tzinfo=utc8)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


class _Endpoints:
    """_get_retry stand-in routing by URL; records every call it answers."""

    def __init__(self, mod, resets):
        self.mod = mod
        self.resets = resets
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url == self.mod.ZAI_URL:
            return _zai_envelope()
        if url == self.mod.ZAI_RESETS_URL:
            if isinstance(self.resets, BaseException):
                raise self.resets
            return self.resets
        raise AssertionError(f"unexpected fetch {url}")

    def urls(self):
        return [url for url, _ in self.calls]


def _query(mod, tmp, resets, argv=None, resets_cache=None, quota_cache=None):
    """Run query_zai (or main(argv)) against the routed endpoints, with every
    cache and key source pinned inside `tmp`."""
    key_file = os.path.join(tmp, "api-key")
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write("k" * 49 + "\n")
    endpoints = _Endpoints(mod, resets)
    with _zai_sources(mod, files=(key_file,)), \
            mock.patch.object(mod, "ZAI_CACHE",
                              quota_cache or os.path.join(tmp, "quota.json")), \
            mock.patch.object(mod, "ZAI_RESETS_CACHE",
                              resets_cache or os.path.join(tmp, "resets.json")), \
            mock.patch.object(mod, "_get_retry", side_effect=endpoints), \
            _frozen_clock(mod, NOW):
        if argv is None:
            return mod.query_zai(), endpoints
        out = io.StringIO()
        with redirect_stdout(out):
            rc = mod.main(argv)
        assert rc == 0
        return out.getvalue(), endpoints


def _weekly_line(text):
    return next(line for line in text.splitlines()
                if line.startswith(" ") and " 7d " in f" {line} ")


def test_available_weekly_resets_are_counted_with_the_earliest_expiry(tmp):
    mod = _load()
    resets = _reset_envelope(
        [_entry(2, "2026-10-29 00:13:36"), _entry(3, "2026-10-20 08:00:00"),
         _entry(4, "2026-11-02 12:30:00"), _entry(5, "2026-10-29 00:13:36")],
        five_hour=[_entry(9, "2026-09-01 00:00:00")])
    out, endpoints = _query(mod, tmp, resets)
    block = out["_weekly_resets"]
    assert block["available"] == 4
    assert block["earliest_expires_at"].startswith(_local("2026-10-20 08:00:00"))
    assert block["earliest_expires_in"] == "52d12h"  # 2026-10-20 00:00 UTC
    # Same auth and headers as the quota call.
    quota_headers = dict(endpoints.calls[0][1])
    assert endpoints.urls() == [mod.ZAI_URL, mod.ZAI_RESETS_URL]
    assert endpoints.calls[1][1] == quota_headers
    assert quota_headers["Authorization"] == "k" * 49
    # Cached like the quota response, in a file of its own.
    with open(os.path.join(tmp, "resets.json"), encoding="utf-8") as fh:
        assert json.load(fh)["data"]["data"]["weekResets"][0]["recordId"] == 2


def test_the_7d_row_names_the_available_weekly_resets(tmp):
    mod = _load()
    resets = _reset_envelope(
        [_entry(2, "2026-10-29 00:13:36"), _entry(3, "2026-10-20 08:00:00")],
        five_hour=[_entry(9, "2026-09-01 00:00:00")])
    text, _ = _query(mod, tmp, resets, argv=["--zai"])
    line = _weekly_line(text)
    assert line.endswith(
        "2 weekly resets available (earliest expires "
        f"{_local('2026-10-20 08:00:00')}, in 52d12h)"), line
    # Weekly only: an available five-hour reset is never counted or named.
    assert text.count("available") == 1, text


def test_one_available_weekly_reset_reads_in_the_singular(tmp):
    mod = _load()
    resets = _reset_envelope([_entry(2, "2026-10-29 00:13:36")])
    text, _ = _query(mod, tmp, resets, argv=["--zai"])
    assert "1 weekly reset available (earliest expires" in _weekly_line(text)


def test_json_carries_the_weekly_reset_count_and_earliest_expiry(tmp):
    mod = _load()
    resets = _reset_envelope([_entry(2, "2026-10-29 00:13:36")])
    text, _ = _query(mod, tmp, resets, argv=["--zai", "--json"])
    block = json.loads(text)["usage"]["zai"]["_weekly_resets"]
    assert block["available"] == 1
    assert block["earliest_expires_at"].startswith(_local("2026-10-29 00:13:36"))
    assert block["earliest_expires_in"] == "61d4h"  # 2026-10-28 16:13 UTC


def test_mixed_availability_counts_and_dates_only_the_available_ones(tmp):
    mod = _load()
    resets = _reset_envelope([
        _entry(2, "2026-09-05 00:00:00", available=False),  # earliest, spent
        _entry(3, "2026-10-29 00:13:36"),
        {"recordId": 4, "expireTime": "2026-09-06 00:00:00"},  # no flag
        _entry(5, "2026-09-07 00:00:00", available="true"),  # not a boolean
        "not-a-record",
        _entry(6, "2026-10-21 10:00:00"),
    ])
    out, _ = _query(mod, tmp, resets)
    assert out["_weekly_resets"]["available"] == 2
    assert out["_weekly_resets"]["earliest_expires_at"].startswith(
        _local("2026-10-21 10:00:00"))


def test_zero_available_is_reported_in_json_and_omitted_from_the_table(tmp):
    """Nothing to spend is not news - the Codex banked-reset convention: the
    human view stays as quiet as before, while JSON still says 0, so a script
    can tell 'none left' from 'could not ask'."""
    mod = _load()
    resets = _reset_envelope(
        [_entry(2, "2026-10-29 00:13:36", available=False)],
        five_hour=[_entry(9, "2026-09-01 00:00:00")])
    out, _ = _query(mod, tmp, resets)
    assert out["_weekly_resets"] == {"available": 0,
                                     "earliest_expires_at": None,
                                     "earliest_expires_in": None}
    text, _ = _query(mod, tmp, resets, argv=["--zai"])
    assert "reset" not in _weekly_line(text)
    assert "available" not in text


def _quota_only_output(mod, tmp, failure, name):
    """query_zai and the human table under a failing reset list, each run
    against fresh caches so no earlier success can answer for it."""
    sub = os.path.join(tmp, name)
    os.makedirs(sub)
    out, endpoints = _query(mod, sub, failure)
    assert endpoints.urls() == [mod.ZAI_URL, mod.ZAI_RESETS_URL], name
    text, _ = _query(mod, sub, failure, argv=["--zai"],
                     resets_cache=os.path.join(sub, "resets2.json"),
                     quota_cache=os.path.join(sub, "quota2.json"))
    assert not os.path.exists(os.path.join(sub, "resets.json")), name
    return out, text


def test_a_failed_reset_list_leaves_the_quota_rows_exactly_as_before(tmp):
    mod = _load()
    baseline_out, baseline_text = _quota_only_output(
        mod, tmp, RuntimeError("stand-in for the pre-feature path"), "base")
    # The quota-only baseline is the pre-feature shape and table.
    assert set(baseline_out) == QUOTA_ONLY_KEYS, set(baseline_out)
    assert "resets available" not in baseline_text

    no_week = _reset_envelope([])
    del no_week["data"]["weekResets"]
    week_not_list = _reset_envelope([])
    week_not_list["data"]["weekResets"] = {"recordId": 1}
    failures = {
        "http": urllib.error.HTTPError(mod.ZAI_RESETS_URL, 500, "boom",
                                       None, None),
        "timeout": TimeoutError("timed out"),
        "code": dict(_reset_envelope([_entry(2, "2026-10-29 00:13:36")]),
                     code=1001, msg="Authorization failure"),
        "no_data": {"code": 200, "msg": "ok", "data": None},
        "no_week_key": no_week,
        "week_not_list": week_not_list,
        "not_an_object": ["weekResets"],
    }
    for name, failure in failures.items():
        out, text = _quota_only_output(mod, tmp, failure, name)
        assert set(out) == QUOTA_ONLY_KEYS, (name, set(out))
        assert out["weekly"] == baseline_out["weekly"], name
        assert text == baseline_text, name


def test_a_failed_reset_fetch_keeps_the_last_good_list(tmp):
    """Cached the way the quota is: a failure never blanks the cache, and the
    last good list answers - however stale - tagged with its age."""
    mod = _load()
    cache = os.path.join(tmp, "resets.json")
    good = _reset_envelope([_entry(2, "2026-10-29 00:13:36")])
    written = time.time() - 600
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": written, "data": good}, fh)
    failure = urllib.error.HTTPError(mod.ZAI_RESETS_URL, 502, "bad gateway",
                                     None, None)
    out, endpoints = _query(mod, tmp, failure)
    assert mod.ZAI_RESETS_URL in endpoints.urls()
    assert out["_weekly_resets"]["available"] == 1
    assert 595 <= out["_weekly_resets"]["stale_age"] <= 700
    with open(cache, encoding="utf-8") as fh:
        assert json.load(fh) == {"fetched_at": written, "data": good}


def test_a_fresh_reset_cache_is_used_without_a_fetch(tmp):
    mod = _load()
    cache = os.path.join(tmp, "resets.json")
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": time.time(),
                   "data": _reset_envelope(
                       [_entry(2, "2026-10-29 00:13:36")] * 3)}, fh)
    out, endpoints = _query(mod, tmp, AssertionError("fetched past the cache"))
    assert endpoints.urls() == [mod.ZAI_URL]
    assert out["_weekly_resets"]["available"] == 3
    assert "stale_age" not in out["_weekly_resets"]


def test_a_quota_served_stale_does_not_wait_on_the_reset_list(tmp):
    """When the quota fetch itself failed and the rows come from the stale
    cache, the API is unreachable; asking again for the reset list would
    only add a second timeout in front of rows that are ready."""
    mod = _load()
    quota_cache = os.path.join(tmp, "quota.json")
    with open(quota_cache, "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": time.time() - 600, "data": _zai_envelope()},
                  fh)
    key_file = os.path.join(tmp, "api-key")
    with open(key_file, "w", encoding="utf-8") as fh:
        fh.write("k" * 49 + "\n")
    urls = []

    def unreachable(url, headers):
        urls.append(url)
        raise TimeoutError("timed out")

    with _zai_sources(mod, files=(key_file,)), \
            mock.patch.object(mod, "ZAI_CACHE", quota_cache), \
            mock.patch.object(mod, "ZAI_RESETS_CACHE",
                              os.path.join(tmp, "resets.json")), \
            mock.patch.object(mod, "_get_retry", side_effect=unreachable), \
            _frozen_clock(mod, NOW):
        out = mod.query_zai()
    assert urls == [mod.ZAI_URL]
    assert out["_stale_age"] >= 595
    assert "_weekly_resets" not in out


def test_the_resets_annotation_follows_an_over_pace_flag(tmp):
    del tmp
    mod = _load()
    rows = mod._table_rows("z.ai", {
        "weekly": {"pct": 90.0, "pace_pct": 50.0, "recover_in": "2d",
                   "resets_at": "r", "resets_in": "1d"},
        "_weekly_resets": {"available": 2,
                           "earliest_expires_at": "2026-10-20 02:00"
                                                  + mod._TZ_NOTE,
                           "earliest_expires_in": "52d"},
    })
    # Its own trailing column, not joined into the flag: the flag column is
    # right-justified across every provider's rows, so a long note there
    # would push every other row's OVER PACE out to its width.
    assert rows[0][6] == "OVER PACE (on pace in 2d)"
    assert rows[0][7] == ("2 weekly resets available (earliest expires "
                          "2026-10-20 02:00, in 52d)")


def test_three_runs_on_a_fresh_quota_cache_ask_for_the_list_once(tmp):
    """The list is fetched only alongside a live quota fetch. A run answered
    from the quota cache used to make no network call, and must not start
    making one - least of all to an endpoint that hangs for the timeout."""
    mod = _load()
    urls = []
    for _ in range(3):
        _, endpoints = _query(mod, tmp, TimeoutError("timed out"))
        urls.extend(endpoints.urls())
    assert urls == [mod.ZAI_URL, mod.ZAI_RESETS_URL], urls

    # With the quota cache already fresh, no run touches the network.
    sub = os.path.join(tmp, "warm")
    os.makedirs(sub)
    with open(os.path.join(sub, "quota.json"), "w", encoding="utf-8") as fh:
        json.dump({"fetched_at": time.time(), "data": _zai_envelope()}, fh)
    urls = []
    for _ in range(3):
        _, endpoints = _query(mod, sub, TimeoutError("timed out"))
        urls.extend(endpoints.urls())
    assert urls == [], urls


# origin/main's campaign labels (1.4.1), the widest the window column has
# been; the short form must never exceed them.
ORIGIN_CAMPAIGN_LABELS = (
    "off-peak 0.5x · GLM-5.3-Flash campaign 2x quota until 09:00 UTC+8",
    "off-peak 0.5x · GLM-5.3-Flash campaign 2x quota from 23:00 UTC+8",
)


def _claude_rows(mod):
    return mod._table_rows("Claude", {
        "five_hour": {"pct": 80.0, "pace_pct": 40.0, "recover_in": "1h00m",
                      "resets_at": "2026-09-29 14:00", "resets_in": "2h"},
        "weekly": {"pct": 10.0, "pace_pct": 50.0, "recover_in": None,
                   "resets_at": "2026-10-02 09:00", "resets_in": "3d"},
    })


def _zai_result(mod, with_resets):
    note = mod._zai_peak_note()
    result = {
        "five_hour": {"pct": 5.0, "pace_pct": 50.0, "recover_in": None,
                      "resets_at": "2026-09-29 14:16", "resets_in": "3h01m",
                      "peak_note": note},
        "weekly": {"pct": 90.0, "pace_pct": 10.0, "recover_in": "16h37m",
                   "resets_at": "2026-10-05 18:15", "resets_in": "6d7h",
                   "peak_note": note},
    }
    if with_resets:
        result["_weekly_resets"] = {
            "available": 4,
            "earliest_expires_at": "2026-10-28 17:13" + mod._TZ_NOTE,
            "earliest_expires_in": "29d6h"}
    return result


def _other_lines(mod, zai):
    claude = _claude_rows(mod)
    lines = mod._render_table(claude + mod._table_rows("z.ai", zai)) \
        .splitlines()
    return lines[1:1 + len(claude)]


def test_other_providers_rows_keep_their_width(tmp):
    """z.ai's annotations share the table with every other provider, so they
    must not widen anyone else's rows. Outside the campaign the other rows are
    byte-identical with or without the resets note; during it they are never
    wider than under origin/main's campaign label."""
    del tmp
    mod = _load()
    with _frozen_clock(mod, datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)):
        bare = _other_lines(mod, _zai_result(mod, with_resets=False))
        noted = _other_lines(mod, _zai_result(mod, with_resets=True))
    assert noted == bare, (noted, bare)

    for moment in (datetime(2026, 9, 29, 16, 30, tzinfo=timezone.utc),
                   datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc),
                   datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)):
        with _frozen_clock(mod, moment):
            new = _other_lines(mod, _zai_result(mod, with_resets=True))
            for label in ORIGIN_CAMPAIGN_LABELS:
                origin = _zai_result(mod, with_resets=False)
                for window in ("five_hour", "weekly"):
                    origin[window]["peak_note"] = label
                old = _other_lines(mod, origin)
                for new_line, old_line in zip(new, old):
                    assert new_line.split() == old_line.split(), moment
                    assert len(new_line) <= len(old_line), (
                        moment, new_line, old_line)


def main():
    return _util.runner(_util.collect(globals()),
                        tmp_prefix="usagequeryweeklyresets_")


if __name__ == "__main__":
    raise SystemExit(main())
