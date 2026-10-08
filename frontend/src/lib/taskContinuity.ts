import { API_URL } from "../config/constants";
import { apiFetch } from "./api";

export interface TaskContextPacket {
  task_id: string;
  goal_id: string;
  revision: number;
  status: string;
  summary: string;
  summary_kind: "factual_canonical_timeline";
  conversation_ids: string[];
  verified_artifact_refs: string[];
  private_source_refs: string[];
  open_questions: string[];
  next_actions: string[];
  unresolved_effect: string | null;
  ownership_access: "current" | "recovered_read_only";
  evidence_state: string;
  assistant_context_state: string;
  source_egress: { source_id: string; private_source: boolean; model_context_allowed: boolean }[];
  truncated: boolean;
}

async function readResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail?.code ?? "task_context_unavailable");
  }
  return response.json();
}

export async function readTaskContext(taskId: string): Promise<TaskContextPacket> {
  return readResponse(await apiFetch(`${API_URL}/api/sessions/task-context/${encodeURIComponent(taskId)}`));
}

export async function continueTask(packet: TaskContextPacket, conversationId: string): Promise<string> {
  const result = await readResponse<{ conversation_id: string }>(await apiFetch(`${API_URL}/api/sessions/continue-task`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: packet.task_id, expected_revision: packet.revision, new_conversation_id: conversationId }),
  }));
  return result.conversation_id;
}
