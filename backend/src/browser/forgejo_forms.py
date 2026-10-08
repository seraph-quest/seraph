"""Closed reviewed Forgejo v15.0.9 form grammar; no activation on import."""
from __future__ import annotations

import json
import re
from urllib.parse import quote_plus

from src.browser.forgejo_issue_title import ForgejoError, PROVIDER_VERSION, canonical, digest, checked_title, positive_id
from src.browser.forgejo_form_grammar import signature_digest

FORM_PROFILES = ("forgejo.issue-comment.v1", "forgejo.issue-create.v1")
FORM_CAPABILITY = "browser.forgejo-forms.v1"
FORM_JOB_KIND = "forgejo_form_transaction_v1"
# Official pinned source inventories reviewed on 2026-10-08. These are source
# compatibility receipts, never deployed-account acceptance or a write grant.
SOURCE_MANIFEST_DIGEST = digest({
    "original": "fd12e902c740171da6181d4b5b04bf97025632ce6b052e4bc4db80dd49d16a73",
    "supplement": "f4aadbfd13e32a61150677babfa0dbae0b370383ce4dbdb3f64b072c01321114",
    "fixed_form_grammar": signature_digest(),
})
MAX_FORM_BODY = 16384
MAX_FORM_CONTACTS = 6


def profile_document(profile_ids):
    if (type(profile_ids) is not list or len(profile_ids) > 2
        or any(type(value) is not str or value not in FORM_PROFILES for value in profile_ids)
        or profile_ids != sorted(set(profile_ids))):
        raise ForgejoError("forgejo_reviewed_profile_set_invalid", status_code=422)
    value = {"schema_version": 1, "profile_ids": profile_ids,
             "provider_version": PROVIDER_VERSION, "source_manifest_digest": SOURCE_MANIFEST_DIGEST}
    raw = canonical(value).decode()
    if len(raw.encode()) > 1024:
        raise ForgejoError("forgejo_reviewed_profile_set_bound", status_code=422)
    return raw


def active_profiles(raw):
    try:
        if type(raw) is not str or len(raw.encode()) > 1024: raise ValueError()
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != {"schema_version", "profile_ids", "provider_version", "source_manifest_digest"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["provider_version"] != PROVIDER_VERSION
            or value["source_manifest_digest"] != SOURCE_MANIFEST_DIGEST):
            raise ValueError()
        if raw != profile_document(value["profile_ids"]): raise ValueError()
        return value["profile_ids"]
    except (ValueError, TypeError, KeyError):
        raise ForgejoError("forgejo_reviewed_profile_manifest_changed") from None


EMPTY_FORM_PROFILES = profile_document([])


def clear_profiles(row):
    # Reauthentication/credential withdrawal advances the generation even if
    # already empty, making all older exact transaction pins unusable.
    row.reviewed_form_profiles_json = EMPTY_FORM_PROFILES
    row.form_profiles_revision += 1


def encoded_controls(controls):
    """WHATWG URL-encoded successful controls, in source/native DOM order."""
    def encode(value):
        return quote_plus(value, safe="*").replace("~", "%7E")
    body = "&".join(encode(name) + "=" + encode(value) for name, value in controls).encode("ascii")
    if len(body) > MAX_FORM_BODY:
        raise ForgejoError("forgejo_complete_form_body_bound", status_code=422)
    return body


def checked_content(value):
    if (type(value) is not str or not value.strip() or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ForgejoError("forgejo_form_content_invalid", status_code=422)
    # Native textarea submissions normalize line breaks. Approval is for the
    # resulting exact literal body, so noncanonical input is visibly rejected.
    value=value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    if len(value.encode())>MAX_FORM_BODY:raise ForgejoError("forgejo_form_content_invalid", status_code=422)
    return value


def strict_json(content):
    def object_pairs(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError("duplicate JSON property")
            value[key]=item
        return value
    return json.loads(content,object_pairs_hook=object_pairs)


def form_destination(content, *, profile, owner, repository, issue_index=None):
    try:
        value = strict_json(content)
        if type(value) is not dict or set(value) != {"redirect"} or type(value["redirect"]) is not str:
            raise ValueError()
        prefix = f"/{owner}/{repository}/issues/"
        pattern = re.escape(prefix) + (r"([1-9][0-9]*)" if profile == "forgejo.issue-create.v1"
            else re.escape(str(issue_index)) + r"#issuecomment-([1-9][0-9]*)")
        match = re.fullmatch(pattern, value["redirect"])
        if match is None or int(match[1]) > 2**63 - 1: raise ValueError()
        return int(match[1])
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ForgejoError("forgejo_form_destination_unknown") from None


class FormDocument:
    """Exact lexical signature only; native ownership is validated separately."""
    def __init__(self, *, form_id, action, profile, slots):
        from src.browser.forgejo_form_grammar import CREATE, COMMENT, render, manifest
        if profile not in (CREATE, COMMENT): raise ForgejoError("forgejo_reviewed_profile_required")
        expected_form = render(manifest()[profile]["native"]["form"], slots)
        if (form_id != expected_form["id"] or action != dict(expected_form["attributes"])["action"]):
            raise ForgejoError("forgejo_native_form_changed")
        self.form_id, self.profile, self.slots = form_id, profile, slots
        self.form_attributes = dict(expected_form["attributes"])
        self.source, self.source_signature = "", None

    def feed(self, source):
        if type(source) is not str or len((self.source + source).encode()) > 524288:
            raise ForgejoError("forgejo_document_bound")
        self.source += source

    def close(self):
        from src.browser.forgejo_form_grammar import validate_source
        self.source_signature = validate_source(self.source.encode(), profile=self.profile,
            slots=self.slots, form_id=self.form_id)

    def reviewed_controls(self, *, title, content):
        from src.browser.forgejo_form_grammar import CREATE, manifest, render
        if self.source_signature is None: raise ForgejoError("forgejo_exact_form_missing")
        ordinary = render(manifest()[self.profile]["native"]["successful"], self.slots)
        result = [(name, checked_title(title) if name == "title" and self.profile == CREATE
                   else checked_content(content) if name == "content" else value)
                  for name, value in ordinary]
        encoded_controls(result)
        return result


def exact_readback(value, target, exact_id):
    """Typed numeric identity and full literal fields, never body-only attribution."""
    positive_id(exact_id)
    if type(value) is not dict: raise ForgejoError("forgejo_form_readback_unknown")
    actor = value.get("user")
    if (type(actor) is not dict or type(actor.get("id")) is not int
        or actor["id"] != target["provider_user_id"] or type(value.get("id")) is not int
        or value["id"] <= 0 or value.get("body") != target["content"]):
        raise ForgejoError("forgejo_form_readback_unknown")
    if target["profile"] == "forgejo.issue-create.v1":
        repository = value.get("repository")
        if (type(value.get("number")) is not int or value["number"] != exact_id
            or value.get("title") != target["title"] or value.get("pull_request") is not None
            or type(repository) is not dict or type(repository.get("id")) is not int
            or repository["id"] != target["repository_id"]
            or repository.get("full_name") != target["owner"] + "/" + target["repository"]):
            raise ForgejoError("forgejo_form_readback_unknown")
    elif (value["id"] != exact_id or value.get("pull_request_url") != ""
        or value.get("issue_url") != "https://codeberg.org" + target["issue_api_path"]):
        raise ForgejoError("forgejo_form_readback_unknown")
    return value
