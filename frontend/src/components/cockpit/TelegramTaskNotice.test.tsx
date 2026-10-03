import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { TelegramTaskNotice } from "./TelegramTaskNotice";
import type { WorkBoardTask } from "../../types";

const fetchMock = vi.fn();
vi.mock("../../lib/api", () => ({ apiFetch: (...args: unknown[]) => fetchMock(...args) }));
const task = { task_id: "task-a", task_revision: 3, owner_principal_id: "owner-a",
  owner_session_id: "root-a", title: "private title", body: "private body" } as WorkBoardTask;
const response = (value: unknown) => ({ ok: true, json: async () => value });
beforeEach(() => fetchMock.mockReset());

it("sends only revision/key and delivers the exact persisted notice", async () => {
  fetchMock.mockResolvedValueOnce(response({ id: "outbox-a" })).mockResolvedValueOnce(response({ status: "delivered" }));
  render(<TelegramTaskNotice task={task} ownerSessionId="root-a" />);
  fireEvent.click(screen.getByRole("button", { name: "Send neutral Telegram notice" }));
  await screen.findByText(/accepted by the configured transport/);
  expect(fetchMock.mock.calls[0][0]).toMatch(/\/telegram\/tasks\/task-a\/notice$/);
  expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ expected_revision: 3, idempotency_key: expect.any(String) });
  expect(fetchMock.mock.calls[1][0]).toMatch(/\/telegram\/outbox\/outbox-a\/deliver$/);
  expect(screen.queryByText(task.title)).not.toBeInTheDocument();
});

it("keeps uncertain delivery on the same outbox and makes replacement explicit", async () => {
  fetchMock.mockResolvedValueOnce(response({ id: "outbox-a" })).mockResolvedValue(response({ status: "unknown" }));
  render(<TelegramTaskNotice task={task} ownerSessionId="root-a" />);
  fireEvent.click(screen.getByRole("button", { name: "Send neutral Telegram notice" }));
  await screen.findByText(/delivery is uncertain/);
  fireEvent.click(screen.getByRole("button", { name: "Retry same notice" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
  expect(fetchMock.mock.calls[2][0]).toMatch(/outbox-a\/deliver$/);
  expect(screen.getByRole("button", { name: "Send fresh notice and retire old controls" })).toBeInTheDocument();
});

it("keeps recovered history read-only and ignores late results after task changes", async () => {
  const view = render(<TelegramTaskNotice task={task} ownerSessionId="other-root" />);
  expect(screen.getByRole("button", { name: "Send neutral Telegram notice" })).toBeDisabled();
  view.rerender(<TelegramTaskNotice task={task} ownerSessionId="root-a" />);
  let finish!: (value: unknown) => void;
  fetchMock.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  fireEvent.click(screen.getByRole("button", { name: "Send neutral Telegram notice" }));
  view.rerender(<TelegramTaskNotice task={{ ...task, task_id: "task-b" }} ownerSessionId="root-a" />);
  finish(response({ id: "outbox-a" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Send neutral Telegram notice" })).not.toBeDisabled());
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
