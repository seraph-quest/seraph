"""Browser session runtime for structured browsing workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit, urlunsplit
import uuid
from typing import Any

from config.settings import settings


_JOURNAL_SCHEMA = "seraph.browser_session_journal.v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _metadata_summary(*, capture: str, content: str) -> str:
    return f"{capture} capture metadata digest {_stable_digest(('browser-summary', content))[:16]}"


def _public_url(url: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"url-digest:{_stable_digest(('url', url))[:16]}"
    if not parts.scheme or not parts.netloc:
        return f"url-digest:{_stable_digest(('url', url))[:16]}"
    hostname = parts.hostname or ""
    netloc = hostname
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    path = parts.path or ""
    query = "redacted" if parts.query else ""
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def _url_redaction_required(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    return bool(parts.username or parts.password or parts.query or parts.fragment)


def _journal_path_for_workspace(workspace_dir: str | None = None) -> Path:
    root = Path(workspace_dir or settings.workspace_dir).expanduser().resolve()
    return root / "artifacts" / "browser-session-journal" / "session-journal.jsonl"


def _metadata_snapshot(snapshot: "BrowserSnapshot") -> dict[str, object]:
    return {
        "ref": snapshot.ref,
        "capture": snapshot.capture,
        "created_at": snapshot.created_at,
        "summary": snapshot.summary,
        "content_digest": snapshot.artifact_provenance.get("content_digest")
        or snapshot.artifact_provenance.get("artifact_body_digest"),
        "content_available": bool(snapshot.content),
        "artifact_provenance": snapshot.artifact_provenance,
    }


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

    def as_metadata(self) -> dict[str, object]:
        return _metadata_snapshot(self)


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
    replayable: bool = True

    def latest_snapshot(self) -> BrowserSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def as_summary(self) -> dict[str, object]:
        latest = self.latest_snapshot()
        public_url = _public_url(self.url)
        return {
            "session_id": self.session_id,
            "owner_session_id": self.owner_session_id,
            "url": public_url,
            "url_redacted": self.url != public_url,
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
            "journal_entry_count": len(self.control_events),
            "journal_schema": _JOURNAL_SCHEMA,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "snapshot_count": len(self.snapshots),
            "latest_ref": latest.ref if latest is not None else None,
            "latest_capture": latest.capture if latest is not None else None,
            "latest_summary": latest.summary if latest is not None else "",
            "latest_artifact_provenance": latest.artifact_provenance if latest is not None else None,
            "replayable": self.replayable,
            "replayability_reason": (
                ""
                if self.replayable
                else "private_execution_target_not_persisted"
            ),
        }


class BrowserSessionRuntime:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, BrowserSession] = {}
        self._refs: dict[str, tuple[str, int]] = {}
        self._loaded_journal_path: Path | None = None

    def _journal_path_locked(self) -> Path:
        if self._loaded_journal_path is None:
            self._loaded_journal_path = _journal_path_for_workspace()
        return self._loaded_journal_path

    def reset_for_tests(self, *, delete_journal: bool = False) -> None:
        path = self._loaded_journal_path or _journal_path_for_workspace()
        with self._lock:
            self._sessions = {}
            self._refs = {}
            self._loaded_journal_path = None
        if delete_journal:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _ensure_loaded_locked(self) -> None:
        if self._loaded_journal_path is not None:
            return
        path = self._journal_path_locked()
        self._sessions = {}
        self._refs = {}
        if not path.exists():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for raw_line in lines:
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("journal_schema") != _JOURNAL_SCHEMA:
                continue
            self._apply_journal_entry_locked(entry)

    def _append_journal_entry_locked(self, entry: dict[str, object]) -> None:
        path = self._journal_path_locked()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "journal_schema": _JOURNAL_SCHEMA,
            "entry_id": entry.get("entry_id") or f"browser-journal:{uuid.uuid4().hex}",
            "recorded_at": entry.get("recorded_at") or _utc_now(),
            **entry,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    def _append_session_journal_entry_locked(
        self,
        session: BrowserSession,
        *,
        action: str,
        status: str,
        reason: str = "",
        snapshot: BrowserSnapshot | None = None,
        event: dict[str, object] | None = None,
    ) -> None:
        latest = session.latest_snapshot()
        target_snapshot = snapshot or latest
        entry = {
            "entry_id": f"browser-journal:{session.session_id}:{action}:{len(session.control_events)}:{uuid.uuid4().hex[:8]}",
            "recorded_at": _utc_now(),
            "action": action,
            "status": status,
            "reason": reason,
            "session": session.as_summary(),
            "snapshot": target_snapshot.as_metadata() if target_snapshot is not None else None,
            "event": event or {},
            "redaction": {
                "metadata_only": True,
                "raw_dom_stored": False,
                "screenshot_stored": False,
                "secret_values_stored": False,
                "credential_values_stored": False,
                "profile_path_stored": False,
                "private_page_content_stored": False,
            },
        }
        self._append_journal_entry_locked(entry)

    def _apply_journal_entry_locked(self, entry: dict[str, object]) -> None:
        session_payload = entry.get("session")
        if not isinstance(session_payload, dict):
            return
        session_id = str(session_payload.get("session_id") or "")
        owner_session_id = str(session_payload.get("owner_session_id") or "")
        if not session_id or not owner_session_id:
            return
        snapshots: list[BrowserSnapshot] = []
        snapshot_payload = entry.get("snapshot")
        if isinstance(snapshot_payload, dict):
            ref = str(snapshot_payload.get("ref") or "")
            if ref:
                snapshots.append(
                    BrowserSnapshot(
                        ref=ref,
                        capture=str(snapshot_payload.get("capture") or "extract"),
                        content="",
                        created_at=str(snapshot_payload.get("created_at") or entry.get("recorded_at") or ""),
                        summary=str(snapshot_payload.get("summary") or ""),
                        artifact_provenance=(
                            snapshot_payload.get("artifact_provenance")
                            if isinstance(snapshot_payload.get("artifact_provenance"), dict)
                            else {}
                        ),
                    )
                )
        existing = self._sessions.get(session_id)
        if existing is None:
            stored_url = str(session_payload.get("url") or "")
            recovered_redacted_url = bool(session_payload.get("url_redacted")) or _url_redaction_required(
                stored_url
            )
            existing = BrowserSession(
                session_id=session_id,
                owner_session_id=owner_session_id,
                url=stored_url,
                provider_name=str(session_payload.get("provider_name") or "unknown"),
                provider_kind=str(session_payload.get("provider_kind") or "unknown"),
                execution_mode=str(session_payload.get("execution_mode") or "unknown"),
                created_at=str(session_payload.get("created_at") or entry.get("recorded_at") or ""),
                updated_at=str(session_payload.get("updated_at") or entry.get("recorded_at") or ""),
                status=str(session_payload.get("status") or "open"),
                risk_state=str(session_payload.get("risk_state") or "nominal"),
                recovery_state=str(session_payload.get("recovery_state") or "ready"),
                partition_id=str(session_payload.get("partition_id") or ""),
                partition_revision=int(session_payload.get("partition_revision") or 1),
                boundary_decisions=(
                    session_payload.get("boundary_decisions")
                    if isinstance(session_payload.get("boundary_decisions"), dict)
                    else {}
                ),
                provider_degradation=(
                    session_payload.get("provider_degradation")
                    if isinstance(session_payload.get("provider_degradation"), dict)
                    else {}
                ),
                control_events=[],
                snapshots=[],
                replayable=not recovered_redacted_url,
            )
            self._sessions[session_id] = existing
        elif bool(session_payload.get("url_redacted")):
            # A journal entry carries only the public URL.  Once that is the
            # recovered state, the private execution target is unavailable.
            existing.replayable = False
        existing.status = str(session_payload.get("status") or existing.status)
        existing.risk_state = str(session_payload.get("risk_state") or existing.risk_state)
        existing.recovery_state = str(session_payload.get("recovery_state") or existing.recovery_state)
        existing.partition_id = str(session_payload.get("partition_id") or existing.partition_id)
        existing.partition_revision = int(session_payload.get("partition_revision") or existing.partition_revision)
        existing.updated_at = str(session_payload.get("updated_at") or entry.get("recorded_at") or existing.updated_at)
        control_events = session_payload.get("control_events")
        if isinstance(control_events, list):
            existing.control_events = [
                item for item in control_events if isinstance(item, dict)
            ]
        for snapshot in snapshots:
            if all(item.ref != snapshot.ref for item in existing.snapshots):
                existing.snapshots.append(snapshot)
        existing.snapshots.sort(key=lambda item: item.created_at)
        for index, snapshot in enumerate(existing.snapshots):
            self._refs[snapshot.ref] = (session_id, index)

    def list_journal(self, *, owner_session_id: str, session_id: str | None = None) -> list[dict[str, object]]:
        with self._lock:
            self._ensure_loaded_locked()
            path = self._journal_path_locked()
            if not path.exists():
                return []
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        entries: list[dict[str, object]] = []
        for raw_line in lines:
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("journal_schema") != _JOURNAL_SCHEMA:
                continue
            session = entry.get("session")
            if not isinstance(session, dict) or session.get("owner_session_id") != owner_session_id:
                continue
            if session_id and session.get("session_id") != session_id:
                continue
            public_entry = dict(entry)
            public_entry.pop("content", None)
            public_entry["raw_content_available"] = False
            entries.append(public_entry)
        entries.sort(key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
        return entries

    def list_sessions(self, *, owner_session_id: str) -> list[dict[str, object]]:
        with self._lock:
            self._ensure_loaded_locked()
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
            summary=_metadata_summary(capture=capture, content=content),
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
            self._ensure_loaded_locked()
            self._sessions[session_id] = session
            self._refs[snapshot.ref] = (session_id, 0)
            self._append_session_journal_entry_locked(
                session,
                action="open",
                status="recorded",
                snapshot=snapshot,
                event=session.control_events[-1],
            )
        payload = session.as_summary()
        payload["content"] = snapshot.content
        return payload

    def snapshot_session(
        self,
        *,
        owner_session_id: str,
        session_id: str,
        capture: str,
        content: str,
    ) -> dict[str, object] | None:
        with self._lock:
            self._ensure_loaded_locked()
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
                summary=_metadata_summary(capture=capture, content=content),
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
            self._append_session_journal_entry_locked(
                session,
                action="snapshot",
                status="recorded",
                snapshot=snapshot,
                event=session.control_events[-1],
            )
            payload = session.as_summary()
            payload["content"] = snapshot.content
            return payload

    def get_session(self, session_id: str, *, owner_session_id: str) -> dict[str, object] | None:
        with self._lock:
            self._ensure_loaded_locked()
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            payload = session.as_summary()
            payload["snapshots"] = [snapshot.as_metadata() for snapshot in session.snapshots]
            return payload

    def get_session_capture_url(self, session_id: str, *, owner_session_id: str) -> str | None:
        with self._lock:
            self._ensure_loaded_locked()
            session = self._sessions.get(session_id)
            if (
                session is None
                or session.owner_session_id != owner_session_id
                or not session.replayable
            ):
                return None
            return session.url

    def read_ref(self, ref: str, *, owner_session_id: str) -> dict[str, object] | None:
        with self._lock:
            self._ensure_loaded_locked()
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
            public_url = _public_url(session.url)
            return {
                "session_id": session_id,
                "owner_session_id": session.owner_session_id,
                "ref": snapshot.ref,
                "capture": snapshot.capture,
                "content": snapshot.content if snapshot.content else None,
                "content_available": bool(snapshot.content),
                "summary": snapshot.summary,
                "url": public_url,
                "url_redacted": public_url != session.url or _url_redaction_required(session.url),
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
            self._ensure_loaded_locked()
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            if session.status == "quarantined":
                return {"error": "session_quarantined", "session": session.as_summary()}
            if session.provider_degradation.get("degraded") is True and not acknowledge_degraded_fallback:
                return {"error": "degraded_fallback_acknowledgement_required", "session": session.as_summary()}
            if not session.replayable:
                return {
                    "error": "session_replay_unavailable_after_reload",
                    "session": session.as_summary(),
                }
            return {"session": session.as_summary()}

    def close_session(self, session_id: str, *, owner_session_id: str) -> dict[str, object] | None:
        with self._lock:
            self._ensure_loaded_locked()
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            session = self._sessions.pop(session_id)
            for snapshot in session.snapshots:
                self._refs.pop(snapshot.ref, None)
            session.status = "closed"
            session.recovery_state = "closed_by_operator"
            session.updated_at = _utc_now()
            session.control_events.append(
                {
                    "id": f"browser-control:{session_id}:close:{len(session.control_events) + 1}",
                    "action": "close",
                    "status": "applied",
                    "created_at": session.updated_at,
                    "operator_visible": True,
                    "reason": "operator_requested",
                }
            )
            self._append_session_journal_entry_locked(
                session,
                action="close",
                status="applied",
                event=session.control_events[-1],
            )
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
            self._ensure_loaded_locked()
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
            if normalized_action == "replay_snapshot" and not session.replayable:
                return {
                    "error": "session_replay_unavailable_after_reload",
                    "session": session.as_summary(),
                }
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
            self._append_session_journal_entry_locked(
                session,
                action=normalized_action,
                status="applied",
                reason=reason.strip() or "operator_requested",
                event=event,
            )
            return {"event": event, "session": session.as_summary()}


browser_session_runtime = BrowserSessionRuntime()


class ProfiledInteractionSessions:
    """Explicit current-Python lifecycle over the canonical durable job owner.

    Live pages are ephemeral resource handles, never an authority ledger. All
    action history and original bounds remain in existing durable job rows.
    """

    def __init__(self, *, jobs=None, request=None, browser_launcher=None, source_digest=None):
        from src.workflows.job_runtime import durable_job_repository
        self.jobs = jobs or durable_job_repository
        self.request, self.browser_launcher = request, browser_launcher
        self.source_digest = source_digest
        self.active = {}
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        for job_id in list(self.active):
            await self._close(job_id, reason="runtime_stopped")
        self.started = False

    @staticmethod
    async def _root(db, owner):
        from sqlmodel import select
        from src.db.models import OperatorSession
        from .interaction_contracts import InteractionError
        now = datetime.now(timezone.utc)
        row = await db.scalar(select(OperatorSession).where(
            OperatorSession.id == owner.session_id,
            OperatorSession.principal_id == owner.principal_id,
            OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
            OperatorSession.is_bearer_tombstone.is_(False),
            OperatorSession.absolute_expires_at > now, OperatorSession.idle_expires_at > now))
        if row is None or not getattr(owner, "authenticated_token_hash", None) or row.token_hash != owner.authenticated_token_hash:
            raise InteractionError("browser_original_root_inactive", status_code=403)
        return row

    @classmethod
    async def _stable_root(cls, db, owner):
        from src.db.models import OperatorIdentity
        from .interaction_contracts import InteractionError
        row = await cls._root(db, owner)
        identity = await db.get(OperatorIdentity, row.operator_identity_id) if row.operator_identity_id else None
        if identity is None or identity.revoked_at is not None:
            raise InteractionError("browser_operator_continuity_required", status_code=403)
        return row

    async def _current(self, owner, job_id, *, db=None, run=None, historical=False):
        from src.db import engine
        from src.work_board.repository import WorkBoardRepository
        from src.workflows.job_runtime import _digest
        from .interaction_contracts import CAPABILITY, JOB_KIND, PROFILE, SOURCE_SHA256, InteractionError
        if db is None:
            async with engine.get_session() as session:
                return await self._current(owner, job_id, db=session, historical=historical)
        if run is None:
            run = await self.jobs._fetch(db, job_id)
        if (run.owner_principal_id != owner.principal_id or run.operator_session_id != owner.session_id
            or run.session_id != owner.session_id or run.owner_kind != "user"
            or run.job_kind != JOB_KIND or run.capability_version != "2"):
            raise InteractionError("browser_interaction_not_found", status_code=404)
        await self._root(db, owner)
        authority = json.loads(run.declared_authority_json)
        if (run.authority_digest != _digest(authority) or authority.get("capability_id") != CAPABILITY
            or authority.get("profile_id") != PROFILE
            or authority.get("principal") != owner.principal_id or authority.get("session_id") != owner.session_id
            or authority.get("no_learning") is not True
            or authority.get("goal_id") != run.goal_id or authority.get("goal_revision") != run.goal_revision
            or authority.get("request_body_digest") != run.input_digest
            or authority.get("profile_source_digest") != (self.source_digest or SOURCE_SHA256)
            or authority.get("root_binding_digest") != hashlib.sha256(owner.authenticated_token_hash.encode()).hexdigest()):
            raise InteractionError("browser_original_authority_changed")
        if not historical:
            await self._stable_root(db, owner)
            await WorkBoardRepository._validate_goal(db, owner, goal_id=run.goal_id, goal_revision=run.goal_revision)
            deadline = run.deadline_at
            if deadline is None or deadline.replace(tzinfo=deadline.tzinfo or timezone.utc) <= datetime.now(timezone.utc):
                raise InteractionError("browser_original_deadline_expired")
        return run

    async def _authority(self, owner, job_id):
        from .interaction_contracts import InteractionError
        state = self.active.get(job_id)
        if state is None or state.get("closing"):
            raise InteractionError("browser_live_page_unavailable")
        await self._current(owner, job_id)
        run = await self.jobs.assert_active_lease(job_id, owner=state["lease_owner"], fencing_token=state["fence"])
        if run["revision"] != state["revision"]:
            raise InteractionError("browser_current_job_revision_changed")

    async def _record(self, job_id, event):
        state = self.active[job_id]
        entry = {"sequence": len(state["history"]) + 1, "recorded_at": _utc_now(), **event}
        # Only structural intent/output metadata. No private input is passed.
        history = [*state["history"], entry]
        run = await self.jobs.record_checkpoint(job_id, checkpoint_id=f"browser-interact:event:{entry['sequence']}",
            state=entry, checkpoint_payload=entry,
            owner=state["lease_owner"], fencing_token=state["fence"],
            expected_revision=state["revision"], safe=True)
        state["revision"], state["history"] = run["revision"], history

    async def open(self, owner, inputs):
        import asyncio
        from datetime import timedelta
        from src.db import engine
        from src.work_board.repository import WorkBoardRepository
        from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
        from .interaction_contracts import CAPABILITY, JOB_KIND, PROFILE, SOURCE_SHA256, InteractionError, digest
        from .task_lane import try_acquire_browser_task_lane
        from .task_runner import ProfiledInteractionPage
        if not self.started:
            raise InteractionError("browser_interaction_runtime_inactive", status_code=503)
        job_id = "browser-interact-" + digest([owner.principal_id, owner.session_id, inputs.request_key])[:32]
        existing = await self.jobs.get_job(job_id)
        if existing is not None:
            await self._current(owner, job_id, historical=True)
            if existing["declared_authority"].get("request_body_digest") != digest(inputs.model_dump()):
                raise InteractionError("browser_request_key_body_changed")
            return await self.inspect(owner, job_id)
        async with engine.get_session() as db:
            root = await self._stable_root(db, owner)
            await WorkBoardRepository._validate_goal(db, owner, goal_id=inputs.goal_id, goal_revision=inputs.goal_revision)
            deadline = min(datetime.now(timezone.utc) + timedelta(seconds=180),
                root.absolute_expires_at.replace(tzinfo=root.absolute_expires_at.tzinfo or timezone.utc),
                root.idle_expires_at.replace(tzinfo=root.idle_expires_at.tzinfo or timezone.utc))
        lane = try_acquire_browser_task_lane(settings.workspace_dir)
        if lane is None:
            from .task_lane import browser_task_lane_wait_reason
            raise InteractionError(browser_task_lane_wait_reason(settings.workspace_dir) or "browser_lane_busy")
        state = None
        try:
            spec = DurableJobSpec(identity=DurableJobIdentity(job_id, "user", owner.principal_id,
                JOB_KIND, "2", CAPABILITY + ":" + owner.session_id, inputs.request_key),
                inputs=inputs.model_dump(), session_id=owner.session_id, operator_session_id=owner.session_id,
                goal_id=inputs.goal_id, goal_revision=inputs.goal_revision,
                resource_claims=("browser-task-lane",), max_attempts=1,
                deadline_at=deadline, budget_microusd=0,
                declared_authority={"capability_id": CAPABILITY, "profile_id": PROFILE,
                    "principal": owner.principal_id, "session_id": owner.session_id,
                    "goal_id": inputs.goal_id, "goal_revision": inputs.goal_revision,
                    "profile_source_digest": self.source_digest or SOURCE_SHA256,
                    "root_binding_digest": hashlib.sha256(owner.authenticated_token_hash.encode()).hexdigest(),
                    "request_body_digest": digest(inputs.model_dump()), "no_learning": True})
            row = await self.jobs.admit_job(spec)
            row = await self.jobs.queue_job(job_id, expected_revision=row["revision"])
            lease_owner = "browser-interact:" + uuid.uuid4().hex
            row = await self.jobs.claim_job(job_id, owner=lease_owner, lease_seconds=180,
                expected_revision=row["revision"],
                claim_authority_check=lambda db, run: self._current(owner, job_id, db=db, run=run))
            state = {"owner": owner, "lane": lane, "history": [], "revision": row["revision"],
                "fence": row["lease"]["fencing_token"], "lease_owner": lease_owner, "closing": False,
                "lock": asyncio.Lock(), "deadline": deadline, "timer": None, "page": None}
            self.active[job_id] = state
            state["cleanup_witness"] = lane.require_positive_cleanup(job_id)
            page = ProfiledInteractionPage(authority=lambda: self._authority(owner, job_id),
                intent=lambda event: self._record(job_id, {**event, "status": "intent"}),
                result=lambda event: self._record(job_id, event),
                request=self.request, browser_launcher=self.browser_launcher, source_digest=self.source_digest)
            state["page"] = page
            # Original finite deadline includes teardown; timer releases no capacity
            # without positive cleanup. It cannot renew an old Root or attempt.
            state["timer"] = asyncio.create_task(self._expire(job_id,
                max(0.001, (deadline - datetime.now(timezone.utc)).total_seconds() - 12)))
            async with state["lock"]:
                await asyncio.wait_for(page.start(), timeout=min(25,
                    max(0.001, (deadline - datetime.now(timezone.utc)).total_seconds() - 12)))
            return await self.inspect(owner, job_id)
        except BaseException:
            if state is not None:
                await self._close(job_id, reason="browser_open_failed")
            else:
                lane.release()
            raise

    async def _expire(self, job_id, seconds):
        import asyncio
        await asyncio.sleep(seconds)
        await self._close(job_id, reason="browser_original_deadline")

    async def inspect(self, owner, job_id):
        await self._current(owner, job_id, historical=True)
        row = await self.jobs.get_job(job_id)
        history = []
        for checkpoint in row.get("checkpoints", []):
            if str(checkpoint.get("checkpoint_id", "")).startswith("browser-interact:event:"):
                history.append(checkpoint.get("payload", {}))
        state = self.active.get(job_id)
        payload = {"capability_id": "browser.interact.v2", "job_id": job_id,
            "revision": row["revision"], "fencing_token": row["lease"]["fencing_token"],
            "status": row["status"], "history": history, "no_learning": True,
            "live": state is not None and not state["closing"],
            "recovery": "none" if state else "read_only_history_new_job_required"}
        payload["blocked_reason"] = row.get("failure_reason")
        payload["durable_status"] = row["status"]
        if state is None and row["status"] in {"running", "queued"}:
            from .task_lane import browser_task_lane_wait_reason
            reason = browser_task_lane_wait_reason(settings.workspace_dir)
            payload["status"] = "blocked"
            payload["blocked_reason"] = reason or "browser_live_page_unavailable"
            if reason:
                payload["recovery"] = "positive_original_cleanup_required"
        if state and state["page"] and state["page"].latest:
            payload["page"] = state["page"].latest.model_dump(mode="json")
        return payload

    async def list_jobs(self, owner):
        from sqlmodel import select
        from src.db import engine
        from src.db.models import WorkflowRunState
        from .interaction_contracts import JOB_KIND
        async with engine.get_session() as db:
            await self._root(db, owner)
            ids = (await db.execute(select(WorkflowRunState.run_identity).where(
                WorkflowRunState.owner_principal_id == owner.principal_id,
                WorkflowRunState.operator_session_id == owner.session_id,
                WorkflowRunState.job_kind == JOB_KIND).order_by(WorkflowRunState.updated_at.desc()).limit(21))).scalars().all()
        return {"jobs": [await self.inspect(owner, job_id) for job_id in ids[:20]], "has_more": len(ids) > 20}

    async def action(self, owner, job_id, request):
        import asyncio
        from .interaction_contracts import InteractionError, MAX_ACTIONS
        state = self.active.get(job_id)
        await self._current(owner, job_id)
        if state is None:
            raise InteractionError("browser_fresh_read_job_required")
        async with state["lock"]:
            await self._authority(owner, job_id)
            if request.expected_revision != state["revision"] or request.fencing_token != state["fence"]:
                raise InteractionError("browser_current_job_revision_changed")
            if state["page"].actions >= MAX_ACTIONS:
                raise InteractionError("browser_action_limit")
            if request.private_input is not None:
                await self._private_input(owner, job_id, request.action.input_value_ref, request.private_input)
            remaining = (state["deadline"] - datetime.now(timezone.utc)).total_seconds() - 12
            if remaining <= 0:
                raise InteractionError("browser_original_deadline_expired")
            if request.action.kind in {"fill", "select", "check", "click"}:
                state["preview_verified"] = False
            result = await asyncio.wait_for(state["page"].apply(request.action, request.private_input),
                timeout=min(10, remaining))
            if "preview" in result:
                preview_path, preview_digest = await self._private_input(owner, job_id, "preview-" + uuid.uuid4().hex, result["preview"])
                row = await self.jobs.record_effect(job_id, effect_type="browser_private_preview",
                    effect_id="browser-preview:" + uuid.uuid4().hex, receipt_kind="readback",
                    status="succeeded", target_path=preview_path, content_sha256=preview_digest,
                    details={"verified": True, "private": True, "no_learning": True},
                    owner=state["lease_owner"], fencing_token=state["fence"],
                    expected_revision=state["revision"],
                    readback_authority_check=lambda db, run: self._current(owner, job_id, db=db, run=run))
                state["revision"] = row["revision"]
                await self._record(job_id, {"kind": "preview_readback", "status": "completed",
                    "private_artifact_ref": preview_path, "no_learning": True})
                state["preview_verified"] = True
                state["preview_document_digest"] = state["page"].latest.document_digest
                state["preview_artifact"] = (preview_path, preview_digest)
            return {**await self.inspect(owner, job_id), **result}

    async def refresh(self, owner, job_id, request):
        from .interaction_contracts import InteractionError, MAX_ACTIONS
        state = self.active.get(job_id)
        await self._current(owner, job_id)
        if state is None:
            raise InteractionError("browser_fresh_read_job_required")
        async with state["lock"]:
            await self._authority(owner, job_id)
            if request.expected_revision != state["revision"] or request.fencing_token != state["fence"]:
                raise InteractionError("browser_current_job_revision_changed")
            if state["page"].actions >= MAX_ACTIONS:
                raise InteractionError("browser_action_limit")
            state["page"].actions += 1
            await self._record(job_id, {"kind": "snapshot", "status": "intent"})
            import asyncio
            remaining = max(0.001, min(10, (state["deadline"] - datetime.now(timezone.utc)).total_seconds() - 12))
            await asyncio.wait_for(state["page"].snapshot(), timeout=remaining)
            await self._record(job_id, {"kind": "snapshot", "status": "completed"})
            return await self.inspect(owner, job_id)

    async def _private_input(self, owner, job_id, ref, value):
        from src.vault.crypto import encrypt, decrypt
        from .task_runner import browser_artifact_path_for_job, _write_browser_artifact_bytes, read_browser_artifact_bytes
        from .interaction_contracts import canonical, InteractionError
        await self._authority(owner, job_id)
        path = browser_artifact_path_for_job(job_id + ":" + ref)
        # Single immutable value ref per job. Private literal readback precedes event.
        prior = read_browser_artifact_bytes(path)
        if prior is None:
            raw = encrypt(canonical(value).decode()).encode()
            _write_browser_artifact_bytes(path, raw)
        raw = read_browser_artifact_bytes(path)
        if raw is None or json.loads(decrypt(raw.decode())) != value:
            raise InteractionError("browser_private_value_reference_changed")
        await self._authority(owner, job_id)
        return path, hashlib.sha256(raw).hexdigest()

    async def close(self, owner, job_id, request):
        from .interaction_contracts import InteractionError
        await self._current(owner, job_id, historical=True)
        state = self.active.get(job_id)
        if state is None:
            return await self.inspect(owner, job_id)
        if request.expected_revision != state["revision"] or request.fencing_token != state["fence"]:
            raise InteractionError("browser_current_job_revision_changed")
        await self._close(job_id, reason="operator_closed")
        return await self.inspect(owner, job_id)

    async def _close(self, job_id, *, reason):
        import asyncio
        state = self.active.get(job_id)
        if state is None:
            return
        async with state["lock"]:
            if state["closing"]:
                return
            state["closing"] = True
            timer = state.get("timer")
            if timer is not None and timer != asyncio.current_task():
                timer.cancel()
            preview_current = False
            if state.get("preview_verified"):
                from .task_runner import read_browser_artifact_bytes
                try:
                    _, document_digest = await asyncio.wait_for(state["page"]._state(), timeout=1)
                    preview_path, preview_digest = state["preview_artifact"]
                    raw = read_browser_artifact_bytes(preview_path)
                    preview_current = (document_digest == state["preview_document_digest"]
                        and raw is not None and hashlib.sha256(raw).hexdigest() == preview_digest)
                except Exception:
                    preview_current = False
            cleanup_remaining = max(0.001, min(10, (state["deadline"] - datetime.now(timezone.utc)).total_seconds()))
            try:
                clean = state["page"] is None or await asyncio.wait_for(state["page"].stop(), timeout=cleanup_remaining)
            except BaseException:
                clean = False
            # Physical resource closure is independent from job CAS/authority.
            # A failed audit write cannot turn a positively closed browser into
            # an unknown live context. Exact marker confirmation still gates
            # capacity release.
            if clean:
                try:
                    state["lane"].confirm_positive_cleanup(state["cleanup_witness"])
                except Exception:
                    clean = False
            try:
                row = await self.jobs.record_effect(job_id, effect_type="browser_context_cleanup",
                    status="succeeded" if clean else "unknown", effect_id="browser-cleanup:" + job_id,
                    details={"cleanup_status": "closed" if clean else "cleanup_unknown", "no_learning": True},
                    owner=state["lease_owner"], fencing_token=state["fence"], expected_revision=state["revision"])
                state["revision"] = row["revision"]
                completed = clean and reason == "operator_closed" and preview_current
                terminal = "succeeded" if completed else "cancelled" if clean and reason == "operator_closed" else "blocked"
                try:
                    await self.jobs.transition_job(job_id, terminal,
                        owner=state["lease_owner"], fencing_token=state["fence"], expected_revision=state["revision"],
                        reason=reason if clean else "browser_cleanup_required", result={"no_learning": True},
                        terminal_authority_check=lambda db, run: self._current(state["owner"], job_id, db=db, run=run))
                except Exception:
                    if terminal != "succeeded":
                        raise
                    # Original success authority may have expired or changed
                    # during positive teardown. Never grant success in that case.
                    await self.jobs.transition_job(job_id, "cancelled",
                        owner=state["lease_owner"], fencing_token=state["fence"], expected_revision=state["revision"],
                        reason="browser_closed_success_authority_unavailable", result={"no_learning": True})
            except Exception:
                # Preserve unresolved durable state for reconciliation without
                # re-quarantining a positively closed original resource.
                if clean:
                    try:
                        await self.jobs.transition_job(job_id, "cancelled",
                            owner=state["lease_owner"], fencing_token=state["fence"], expected_revision=state["revision"],
                            reason="browser_closed_settlement_unresolved", result={"no_learning": True})
                    except Exception:
                        pass
            if clean:
                state["lane"].release()
            else:
                state["lane"].quarantine(job_id)
            self.active.pop(job_id, None)


# No start, context, network or task creation at import time.
profiled_interaction_sessions = ProfiledInteractionSessions()
