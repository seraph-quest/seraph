import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { EvidenceExecutionControls } from './EvidenceExecutionControls';

const fetchMock = vi.fn();
vi.mock('../../lib/api', () => ({ apiFetch: (...args: unknown[]) => fetchMock(...args) }));
const response = (value: unknown) => ({ ok: true, json: async () => value });
const props = { endpoint: '/api/work-board/tasks/task-a/evidence', taskId: 'task-a',
  taskRevision: 3, ownerSessionId: 'root-a', canEdit: true, packetRevision: 2,
  packetDigest: 'a'.repeat(64) };
const inspection = { task_id: 'task-a', task_revision: 3, binding_state: 'unbound', binding_count: 0, applied_result: null };
const preview = { task_id: 'task-a', task_revision: 3, packet_revision: 2, packet_digest: props.packetDigest,
  preview_digest: 'b'.repeat(64), executor_input_digest: 'c'.repeat(64), operation: 'bind', affected_slots: [] };
const request = { expected_task_revision: 3, expected_packet_revision: 2, expected_packet_digest: props.packetDigest,
  operation: 'bind', preview_digest: preview.preview_digest, idempotency_key: 'b1964ebd-c567-4a82-968b-a40976f13b9e', acknowledge_execution_use: true };
const storageKey = 'seraph:evidence-execution:root-a:task-a';

describe('EvidenceExecutionControls', () => {
  beforeEach(() => {
    sessionStorage.clear(); fetchMock.mockReset();
    fetchMock.mockImplementation((url: string) => Promise.resolve(response(url.endsWith('execution-preview') ? preview : inspection)));
  });
  async function prepare() {
    await screen.findByText(/Execution evidence: unbound/);
    fireEvent.click(screen.getByRole('button', { name: 'Preview execution evidence replacement' }));
    await screen.findByText(/Exact executor input SHA/);
  }

  it('requires a distinct unchecked execution acknowledgment and stores exact bytes before POST', async () => {
    fetchMock.mockImplementation(async (url: string, init: RequestInit) => {
      if (init.method === 'POST' && url.endsWith('execution-binding')) {
        expect(sessionStorage.getItem(storageKey)).toBe(init.body);
        return response({ task_revision: 4, binding_state: 'bound' });
      }
      return response(url.endsWith('execution-preview') ? preview : inspection);
    });
    render(<EvidenceExecutionControls {...props} />); await prepare();
    const ack = screen.getByRole('checkbox'); expect(ack).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Accept exact execution binding' })).toBeDisabled();
    fireEvent.click(ack); fireEvent.click(screen.getByRole('button', { name: 'Accept exact execution binding' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => url.endsWith('execution-binding') && init.method === 'POST')).toBe(true));
    const actual = JSON.parse(sessionStorage.getItem(storageKey)!);
    expect(actual).toMatchObject({ ...request, idempotency_key: expect.any(String) });
    expect(Object.keys(actual)).toHaveLength(7);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Retry exact execution request' })).toBeEnabled());
  });

  it('reload only inspects; timeout retains the same UUID and explicit retry uses identical bytes', async () => {
    sessionStorage.setItem(storageKey, JSON.stringify(request));
    render(<EvidenceExecutionControls {...props} />);
    await screen.findByText(/Exact execution request retained/);
    expect(fetchMock.mock.calls.every(([, init]) => init.method === 'GET')).toBe(true);
    fetchMock.mockRejectedValueOnce(new Error('Ambiguous response loss'));
    fireEvent.click(screen.getByRole('button', { name: 'Retry exact execution request' }));
    await screen.findByRole('alert');
    expect(sessionStorage.getItem(storageKey)).toBe(JSON.stringify(request));
    fireEvent.click(screen.getByRole('button', { name: 'Inspect execution binding' }));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Retry exact execution request' }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init.method === 'POST')).toHaveLength(2));
    const posts = fetchMock.mock.calls.filter(([, init]) => init.method === 'POST');
    expect(posts[0][1].body).toBe(posts[1][1].body);
  });

  it('hides old previews and resets acknowledgment when the original Root changes for the same task', async () => {
    const { rerender } = render(<EvidenceExecutionControls {...props} />); await prepare();
    fireEvent.click(screen.getByRole('checkbox'));
    rerender(<EvidenceExecutionControls {...props} ownerSessionId="root-b" canEdit={false} />);
    expect(screen.queryByText(/Exact executor input SHA/)).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Preview execution evidence replacement' })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([url, init]) => url.endsWith('execution-binding') && init.method === 'POST')).toHaveLength(0);
    await screen.findByText(/Execution evidence: unbound/);
  });

  it('blocks mutation when retained storage exceeds its finite bound or is corrupt', async () => {
    sessionStorage.setItem(storageKey, 'x'.repeat(2049));
    render(<EvidenceExecutionControls {...props} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('exceeds its bound');
    expect(screen.getByRole('button', { name: 'Preview execution evidence replacement' })).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('an old acknowledgment does not apply to a replaced packet', async () => {
    const { rerender } = render(<EvidenceExecutionControls {...props} />); await prepare();
    fireEvent.click(screen.getByRole('checkbox'));
    rerender(<EvidenceExecutionControls {...props} packetRevision={3} packetDigest={'d'.repeat(64)} />);
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.queryByText(/Exact executor input SHA/)).not.toBeInTheDocument();
    await screen.findByText(/Execution evidence: unbound/);
  });
});
