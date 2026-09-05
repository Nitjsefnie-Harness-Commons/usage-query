#!/usr/bin/env python3
"""Standalone ``usage-query`` command for Claude, Kimi-Code, Codex, and z.ai.

This public distribution provides a direct, on-demand companion to the
``usage-monitor.py`` PostToolUse hook: that hook announces usage passively on
10%-band crossings; THIS command lets you ask "what's my usage right now?" from
the shell or a tool call, on demand. It can be installed as the
``usage-query`` console script or imported as ``usage_query_lib.query``.

Sources (same endpoints/credentials the hook uses):
  - Claude: GET https://api.anthropic.com/api/oauth/usage
            bearer = ~/.claude/.credentials.json -> claudeAiOauth.accessToken
            header anthropic-beta: oauth-2025-04-20
            5h window  = five_hour.utilization  (+ resets_at)
            weekly     = seven_day.utilization  (+ resets_at)
  - Kimi:   GET https://api.kimi.com/coding/v1/usages
            bearer = ~/.kimi-code/credentials/kimi-code.json -> access_token
            5h window  = limits[] entry with window.duration==300 MINUTE (used/limit)
            weekly     = top-level usage block (used/limit)
  - z.ai:   GET https://api.z.ai/api/monitor/usage/quota/limit
            auth   = RAW key, no Bearer prefix; resolved in the same order the
                     bundle's spawn_zai launchers resolve it: $ZAI_API_KEY,
                     ~/.config/claude-zai/api-key, the copy shipped beside those
                     launchers, then ~/.pi/agent/auth.json -> zai.key
            5h window  = limits[] with unit 3 (hours) x number 5
            weekly     = limits[] with unit 6 (weeks) x number 1
            each window line is tagged with the peak/off-peak billing state
  - Codex:  GET https://chatgpt.com/backend-api/wham/usage
            bearer = $CODEX_HOME/auth.json -> tokens.access_token
            header ChatGPT-Account-Id: tokens.account_id
            windows      = every primary/secondary window in every limit bucket

CREDENTIAL DISCIPLINE: for the Claude, Kimi AND Codex files, when the stored
access token is stale we refresh on demand and PERSIST the rotated result, the
same way the official clients (Claude Code / kimi-cli / codex) do.

The Kimi-Code refresh_token is SINGLE-USE / rotated server-side: calling the
refresh grant invalidates the refresh_token you sent and returns a NEW one
alongside the fresh access_token. So a refresh MUST write the rotated tokens back
to the credential file — otherwise the consumed refresh_token stays on disk and the
very next refresh (by this script OR by kimi itself) fails with invalid_grant. An
earlier version of this script refreshed in memory and did NOT write back, claiming
the refresh_token was "reusable"; that was wrong and it is exactly what broke the
chain (confirmed 2026-06-11 by an invalid_grant on a refresh_token whose own JWT
exp was still ~29 days out — server-side rotation, not expiry).

So: if the stored access token is still valid we use it as-is (no network); if it
is stale we refresh from the refresh_token and atomically rewrite the credential
file (temp + os.replace, 0600 preserved) with the rotated access_token +
refresh_token + new expiry — identical to what kimi writes on its own runs, so the
file always holds a live refresh token for whoever reads it next. The atomic
rewrite is also what keeps a concurrent kimi run from ever reading a torn file.
Not refreshing here is not a safe default: the Kimi access token lives ~15 min
(expires_in 900), so a read-only query would fail every time unless an unrelated
kimi session happened to run in the last quarter hour.

Codex is read the same way, and deliberately WITHOUT starting `codex app-server`.
Booting the app-server to answer one question costs a full CLI start — marketplace
refreshes with their own git fetches, and on Windows a console window that pops up
in front of whoever is working — and a status line asking every 30 s paid that
once a minute. So this reads $CODEX_HOME/auth.json (auth_mode, tokens{id_token,
access_token, refresh_token, account_id}, last_refresh) and calls the same backend
endpoint the app-server's account/rateLimits/read ends up calling, then maps the
reply into that method's response shape so the cache file and the normalizer are
unchanged. The Codex access token is a JWT whose own `exp` claim decides staleness
(~10 days); when it is stale — or the call answers 401 — we run Codex's refresh
grant against auth.openai.com/oauth/token with its public client id and atomically
rewrite auth.json (temp + os.replace, permissions preserved) with the rotated
id/access/refresh tokens and a fresh last_refresh, exactly the set codex writes
itself. An API-key login has no subscription quota windows and says so.

The Claude side works the same way and for the same reason: its OAuth access token
(in ~/.claude/.credentials.json -> claudeAiOauth, expiresAt in ms) lasts ~8h and
the harness refreshes it during active use, but after a long idle it can be stale,
so a manual run would 401. Anthropic ALSO rotates the Claude refresh_token
(single-use, verified 2026-06-11), so when stale we refresh against
api.anthropic.com/v1/oauth/token (NOT platform.claude.com — that host is
Cloudflare-WAF-gated and 1010-blocks a bare urllib request) with the SDK
User-Agent, then atomically persist the rotated set back, exactly as Claude Code
itself does. The harness reads this file as its source of truth, so a valid fresh
set keeps it working.

Pace ceiling: alongside each utilization %, the output shows the MAX % you could
be at right now under a constant, linear burn across the whole window — i.e. the
fraction of the window already elapsed. Stay at/below it and the quota lasts to
the reset; exceed it ("OVER PACE") and you are ahead of a linear burn and will
exhaust the window early. For an OVER PACE window the flag also carries
"on pace in T" — the coast-to-on-pace time: if all burn STOPS now, how long
until the rising linear pace line catches back up to the current utilization
(always before the reset, since utilization <= 100%). This is exactly the
difference from the passive hook: the hook reports where you ARE, this also
reports the max where you SHOULD be and how long a pause takes to get there.
Window lengths: every window is a fixed window that resets at its reset timestamp
(Kimi's work the same as Claude's). Claude 5h & weekly are fixed by key name;
Kimi's 5h comes from its window.duration; Kimi's longer "usage" block is the
weekly quota and is a fixed 7-day window (KIMI_WEEKLY_SECS), its payload just
omits the duration field. If Kimi ever adds a `window` to that block, the derived
value wins over the 7-day default.

Flags:
  --claude        only query Claude
  --kimi          only query Kimi
  --codex         only query Codex
  --zai           only query z.ai
  (default)       query all four
  --json          emit a JSON object instead of human-readable lines
  --quiet         suppress per-account error lines (still exits non-zero on
                  any requested-account failure)

Exit status: 0 if every requested account was queried successfully; 1 if any
requested account failed (missing creds, HTTP error, unexpected payload).

Cross-platform: stdlib urllib only (no curl). Reset timestamps are rendered in
the machine's own timezone and labelled "machine-local ... no tz conversion" so
they can be fed straight into local-time consumers (cron etc.) without manual
UTC->local conversion — the same convention as the hook.
"""
import argparse
import base64
import glob
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


__version__ = "1.3.1"

CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_CRED = os.path.expanduser("~/.claude/.credentials.json")
# Claude Code OAuth refresh grant. The token endpoint is served by
# api.anthropic.com (NOT platform.claude.com, which is Cloudflare-WAF-gated and
# 1010-blocks a bare urllib request); the client_id is the PUBLIC Claude Code
# OAuth client (ships in the CLI). A User-Agent is sent to satisfy edge bot rules.
CLAUDE_OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_UA = "anthropic-sdk-typescript/0.65.0"
# Refresh the stored Claude access token (and persist the rotation) if it expires
# within this many seconds. Stored lifetime is ~8h; the margin avoids racing the
# tail of its expiry.
CLAUDE_EXPIRY_MARGIN = 120
# Window lengths for the linear-pace ceiling (see _pace_pct). Claude's payload
# carries no duration field, but the window identity is fixed by its key name:
# five_hour = 5h, seven_day = 7d.
CLAUDE_WINDOW_SECS = {"five_hour": 5 * 3600, "weekly": 7 * 86400}
KIMI_URL = "https://api.kimi.com/coding/v1/usages"
KIMI_CRED = os.path.expanduser("~/.kimi-code/credentials/kimi-code.json")
# Kimi-Code OAuth refresh grant; host is auth.kimi.com. The client_id is the
# PUBLIC kimi-cli OAuth client (not a secret — ships in every install at
# kimi_cli/auth/oauth.py: KIMI_CODE_CLIENT_ID). It is read LIVE from that source
# at runtime (see _kimi_client_id) so it self-heals if upstream rotates it; the
# literal below is only the last-resort fallback.
KIMI_OAUTH_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
KIMI_OAUTH_CLIENT_ID_FALLBACK = "17e5f671-d194-4dfb-9706-5516cb48c098"
# The z.ai key authenticates with the bare value in the Authorization header -
# deliberately NO "Bearer" prefix (the documented form; verified live 2026-08-28).
ZAI_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
# Key resolution, in the order the bundle's own launchers resolve it: the
# environment first, then the machine-local override file, then the copy shipped
# beside spawn_zai.sh / spawn_zai.ps1 (claude/scripts/_zai_lane.sh zai_api_key,
# _zai_lane.ps1 Get-ZaiApiKey). Reading only the last entry below is what made a
# machine that had written the documented override file report the lane
# unconfigured, so the override file is not optional here.
ZAI_ENV_VAR = "ZAI_API_KEY"
ZAI_KEY_FILES = (os.path.expanduser("~/.config/claude-zai/api-key"),
                 os.path.expanduser("~/.claude/scripts/zai-api-key"))
# Lowest-priority fallback: some installs keep the key in a harness auth file
# under a "zai" entry instead of a bare key file.
ZAI_CRED = os.path.expanduser("~/.pi/agent/auth.json")
# Window unit codes observed in the limits[] payload: 3 = hours, 6 = weeks. An
# unlisted unit still surfaces (as a scoped row labelled unitNxM) rather than
# being dropped - an unobserved code must degrade visibly, not silently.
ZAI_UNIT_SECS = {3: 3600, 6: 604800}
# Peak-hours billing window for the z.ai coding plan (see _zai_peak_note):
# PUBLISHED POLICY, not an API fact - no endpoint exposes it (checked
# 2026-08-28). Re-verify against https://z.ai/blog/glm-5.3 if the 0.5x off-peak
# rate or weekday-only window ever looks wrong.
ZAI_TZ = timezone(timedelta(hours=8))  # China: no DST, fixed offset is exact
ZAI_PEAK_DAYS = (0, 1, 2, 3, 4)  # Monday..Friday, datetime.weekday() (Mon=0)
ZAI_PEAK_START_MIN = 14 * 60
ZAI_PEAK_END_MIN = 18 * 60  # end exclusive: 18:00:00 is already off-peak
ZAI_PEAK_HOURS = "Mon-Fri 14:00-18:00 UTC+8"
ZAI_OFF_PEAK_HOURS = (
    "Mon-Fri 00:00-14:00 and 18:00-24:00 UTC+8; all day Sat-Sun")
# Codex's own credential file: CODEX_HOME (or ~/.codex) / auth.json. Its shape
# was read off a live file rather than assumed - auth_mode, OPENAI_API_KEY,
# tokens{id_token, access_token, refresh_token, account_id}, last_refresh - and
# matches codex-rs login::AuthDotJson.
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
CODEX_AUTH = os.path.join(CODEX_HOME, "auth.json")
# Codex's config default (codex-rs/config/defaults.toml chatgpt_base_url). The
# path under it follows codex-rs backend-client PathStyle: a base carrying
# /backend-api uses the /wham/... spelling, anything else /api/codex/....
CODEX_BASE_URL = (os.environ.get("CODEX_CHATGPT_BASE_URL")
                  or "https://chatgpt.com/backend-api")
# Codex's OAuth refresh grant. The client_id is the PUBLIC codex client that
# ships in every install (codex-rs login::CLIENT_ID) - not a secret.
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# Refresh the stored Codex access token (and persist the rotation) if its own
# `exp` claim is within this many seconds; the stored token lives ~10 days.
CODEX_EXPIRY_MARGIN = 120
CODEX_UA = f"usage-query/{__version__}"
CODEX_NO_CHATGPT_MSG = (
    "Codex returned no ChatGPT rate limits (API-key logins do not have "
    "subscription quota windows)")


class ProviderNotConfigured(RuntimeError):
    """This machine does not have that provider: its credential source is absent.

    Distinct from every other failure here, which describes a provider that IS
    installed and could not be read. A box that never had a provider, or had it
    uninstalled, should not be told once per query that reading it failed --
    that is a report about a machine the operator does not have.
    """


def _require_configured(account, source):
    """Raise ProviderNotConfigured unless `source` (a credential file) exists."""
    if not os.path.exists(source):
        raise ProviderNotConfigured(
            f"{account} is not configured on this machine: {source} is absent")


TIMEOUT = 5
# Refresh the stored Kimi access token (and persist the rotation) if it expires
# within this many seconds; the stored token's own lifetime is ~15 min, so a
# margin avoids racing its expiry mid-request.
KIMI_EXPIRY_MARGIN = 120
# Length of Kimi's top-level "usage" (weekly) window for the linear-pace ceiling.
# That block carries no window.duration field, but it is the weekly quota, so we
# treat it as a fixed 7 days; _kimi_window_secs of a real `window` (if upstream
# ever adds one) takes precedence over this default.
KIMI_WEEKLY_SECS = 7 * 86400
# Shared cache files written by usage-monitor.py; reused here to avoid a
# network round-trip when the hook has already fetched data recently.
TEMPDIR = tempfile.gettempdir()
CACHE = os.path.join(TEMPDIR, ".claude_usage_cache.json")
KIMI_CACHE = os.path.join(TEMPDIR, ".claude_kimi_usage_cache.json")
CODEX_CACHE = os.path.join(TEMPDIR, ".codex_usage_cache.json")
ZAI_CACHE = os.path.join(TEMPDIR, ".zai_usage_cache.json")
CACHE_TTL = 30
# Codex gets its own, much longer TTL. A status line refreshing every 30 s and a
# 30 s TTL expire in lockstep, so essentially every refresh missed and paid for a
# full round trip; and Codex's shortest window is 5 hours, so a percentage that
# moved inside 15 minutes is not a number anyone acts on. 900 s is under 5% of
# that shortest window and turns thirty fetches per quarter-hour into one.
CODEX_CACHE_TTL = 900


def _fmt_dur(secs):
    """Compact d/h/m duration string from a second count (e.g. '3d1h', '2h05m',
    '44m') — the shared time-delta rendering used for both time-to-reset and the
    coast-to-on-pace estimate."""
    secs = max(0, int(secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d{h}h" if d else (f"{h}h{m:02d}m" if h else f"{m}m")


def _reset_info(iso):
    """('2026-06-11 14:39 machine-local ...', '1h22m') from an API reset timestamp.

    Rendered in the MACHINE'S OWN timezone (astimezone() with no arg = local tz)
    and labelled as such, so it can be fed straight into local-time consumers
    WITHOUT conversion. Trailing 'Z' is normalized to +00:00 so fromisoformat
    accepts both Anthropic's and Kimi's timestamp forms; naive timestamps are
    assumed UTC. A null/absent timestamp (the Claude /usage payload returns
    resets_at=null for a window with no active usage) yields a 'no reset
    scheduled' label rather than crashing the whole account."""
    if iso is None or str(iso).strip() in ("", "None"):
        return "no reset scheduled (window inactive)", "—"
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dur = _fmt_dur((dt - datetime.now(timezone.utc)).total_seconds())
    label = dt.astimezone().strftime("%Y-%m-%d %H:%M") + \
        " machine-local (already adjusted; use as-is, no tz conversion)"
    return label, dur


def _pace_pct(iso, window_secs):
    """Max utilization % you could be at right now under constant linear burn: the
    fraction of the window already elapsed (0..100). At/below it the quota lasts to
    the reset; above it you are burning faster than linear and will exhaust early.

    Derived as (window_secs - remaining)/window_secs from the reset timestamp.
    Returns None when window_secs is unknown/zero — we'd otherwise have to guess
    where 'now' sits in the window, which the module docstring forbids (Kimi's
    long 'usage' window has no duration field). Also None when the reset
    timestamp itself is null (an inactive window — see _reset_info)."""
    if not window_secs or iso is None or str(iso).strip() in ("", "None"):
        return None
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    remaining = max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    elapsed = max(0.0, window_secs - remaining)
    return min(100.0, elapsed / window_secs * 100.0)


def _recover_dur(iso, window_secs, pct):
    """Coast-to-on-pace time: if all burn STOPS now, how long until the linear
    pace line (fraction of window elapsed) rises to meet the current utilization
    `pct` — i.e. when you'd be back on pace. Returns a compact duration string
    (via _fmt_dur), or None when you're already at/under pace (nothing to
    recover) or the window length / reset timestamp is unknown (same guards as
    _pace_pct).

    The pace line reaches pct% when the elapsed fraction equals pct/100, i.e. at
    elapsed = window_secs*pct/100; subtract the elapsed-so-far to get the wait.
    Because pct <= 100 that target is always at/before the reset, so a coasting
    window always returns to pace before it rolls over."""
    if not window_secs or iso is None or str(iso).strip() in ("", "None"):
        return None
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    remaining = max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    elapsed = max(0.0, window_secs - remaining)
    back = window_secs * (pct / 100.0) - elapsed
    if back <= 0:
        return None
    return _fmt_dur(back)


def _get(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _write_cache(path, data):
    """Atomically seed the shared box-wide cache (temp + os.replace) on a
    SUCCESSFUL fetch only — same file the usage-monitor hook reads/writes. Never
    called with None, so a failed fetch never blanks the cache. This lets a
    standalone `usage_query.py` run populate its own stale-fallback source instead
    of depending entirely on the hook having fetched recently."""
    try:
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.time(), "data": data}, f)
        os.replace(tmp, path)
    except OSError:
        pass


# Retry the usage GET on a transient 429 (the endpoint is rate-limited under load
# and usually recovers within a fraction of a second): up to RETRY_ATTEMPTS tries,
# RETRY_DELAY apart. Non-429 errors are not retried — they re-raise immediately.
RETRY_ATTEMPTS = 5
RETRY_DELAY = 0.2


def _get_retry(url, headers):
    last = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return _get(url, headers)
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429 and attempt < RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY)
                continue
            raise
    assert last is not None
    raise last  # unreachable: loop always returns or raises


def _persist_claude_cred(full, resp):
    """Atomically write a refreshed Claude token set back into the credential file,
    preserving the rest of the claudeAiOauth object and 0600 perms. Anthropic
    ROTATES the refresh_token (single-use — verified 2026-06-11), so the rotated
    refresh_token MUST be persisted or the next refresh fails; the live harness
    reads this file as its source of truth, so writing a valid fresh set keeps it
    working. tempfile + os.replace so a concurrent reader never sees a torn file."""
    oa = full["claudeAiOauth"]
    if resp.get("access_token"):
        oa["accessToken"] = resp["access_token"]
    if resp.get("refresh_token"):
        oa["refreshToken"] = resp["refresh_token"]
    if resp.get("expires_in") is not None:
        oa["expiresAt"] = int((time.time() + float(resp["expires_in"])) * 1000)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CLAUDE_CRED),
                               prefix=".credentials.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(full, f)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, CLAUDE_CRED)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return oa["accessToken"]


def _claude_access_token():
    """A usable Claude access token. Uses the stored token if still valid (no
    network); if it is stale — e.g. after a long idle when the harness hasn't
    refreshed it — refreshes from the refresh_token against api.anthropic.com AND
    persists the rotated set back to the credential file (single-use grant — see
    module docstring), then returns the fresh access token."""
    with open(CLAUDE_CRED, encoding="utf-8") as f:
        full = json.load(f)
    oa = full.get("claudeAiOauth") or {}
    access = oa.get("accessToken") or ""
    expires_at = float(oa.get("expiresAt") or 0) / 1000.0  # stored in ms
    if access and expires_at - time.time() > CLAUDE_EXPIRY_MARGIN:
        return access
    refresh = oa.get("refreshToken")
    if not refresh:
        raise RuntimeError("Claude access token expired and no refreshToken present")
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(
        CLAUDE_OAUTH_TOKEN_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": CLAUDE_UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError(
            f"Claude token refresh failed (HTTP {e.code}). The harness normally "
            "refreshes this token on its own activity — send Claude a message and "
            f"retry, or re-login if it persists. {detail}") from e
    return _persist_claude_cred(full, resp)


def query_claude():
    """{'five_hour': {...}, 'weekly': {...}, '_stale_age': int?} normalized, or
    raises. A fresh cache is used as-is; otherwise we fetch live (retrying a
    transient 429). If the live fetch still fails, we fall back to the last cached
    payload however stale, tagged with '_stale_age' (seconds) so the caller can
    label it — better a known-old number than an error."""
    _require_configured("Claude", CLAUDE_CRED)
    now = time.time()
    data, stale = None, None
    try:
        with open(CACHE, encoding="utf-8") as f:
            c = json.load(f)
        cdata = c.get("data")
        cage = now - float(c.get("fetched_at") or 0)
        if cdata is not None:
            if cage < CACHE_TTL:
                data = cdata
            else:
                stale = (cdata, int(cage))
    except Exception:
        pass
    age = 0
    if data is None:
        token = _claude_access_token()
        try:
            data = _get_retry(CLAUDE_URL, {
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            })
            _write_cache(CACHE, data)
        except Exception:
            if stale is None:
                raise
            data, age = stale
    out = {}
    for key, src in (("five_hour", "five_hour"), ("weekly", "seven_day")):
        block = data[src]
        at, dur = _reset_info(block["resets_at"])
        out[key] = {"pct": float(block["utilization"]),
                    "resets_at": at, "resets_in": dur,
                    "pace_pct": _pace_pct(block["resets_at"],
                                          CLAUDE_WINDOW_SECS.get(key)),
                    "recover_in": _recover_dur(block["resets_at"],
                                               CLAUDE_WINDOW_SECS.get(key),
                                               float(block["utilization"]))}
    # Model- or surface-SCOPED caps live ONLY in the limits[] array — the flat
    # five_hour/seven_day utilization fields above never carry them, and the
    # per-model seven_day_opus/seven_day_sonnet keys are null. A scoped weekly
    # cap can bind BEFORE weekly_all (e.g. a per-model weekly at 95% critical
    # while weekly_all reads 85%), so surface every scoped entry — driven off
    # whatever scope the payload names, NEVER a hardcoded model. Non-scoped
    # limits[] entries (session, weekly_all) duplicate the rows above; skip them.
    group_secs = {"weekly": CLAUDE_WINDOW_SECS["weekly"],
                  "session": CLAUDE_WINDOW_SECS["five_hour"]}
    scoped = []
    for it in (data.get("limits") or []):
        scope = it.get("scope")
        if not scope:
            continue
        model = ((scope.get("model") or {}).get("display_name")
                 or scope.get("surface") or "scoped")
        group = str(it.get("group") or "")
        win_label = {"weekly": "7d", "session": "5h"}.get(group, group or "?")
        at, dur = _reset_info(it.get("resets_at"))
        scoped.append({"label": f"{win_label}:{model}",
                       "pct": float(it.get("percent") or 0),
                       "resets_at": at, "resets_in": dur,
                       "pace_pct": _pace_pct(it.get("resets_at"),
                                             group_secs.get(group)),
                       "recover_in": _recover_dur(it.get("resets_at"),
                                                  group_secs.get(group),
                                                  float(it.get("percent") or 0))})
    if scoped:
        out["_scoped"] = scoped
    if age:
        out["_stale_age"] = age
    return out


def _kimi_client_id():
    """The kimi-cli OAuth client_id, read live from the installed kimi_cli source
    (located via the `kimi` launcher's shebang -> venv, then the pipx default
    path) so it tracks upstream; falls back to KIMI_OAUTH_CLIENT_ID_FALLBACK if
    the source can't be found/parsed. It is a public client id, not a secret."""
    cands = []
    exe = shutil.which("kimi")
    if exe:
        try:
            with open(exe, encoding="utf-8", errors="replace") as f:
                m = re.match(r"#!\s*(\S+)", f.readline())
            if m:
                root = os.path.dirname(os.path.dirname(m.group(1)))
                cands += glob.glob(os.path.join(
                    root, "lib", "python*", "site-packages",
                    "kimi_cli", "auth", "oauth.py"))
        except Exception:
            pass
    cands += glob.glob(os.path.expanduser(
        "~/.local/share/pipx/venvs/kimi-cli/lib/python*/"
        "site-packages/kimi_cli/auth/oauth.py"))
    for path in cands:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                m = re.search(r'KIMI_CODE_CLIENT_ID\s*=\s*"([0-9a-fA-F-]+)"', f.read())
            if m:
                return m.group(1)
        except Exception:
            continue
    return KIMI_OAUTH_CLIENT_ID_FALLBACK


def _persist_kimi_cred(cred, resp):
    """Atomically write a refreshed Kimi token set back to the credential file,
    preserving 0600 perms. The refresh_token is single-use / rotated, so the
    refresh `resp` carries a NEW refresh_token (and access_token) that REPLACES
    the consumed one; persisting it is mandatory or the next refresh — ours or
    kimi's — fails with invalid_grant. Written via temp file + os.replace so a
    concurrent reader never sees a torn file. Returns the fresh access token."""
    updated = dict(cred)
    for k in ("access_token", "refresh_token", "expires_in", "scope", "token_type"):
        if resp.get(k) is not None:
            updated[k] = resp[k]
    if resp.get("expires_in") is not None:
        updated["expires_at"] = time.time() + float(resp["expires_in"])
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(KIMI_CRED),
                               prefix=".kimi-code.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(updated, f)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, KIMI_CRED)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return updated["access_token"]


def _kimi_access_token():
    """A usable Kimi access token. Uses the stored access token if it is still
    valid (no network); otherwise refreshes from the refresh_token AND persists
    the rotated tokens back to the credential file (single-use grant — see module
    docstring), then returns the fresh access token."""
    with open(KIMI_CRED, encoding="utf-8") as f:
        cred = json.load(f)
    access = cred.get("access_token") or ""
    expires_at = float(cred.get("expires_at") or 0)
    if access and expires_at - time.time() > KIMI_EXPIRY_MARGIN:
        return access
    refresh = cred.get("refresh_token")
    if not refresh:
        raise RuntimeError("Kimi access token expired and no refresh_token present")
    body = urllib.parse.urlencode({
        "client_id": _kimi_client_id(),
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }).encode("utf-8")
    req = urllib.request.Request(
        KIMI_OAUTH_TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 400:
            raise RuntimeError(
                "Kimi refresh_token rejected (invalid_grant): the stored token was "
                "already consumed/rotated by an earlier non-persisting refresh. Run "
                "`kimi login` once to re-authenticate — from then on this script "
                "persists each rotation, so it will not recur") from e
        raise
    return _persist_kimi_cred(cred, resp)


def _kimi_window_secs(win):
    """Length in seconds of a Kimi limit `window` ({'duration': N, 'timeUnit':
    'TIME_UNIT_MINUTE'}), for the linear-pace ceiling. Returns None if the field is
    absent/unrecognized; the caller for the top-level `usage` ('weekly') block —
    which carries no window today — falls back to KIMI_WEEKLY_SECS (7 days)."""
    dur = float(win.get("duration") or 0)
    if not dur:
        return None
    unit = str(win.get("timeUnit") or "")
    for name, secs in (("SECOND", 1), ("MINUTE", 60), ("HOUR", 3600),
                       ("DAY", 86400), ("WEEK", 604800)):
        if name in unit:
            return dur * secs
    return None


def _kimi_pct(block):
    """Utilization percent (used/limit*100) from a Kimi usage block. The /usages
    payload is protobuf-JSON, which OMITS zero-valued fields — so an unused window
    has `limit`+`remaining` but NO `used` key. Derive used from limit-remaining
    when `remaining` is present (always, in observed payloads); fall back to the
    explicit `used` otherwise. Values arrive as strings; float() coerces them."""
    limit = float(block["limit"])
    if not limit:
        return 0.0
    if "remaining" in block:
        used = limit - float(block["remaining"])
    else:
        used = float(block.get("used") or 0)
    return used / limit * 100.0


def query_kimi():
    """{'five_hour': {...}, 'weekly': {...}, '_stale_age': int?} normalized, or
    raises.

    5h window = limits[] entry whose window is 300 MINUTE (falls back to the
    first limit); weekly = the top-level `usage` block. Percentages are
    used/limit*100, the same 'how full' utilization sense as the Claude side.
    On a failed live fetch we fall back to the last cached payload (however
    stale), tagged with '_stale_age' seconds — same as the Claude side."""
    _require_configured("Kimi", KIMI_CRED)
    now = time.time()
    data, stale = None, None
    try:
        with open(KIMI_CACHE, encoding="utf-8") as f:
            c = json.load(f)
        cdata = c.get("data")
        cage = now - float(c.get("fetched_at") or 0)
        if cdata is not None:
            if cage < CACHE_TTL:
                data = cdata
            else:
                stale = (cdata, int(cage))
    except Exception:
        pass
    age = 0
    if data is None:
        token = _kimi_access_token()
        try:
            data = _get(KIMI_URL, {"Authorization": f"Bearer {token}"})
            _write_cache(KIMI_CACHE, data)
        except Exception:
            if stale is None:
                raise
            data, age = stale

    limits = data.get("limits") or []
    five = None
    for it in limits:
        w = it.get("window") or {}
        if int(w.get("duration") or 0) == 300 and \
                "MINUTE" in str(w.get("timeUnit") or ""):
            five = it
            break
    if five is None and limits:
        five = limits[0]
    d = (five or {}).get("detail") or {}
    s_at, s_dur = _reset_info(d["resetTime"])
    s_win = _kimi_window_secs((five or {}).get("window") or {})
    wk = data["usage"]
    w_at, w_dur = _reset_info(wk["resetTime"])
    # No window field on the usage block today -> default to the 7-day weekly.
    w_win = _kimi_window_secs(wk.get("window") or {}) or KIMI_WEEKLY_SECS
    out = {}
    out["five_hour"] = {"pct": _kimi_pct(d), "resets_at": s_at, "resets_in": s_dur,
                        "pace_pct": _pace_pct(d["resetTime"], s_win),
                        "recover_in": _recover_dur(d["resetTime"], s_win,
                                                   _kimi_pct(d))}
    out["weekly"] = {"pct": _kimi_pct(wk), "resets_at": w_at, "resets_in": w_dur,
                     "pace_pct": _pace_pct(wk["resetTime"], w_win),
                     "recover_in": _recover_dur(wk["resetTime"], w_win,
                                                _kimi_pct(wk))}
    if age:
        out["_stale_age"] = age
    return out


def _zai_key_from_auth_json(path):
    """The key under the "zai" field of a harness auth file, or None when the
    file is unreadable, is not JSON, or carries no key there."""
    try:
        with open(path, encoding="utf-8") as f:
            auth = json.load(f)
    except (OSError, ValueError):
        return None
    entry = auth.get("zai") if isinstance(auth, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else entry
    return str(key).strip() or None if key else None


def _zai_cred():
    """The z.ai API key, resolved the way the bundle's spawn_zai launchers
    resolve it: $ZAI_API_KEY, the machine-local override file, the copy shipped
    beside those launchers, then a harness auth file's "zai" entry. Raises
    ProviderNotConfigured naming every place looked at — never a key value."""
    env = (os.environ.get(ZAI_ENV_VAR) or "").strip()
    if env:
        return env
    for path in ZAI_KEY_FILES:
        try:
            with open(path, encoding="utf-8") as f:
                key = f.read().strip()
        except OSError:
            continue
        if key:
            return key
    key = _zai_key_from_auth_json(ZAI_CRED)
    if key:
        return key
    looked = ", ".join(("$" + ZAI_ENV_VAR,) + tuple(ZAI_KEY_FILES) + (ZAI_CRED,))
    raise ProviderNotConfigured(
        f"z.ai is not configured on this machine: no API key in {looked}")


def _zai_billing_status(now=None):
    """Current z.ai billing state, published hours, and next transition.

    Computed, not queried: no z.ai payload carries a peak flag (checked against
    the quota and model-usage monitor endpoints, 2026-08-28), so the rule the
    window constants at the top of this module encode IS the source of truth,
    and they carry the provenance needed to re-verify it when the policy
    moves. `now` is a UTC datetime by convention; the real clock is the default.
    End-exclusive means 18:00:00 itself is off-peak and the next peak begins on
    the next weekday.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(ZAI_TZ)
    peak = (moment.weekday() in ZAI_PEAK_DAYS
            and ZAI_PEAK_START_MIN <= moment.hour * 60 + moment.minute
            < ZAI_PEAK_END_MIN)
    if peak:
        transition = moment.replace(hour=18, minute=0, second=0, microsecond=0)
        next_state = "off-peak"
    else:
        transition = moment.replace(hour=14, minute=0, second=0, microsecond=0)
        if (moment.weekday() not in ZAI_PEAK_DAYS
                or transition <= moment):
            transition += timedelta(days=1)
            while transition.weekday() not in ZAI_PEAK_DAYS:
                transition += timedelta(days=1)
        next_state = "peak"
    return {
        "state": "peak" if peak else "off-peak",
        "multiplier": 1.0 if peak else 0.5,
        "peak_hours": ZAI_PEAK_HOURS,
        "off_peak_hours": ZAI_OFF_PEAK_HOURS,
        "next_transition": {
            "state": next_state,
            "at": transition.isoformat(timespec="seconds"),
            "in": _fmt_dur((transition - moment).total_seconds()),
        },
    }


def _zai_peak_note(now=None):
    """Backward-compatible compact label for the current z.ai billing state."""
    status = _zai_billing_status(now)
    return f"{status['state']} {status['multiplier']:g}x"


def _zai_billing_note(status):
    """One human-readable line carrying the complete z.ai billing clock."""
    transition = status["next_transition"]
    at = datetime.fromisoformat(transition["at"])
    return (
        f"z.ai billing: {status['state']} {status['multiplier']:g}x now; "
        f"peak {status['peak_hours']}; off-peak {status['off_peak_hours']}; "
        f"next {transition['state']} starts "
        f"{at.strftime('%Y-%m-%d %H:%M')} UTC+8 (in {transition['in']})."
    )


def _normalize_zai(envelope, peak_note):
    """Normalize one fetched or cache-fallen-back quota envelope: windows keyed
    five_hour/weekly when the unit/number pair names them, every other window
    as a scoped row (an unobserved unit degrades visibly), plus the plan tier.
    `peak_note` is computed once per query by the caller and stamped on every
    window - all windows share the same billing clock."""
    data = envelope.get("data") or {}
    unit_names = {3: "h", 6: "w"}
    out, scoped = {}, []
    for it in data.get("limits") or []:
        unit = int(it.get("unit") or 0)
        number = int(it.get("number") or 0)
        seconds = ZAI_UNIT_SECS.get(unit, 0) * number or None
        reset_iso = _codex_reset_iso(
            (float(it.get("nextResetTime") or 0) / 1000.0) or None)
        at, dur = _reset_info(reset_iso)
        pct = float(it.get("percentage") or 0)
        window = {"pct": pct,
                  "resets_at": at, "resets_in": dur,
                  "pace_pct": _pace_pct(reset_iso, seconds),
                  "recover_in": _recover_dur(reset_iso, seconds, pct),
                  "peak_note": peak_note}
        key = {5 * 3600: "five_hour", 7 * 86400: "weekly"}.get(seconds or 0)
        if key and key not in out:
            out[key] = window
            continue
        window["label"] = (f"{number}{unit_names[unit]}"
                           if unit in unit_names and number
                           else f"unit{unit}x{number}")
        scoped.append(window)
    if scoped:
        out["_scoped"] = scoped
    if data.get("level"):
        out["_plan_type"] = str(data["level"])
    if not any(name in out for name in ("five_hour", "weekly", "_scoped")):
        raise RuntimeError("z.ai quota response contained no windows")
    return out


def query_zai():
    """{'five_hour': {...}, 'weekly': {...}, '_scoped': [...], '_plan_type': str,
    '_billing': {...}, '_stale_age': int?} normalized, or raises.

    `limits[]` entries carry the window in unit/number, the allowance in
    `usage` (a confusing name - `currentValue` is what was consumed), and
    `percentage` used, with `nextResetTime` in epoch milliseconds. Fresh cache
    wins; on a failed live fetch the last cached payload is used however
    stale, tagged with '_stale_age' - same as every other provider here."""
    key = _zai_cred()
    now = time.time()
    data, stale = None, None
    try:
        with open(ZAI_CACHE, encoding="utf-8") as f:
            c = json.load(f)
        cdata = c.get("data")
        cage = now - float(c.get("fetched_at") or 0)
        if cdata is not None:
            if cage < CACHE_TTL:
                data = cdata
            else:
                stale = (cdata, int(cage))
    except Exception:
        pass
    age = 0
    if data is None:
        try:
            fetched = _get_retry(ZAI_URL, {
                "Authorization": key,
                "Accept-Language": "en-US,en",
                "Content-Type": "application/json",
            })
            if fetched.get("code") not in (None, 200):
                raise RuntimeError(
                    "z.ai quota API error: %s" % fetched.get("msg"))
            data = fetched
            _write_cache(ZAI_CACHE, data)
        except Exception:
            if stale is None:
                raise
            data, age = stale
    billing = _zai_billing_status()
    peak_note = f"{billing['state']} {billing['multiplier']:g}x"
    out = _normalize_zai(data, peak_note)
    out["_billing"] = billing
    if age:
        out["_stale_age"] = age
    return out


def _codex_backend_url(leaf):
    """A backend URL for CODEX_BASE_URL, following codex's own routing:
    Client::new appends /backend-api to a bare chatgpt.com host, and
    PathStyle::from_base_url picks /wham/... for a base carrying /backend-api and
    /api/codex/... for anything else. Both styles spell every leaf the same, so
    the routing is decided once here rather than per endpoint."""
    base = CODEX_BASE_URL.rstrip("/")
    if (base.startswith("https://chatgpt.com")
            or base.startswith("https://chat.openai.com")) and (
                "/backend-api" not in base):
        base += "/backend-api"
    return (f"{base}/wham/{leaf}" if "/backend-api" in base
            else f"{base}/api/codex/{leaf}")


def _codex_usage_url():
    """Where the quota windows and the banked-reset COUNT are read."""
    return _codex_backend_url("usage")


def _codex_reset_credits_url():
    """Where the per-credit banked-reset detail is read (codex-rs
    list_rate_limit_reset_credits); the usage payload carries only a count."""
    return _codex_backend_url("rate-limit-reset-credits")


def _jwt_expiry(token):
    """The `exp` claim (epoch seconds) of a JWT, or None when the token is not a
    readable JWT. The payload segment is base64-decoded, not verified: the
    signature is the issuer's business and `exp` is the only claim needed here.
    Nothing from the token is returned, logged or raised."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(
            payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"])
    except Exception:
        return None


def _codex_auth():
    """Codex's stored credentials, or ProviderNotConfigured when this machine
    has no Codex install to read."""
    _require_configured("Codex", CODEX_AUTH)
    with open(CODEX_AUTH, encoding="utf-8") as f:
        full = json.load(f)
    if not isinstance(full, dict):
        raise RuntimeError(f"Codex {CODEX_AUTH} is not a JSON object")
    return full


def _persist_codex_cred(full, resp):
    """Atomically write a refreshed Codex token set back into auth.json,
    preserving every other field and the file's own permissions. OpenAI rotates
    the refresh_token, so the rotated one MUST land on disk or codex's next
    refresh — ours or its own — fails; codex reads this file as its source of
    truth, and it stamps last_refresh alongside the tokens, so we do too.
    tempfile + os.replace, so a concurrent codex never reads a torn file."""
    tokens = full.setdefault("tokens", {})
    for field in ("id_token", "access_token", "refresh_token"):
        if resp.get(field):
            tokens[field] = resp[field]
    full["last_refresh"] = datetime.now(timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    try:
        mode = stat.S_IMODE(os.stat(CODEX_AUTH).st_mode)
    except OSError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CODEX_AUTH) or ".",
                               prefix=".auth.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(full, f, indent=2)
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, CODEX_AUTH)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return tokens.get("access_token") or ""


def _codex_refresh(full):
    """Run codex's refresh grant and persist the rotated set; returns the fresh
    access token."""
    refresh = (full.get("tokens") or {}).get("refresh_token")
    if not refresh:
        raise RuntimeError(
            "Codex access token is expired and auth.json carries no "
            "refresh_token; run `codex login` to re-authenticate")
    body = json.dumps({"client_id": CODEX_OAUTH_CLIENT_ID,
                       "grant_type": "refresh_token",
                       "refresh_token": refresh}).encode("utf-8")
    req = urllib.request.Request(
        CODEX_OAUTH_TOKEN_URL, data=body,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json",
                 "User-Agent": CODEX_UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RuntimeError(
            f"Codex token refresh failed (HTTP {e.code}). Codex normally "
            "refreshes this token on its own activity — run codex once and "
            f"retry, or `codex login` if it persists. {detail}") from e
    if not resp.get("access_token"):
        raise RuntimeError("Codex token refresh returned no access_token")
    return _persist_codex_cred(full, resp)


def _codex_access_token(full):
    """(access token, whether it was just refreshed). The stored token is used
    as-is while its own `exp` is still ahead of CODEX_EXPIRY_MARGIN; a stale one
    is refreshed and persisted first. An unreadable `exp` is not a refresh
    trigger — the request itself answers that, with the 401 retry below."""
    access = (full.get("tokens") or {}).get("access_token") or ""
    if not access:
        raise RuntimeError(CODEX_NO_CHATGPT_MSG)
    exp = _jwt_expiry(access)
    if exp is None or exp - time.time() > CODEX_EXPIRY_MARGIN:
        return access, False
    return _codex_refresh(full), True


def _codex_window_from_payload(window):
    """One backend rate-limit window in the app-server's spelling. Minutes are
    rounded UP from limit_window_seconds, as codex's own
    window_minutes_from_seconds does, and a non-positive span has no duration."""
    if not isinstance(window, dict):
        return None
    try:
        seconds = int(window.get("limit_window_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    raw_reset = window.get("reset_at")
    try:
        resets = None if raw_reset is None else int(raw_reset)
    except (TypeError, ValueError):
        resets = None
    return {"usedPercent": float(window.get("used_percent") or 0),
            "windowDurationMins": -(-seconds // 60) if seconds > 0 else None,
            "resetsAt": resets}


def _codex_snapshot(limit_id, limit_name, rate_limit, plan_type):
    """One bucket in the app-server's RateLimitSnapshot shape, limited to the
    fields _normalize_codex reads. The unread halves of that struct (credits,
    individualLimit, spendControlReached, rateLimitReachedType) are deliberately
    not synthesised: nothing here consumes them."""
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    return {
        "limitId": limit_id,
        "limitName": limit_name,
        "primary": _codex_window_from_payload(rate_limit.get("primary_window")),
        "secondary": _codex_window_from_payload(
            rate_limit.get("secondary_window")),
        "planType": plan_type,
    }


def _codex_rpc_shape(payload):
    """The backend's usage payload in account/rateLimits/read's response shape.

    Mirrors codex-rs backend-client rate_limit_snapshots_from_payload: one
    snapshot under limit_id "codex" built from `rate_limit`, then one per
    `additional_rate_limits` entry keyed by its metered_feature and labelled with
    its limit_name. Keeping the RPC shape is what lets the shared cache file, the
    normalizer and every downstream consumer stay exactly as they were.
    """
    plan = payload.get("plan_type")
    default = _codex_snapshot("codex", None, payload.get("rate_limit"), plan)
    by_id = {"codex": default}
    for extra in (payload.get("additional_rate_limits") or []):
        if not isinstance(extra, dict):
            continue
        limit_id = extra.get("metered_feature") or extra.get("limit_name")
        if not limit_id:
            continue
        by_id[limit_id] = _codex_snapshot(limit_id, extra.get("limit_name"),
                                          extra.get("rate_limit"), plan)
    out = {"rateLimits": default, "rateLimitsByLimitId": by_id}
    reset_credits = payload.get("rate_limit_reset_credits")
    if isinstance(reset_credits, dict):
        out["rateLimitResetCredits"] = {
            "availableCount": int(reset_credits.get("available_count") or 0)}
    return out


def _codex_rate_limits():
    """Codex ChatGPT limits, read straight from the backend with Codex's own
    stored OAuth credentials — no `codex` process is started (see the module
    docstring for why). A 401 on a token we did not just refresh is treated as
    "stale despite its exp": refresh once, retry once, then give up.
    """
    full = _codex_auth()
    token, refreshed = _codex_access_token(full)
    url = _codex_usage_url()
    account = (full.get("tokens") or {}).get("account_id") or ""
    while True:
        headers = {"Authorization": f"Bearer {token}",
                   "User-Agent": CODEX_UA,
                   "Accept": "application/json"}
        if account:
            headers["ChatGPT-Account-Id"] = account
        try:
            shape = _codex_rpc_shape(_get_retry(url, headers))
            _codex_attach_reset_credit_detail(shape, headers)
            return shape
        except urllib.error.HTTPError as e:
            if e.code == 401 and not refreshed:
                token, refreshed = _codex_refresh(full), True
                continue
            raise RuntimeError(
                f"Codex rate-limit request failed (HTTP {e.code})") from e


def _codex_attach_reset_credit_detail(shape, headers):
    """Name the banked resets the usage payload only counted.

    The count and the detail come from two different endpoints, exactly as
    codex-rs splits get_rate_limits_for_usage from
    list_rate_limit_reset_credits, so a note built from the usage payload alone
    could not say WHICH credit it is telling you to spend or when it dies. The
    second request is made only when the count says there is something to name,
    and a failure to reach it costs the name, not the account: the windows are
    why anyone ran this. The wire's snake_case is translated here, so the cache
    and the normalizer see one spelling whichever route filled them.
    """
    summary = shape.get("rateLimitResetCredits")
    if not isinstance(summary, dict):
        return
    if int(summary.get("availableCount") or 0) <= 0:
        return
    try:
        detail = _get_retry(_codex_reset_credits_url(), headers)
    except Exception:
        return
    rows = (detail or {}).get("credits") if isinstance(detail, dict) else None
    if not isinstance(rows, list):
        return
    summary["credits"] = [{
        "id": row.get("id"),
        "status": row.get("status"),
        "title": row.get("title"),
        "expiresAt": row.get("expires_at"),
    } for row in rows if isinstance(row, dict)]


def _codex_reset_iso(value):
    """A backend timestamp -> the ISO form used by shared reset helpers.

    Windows carry epoch seconds; a reset credit's expiry is an ISO string on
    the wire (backend-client types spell RateLimitResetCreditDetails.expires_at
    a String where a window's reset_at is a number), so both arrive here and an
    ISO value is passed through rather than fed to fromtimestamp.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return value
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _window_label(minutes):
    """Compact label for an arbitrary Codex quota-window duration."""
    minutes = int(float(minutes or 0))
    if minutes and minutes % (24 * 60) == 0:
        return "%dd" % (minutes // (24 * 60))
    if minutes and minutes % 60 == 0:
        return "%dh" % (minutes // 60)
    return "%dm" % minutes if minutes else "?"


def _codex_window(block):
    """Normalize one app-server primary/secondary rate-limit window."""
    reset_iso = _codex_reset_iso(block.get("resetsAt"))
    minutes = float(block.get("windowDurationMins") or 0)
    pct = float(block.get("usedPercent") or 0)
    at, dur = _reset_info(reset_iso)
    seconds = minutes * 60 if minutes else None
    return {
        "pct": pct,
        "resets_at": at,
        "resets_in": dur,
        "pace_pct": _pace_pct(reset_iso, seconds),
        "recover_in": _recover_dur(reset_iso, seconds, pct),
    }


def _normalize_codex(data):
    """Normalize every Codex bucket without duplicating the legacy view.

    The backward-compatible `rateLimits` block is also present in
    `rateLimitsByLimitId` on current Codex. The default 5h/7d windows retain the
    shared keys consumed by existing status integrations; other durations and
    named/model buckets are emitted as scoped rows instead of being discarded.
    """
    single = data.get("rateLimits") or {}
    default_id = single.get("limitId") or "codex"
    by_id = dict(data.get("rateLimitsByLimitId") or {})
    if single and default_id not in by_id:
        by_id[default_id] = single
    if not by_id:
        raise RuntimeError("Codex rate-limit response contained no buckets")
    default_bucket = by_id.get(default_id) or single

    ordered = []
    if default_id in by_id:
        ordered.append((default_id, by_id.pop(default_id)))
    ordered.extend(sorted(by_id.items()))

    out = {}
    scoped = []
    for limit_id, bucket in ordered:
        is_default = limit_id == default_id
        scope = bucket.get("limitName") or (None if is_default else limit_id)
        for slot in ("primary", "secondary"):
            block = bucket.get(slot)
            if not isinstance(block, dict):
                continue
            minutes = int(float(block.get("windowDurationMins") or 0))
            normalized = _codex_window(block)
            shared_key = {300: "five_hour", 10080: "weekly"}.get(minutes)
            if is_default and shared_key and shared_key not in out:
                out[shared_key] = normalized
                continue
            label = _window_label(minutes)
            suffix = scope or (slot if shared_key in out else None)
            if suffix:
                label += ":%s" % suffix
            normalized["label"] = label
            scoped.append(normalized)
    if scoped:
        out["_scoped"] = scoped
    plan = default_bucket.get("planType")
    if plan:
        out["_plan_type"] = plan
    reset_credits = data.get("rateLimitResetCredits")
    if isinstance(reset_credits, dict):
        out["_reset_credits_available"] = int(
            reset_credits.get("availableCount") or 0)
        detail = _codex_reset_credits(reset_credits)
        if detail:
            out["_reset_credits"] = detail
    if not any(key in out for key in ("five_hour", "weekly", "_scoped")):
        raise RuntimeError("Codex rate-limit response contained no windows")
    return out


def _codex_reset_credits(block):
    """Detail rows for the banked resets a human can spend by hand.

    A banked reset is redeemed deliberately -- it is not applied for you -- so
    the row has to name WHICH credit and how long it lasts. Only `available`
    credits are offered (a redeemed one cannot be spent again) and they are
    ordered by expiry, most perishable first, because that is the one worth
    acting on. `expiresAt` is epoch seconds, the same clock as `resetsAt`; a
    credit without one does not expire. The app-server omits the per-credit
    detail on its periodic refresh, so an empty list is a normal answer.
    """
    rows = []
    for credit in block.get("credits") or []:
        if not isinstance(credit, dict):
            continue
        if credit.get("status") != "available":
            continue
        expires = _codex_reset_iso(credit.get("expiresAt"))
        row = {
            "id": credit.get("id"),
            "title": (credit.get("title") or "").strip() or "Full reset",
        }
        if expires is None:
            row["expires_at"], row["expires_in"] = None, None
        else:
            row["expires_at"], row["expires_in"] = _reset_info(expires)
        # Sorted on the normalized ISO form, not the raw field: epoch seconds
        # and ISO strings both arrive, and ordering the two spellings against
        # each other is only meaningful once they are one spelling.
        rows.append((expires or "9999", row))
    rows.sort(key=lambda pair: pair[0])
    return [row for _, row in rows]


def _codex_reset_credits_note(res):
    """One human line naming the banked resets this account can spend.

    Read-only by construction: this tool reports the credits, it never calls
    the consume RPC, so the line names that RPC instead of implying the count
    it printed has already been acted on.
    """
    available = int(res.get("_reset_credits_available") or 0)
    if available <= 0:
        return None
    detail = ""
    named = res.get("_reset_credits") or []
    if named:
        parts = []
        for credit in named:
            if credit.get("expires_at"):
                parts.append('"%s" expires %s (in %s)' % (
                    credit["title"],
                    str(credit["expires_at"]).replace(_TZ_NOTE, ""),
                    credit["expires_in"]))
            else:
                parts.append('"%s" does not expire' % credit["title"])
        detail = " -- " + "; ".join(parts)
    return ("Codex banked resets: %d available%s. Spend one deliberately with "
            "the app-server RPC account/rateLimitResetCredit/consume; "
            "usage-query only reads them." % (available, detail))


def query_codex():
    """Normalized Codex quota windows, with fresh-cache and stale fallback."""
    now = time.time()
    data, stale = None, None
    try:
        with open(CODEX_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        cached_data = cached.get("data")
        age = now - float(cached.get("fetched_at") or 0)
        if cached_data is not None:
            if age < CODEX_CACHE_TTL:
                data = cached_data
            else:
                stale = (cached_data, int(age))
    except Exception:
        pass
    stale_age = 0
    if data is None:
        try:
            data = _codex_rate_limits()
            _write_cache(CODEX_CACHE, data)
        except Exception:
            if stale is None:
                raise
            data, stale_age = stale
    out = _normalize_codex(data)
    if stale_age:
        out["_stale_age"] = stale_age
    return out


_TZ_NOTE = " machine-local (already adjusted; use as-is, no tz conversion)"
# Table account names, for the few where capitalize() reads wrong.
_DISPLAY = {"zai": "z.ai"}


def _table_rows(account, res):
    """Table rows for one account: the 5h + 7d windows, plus one row per
    model/surface-SCOPED cap the payload carries (Claude's _scoped list). Each
    row: pct, pace ceiling (or — when the window length is unknown), the OVER
    PACE flag, reset timestamp (tz note moved to a shared footnote) and
    time-to-reset, plus the cached-age marker on the account name. Window
    labels are duration-style throughout (5h, 7d, and e.g. 7d:Fable for a
    scoped weekly) so they read consistently."""
    stale = res.get("_stale_age")
    name = account + (f" (cached {stale}s)" if stale else "")
    rows = []
    for label, key in (("5h", "five_hour"), ("7d", "weekly")):
        w = res.get(key)
        if not isinstance(w, dict):
            continue
        if w.get("peak_note"):
            label += f" ({w['peak_note']})"
        pace = w.get("pace_pct")
        rec = w.get("recover_in")
        flag = ("OVER PACE" + (f" (on pace in {rec})" if rec else "")
                if pace is not None and w["pct"] > pace + 0.5 else "")
        rows.append((name, label, f"{w['pct']:.0f}%",
                     "—" if pace is None else f"{pace:.0f}%",
                     str(w["resets_at"]).replace(_TZ_NOTE, ""),
                     str(w["resets_in"]), flag))
        name = ""
    for sc in res.get("_scoped") or []:
        label = sc["label"] + (f" ({sc['peak_note']})"
                               if sc.get("peak_note") else "")
        pace = sc.get("pace_pct")
        rec = sc.get("recover_in")
        flag = ("OVER PACE" + (f" (on pace in {rec})" if rec else "")
                if pace is not None and sc["pct"] > pace + 0.5 else "")
        rows.append((name, label, f"{sc['pct']:.0f}%",
                     "—" if pace is None else f"{pace:.0f}%",
                     str(sc["resets_at"]).replace(_TZ_NOTE, ""),
                     str(sc["resets_in"]), flag))
        name = ""
    return rows


def _render_table(rows, provider_notes=None):
    """Aligned table, provider notes, and shared pace/timezone footnotes."""
    head = ("account", "window", "used", "max*", "resets", "in", "")
    widths = [max(len(r[i]) for r in [head] + rows) for i in range(len(head))]
    out = []
    for r in [head] + rows:
        cells = [r[i].ljust(widths[i]) if i in (0, 1, 4)
                 else r[i].rjust(widths[i]) for i in range(len(head))]
        out.append("  ".join(cells).rstrip())
    out.append("")
    if provider_notes:
        out.extend(provider_notes)
        out.append("")
    out.append("* max = utilization ceiling if burning at a constant rate; "
               "OVER PACE = above it (will exhaust before the reset); "
               "'on pace in T' = if burn stops now, the pace line catches up to "
               "current usage in T. — = window length unknown.")
    out.append("reset times are machine-local (already adjusted; "
               "use as-is, no tz conversion).")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Query Claude, Kimi, Codex, and/or z.ai rate-limit "
                    "utilization.")
    ap.add_argument("--version", action="version",
                    version=f"usage_query {__version__}")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--claude", action="store_true",
                   help="only query Claude usage")
    g.add_argument("--kimi", action="store_true",
                   help="only query Kimi usage")
    g.add_argument("--codex", action="store_true",
                   help="only query Codex usage")
    g.add_argument("--zai", action="store_true",
                   help="only query z.ai usage")
    ap.add_argument("--json", action="store_true",
                    help="emit a JSON object instead of human-readable lines")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-account error lines")
    args = ap.parse_args(argv)

    want_claude = not (args.kimi or args.codex or args.zai)
    want_kimi = not (args.claude or args.codex or args.zai)
    want_codex = not (args.claude or args.kimi or args.zai)
    want_zai = not (args.claude or args.kimi or args.codex)

    # Asking for one provider by name makes its absence the answer to the
    # question, so it stays an error. A default sweep asks "what is on this
    # box", and a provider that is not on it is not a failure of anything.
    asked_by_name = args.claude or args.kimi or args.codex or args.zai

    results, errors, unconfigured = {}, {}, {}
    for account, wanted, query in (("claude", want_claude, query_claude),
                                   ("kimi", want_kimi, query_kimi),
                                   ("codex", want_codex, query_codex),
                                   ("zai", want_zai, query_zai)):
        if not wanted:
            continue
        try:
            results[account] = query()
        except ProviderNotConfigured as e:
            (errors if asked_by_name else unconfigured)[account] = (
                f"{type(e).__name__}: {e}")
        except Exception as e:
            errors[account] = f"{type(e).__name__}: {e}"

    if args.json:
        json.dump({"usage": results, "errors": errors,
                   "unconfigured": unconfigured}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        rows = []
        provider_notes = []
        for account in ("claude", "kimi", "codex", "zai"):
            name = _DISPLAY.get(account, account.capitalize())
            if account in results:
                result = results[account]
                rows.extend(_table_rows(name, result))
                if account == "zai" and isinstance(result.get("_billing"), dict):
                    provider_notes.append(_zai_billing_note(result["_billing"]))
                if account == "codex":
                    note = _codex_reset_credits_note(result)
                    if note:
                        provider_notes.append(note)
            elif account in errors and not args.quiet:
                print(f"{name}: ERROR — {errors[account]}", file=sys.stderr)
        if rows:
            print(_render_table(rows, provider_notes))

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
