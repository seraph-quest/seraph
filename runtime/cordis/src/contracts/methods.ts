/** Closed #1007 method inventory; authority is never carried in payloads. */
import { object, ref, digest, number, text, boolean, literal, nullable, array, bounded, type Infer } from './schema.js';
import { ProtocolError } from '../protocol.js';

const state = literal('accepted', 'queued', 'running', 'awaiting_approval', 'paused', 'succeeded', 'degraded', 'blocked', 'cancelled', 'failed', 'unknown_external_effect', 'cost_liability');
const capability = object({capability_id: ref, version: ref, state: literal('available', 'blocked'), reason_code: nullable(ref)});
const artifact = {artifact_ref: ref, digest, size_bytes: number(0, 65536)};
const outputs = {
 authority: object({authority_ref: ref, revision: number(1), mode: literal('operator-root', 'goal-programme')}),
 goal: object({goal_ref: ref, revision: number(1), title: text(200), description: text(8192)}),
 job: object({job_ref: ref, revision: number(1), state}),
 admission: object({job_ref: ref, revision: number(1), state, replayed: boolean}),
 inspection: object({job_ref: ref, revision: number(1), state, artifact_refs: array(ref, 16)}),
 receipt: object({receipt_ref: ref, revision: number(1)}),
 capabilities: object({capabilities: array(capability, 50), next_cursor: nullable(ref)}), capability,
 execution: object({receipt_ref: ref, artifact_refs: array(ref, 16)}),
 inference: object({receipt_ref: ref, output_ref: ref}),
 memories: object({records: array(object({record_ref: ref, text: text(8192), text_digest: digest}), 20)}),
 proposal: object({proposal_ref: ref, revision: number(1), state: literal('proposed', 'blocked')}),
 record: object({record_ref: ref, receipt_ref: ref}),
 artifact: object(artifact), artifactRead: object({...artifact, content: text(65536)}),
 adoption: object({...artifact, receipt_ref: ref}),
 plan: object({plan_ref: ref, revision: number(1)}),
 turn: object({turn_ref: ref, job_ref: ref, replayed: boolean}),
 message: object({message_ref: ref, revision: number(1)}),
 messages: object({messages: array(object({message_ref: ref, role: literal('user', 'assistant', 'step', 'error'), content: text(8192)}), 100), next_cursor: nullable(ref)}),
 schedule: object({schedule_ref: ref, revision: number(1), state: literal('active', 'disabled', 'blocked')}),
 jobs: object({job_refs: array(ref, 20)}),
 connection: object({connection_ref: ref, revision: number(1), state: literal('ready', 'blocked', 'revoked'), reason_code: nullable(ref)}),
 evidence: object({artifact_ref: ref, input_digest: digest, provider_digest: digest, config_digest: digest, evidence: array(object({source_ref: ref, text: text(4096)}), 64)}),
};
export const serviceSchemas = {
  'authority.resolve': {input: object({}), output: outputs.authority},
  'goals.read': {input: object({}), output: outputs.goal},
  'tasks.admit': {input: object({request_ref: ref}), output: outputs.admission},
  'tasks.inspect': {input: object({job_ref: ref}), output: outputs.inspection},
  'tasks.cancel': {input: object({job_ref: ref, expected_revision: number(1)}), output: outputs.job},
  'tasks.checkpoint': {input: object({checkpoint_ref: ref}), output: outputs.receipt},
  'tasks.settle': {input: object({outcome_ref: ref}), output: outputs.job},
  'capabilities.list': {input: object({cursor: nullable(ref), limit: number(1, 50)}), output: outputs.capabilities},
  'capabilities.describe': {input: object({capability_id: ref}), output: outputs.capability},
  'capabilities.invoke': {input: object({request_ref: ref}), output: outputs.execution},
  'inference.request': {input: object({request_ref: ref}), output: outputs.inference},
  'memory.retrieve': {input: object({query_ref: ref, limit: number(1, 20)}), output: outputs.memories},
  'memory.propose': {input: object({request_ref: ref}), output: outputs.proposal},
  'memory.applyReviewed': {input: object({review_ref: ref}), output: outputs.record},
  'memory.forget': {input: object({request_ref: ref}), output: outputs.record},
  'artifacts.read': {input: object({artifact_ref: ref, max_bytes: number(1, 65536)}), output: outputs.artifactRead},
  'artifacts.stage': {input: object({request_ref: ref}), output: outputs.artifact},
  'artifacts.adopt': {input: object({request_ref: ref}), output: outputs.adoption},
  'audit.append': {input: object({event_ref: ref}), output: outputs.receipt},
  'research.buildPlan': {input: object({task_ref: ref}), output: outputs.plan},
  'research.executeAccepted': {input: object({plan_ref: ref}), output: outputs.job},
  'conversation.accept': {input: object({turn_ref: ref}), output: outputs.turn},
  'conversation.append': {input: object({message_ref: ref}), output: outputs.message},
  'conversation.read': {input: object({conversation_ref: ref, limit: number(1, 100), before_message_ref: nullable(ref)}), output: outputs.messages},
  'conversation.cancel': {input: object({turn_ref: ref, expected_revision: number(1)}), output: outputs.job},
  'scheduler.register': {input: object({request_ref: ref}), output: outputs.schedule},
  'scheduler.disable': {input: object({schedule_ref: ref, expected_revision: number(1)}), output: outputs.schedule},
  'scheduler.dispatchDue': {input: object({limit: number(1, 20)}), output: outputs.jobs},
  'connections.inspect': {input: object({connection_ref: ref}), output: outputs.connection},
  'connections.invokeBoundAdapter': {input: object({operation_ref: ref}), output: outputs.execution},
  'agent-loop.startTurn': {input: object({turn_ref: ref}), output: outputs.job},
  'agent-loop.cancelTurn': {input: object({turn_ref: ref, expected_revision: number(1)}), output: outputs.job},
  'agent-loop.inspectTurn': {input: object({turn_ref: ref}), output: outputs.inspection},
  'source-extraction.extract': {input: object({artifact_ref: ref, acquisition_receipt_ref: ref, source_slot: number(0, 3), first_line: number(1, 4096), last_line: number(1, 4096)}), output: outputs.evidence},
} as const;
export type ServiceMethod = keyof typeof serviceSchemas;
export const SERVICE_METHODS = Object.freeze(Object.keys(serviceSchemas) as ServiceMethod[]);
export function isServiceMethod(value: unknown): value is ServiceMethod {
  return typeof value === 'string' && Object.hasOwn(serviceSchemas, value);
}
export type Input<M extends ServiceMethod> = Infer<(typeof serviceSchemas)[M]['input']>;
export type Output<M extends ServiceMethod> = Infer<(typeof serviceSchemas)[M]['output']>;
export type MemoryStatus<M extends ServiceMethod> = M extends 'memory.propose' ? 'proposal_only' : M extends 'memory.applyReviewed' ? 'reviewed_update' : M extends 'memory.forget' ? 'forgotten' : 'no_learning';
export type Result<M extends ServiceMethod> = {status: 'blocked'; reason_code: string; memory_status: 'no_learning'} | {status: 'succeeded'; value: Output<M>; memory_status: MemoryStatus<M>};
export function validateInput<M extends ServiceMethod>(method: M, value: unknown): Input<M> {
  if (!isServiceMethod(method)) throw new ProtocolError('unknown service method');
  const input = serviceSchemas[method].input.parse(bounded(value));
  if (method === 'source-extraction.extract') {
    const span = input as Input<'source-extraction.extract'>;
    if (span.last_line < span.first_line) throw new ProtocolError('invalid source span');
  }
  return input as Input<M>;
}
export function validateResult<M extends ServiceMethod>(method: M, value: unknown): Result<M> {
  if (!isServiceMethod(method)) throw new ProtocolError('unknown service method');
  const candidate = bounded(value);
  if (candidate && typeof candidate === 'object' && !Array.isArray(candidate) && 'status' in candidate && candidate.status === 'blocked') {
    return object({status: literal('blocked'), reason_code: ref, memory_status: literal('no_learning')}).parse(candidate);
  }
  const memoryStatus = method === 'memory.propose' ? 'proposal_only' : method === 'memory.applyReviewed' ? 'reviewed_update' : method === 'memory.forget' ? 'forgotten' : 'no_learning';
  return object({status: literal('succeeded'), value: serviceSchemas[method].output, memory_status: literal(memoryStatus)}).parse(candidate) as Result<M>;
}
