# usage-query

Rate-limit and quota utilization across Claude Code, Kimi, Codex and z.ai accounts.

## Install

Not on PyPI. Every release publishes the wheel with a `SHA256SUMS` file beside
it, and checking against it is the point: fetching "the newest release" is
otherwise a promise about a URL, not about the artifact CI built.

```sh
gh release download v1.9.1 --repo Nitjsefnie-Harness-Commons/usage-query
sha256sum -c SHA256SUMS
pip install ./usage_query-1.9.1-py3-none-any.whl
```

The `usage-query` command queries all four providers by default, or one with
`--claude`, `--kimi`, `--codex`, or `--zai`. Use `--json` for machine-readable output;
`--quiet` suppresses per-account error lines. The command reports its own
version with `--version`.

Claude reads the live account from `~/.claude/.credentials.json` as usual.
When `~/.claude/accounts/live` names an installed `*.json` stash in that
directory, the default query and `--claude` also report every installed Claude
account. Other files and directories are ignored. A missing or empty accounts
directory, or a missing or unknown marker, keeps the existing output.
This command reads the marker; it does not switch accounts or write it.

JSON keeps `usage.claude` and all of `usage`'s provider keys unchanged, and
adds a top-level `claude_accounts` object. For example:

```json
{
  "claude_accounts": {
    "alpha": {
      "live": true,
      "five_hour": {"pct": 0.0, "resets_at": "no reset scheduled (window inactive)", "resets_in": "—", "pace_pct": null, "recover_in": null},
      "weekly": {"pct": 0.0, "resets_at": "no reset scheduled (window inactive)", "resets_in": "—", "pace_pct": null, "recover_in": null}
    }
  }
}
```

The live entry reuses `usage.claude`'s window readings. Inactive accounts use
their stash's token, refreshing stale credentials atomically into that same
account's current credential file with mode `0600`. Refresh HTTP requests run
without holding the accounts lock. Persistence takes `accounts/.lock`, re-reads
`live`, and writes to `.credentials.json` if the account is now live, or its
stash otherwise. The query also takes a brief lock to read the account name
and credentials together before HTTP. Lock acquisition retries for up to 30 seconds and reclaims a lock
whose modification time is older than 30 seconds. A lock timeout leaves both
credential locations untouched, records an account error, and still uses the
refreshed access token for this reading. Without an accounts directory, live
credential refresh keeps its existing behavior.

Their readings have separate five-minute caches
beside the stashes (`.<name>.usage-cache`); the live account keeps its existing
cache. A failed account is omitted and named under `errors` as
`claude:<name>`, while the others still report. Text output keeps the live
`Claude` rows and places each inactive account's `Claude <name>` rows after
them, in the same columns. An unreadable directory or marker is reported as
`errors.claude_accounts`, preserving the live result.

When `~/.agent-bundle/scripts/openrouter_key.py` exists, every provider selection
also runs it as `credits --json` with a timeout. Its returned object appears
under the top-level `openrouter` key, for example
`{"total_credits": 170.0, "total_usage": 166.6, "balance": 3.36}`; text output
adds `OpenRouter balance $3.36`. An absent helper adds nothing. A failed helper
adds one `errors.openrouter` entry and no balance. Account or helper errors
make the command exit nonzero; `--quiet` suppresses their text error lines.

The z.ai provider resolves its API key the way the bundle's `spawn_zai`
launchers do — `$ZAI_API_KEY`, then `~/.config/claude-zai/api-key`, then the
copy shipped beside those launchers, then a harness auth file's `zai.key`
(`~/.pi/agent/auth.json`) — and authenticates with the bare key in the
`Authorization` header — no `Bearer` prefix. Every z.ai window line carries the
peak/off-peak billing state at query time (`peak 1x` / `off-peak 0.5x`). Human
output also names the peak and off-peak hours and the next transition; JSON
exposes the same information once under `usage.zai._billing`. It is computed
from the published policy window (Mon–Fri 14:00–18:00 UTC+8) because no z.ai
endpoint exposes it; the rule and its provenance live at the top of
`usage_query_lib/query.py` for re-verification when the policy moves.

Time-limited promotions from the same page are modelled the same way, as
frozen, commented constants beside the peak window (expired windows stay in
the code — they are history, not config — and simply report inactive):

- **All-day off-peak promotion** — 2026-09-25 00:00 through 2026-10-07 24:00
  UTC+8 (end exclusive), every hour bills the off-peak 0.5x rate, including
  the weekday peak window; the next-transition scan skips past would-be peaks
  inside the promotion.
- **GLM-5.3-Flash usage campaign** — nightly windows 23:00–09:00 UTC+8 for
  every date from 2026-09-03 through 2026-10-07, doubling that model's
  available quota. This is quota, not a billing rate, and the page does not
  say whether it stacks with the off-peak promotion — so it is never folded
  into the multiplier. It is reported as its own `campaign` entry under
  `usage.zai._billing` (active now or not, the 2x quota multiplier, the
  window hours in UTC+8 and machine-local, the current window's end or the
  next one's start with the time left, and when the offer ends). While the
  campaign period runs, the billing line carries the full clause once, e.g.
  `GLM-5.3-Flash 2x quota nightly 23:00-09:00 UTC+8 (17:00-03:00 local)
  until 2026-10-08 09:00 UTC+8, next window in 8h00m` (or `active now,
  window ends in …` inside a window), and each window row carries only the
  short form `GLM-5.3-Flash 2x starts in 8h00m` / `GLM-5.3-Flash 2x active,
  ends in 8h30m` — the rows share one column width with every provider's,
  so a long label would widen the whole table. Outside the period nothing
  is printed.
  Through ZCode the same window is zero-consumption; this tool is not ZCode,
  so it reports the doubling.

z.ai accounts can hold **weekly quota resets** too. Beside the quota, the z.ai
query reads `GET /api/biz/customer-package-reset/list?targetType=PERSONAL`
with the same key and headers, cached in its own file with the same TTL and
stale fallback. It is fetched only alongside a live quota fetch, so a run
the quota cache answers makes no network call at all. The z.ai `7d` row ends
with the shared banked-resets annotation — how many weekly resets are
available and when the earliest of them expires, machine-local like every
other reset time — e.g. `4 banked resets available (earliest expires
2026-10-28 17:13, in 29d6h)` for an expiry of 2026-10-29 00:13:36 UTC+8 seen
from Europe/Prague; a stale list appends `; cached Ns`. JSON carries the
private `usage.zai._weekly_resets` (`available`, `earliest_expires_at`,
`earliest_expires_in`, plus `stale_age` when served from an old cache) with
the public `usage.zai.banked_resets` beside it. Zero available prints
nothing on the row but stays `available: 0` in the private block; if the
list cannot be read at all, both keys are absent and the quota rows print
exactly as they would without it. Five-hour resets from the same payload are
not reported.

Codex accounts can hold **banked rate-limit resets** — a credit the account
holder spends deliberately, unlike the scheduled reset that arrives on its own.
It renders in the same three shapes as z.ai's weekly resets: the same trailing
annotation on the account's first whole-account row (a "Full reset" restores
every window, and the annotation is worded identically to z.ai's), the same
footer hint naming where one is spent (for Codex, the app-server RPC
`account/rateLimitResetCredit/consume`), and the public
`usage.codex.banked_resets` in JSON beside the private
`_reset_credits_available` and `_reset_credits`. This tool never consumes a
credit — it only reports them, and it stays silent entirely when there is
nothing to spend.

A Codex plan window at 100% is not an exhausted account when it holds a
**credit balance**: the backend keeps serving from the balance until it runs
out, the overage limit is reached, or spend control stops it. When the usage
payload carries a `credits` block, a spent window reads `SPENT (credits
serving)` instead of `OVER PACE` (or `SPENT` when the credits cannot be used), a
footer line names the balance and the backend's approximate message counts, and
JSON carries `usage.codex.credits` with `balance`, `has_credits`, `unlimited`,
`overage_limit_reached`, `spend_control_reached`, the approximate message
ranges and `usable`. An account whose payload has no `credits` block prints
exactly as before.

The importable implementation is `usage_query_lib.query`. It uses only the
Python standard library and reads the provider credential files used by the
official clients. Stale Claude, Kimi **and Codex** OAuth tokens are refreshed
and their rotated credentials are atomically written back.

Codex is read from `$CODEX_HOME/auth.json` and the ChatGPT backend directly, and
deliberately **without starting `codex app-server`**: booting the CLI to answer
one question costs a full start — marketplace refreshes with their own git
fetches, and on Windows a console window in front of whoever is working — which
a status line asking every 30 s paid roughly once a minute. Codex also keeps its
own, much longer cache TTL (`CODEX_CACHE_TTL`, 900 s) for the same reason: the
shared 30 s TTL expired in lockstep with a 30 s status-line refresh, so
essentially every refresh was a miss, and a quota percentage does not move
meaningfully inside a quarter of an hour.

## Development

```sh
python3 run_tests.py                                             # the suite
git ls-files -co --exclude-standard '*.py' | xargs pylint        # lint
git ls-files -co --exclude-standard '*.py' | xargs pycodestyle   # lint
pyright                                                          # types
pip-audit -r requirements-dev.txt -r requirements-test.txt       # audit
actionlint .github/workflows/*.yml && zizmor .github/workflows/  # workflows
```

`pip install -r requirements-dev.txt -r requirements-test.txt` gets the
pinned toolchain. Use `-co --exclude-standard`, not a bare `git ls-files`:
a brand-new module is untracked until you stage it, and pylint would
otherwise report a clean run over every file except the one you just
wrote.

Coverage:

```sh
for s in tests/test_*.py; do
  python3 -m coverage run --parallel-mode --source=usage_query_lib "$s"
done
python3 -m coverage combine && python3 -m coverage report
```

Each suite in its own subprocess, because that is what `run_tests.py`
does — measuring a different execution shape than CI runs would report
coverage for a program nobody executes. Gated at **54%**, a ratchet
under the current 56.4%, not a target. Raise it as coverage climbs;
never lower it to turn a build green.

### CI

| Workflow | What it does |
| --- | --- |
| `tests` | `run_tests.py` across 3 OSes × 3 Pythons, plus a single-run coverage job — the matrix would otherwise report the same coverage number nine times. |
| `lint` / `types` | pylint + pycodestyle, and pyright. |
| `codeql` | Security analysis (Python only — no JS here). Findings go to the Security tab, never the build. Weekly cron on top of push, because a query published today would otherwise only ever run against files touched after it shipped. |
| `audit` | `pip-audit` over both requirements files, resolving the full transitive tree. **Daily** cron: this answers "is a version we froze months ago still safe", and that answer changes with no commit here to hang it on. |
| `actionlint` | `actionlint` + `zizmor` over the workflow YAML. A broken workflow does not go red, it silently stops running. |
| `tag` | Watches `usage_query_lib/__init__.py`. When `__version__` changes on `main`, it waits for every other check on that commit and then pushes `v<version>`. |
| `release` | Fires on that tag: runs every suite, builds the wheel, and publishes `SHA256SUMS` beside it. |

**There is deliberately no speed gate here**, unlike the dashboard repos.
pytest cannot run these suites — the tests are plain functions taking
helper arguments, which pytest reads as fixture requests — and the
comparator needs `--junitxml`, which only pytest emits. Rather than
reshape the tests to suit a gate, the gate is omitted.

**Releasing = bumping `__version__`.** `tag` creates the tag, `release`
publishes from it. Nothing bumps the version automatically: deciding
patch-vs-minor is a judgement about what changed.

**Actions are hash-pinned**, with the version in a trailing comment. Do
not "tidy" one back to `@v4`: a tag is a moving pointer, and these jobs
run with a repository token. Dependabot keeps the hashes current.

**`.gitignore` is deny-by-default**: `*` first, then each shipped path
named back. `build/`, `dist/` and `*.egg-info/` need no rules at all now —
they are simply never named back. A new file of an unlisted type is
invisible to git and will NOT appear in `git status`;
`git check-ignore -v <path>` names the rule hiding it. Never "fix" it by
loosening the leading `*`.
