"""Codex's credit balance: a spent plan window is not an exhausted lane.

The usage endpoint answers with the plan window AND a `credits` block. An
account whose window is at 100% can still be served from its credit balance,
so a reading that reports only the window tells a dispatcher the lane is dead
while Codex keeps answering.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _util  # noqa: E402
from test_banked_resets_display import _main_text  # noqa: E402
from test_usage_query_oauth import _wham_payload  # noqa: E402

USAGE_QUERY = _util.script("query.py")

# The credits and spend-control blocks as the endpoint returned them live.
_CREDITS = {"has_credits": True, "unlimited": False,
            "overage_limit_reached": False, "balance": "62378.5917775000",
            "approx_local_messages": [15595, 81092],
            "approx_cloud_messages": [2495, 15595]}


def _spent_payload(**credits):
    payload = _wham_payload()
    payload["rate_limit"].update(allowed=False, limit_reached=True)
    payload["rate_limit"]["primary_window"]["used_percent"] = 100
    payload["credits"] = dict(_CREDITS, **credits)
    payload["spend_control"] = {"reached": False, "individual_limit": None}
    return payload


def _result(mod, payload):
    return mod._normalize_codex(mod._codex_rpc_shape(payload))


def test_the_rpc_shape_carries_credits_and_spend_control(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_credits_shape")
    got = mod._codex_rpc_shape(_spent_payload())
    assert got["credits"] == {
        "hasCredits": True, "unlimited": False, "overageLimitReached": False,
        "balance": "62378.5917775000",
        "approxLocalMessages": [15595, 81092],
        "approxCloudMessages": [2495, 15595]}, got.get("credits")
    assert got["spendControlReached"] is False


def test_usable_credits_are_public_and_numeric(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_credits_public")
    out = _result(mod, _spent_payload())
    assert out["weekly"]["pct"] == 100
    assert out["credits"] == {
        "has_credits": True, "unlimited": False,
        "overage_limit_reached": False, "spend_control_reached": False,
        "balance": 62378.5917775,
        "approx_local_messages": [15595, 81092],
        "approx_cloud_messages": [2495, 15595],
        "usable": True}, out["credits"]


def test_each_limit_alone_makes_credits_unusable(tmp):
    """One isolating case per conjunct: no credits, overage limit reached,
    spend control reached. Unlimited credits are usable without a balance."""
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_credits_limbs")
    assert _result(mod, _spent_payload(has_credits=False))["credits"]["usable"] is False
    assert _result(mod, _spent_payload(overage_limit_reached=True))["credits"]["usable"] is False
    payload = _spent_payload()
    payload["spend_control"]["reached"] = True
    assert _result(mod, payload)["credits"]["usable"] is False
    unlimited = _spent_payload(has_credits=False, unlimited=True, balance=None)
    got = _result(mod, unlimited)["credits"]
    assert got["usable"] is True and got["balance"] is None, got


def test_a_payload_without_credits_adds_nothing(tmp):
    """A cache written before credits were read, or an account the backend
    reports no credits block for, keeps the output it always had."""
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_credits_absent")
    out = _result(mod, _wham_payload())
    assert "credits" not in out
    text = _main_text(mod, codex=out)
    assert "credits" not in text.lower(), text


def test_json_reports_the_balance(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_credits_json")
    data = json.loads(_main_text(mod, codex=_result(mod, _spent_payload()),
                                 argv=("--json",)))
    credits = data["usage"]["codex"]["credits"]
    assert credits["balance"] == 62378.5917775
    assert credits["usable"] is True


def _codex_rows(text):
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Codex"))
    rows = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith(" "):
            break
        rows.append(line)
    return rows


def test_a_spent_window_served_by_credits_says_so(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_credits_text")
    text = _main_text(mod, codex=_result(mod, _spent_payload()))
    weekly = next(r for r in _codex_rows(text) if " 7d " in r)
    assert "SPENT (credits serving)" in weekly, text
    assert "OVER PACE" not in weekly, text
    assert ("Codex credits: balance 62379 (~15595-81092 local / "
            "~2495-15595 cloud messages); usable past a spent window") in text, text


def test_a_spent_window_without_usable_credits_reads_spent(tmp):
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_credits_text_dead")
    text = _main_text(mod, codex=_result(
        mod, _spent_payload(overage_limit_reached=True)))
    weekly = next(r for r in _codex_rows(text) if " 7d " in r)
    assert "SPENT" in weekly and "credits serving" not in weekly, text
    assert ("Codex credits: balance 62379 (~15595-81092 local / "
            "~2495-15595 cloud messages); not usable: overage limit "
            "reached") in text, text


def test_a_window_under_100_keeps_its_pace_flag(tmp):
    """Credits change nothing about a window that is not spent."""
    del tmp
    mod = _util.load(USAGE_QUERY, "usage_query_credits_unspent")
    payload = _spent_payload()
    payload["rate_limit"]["primary_window"]["used_percent"] = 95
    text = _main_text(mod, codex=_result(mod, payload))
    weekly = next(r for r in _codex_rows(text) if " 7d " in r)
    assert "SPENT" not in weekly, text


def main():
    return _util.runner(_util.collect(globals()), "usagequerycredits_")


if __name__ == "__main__":
    raise SystemExit(main())
