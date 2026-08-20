# usage-query

Rate-limit and quota utilization across Claude Code, Kimi and Codex accounts.

Install: `pip install usage-query`

The `usage-query` command queries all three providers by default, or one with
`--claude`, `--kimi`, or `--codex`. Use `--json` for machine-readable output;
`--quiet` suppresses per-account error lines. The command reports its own
version with `--version`.

The importable implementation is `usage_query_lib.query`. It uses only the
Python standard library and reads the provider credential files used by the
official clients. Stale Claude and Kimi OAuth tokens are refreshed and their
rotated credentials are atomically written back, while Codex rate limits are
read through its supported app-server interface.
