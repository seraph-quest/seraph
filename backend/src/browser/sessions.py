"""In-memory browser session runtime for structured browsing workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import uuid


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _excerpt(content: str, *, limit: int = 180) -> str:
    collapsed = " ".join(str(content or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit - 1]}…"


@dataclass
class BrowserSnapshot:
    ref: str
    capture: str
    content: str
    created_at: str
    summary: str

    def as_payload(self) -> dict[str, str]:
        return {
            "ref": self.ref,
            "capture": self.capture,
            "created_at": self.created_at,
            "summary": self.summary,
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
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "snapshot_count": len(self.snapshots),
            "latest_ref": latest.ref if latest is not None else None,
            "latest_capture": latest.capture if latest is not None else None,
            "latest_summary": latest.summary if latest is not None else "",
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
        snapshot = BrowserSnapshot(
            ref=f"{session_id}:1",
            capture=capture,
            content=content,
            created_at=created_at,
            summary=_excerpt(content),
        )
        session = BrowserSession(
            session_id=session_id,
            owner_session_id=owner_session_id,
            url=url,
            provider_name=provider_name,
            provider_kind=provider_kind,
            execution_mode=execution_mode,
            created_at=created_at,
            updated_at=created_at,
            snapshots=[snapshot],
        )
        with self._lock:
            self._sessions[session_id] = session
            self._refs[snapshot.ref] = (session_id, 0)
        payload = session.as_summary()
        payload["content"] = content
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
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            created_at = _utc_now()
            snapshot = BrowserSnapshot(
                ref=f"{session_id}:{len(session.snapshots) + 1}",
                capture=capture,
                content=content,
                created_at=created_at,
                summary=_excerpt(content),
            )
            session.snapshots.append(snapshot)
            session.updated_at = created_at
            self._refs[snapshot.ref] = (session_id, len(session.snapshots) - 1)
            payload = session.as_summary()
            payload["content"] = content
            return payload

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
            }

    def close_session(self, session_id: str, *, owner_session_id: str) -> dict[str, object] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.owner_session_id != owner_session_id:
                return None
            session = self._sessions.pop(session_id)
            for snapshot in session.snapshots:
                self._refs.pop(snapshot.ref, None)
            return session.as_summary()


browser_session_runtime = BrowserSessionRuntime()
