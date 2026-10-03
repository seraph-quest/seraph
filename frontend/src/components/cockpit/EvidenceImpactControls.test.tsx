import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EvidenceImpactControls } from './EvidenceImpactControls';
const fetchMock = vi.fn();
vi.mock('../../lib/api', () => ({ apiFetch: (...args: unknown[]) => fetchMock(...args) }));
const props = { endpoint: '/api/work-board/tasks/task-a/evidence', taskId: 'task-a', taskRevision: 3,
  ownerSessionId: 'root-a', sourceId: 'a'.repeat(64), canEdit: true };
const page = { source_id: props.sourceId, snapshot_digest: 'b'.repeat(64), cursor: null, next_cursor: null,
  tasks: [{ task_id: 'dependent-task', task_revision: 2, status: 'ready', stale: true, reason_code: 'evidence_dependency_stale' }],
  applied_result: null };
const response = (value: unknown) => ({ ok: true, json: async () => value });
const storageKey = `seraph:evidence-impact:root-a:task-a:${props.sourceId}`;
const request = { source_id: props.sourceId, cursor: null, expected_snapshot_digest: page.snapshot_digest,
  idempotency_key: 'b1964ebd-c567-4a82-968b-a40976f13b9e', acknowledge_safety_pause: true };
describe('EvidenceImpactControls', () => {
  beforeEach(() => { sessionStorage.clear(); fetchMock.mockReset(); fetchMock.mockResolvedValue(response(page)); });
  it('renders the safe bounded page and requires a separate acknowledgment with retained bytes before POST', async () => {
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        expect(sessionStorage.getItem(storageKey)).toBe(init.body);
        return response({ paused_task_ids: ['dependent-task'], retained_task_ids: [] });
      }
      const applied = sessionStorage.getItem(storageKey) !== null;
      return response({ ...page, applied_result: applied ? { paused_task_ids: ['dependent-task'], retained_task_ids: [] } : null });
    });
    render(<EvidenceImpactControls {...props} />);
    await screen.findByText('dependent-task');
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Evaluate reviewed impact page' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Evaluate reviewed impact page' }));
    await screen.findByRole('button', { name: 'Dismiss applied safety-pause request' });
    const body = JSON.parse(sessionStorage.getItem(storageKey)!);
    expect(body).toMatchObject({ ...request, idempotency_key: expect.any(String) });
    expect(Object.keys(body)).toHaveLength(5);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry exact safety pause' })).toBeEnabled());
  });
  it('reload inspects retained exact UUID/body with zero automatic POST; explicit retry preserves it', async () => {
    sessionStorage.setItem(storageKey, JSON.stringify(request));
    render(<EvidenceImpactControls {...props} />);
    await screen.findByText('dependent-task');
    expect(fetchMock.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true);
    expect(fetchMock.mock.calls[0][0]).toContain('pending_request=');
    fireEvent.click(screen.getByRole('button', { name: 'Retry exact safety pause' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => init?.body === JSON.stringify(request))).toBe(true));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry exact safety pause' })).toBeEnabled());
    expect(sessionStorage.getItem(storageKey)).toBe(JSON.stringify(request));
  });
  it('Root change removes old acknowledgment and projection while recovered mutation remains disabled', async () => {
    const { rerender } = render(<EvidenceImpactControls {...props} />);
    await screen.findByText('dependent-task'); fireEvent.click(screen.getByRole('checkbox'));
    rerender(<EvidenceImpactControls {...props} ownerSessionId="root-b" canEdit={false} />);
    expect(screen.queryByText('dependent-task')).not.toBeInTheDocument();
    await screen.findByText('dependent-task');
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Evaluate reviewed impact page' })).toBeDisabled();
    expect(fetchMock.mock.calls.every(([, init]) => init?.method !== 'POST')).toBe(true);
  });
  it('blocks oversized retained storage before parse or any HTTP', async () => {
    sessionStorage.setItem(storageKey, 'x'.repeat(4097));
    render(<EvidenceImpactControls {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('finite bound');
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
