"""Native M2 evidence staging and bounded execution helpers."""
from __future__ import annotations

import os

from config.settings import settings
from src.guardian.opportunity_contracts import EvidenceSource, OpportunityEvidence, OpportunityError, digest, json_bytes
from src.workspace import canonical_workspace_root

CAPABILITY = "guardian.opportunity-assess.v1"
JOB_KIND = "guardian_opportunity_assess"
PREFIX = "artifacts/work-board/opportunities/"
EVIDENCE_LIMIT = 16384


def build_evidence(*, packet, observations):
    """Select first two already-redacted material public observations only."""
    selected = sorted((item for item in observations if item.source.kind == "public_https_text"),
                      key=lambda item: item.source.source_key)[:2]
    remaining = 4096
    sources = []
    for item in selected:
        lines = []
        # First 200 normalized whole redacted lines, never byte slicing.
        normalized = item.after_excerpt.replace("\r\n", "\n").replace("\r", "\n")
        for line in normalized.split("\n")[:200]:
            proposed = "\n".join([*lines, line])
            if len(proposed.encode("utf-8")) > remaining:
                break
            lines.append(line)
        excerpt = "\n".join(lines)
        if not excerpt.strip():
            continue
        from src.guardian.source_watch import _safe_public_source_target
        if _safe_public_source_target(item.source.target) != item.source.target:
            raise OpportunityError("source_excerpt_unavailable")
        remaining -= len(excerpt.encode("utf-8"))
        sources.append(EvidenceSource(source_key=item.source.source_key,
            identity_digest=item.source.identity_digest, target=item.source.target,
            new_hash=item.new_hash, excerpt=excerpt, excerpt_sha256=digest(excerpt.encode("utf-8"))))
    if not sources:
        raise OpportunityError("source_excerpt_unavailable")
    return OpportunityEvidence(packet_id=packet.id, checkpoint_sha256=packet.observed_checkpoint_sha256,
        watch_revision=packet.plan_revision, goal_revision=packet.goal_revision, sources=sources)


def stage_snapshot(evidence):
    from src.work_board.input_artifacts import _write_payload
    payload = json_bytes(evidence.model_dump(mode="json"))
    if len(payload) > EVIDENCE_LIMIT:
        raise OpportunityError("source_excerpt_unavailable")
    sha = digest(payload)
    reference = f"{PREFIX}{evidence.packet_id}-{sha}.json"
    _write_payload(canonical_workspace_root(settings.workspace_dir) / reference, payload)
    if read_snapshot(reference, sha) != evidence:
        raise OpportunityError("source_stale")
    return reference, sha


def read_snapshot(reference, sha):
    from src.work_board.input_artifacts import _open_input_artifact_parent, _safe_file_bytes
    from src.work_board.repository import BoardError
    if not isinstance(reference, str) or not reference.startswith(PREFIX) or ".." in reference.split("/"):
        raise OpportunityError("source_excerpt_unavailable")
    path = canonical_workspace_root(settings.workspace_dir) / reference
    try:
        parent, leaf = _open_input_artifact_parent(path, create=False)
        try:
            fd = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            try:
                size = os.fstat(fd).st_size
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        if not 0 < size <= EVIDENCE_LIMIT:
            raise ValueError("snapshot byte limit")
        payload = _safe_file_bytes(path, expected_digest=sha, expected_size=size)
        evidence = OpportunityEvidence.model_validate_json(payload)
    except (OSError, ValueError, BoardError) as exc:
        raise OpportunityError("source_excerpt_unavailable") from exc
    if sum(len(item.excerpt.encode("utf-8")) for item in evidence.sources) > 4096:
        raise OpportunityError("source_stale")
    for source in evidence.sources:
        if digest(source.excerpt.encode("utf-8")) != source.excerpt_sha256 or len(source.excerpt.split("\n")) > 200:
            raise OpportunityError("source_stale")
    return evidence
