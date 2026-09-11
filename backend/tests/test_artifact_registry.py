import hashlib
import json
import time
from dataclasses import replace

import pytest

from config.settings import settings
from src.artifacts.registry import build_artifact_record
from src.security.trust_contract import (
    AuthorityGrant,
    ContentOrigin,
    DestinationClass,
    EgressClass,
    NO_OBJECT,
    NO_RESOURCE_LIMITS,
    NO_SECRET_SCOPE,
    NO_TRANSFORMATION,
    PrincipalType,
    TrustDestination,
    TrustOperation,
    TrustPrincipal,
    TrustProvenance,
    TrustRequest,
    TrustResource,
    authority_scope_digest,
    canonical_digest,
    evaluate_trust,
)


def _artifact_request(
    content: str,
    *,
    source_id: str = "workflow:brief:output",
) -> TrustRequest:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    destination = TrustDestination(
        destination_id="workspace-artifact-registry",
        destination_class=DestinationClass.LOCAL_RUNTIME,
    )
    resource = TrustResource(
        resource_type="artifact",
        resource_id="artifact:workspace-output",
        object_digest=NO_OBJECT,
    )
    return TrustRequest(
        principal=TrustPrincipal(
            principal_id="operator:artifact-test",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.ARTIFACT_TRANSFER,),
            session_id="session-1",
        ),
        provenance=(
            TrustProvenance(
                origin=ContentOrigin.PROVIDER_OUTPUT,
                source_id=source_id,
                data_digest=digest,
                egress_class=EgressClass.LOCAL_ONLY,
                instruction_authority=False,
            ),
        ),
        destination=destination,
        operation=TrustOperation.ARTIFACT_TRANSFER,
        required_grant=AuthorityGrant.ARTIFACT_TRANSFER,
        capability_id="filesystem_patch_receipt",
        capability_version="v1",
        data_digest=digest,
        secret_scope_digest=NO_SECRET_SCOPE,
        resource_limits_digest=NO_RESOURCE_LIMITS,
        transformation_digest=NO_TRANSFORMATION,
        authority_scope_digest=authority_scope_digest(
            required_grant=AuthorityGrant.ARTIFACT_TRANSFER,
            capability_id="filesystem_patch_receipt",
            destination=destination,
            resource=resource,
        ),
        resource=resource,
        session_id="session-1",
        job_id="",
        request_id="request:artifact-test",
        attempt_id="attempt:artifact-test",
        replay_id="replay:artifact-test",
        decision_expires_at=time.time() + 60.0,
        egress_class=EgressClass.LOCAL_ONLY,
    )


def test_legacy_artifact_is_truthfully_unclassified(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))

    record = build_artifact_record(
        file_path="notes/legacy.md",
        producer="legacy-workflow",
        content="legacy content",
    )

    assert record["trust"] == {
        "state": "legacy_unclassified",
        "policy_version": None,
        "egress_class": None,
        "principal": None,
        "decision": None,
        "provenance": [],
    }


def test_governed_artifact_preserves_safe_trust_metadata_without_content(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    content = "private artifact content"
    request = _artifact_request(content)
    decision = evaluate_trust(request)

    record = build_artifact_record(
        file_path="notes/governed.md",
        artifact_type="workspace_patch",
        producer="filesystem:patch",
        content=content,
        governed=True,
        trust_request=request,
        trust_decision=decision,
    )

    assert record["content_sha256"] == request.data_digest
    assert record["size_bytes"] == len(content.encode("utf-8"))
    assert record["trust"]["state"] == "governed"
    assert record["trust"]["egress_class"] == "local_only"
    assert record["trust"]["principal"] == {
        "principal_id_digest": canonical_digest(
            {"principal_id": "operator:artifact-test"}
        ),
        "principal_type": "operator",
        "authenticated": True,
        "revoked": False,
    }
    assert record["trust"]["decision"]["allowed"] is True
    assert record["trust"]["provenance"] == [
        {
            "origin": "provider_output",
            "source_ref_digest": canonical_digest(
                {"source_id": "workflow:brief:output"}
            ),
            "data_digest_receipt": canonical_digest(
                {"data_digest": request.data_digest}
            ),
            "egress_class": "local_only",
            "instruction_authority": False,
        }
    ]
    serialized = json.dumps(record, sort_keys=True)
    assert content not in serialized
    assert "operator:artifact-test" not in serialized


def test_governed_artifact_digests_secret_like_source_references(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    content = "safe artifact"
    secret_source = "token-value-private-path"
    request = _artifact_request(content, source_id=secret_source)

    record = build_artifact_record(
        file_path="notes/governed.md",
        content=content,
        governed=True,
        trust_request=request,
        trust_decision=evaluate_trust(request),
    )

    serialized = json.dumps(record, sort_keys=True)
    assert content not in serialized
    assert secret_source not in serialized
    assert "token-value" not in serialized
    assert record["trust"]["provenance"][0]["source_ref_digest"] == canonical_digest(
        {"source_id": secret_source}
    )


def test_governed_artifact_denies_raw_secret_in_digest_field(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    content = "safe artifact"
    request = _artifact_request(content)
    hostile_request = replace(
        request,
        provenance=(
            replace(
                request.provenance[0],
                data_digest="RAW-SECRET-IN-DIGEST-FIELD",
            ),
        ),
    )

    with pytest.raises(PermissionError, match="provenance_digest_invalid"):
        build_artifact_record(
            file_path="notes/governed.md",
            content=content,
            governed=True,
            trust_request=hostile_request,
            trust_decision=evaluate_trust(hostile_request),
        )


@pytest.mark.parametrize("missing", ["request", "decision"])
def test_governed_artifact_denies_missing_authority_metadata(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    content = "governed content"
    request = _artifact_request(content)
    decision = evaluate_trust(request)

    with pytest.raises(ValueError, match="authority request and decision"):
        build_artifact_record(
            file_path="notes/governed.md",
            content=content,
            governed=True,
            trust_request=None if missing == "request" else request,
            trust_decision=None if missing == "decision" else decision,
        )


def test_governed_artifact_denies_digest_or_principal_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    content = "governed content"
    request = _artifact_request(content)

    with pytest.raises(ValueError, match="exact content digest"):
        build_artifact_record(
            file_path="notes/governed.md",
            content="changed after decision",
            governed=True,
            trust_request=request,
            trust_decision=evaluate_trust(request),
        )

    revoked_request = replace(
        request,
        principal=replace(request.principal, revoked=True),
    )
    with pytest.raises(PermissionError, match="principal_revoked"):
        build_artifact_record(
            file_path="notes/governed.md",
            content=content,
            governed=True,
            trust_request=revoked_request,
            trust_decision=evaluate_trust(revoked_request),
        )

    expired_request = replace(request, decision_expires_at=time.time() - 1.0)
    with pytest.raises(PermissionError, match="decision_expired"):
        build_artifact_record(
            file_path="notes/governed.md",
            content=content,
            governed=True,
            trust_request=expired_request,
            trust_decision=evaluate_trust(expired_request),
        )
