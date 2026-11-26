"""Claude utilization from inference rate-limit headers."""
import json
import re
import time
import urllib.request

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
PROBE_MODEL = "claude-haiku-5-5"
_PREFIX = "anthropic-ratelimit-unified-"


def _open(url, token, agent, timeout, body=None):
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "anthropic-version": "2023-06-01", "content-type": "application/json",
        "User-Agent": agent})
    return urllib.request.urlopen(request, timeout=timeout)


def response_headers(token, model, agent, timeout):
    """The response headers of one minimal inference request on `model`."""
    body = json.dumps({
        "model": model, "max_tokens": 1,
        "system": [{"type": "text",
                    "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
        "messages": [{"role": "user", "content": "."}]}).encode()
    with _open(MESSAGES_URL, token, agent, timeout, body) as response:
        return response.headers


def _window(headers, name):
    used = headers.get(f"{_PREFIX}{name}-utilization")
    if used is None:
        return None
    reset = headers.get(f"{_PREFIX}{name}-reset")
    return {"utilization": round(float(used) * 100, 1),
            "resets_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(int(reset))) if reset else None}


def windows(token, agent, timeout):
    """The five_hour/seven_day blocks; raises without a 7d header."""
    headers = response_headers(token, PROBE_MODEL, agent, timeout)
    out = {}
    for key, name in (("five_hour", "5h"), ("seven_day", "7d")):
        block = _window(headers, name)
        if block:
            out[key] = block
    if "seven_day" not in out:
        raise RuntimeError("no anthropic-ratelimit-unified-7d-utilization header")
    return out


def scoped(token, limits, agent, timeout):
    """{(model name, group): {'percent', 'resets_at'}} per model-scoped limit, read
    from a request on the newest listed model carrying the scope's name."""
    live = {}
    names = {((it.get("scope") or {}).get("model") or {}).get("display_name")
             for it in limits or []}
    for name in filter(None, names):
        try:
            with _open(MODELS_URL, token, agent, timeout) as response:
                listed = json.load(response).get("data") or []
            model = next(m["id"] for m in listed  # newest first
                         if name.lower() in str(m.get("display_name")).lower().split())
            headers = response_headers(token, model, agent, timeout)
            for key in headers.keys():
                found = re.fullmatch(re.escape(_PREFIX) + r"((5h|7d)_\w+)-utilization", key.lower())
                block = _window(headers, found.group(1)) if found else None
                if found and block:
                    live[name, {"5h": "session", "7d": "weekly"}[found.group(2)]] = {
                        "percent": block["utilization"], "resets_at": block["resets_at"]}
        except Exception:  # an unread scope keeps its value and age
            continue
    return live
