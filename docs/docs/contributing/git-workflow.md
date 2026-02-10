---
sidebar_position: 1
---

# Git Workflow

Seraph uses **trunk-based development** with `main` as the single integration branch.

## Branching Model

```
main (default, protected)
 ├── feat/my-feature    → PR → main
 ├── fix/broken-thing   → PR → main
 ├── docs/update-readme → PR → main
 └── chore/cleanup      → PR → main
```

All work happens on short-lived topic branches created from `main`. There are no `develop`, `release/*`, or `hotfix/*` branches.

### Branch Prefixes

| Prefix | Use for |
|--------|---------|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `docs/` | Documentation changes |
| `chore/` | Maintenance, CI, dependencies |
| `refactor/` | Code restructuring without behavior change |

### Workflow

1. Create a branch from `main`: `git checkout -b feat/my-feature main`
2. Make your changes, commit with clear messages
3. Push and open a PR targeting `main`
4. CI runs tests automatically
5. After review, merge to `main`
6. Delete the feature branch

## Versioning

Seraph uses **CalVer** (Calendar Versioning):

```
vYYYY.M.D[-patch]
```

Examples:
- `v2026.2.10` — release on Feb 10, 2026
- `v2026.2.10-1` — first patch on the same day
- `v2026.3.5` — release on Mar 5, 2026

Releases are tagged directly on `main`.

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

## CI/CD

- **Tests**: Run on every push to `main` and on all PRs (`test.yml`)
- **Docs deploy**: Triggered on push to `main` when `docs/**` files change (`deploy-docs.yml`)
- **Releases**: Tagged on `main`, no separate release branches

## What NOT to Do

- Don't push directly to `main` — always use a PR
- Don't create long-lived feature branches — keep them short and focused
- Don't use `develop` or `release/*` branches — we use trunk-based development
