---
sidebar_position: 1
---

# Git Workflow

Seraph uses a two-step integration flow:

- feature and fix branches merge into `develop`
- `develop` merges into `main` only when a human explicitly requests it

## Branching Model

```
feat/my-feature  →  develop  →  main
fix/broken-thing →  develop  →  main
docs/update-docs →  develop  →  main
chore/cleanup    →  develop  →  main
```

All active work happens on short-lived topic branches created from `develop`.

### Branch Prefixes

| Prefix | Use for |
|--------|---------|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `docs/` | Documentation changes |
| `chore/` | Maintenance, CI, dependencies |
| `refactor/` | Code restructuring without behavior change |

### Workflow

1. Create a branch from `develop`: `git checkout -b feat/my-feature develop`
2. Make your changes, commit with clear messages
3. Push and open a PR targeting `develop`
4. CI runs tests automatically
5. After review, merge to `develop`
6. Delete the feature branch

Only merge `develop` into `main` when a human explicitly asks for it.

## Versioning

Seraph uses **CalVer** (Calendar Versioning):

```
vYYYY.M.D[-patch]
```

Examples:
- `v2026.2.10` — release on Feb 10, 2026
- `v2026.2.10-1` — first patch on the same day
- `v2026.3.5` — release on Mar 5, 2026

Releases are tagged on `main` after the requested `develop` to `main` merge.

## Commit Messages

Use conventional-style prefixes:

```
feat: add morning briefing scheduler
fix: prevent duplicate memory consolidation
docs: update roadmap with Phase 3 progress
chore: upgrade FastAPI to 0.115
refactor: extract tool registry from agent factory
```

Keep the first line under 72 characters. Add a blank line and description body for complex changes.

## Pull Request Format

PR titles should stay short and concrete:

- `Add provider run controls`
- `Trace observer time source runtime boundaries`
- `Refresh failed-session retry readiness`

PR bodies should use the shared checklist format:

```md
## Done on develop
- [x] <already shipped baseline>

## Working in this PR
- [ ] <what this branch changes>

## Still to do after this PR
- [ ] <real remaining work after merge>

## Validation
- <command>
```

Keep the `Done on develop` section factual against `develop`. Do not mark branch-only work as already done.

## CI/CD

- **Tests**: Run on every PR and protected-branch update (`test.yml`)
- **Docs deploy**: Triggered on push to `main` when `docs/**` files change (`deploy-docs.yml`)
- **Releases**: Tagged on `main` after a requested promotion from `develop`

## What NOT to Do

- Don't push directly to `main` or `develop` — always use a PR
- Don't create long-lived feature branches — keep them short and focused
- Don't open feature PRs against `main` unless a human explicitly asks for that
