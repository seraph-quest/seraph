# Security Policy

## Supported Branches

Security fixes should target the active development line:

- `develop`

Feature branches may contain in-progress work and are not treated as supported release lines.

## Reporting a Vulnerability

Do not open public GitHub issues for security-sensitive reports.

Instead:

1. Use GitHub private vulnerability reporting for this repository: `https://github.com/seraph-quest/seraph/security/advisories/new`
2. If that form is unavailable, email `nat@neurion.ai` with the affected branch, commit, environment details, impact, and reproduction steps.
3. Give the maintainers reasonable time to confirm and prepare a fix before public disclosure.

## Scope Notes

Seraph currently includes:

- a backend service with tool execution and approval paths
- workflow and MCP integration surfaces
- a browser operator workspace
- an optional native macOS daemon

Reports involving credential handling, tool boundaries, approval bypasses, sandbox escapes, secret leakage, or unsafe native/system actions are especially useful.
