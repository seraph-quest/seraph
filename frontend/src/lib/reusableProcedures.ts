import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type { WorkBoardTask } from "../types";
import { object } from "./researchMethods";

export interface ProcedureParameter {
  name: string; step_id: string; input_pointer: string; schema: Record<string, unknown>;
  producer_id: string; producer_contract_digest: string;
}
export interface ProcedureOffer extends Omit<ProcedureParameter, "name"> { offer_id: string }
export interface ProcedureSource {
  task_id: string; expected_revision: number; eligible: boolean; reason_code: string;
  source_attempt: string | null; parameter_offers: ProcedureOffer[];
  source_refs?: string[]; source_receipt?: unknown; source_current?: boolean;
}
const sha = (v: unknown) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const scalarSchema = (v: unknown): v is Record<string, unknown> => object(v)
  && ["string", "integer", "boolean", "null"].includes(String(v.type))
  && Object.keys(v).every(k => ["type", "minLength", "maxLength", "minimum", "maximum", "enum", "const", "pattern"].includes(k));
export function readProcedureSource(value: unknown, task: WorkBoardTask): ProcedureSource {
  if (!object(value) || value.task_id !== task.task_id || value.expected_revision !== task.task_revision
    || typeof value.eligible !== "boolean" || typeof value.reason_code !== "string" || !Array.isArray(value.parameter_offers)
    || value.parameter_offers.length > 16 || value.behavior_changed !== false || value.quality_evidence !== "unmeasured") throw Error("Source readback changed. Inspect the current completed Task again.");
  if (value.eligible && (typeof value.source_attempt !== "string" || value.source_current !== true
    || !object(value.scope) || value.scope.family !== "general" || value.scope.goal_id !== task.goal_id || value.scope.goal_revision !== task.goal_revision
    || task.latest_attempt?.attempt_id && task.latest_attempt.attempt_id !== value.source_attempt)) throw Error("Original Task attempt or Goal changed. Inspect again.");
  for (const offer of value.parameter_offers) {
    if (!object(offer) || !sha(offer.offer_id) || !sha(offer.producer_contract_digest) || typeof offer.producer_id !== "string"
      || Object.keys(offer).sort().join(",") !== "input_pointer,offer_id,producer_contract_digest,producer_id,schema,step_id"
      || typeof offer.step_id !== "string" || typeof offer.input_pointer !== "string" || !scalarSchema(offer.schema)) throw Error("Producer parameter offers are unconfirmed. Inspect again.");
  }
  if (new Set(value.parameter_offers.map(o => o.offer_id)).size !== value.parameter_offers.length) throw Error("Duplicate parameter offers. Inspect again.");
  return value as unknown as ProcedureSource;
}
export function readProcedureParameters(value: unknown): ProcedureParameter[] {
  if (!Array.isArray(value) || value.length > 16 || value.some(p => !object(p) || typeof p.name !== "string"
    || Object.keys(p).sort().join(",") !== "input_pointer,name,producer_contract_digest,producer_id,schema,step_id"
    || !/^[A-Za-z0-9_-]{1,64}$/.test(p.name) || !scalarSchema(p.schema)
    || typeof p.step_id !== "string" || typeof p.input_pointer !== "string" || !sha(p.producer_contract_digest) || typeof p.producer_id !== "string")
    || new Set(value.map(p => p.name)).size !== value.length) throw Error("Exact method parameters are unconfirmed. Inspect again.");
  return value as ProcedureParameter[];
}
export function parameterValue(parameter: ProcedureParameter, raw: string): string | number | boolean | null {
  const schema = parameter.schema;
  let value: string | number | boolean | null;
  switch (schema.type) {
    case "integer": if (!/^-?\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) throw Error(`Enter an integer for ${parameter.name}.`); value = Number(raw); break;
    case "boolean": if (!["true", "false"].includes(raw)) throw Error(`Choose true or false for ${parameter.name}.`); value = raw === "true"; break;
    case "null": if (raw !== "null") throw Error(`Choose null for ${parameter.name}.`); value = null; break;
    default: value = raw;
  }
  if (Array.isArray(schema.enum) && !schema.enum.includes(value)
    || Object.prototype.hasOwnProperty.call(schema, "const") && schema.const !== value
    || typeof value === "number" && (typeof schema.minimum === "number" && value < schema.minimum || typeof schema.maximum === "number" && value > schema.maximum)
    || typeof value === "string" && (typeof schema.minLength === "number" && [...value].length < schema.minLength || typeof schema.maxLength === "number" && [...value].length > schema.maxLength)) throw Error(`Value for ${parameter.name} does not match its producer schema.`);
  return value;
}
export async function procedureRequest(path: string, body?: unknown): Promise<unknown> {
  const response = await apiFetch(`${API_URL}${path}`, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = object(value) ? value.detail : null;
    const code = typeof detail === "string" ? detail : object(detail) && typeof detail.code === "string" ? detail.code : "request_blocked";
    throw Error(`${code}: method source, current authority or pointer changed. Inspect again for fresh canonical readback before retrying.`);
  }
  return value;
}
