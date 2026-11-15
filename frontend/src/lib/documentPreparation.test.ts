import { describe, expect, it } from "vitest";
import {
  MAX_PRIVATE_PREPARATION_BYTES,
  citationLeaves,
  parseDocumentPreparationView,
  serializeDocumentPreparation,
} from "./documentPreparation";

const artifact_ref = "document-source:11111111-1111-4111-8111-111111111111";

describe("document preparation contract", () => {
  it("does not expose a table container as a selectable citation leaf", () => {
    expect(citationLeaves({ sections: [
      { source_ref: "pdf#page=1", text: "paragraph", table_cells: [] },
      { source_ref: "xlsx#sheet=Data&row=1", text: "container", table_cells: [
        { source_ref: "xlsx#sheet=Data&cell=A1", text: "one", formula: null, cached_value: null },
        { source_ref: "xlsx#sheet=Data&cell=B1", text: "two", formula: "=1+1", cached_value: "2" },
      ] },
    ] })).toEqual([
      { source_ref: "pdf#page=1", text: "paragraph", formula: null, cached_value: null },
      { source_ref: "xlsx#sheet=Data&cell=A1", text: "one", formula: null, cached_value: null },
      { source_ref: "xlsx#sheet=Data&cell=B1", text: "two", formula: "=1+1", cached_value: "2" },
    ]);
  });

  it("serializes only exact refs after separate explicit local-use acknowledgement", () => {
    const body = serializeDocumentPreparation({ artifact_ref, expected_source_revision: 4, citation_refs: ["pdf#page=1"], acknowledge_local_use: true, idempotency_key: "prep-1" });
    expect(JSON.parse(body)).toEqual({ artifact_ref, expected_source_revision: 4, citation_refs: ["pdf#page=1"], acknowledge_local_use: true, idempotency_key: "prep-1" });
    expect(() => serializeDocumentPreparation({ artifact_ref, expected_source_revision: 4, citation_refs: ["pdf#page=1", "pdf#page=1"], acknowledge_local_use: true, idempotency_key: "prep-1" })).toThrow(/duplicate/);
    expect(() => serializeDocumentPreparation({ artifact_ref, expected_source_revision: 4, citation_refs: ["pdf#page=1"], acknowledge_local_use: false, idempotency_key: "prep-1" })).toThrow(/acknowledgement/);
    expect(() => serializeDocumentPreparation({ artifact_ref, expected_source_revision: 4, citation_refs: ["pdf#page=1"], acknowledge_local_use: true, idempotency_key: "bad key" })).toThrow(/idempotency/);
  });

  it("rejects output that changes selected refs or exceeds the private view byte cap", () => {
    const base = { task_id: "task-1", status: "succeeded", no_learning: true as const, provider_contacts: 0 as const };
    expect(() => parseDocumentPreparationView({ ...base, sections: [{ source_ref: "pdf#page=2", text: "wrong", formula: null, cached_value: null }] }, ["pdf#page=1"])).toThrow(/exact selected/);
    expect(() => parseDocumentPreparationView({ ...base, sections: [{ source_ref: "pdf#page=1", text: "x".repeat(MAX_PRIVATE_PREPARATION_BYTES), formula: null, cached_value: null }] }, ["pdf#page=1"])).toThrow(/byte/);
  });
});
