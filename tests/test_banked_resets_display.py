#!/usr/bin/env python3
"""Banked resets render the same way for every provider.

z.ai's weekly resets and Codex's rate-limit reset credits are the same thing
-- a credit the holder spends deliberately -- so they share one annotation
shape, one footer hint shape, and one public JSON shape. How each provider
FETCHES its block is covered elsewhere (test_zai_weekly_resets,
test_usage_query_oauth); this suite is about how the blocks RENDER.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402
from test_usage_query_behavior import _frozen_clock, _load  # noqa: E402
from test_zai_weekly_resets import (  # noqa: E402
    _entry, _local, _query, _reset_envelope)


# Codex's expiry below (2026-10-29 19:56 UTC) read from this moment, so the
# assertions can name a fixed duration.
FROZEN = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


def _at(moment):
    """An aware UTC moment rendered the way the table renders resets."""
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


# One available "Full reset" credit, as the credit endpoint spells it.
_FULL_RESET = [{"id": "cred_1", "title": "Full reset",
                "status": "available",
                "expiresAt": "2026-10-29T19:56:00Z"}]


def _main_text(mod, codex=None, zai=None, argv=()):
    """main(argv) rendered as text (or JSON) with the provider results pinned.

    Providers not handed a result stay not-configured, which a default sweep
    reports as unconfigured -- a silent exit -- rather than as an error."""
    absent = mod.ProviderNotConfigured("not configured on this machine")

    def pinned(value):
        def call():
            if value is None:
                raise absent
            return value
        return call

    with mock.patch.object(mod, "query_claude", side_effect=pinned(None)), \
            mock.patch.object(mod, "query_kimi", side_effect=pinned(None)), \
            mock.patch.object(mod, "query_codex", side_effect=pinned(codex)), \
            mock.patch.object(mod, "query_zai", side_effect=pinned(zai)):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = mod.main(list(argv))
    assert rc == 0, out.getvalue()
    return out.getvalue()


def _codex_result(mod, count=None, credit_rows=None, five_hour=True):
    """A Codex result that went through the real normalizer, so the public
    banked_resets key is present exactly as a live query would carry it."""
    shape = {"rateLimits": {
        "secondary": {"usedPercent": 73.0, "resetsAt": 1799058900,
                      "windowDurationMins": 10080}}}
    if five_hour:
        shape["rateLimits"]["primary"] = {
            "usedPercent": 10.0, "resetsAt": 1798809600,
            "windowDurationMins": 300}
    if count is not None:
        summary = {"availableCount": count}
        if credit_rows is not None:
            summary["credits"] = list(credit_rows)
        shape["rateLimitResetCredits"] = summary
    return mod._normalize_codex(shape)


def _zai_result(mod, available=None, stale_age=None):
    """A z.ai-shaped result for the rendering half of main(). The annotation
    and the footer read _weekly_resets, which is theirs to carry."""
    res = {"five_hour": {"pct": 5.0, "pace_pct": 18.0, "recover_in": None,
                         "resets_at": "r", "resets_in": "4h"},
           "weekly": {"pct": 59.0, "pace_pct": 30.0, "recover_in": "2d1h",
                      "resets_at": "r", "resets_in": "1d22h"}}
    if available is not None:
        block = {"available": available,
                 "earliest_expires_at": "2026-10-28 17:13" + mod._TZ_NOTE,
                 "earliest_expires_in": "27d22h"}
        if stale_age:
            block["stale_age"] = stale_age
        res["_weekly_resets"] = block
    return res


def test_identical_annotation_for_a_zai_block_and_a_codex_block(tmp):
    """One shape for every provider: same count and expiry, same words."""
    del tmp
    mod = _load()
    at = "2026-10-29 19:56" + mod._TZ_NOTE
    zai = mod._table_rows("z.ai", _zai_result(mod) | {
        "_weekly_resets": {"available": 2, "earliest_expires_at": at,
                           "earliest_expires_in": "29d1h"}})
    codex = mod._table_rows("Codex", {
        "five_hour": {"pct": 10.0, "pace_pct": 50.0, "recover_in": None,
                      "resets_at": "r", "resets_in": "1h"},
        "weekly": {"pct": 73.0, "pace_pct": 58.0, "recover_in": "1d1h",
                   "resets_at": "r", "resets_in": "2d23h"},
        "_reset_credits_available": 2,
        "_reset_credits": [{"id": "c1", "title": "Full reset",
                            "expires_at": at, "expires_in": "29d1h"}]})
    # z.ai's annotation rides the 7d weekly row; Codex's rides the first
    # whole-account row, the one carrying the account name. Identical text.
    assert zai[1][7] == codex[0][7] == (
        "2 banked resets available (earliest expires 2026-10-29 19:56, "
        "in 29d1h)")
    assert zai[0][7] == ""  # the 5h row carries no annotation
    assert codex[1][7] == ""  # and no other row carries it either


def test_codex_annotation_rides_the_first_row_and_the_old_footer_is_gone(tmp):
    """A Full reset restores every window, so the annotation rides the first
    whole-account row -- the one carrying the account name -- and the old
    'Codex banked resets:' line is gone."""
    del tmp
    mod = _load()
    with _frozen_clock(mod, FROZEN):
        codex = _codex_result(
            mod, count=1,
            credit_rows=_FULL_RESET)
        text = _main_text(mod, codex=codex)
    first = next(line for line in text.splitlines()
                 if line.startswith("Codex"))
    assert first.rstrip().startswith("Codex"), text
    assert ("1 banked reset available (earliest expires "
            f"{_at(datetime(2026, 10, 29, 19, 56, tzinfo=timezone.utc))}, "
            "in 29d7h)") in first, text
    assert "Codex banked resets:" not in text
    assert "account/rateLimitResetCredit/consume" in text


def test_the_spend_hint_prints_once_per_provider(tmp):
    """One footer line per provider that has any available, in one shape:
    what it says about spending is the only per-provider part."""
    del tmp
    mod = _load()
    codex = _codex_result(
        mod, count=1,
        credit_rows=_FULL_RESET)
    text = _main_text(mod, codex=codex, zai=_zai_result(mod, available=4))
    assert text.count(
        "Codex banked resets are only read here; spend one deliberately: "
        "the app-server RPC account/rateLimitResetCredit/consume") == 1
    assert text.count(
        "z.ai banked resets are only read here; spend one deliberately: "
        "through the z.ai console") == 1


def test_banked_resets_json_carries_both_providers(tmp):
    """One public shape beside the private keys, which stay for the tools
    already reading them."""
    del tmp
    mod = _load()
    codex = _codex_result(
        mod, count=1,
        credit_rows=_FULL_RESET)
    text = _main_text(mod, codex=codex, zai=_zai_result(mod, available=4),
                      argv=["--json"])
    usage = json.loads(text)["usage"]
    assert usage["codex"]["banked_resets"] == {
        "available": 1,
        "earliest_expires_at": usage["codex"]["_reset_credits"][0][
            "expires_at"],
        "earliest_expires_in": usage["codex"]["_reset_credits"][0][
            "expires_in"],
        "windows": ["five_hour", "weekly"]}
    assert usage["codex"]["_reset_credits_available"] == 1
    assert usage["codex"]["_reset_credits"][0]["title"] == "Full reset"
    # The mocked z.ai result bypasses query_zai, so its public key is absent
    # here; the real pipeline adds it, and the next test proves that.
    assert usage["zai"]["_weekly_resets"]["available"] == 4
    assert "banked_resets" not in usage["zai"]


def test_zai_json_carries_banked_resets_from_the_real_envelope(tmp):
    """query_zai adds the public key beside _weekly_resets, windows naming the
    row the reset restores."""
    mod = _load()
    resets = _reset_envelope([_entry(2, "2026-10-29 00:13:36")])
    text, _ = _query(mod, tmp, resets, argv=["--zai", "--json"])
    zai = json.loads(text)["usage"]["zai"]
    assert zai["banked_resets"] == {
        "available": 1,
        "earliest_expires_at": zai["_weekly_resets"]["earliest_expires_at"],
        "earliest_expires_in": "61d4h",
        "windows": ["weekly"]}
    assert zai["banked_resets"]["earliest_expires_at"].startswith(
        _local("2026-10-29 00:13:36"))
    # The private block is untouched beside it.
    assert zai["_weekly_resets"]["available"] == 1


def test_zero_available_is_silent_and_json_carries_no_key(tmp):
    """Nothing to spend is not news: no annotation, no footer, and no public
    key -- while the private blocks still say 0."""
    del tmp
    mod = _load()
    text = _main_text(mod,
                      codex=_codex_result(mod, count=0),
                      zai=_zai_result(mod, available=0))
    assert "banked" not in text
    json_text = _main_text(mod,
                           codex=_codex_result(mod, count=0),
                           zai=_zai_result(mod, available=0),
                           argv=["--json"])
    usage = json.loads(json_text)["usage"]
    assert "banked_resets" not in usage["codex"]
    assert "banked_resets" not in usage["zai"]
    # The private blocks still answer the question.
    assert usage["codex"]["_reset_credits_available"] == 0
    assert usage["zai"]["_weekly_resets"]["available"] == 0


def test_a_credit_without_an_expiry_reads_does_not_expire(tmp):
    """A credit the backend sent with no expiry never expires; the
    annotation says so in the shared parenthetical shape."""
    del tmp
    mod = _load()
    codex = _codex_result(mod, count=1, credit_rows=[
        {"id": "cred_1", "title": "Full reset", "status": "available",
         "expiresAt": None}])
    text = _main_text(mod, codex=codex)
    first = next(line for line in text.splitlines()
                 if line.startswith("Codex"))
    assert "1 banked reset available (does not expire)" in first, text
    payload = json.loads(_main_text(mod, codex=codex, argv=["--json"]))
    assert payload["usage"]["codex"]["banked_resets"] == {
        "available": 1, "earliest_expires_at": None,
        "earliest_expires_in": None, "windows": ["five_hour", "weekly"]}


def test_a_count_without_detail_prints_a_bare_note(tmp):
    """The app-server omits the per-credit detail on its periodic refresh, so
    a count with no named credits reads without a parenthetical -- it cannot
    claim an expiry it was never told."""
    del tmp
    mod = _load()
    codex = _codex_result(mod, count=1)
    text = _main_text(mod, codex=codex)
    first = next(line for line in text.splitlines()
                 if line.startswith("Codex"))
    assert "1 banked reset available" in first, text
    assert "(" not in first.split("available", 1)[1], text
    payload = json.loads(_main_text(mod, codex=codex, argv=["--json"]))
    assert payload["usage"]["codex"]["banked_resets"]["earliest_expires_at"] \
        is None


def test_zai_annotation_keeps_the_cached_age(tmp):
    """The reset list has a cache of its own; a note answered from an old one
    says so, as it always has."""
    del tmp
    mod = _load()
    res = _zai_result(mod, available=2, stale_age=600)
    rows = mod._table_rows("z.ai", res)
    assert rows[1][7] == ("2 banked resets available (earliest expires "
                          "2026-10-28 17:13, in 27d22h; cached 600s)")


def test_codex_annotates_the_weekly_row_when_no_five_hour_exists(tmp):
    """A Codex account can carry only a weekly window (the live pro plan has
    looked exactly like this); 'first whole-account row' follows the rows,
    and the JSON windows list what is present."""
    del tmp
    mod = _load()
    with _frozen_clock(mod, FROZEN):
        codex = _codex_result(
            mod, count=1, five_hour=False,
            credit_rows=_FULL_RESET)
        rows = mod._table_rows("Codex", codex)
    assert len(rows) == 1
    assert rows[0][1] == "7d"
    assert rows[0][0] == "Codex"
    assert rows[0][7] == ("1 banked reset available (earliest expires "
                          f"{_at(datetime(2026, 10, 29, 19, 56, tzinfo=timezone.utc))}, "
                          "in 29d7h)")
    assert codex["banked_resets"]["windows"] == ["weekly"]


def test_claude_and_kimi_report_no_banked_resets(tmp):
    """Providers without the concept stay untouched: a result carrying only
    the shared window keys renders no annotation and no footer."""
    del tmp
    mod = _load()
    bare = {"five_hour": {"pct": 6.0, "pace_pct": 84.0, "recover_in": None,
                          "resets_at": "r", "resets_in": "49m"}}
    for name in ("Claude", "Kimi"):
        rows = mod._table_rows(name, bare)
        assert all(row[7] == "" for row in rows), (name, rows)
    text = _main_text(mod)
    assert "banked" not in text


def main():
    return _util.runner(_util.collect(globals()),
                        tmp_prefix="usagequerybankeddisplay_")


if __name__ == "__main__":
    raise SystemExit(main())
