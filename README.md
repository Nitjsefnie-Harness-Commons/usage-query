# usage-query

Rate-limit and quota utilization across Claude Code, Kimi, Codex and z.ai accounts.

## Install

Not on PyPI. Every release publishes the wheel with a `SHA256SUMS` file beside
it, and checking against it is the point: fetching "the newest release" is
otherwise a promise about a URL, not about the artifact CI built.

```sh
gh release download v1.2.1 --repo Nitjsefnie-Harness-Commons/usage-query
sha256sum -c SHA256SUMS
pip install ./usage_query-1.2.1-py3-none-any.whl
```

The `usage-query` command queries all four providers by default, or one with
`--claude`, `--kimi`, `--codex`, or `--zai`. Use `--json` for machine-readable output;
`--quiet` suppresses per-account error lines. The command reports its own
version with `--version`.

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
