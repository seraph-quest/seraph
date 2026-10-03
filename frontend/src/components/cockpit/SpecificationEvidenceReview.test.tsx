import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { WorkBoardProposal, WorkBoardTask } from '../../types';
import { SpecificationEvidenceReview, retainSpecificationAcceptance, retainedSpecificationAcceptance, specificationScope } from './SpecificationEvidenceReview';
const fetchMock = vi.fn();
vi.mock('../../lib/api', () => ({ apiFetch: (...args: unknown[]) => fetchMock(...args) }));
const task = { task_id: 'task-a', task_revision: 2, owner_principal_id: 'operator-a',
  owner_session_id: 'root-a' } as WorkBoardTask;
const proposal = { proposal_id: 'proposal-a', proposal_revision: 1, parent_revision: 2,
  proposal_digest: 'd'.repeat(64), proposed_tasks: [{ capability_id: 'browser.public-task.v1' }] } as WorkBoardProposal;
const packet = { revision: 3, digest: 'a'.repeat(64), claims: [{ source_id: 'b'.repeat(64),
  source_digest: 'c'.repeat(64), source_kind: 'canonical_memory', version: 'current',
  line_start: 1, line_end: 1, text: '<script>Literal private fact</script>', confidence: null,
  freshness: 'current', memory_id: 'memory-a', model_context_allowed: true }],
  invalidated_count: 0, excluded_source_ids: [], blocked_sources: [], allow_model_context: false };
describe('SpecificationEvidenceReview', () => {
  beforeEach(() => { sessionStorage.clear(); fetchMock.mockReset(); fetchMock.mockResolvedValue({ ok: true, json: async () => packet }); });
  it('requires explicit inspection and unchecked execution acknowledgment; renders text literally', async () => {
    const changed = vi.fn();
    render(<SpecificationEvidenceReview task={task} proposal={proposal} ownerSessionId="root-a" onReplacement={changed} />);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Inspect replacement evidence' }));
    await screen.findByText(packet.claims[0].text);
    await waitFor(() => expect(screen.getByRole('button')).toBeEnabled());
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    fireEvent.click(screen.getByRole('checkbox'));
    expect(changed).toHaveBeenLastCalledWith(specificationScope(task, proposal, 'root-a'), {
      expected_packet_revision: 3, expected_packet_digest: packet.digest, acknowledge_execution_use: true });
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
  });
  it('same task with a new Root immediately hides prior acknowledgment and private projection', async () => {
    const { rerender } = render(<SpecificationEvidenceReview task={task} proposal={proposal} ownerSessionId="root-a" onReplacement={vi.fn()} />);
    fireEvent.click(screen.getByRole('button')); await screen.findByText(packet.claims[0].text);
    await waitFor(() => expect(screen.getByRole('button')).toBeEnabled()); fireEvent.click(screen.getByRole('checkbox'));
    rerender(<SpecificationEvidenceReview task={task} proposal={proposal} ownerSessionId="root-b" onReplacement={vi.fn()} />);
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.queryByText(packet.claims[0].text)).not.toBeInTheDocument();
    expect(screen.getByRole('button')).toBeDisabled();
  });
  it('persists exact initial reviewed request and returns it for explicit retry after reload', () => {
    const scope = specificationScope(task, proposal, 'root-a');
    const body = { expected_proposal_revision: 1, expected_parent_revision: 2,
      execution_replacement: { expected_packet_revision: 3, expected_packet_digest: packet.digest, acknowledge_execution_use: true as const } };
    expect(retainSpecificationAcceptance(scope, body)).toEqual(body);
    expect(retainedSpecificationAcceptance(scope)).toEqual(body);
    expect(retainSpecificationAcceptance(scope, { expected_proposal_revision: 1, expected_parent_revision: 2 })).toEqual(body);
    render(<SpecificationEvidenceReview task={task} proposal={proposal} ownerSessionId="root-a" onReplacement={vi.fn()} />);
    expect(screen.getByRole('status')).toHaveTextContent('retained');
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it('bounds retained storage before parsing and blocks altered revision or acknowledgment', () => {
    const scope = specificationScope(task, proposal, 'root-a');
    const key = `seraph.specify-accept.v1:${scope}`;
    sessionStorage.setItem(key, 'x'.repeat(2049));
    expect(() => retainedSpecificationAcceptance(scope)).toThrow('limit');
    sessionStorage.setItem(key, JSON.stringify({ expected_proposal_revision: 1, expected_parent_revision: 2,
      execution_replacement: { expected_packet_revision: 3, expected_packet_digest: packet.digest, acknowledge_execution_use: 1 } }));
    expect(() => retainedSpecificationAcceptance(scope)).toThrow('acknowledgment');
  });
});
