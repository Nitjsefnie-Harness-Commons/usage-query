#!/usr/bin/env python3
"""Standalone ``usage-query`` command for Claude, Kimi-Code, and Codex.

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
  - Codex:  `codex app-server` JSON-RPC account/rateLimits/read
            auth/refresh = Codex-owned (file, keyring, API key, or access token)
            windows      = every primary/secondary window in every limit bucket

CREDENTIAL DISCIPLINE: for BOTH the Claude and Kimi files, when the stored access
token is stale we refresh on demand and PERSIST the rotated result, the same way
the official clients (Claude Code / kimi-cli) do.

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

Codex credentials may live in auth.json OR the operating-system keyring and
support several login modes, so this utility never reads or refreshes them
itself. It starts Codex's supported app-server, whose account/rateLimits/read
method uses the native credential manager and returns every active quota bucket.

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
  (default)       query all three
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
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


__version__ = "1.1.0"

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
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"


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
CODEX_RPC_TIMEOUT = 10
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
CACHE_TTL = 30


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


def _codex_rpc_rate_limits():
    """Read Codex ChatGPT limits through the supported app-server RPC surface.

    Codex owns its credential lifecycle, including keyring-backed credentials
    and OAuth refresh. Going through app-server avoids teaching this shared
    utility the shape of auth.json or creating a second token refresher.
    """
    try:
        proc = subprocess.Popen(
            [CODEX_BIN, "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError as exc:
        raise ProviderNotConfigured(
            "Codex is not configured on this machine: the codex CLI "
            f"({CODEX_BIN}) is absent") from exc
    except OSError as exc:
        raise RuntimeError("Codex CLI/app-server is unavailable: %s" % exc) from exc

    messages = queue.Queue()
    eof = object()

    def read_stdout():
        try:
            for raw in proc.stdout:  # pyright: ignore[reportOptionalIterable]
                messages.put(raw)
        finally:
            messages.put(eof)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    deadline = time.monotonic() + CODEX_RPC_TIMEOUT

    def send(message):
        if proc.stdin is None:
            raise RuntimeError("Codex app-server stdin is unavailable")
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    def receive(response_id):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Codex app-server rate-limit query timed out")
            try:
                raw = messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise RuntimeError(
                    "Codex app-server rate-limit query timed out") from exc
            if raw is eof:
                raise RuntimeError("Codex app-server exited before replying")
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if message.get("id") != response_id:
                continue
            if message.get("error"):
                err = message["error"]
                detail = err.get("message") if isinstance(err, dict) else str(err)
                raise RuntimeError("Codex app-server error: %s" % detail)
            return message.get("result") or {}

    try:
        send({"method": "initialize", "id": 0, "params": {
            "clientInfo": {
                "name": "agent_harness_bundle_usage_query",
                "title": "Agent Harness Bundle Usage Query",
                "version": "1.0.0",
            }}})
        receive(0)
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 1})
        result = receive(1)
        if not result.get("rateLimits") and not result.get("rateLimitsByLimitId"):
            raise RuntimeError(
                "Codex returned no ChatGPT rate limits (API-key logins do not "
                "have subscription quota windows)")
        return result
    finally:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)


def _codex_reset_iso(value):
    """App-server epoch seconds -> the ISO form used by shared reset helpers."""
    if value is None:
        return None
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
    if not any(key in out for key in ("five_hour", "weekly", "_scoped")):
        raise RuntimeError("Codex rate-limit response contained no windows")
    return out


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
            if age < CACHE_TTL:
                data = cached_data
            else:
                stale = (cached_data, int(age))
    except Exception:
        pass
    stale_age = 0
    if data is None:
        try:
            data = _codex_rpc_rate_limits()
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
        pace = sc.get("pace_pct")
        rec = sc.get("recover_in")
        flag = ("OVER PACE" + (f" (on pace in {rec})" if rec else "")
                if pace is not None and sc["pct"] > pace + 0.5 else "")
        rows.append((name, sc["label"], f"{sc['pct']:.0f}%",
                     "—" if pace is None else f"{pace:.0f}%",
                     str(sc["resets_at"]).replace(_TZ_NOTE, ""),
                     str(sc["resets_in"]), flag))
        name = ""
    return rows


def _render_table(rows):
    """Aligned plain-text table + the two footnotes carrying the prose that was
    inlined per-line before (pace semantics, tz note)."""
    head = ("account", "window", "used", "max*", "resets", "in", "")
    widths = [max(len(r[i]) for r in [head] + rows) for i in range(len(head))]
    out = []
    for r in [head] + rows:
        cells = [r[i].ljust(widths[i]) if i in (0, 1, 4)
                 else r[i].rjust(widths[i]) for i in range(len(head))]
        out.append("  ".join(cells).rstrip())
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
        description="Query Claude, Kimi, and/or Codex rate-limit utilization.")
    ap.add_argument("--version", action="version",
                    version=f"usage_query {__version__}")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--claude", action="store_true",
                   help="only query Claude usage")
    g.add_argument("--kimi", action="store_true",
                   help="only query Kimi usage")
    g.add_argument("--codex", action="store_true",
                   help="only query Codex usage")
    ap.add_argument("--json", action="store_true",
                    help="emit a JSON object instead of human-readable lines")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-account error lines")
    args = ap.parse_args(argv)

    want_claude = not (args.kimi or args.codex)
    want_kimi = not (args.claude or args.codex)
    want_codex = not (args.claude or args.kimi)

    # Asking for one provider by name makes its absence the answer to the
    # question, so it stays an error. A default sweep asks "what is on this
    # box", and a provider that is not on it is not a failure of anything.
    asked_by_name = args.claude or args.kimi or args.codex

    results, errors, unconfigured = {}, {}, {}
    for account, wanted, query in (("claude", want_claude, query_claude),
                                   ("kimi", want_kimi, query_kimi),
                                   ("codex", want_codex, query_codex)):
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
        for account in ("claude", "kimi", "codex"):
            if account in results:
                rows.extend(_table_rows(account.capitalize(), results[account]))
            elif account in errors and not args.quiet:
                print(f"{account.capitalize()}: ERROR — {errors[account]}",
                      file=sys.stderr)
        if rows:
            print(_render_table(rows))

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
