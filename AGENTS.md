# Agent Guidelines

## Git Branching Strategy

**Never commit directly to `develop` or `main`.**

1. **Feature/fix branches**: Always create a `feat/` or `fix/` branch off `develop` for your work.
2. **Merge to develop**: Once the feature branch is ready, merge it into `develop` (typically via PR).
3. **Merge to main**: Only merge `develop` into `main` when explicitly requested by the user.

```
feat/my-feature  →  develop  →  main
fix/my-bugfix    →  develop  →  main
```
