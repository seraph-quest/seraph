export interface CordisRuntimeSnapshot {
  state: "ready" | "blocked" | "stopped" | "cleanup_unknown" | "starting" | "quiescing";
  reason: string | null; recovery: string | null; profile_id: string | null; cordis_version: string;
  node_version: string | null; composition_digest: string | null; package_digest: string | null;
  readiness: { state: "verified" | "unknown" | "blocked"; checked_at: number | null };
  plugins: { id: string; state: "ready" | "blocked" | "stopped"; reason: string | null }[];
  cleanup: { state: "not_started" | "pending" | "clean" | "unknown"; process_reaped: boolean;
    resources_remaining: number | null; cordis_disposal: "not_started" | "confirmed" | "unconfirmed" };
}
const record = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
const nullableText = (v: unknown) => v === null || (typeof v === "string" && v.length <= 256);
export function normalizeCordisRuntime(value: unknown): CordisRuntimeSnapshot | null {
  if (!record(value) || !["ready", "blocked", "stopped", "cleanup_unknown", "starting", "quiescing"].includes(String(value.state))
    || !nullableText(value.reason) || !(value.recovery === undefined || nullableText(value.recovery)) || !nullableText(value.profile_id) || value.cordis_version !== "4.0.0-rc.10"
    || !nullableText(value.node_version) || !nullableText(value.composition_digest) || !nullableText(value.package_digest)
    || !Array.isArray(value.plugins) || value.plugins.length > 64 || value.plugins.some(p => !record(p) || typeof p.id !== "string"
      || p.id.length > 128 || !["ready", "blocked", "stopped"].includes(String(p.state)) || !nullableText(p.reason))
    || !record(value.cleanup) || !["not_started", "pending", "clean", "unknown"].includes(String(value.cleanup.state))
    || typeof value.cleanup.process_reaped !== "boolean" || !(value.cleanup.resources_remaining === null || (Number.isSafeInteger(value.cleanup.resources_remaining) && Number(value.cleanup.resources_remaining) >= 0))
    || !["not_started", "confirmed", "unconfirmed"].includes(String(value.cleanup.cordis_disposal))) return null;
  // Construct the public projection. Unknown fields (keys, environment, paths,
  // process IDs or protocol nonces) can never enter UI rendering or retention.
  const freshness = value.readiness;
  if (freshness != null && (!record(freshness) || !["verified", "unknown", "blocked"].includes(String(freshness.state))
    || !(freshness.checked_at === null || (Number.isSafeInteger(freshness.checked_at) && Number(freshness.checked_at) >= 0 && Number.isFinite(new Date(Number(freshness.checked_at)).getTime())))
    || (freshness.state === "verified" && freshness.checked_at === null))) return null;
  const readiness = record(freshness) ? { state: freshness.state as CordisRuntimeSnapshot["readiness"]["state"], checked_at: freshness.checked_at as number | null }
    : { state: "unknown" as const, checked_at: null };
  const verified = readiness.state === "verified";
  return { state: value.state === "ready" && !verified ? "blocked" : value.state as CordisRuntimeSnapshot["state"],
    reason: value.state === "ready" && !verified ? "readiness_not_checked" : value.reason as string | null, readiness,
    recovery: (value.recovery as string | null | undefined) ?? null,
    profile_id: value.profile_id as string | null, cordis_version: value.cordis_version,
    node_version: value.node_version as string | null, composition_digest: value.composition_digest as string | null,
    package_digest: value.package_digest as string | null,
    plugins: value.plugins.map(p => ({ id: p.id, state: p.state === "ready" && !verified ? "blocked" : p.state, reason: p.state === "ready" && !verified ? "readiness_not_checked" : p.reason })),
    cleanup: { state: value.cleanup.state as CordisRuntimeSnapshot["cleanup"]["state"], process_reaped: value.cleanup.process_reaped,
      resources_remaining: value.cleanup.resources_remaining as number | null, cordis_disposal: value.cleanup.cordis_disposal as CordisRuntimeSnapshot["cleanup"]["cordis_disposal"] } };
}

/** Which agent loop chat runs on (#1019): never inferred, only the server's owner row. */
export interface ChatRuntimeStatus {
  loop: "cordis.agent-loop.v1" | "legacy.smolagents" | "blocked";
  route: string; owner_epoch: number | null; reason: string | null; recovery: string | null;
  owner_kind: "legacy" | "cordis" | null;
}
export function normalizeChatRuntime(value: unknown): ChatRuntimeStatus | null {
  if (!record(value) || !["cordis.agent-loop.v1", "legacy.smolagents", "blocked"].includes(String(value.loop))
    || typeof value.route !== "string" || value.route.length > 64
    || !(value.owner_epoch === null || (Number.isSafeInteger(value.owner_epoch) && Number(value.owner_epoch) >= 1))
    || !nullableText(value.reason) || !(value.recovery === null || (typeof value.recovery === "string" && value.recovery.length <= 512))
    || (value.loop === "blocked") !== (value.reason !== null)
    || !(value.owner_kind === undefined || value.owner_kind === null || value.owner_kind === "legacy" || value.owner_kind === "cordis")) return null;
  return { loop: value.loop as ChatRuntimeStatus["loop"], route: value.route, owner_epoch: value.owner_epoch as number | null,
    reason: value.reason as string | null, recovery: value.recovery as string | null,
    owner_kind: (value.owner_kind ?? null) as ChatRuntimeStatus["owner_kind"] };
}
export function chatRuntimeLabel(status: ChatRuntimeStatus): string {
  if (status.loop === "blocked") return `Chat blocked · ${status.reason}`;
  const loop = status.loop === "cordis.agent-loop.v1" ? "Cordis agent loop" : "legacy agent loop";
  return `Chat: ${loop} (${status.loop}) · route ${status.route}${status.owner_epoch === null ? "" : ` · owner epoch ${status.owner_epoch}`}`;
}

/** Which path scheduled guardian work takes (#1020): the server's guardian owner row, never inferred. */
export interface GuardianRuntimeStatus {
  loop: "cordis.guardian.v1" | "legacy.guardian" | "blocked";
  owner_epoch: number | null; reason: string | null; recovery: string | null;
}
export function normalizeGuardianRuntime(value: unknown): GuardianRuntimeStatus | null {
  if (!record(value) || !["cordis.guardian.v1", "legacy.guardian", "blocked"].includes(String(value.loop))
    || !(value.owner_epoch === null || (Number.isSafeInteger(value.owner_epoch) && Number(value.owner_epoch) >= 1))
    || !nullableText(value.reason) || !(value.recovery === null || (typeof value.recovery === "string" && value.recovery.length <= 512))
    || (value.loop === "blocked") !== (value.reason !== null)) return null;
  return { loop: value.loop as GuardianRuntimeStatus["loop"], owner_epoch: value.owner_epoch as number | null,
    reason: value.reason as string | null, recovery: value.recovery as string | null };
}
export function guardianRuntimeLabel(status: GuardianRuntimeStatus): string {
  if (status.loop === "blocked") return `Scheduled guardian work blocked · ${status.reason}`;
  const loop = status.loop === "cordis.guardian.v1" ? "Cordis guardian loop" : "legacy guardian path";
  return `Scheduled guardian work: ${loop} (${status.loop})${status.owner_epoch === null ? "" : ` · owner epoch ${status.owner_epoch}`}`;
}

/** One runtime domain owner row; a non-ready row carries the reason and its fix. */
export interface RuntimeDomainStatus {
  domain: string; owner_kind: "legacy" | "cordis" | null; epoch: number | null;
  state: "ready" | "draining" | "blocked" | "missing" | "unseeded" | "unavailable"; reason: string | null; recovery: string | null;
}
export function normalizeRuntimeDomains(value: unknown): RuntimeDomainStatus[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 64).flatMap((item): RuntimeDomainStatus[] => {
    if (!record(item) || typeof item.domain !== "string" || item.domain.length > 64
      || !["ready", "draining", "blocked", "missing", "unseeded", "unavailable"].includes(String(item.state))
      || !(item.owner_kind === null || item.owner_kind === "legacy" || item.owner_kind === "cordis")
      || !(item.epoch === null || (Number.isSafeInteger(item.epoch) && Number(item.epoch) >= 1))
      || !nullableText(item.reason) || !(item.recovery === null || (typeof item.recovery === "string" && item.recovery.length <= 512))) return [];
    return [{ domain: item.domain, owner_kind: item.owner_kind as RuntimeDomainStatus["owner_kind"], epoch: item.epoch as number | null,
      state: item.state as RuntimeDomainStatus["state"], reason: item.reason as string | null, recovery: item.recovery as string | null }];
  });
}
/** Domains an operator must act on: anything not ready, except a never-seeded inventory. */
export function blockedRuntimeDomains(domains: RuntimeDomainStatus[]): RuntimeDomainStatus[] {
  return domains.filter(item => item.state !== "ready" && item.state !== "unseeded");
}

/** Scheduler / dispatch lifecycle activation (#1025): the server's own state, never inferred. */
export interface LifecycleRuntimeStatus {
  domain: "seraph.scheduler.v1" | "seraph.dispatch.v1";
  state: "active" | "inactive" | "draining" | "blocked" | "disabled" | "unavailable";
  reason: string | null; recovery: string | null;
  owner: "legacy" | "cordis" | null; owner_epoch: number | null;
  transition: string | null;
  last_transition: { status: "succeeded" | "failed"; target: string; reason: string | null } | null;
  blocked_jobs: { job_id: string; reason: string }[];
}
const LIFECYCLE_DOMAINS = ["seraph.scheduler.v1", "seraph.dispatch.v1"];
const LIFECYCLE_STATES = ["active", "inactive", "draining", "blocked", "disabled", "unavailable"];
export function normalizeLifecycleRuntime(value: unknown): LifecycleRuntimeStatus | null {
  if (!record(value) || !LIFECYCLE_DOMAINS.includes(String(value.domain)) || !LIFECYCLE_STATES.includes(String(value.state))
    || !nullableText(value.reason) || !(value.recovery === null || value.recovery === undefined || (typeof value.recovery === "string" && value.recovery.length <= 1024))
    || !(value.owner === null || value.owner === undefined || value.owner === "legacy" || value.owner === "cordis")
    || !(value.owner_epoch === null || value.owner_epoch === undefined || (Number.isSafeInteger(value.owner_epoch) && Number(value.owner_epoch) >= 1))
    || !(value.transition === undefined || value.transition === null || (typeof value.transition === "string" && value.transition.length <= 64))) return null;
  const last = value.last_transition;
  const lastTransition = record(last) && (last.status === "succeeded" || last.status === "failed") && typeof last.target === "string"
    && last.target.length <= 32 && nullableText(last.reason ?? null)
    ? { status: last.status as "succeeded" | "failed", target: last.target, reason: (last.reason ?? null) as string | null } : null;
  const jobs = Array.isArray(value.blocked_jobs) ? value.blocked_jobs.slice(0, 64).flatMap((item): LifecycleRuntimeStatus["blocked_jobs"] =>
    record(item) && typeof item.job_id === "string" && item.job_id.length <= 128 && typeof item.reason === "string" && item.reason.length <= 256
      ? [{ job_id: item.job_id, reason: item.reason }] : []) : [];
  return { domain: value.domain as LifecycleRuntimeStatus["domain"], state: value.state as LifecycleRuntimeStatus["state"],
    reason: value.reason as string | null, recovery: (value.recovery ?? null) as string | null,
    owner: (value.owner ?? null) as LifecycleRuntimeStatus["owner"], owner_epoch: (value.owner_epoch ?? null) as number | null,
    transition: (value.transition ?? null) as string | null, last_transition: lastTransition, blocked_jobs: jobs };
}
export function lifecycleRuntimeLabel(status: LifecycleRuntimeStatus): string {
  const name = status.domain === "seraph.scheduler.v1" ? "Scheduled work" : "Work Board dispatch";
  const owner = status.owner === null ? "" : ` · ${status.owner} owner${status.owner_epoch === null ? "" : ` epoch ${status.owner_epoch}`}`;
  const transition = status.transition ? ` · transition: ${status.transition}` : "";
  if (status.state === "active") return `${name}: active${owner}${transition}`;
  if (status.state === "disabled") return `${name}: disabled${status.reason ? ` · ${status.reason}` : ""}`;
  return `${name}: ${status.state}${status.reason ? ` · ${status.reason}` : ""}${owner}${transition}`;
}
/** A lifecycle domain the operator must act on: not active and not deliberately disabled. */
export function lifecycleNeedsAction(status: LifecycleRuntimeStatus | null): boolean {
  return status !== null && status.state !== "active" && status.state !== "disabled";
}
