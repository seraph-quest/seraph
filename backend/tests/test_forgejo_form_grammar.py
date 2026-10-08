"""Closed grammar checks against retained actual signed source/native captures."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.browser.forgejo_form_grammar import (
    CREATE, COMMENT, typed_slots, validate_source, validate_native,
    validate_native_handoff, source_snapshot,
)
from src.browser.forgejo_forms import FormDocument
from src.browser.forgejo_issue_title import ForgejoError

FIXTURES = Path(__file__).parent / "fixtures"


def captured(profile):
    source = (FIXTURES / (profile + ".signed-v15.html")).read_bytes()
    native = json.loads((FIXTURES / (profile + ".native-v15.json")).read_bytes())
    slots = typed_slots(owner="seraph_fixture1013", repository="fixture1013", username="seraph_fixture1013",
        provider_user_id=1, avatar_url="https://codeberg.org/avatars/ff7782f43f676b71f37404d310262a8f",
        profile=profile, issue_index=1 if profile == COMMENT else None,
        issue_title="Ordinary fixture issue" if profile == COMMENT else None)
    form_id = "new-issue" if profile == CREATE else "comment-form"
    return source, native, slots, form_id


@pytest.mark.parametrize("profile", [CREATE, COMMENT])
def test_unmodified_signed_source_and_complete_actual_native_inventory(profile):
    source, native, slots, form_id = captured(profile)
    expected_sha = {CREATE: "2e821f54f48de8c3bdfd8e00e3581d026f7588ca9ea7e653c28f87aaa3e9455b",
                    COMMENT: "da67cf24ec8d085ce35d4d390f5c369fab5ee73847be1912d768a120c05244c0"}
    assert hashlib.sha256(source).hexdigest() == expected_sha[profile]
    assert validate_source(source, profile=profile, slots=slots, form_id=form_id)
    assert validate_native(native, profile=profile, slots=slots)
    assert len(native["controls"]) == (29 if profile == CREATE else 21)
    assert len(native["ordinary_submitters"]) == 1
    assert len(native["active_scripts"]) == 4
    lexical = source_snapshot(source, form_id)
    assert len(lexical["templates"]) == (0 if profile == CREATE else 1)
    if profile == CREATE:
        assert native["redirect_controls"][0]["owner"] is None
        assert [name for name, value in native["successful"]] == ["title","content","ref","edit_mode","search","label_ids","milestone_id","project_id","assignee_ids"]
    else:
        assert native["successful"] == [["content", ""]]
        assert lexical["templates"][0]["attributes"] == [["id", "issue-comment-editor-template"]]


def test_reviewed_control_sequence_comes_from_fixed_native_ownership_not_source_nesting():
    for profile in (CREATE, COMMENT):
        source, native, slots, form_id = captured(profile)
        document = FormDocument(form_id=form_id, action=dict(native["form"]["attributes"])["action"], profile=profile, slots=slots)
        document.feed(source.decode()); document.close()
        controls = document.reviewed_controls(title="Exact title", content="Exact body")
        assert dict(controls)["content"] == "Exact body"
        assert "redirect_after_creation" not in dict(controls)
        if profile == CREATE: assert "project_id" in dict(controls)


@pytest.mark.parametrize("kind", [
    "nested_stub", "extra_form", "association", "redirect_value", "redirect_movement",
    "missing_project", "duplicate_named", "unnamed", "disabled", "submit_override",
    "submit_ambiguity", "prefill", "file", "script", "handler", "nil_marker",
])
def test_create_source_drift_fails_closed(kind):
    source, _, slots, form_id = captured(CREATE)
    old, new = {
        "nested_stub": (b'issues//ref', b'issues/new'),
        "extra_form": (b'id="new-issue"', b'id="new-issue"><form></form'),
        "association": (b'name="search"', b'name="search" form="new-issue"'),
        "redirect_value": (b'name="redirect_after_creation" value=""', b'name="redirect_after_creation" value="project"'),
        "redirect_movement": (b'<input type="hidden" name="redirect_after_creation" value="">', b''),
        "missing_project": (b'<input id="project_id" name="project_id" type="hidden" value="">', b''),
        "duplicate_named": (b'name="label_ids"', b'name="label_ids" name="label_ids"'),
        "unnamed": (b'placeholder="Filter assignee"', b'placeholder="Filter assignee" data-surprise="true"'),
        "disabled": (b'name="table-header" value="Header" disabled', b'name="table-header" value="Header"'),
        "submit_override": (b'<button class="ui primary button">', b'<button class="ui primary button" formaction="/evil">'),
        "submit_ambiguity": (b'<button class="ui primary button">', b'<button class="ui primary button">Extra</button><button class="ui primary button">'),
        "prefill": (b'placeholder="Title" value=""', b'placeholder="Title" value="Prefilled"'),
        "file": (b'name="search"', b'name="search" type="file"'),
        "script": (b'</body>', b'<script>fetch("/evil")</script></body>'),
        "handler": (b'data-tab-for="markdown-writer"', b'data-tab-for="markdown-writer" onclick="evil()"'),
        "nil_marker": (b'Create issue', b'Create issue<nil>'),
    }[kind]
    assert old in source
    changed = source.replace(old, new, 1)
    if kind == "redirect_movement":
        changed = changed.replace(b'<input name="title"', b'<input type="hidden" name="redirect_after_creation" value=""><input name="title"', 1)
    with pytest.raises(ForgejoError): validate_source(changed, profile=CREATE, slots=slots, form_id=form_id)


@pytest.mark.parametrize("kind", ["template_id", "template_control", "template_script", "template_activation", "nil_marker"])
def test_inert_comment_template_full_source_shape_is_pinned(kind):
    source, _, slots, form_id = captured(COMMENT)
    at = source.index(b'<template id="issue-comment-editor-template">')
    before, template = source[:at], source[at:]
    old, new = {
        "template_id": (b'issue-comment-editor-template', b'other-template'),
        "template_control": (b'name="link-url"', b'name="link-url" form="comment-form"'),
        "template_script": (b"localStorage?.getItem", b"evil?.getItem"),
        "template_activation": (b'<template id="issue-comment-editor-template">', b'<div id="issue-comment-editor-template">'),
        "nil_marker": (b'Delete branch "%!s(<nil>)"', b'Delete branch "%!s(<nil class="extra">)"'),
    }[kind]
    assert old in template
    with pytest.raises(ForgejoError):
        validate_source(before + template.replace(old, new, 1), profile=COMMENT, slots=slots, form_id=form_id)


@pytest.mark.parametrize("profile", [CREATE, COMMENT])
@pytest.mark.parametrize("kind", ["owner", "ancestor", "type", "disabled", "order", "unnamed", "submitter", "script", "value"])
def test_complete_native_signature_denies_drift(profile, kind):
    _, original, slots, _ = captured(profile)
    actual = deepcopy(original)
    if kind == "owner": actual["controls"][0]["owner"] = None
    if kind == "ancestor": actual["controls"][0]["ancestors"][0]["attributes"].append(["class", "extra"])
    if kind == "type": actual["controls"][0]["type"] = "file"
    if kind == "disabled": actual["controls"][0]["disabled"] = True
    if kind == "order": actual["controls"][0], actual["controls"][1] = actual["controls"][1], actual["controls"][0]
    if kind == "unnamed": actual["controls"][2]["attributes"].append(["data-unreviewed", ""])
    if kind == "submitter": actual["ordinary_submitters"].append(actual["ordinary_submitters"][0])
    if kind == "script": actual["active_scripts"][2]["text"] += "; fetch('/evil')"
    if kind == "value": actual["successful"][0][1] = "Unapproved"
    with pytest.raises(ForgejoError): validate_native(actual, profile=profile, slots=slots)


def test_external_redirect_remains_unowned_and_never_successful():
    _, original, slots, _ = captured(CREATE)
    for kind in ("owner", "formdata", "missing_project"):
        actual = deepcopy(original)
        if kind == "owner": actual["redirect_controls"][0]["owner"] = actual["form"]
        if kind == "formdata": actual["successful"].append(["redirect_after_creation", ""])
        if kind == "missing_project":
            actual["controls"] = [node for node in actual["controls"] if node["name"] != "project_id"]
            actual["successful"] = [item for item in actual["successful"] if item[0] != "project_id"]
        with pytest.raises(ForgejoError): validate_native(actual, profile=CREATE, slots=slots)


@pytest.mark.parametrize("profile", [CREATE, COMMENT])
def test_only_approved_literal_value_slots_change_after_native_fill(profile):
    _, actual, slots, _ = captured(profile)
    title = "Reviewed title" if profile == CREATE else ""
    content = "First line\r\nSecond & literal"
    for node in actual["controls"]:
        if node["name"] == "title": node["value"] = title
        if node["name"] == "content": node["value"] = content.replace("\r\n", "\n")
    for item in actual["successful"]:
        if item[0] == "title": item[1] = title
        if item[0] == "content": item[1] = content.replace("\r\n", "\n")
    assert validate_native(actual, profile=profile, slots=slots, title=title, content=content)
    actual["controls"][0]["attributes"].append(["onclick","unreviewed()"])
    with pytest.raises(ForgejoError): validate_native(actual, profile=profile, slots=slots, title=title, content=content)


@pytest.mark.parametrize("change", [{"sec-fetch-site":None}, {"sec-fetch-site":"cross-site"},
    {"origin":"https://evil.example"}, {"referer":"https://codeberg.org/other"}])
def test_real_same_origin_fetch_metadata_is_mandatory_before_handoff(change):
    path = "/seraph_fixture1013/fixture1013/issues/new"
    headers = {"origin":"https://codeberg.org", "referer":"https://codeberg.org"+path,
               "sec-fetch-site":"same-origin", "content-type":"application/x-www-form-urlencoded"}
    validate_native_handoff(headers, page_path=path, frame_url=headers["referer"], page_url=headers["referer"])
    with pytest.raises(ForgejoError):
        validate_native_handoff({**headers, **change}, page_path=path, frame_url=headers["referer"], page_url=headers["referer"])


def test_current_numeric_issue_title_and_original_actor_slots_cannot_be_guessed():
    source, native, slots, form_id = captured(COMMENT)
    for changes in ({"issue_label":"#1 - Other issue"},
                    {"username":"other"}, {"repository_path":"/seraph_fixture1013/other"}):
        with pytest.raises(ForgejoError): validate_native(native, profile=COMMENT, slots={**slots, **changes})
    with pytest.raises(ForgejoError): validate_source(source, profile=COMMENT, slots={**slots,"issue_index":"2"}, form_id=form_id)
    for actor_id in (True,0,-1):
        with pytest.raises(ForgejoError):
            typed_slots(owner="a",repository="r",username="a",provider_user_id=actor_id,
                avatar_url="https://codeberg.org/avatars/"+"a"*32,profile=CREATE)
