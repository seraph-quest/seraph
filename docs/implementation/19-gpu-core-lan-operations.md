---
slug: /gpu-core-lan-operations
title: 19. GPU Core LAN Operations
---

# GPU Core LAN Operations

**Status:** Planned for issue [#741](https://github.com/seraph-quest/seraph/issues/741)

This runbook defines the acceptance contract for moving the Seraph core to the
GPU host. It does not claim that the deployment is live. The current shipped
Mac-core topology remains in [Current App Guide](./12-current-app-guide.md).

## Supported Boundary

The operator uses one trusted-CA HTTPS origin on the LAN. The browser must not
connect directly to backend, VLM-wrapper, model-server, database, or worker
ports. Those services remain host-internal; the HTTPS ingress is the sole
product entry point.

Authentication must provide login, server-side session validation, bounded
session refresh, and logout/revocation. Session cookies are `Secure`,
`HttpOnly`, and use an explicit `SameSite` policy. Ingress rejects unexpected
`Host` and mutating cross-origin requests, applies bounded login and API
throttles, and trusts forwarded client information only from the configured
local reverse proxy. Seraph runs as a single application worker while SQLite
and in-process scheduling remain authoritative.

The Mac native folder picker is not remotely available through the browser.
Screenshot-folder selection and Mac capture therefore remain explicitly
degraded until the paired edge ships; a server-local path is not presented as
a Mac folder.

## Lifecycle And Verification

Production commands are explicit and always use the production environment:

```bash
./manage.sh -e prod production config-validate
./manage.sh -e prod production start
./manage.sh -e prod production status
./manage.sh -e prod production logs
./manage.sh -e prod production restart
./manage.sh -e prod production rollback <40-hex-app-sha> <wrapper-image@sha256:64-hex-digest> </path/to/target-inventory.json>
./manage.sh -e prod production stop
```

`SERAPH_IMAGE_TAG` must equal the full current Git `HEAD`, and production builds
require a clean worktree. The wrapper image is digest-pinned. The active release
record stores the application SHA, wrapper digest, and accepted inventory
receipt path so a failed candidate or rollback can restore the entire tuple.

Before `start` or `rollback`, generate a fresh inventory JSON from the verified
GPU administrator shell. It must contain the configured SSH alias and exact
out-of-band-verified host-key fingerprint, an explicit UTC `captured_at`, raw
`ss -lntp` output, current Docker bridge addresses and `host-gateway` binding,
firewall results proving LAN ingress to 8000/8001/8004 is blocked, and the exact
wrapper digest/interface contract being accepted. Set its path in
`SERAPH_HOST_INVENTORY_RECEIPT`; rollback takes the target tuple's receipt as an
explicit third argument. Missing, stale, mismatched, wildcard-listener, or
unverified receipts fail closed.

The acceptance receipt must cover start, status, health, logs, restart
persistence, degraded dependency reporting, and rollback to the previous
known-good release. Do not infer readiness from a running process or configured
URL alone.

From the operator machine, verify the public origin with the trusted CA file:

```bash
curl --cacert /path/to/seraph-lan-ca.pem https://seraph.lan/health
```

Never use `curl -k` or disabled certificate verification as an acceptance
receipt. Record the effective origin, certificate identity, authenticated
login/session/refresh/logout results, restart result, and sanitized health
payload. Exercise Host, Origin, unauthenticated, revoked-session, and throttle
negative cases. Confirm internal service ports are not reachable as product
entry points from the LAN.

If an SSH host key changes, stop and compare the observed fingerprint with a
trusted out-of-band administrator record. A changed or unverified fingerprint
is a deployment blocker; do not bypass host-key verification. SSH remains an
administrator route for inventory, deployment, process inspection, and logs.
It is never Seraph application transport and must not become a user tunnel.

Operator-shell receipts are authoritative. When a Codex/Desktop process cannot
reach the LAN but the normal shell that launches Seraph can, record the agent
network limitation and retain the direct HTTPS operator-shell receipt. Do not
replace the product topology with a tunnel.

## Degraded Operation And Rollback

The public health/status surface must distinguish ingress, authentication,
backend, VLM wrapper, model backend, queue, storage, and paired-edge state.
Loss of the Mac edge degrades capture/native interaction without moving
canonical authority. Loss of VLM/model services leaves the cockpit and recovery
controls available and reports the failed dependency. Rollback restores the
last known-good application release and compatible persistent state; backup and
restore proof remains a separate migration gate.

An accepted in-flight request may finish during a graceful deployment or
configuration sync. New work must stop entering the retiring instance, and no
tool may claim that an external side effect was cancelled once execution has
crossed that boundary. The receipt must distinguish drained, cancelled-before-
execution, completed, and outcome-unknown work.

## Ticket And Evidence Mapping

- [#688](https://github.com/seraph-quest/seraph/issues/688) remains the
  production-readiness gate; #741 supplies a deployment receipt, not a blanket
  production-ready claim.
- [#695](https://github.com/seraph-quest/seraph/issues/695) owns the durable
  documentation slice represented by this runbook and linked guides.
- [#687](https://github.com/seraph-quest/seraph/issues/687) retains useful
  queue, wrapper-health, and direct-route evidence. Its Mac-core/public-service-
  port target is superseded by ADR-004 and the one-origin authenticated GPU-core
  contract.
