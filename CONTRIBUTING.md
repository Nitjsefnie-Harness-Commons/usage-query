# Contributing

## Commit messages

Every commit message follows [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope)!: subject`. The scope and the `!` are optional. The type is one of
`build`, `chore`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `revert`,
`style` or `test`. A breaking change carries the `!`.

Messages an automation writes follow the same format: a bot's commits, and the
commit-message prefix Dependabot uses.

## Proposing a change

A change is proposed as a pull request, with its description filled in from
`.github/PULL_REQUEST_TEMPLATE.md`.

The `pr gate` workflow (`.github/workflows/pr-gate.yml`, running
Nitjsefnie-Actions/pr-gate) checks a non-draft pull request's description
against that template and its assigned issues, and its commits' attribution
(`require-commit-attribution: 'true'`) and Conventional Commits subjects. A
draft is checked when it is marked ready, and bot-authored pull requests are
not gated; the format above applies to them all the same.

Review, an external pull request's included, checks that each type and `!`
match the change. A wrong message is a required change, fixed by rewording the
commit before the merge.

## Development

The test, lint and audit commands are in the README's
[Development](README.md#development) section.
