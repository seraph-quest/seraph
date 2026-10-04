"""Closed English bootstrap from signed Forgejo v15.0.9, never executed here.

Pin executable tokens separately from the one literal participant list. That
list permits only the same original provider login and fixed-origin avatars.
The original provider HTML/JS is returned unchanged after validation.
"""
from html.parser import HTMLParser
import re

from src.browser.forgejo_issue_title import ForgejoError, digest, checked_segment

VERSION = "15.0.9~gitea-1.22.0"
BOOTSTRAP_SHA256 = "4b2b05dd5ce94f525c99807850dd681faf376c014dbbbb86d39e4202dda6a834"
MONOSPACE = """if (localStorage?.getItem('markdown-editor-monospace') === 'true') {
 document.querySelector('.markdown-text-editor').classList.add('tw-font-mono');
}"""
ASSET_ERROR = "alert('Failed to load asset files from {path}. Please make sure the asset files can be accessed.'.replace('{path}', this.src))"

TOKEN = re.compile(r"\s+|(?:'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")|"
                   r"[A-Za-z_$][A-Za-z0-9_$]*|[0-9]+|===|\?\.|\|\||[{}\[\]().,:;=+?<>!|-]")


def tokens(value):
    if not isinstance(value, str) or len(value.encode()) > 16384:
        raise ForgejoError("forgejo_bootstrap_bound")
    result, at = [], 0
    while at < len(value):
        match = TOKEN.match(value, at)
        if match is None: raise ForgejoError("forgejo_bootstrap_token_changed")
        item = match.group()
        if not item.isspace(): result.append(item)
        at = match.end()
    return result


def bootstrap_digest(value, login):
    checked_segment(login)
    items = tokens(value)
    prefix = tokens("mentionValues: Array.from(new Map([")
    starts = [i for i in range(len(items)) if items[i:i+len(prefix)] == prefix]
    if len(starts) != 1: raise ForgejoError("forgejo_bootstrap_mentions_changed")
    start = starts[0] + len(prefix)
    cursor, count, avatars = start, 0, set()
    while cursor < len(items) and items[cursor] != "]":
        # Pinned source emits five literal fields in this exact order. No
        # expression/function, other identity, full name or arbitrary origin.
        row = tokens(f"['{login}', {{key:'{login} ',value:'{login}',name:'{login}',fullname:'',avatar:'PLACEHOLDER'}}],")
        candidate = items[cursor:cursor+len(row)]
        avatar_at = row.index("'PLACEHOLDER'")
        if len(candidate) != len(row): raise ForgejoError("forgejo_bootstrap_mentions_changed")
        avatar = candidate[avatar_at]
        if not re.fullmatch(r"'https:\\/\\/codeberg\.org\\/avatars\\/[0-9a-f]{32}'", avatar):
            raise ForgejoError("forgejo_bootstrap_avatar_changed")
        avatars.add(avatar); candidate[avatar_at] = "'PLACEHOLDER'"
        if candidate != row: raise ForgejoError("forgejo_bootstrap_mentions_changed")
        count += 1; cursor += len(row)
        if count > 20: raise ForgejoError("forgejo_bootstrap_mentions_bound")
    if not 1 <= count <= 20 or len(avatars) != 1:
        raise ForgejoError("forgejo_bootstrap_mentions_changed")
    return digest(items[:start] + ["SOURCE_PINNED_LITERAL_MENTIONS"] + items[cursor:])


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts, self.active, self.handlers = [], None, []

    def handle_starttag(self, tag, attributes):
        if len({name for name, _ in attributes}) != len(attributes):
            raise ForgejoError("forgejo_document_duplicate_attribute")
        fields = dict(attributes)
        for name, value in attributes:
            if name.startswith("on"):
                self.handlers.append((tag, name, value))
        if tag == "script":
            if self.active is not None: raise ForgejoError("forgejo_nested_script_changed")
            self.active = {"attributes": fields, "text": ""}

    def handle_data(self, value):
        if self.active is not None: self.active["text"] += value

    def handle_endtag(self, tag):
        if tag == "script" and self.active is not None:
            self.scripts.append(self.active); self.active = None


def validate_document(content, login):
    if type(content) is not bytes or len(content) > 524288:
        raise ForgejoError("forgejo_document_bound")
    try:
        document = _Document(); document.feed(content.decode("utf-8", errors="strict")); document.close()
    except (UnicodeError, ValueError):
        raise ForgejoError("forgejo_document_parse_changed") from None
    scripts = document.scripts
    if document.active is not None or len(scripts) != 5:
        raise ForgejoError("forgejo_inline_script_inventory_changed")
    expected = [{}, {"src": f"/assets/js/webcomponents.js?v={VERSION}"}, {}, {},
                {"src": f"/assets/js/index.js?v={VERSION}", "onerror": ASSET_ERROR}]
    if ([item["attributes"] for item in scripts] != expected
        or document.handlers != [("script", "onerror", ASSET_ERROR)]
        or bootstrap_digest(scripts[0]["text"], login) != BOOTSTRAP_SHA256
        or any(tokens(scripts[i]["text"]) != tokens(MONOSPACE) for i in (2, 3))
        or any(scripts[i]["text"].strip() for i in (1, 4))):
        raise ForgejoError("forgejo_signed_bootstrap_or_layout_changed")
    return {"bootstrap_sha256": BOOTSTRAP_SHA256, "script_count": len(scripts),
            "locale": "en-US", "literal_participants_only": True}
