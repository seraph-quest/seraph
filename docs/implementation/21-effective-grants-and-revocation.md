# Effective grants and revocation

**Status: Partial.** This describes the branch-local permission composition path;
reviewed program integration and managed UI proof are required before shipped
`develop` truth. The existing adapter/goal/provider/approval stores remain the
authority. There is no additional permission database.

Each row displays its stable owned record reference and grant ID before the
operator chooses a control. Already-disabled goals have no revoke control;
enabled goals that are currently blocked still permit an exact revision-bound
local stop.

Connections displays a bounded current authenticated-root projection with the
boundary (observation, inference transfer, external mutation or credential),
purpose, source, destination, revision, expiry, origin and related waiting or
running work. Private bodies, source addresses, account labels, credential
references, tokens and foreign-root metadata are excluded. Credentials are
configuration evidence, never permission. The displayed snapshot and digest
are readback information and cannot authorize dispatch.

Controls call the owning Calendar, Mail, audio, goal, schedule, source-watch,
GitHub, Telegram or node operation. A source-consent revoke also fences its
model-transfer consent; the UI describes that cascade. Revoke uses the owning
revision and, where its owner supports recovery, the same immutable request key
on retry. A stale revision requires fresh readback. It never silently binds a
new request to historical authority.

Local revocation fences future dispatch before credential/private artifact
cleanup. A cleanup failure reports partial failure with the actual local
readback and an unconfirmed external outcome. An unavailable readback does
not establish local success. Retrying exact cleanup does not resend the
original effect. No toggle claims universal account revocation or undo of a
contacted external effect. An active GitHub reservation remains retained on
connection revoke; a post-contact authority loss becomes an unknown outcome,
requiring destination reconciliation before any retry. Connected credentials
remain separate from local execution consent.

Pending and approved unconsumed one-action approvals can be revoked in their
own store using the exact authenticated owner and displayed state revision.
The conditional transition serializes with consumption: revocation prevents
consumption, while a consumption winner returns `already_consumed`. A consumed
row remains spent historical evidence; the panel offers no undo toggle and
directs inspection of the effect/readback. Capability or source grants are
revoked separately. Neither operation mutates signed execution proofs.

Adapters re-read their exact owner and revision after DNS, immediately before
transport, and again before adopting returned evidence. Audio revalidates its
durable consent/transport lease; revoked running responses do not become
transcripts. Source-watch reads revalidate the exact plan, original owner/root,
source grant and finite goal period, so a revoked source cannot become a new
canonical baseline or local evidence. Source-watch stop is permitted for the
exact owner even when the former goal/grant is stale; that exception cannot
change sources, schedules or restart work. Policy/cost accounting is owned by
the model-fabric configuration and canonical ledger: incurred cost survives
revocation even when the returned output is withheld.

An explicit finite public source-watch standing grant is separate from an interactive
bearer. Original browser logout or idle expiry does not revoke that service
grant. The immutable original root/principal still identifies its owner; a new
root never inherits it. Identity revocation/deletion, source/watch/goal grant
revocation, expiry of the finite reviewed period or changed plan/source
revision fences service reads and adoption. Legacy records without that proof
remain visibly blocked. Read-only historical recovery cannot regrant or
execute old jobs. The UI's current-login inventory does not expose foreign
standing work merely because a new login uses the same deployment password.

The exemption requires a nonempty validated plan containing only
`public_https_text` sources. Standing-mode creation/review rejects workspace
or mixed sources and publishes this browser-expiry scope in owner readback.
Final read/adoption checks classify the persisted plan again: malformed or
unknown sources cannot acquire the exemption. Legacy private/mixed standing
plans are blocked; authenticated `approval_each_run` workspace observation
remains available while its original browser authority is live.

That exception permits public source observation only. Private Calendar/Mail
and procedure schedules retain their existing original-browser liveness,
connector/consent and current-scope review requirements. Connections evaluates
the owning read-only readiness predicates and shows `blocked`, the exact safe
reason, and the stored state separately. Local stop/revoke remains usable for
blocked rows. Calendar/Mail related jobs list only exact schedule-consent
bindings; same-goal tasks are not presumed to use those grants.

Provider egress policy is a deployment setting, labelled accordingly, and is
projected through its model-fabric owner. Missing provider metadata is degraded
and conveys no permission. Regrant requires explicit reviewed settings action;
credentials, readback, refresh and recovered history cannot restore it.

A metadata/network failure retains the last confirmed Connections view with a
stale warning and disabled controls. A finite authenticated-root change clears
that view and invalidates in-flight responses. Conversation navigation is not
the authority key. The panel validates bounded inventory and revoke readback
before replacing state; malformed 200 metadata is degraded rather than rendered.
Requests have a 15-second abort deadline. Revoke gesture keys support browsers
without randomUUID and remain unchanged across exact retries.

## Host-local legacy pairing reset

A foreign/legacy pairing is shown only as `re_pair_required`. A browser cannot
claim or overwrite it. For an intentional device reassignment, stop the
managed services and run the host-local command with the exact adapter and
current extension-state revision:

```bash
backend/.venv/bin/python scripts/reset-node-pairing.py \
  --workspace /absolute/canonical/workspace \
  --extension-id seraph.openclaw-node \
  --reference connectors/nodes/device.yaml \
  --expected-revision 12 \
  --acknowledge-owner-reset
```

The command requires an already compatible migrated workspace, fences that
single pairing first, invalidates only its exact credential generation, and
then clears its binding under the new exact state revision. A concurrent
rotation/reset or cleanup failure leaves a blocked/revoked state; it does not
advertise a completed reset. It preserves unrelated adapters, secrets and
history, and imports no old grants or owner identities. Fresh pairing is then
an explicit review under the current root. This command is not an HTTP route.
No real device reset, provider call or operator snapshot mutation is part of
the isolated verification.

## Verification limits

Focused authenticated API/SQLite and intercepted transport proof cover
inventory, wrong owner/private data exclusion, stale revisions, queued audio,
late running results, cleanup failure/exact retry, isolated pairing reset,
standing service expiry/identity revocation and DNS/response revocation.
These are mechanical boundary receipts, not a live provider/account claim.
Canonical learning is explicitly `no_learning`; permission controls do not
infer operator preferences. The combined provider-policy/accounting module and
managed browser journey require the lead's integration receipt.
