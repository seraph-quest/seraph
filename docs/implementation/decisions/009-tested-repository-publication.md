---
title: "ADR-009: Tested repository publication"
---

# ADR-009: Tested repository publication

**Status:** Accepted target for #911. Implementation and platform readiness are
not claimed shipped.

**Decision class:** Additive capability-runtime contract

## Context

An approved repository repair and a passing test do not authorize publishing
code to an external repository. Publication also needs a selected canonical Git
base, proof that the published bytes were actually tested, a fresh approval,
bounded local execution, and independently verified external effects.

## Decision

The existing capability runtime owns typed tested-repair publication. A separate
durable publication root depends on a succeeded repair; it never reopens that
terminal repair or uses it as a running parent. Immutable bindings cover the
original authenticated principal and finite root, goal/revision, repair job,
task/attempt/proposal, input/artifact digests, selected repository/connection
revision, Git base, test/runtime proof, public text, new branch and ready PR.

The actual repair executor captures its materialized pre-patch and pre-test
paths, bytes and effective modes. Publication must prove that the selected Git
base reconstructs exactly those tested base inputs and that the patched tree
matches the actual tested and quiescent post-test inputs. Missing legacy
attestation, unexplained ignored/untracked files, subtree/commit/byte/mode
mismatch, or changed source/config/runtime proof blocks publication. Independent
digests alone do not establish equivalence. The existing bounded source walker
and ordinary-file scope are retained.

The same fresh, exact approval covers `local_host_execution`, named Git object
writes, creation of a new ref, and creation of the exact ready PR. The old repair
approval is historical evidence only. Preview, approval and adopted job authority
bind selected effective profile/posture/config revision and immutable content;
source and authority are rechecked before execution and at effect boundaries.
No scheduler or model response automatically publishes a repair.

Local Git constructs a commit in a separate job-owned worktree with fixed
commands, finite wall/output bounds, explicit identity and a closed environment.
Operator hooks/config/filters/credentials are not inherited, and the operator
tree is not reset or modified. Production remote effects use pinned GitHub Git
Data REST and the existing protected connection/credential/effect ledger,
including final authority checks after destination resolution. Git subprocesses
never receive remote credentials or perform network publication.

Each effect has durable exact intent before dispatch and independent object,
tree, commit, ref and PR readback afterward. Unknown effects survive restart;
reconciliation performs GET-only readback and never automatically replays a
remote write. Cancellation cannot erase uncertain effects. Success records a
durable verified result with an explicit `no_learning` outcome; no merge or
release is part of publication.

## Optional bounded Python profile

Add the explicit selected `repo-python-pytest-publication-v1` profile alongside
`repo-python-pytest-v1` and `repo-node24-npm-v1`. The existing Python default and
its commands remain unchanged; profile selection uses existing settings and
executor seams, without downloads or dependency installation.

The selected trusted interpreter uses fixed `-I -S -B` and a fixed bootstrap.
Its closed import exposure consists of the attested interpreter/stdlib,
lib-dynload and actual loaded libpython, job-owned copies of pytest, _pytest,
pluggy, packaging, iniconfig and pygments, and the bounded staged project.
Import pytest before exposing project paths. Exclude bytecode caches, site/user
packages, `.pth` processing, ambient import paths and plugin autoload. Internal
`-p no:cacheprovider` is part of the recorded effective test arguments.

The child receives only declared effective environment values: private job
HOME/TMPDIR, fixed locale and PATH, and plugin-autoload disablement. No ambient
environment is inherited, including `PYTEST_ADDOPTS`, other pytest controls,
Python path/site controls, loader injection controls or credentials. Record the
exact effective environment and bootstrap digest with the tested input.

Runtime bounds are 96 MiB aggregate, 8,000 regular files, depth 16, 48 MiB per
file, and a separate 15-second attestation deadline within the existing job
deadline. Hash every exposed path/byte/mode, safely copy immutable intended
runtime inputs, and rehash actual materialization before spawn and after process
quiescence. Safe link resolution must retain/check link text, in-root target
identity and content through no-follow traversal. Prove the actual loaded
libpython; a sysconfig filename alone is insufficient. Unproven resolution,
unexpected exposure, changed inputs or exceeded bounds makes the profile
unavailable. Do not hash a package subset while exposing ambient dependencies.

Local execution explicitly retains host-user authority: `isolation_claim=none`
and network isolation is not verified. This bounded import/runtime proof is not
an OS sandbox or a claim that every OS library/kernel dependency is hermetic.
Optional platform readiness requires actual proof; unavailable Mac execution
or missing host tooling is shown as blocked. Optional execution availability
must not create a global Linux/process dependency or prevent the CPU core and
cockpit from starting on another supported host.

## Consequences and verification

This extends the existing capability, approvals, settings, jobs and GitHub
adapter seams; it adds no independent runtime, authority store or scheduler.
ADR-001 inference-only authority and ADR-003 canonical memory remain unchanged.
Portable host topology is owned separately by ADR-008; this ADR accepts no new
core host requirement.

Acceptance requires an authenticated real repair producer/test execution through
the selected profile, fresh exact publication approval, real bounded local Git,
intercepted protected REST serialization/readback and durable operator-visible
success. Local bare-remote Git proof and intercepted production REST proof have
separate identities; neither alone proves the full journey. Negative source,
mode, environment, config, owner/root and authority races must cause zero
remote writes. Restart, cancellation, uncertainty and idempotency must prove
GET-only reconciliation without duplicate branches or PRs. Real-account and
Mac execution remain unverified until separately authorized operational proof.
