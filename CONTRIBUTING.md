# Contributing to Seraph

Seraph is an active workspace-first AI guardian project. The fastest way to contribute well is to stay aligned with the current product shape and shipped-state docs before making changes.

## Before You Start

- Read [`README.md`](README.md) for the current public project story.
- Read [`docs/implementation/STATUS.md`](docs/implementation/STATUS.md) for what is already shipped on `develop`.
- Read the [`Project Constitution`](docs/implementation/00-project-constitution.md) for accepted direction.
- Use the GitHub Project and owning issue for current priority and execution state.

## Branching Rules

Never commit directly to `develop` or `main`.

Use one of these branch patterns from the base required by the owning issue:

- `feat/<short-name>`
- `fix/<short-name>`

Normally target `develop`. Epic #736 milestone PRs target its integration branch
as defined by ADR-005. Only merge `develop` into `main` when explicitly requested.

## Local Setup

Recommended local flow:

```bash
cp env.dev.example .env.dev
# Set OPENROUTER_API_KEY in .env.dev
./manage.sh -e dev local up
```

Useful commands:

```bash
./manage.sh -e dev local status
./manage.sh -e dev local logs backend
./manage.sh -e dev local logs frontend
./manage.sh -e dev local down
```

Optional daemon for screen awareness:

```bash
./daemon/run.sh
./daemon/run.sh --ocr
```

## Pull Requests

Good pull requests are scoped, validated, and explicit about shipped impact.

- Keep the PR focused on one change set.
- Update docs when the shipped product story changes.
- Add or update tests when behavior changes.
- Use the pull request template and keep the "Done on develop / Working in this PR / Still to do after this PR" sections honest.

## Issues and Proposals

- Use bug reports for regressions, broken setup, runtime failures, or mismatches between docs and shipped behavior.
- Use feature requests for proposals that improve the current guardian workspace direction.
- If you are unsure whether something fits the roadmap, start from [`SUPPORT.md`](SUPPORT.md) and link the relevant roadmap or status doc sections.

## Design Direction

Seraph is a workspace-first guardian system. The retired village/editor line is not part of the active repo direction and should not be revived in new contributions.
