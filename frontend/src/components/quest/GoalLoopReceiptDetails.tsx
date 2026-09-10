import type { GoalLoopReceipt } from "../../types";

type ReceiptFieldKind = "scalar" | "list";

interface ReceiptField {
  key: keyof GoalLoopReceipt;
  label: string;
  kind: ReceiptFieldKind;
}

const RECEIPT_FIELDS: ReceiptField[] = [
  { key: "audit_event_id", label: "Audit event", kind: "scalar" },
  { key: "receipt_version", label: "Receipt version", kind: "scalar" },
  { key: "receipt_type", label: "Receipt type", kind: "scalar" },
  { key: "event_type", label: "Event type", kind: "scalar" },
  { key: "proposal_only", label: "Proposal only", kind: "scalar" },
  { key: "outcome_id", label: "Outcome", kind: "scalar" },
  { key: "candidate_id", label: "Candidate", kind: "scalar" },
  { key: "goal_id", label: "Goal", kind: "scalar" },
  { key: "goal_revision", label: "Goal revision", kind: "scalar" },
  { key: "criterion_id", label: "Criterion", kind: "scalar" },
  { key: "action", label: "Action", kind: "scalar" },
  { key: "capability_id", label: "Capability", kind: "scalar" },
  { key: "capability_version", label: "Capability version", kind: "scalar" },
  { key: "execution_status", label: "Execution", kind: "scalar" },
  { key: "verification", label: "Verification", kind: "scalar" },
  { key: "usefulness", label: "Usefulness", kind: "scalar" },
  { key: "learning", label: "Learning", kind: "scalar" },
  { key: "learning_record_id", label: "Learning record", kind: "scalar" },
  { key: "expected_outcome", label: "Expected outcome", kind: "scalar" },
  { key: "input_digest", label: "Input digest", kind: "scalar" },
  { key: "decision_input_digest", label: "Decision input digest", kind: "scalar" },
  { key: "dedupe_key", label: "Dedupe key", kind: "scalar" },
  { key: "strategy_delta_id", label: "Strategy delta", kind: "scalar" },
  { key: "strategy_delta_provenance", label: "Strategy provenance", kind: "scalar" },
  { key: "artifact_ref", label: "Artifact", kind: "scalar" },
  { key: "evidence_refs", label: "Evidence refs", kind: "list" },
  { key: "input_keys", label: "Input keys", kind: "list" },
  { key: "expires_at", label: "Expires", kind: "scalar" },
  { key: "created_at", label: "Created", kind: "scalar" },
  { key: "reason", label: "Reason", kind: "scalar" },
  { key: "content_redacted", label: "Content redacted", kind: "scalar" },
];

function scalarValue(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  return null;
}

function listValue(value: unknown): string {
  if (!Array.isArray(value)) return "unknown";
  if (!value.every((entry) => typeof entry === "string")) return "unknown";
  const entries = value.filter((entry): entry is string => Boolean(entry.trim()));
  return entries.length > 0 ? entries.join(", ") : "none recorded";
}

function fieldValue(receipt: GoalLoopReceipt, field: ReceiptField): string {
  const value = receipt[field.key];
  return field.kind === "list" ? listValue(value) : scalarValue(value) ?? "unknown";
}

interface Props {
  receipt: GoalLoopReceipt;
}

/**
 * Show the safe, typed fields returned by the goal-loop endpoint without
 * inventing a client-side outcome or coercing malformed values into text.
 */
export function GoalLoopReceiptDetails({ receipt }: Props) {
  const contentRedacted = receipt.content_redacted === true;

  return (
    <details
      className="mt-2 border-t border-retro-text/10 pt-1"
      data-testid="goal-loop-receipt-details"
    >
      <summary
        className="cursor-pointer text-[9px] text-retro-text/60 hover:text-retro-highlight focus-visible:outline focus-visible:outline-1 focus-visible:outline-retro-highlight"
        onKeyDown={(event) => {
          if (event.key !== "Enter" && event.key !== " ") return;
          event.preventDefault();
          const details = event.currentTarget.parentElement;
          if (details instanceof HTMLDetailsElement) details.open = !details.open;
        }}
      >
        Inspect exact backend receipt fields
      </summary>
      {contentRedacted ? (
        <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-2 gap-y-1 mt-2 text-[9px]">
          {RECEIPT_FIELDS.map((field) => (
            <div key={field.key} className="min-w-0">
              <dt className="text-retro-text/40 uppercase tracking-wider">{field.label}</dt>
              <dd
                className="text-retro-text break-words"
                data-testid={`goal-loop-receipt-${String(field.key).replace(/_/g, "-")}`}
              >
                {fieldValue(receipt, field)}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="mt-2 text-[9px] text-amber-300" data-testid="goal-loop-receipt-withheld" role="note">
          Receipt fields withheld until the backend confirms redacted content.
        </p>
      )}
    </details>
  );
}
