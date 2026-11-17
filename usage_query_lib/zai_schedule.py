"""The printed z.ai peak schedule, rendered from the policy values passed in."""
from datetime import datetime, timezone

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def day_runs(days) -> str:
    """Weekday numbers (datetime.weekday(), Mon=0) as 'Mon-Wed,Fri': each
    maximal run of consecutive days reads First-Last, a single day alone."""
    runs: list[list[int]] = []
    for day in sorted(set(days)):
        if runs and runs[-1][1] == day - 1:
            runs[-1][1] = day
        else:
            runs.append([day, day])
    return ",".join(DAY_NAMES[first] if first == last
                    else f"{DAY_NAMES[first]}-{DAY_NAMES[last]}"
                    for first, last in runs)


def zone_label(zone: timezone) -> str:
    """'UTC+8', or 'UTC+5:30' when the offset has minutes."""
    offset = int(zone.utcoffset(None).total_seconds()) // 60
    hours, minutes = divmod(abs(offset), 60)
    sign = "-" if offset < 0 else "+"
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def zone_time(moment: datetime, zone: timezone) -> str:
    """`moment` as '2026-01-05 18:45 UTC+8' in `zone`."""
    return f"{moment.astimezone(zone).strftime('%Y-%m-%d %H:%M')} {zone_label(zone)}"


def hours(days, start_min: int, end_min: int, zone: timezone) -> dict[str, str]:
    """The peak_hours and off_peak_hours of a [start_min, end_min) window."""
    start = f"{start_min // 60:02d}:{start_min % 60:02d}"
    end = f"{end_min // 60:02d}:{end_min % 60:02d}"
    on, off, label = day_runs(days), day_runs(set(range(7)) - set(days)), zone_label(zone)
    return {"peak_hours": f"{on} {start}-{end} {label}",
            "off_peak_hours": f"{on} 00:00-{start} and {end}-24:00 {label}" + (f"; all day {off}" if off else "")}
