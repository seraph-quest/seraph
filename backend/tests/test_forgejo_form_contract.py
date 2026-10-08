"""Source schema/body/readback boundaries, with no runtime or network effects."""
import json

import pytest

from src.browser.forgejo_forms import (EMPTY_FORM_PROFILES, FORM_PROFILES, FormDocument,
    active_profiles, profile_document, encoded_controls, form_destination, exact_readback)
from src.browser.forgejo_issue_title import ForgejoError
from tests.test_inference_accounting import accounting_db


def test_activation_closed_sorted_versioned_empty_default():
    assert active_profiles(EMPTY_FORM_PROFILES) == []
    assert active_profiles(profile_document(list(FORM_PROFILES))) == list(FORM_PROFILES)
    for ids in ([FORM_PROFILES[1], FORM_PROFILES[0]], [FORM_PROFILES[0]] * 2, ["other"], "other", [True]):
        with pytest.raises(ForgejoError): profile_document(ids)
    value = json.loads(EMPTY_FORM_PROFILES)
    for key, replacement in (("schema_version", True), ("provider_version", "16"),
                              ("source_manifest_digest", "0" * 64), ("unknown", "hidden")):
        with pytest.raises(ForgejoError): active_profiles(json.dumps({**value, key: replacement}))


def test_typed_exact_transaction_excludes_boolean_mutation_and_foreign_field_identity():
    from pydantic import ValidationError
    from src.browser.interaction_contracts import FormTransaction
    value={"profile_ref":"forgejo.issue-comment.v1","page_digest":"a"*64,"form_identity":"b"*64,
        "field_digests":{"content":"c"*64},"submit_node":"comment-form:ordinary-primary",
        "expected_destination":"/a/r/issues/2#issuecomment-{positive_id}",
        "readback_contract":"numeric-basic-api-full-literal.v1","encoded_body_digest":"d"*64}
    assert FormTransaction(**value).mutation_allowance==1
    for change in ({"mutation_allowance":True},{"mutation_allowance":2},{"field_digests":{"status":"c"*64}},
                   {"submit_node":"new-issue:ordinary-primary"},{"field_digests":{"content":"invalid"}}):
        with pytest.raises(ValidationError):FormTransaction(**{**value,**change})
    create={**value,"profile_ref":"forgejo.issue-create.v1","submit_node":"new-issue:ordinary-primary",
        "field_digests":{name:"c"*64 for name in ("title","content","ref","edit_mode","search","label_ids","milestone_id","project_id","assignee_ids")}}
    assert FormTransaction(**create).mutation_allowance == 1
    for fields in ({name:d for name,d in create["field_digests"].items() if name!="project_id"},
                   {**create["field_digests"],"redirect_after_creation":"c"*64}):
        with pytest.raises(ValidationError):FormTransaction(**{**create,"field_digests":fields})


@pytest.mark.asyncio
async def test_additive_profile_migration_is_rerunnable_and_preserves_original_rows():
    from sqlalchemy.ext.asyncio import create_async_engine
    from src.db.engine import _ensure_forgejo_form_profiles
    database = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with database.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE forgejo_connections (id VARCHAR PRIMARY KEY, revision INTEGER, site_profile VARCHAR, read_consent_revision INTEGER)")
            await conn.exec_driver_sql("INSERT INTO forgejo_connections VALUES ('existing', 7, 'seraph.forgejo.codeberg-title.v1', 3)")
            await _ensure_forgejo_form_profiles(conn)
            await _ensure_forgejo_form_profiles(conn)
            row=(await conn.exec_driver_sql("SELECT * FROM forgejo_connections")).one()
            assert tuple(row[:4]) == ('existing',7,'seraph.forgejo.codeberg-title.v1',3)
            assert row[4] == EMPTY_FORM_PROFILES and row[5] == 0
    finally:
        await database.dispose()


def test_complete_encoded_body_bound_including_names_and_escape_expansion():
    assert encoded_controls([("content", " *~&")]) == b"content=+*%7E%26"
    assert len(encoded_controls([("content", "a" * (16384 - len("content=")))])) == 16384
    for value in ("a" * 16384, "&" * 6000):
        with pytest.raises(ForgejoError): encoded_controls([("content", value)])


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", FORM_PROFILES)
async def test_encrypted_preview_pairs_roundtrip_and_strict_readback(accounting_db, profile):
    from src.browser.forgejo_native import stage
    from src.browser.forgejo_issue_title import digest
    from tests.test_forgejo_form_grammar import captured
    root, _, _ = accounting_db
    source, native, slots, form_id = captured(profile)
    document = FormDocument(form_id=form_id, action=dict(native["form"]["attributes"])["action"],
                            profile=profile, slots=slots)
    document.feed(source.decode()); document.close()
    controls = document.reviewed_controls(title="Private preview title", content="Private preview & literal body")
    pairs = [[name, value] for name, value in controls]
    payload = {"target": {"profile": profile, "controls": pairs,
                          "encoded_body_digest": digest(encoded_controls(controls))}, "no_learning": True}
    job = "forgejo:" + ("1" if profile.endswith("create.v1") else "2") * 40
    ref, cipher_digest, actual = stage(job, "output", payload)
    assert actual == payload == json.loads(json.dumps(payload))
    assert actual["target"]["encoded_body_digest"] == digest(encoded_controls(actual["target"]["controls"]))
    assert stage(job, "output")[2] == payload
    encrypted = (root / ref).read_bytes()
    assert digest(encrypted) == cipher_digest and b"Private preview" not in encrypted
    # Do not normalize or weaken the strict encrypted-artifact comparison to
    # accommodate an unsupported in-memory tuple payload or changed literal.
    tuple_payload = {**payload, "target": {**payload["target"], "controls": controls}}
    with pytest.raises(ForgejoError, match="forgejo_private_artifact_readback_changed"):
        stage(job, "output", tuple_payload)
    changed = {**payload, "target": {**payload["target"], "controls": pairs + [["status", "close"]]}}
    with pytest.raises(ForgejoError, match="forgejo_private_artifact_readback_changed"):
        stage(job, "output", changed)


def test_comment_ordinary_control_and_exact_submitter():
    document = '<form id="comment-form" action="/a/r/issues/2/comments" method="post"><textarea name="content"></textarea><button id="status-button" name="status" value="close">Close</button><button class="primary button">Comment</button></form>'
    def parsed(raw):
        from tests.test_forgejo_form_grammar import captured
        from src.browser.forgejo_form_grammar import COMMENT
        _, _, slots, _ = captured(COMMENT)
        value = FormDocument(form_id="comment-form", action="/seraph_fixture1013/fixture1013/issues/1/comments", profile=COMMENT, slots=slots)
        value.feed(raw); value.close()
        return value.reviewed_controls(title="", content="Exact body")
    # A guessed small form is insufficient; real signed-source positive
    # acceptance is owned by test_forgejo_form_grammar's complete captures.
    with pytest.raises(ForgejoError): parsed(document)
    for changed in (document.replace('</textarea>', 'Prefill</textarea>'),
                    document.replace('</form>', '<input name="hidden" value=""></form>'),
                    document.replace('</form>', '<input type="file"></form>'),
                    document.replace('class="primary button"', 'name="status" value="close" class="primary button"')):
        with pytest.raises(ForgejoError): parsed(changed)


def test_destination_and_comment_readback_require_exact_id_actor_parent_and_empty_pr():
    profile = "forgejo.issue-comment.v1"
    assert form_destination(b'{"redirect":"/a/r/issues/2#issuecomment-12"}', profile=profile, owner="a", repository="r", issue_index=2) == 12
    for content in (b'{"redirect":"/a/r/issues/3#issuecomment-12"}', b'{"redirect":"https://evil.example/"}',
                    b'{"redirect":"/a/r/issues/2#issuecomment-12","ok":true}'):
        with pytest.raises(ForgejoError): form_destination(content, profile=profile, owner="a", repository="r", issue_index=2)
    target = {"profile": profile, "provider_user_id": 7, "content": "Exact body", "issue_api_path": "/api/v1/repos/a/r/issues/2"}
    value = {"id": 12, "user": {"id": 7}, "body": "Exact body", "pull_request_url": "", "issue_url": "https://codeberg.org/api/v1/repos/a/r/issues/2"}
    assert exact_readback(value, target, 12) == value
    for key, altered in (("id", True), ("user", {"id": 8}), ("body", "Different"), ("pull_request_url", None), ("issue_url", "https://evil.example/")):
        with pytest.raises(ForgejoError): exact_readback({**value, key: altered}, target, 12)
