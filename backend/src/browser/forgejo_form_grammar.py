"""Finite signed Forgejo source grammar, distinct from native HTML5 ownership."""
from __future__ import annotations

from functools import lru_cache
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from src.browser.forgejo_bootstrap import (
    ASSET_ERROR, BOOTSTRAP_SHA256, MONOSPACE, VERSION, _Document,
    bootstrap_digest, tokens,
)
from src.browser.forgejo_issue_title import ForgejoError, checked_segment, digest, positive_id

CREATE = "forgejo.issue-create.v1"
COMMENT = "forgejo.issue-comment.v1"
CREATE_SELECTOR = ":scope > .issue-content-left > .ui.comments > .comment > .ui.segment.content > .text.right > button.ui.primary"
COMMENT_SELECTOR = ":scope > .field.footer > .button-sequence > button.primary"
VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())

# This is fixed trusted read-only introspection, never a caller script. Provider
# JavaScript is disabled. Template contents remain inert and are never cloned.
NATIVE_SNAPSHOT = """(form, selector) => {
    const attributes = e => Array.from(e.attributes).map(a => [a.name, a.value]);
    const identity = e => e === null ? null :
        ({tag:e.tagName, id:e.id, attributes:attributes(e)});
    const ancestors = e => {
        const result = [];
        for (let parent=e.parentElement; parent; parent=parent.parentElement)
            result.push(identity(parent));
        return result;
    };
    const node = e => ({...identity(e), name:e.name, type:e.type,
        value:e.value, disabled:e.disabled, owner:identity(e.form),
        ancestors:ancestors(e), text:e.tagName === 'BUTTON' ? e.textContent : null});
    return {action:form.action, method:form.method, form:identity(form),
        controls:Array.from(form.elements).map(node),
        successful:Array.from(new FormData(form).entries()),
        ordinary_submitters:Array.from(form.querySelectorAll(selector)).map(node),
        forms:Array.from(document.forms).map(identity),
        active_scripts:Array.from(document.querySelectorAll('script')).map(e =>
            ({attributes:attributes(e),text:e.textContent})),
        redirect_controls:Array.from(document.querySelectorAll(
            '[name="redirect_after_creation"]')).map(node)};
}"""


def typed_slots(*, owner, repository, username, provider_user_id, avatar_url,
                profile, issue_index=None, issue_title=None):
    checked_segment(owner); checked_segment(repository); checked_segment(username)
    positive_id(provider_user_id)
    if owner != username or profile not in (CREATE, COMMENT):
        raise ForgejoError("forgejo_selected_owned_repository_required")
    if (type(avatar_url) is not str or
        re.fullmatch(r"https://codeberg\.org/avatars/[0-9a-f]{32}", avatar_url) is None):
        raise ForgejoError("forgejo_original_avatar_changed")
    repository_path = f"/{owner}/{repository}"
    result = {"repository_path": repository_path, "username": username,
              "provider_user_id": str(provider_user_id), "avatar_path": avatar_url[len("https://codeberg.org"):],
              "assignee_id": "assignee_" + str(provider_user_id)}
    if profile == COMMENT:
        positive_id(issue_index)
        if type(issue_title) is not str or not issue_title or len(issue_title.encode()) > 4096:
            raise ForgejoError("forgejo_original_issue_title_changed")
        result.update(issue_index=str(issue_index), issue_label=f"#{issue_index} - {issue_title}")
    elif issue_index is not None or issue_title is not None:
        raise ForgejoError("forgejo_original_form_identity_changed")
    return result


def render(value, slots):
    """Render only compiled, explicit expected-value slots; never normalize input."""
    if type(value) is dict:
        if set(value) == {"slot"}:
            if value["slot"] not in slots: raise ForgejoError("forgejo_form_slot_missing")
            return slots[value["slot"]]
        if set(value) == {"parts"}:
            return "".join(render(part, slots) for part in value["parts"])
        return {key: render(item, slots) for key, item in value.items()}
    if type(value) is list: return [render(item, slots) for item in value]
    return value


@lru_cache(maxsize=1)
def manifest():
    return json.loads(Path(__file__).with_name("forgejo_form_signatures_v15.json").read_bytes())


def signature_digest():
    return digest(Path(__file__).with_name("forgejo_form_signatures_v15.json").read_bytes())


class FixedSourceSignature(HTMLParser):
    """Lexical source event signature. It makes no native form-owner assertion."""
    def __init__(self, form_id):
        super().__init__(convert_charrefs=True)
        self.form_id = form_id
        self.stack, self.forms, self.selected, self.templates = [], [], [], []
        self.selected_depth, self.found, self.template = None, 0, None
        self.inert_markers, self.previous_data, self.pending_marker = [], "", None

    def event(self, value):
        if self.selected_depth is not None: self.selected.append(value)
        if self.template is not None: self.template["events"].append(value)

    def handle_starttag(self, tag, attrs):
        if len(attrs) != len({name for name, value in attrs}):
            raise ForgejoError("forgejo_form_duplicate_attribute")
        fields = dict(attrs)
        if "form" in fields:
            raise ForgejoError("forgejo_external_form_control")
        identity = {"tag": tag, "attributes": [list(item) for item in attrs]}
        # Signed 15.0.9 emits Go's literal %!s(<nil>) in its inactive empty
        # delete-branch dialog. Pin that one source defect, never generalize
        # malformed form nesting or infer native ownership from this lexer.
        if tag == "nil":
            if (self.form_id != "comment-form" or attrs or self.selected_depth is not None
                or self.template is not None or len(self.stack) < 2
                or self.stack[-1] != {"tag":"div","attributes":[["class","header"]]}
                or self.stack[-2] != {"tag":"div","attributes":[["class","ui g-modal-confirm delete modal"],["id","delete-branch"]]}
                or not self.previous_data.endswith('Delete branch "%!s(')
                or self.get_starttag_text() != "<nil>" or self.pending_marker is not None):
                raise ForgejoError("forgejo_source_structure_changed")
            self.pending_marker = {"ancestors": list(reversed(self.stack)), "prefix": self.previous_data}
            return
        if tag == "form":
            self.forms.append({"attributes": identity["attributes"], "ancestors": list(reversed(self.stack))})
            if fields.get("id") == self.form_id:
                self.found += 1
                if self.selected_depth is not None: raise ForgejoError("forgejo_exact_form_missing")
                self.selected_depth = len(self.stack)
        if tag == "template":
            if self.template is not None: raise ForgejoError("forgejo_template_changed")
            self.template = {"attributes": identity["attributes"], "ancestors": list(reversed(self.stack)), "events": []}
        self.event(["start", tag, identity["attributes"]])
        if tag not in VOID: self.stack.append(identity)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID: self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID: raise ForgejoError("forgejo_source_structure_changed")
        if not self.stack or self.stack[-1]["tag"] != tag:
            raise ForgejoError("forgejo_source_structure_changed")
        self.event(["end", tag])
        self.stack.pop()
        if self.selected_depth == len(self.stack): self.selected_depth = None
        if tag == "template":
            self.templates.append(self.template); self.template = None

    def handle_data(self, data):
        if self.pending_marker is not None:
            if not data.startswith(')"'): raise ForgejoError("forgejo_source_structure_changed")
            self.inert_markers.append({**self.pending_marker,"suffix":data})
            self.pending_marker = None
        self.previous_data = data
        self.event(["data", data])
    def handle_comment(self, data): self.event(["comment", data])
    def handle_decl(self, decl):
        if self.selected_depth is not None or self.template is not None:
            raise ForgejoError("forgejo_source_structure_changed")

    def snapshot(self):
        if self.stack or self.selected_depth is not None or self.template is not None or self.found != 1 or self.pending_marker is not None:
            raise ForgejoError("forgejo_exact_form_missing")
        return {"forms": self.forms, "selected": self.selected, "templates": self.templates,
                "inert_markers": self.inert_markers}


def source_snapshot(content, form_id):
    if type(content) is not bytes or len(content) > 524288:
        raise ForgejoError("forgejo_document_bound")
    try:
        source = FixedSourceSignature(form_id)
        source.feed(content.decode("utf-8", errors="strict")); source.close()
        return source.snapshot()
    except (UnicodeError, ValueError):
        raise ForgejoError("forgejo_source_structure_changed") from None


def validate_scripts(content, *, profile, username):
    """Exact lexical scripts, including the inert comment-editor script."""
    source = _Document(); source.feed(content.decode("utf-8", errors="strict")); source.close()
    count = 4 if profile == CREATE else 5 if profile == COMMENT else 0
    expected = [{}, {"src": f"/assets/js/webcomponents.js?v={VERSION}"}]
    expected += [{}] * (count - 3)
    expected += [{"src": f"/assets/js/index.js?v={VERSION}", "onerror": ASSET_ERROR}]
    scripts = source.scripts
    if (not count or source.active is not None or len(scripts) != count
        or [script["attributes"] for script in scripts] != expected
        or source.handlers != [("script", "onerror", ASSET_ERROR)]
        or bootstrap_digest(scripts[0]["text"], username) != BOOTSTRAP_SHA256
        or any(tokens(script["text"]) != tokens(MONOSPACE) for script in scripts[2:-1])
        or scripts[1]["text"].strip() or scripts[-1]["text"].strip()):
        raise ForgejoError("forgejo_form_script_inventory_changed")


def validate_source(content, *, profile, slots, form_id):
    validate_scripts(content, profile=profile, username=slots["username"])
    actual = source_snapshot(content, form_id)
    if actual != render(manifest()[profile]["source"], slots):
        raise ForgejoError("forgejo_fixed_source_signature_changed")
    return digest(actual)


def native_expected(profile, slots, *, title="", content=""):
    expected = render(manifest()[profile]["native"], slots)
    for control in expected["controls"]:
        if control["name"] == "title": control["value"] = title
        if control["name"] == "content": control["value"] = content.replace("\r\n", "\n")
    expected["successful"] = [[name, title if name == "title" else content.replace("\r\n", "\n") if name == "content" else value]
                              for name, value in expected["successful"]]
    return expected


def validate_native(actual, *, profile, slots, title="", content=""):
    # No broad inert-control whitelist or ownership inferred from source syntax.
    if type(actual) is not dict: raise ForgejoError("forgejo_native_signature_changed")
    active = actual.get("active_scripts")
    expected_attrs = [[], [["src", f"/assets/js/webcomponents.js?v={VERSION}"]], [],
                      [["src", f"/assets/js/index.js?v={VERSION}"], ["onerror", ASSET_ERROR]]]
    if (type(active) is not list or len(active) != 4
        or any(type(item) is not dict or set(item) != {"attributes","text"} for item in active)
        or [item["attributes"] for item in active] != expected_attrs
        or bootstrap_digest(active[0]["text"], slots["username"]) != BOOTSTRAP_SHA256
        or tokens(active[2]["text"]) != tokens(MONOSPACE)
        or active[1]["text"].strip() or active[3]["text"].strip()):
        raise ForgejoError("forgejo_native_script_inventory_changed")
    inventory = {key:value for key,value in actual.items() if key != "active_scripts"}
    if inventory != native_expected(profile, slots, title=title, content=content):
        raise ForgejoError("forgejo_native_signature_changed")
    return digest(actual)


def ordinary_selector(profile):
    if profile == CREATE: return CREATE_SELECTOR
    if profile == COMMENT: return COMMENT_SELECTOR
    raise ForgejoError("forgejo_reviewed_profile_required")


def validate_native_handoff(headers, *, page_path, frame_url, page_url):
    origin = "https://codeberg.org"
    if (frame_url != origin + page_path or page_url != origin + page_path
        or headers.get("origin") != origin or headers.get("referer") != origin + page_path
        or headers.get("sec-fetch-site") != "same-origin"
        or headers.get("content-type", "").lower() != "application/x-www-form-urlencoded"):
        raise ForgejoError("forgejo_exact_native_submission_changed")
