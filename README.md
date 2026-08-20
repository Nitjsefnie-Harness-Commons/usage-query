# usage-query

Rate-limit and quota utilization across Claude Code, Kimi and Codex accounts.

## Install

Not on PyPI. Every release publishes the wheel with a `SHA256SUMS` file beside
it, and checking against it is the point: fetching "the newest release" is
otherwise a promise about a URL, not about the artifact CI built.

```sh
gh release download v1.0.0 --repo Nitjsefnie-Harness-Commons/usage-query
sha256sum -c SHA256SUMS
pip install ./usage_query-1.0.0-py3-none-any.whl
```

The `usage-query` command queries all three providers by default, or one with
`--claude`, `--kimi`, or `--codex`. Use `--json` for machine-readable output;
`--quiet` suppresses per-account error lines. The command reports its own
version with `--version`.

The importable implementation is `usage_query_lib.query`. It uses only the
Python standard library and reads the provider credential files used by the
official clients. Stale Claude and Kimi OAuth tokens are refreshed and their
rotated credentials are atomically written back, while Codex rate limits are
read through its supported app-server interface.
