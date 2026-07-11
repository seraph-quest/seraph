---
slug: /gpu-core-lan-operations
title: 19. GPU Core LAN Operations
---

# GPU Core LAN Operations

**Status:** Planned for issue [#741](https://github.com/seraph-quest/seraph/issues/741)

This runbook defines the acceptance contract for operating the Seraph core on
the current GPU host, `jupyter` (`192.168.1.26`). It does not by itself claim
that every production acceptance gate is complete.

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

The repository, frontend, backend, canonical data, VLM wrapper, and model
server are local to `jupyter`. Administrators use direct Docker, process,
listener, filesystem, and log inspection; they do not SSH from jupyter back to
jupyter. The target Mac edge captures consented screenshots and pushes them
through authenticated ingest. That paired-edge upload is planned in #749; no
current screenshot upload API exists, so this is not shipped push support. The
Mac owns no canonical state and has no direct access to ports 8000, 8001, or
8004.

The Mac native folder picker is not remotely available through the browser.
Screenshot-folder selection and Mac capture therefore remain explicitly
degraded until the paired edge ships; a server-local path is not presented as
a Mac folder.

## Lifecycle And Verification

Production commands are explicit and always use the production environment:

```bash
./manage.sh -e prod production config-validate
./manage.sh -e prod production start
./manage.sh -e prod production accept /path/to/signed-mac-receipt.json
./manage.sh -e prod production status
./manage.sh -e prod production logs
./manage.sh -e prod production restart
./manage.sh -e prod production accept-restart /path/to/signed-mac-receipt.json
./manage.sh -e prod production rollback <40-hex-app-sha> <wrapper-image@sha256:64-hex-digest>
./manage.sh -e prod production accept-rollback /path/to/signed-mac-receipt.json
./manage.sh -e prod production accept-restore /path/to/signed-mac-receipt.json
./manage.sh -e prod production abort <candidate|rollback|restore|restart>
./manage.sh -e prod production stop
```

`SERAPH_IMAGE_TAG` must equal the full current Git `HEAD`, and production builds
require a clean worktree. The wrapper image is digest-pinned. The active release
record stores the application SHA, wrapper digest, and accepted inventory
receipt path so a failed candidate or rollback can restore the entire tuple.

`production start` is a staged operation. Before cutover it automatically
generates a local identity/listener receipt and refuses a wildcard/LAN model
listener, an old wrapper on 8001, or an unexpected backend on 8004. After the
private Compose candidate starts, it automatically binds an attestation to the
actual wrapper container/image/network and tests `/health`, `/health/backend`,
`/queue/status`, unauthenticated `/health/chat` denial, and authenticated
`/health/chat` success. It then stops at `candidate awaiting LAN acceptance`
without writing accepted state.

From the operator Mac, generate measured and signed evidence (this is deployment
acceptance, not #749 paired-edge identity). First copy the non-secret exact
stage challenge from
`$PID_DIR/seraph-prod-<candidate|rollback|restore|restart>-challenge.json` on
the GPU host to the Mac. Then run:

```bash
python3 scripts/generate_mac_lan_acceptance.py \
  --challenge-file /path/to/seraph-prod-candidate-challenge.json \
  --https-origin https://seraph.lan \
  --lan-host seraph.lan \
  --lan-ip 192.168.1.26 \
  --trusted-ca /path/to/seraph-lan-ca.pem \
  --operator-secret-file /path/to/operator-login-secret \
  --probe-key-file /path/to/distinct-mac-probe-hmac-key \
  --client-identity operator-mac > /path/to/signed-mac-receipt.json
```

The generator requires DNS for the HTTPS hostname to include the pinned LAN IP,
logs in, proves the authenticated session, actively attempts TCP connections to
that exact IP on 8000/8001/8004, records only bounded refusal/timeout classes,
and signs canonical JSON with HMAC-SHA256. DNS mismatch, no-route, and
unclassified network errors fail closed. The GPU host pins the exact expected
origin, LAN hostname, LAN IP, client label, and a distinct nonempty probe HMAC
key file. That HMAC key is separate from the operator login secret. The receipt
must contain the exact server challenge and its digest. Mac receipt nonces and
server challenge nonces are recorded as separate consumed authorities and
neither can be replayed. Operator login and probe key material never appear in
the receipt.

Start, rollback, restart, and automatic restoration stop in explicit
candidate/rollback/restart/restore-awaiting-LAN states. Their matching
`accept*` command freshly reattests the unchanged three-container/network
binding. Every mutating production command is serialized by one nonblocking
lifecycle `flock`; a concurrent mutation is rejected. Acceptance prepares the
local and Mac evidence copies, validates the completed immutable bundle, and
prepares the consumed nonce/challenge ledger and active state before publishing
the active state last. The immutable bundle contains the app/VLM tuple, Compose
project and network ID, ingress/backend/VLM container IDs, ingress/backend image
IDs and revision labels through the local attestation, the wrapper RepoDigest,
hashed evidence copies, and expected origin/client identity. Live identities
are read again immediately before publication. A failed pre-publication attempt
can leave only hash-named, read-only orphan evidence in `releases/`; it cannot
create active state, and those unreferenced files may be removed during a later
maintenance cleanup after confirming no state or bundle references them.

`production abort <stage>` runs under the same lifecycle lock, removes only the
named stage and its challenge, and never accepts it. When previous accepted
state exists, abort restores that tuple locally and leaves it in `restore
awaiting LAN acceptance`, requiring fresh `accept-restore` evidence. With no
previously accepted tuple, abort stops production. A failed local restoration
is catastrophic; a successful restoration is not called accepted until
`accept-restore` receives new Mac evidence.

The candidate attestation can also be reproduced locally for diagnosis:

```bash
python3 scripts/generate_local_gpu_inventory.py \
  --expected-vlm-image "$SERAPH_VLM_IMAGE" \
  --ingress-container seraph-prod-ingress-1 \
  --backend-container seraph-prod-backend-1 \
  --vlm-container seraph-prod-vlm-wrapper-1 \
  --vlm-api-key-file "$SERAPH_VLM_API_KEY_FILE" \
  > /path/to/gpu-host-inventory.json
```

The script hashes the local machine-id in memory and emits only its SHA-256
digest. It inspects all three containers, their immutable image identities,
revision labels, shared network identity and fixed private addresses, the
running wrapper's RepoDigest, network/port state, and tested interface
behaviors. Locally built legacy images without the pinned identity are
non-accepted. Local firewall parsing is not an acceptance gate; the Mac-side
negative reachability receipt is authoritative for LAN isolation.

Bootstrap `SERAPH_GPU_MACHINE_IDENTITY_SHA256` once at a trusted local console
by hashing `/etc/machine-id` without printing its raw value. Review and pin the
digest in protected deployment configuration. Inventory generation reports the
observed digest but never updates the expected value; mismatches require an
explicit identity investigation and repinning ceremony.

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

Administration from this workspace is local. A separate remote administrator
would still verify host identity normally, but that is not the repository's
local lifecycle path and never becomes Seraph application transport. LAN
acceptance uses the authenticated HTTPS origin plus negative reachability proof
for internal ports; do not replace the product topology with a tunnel.

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
