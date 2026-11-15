import { describe, expect, it } from "vitest";
import { parameterValue, readProcedureParameters, type ProcedureParameter } from "./reusableProcedures";

describe("fresh procedure parameter validation", () => {
  const parameter = (schema: Record<string, unknown>): ProcedureParameter => ({ name: "limit", step_id: "search", input_pointer: "/max_results", schema, producer_id: "test.producer.v1", producer_contract_digest: "a".repeat(64) });
  it.each(["1.5", "1e1", "true", "", "9007199254740993", "{\"$dependency\":\"secret\"}"])("rejects non-strict scalar integer %s", raw => {
    expect(() => parameterValue(parameter({ type: "integer", minimum: 1, maximum: 10 }), raw)).toThrow();
  });
  it("enforces declared scalar bounds without coercing booleans or null", () => {
    expect(() => parameterValue(parameter({ type: "integer", maximum: 10 }), "11")).toThrow();
    expect(() => parameterValue(parameter({ type: "boolean" }), "0")).toThrow();
    expect(() => parameterValue(parameter({ type: "null" }), "")).toThrow();
    expect(() => parameterValue(parameter({ type: "string", maxLength: 3 }), "long")).toThrow();
    expect(parameterValue(parameter({ type: "boolean" }), "false")).toBe(false);
    expect(parameterValue(parameter({ type: "null" }), "null")).toBeNull();
  });
  it("rejects duplicate names, arrays and fabricated producer digests", () => {
    const good = parameter({ type: "integer" });
    expect(() => readProcedureParameters([good, good])).toThrow();
    expect(() => readProcedureParameters([{ ...good, schema: { type: "array" } }])).toThrow();
    expect(() => readProcedureParameters([{ ...good, producer_contract_digest: "missing" }])).toThrow();
    expect(() => readProcedureParameters([{ ...good, default: 1 }])).toThrow();
    expect(() => readProcedureParameters([{ ...good, schema: { type: "integer", default: 1 } }])).toThrow();
  });
});
