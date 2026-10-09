import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AudioDocumentationControls } from "./AudioDocumentationControls";
afterEach(() => vi.unstubAllGlobals());
const settings = { egress_revision: 2, profiles: [{ id: "openrouter.audio", profile_contract_hash: "a".repeat(64) }] };
const stage = { state: "staged", staged_ref: "original-stage", revision: 1, bundle_digest: "c".repeat(64), error_code: "audio_documentation_source_incomplete", coverage: { missing: ["exact_codec"] } };
function json(value: unknown, status = 200) { return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } })); }
it("uses the owner-private name and current profile/revision without exposing a credential", async () => {
  const fetch = vi.fn().mockImplementationOnce(() => json(settings)).mockImplementationOnce(() => json(settings));
  vi.stubGlobal("fetch", fetch); render(<AudioDocumentationControls />);
  fireEvent.change(screen.getByLabelText("Audio metadata Vault key name"), { target: { value: "owned_management" } });
  fireEvent.click(screen.getByText("Select metadata credential"));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ expected_policy_revision: 2, audio_metadata_access: { action: "select_existing_management_credential", vault_key_name: "owned_management", expected_audio_profile_hash: "a".repeat(64), operation_scope: "selected_audio_profile_metadata" } });
  expect(fetch.mock.calls[1][1].credentials).toBe("include");
  await waitFor(() => expect(screen.getByLabelText("Audio metadata Vault key name")).toHaveValue(""));
});
it("reloads original staging and leaves incomplete acceptance blocked", async () => {
  const fetch = vi.fn().mockImplementationOnce(() => json({ staged: [stage] })).mockImplementationOnce(() => json({ detail: { code: "audio_documentation_source_incomplete" } }, 409));
  vi.stubGlobal("fetch", fetch); render(<AudioDocumentationControls />);
  fireEvent.click(screen.getByText("Reload original staging"));
  await screen.findByText("staged: audio_documentation_source_incomplete");
  fireEvent.click(screen.getByText("Review and accept exact sources"));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ action: "accept_staged_documentation", staged_ref: "original-stage", expected_staged_revision: 1, expected_bundle_digest: "c".repeat(64) });
  await screen.findByText("audio_documentation_source_incomplete");
  expect(screen.queryByText("Ready")).not.toBeInTheDocument();
});
it("explains that ambiguous acquisition remains charged after rejection denial", async () => {
  const failed = { ...stage, revision: 0, error_code: "audio_documentation_acquisition_incomplete" };
  const fetch = vi.fn().mockImplementationOnce(() => json({ staged: [failed] })).mockImplementationOnce(() => json({ detail: { code: "audio_documentation_cleanup_unknown" } }, 409));
  vi.stubGlobal("fetch", fetch); render(<AudioDocumentationControls />);
  fireEvent.click(screen.getByText("Reload original staging"));
  await screen.findByText("staged: audio_documentation_acquisition_incomplete");
  fireEvent.click(screen.getByText("Reject and remove staged sources"));
  await screen.findByText("Acquisition closure is unproven. Private staging remains charged; it cannot be rejected or retried to renew its allowance.");
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toMatchObject({ expected_staged_revision: 0, staged_ref: "original-stage" });
  expect(screen.queryByText("Ready")).not.toBeInTheDocument();
});
