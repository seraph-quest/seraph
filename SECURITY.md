# Security Policy

## Supported Branches

Security fixes should target the active development line:

- `develop`

Feature branches may contain in-progress work and are not treated as supported release lines.

## Reporting a Vulnerability

Do not open public GitHub issues for security-sensitive reports.

Instead:

1. Use GitHub private vulnerability reporting if it is enabled for this repository.
2. If private reporting is not available, contact the maintainers directly through a private channel before sharing details publicly.
3. Include the affected branch, commit, environment details, impact, and reproduction steps if known.
4. Give the maintainers reasonable time to confirm and prepare a fix before public disclosure.

If you do not have a maintainer contact yet, open a minimal issue requesting a private security contact path without disclosing the vulnerability details.

## Scope Notes

Seraph currently includes:

- a backend service with tool execution and approval paths
- workflow and MCP integration surfaces
- a browser operator workspace
- an optional native macOS daemon

Reports involving credential handling, tool boundaries, approval bypasses, sandbox escapes, secret leakage, or unsafe native/system actions are especially useful.
