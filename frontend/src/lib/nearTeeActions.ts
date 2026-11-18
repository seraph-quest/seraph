import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import { NEAR_TEE_API_BASE, normalizeModelFabricSettings } from "./modelFabric";
import type { ModelFabricSettingsStatus, NearTeeSetupInput, NearTeeSlotName, NearTeeSlotStatus } from "./modelFabric";

/**
 * Shared NEAR TEE settings actions (#1280): the slot save payload, the free
 * catalog check and one billed capability check. The Model card and the
 * detailed NEAR TEE panel both use these, so neither re-implements the
 * consent or revision rules.
 */

/** Stored slot values a save sends back. */
export interface NearTeeSlotFields {
  model_id: string;
  enabled: boolean;
  max_input_tokens: number;
  max_output_tokens: number;
  timeout_seconds: number;
  catalog_ttl_hours: number;
  /** Sent only when set, so an unchanged slot stays byte-for-byte unchanged. */
  temperature?: number | null;
  max_batch_size?: number | null;
}

export interface NearTeeSlotSaveOptions {
  slot: NearTeeSlotName;
  fields: NearTeeSlotFields;
  /** Only ever true from an operator's own tick in the same render. */
  acknowledged: boolean;
  /** Vision only: grant or withdraw the separate screenshot consent; omitted keeps it. */
  screenshot?: "grant" | "withdraw" | null;
  /** A new key; blank or omitted keeps the stored key. */
  credential?: string;
  revision: number;
}

/** The PUT /api/settings/model-fabric body for one NEAR TEE slot. */
export function nearTeeSlotSavePayload({ slot, fields, acknowledged, screenshot, credential, revision }: NearTeeSlotSaveOptions): Record<string, unknown> {
  const slotInput = {
    model_id: fields.model_id, enabled: fields.enabled,
    max_input_tokens: fields.max_input_tokens, max_output_tokens: fields.max_output_tokens,
    timeout_seconds: fields.timeout_seconds,
    catalog_ttl_hours: fields.catalog_ttl_hours,
    ...(fields.temperature != null ? { temperature: fields.temperature } : {}),
    ...(fields.max_batch_size != null ? { max_batch_size: fields.max_batch_size } : {}),
    plaintext_provider_egress_acknowledged: fields.enabled && acknowledged,
    // Sent only to grant (true) or to withdraw (false); omitted, an
    // unchanged slot keeps its current screenshot consent.
    ...(slot === "vision" && screenshot === "withdraw" ? { screenshot_plaintext_egress_acknowledged: false }
      : slot === "vision" && fields.enabled && screenshot === "grant" ? { screenshot_plaintext_egress_acknowledged: true } : {}),
  };
  // Only this slot is sent; the backend keeps the other stored slots.
  const value: NearTeeSetupInput = {
    schema_version: "seraph.near.tee.setup.v1", api_base: NEAR_TEE_API_BASE,
    slots: { [slot]: slotInput },
    ...(credential?.trim() ? { api_key: credential } : {}),
  };
  return { expected_policy_revision: revision, near_tee: value };
}

/** A key-only save: no slot is sent, so every stored slot is kept. */
export function nearTeeKeySavePayload(credential: string, revision: number): Record<string, unknown> {
  const value: NearTeeSetupInput = { schema_version: "seraph.near.tee.setup.v1", api_base: NEAR_TEE_API_BASE, slots: {}, api_key: credential };
  return { expected_policy_revision: revision, near_tee: value };
}

/** The stored values of a slot, unchanged. */
export function nearTeeSlotFields(slot: NearTeeSlotStatus): NearTeeSlotFields {
  return {
    model_id: slot.model_id, enabled: slot.enabled, max_input_tokens: slot.max_input_tokens,
    max_output_tokens: slot.max_output_tokens, timeout_seconds: slot.timeout_seconds,
    catalog_ttl_hours: slot.catalog_ttl_hours,
    temperature: slot.temperature ?? null, max_batch_size: slot.max_batch_size ?? null,
  };
}

/** A refused or failed request. `status` is null when the request never got an answer. */
export class NearTeeRequestError extends Error {
  readonly status: number | null;
  constructor(message: string, status: number | null) {
    super(message);
    this.name = "NearTeeRequestError";
    this.status = status;
  }
}

export async function responseDetail(response: Response): Promise<string> {
  try {
    const body = await response.json();
    const value = body?.detail;
    return typeof value === "string" ? value : JSON.stringify(value ?? response.status);
  } catch { return String(response.status); }
}

async function post(path: string, body: unknown): Promise<Response> {
  try {
    return await apiFetch(API_URL + path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (reason) {
    throw new NearTeeRequestError(reason instanceof Error ? reason.message : "network error", null);
  }
}

/** The free catalog check: reads NEAR's catalog record, never runs inference. */
export async function postNearTeeCatalogCheck(revision: number): Promise<ModelFabricSettingsStatus | null> {
  const response = await post("/api/settings/model-fabric/near-tee/catalog-check", { expected_policy_revision: revision });
  if (!response.ok) throw new NearTeeRequestError(await responseDetail(response), response.status);
  return normalizeModelFabricSettings(await response.json());
}

export interface NearTeeCheckResult { outcome: string; error_code: string | null }

/** One capability check: one billed NEAR request. */
export async function postNearTeeCapabilityCheck(
  slot: Pick<NearTeeSlotStatus, "profile_id" | "capability_check_timeout_seconds">,
  capability: string,
  structuredMode?: "json_schema" | "prompt_json",
): Promise<NearTeeCheckResult> {
  const response = await post("/api/settings/model-fabric/canary", {
    profile_id: slot.profile_id, capability, timeout_seconds: slot.capability_check_timeout_seconds,
    proof_ttl_seconds: 86400, ...(structuredMode ? { structured_mode: structuredMode } : {}),
  });
  if (!response.ok) throw new NearTeeRequestError(await responseDetail(response), response.status);
  const body = await response.json();
  return { outcome: String(body?.outcome ?? "unknown"), error_code: typeof body?.error_code === "string" ? body.error_code : null };
}

/** Codes and statuses that mean NEAR itself did not answer. */
export function isNearUnavailable(code: string | null | undefined, status?: number | null): boolean {
  if (status === null || (typeof status === "number" && status >= 500)) return true;
  return Boolean(code && /(catalog_unavailable|_transport_uncertain|_http_error|_deadline_expired|_stream_deadline|_stream_interrupted)$/.test(code));
}
