#!/usr/bin/env python3
"""The z.ai billing state, schedule and line follow window, days and zone."""
import os
from datetime import date, datetime, timedelta, timezone
from unittest import mock
import _util

MOVED = {"ZAI_PEAK_START_MIN": 14 * 60 + 30, "ZAI_PEAK_END_MIN": 18 * 60 + 45}
HOURS = ("Mon-Fri 14:00-18:00 UTC+8", "Mon-Fri 00:00-14:00 and 18:00-24:00 UTC+8; all day Sat-Sun")
MOVED_HOURS = ("Mon-Fri 14:30-18:45 UTC+8", "Mon-Fri 00:00-14:30 and 18:45-24:00 UTC+8; all day Sat-Sun")
PROMO = "; all-day off-peak promotion until 2026-10-08 00:00 UTC+8"
IST = {"ZAI_TZ": timezone(timedelta(hours=5, minutes=30))}
IST_HOURS = ("Mon-Fri 14:00-18:00 UTC+5:30", "Mon-Fri 00:00-14:00 and 18:00-24:00 UTC+5:30; all day Sat-Sun")

# Patches, moment, state, (peak, off-peak) hours, next at and in, promo clause;
# the campaign clause, pinned in the behaviour suite, is kept off every line.
CASES = [
    ({}, "2026-01-05T05:00+00:00", "off-peak", HOURS, "2026-01-05T14:00:00+08:00", "1h00m", ""),
    ({}, "2026-01-05T06:00+00:00", "peak", HOURS, "2026-01-05T18:00:00+08:00", "4h00m", ""),
    ({}, "2026-01-05T10:00+00:00", "off-peak", HOURS, "2026-01-06T14:00:00+08:00", "20h00m", ""),
    ({}, "2026-01-09T10:00+00:00", "off-peak", HOURS, "2026-01-12T14:00:00+08:00", "2d20h", ""),
    ({}, "2026-01-10T15:00+00:00", "off-peak", HOURS, "2026-01-12T14:00:00+08:00", "1d15h", ""),
    # The promotion [2026-09-25, 2026-10-08) UTC+8 bills 0.5x and hides peaks.
    ({}, "2026-09-24T07:00+00:00", "peak", HOURS, "2026-09-24T18:00:00+08:00", "3h00m", ""),
    ({}, "2026-09-24T10:00+00:00", "off-peak", HOURS, "2026-10-08T14:00:00+08:00", "13d20h", ""),
    ({}, "2026-09-25T07:00+00:00", "off-peak", HOURS, "2026-10-08T14:00:00+08:00", "12d23h", PROMO),
    ({}, "2026-09-28T07:00+00:00", "off-peak", HOURS, "2026-10-08T14:00:00+08:00", "9d23h", PROMO),
    ({}, "2026-10-07T15:59:59+00:00", "off-peak", HOURS, "2026-10-08T14:00:00+08:00", "14h00m", PROMO),
    ({}, "2026-10-08T06:00+00:00", "peak", HOURS, "2026-10-08T18:00:00+08:00", "4h00m", ""),
    # A window off the hour, each boundary with its near misses.
    (MOVED, "2026-01-05T18:15+08:00", "peak", MOVED_HOURS, "2026-01-05T18:45:00+08:00", "30m", ""),
    (MOVED, "2026-01-05T14:15+08:00", "off-peak", MOVED_HOURS, "2026-01-05T14:30:00+08:00", "15m", ""),
    (MOVED, "2026-01-05T19:00+08:00", "off-peak", MOVED_HOURS, "2026-01-06T14:30:00+08:00", "19h30m", ""),
    (MOVED, "2026-01-09T19:00+08:00", "off-peak", MOVED_HOURS, "2026-01-12T14:30:00+08:00", "2d19h", ""),
    (MOVED, "2026-01-10T12:00+08:00", "off-peak", MOVED_HOURS, "2026-01-12T14:30:00+08:00", "2d2h", ""),
    (MOVED, "2026-01-05T14:29+08:00", "off-peak", MOVED_HOURS, "2026-01-05T14:30:00+08:00", "1m", ""),
    (MOVED, "2026-01-05T14:30+08:00", "peak", MOVED_HOURS, "2026-01-05T18:45:00+08:00", "4h15m", ""),
    (MOVED, "2026-01-05T14:31+08:00", "peak", MOVED_HOURS, "2026-01-05T18:45:00+08:00", "4h14m", ""),
    (MOVED, "2026-01-05T18:44+08:00", "peak", MOVED_HOURS, "2026-01-05T18:45:00+08:00", "1m", ""),
    (MOVED, "2026-01-05T18:45+08:00", "off-peak", MOVED_HOURS, "2026-01-06T14:30:00+08:00", "19h45m", ""),
    (MOVED, "2026-01-05T18:46+08:00", "off-peak", MOVED_HOURS, "2026-01-06T14:30:00+08:00", "19h44m", ""),
    (MOVED, "2026-09-24T19:00+08:00", "off-peak", MOVED_HOURS, "2026-10-08T14:30:00+08:00", "13d19h", ""),
    (MOVED, "2026-09-28T15:00+08:00", "off-peak", MOVED_HOURS, "2026-10-08T14:30:00+08:00", "9d23h", PROMO),
    ({"ZAI_PEAK_START_MIN": 9 * 60 + 5, "ZAI_PEAK_END_MIN": 9 * 60 + 50}, "2026-01-05T09:30+08:00", "peak", ("Mon-Fri 09:05-09:50 UTC+8", "Mon-Fri 00:00-09:05 and 09:50-24:00 UTC+8; all day Sat-Sun"), "2026-01-05T09:50:00+08:00", "20m", ""),
    ({"ZAI_PEAK_DAYS": (0, 1, 2, 3, 4, 5)}, "2026-01-10T15:00+08:00", "peak", ("Mon-Sat 14:00-18:00 UTC+8", "Mon-Sat 00:00-14:00 and 18:00-24:00 UTC+8; all day Sun"), "2026-01-10T18:00:00+08:00", "3h00m", ""),
    ({"ZAI_PEAK_DAYS": (0, 1, 2, 4)}, "2026-01-08T15:00+08:00", "off-peak", ("Mon-Wed,Fri 14:00-18:00 UTC+8", "Mon-Wed,Fri 00:00-14:00 and 18:00-24:00 UTC+8; all day Thu,Sat-Sun"), "2026-01-09T14:00:00+08:00", "23h00m", ""),
    ({"ZAI_PEAK_DAYS": (0, 2, 3, 6)}, "2026-01-11T15:00+08:00", "peak", ("Mon,Wed-Thu,Sun 14:00-18:00 UTC+8", "Mon,Wed-Thu,Sun 00:00-14:00 and 18:00-24:00 UTC+8; all day Tue,Fri-Sat"), "2026-01-11T18:00:00+08:00", "3h00m", ""),
    ({"ZAI_PEAK_DAYS": (1, 2, 3, 4, 5)}, "2026-01-05T15:00+08:00", "off-peak", ("Tue-Sat 14:00-18:00 UTC+8", "Tue-Sat 00:00-14:00 and 18:00-24:00 UTC+8; all day Mon,Sun"), "2026-01-06T14:00:00+08:00", "23h00m", ""),
    ({"ZAI_PEAK_DAYS": (6, 0, 5, 0)}, "2026-01-06T12:00+08:00", "off-peak", ("Mon,Sat-Sun 14:00-18:00 UTC+8", "Mon,Sat-Sun 00:00-14:00 and 18:00-24:00 UTC+8; all day Tue-Fri"), "2026-01-10T14:00:00+08:00", "4d2h", ""),
    ({"ZAI_PEAK_DAYS": (0, 1, 2, 3, 4, 5, 6)}, "2026-01-11T15:00+08:00", "peak", ("Mon-Sun 14:00-18:00 UTC+8", "Mon-Sun 00:00-14:00 and 18:00-24:00 UTC+8"), "2026-01-11T18:00:00+08:00", "3h00m", ""),
    # Zones: each moment is peak in one of the patched zone and UTC+8 only.
    (IST, "2026-01-05T06:00+00:00", "off-peak", IST_HOURS, "2026-01-05T14:00:00+05:30", "2h30m", ""),
    ({"ZAI_TZ": timezone(-timedelta(hours=3, minutes=30))}, "2026-01-05T18:00+00:00", "peak", ("Mon-Fri 14:00-18:00 UTC-3:30", "Mon-Fri 00:00-14:00 and 18:00-24:00 UTC-3:30; all day Sat-Sun"), "2026-01-05T18:00:00-03:30", "3h30m", ""),
    ({"ZAI_TZ": timezone.utc}, "2026-01-05T15:00+00:00", "peak", ("Mon-Fri 14:00-18:00 UTC+0", "Mon-Fri 00:00-14:00 and 18:00-24:00 UTC+0; all day Sat-Sun"), "2026-01-05T18:00:00+00:00", "3h00m", ""),
    (IST, "2026-09-28T07:00+00:00", "off-peak", IST_HOURS, "2026-10-08T14:00:00+05:30", "10d1h", "; all-day off-peak promotion until 2026-10-07 21:30 UTC+5:30"),
]


def test_state_schedule_and_billing_line_follow_the_constants(tmp):
    mod = _util.load(_util.script("query.py"), "usage_query_zai_schedule")
    assert os.path.samefile(mod.zai_schedule.__file__, _util.script("zai_schedule.py")), "not this checkout's zai_schedule"
    wrong = []
    for patches, moment, state, hours, at, within, promo in CASES:
        with mock.patch.multiple(mod, ZAI_CAMPAIGN_LAST_NIGHT=date(2026, 9, 3), **patches):
            status = mod._zai_billing_status(datetime.fromisoformat(moment))
            note = mod._zai_billing_note(status)
        peak, nxt = state == "peak", "off-peak" if state == "peak" else "peak"
        want = ({"state": state, "multiplier": 1.0 if peak else 0.5, "peak_hours": hours[0], "off_peak_hours": hours[1], "next_transition": {"state": nxt, "at": at, "in": within}},
                f"z.ai billing: {state} {1 if peak else 0.5}x now; peak {hours[0]}; off-peak {hours[1]}; next {nxt} starts {at[:10]} {at[11:16]} {hours[0].rsplit(' ', 1)[1]} (in {within}){promo}.", True)
        if ({key: status[key] for key in want[0]}, note, status["promotion"]["active"] is bool(promo)) != want:
            wrong.append((moment, note))
    assert not wrong, wrong


def main():
    return _util.runner(_util.collect(globals()), tmp_prefix="usagequeryzaischedule_")


if __name__ == "__main__":
    raise SystemExit(main())
