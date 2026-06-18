"""In-memory browser session runtime for structured browsing workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import threading
import uuid
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _excerpt(content: str, *, limit: int = 180) -> str:
    collapsed = " ".join(str(content or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit - 1]}…"


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_artifact_provenance(
    *,
    session_id: str,
    ref: str,
    capture: str,
    url: str,
    provider_name: str,
    provider_kind: str,
    execution_mode: str,
    content: str,
) -> dict[str, object]:
    captured_at = _utc_now()
    payload = {
        "session_id": session_id,
        "ref": ref,
        "capture": capture,
        "captured_at": captured_at,
        "url_digest": _stable_digest(("url", url)),
        "provider_name": provider_name,
        "provider_kind": provider_kind,
        "execution_mode": execution_mode,
        "content_digest": _stable_digest(("content", content)),
    }
    handle = f"seraph://browser-sessions/{session_id}/{ref}/{payload['content_digest'][:20]}"
    safe_receipt = {
        "handle": handle,
        "redaction_layer": "browser_live_session_control_v1",
        "redaction_status": "passed",
        "evidence_body_digest": payload["content_digest"],
        "sanitized_payload_digest": _stable_digest(("sanitized-browser-session", payload)),
        "raw_artifact_body_exposed": False,
        "contains_cookie": False,
        "contains_secret": False,
        "contains_auth_header": False,
        "contains_clipboard_content": False,
        "contains_downloaded_filename": False,
        "contains_account_identifier": False,
        "contains_private_page_content": False,
    }
    return {
        "run_id": session_id,
        "captured_at": captured_at,
        "failure_injection_id": "live_capture",
        "artifact_handle": handle,
        "handle": handle,
        "artifact_body_digest": payload["content_digest"],
        "content_digest": payload["content_digest"],
        "url_digest": payload["url_digest"],
        "redaction_status": "metadata_only",
        "raw_artifact_body_exposed": False,
        "contains_cookie": False,
        "contains_secret": False,
        "contains_auth_header": False,
        "contains_raw_dom": capture == "html",
        "contains_screenshot": capture == "screenshot",
        "contains_clipboard_content": False,
        "contains_downloaded_filename": False,
        "contains_account_identifier": False,
        "contains_private_page_content": False,
        "contains_private_path": False,
        "contains_profile_dir": False,
        "contains_download_path": False,
        "safe_receipt": safe_receipt,
        "tamper_evident_digest": _stable_digest(("browser-session-provenance", payload)),
    }


def _raw_read_artifact_provenance(provenance: dict[str, object]) -> dict[str, object]:
    payload = dict(provenance)
    payload["redaction_status"] = "owner_scoped_raw_ref_read"
    payload["raw_artifact_body_exposed"] = True
    safe_receipt = payload.get("safe_receipt")
    if isinstance(safe_receipt, dict):
        payload["safe_receipt"] = {
            **safe_receipt,
            "redaction_status": "raw_owner_scoped_read",
            "raw_artifact_body_exposed": True,
        }
    return payload


def _boundary_decisions(*, provider_kind: str, execution_mode: str) -> dict[str, dict[str, object]]:
    degraded = execution_mode == "local_fallback"
    remote = provider_kind not in {"local", ""}
    return {
        "profile": {
            "state": "partitioned_ephemeral" if not degraded else "local_fallback_ephemeral",
            "enforced": False,
            "operator_visible": True,
        },
        "cookie": {
            "state": "session_scoped_no_export",
            "enforced": False,
            "operator_visible": True,
        },
        "credential": {
            "state": "scoped_refs_only" if remote else "not_attached",
            "enforced": remote,
            "operator_visible": True,
        },
        "download": {
            "state": "quarantine_required_before_adoption",
            "enforced": False,
            "operator_visible": True,
        },
        "upload": {
            "state": "operator_review_required",
            "enforced": False,
            "operator_visible": True,
        },
        "filesystem": {
            "state": "workspace_boundary_denied_by_default",
            "enforced": False,
            "operator_visible": True,
        },
        "clipboard": {
            "state": "redacted_no_clipboard_payload_storage",
            "enforced": False,
            "operator_visible": True,
        },
        "network": {
            "state": "site_policy_guarded",
            "enforced": True,
            "operator_visible": True,
        },
        "private_data": {
            "state": "metadata_only_redacted_receipts",
            "enforced": True,
            "operator_visible": True,
        },
    }


@dataclass
class BrowserSnapshot:
    ref: str
    capture: str
    content: str
    created_at: str
    summary: str
    artifact_provenance: dict[str, object]

    def as_payload(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "capture": self.capture,
            "created_at": self.created_at,
            "summary": self.summary,
            "artifact_provenance": self.artifact_provenance,
        }


@dataclass
class BrowserSession:
    session_id: str
    owner_session_id: str
    url: str
    provider_name: str
    provider_kind: str
    execution_mode: str
    created_at: str
    updated_at: str
    status: str = "open"
    risk_state: str = "nominal"
    recovery_state: str = "ready"
    partition_id: str = ""
    partition_revision: int = 1
    boundary_decisions: dict[str, dict[str, object]] = field(default_factory=dict)
    provider_degradation: dict[str, object] = field(default_factory=dict)
    control_events: list[dict[str, object]] = field(default_factory=list)
    snapshots: list[BrowserSnapshot] = field(default_factory=list)

    def latest_snapshot(self) -> BrowserSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def as_summary(self) -> dict[str, object]:
        latest = self.latest_snapshot()
        return {
            "session_id": self.session_id,
            "owner_session_id": self.owner_session_id,
            "url": self.url,
            "provider_name": self.provider_name,
            "provider_kind": self.provider_kind,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "risk_state": self.risk_state,
            "recovery_state": self.recovery_state,
            "partition_id": self.partition_id,
            "partition_revision": self.partition_revision,
            "boundary_decisions": self.boundary_decisions,
            "provider_degradation": self.provider_degradation,
            "control_events": list(self.control_events),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "snapshot_count": len(self.snapshots),
            "latest_ref": latest.ref if latest is not None else None,
            "latest_capture": latest.capture if latest is not None else None,
            "latest_summary": latest.summary if latest is not None else "",
            "latest_artifact_provenance": latest.artifact_provenance if latest is not None else None,
        }


class BrowserSessionRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, BrowserSession] = {}
        self._refs: dict[str, tuple[str, int]] = {}

    def reset_for_tests(self) -> None:
        with self._lock:
            self._sessions = {}
            self._refs = {}

    def list_sessions(self, *, owner_session_id: str) -> list[dict[str, object]]:
        with self._lock:
            sessions = [
                session
                for session in self._sessions.values()
                if session.owner_session_id == owner_session_id
            ]
        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return [item.as_summary() for item in sessions]

    def open_session(
        self,
        *,
        owner_session_id: str,
        url: str,
        provider_name: str,
        provider_kind: str,
        execution_mode: str,
        capture: str,
        content: str,
    ) -> dict[str, object]:
        created_at = _utc_now()
        session_id = f"bs-{uuid.uuid4().hex[:10]}"
        ref = f"{session_id}:1"
        snapshot = BrowserSnapshot(
            ref=ref,
            capture=capture,
            content=content,
            created_at=created_at,
            summary=_excerpt(content),
            artifact_provenance=_safe_artifact_provenance(
                session_id=session_id,
                ref=ref,
                capture=capture,
                url=url,
                provider_name=provider_name,
                provider_kind=provider_kind,
                execution_mode=execution_mode,
                content=content,
            ),
        )
        degraded = execution_mode == "local_fallback"
        session = BrowserSession(
            session_id=session_id,
            owner_session_id=owner_session_id,
            url=url,
            provider_name=provider_name,
            provider_kind=provider_kind,
            execution_mode=execution_mode,
            created_at=created_at,
            updated_at=created_at,
            status="degraded" if degraded else "open",
            risk_state="provider_degraded_labeled" if degraded else "nominal",
            recovery_state="operator_acknowledgement_required" if degraded else "ready",
            partition_id=f"bp-{uuid.uuid4().hex[:10]}",
            partition_revision=1,
            boundary_decisions=_boundary_decisions(
                provider_kind=provider_kind,
                execution_mode=execution_mode,
            ),
            provider_degradation={
                "degraded": degraded,
                "fallback_labeled": degraded,
                "fallback_reason": "remote_provider_staged_local_runtime_used" if degraded else "",
                "silent_fallback_allowed": False,
            },
            control_events=[
                {
                    "id": f"browser-control:{session_id}:open",
                    "action": "open",
                    "status": "recorded",
                    "created_at": created_at,
                    "operator_visible": True,
                    "artifact_handle": snapshot.artifact_provenance["handle"],
                }
            ],
            snapshots=[snapshot],
        )
        with self._lock:
            self._sessions[session_id] = session
            self._refs[snapshot.ref] = (session_id, 0)
        return session.as_summary()

    def snapshot_session(
        self,
        *,
        owner_session_id: str,
        session_id: str,
        capture: str,
        content: str,
    ) -> dict[str, object] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            if session.status == "quarantined":
                return {"error": "session_quarantined", "session": session.as_summary()}
            created_at = _utc_now()
            ref = f"{session_id}:{len(session.snapshots) + 1}"
            snapshot = BrowserSnapshot(
                ref=ref,
                capture=capture,
                content=content,
                created_at=created_at,
                summary=_excerpt(content),
                artifact_provenance=_safe_artifact_provenance(
                    session_id=session_id,
                    ref=ref,
                    capture=capture,
                    url=session.url,
                    provider_name=session.provider_name,
                    provider_kind=session.provider_kind,
                    execution_mode=session.execution_mode,
                    content=content,
                ),
            )
            session.snapshots.append(snapshot)
            session.updated_at = created_at
            self._refs[snapshot.ref] = (session_id, len(session.snapshots) - 1)
            session.control_events.append(
                {
                    "id": f"browser-control:{session_id}:snapshot:{len(session.snapshots)}",
                    "action": "snapshot",
                    "status": "recorded",
                    "created_at": created_at,
                    "operator_visible": True,
                    "artifact_handle": snapshot.artifact_provenance["handle"],
                }
            )
            return session.as_summary()

    def get_session(self, session_id: str, *, owner_session_id: str) -> dict[str, object] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            payload = session.as_summary()
            payload["snapshots"] = [snapshot.as_payload() for snapshot in session.snapshots]
            return payload

    def read_ref(self, ref: str, *, owner_session_id: str) -> dict[str, object] | None:
        with self._lock:
            target = self._refs.get(ref)
            if target is None:
                return None
            session_id, index = target
            session = self._sessions.get(session_id)
            if (
                session is None
                or session.owner_session_id != owner_session_id
                or index >= len(session.snapshots)
            ):
                return None
            snapshot = session.snapshots[index]
            return {
                "session_id": session_id,
                "owner_session_id": session.owner_session_id,
                "ref": snapshot.ref,
                "capture": snapshot.capture,
                "content": snapshot.content,
                "summary": snapshot.summary,
                "url": session.url,
                "provider_name": session.provider_name,
                "provider_kind": session.provider_kind,
                "execution_mode": session.execution_mode,
                "created_at": snapshot.created_at,
                "artifact_provenance": _raw_read_artifact_provenance(snapshot.artifact_provenance),
            }

    def validate_replay_session(
        self,
        session_id: str,
        *,
        owner_session_id: str,
        acknowledge_degraded_fallback: bool = False,
    ) -> dict[str, object] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            if session.status == "quarantined":
                return {"error": "session_quarantined", "session": session.as_summary()}
            if session.provider_degradation.get("degraded") is True and not acknowledge_degraded_fallback:
                return {"error": "degraded_fallback_acknowledgement_required", "session": session.as_summary()}
            return {"session": session.as_summary()}

    def close_session(self, session_id: str, *, owner_session_id: str) -> dict[str, object] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            session = self._sessions.pop(session_id)
            for snapshot in session.snapshots:
                self._refs.pop(snapshot.ref, None)
            return session.as_summary()

    def control_session(
        self,
        session_id: str,
        *,
        owner_session_id: str,
        action: str,
        reason: str = "",
        acknowledge_degraded_fallback: bool = False,
    ) -> dict[str, object] | None:
        normalized_action = action.strip().lower()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            if session.status == "quarantined" and normalized_action not in {"recover", "close"}:
                return {"error": "session_quarantined", "session": session.as_summary()}
            if (
                normalized_action == "replay_snapshot"
                and session.provider_degradation.get("degraded") is True
                and not acknowledge_degraded_fallback
            ):
                return {"error": "degraded_fallback_acknowledgement_required", "session": session.as_summary()}
            created_at = _utc_now()
            event = {
                "id": f"browser-control:{session_id}:{normalized_action}:{len(session.control_events) + 1}",
                "action": normalized_action,
                "status": "applied",
                "created_at": created_at,
                "operator_visible": True,
                "reason": reason.strip() or "operator_requested",
            }
            if normalized_action == "quarantine":
                session.status = "quarantined"
                session.risk_state = "operator_quarantined"
                session.recovery_state = "operator_review_required"
            elif normalized_action == "recover":
                session.status = "degraded" if session.provider_degradation.get("degraded") else "open"
                session.risk_state = (
                    "provider_degraded_labeled"
                    if session.provider_degradation.get("degraded")
                    else "nominal"
                )
                session.recovery_state = "operator_recovered"
            elif normalized_action == "reset_partition":
                for snapshot in session.snapshots:
                    self._refs.pop(snapshot.ref, None)
                session.snapshots = []
                session.partition_revision += 1
                session.partition_id = f"bp-{uuid.uuid4().hex[:10]}"
                session.risk_state = "partition_reset"
                session.recovery_state = "needs_fresh_snapshot"
            elif normalized_action == "replay_snapshot":
                session.recovery_state = "replay_snapshot_recorded"
            elif normalized_action == "close":
                self._sessions.pop(session_id, None)
                for snapshot in session.snapshots:
                    self._refs.pop(snapshot.ref, None)
                session.status = "closed"
                session.recovery_state = "closed_by_operator"
            else:
                return {"error": "unsupported_control_action", "session": session.as_summary()}
            session.updated_at = created_at
            session.control_events.append(event)
            return {"event": event, "session": session.as_summary()}


browser_session_runtime = BrowserSessionRuntime()
