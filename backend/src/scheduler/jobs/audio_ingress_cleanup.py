"""Periodic cleanup for expired push-to-talk quarantine state."""

from __future__ import annotations

from src.guardian.audio_worker import cleanup_expired_audio_ingress_jobs


async def run_audio_ingress_cleanup() -> None:
    """Remove only deadline-expired audio artifacts and metadata state."""
    await cleanup_expired_audio_ingress_jobs()
