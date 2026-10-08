export interface CordisRuntimeSnapshot {
  state: "ready" | "blocked" | "stopped" | "cleanup_unknown" | "starting" | "quiescing";
  reason: string | null; profile_id: string | null; cordis_version: string;
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
    || !nullableText(value.reason) || !nullableText(value.profile_id) || value.cordis_version !== "4.0.0-rc.10"
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
    profile_id: value.profile_id as string | null, cordis_version: value.cordis_version,
    node_version: value.node_version as string | null, composition_digest: value.composition_digest as string | null,
    package_digest: value.package_digest as string | null,
    plugins: value.plugins.map(p => ({ id: p.id, state: p.state === "ready" && !verified ? "blocked" : p.state, reason: p.state === "ready" && !verified ? "readiness_not_checked" : p.reason })),
    cleanup: { state: value.cleanup.state as CordisRuntimeSnapshot["cleanup"]["state"], process_reaped: value.cleanup.process_reaped,
      resources_remaining: value.cleanup.resources_remaining as number | null, cordis_disposal: value.cleanup.cordis_disposal as CordisRuntimeSnapshot["cleanup"]["cordis_disposal"] } };
}
