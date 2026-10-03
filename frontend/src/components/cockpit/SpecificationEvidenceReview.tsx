import { useState } from 'react';
import { API_URL } from '../../config/constants';
import { apiFetch } from '../../lib/api';
import type { WorkBoardProposal, WorkBoardTask } from '../../types';
import { validatedPacket } from './TaskEvidencePanel';
import type { Packet } from './TaskEvidencePanel';

export interface SpecificationReplacement {
  expected_packet_revision: number;
  expected_packet_digest: string;
  acknowledge_execution_use: true;
}
export interface SpecificationAcceptance {
  expected_proposal_revision: number;
  expected_parent_revision: number;
  execution_replacement?: SpecificationReplacement;
}

export function specificationScope(task: WorkBoardTask, proposal: WorkBoardProposal, ownerSessionId?: string | null) {
  return JSON.stringify([ownerSessionId, task.owner_principal_id, task.owner_session_id,
    task.goal_id, task.goal_revision, task.task_id, task.task_revision, proposal.proposal_id, proposal.proposal_revision,
    proposal.parent_revision, proposal.proposal_digest]);
}

export function retainedSpecificationAcceptance(scope: string): SpecificationAcceptance | null {
  const raw = sessionStorage.getItem(`seraph.specify-accept.v1:${scope}`);
  if (raw === null) return null;
  if (raw.length > 2048) throw new Error('Retained acceptance exceeds its limit. Inspect the proposal before recovery.');
  const body = JSON.parse(raw) as SpecificationAcceptance;
  if (!body || Object.keys(body).some(key => !['expected_proposal_revision', 'expected_parent_revision', 'execution_replacement'].includes(key))
    || !Number.isInteger(body.expected_proposal_revision) || body.expected_proposal_revision < 1
    || !Number.isInteger(body.expected_parent_revision) || body.expected_parent_revision < 1) throw new Error('Retained acceptance is unavailable.');
  const replacement = body.execution_replacement;
  if ('execution_replacement' in body && (!replacement || typeof replacement !== 'object'
    || Object.keys(replacement).length !== 3
    || !Number.isInteger(replacement.expected_packet_revision) || replacement.expected_packet_revision < 1
    || !/^[a-f0-9]{64}$/.test(replacement.expected_packet_digest)
    || replacement.acknowledge_execution_use !== true)) throw new Error('Retained execution acknowledgment is unavailable.');
  return body;
}

export function retainSpecificationAcceptance(scope: string, body: SpecificationAcceptance): SpecificationAcceptance {
  const existing = retainedSpecificationAcceptance(scope);
  if (existing) {
    if (existing.expected_proposal_revision !== body.expected_proposal_revision
      || existing.expected_parent_revision !== body.expected_parent_revision) throw new Error('Retained acceptance belongs to another revision.');
    return existing; // Explicit retry keeps the original reviewed body.
  }
  const raw = JSON.stringify(body);
  if (raw.length > 2048) throw new Error('Acceptance exceeds its finite limit.');
  const key = `seraph.specify-accept.v1:${scope}`;
  sessionStorage.setItem(key, raw);
  if (sessionStorage.getItem(key) !== raw) throw new Error('Acceptance could not be retained before sending.');
  return body;
}

export function SpecificationEvidenceReview({ task, proposal, ownerSessionId, onReplacement }: {
  task: WorkBoardTask; proposal: WorkBoardProposal; ownerSessionId?: string | null;
  onReplacement: (scope: string, replacement: SpecificationReplacement | null) => void;
}) {
  const scope = specificationScope(task, proposal, ownerSessionId);
  const [inspected, setInspected] = useState<{ scope: string; packet: Packet } | null>(null);
  const [ackScope, setAckScope] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const packet = inspected?.scope === scope ? inspected.packet : null;
  const recovered = !ownerSessionId || ownerSessionId !== task.owner_session_id;
  const target = proposal.proposed_tasks[0];
  const supported = ['browser.public-task.v1', 'work.evidence-dossier.v1', 'work.local-evidence-report.v1'].includes(target?.capability_id ?? '');
  let pending = false;
  try { pending = retainedSpecificationAcceptance(scope) !== null; } catch { /* Mutation fails closed in the sender. */ }

  async function inspect() {
    setBusy(true); setError(null); setAckScope(null); onReplacement(scope, null);
    try {
      const response = await apiFetch(`${API_URL}/api/work-board/tasks/${encodeURIComponent(task.task_id)}/evidence`);
      if (!response.ok) throw new Error('The current private evidence packet is unavailable.');
      const current = validatedPacket(await response.json());
      setInspected({ scope, packet: current });
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  }

  const eligible = packet !== null && packet.revision > 0 && packet.digest !== null
    && packet.claims.length > 0 && packet.invalidated_count === 0 && supported && !recovered;
  return <section aria-label="Specify execution evidence review" className="mt-2 rounded border border-white/10 p-2">
    <p>Execution evidence is separate from model context and execution approval. A bound typed input change requires a reviewed replacement in this acceptance.</p>
    {pending && <p role="status">A reviewed acceptance is retained. Accept proposal explicitly retries that exact request; reload sends no mutation.</p>}
    <button type="button" disabled={busy || recovered || pending} onClick={() => void inspect()}>Inspect replacement evidence</button>
    {error && <p role="alert">{error}</p>}
    {packet && <>
      <p>Packet revision {packet.revision} · digest {packet.digest} · {packet.claims.length} source spans</p>
      {packet.claims.map(claim => <p key={`${claim.source_id}:${claim.line_start}`}>{claim.text}</p>)}
      {!eligible && <p role="status">Review current eligible evidence for this supported consumer before replacing its binding.</p>}
      <label><input type="checkbox" checked={ackScope === scope} disabled={!eligible || pending}
        onChange={event => {
          const checked = event.currentTarget.checked;
          setAckScope(checked ? scope : null);
          onReplacement(scope, checked && packet.digest ? { expected_packet_revision: packet.revision,
            expected_packet_digest: packet.digest, acknowledge_execution_use: true } : null);
        }} />Replace execution evidence with this exact reviewed packet when accepting Specify</label>
    </>}
  </section>;
}
