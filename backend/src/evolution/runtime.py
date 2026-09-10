"""Bounded, review-gated harness improvement runtime.

This module deliberately owns proposal evidence and canary bookkeeping only.
The existing evolution engine owns declarative asset parsing and #755 owns
activation of immutable capability-pack versions.  No model, provider, tool,
policy, or executable code is loaded from this state file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Any, Iterable, Literal, Mapping
import uuid


ProposalState = Literal[
    "proposed",
    "screening",
    "awaiting_review",
    "canary",
    "paused",
    "rejected",
    "rolled_back",
]
EvidenceStatus = Literal[
    "pass",
    "task_fail",
    "policy_violation",
    "infra_unavailable",
    "unknown",
    "missing",
]

SCHEMA_VERSION = 1
MAX_VARIANTS = 3
MAX_TASKS = 24
MAX_ATTEMPTS = 2
MAX_CANARY_JOBS = 5
MAX_CANARY_SECONDS = 24 * 60 * 60
MAX_WALL_CLOCK_SECONDS = 12 * 60 * 60
MAX_TOKENS = 1_000_000
TERMINAL_STATES = frozenset({"rejected", "rolled_back"})
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"screening", "rejected"}),
    "screening": frozenset({"awaiting_review", "rejected", "paused"}),
    "awaiting_review": frozenset({"canary", "rejected", "paused"}),
    "canary": frozenset({"rolled_back", "paused"}),
    "paused": frozenset({"screening", "rejected", "rolled_back"}),
    "rejected": frozenset(),
    "rolled_back": frozenset(),
}

_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$"
_IMMUTABLE_FIELDS = (
    "owner_id",
    "goal_id",
    "goal_revision",
    "baseline_pack_id",
    "baseline_pack_version",
    "baseline_content_hash",
    "candidate_hash",
    "allowed_target_paths",
    "source_failure_ids",
    "authority_digest",
    "effective_model_binding",
    "corpus_manifest_hash",
    "evaluator_hash",
    "development_split_hash",
    "hidden_split_hash",
    "egress_manifest_hash",
)


class EvolutionRuntimeError(ValueError):
    """A bounded, operator-actionable runtime contract failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _content_digest(content: str) -> str:
    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


def _bounded_id(value: Any, field_name: str) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 96 or not __import__("re").fullmatch(_ID_RE, candidate):
        raise EvolutionRuntimeError(f"{field_name} must be a bounded identifier")
    return candidate


def _bounded_hash(value: Any, field_name: str) -> str:
    candidate = str(value or "").strip().lower()
    if len(candidate) != 64 or any(char not in "0123456789abcdef" for char in candidate):
        raise EvolutionRuntimeError(f"{field_name} must be a SHA-256 digest")
    return candidate


def _bounded_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    forbidden = ("authority", "policy", "auth", "vault", "secret", "broker", "evaluator")
    for raw in paths:
        value = str(raw or "").strip().replace("\\", "/")
        if not value or value.startswith("/") or ".." in value.split("/") or "//" in value:
            raise EvolutionRuntimeError("allowed_target_paths must be relative managed asset paths")
        if any(token in value.casefold() for token in forbidden):
            raise EvolutionRuntimeError("harness candidates cannot target authority or secret surfaces")
        if value not in normalized:
            normalized.append(value)
    if not normalized or len(normalized) > 8:
        raise EvolutionRuntimeError("allowed_target_paths must contain one to eight paths")
    return tuple(sorted(normalized))


def _safe_status(value: Any) -> EvidenceStatus:
    status = str(value or "").strip()
    if status not in {"pass", "task_fail", "policy_violation", "infra_unavailable", "unknown", "missing"}:
        raise EvolutionRuntimeError("unsupported evaluation evidence status")
    return status  # type: ignore[return-value]


def _safe_number(value: Any, field_name: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvolutionRuntimeError(f"{field_name} must be numeric") from exc
    if number != number or number in (float("inf"), float("-inf")) or number < minimum:
        raise EvolutionRuntimeError(f"{field_name} is outside its bound")
    return number


@dataclass(frozen=True)
class EvaluationRecord:
    task_id: str
    task_family: str
    status: EvidenceStatus
    baseline_success: bool | None = None
    candidate_success: bool | None = None
    attempts: int = 1
    latency_ms: float = 0.0
    tokens: int = 0
    policy_violation: bool = False
    verifier_digest: str = ""

    def normalized(self) -> "EvaluationRecord":
        task_id = _bounded_id(self.task_id, "task_id")
        family = _bounded_id(self.task_family, "task_family")
        status = _safe_status(self.status)
        attempts = int(self.attempts)
        if attempts < 1 or attempts > MAX_ATTEMPTS:
            raise EvolutionRuntimeError("evaluation attempts exceed the bounded retry limit")
        tokens = int(self.tokens)
        if tokens < 0 or tokens > MAX_TOKENS:
            raise EvolutionRuntimeError("evaluation token count is outside the campaign bound")
        verifier_digest = self.verifier_digest
        if verifier_digest:
            verifier_digest = _bounded_hash(verifier_digest, "verifier_digest")
        policy_violation = bool(self.policy_violation or status == "policy_violation")
        if policy_violation and status != "policy_violation":
            status = "policy_violation"
        return EvaluationRecord(
            task_id=task_id,
            task_family=family,
            status=status,
            baseline_success=self.baseline_success,
            candidate_success=self.candidate_success,
            attempts=attempts,
            latency_ms=_safe_number(self.latency_ms, "latency_ms"),
            tokens=tokens,
            policy_violation=policy_violation,
            verifier_digest=verifier_digest,
        )


@dataclass
class EvolutionProposal:
    proposal_id: str
    state: ProposalState
    owner_id: str
    goal_id: str
    goal_revision: int
    baseline_pack_id: str
    baseline_pack_version: str
    baseline_content_hash: str
    candidate_hash: str
    allowed_target_paths: tuple[str, ...]
    source_failure_ids: tuple[str, ...]
    authority_digest: str
    effective_model_binding: str
    corpus_manifest_hash: str
    evaluator_hash: str
    development_split_hash: str
    hidden_split_hash: str
    egress_manifest_hash: str
    variant_count: int = 1
    budget_microusd: int = 0
    deadline_seconds: int = MAX_WALL_CLOCK_SECONDS
    token_budget: int = MAX_TOKENS
    evaluation: tuple[EvaluationRecord, ...] = ()
    job_ids: tuple[str, ...] = ()
    measured_result: dict[str, Any] = field(default_factory=lambda: {"status": "not_run"})
    review_status: str = "pending"
    approval_id: str | None = None
    approval_digest: str | None = None
    approval_expires_at: float | None = None
    active_version_before: str = ""
    active_version_after: str = ""
    rollback_version: str = ""
    result: str = "no_learning_no_promotion"
    recovery_action: str = "review_or_reject"
    version: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["allowed_target_paths"] = list(self.allowed_target_paths)
        payload["source_failure_ids"] = list(self.source_failure_ids)
        payload["evaluation"] = [asdict(item) for item in self.evaluation]
        payload["job_ids"] = list(self.job_ids)
        return payload


def _proposal_from_dict(payload: Mapping[str, Any]) -> EvolutionProposal:
    raw_evaluation = payload.get("evaluation") or ()
    evaluation = tuple(EvaluationRecord(**dict(item)).normalized() for item in raw_evaluation if isinstance(item, Mapping))
    state = str(payload.get("state") or "proposed")
    if state not in ALLOWED_TRANSITIONS:
        raise EvolutionRuntimeError("unsupported evolution proposal state")
    return EvolutionProposal(
        proposal_id=_bounded_id(payload.get("proposal_id"), "proposal_id"),
        state=state,  # type: ignore[arg-type]
        owner_id=_bounded_id(payload.get("owner_id"), "owner_id"),
        goal_id=_bounded_id(payload.get("goal_id"), "goal_id"),
        goal_revision=int(payload.get("goal_revision")),
        baseline_pack_id=_bounded_id(payload.get("baseline_pack_id"), "baseline_pack_id"),
        baseline_pack_version=_bounded_id(payload.get("baseline_pack_version"), "baseline_pack_version"),
        baseline_content_hash=_bounded_hash(payload.get("baseline_content_hash"), "baseline_content_hash"),
        candidate_hash=_bounded_hash(payload.get("candidate_hash"), "candidate_hash"),
        allowed_target_paths=_bounded_paths(payload.get("allowed_target_paths") or ()),
        source_failure_ids=tuple(_bounded_id(item, "source_failure_id") for item in (payload.get("source_failure_ids") or ())),
        authority_digest=_bounded_hash(payload.get("authority_digest"), "authority_digest"),
        effective_model_binding=_bounded_hash(payload.get("effective_model_binding"), "effective_model_binding"),
        corpus_manifest_hash=_bounded_hash(payload.get("corpus_manifest_hash"), "corpus_manifest_hash"),
        evaluator_hash=_bounded_hash(payload.get("evaluator_hash"), "evaluator_hash"),
        development_split_hash=_bounded_hash(payload.get("development_split_hash"), "development_split_hash"),
        hidden_split_hash=_bounded_hash(payload.get("hidden_split_hash"), "hidden_split_hash"),
        egress_manifest_hash=_bounded_hash(payload.get("egress_manifest_hash"), "egress_manifest_hash"),
        variant_count=int(payload.get("variant_count", 1)),
        budget_microusd=int(payload.get("budget_microusd", 0)),
        deadline_seconds=int(payload.get("deadline_seconds", MAX_WALL_CLOCK_SECONDS)),
        token_budget=int(payload.get("token_budget", MAX_TOKENS)),
        evaluation=evaluation,
        job_ids=tuple(_bounded_id(item, "job_id") for item in (payload.get("job_ids") or ())),
        measured_result=dict(payload.get("measured_result") or {"status": "not_run"}),
        review_status=str(payload.get("review_status") or "pending"),
        approval_id=str(payload["approval_id"]) if payload.get("approval_id") else None,
        approval_digest=str(payload["approval_digest"]) if payload.get("approval_digest") else None,
        approval_expires_at=(float(payload["approval_expires_at"]) if payload.get("approval_expires_at") is not None else None),
        active_version_before=str(payload.get("active_version_before") or ""),
        active_version_after=str(payload.get("active_version_after") or ""),
        rollback_version=str(payload.get("rollback_version") or ""),
        result=str(payload.get("result") or "no_learning_no_promotion"),
        recovery_action=str(payload.get("recovery_action") or "review_or_reject"),
        version=int(payload.get("version", 0)),
        created_at=str(payload.get("created_at") or _now()),
        updated_at=str(payload.get("updated_at") or _now()),
    )


class EvolutionRuntime:
    """Small file-backed CAS store for experimental harness proposals.

    The lock and atomic replace make restart/replay deterministic for the
    single local operator. It stores metadata and evaluator receipts only;
    candidate content stays in the existing staged evolution artifact store.
    """

    def __init__(self, state_path: str | Path):
        self.state_path = Path(state_path).expanduser()
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self._mutex = RLock()

    @staticmethod
    def default_path(workspace_dir: str | Path) -> Path:
        return Path(workspace_dir).expanduser() / "evolution" / "runtime-state.json"

    @contextmanager
    def _locked(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._mutex, self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": SCHEMA_VERSION, "proposals": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise EvolutionRuntimeError("evolution runtime state is unreadable; recovery required") from exc
        if not isinstance(payload, dict) or int(payload.get("schema_version", 0)) != SCHEMA_VERSION:
            raise EvolutionRuntimeError("unsupported evolution runtime state schema")
        proposals = payload.get("proposals")
        if not isinstance(proposals, dict):
            raise EvolutionRuntimeError("evolution runtime proposals are malformed")
        return payload

    def _write(self, payload: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.state_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _save_proposal(self, payload: dict[str, Any], proposal: EvolutionProposal) -> EvolutionProposal:
        proposal.version += 1
        proposal.updated_at = _now()
        payload.setdefault("proposals", {})[proposal.proposal_id] = proposal.to_dict()
        self._write(payload)
        return proposal

    def create_proposal(
        self,
        *,
        owner_id: str,
        goal_id: str,
        goal_revision: int,
        baseline_pack_id: str,
        baseline_pack_version: str,
        baseline_content_hash: str,
        candidate_hash: str,
        allowed_target_paths: Iterable[str],
        source_failure_ids: Iterable[str],
        authority_digest: str,
        effective_model_binding: str,
        corpus_manifest_hash: str,
        evaluator_hash: str,
        development_split_hash: str,
        hidden_split_hash: str,
        egress_manifest_hash: str,
        variant_count: int = 1,
        budget_microusd: int = 0,
        deadline_seconds: int = MAX_WALL_CLOCK_SECONDS,
        token_budget: int = MAX_TOKENS,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        with self._locked():
            payload = self._read()
            proposal = EvolutionProposal(
                proposal_id=_bounded_id(proposal_id or f"evo-{uuid.uuid4().hex[:24]}", "proposal_id"),
                state="proposed",
                owner_id=_bounded_id(owner_id, "owner_id"),
                goal_id=_bounded_id(goal_id, "goal_id"),
                goal_revision=int(goal_revision),
                baseline_pack_id=_bounded_id(baseline_pack_id, "baseline_pack_id"),
                baseline_pack_version=_bounded_id(baseline_pack_version, "baseline_pack_version"),
                baseline_content_hash=_bounded_hash(baseline_content_hash, "baseline_content_hash"),
                candidate_hash=_bounded_hash(candidate_hash, "candidate_hash"),
                allowed_target_paths=_bounded_paths(allowed_target_paths),
                source_failure_ids=tuple(_bounded_id(item, "source_failure_id") for item in source_failure_ids),
                authority_digest=_bounded_hash(authority_digest, "authority_digest"),
                effective_model_binding=_bounded_hash(effective_model_binding, "effective_model_binding"),
                corpus_manifest_hash=_bounded_hash(corpus_manifest_hash, "corpus_manifest_hash"),
                evaluator_hash=_bounded_hash(evaluator_hash, "evaluator_hash"),
                development_split_hash=_bounded_hash(development_split_hash, "development_split_hash"),
                hidden_split_hash=_bounded_hash(hidden_split_hash, "hidden_split_hash"),
                egress_manifest_hash=_bounded_hash(egress_manifest_hash, "egress_manifest_hash"),
                variant_count=int(variant_count),
                budget_microusd=int(budget_microusd),
                deadline_seconds=int(deadline_seconds),
                token_budget=int(token_budget),
            )
            if proposal.proposal_id in payload["proposals"]:
                raise EvolutionRuntimeError("proposal_id already exists")
            if not 1 <= proposal.variant_count <= MAX_VARIANTS:
                raise EvolutionRuntimeError("at most three candidate variants may be screened")
            if proposal.budget_microusd < 0 or proposal.deadline_seconds < 1 or proposal.deadline_seconds > MAX_WALL_CLOCK_SECONDS:
                raise EvolutionRuntimeError("campaign budget or deadline is outside the bound")
            if proposal.token_budget < 1 or proposal.token_budget > MAX_TOKENS:
                raise EvolutionRuntimeError("campaign token budget is outside the bound")
            self._save_proposal(payload, proposal)
            return proposal.to_dict()

    def get(self, proposal_id: str) -> dict[str, Any]:
        proposal_id = _bounded_id(proposal_id, "proposal_id")
        with self._locked():
            payload = self._read()
            raw = payload["proposals"].get(proposal_id)
            if not isinstance(raw, Mapping):
                raise EvolutionRuntimeError("evolution proposal not found")
            return dict(raw)

    def list(self) -> list[dict[str, Any]]:
        with self._locked():
            payload = self._read()
            return [dict(item) for item in payload["proposals"].values() if isinstance(item, Mapping)]

    def _transition(
        self,
        proposal_id: str,
        target: ProposalState,
        *,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any], EvolutionProposal, dict[str, Any]]:
        payload = self._read()
        raw = payload["proposals"].get(proposal_id)
        if not isinstance(raw, Mapping):
            raise EvolutionRuntimeError("evolution proposal not found")
        proposal = _proposal_from_dict(raw)
        if expected_version is not None and proposal.version != int(expected_version):
            raise EvolutionRuntimeError("evolution proposal version is stale")
        if target not in ALLOWED_TRANSITIONS.get(proposal.state, frozenset()):
            raise EvolutionRuntimeError(f"illegal evolution transition {proposal.state} -> {target}")
        proposal.state = target
        return payload, proposal, raw

    def screen(
        self,
        proposal_id: str,
        *,
        structural_pass: bool,
        safety_pass: bool,
        candidate_hash: str,
        expected_version: int | None = None,
        authority_digest: str | None = None,
        goal_revision: int | None = None,
        effective_model_binding: str | None = None,
    ) -> dict[str, Any]:
        with self._locked():
            payload, proposal, previous = self._transition(proposal_id, "screening", expected_version=expected_version)
            if str(previous.get("state")) == "paused":
                if authority_digest is None or _bounded_hash(authority_digest, "authority_digest") != proposal.authority_digest:
                    raise EvolutionRuntimeError("paused proposal authority binding is stale")
                if goal_revision is None or int(goal_revision) != proposal.goal_revision:
                    raise EvolutionRuntimeError("paused proposal goal revision is stale")
                if effective_model_binding is None or _bounded_hash(effective_model_binding, "effective_model_binding") != proposal.effective_model_binding:
                    raise EvolutionRuntimeError("paused proposal model binding is stale")
            if _bounded_hash(candidate_hash, "candidate_hash") != proposal.candidate_hash:
                raise EvolutionRuntimeError("candidate hash changed before screening")
            if not structural_pass or not safety_pass:
                proposal.state = "rejected"
                proposal.review_status = "rejected"
                proposal.measured_result = {"status": "not_run", "reason": "screening_failed"}
                proposal.result = "no_learning_no_promotion"
                proposal.recovery_action = "create_new_proposal_after_review"
            else:
                proposal.state = "awaiting_review"
                proposal.review_status = "awaiting_review"
                proposal.measured_result = {"status": "not_run", "reason": "hidden_evaluation_not_run"}
                proposal.recovery_action = "operator_review_exact_hashes"
            self._save_proposal(payload, proposal)
            return proposal.to_dict()

    def evaluate(
        self,
        proposal_id: str,
        *,
        records: Iterable[EvaluationRecord],
        candidate_hash: str,
        evaluator_hash: str,
        corpus_manifest_hash: str,
        hidden_split_hash: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self._locked():
            payload = self._read()
            raw = payload["proposals"].get(proposal_id)
            if not isinstance(raw, Mapping):
                raise EvolutionRuntimeError("evolution proposal not found")
            proposal = _proposal_from_dict(raw)
            if expected_version is not None and proposal.version != int(expected_version):
                raise EvolutionRuntimeError("evolution proposal version is stale")
            if proposal.state not in {"screening", "awaiting_review"}:
                raise EvolutionRuntimeError("evaluation requires a screening or review proposal")
            if _bounded_hash(candidate_hash, "candidate_hash") != proposal.candidate_hash:
                raise EvolutionRuntimeError("candidate hash changed before evaluation")
            if _bounded_hash(evaluator_hash, "evaluator_hash") != proposal.evaluator_hash:
                raise EvolutionRuntimeError("evaluator hash changed before evaluation")
            if _bounded_hash(corpus_manifest_hash, "corpus_manifest_hash") != proposal.corpus_manifest_hash:
                raise EvolutionRuntimeError("corpus manifest changed before evaluation")
            if _bounded_hash(hidden_split_hash, "hidden_split_hash") != proposal.hidden_split_hash:
                raise EvolutionRuntimeError("hidden evaluation split changed before evaluation")
            normalized = tuple(record.normalized() for record in records)
            if not normalized or len(normalized) > MAX_TASKS:
                raise EvolutionRuntimeError("evaluation corpus is outside the bounded task limit")
            total_tokens = sum(item.tokens for item in normalized)
            if total_tokens > proposal.token_budget:
                proposal.measured_result = {"status": "unknown", "reason": "token_budget_exhausted"}
                proposal.state = "paused"
                proposal.review_status = "paused"
                proposal.recovery_action = "reduce_scope_or_request_new_budget"
            else:
                incomplete = sum(item.status in {"infra_unavailable", "unknown", "missing"} for item in normalized)
                failures = sum(item.status in {"task_fail", "policy_violation"} for item in normalized)
                baseline_values = [1.0 if item.baseline_success else 0.0 for item in normalized if item.baseline_success is not None]
                candidate_values = [1.0 if item.candidate_success else 0.0 for item in normalized if item.candidate_success is not None]
                baseline_score = sum(baseline_values) / len(baseline_values) if baseline_values else 0.0
                candidate_score = sum(candidate_values) / len(candidate_values) if candidate_values else 0.0
                delta = round(candidate_score - baseline_score, 6)
                measured_status = "incomplete" if incomplete else "measured"
                promotion = "no_promotion"
                if incomplete or failures or delta <= 0:
                    result = "no_learning_no_promotion"
                else:
                    result = "experimental_signal_no_promotion"
                proposal.measured_result = {
                    "status": measured_status,
                    "baseline_success_rate": round(baseline_score, 6),
                    "candidate_success_rate": round(candidate_score, 6),
                    "paired_delta": delta,
                    "task_count": len(normalized),
                    "incomplete_count": incomplete,
                    "failure_count": failures,
                    "promotion": promotion,
                    "result": result,
                    "verifier": "external_task_artifacts_only",
                }
                proposal.result = result
                proposal.evaluation = normalized
                proposal.state = "awaiting_review"
                proposal.review_status = "awaiting_review"
                proposal.recovery_action = "operator_review_or_reject"
            self._save_proposal(payload, proposal)
            return proposal.to_dict()

    def approve_canary(
        self,
        proposal_id: str,
        *,
        approval_id: str,
        approval_digest: str,
        owner_id: str,
        goal_id: str,
        goal_revision: int,
        baseline_content_hash: str,
        candidate_hash: str,
        authority_digest: str,
        evaluator_hash: str,
        expires_at: float,
        active_version_before: str,
        active_version_after: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self._locked():
            payload, proposal, _ = self._transition(proposal_id, "canary", expected_version=expected_version)
            if proposal.owner_id != _bounded_id(owner_id, "owner_id") or proposal.goal_id != _bounded_id(goal_id, "goal_id") or proposal.goal_revision != int(goal_revision):
                raise EvolutionRuntimeError("canary approval owner or goal binding is stale")
            bindings = {
                "approval_id": _bounded_id(approval_id, "approval_id"),
                "owner_id": proposal.owner_id,
                "goal_id": proposal.goal_id,
                "goal_revision": proposal.goal_revision,
                "baseline_content_hash": _bounded_hash(baseline_content_hash, "baseline_content_hash"),
                "candidate_hash": _bounded_hash(candidate_hash, "candidate_hash"),
                "authority_digest": _bounded_hash(authority_digest, "authority_digest"),
                "evaluator_hash": _bounded_hash(evaluator_hash, "evaluator_hash"),
                "expires_at": float(expires_at),
                "active_version_before": _bounded_id(active_version_before, "active_version_before"),
                "active_version_after": _bounded_id(active_version_after, "active_version_after"),
            }
            if bindings["baseline_content_hash"] != proposal.baseline_content_hash or bindings["candidate_hash"] != proposal.candidate_hash or bindings["authority_digest"] != proposal.authority_digest or bindings["evaluator_hash"] != proposal.evaluator_hash:
                raise EvolutionRuntimeError("canary approval hash binding is stale")
            if bindings["expires_at"] <= datetime.now(timezone.utc).timestamp():
                raise EvolutionRuntimeError("canary approval has expired")
            if bindings["expires_at"] > datetime.now(timezone.utc).timestamp() + MAX_CANARY_SECONDS:
                raise EvolutionRuntimeError("canary approval exceeds the 24-hour bound")
            if proposal.measured_result.get("status") != "measured" or proposal.measured_result.get("failure_count", 1):
                raise EvolutionRuntimeError("canary requires complete, policy-clean evaluation evidence")
            expected_digest = _digest(bindings)
            if _bounded_hash(approval_digest, "approval_digest") != expected_digest:
                raise EvolutionRuntimeError("canary approval digest does not bind exact inputs")
            proposal.approval_id = bindings["approval_id"]
            proposal.approval_digest = expected_digest
            proposal.approval_expires_at = bindings["expires_at"]
            proposal.active_version_before = bindings["active_version_before"]
            proposal.active_version_after = bindings["active_version_after"]
            proposal.rollback_version = bindings["active_version_before"]
            proposal.review_status = "canary_approved"
            proposal.recovery_action = "run_one_goal_canary_then_rollback"
            self._save_proposal(payload, proposal)
            return proposal.to_dict()

    def record_canary(
        self,
        proposal_id: str,
        *,
        approval_id: str,
        job_ids: Iterable[str],
        outcome: Literal["success", "failed", "unknown", "cancelled"],
        baseline_still_permitted: bool,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self._locked():
            payload, proposal, _ = self._transition(proposal_id, "rolled_back", expected_version=expected_version)
            if proposal.approval_id != _bounded_id(approval_id, "approval_id"):
                raise EvolutionRuntimeError("canary approval does not match the proposal")
            jobs = tuple(_bounded_id(item, "job_id") for item in job_ids)
            if not jobs or len(jobs) > MAX_CANARY_JOBS:
                raise EvolutionRuntimeError("canary job count exceeds the one-goal bound")
            if not baseline_still_permitted:
                proposal.state = "paused"
                proposal.result = "no_learning_no_promotion"
                proposal.recovery_action = "baseline_revoked_operator_recovery_required"
                proposal.review_status = "blocked"
            else:
                proposal.state = "rolled_back"
                proposal.result = "no_learning_no_promotion"
                proposal.review_status = "rolled_back"
                proposal.recovery_action = "baseline_restored_and_candidate_disabled"
            proposal.measured_result = {
                **proposal.measured_result,
                "canary": {
                    "outcome": outcome,
                    "job_count": len(jobs),
                    "job_ids_digest": _digest(jobs),
                    "rollback": "restored" if baseline_still_permitted else "blocked",
                },
            }
            proposal.job_ids = jobs
            if proposal.approval_expires_at is not None and datetime.now(timezone.utc).timestamp() > proposal.approval_expires_at:
                proposal.measured_result["canary"]["outcome"] = "unknown"
                proposal.recovery_action = "canary_expired_baseline_restored"
            self._save_proposal(payload, proposal)
            return proposal.to_dict()

    def pause(self, proposal_id: str, *, reason: str, expected_version: int | None = None) -> dict[str, Any]:
        with self._locked():
            payload, proposal, _ = self._transition(proposal_id, "paused", expected_version=expected_version)
            proposal.review_status = "paused"
            proposal.recovery_action = str(reason or "operator_review")[:160]
            self._save_proposal(payload, proposal)
            return proposal.to_dict()

    def reject(self, proposal_id: str, *, reason: str, expected_version: int | None = None) -> dict[str, Any]:
        with self._locked():
            payload, proposal, _ = self._transition(proposal_id, "rejected", expected_version=expected_version)
            proposal.review_status = "rejected"
            proposal.result = "no_learning_no_promotion"
            proposal.recovery_action = str(reason or "candidate_rejected")[:160]
            self._save_proposal(payload, proposal)
            return proposal.to_dict()

    def rollback(self, proposal_id: str, *, reason: str = "operator_rollback", expected_version: int | None = None) -> dict[str, Any]:
        with self._locked():
            payload, proposal, _ = self._transition(proposal_id, "rolled_back", expected_version=expected_version)
            proposal.review_status = "rolled_back"
            proposal.result = "no_learning_no_promotion"
            proposal.recovery_action = str(reason or "operator_rollback")[:160]
            proposal.rollback_version = proposal.active_version_before
            self._save_proposal(payload, proposal)
            return proposal.to_dict()


def candidate_hash(content: str) -> str:
    """Return the canonical candidate digest used by API callers and tests."""
    return _content_digest(content)


def binding_digest(bindings: Mapping[str, Any]) -> str:
    """Return an approval digest without exposing the binding contents."""
    return _digest(dict(bindings))
