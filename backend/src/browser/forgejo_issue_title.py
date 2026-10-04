"""Closed Forgejo v15.0.9 editor contract; production activation is blocked.

This module owns provider representation, never Root, Goal or job authority.
Native orchestration supplies current canonical checks before every handoff.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from urllib.parse import quote_plus, urlsplit

CAPABILITY = "browser.forgejo-issue-title.v1"
JOB_KIND = "forgejo_issue_title_v1"
PROFILE = "seraph.forgejo.codeberg-title.v1"
ORIGIN = "https://codeberg.org"
PROVIDER_VERSION = "15.0.9"
MAX_CONTACTS = 64
MAX_ASSETS = 32
MAX_ASSET_BYTES = 4 * 1024 * 1024
MAX_ASSET_AGGREGATE = 16 * 1024 * 1024
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_TIMELINE = 20


class ForgejoError(ValueError):
    def __init__(self, reason, *, status_code=409):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def checked_title(value):
    if (type(value) is not str or not value or value != value.strip()
        or unicodedata.normalize("NFC", value) != value or len(value.encode()) > 245
        or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in value)
        or re.search(r"[@#!]|https?://|www\.", value, re.IGNORECASE)):
        raise ForgejoError("forgejo_title_invalid", status_code=422)
    return value


def checked_segment(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value):
        raise ForgejoError("forgejo_repository_segment_invalid", status_code=422)
    if value in {".", ".."}:
        raise ForgejoError("forgejo_repository_segment_invalid", status_code=422)
    return value


def positive_id(value):
    if type(value) is not int or not 0 < value <= 2**63-1:
        raise ForgejoError("forgejo_numeric_identity_invalid", status_code=422)
    return value


@dataclass(frozen=True)
class TitleTarget:
    owner: str
    repository: str
    repository_id: int
    issue_id: int
    issue_index: int
    provider_user_id: int
    provider_login: str
    old_title: str
    new_title: str
    updated_at: str
    timeline_digest: str

    def __post_init__(self):
        for value in (self.owner, self.repository, self.provider_login): checked_segment(value)
        for value in (self.repository_id, self.issue_id, self.issue_index, self.provider_user_id):
            positive_id(value)
        checked_title(self.old_title); checked_title(self.new_title)
        if not re.fullmatch(r"[0-9a-f]{64}", self.timeline_digest):
            raise ForgejoError("forgejo_timeline_binding_invalid", status_code=422)
        if type(self.updated_at) is not str or not self.updated_at or len(self.updated_at) > 64:
            raise ForgejoError("forgejo_issue_revision_invalid", status_code=422)

    @property
    def page_path(self):
        return f"/{self.owner}/{self.repository}/issues/{self.issue_index}"

    @property
    def api_path(self):
        return f"/api/v1/repos/{self.owner}/{self.repository}/issues/{self.issue_index}"

    @property
    def title_body(self):
        # Exact URLSearchParams encoding used by the original upstream editor.
        # WHATWG form encoding leaves '*' literal and percent-encodes '~'.
        return ("title=" + quote_plus(self.new_title, safe="*").replace("~", "%7E")).encode("ascii")


def require_browser_submission(target, *, url, method, body, headers,
                               main_frame_matches, frame_url, page_url):
    lowered = {key.lower(): value for key, value in headers.items()}
    document = ORIGIN + target.page_path
    if (main_frame_matches is not True or frame_url != document or page_url != document
        or url != document + "/title" or method != "POST"
        or body != target.title_body
        or lowered.get("origin") != ORIGIN
        or ("sec-fetch-site" in lowered and lowered["sec-fetch-site"] != "same-origin")
        or lowered.get("referer") != document
        or any(name in lowered for name in ("authorization", "cookie", "proxy-authorization"))
        or lowered.get("content-type", "").lower() != "application/x-www-form-urlencoded;charset=utf-8"):
        raise ForgejoError("forgejo_browser_submission_changed")


def require_issue_identity(value, target, *, title):
    repository = value.get("repository") if isinstance(value, dict) else None
    if (not isinstance(repository, dict) or value.get("id") != target.issue_id
        or type(value.get("id")) is not int or value.get("number") != target.issue_index
        or type(value.get("number")) is not int or value.get("pull_request") is not None
        or repository.get("id") != target.repository_id or type(repository.get("id")) is not int
        or repository.get("full_name") != target.owner + "/" + target.repository
        or value.get("title") != title):
        raise ForgejoError("forgejo_destination_identity_changed")


def bounded_timeline(value):
    if type(value) is not list or len(value) > MAX_TIMELINE:
        raise ForgejoError("forgejo_complete_timeline_unavailable")
    seen = set()
    for item in value:
        if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0:
            raise ForgejoError("forgejo_timeline_identity_invalid")
        if item["id"] in seen: raise ForgejoError("forgejo_timeline_identity_invalid")
        seen.add(item["id"])
    return value


def timeline_response(content, headers):
    """Pinned handler's complete bounded visible page, including nil slice.

    v15.0.9 returns JSON null for its empty nil apiComments slice and sets
    X-Total-Count to the returned visible count. Never infer emptiness from
    missing/invalid bytes, errors, pagination or a caller-supplied count.
    """
    if type(content) is not bytes or len(content) > MAX_DOCUMENT_BYTES:
        raise ForgejoError("forgejo_complete_timeline_unavailable")
    try: value = json.loads(content)
    except (ValueError, UnicodeError):
        raise ForgejoError("forgejo_complete_timeline_unavailable") from None
    if headers.get("link"):
        raise ForgejoError("forgejo_complete_timeline_unavailable")
    count = headers.get("x-total-count")
    if value is None and count == "0": value = []
    if type(value) is not list or count != str(len(value)):
        raise ForgejoError("forgejo_complete_timeline_unavailable")
    return bounded_timeline(value)


def checked_assets(manifest):
    if type(manifest) is not dict or not manifest or len(manifest) > MAX_ASSETS:
        raise ForgejoError("forgejo_asset_inventory_invalid")
    total = 0
    for path, entry in manifest.items():
        parsed = urlsplit(path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/assets/")
            or ".." in parsed.path.split("/") or type(entry) is not dict
            or set(entry) != {"sha256", "bytes"} or type(entry["bytes"]) is not int
            or not 0 < entry["bytes"] <= MAX_ASSET_BYTES
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ForgejoError("forgejo_asset_inventory_invalid")
        total += entry["bytes"]
    if total > MAX_ASSET_AGGREGATE: raise ForgejoError("forgejo_asset_inventory_bound")
    return digest(manifest)
