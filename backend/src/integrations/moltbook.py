"""Fixed optional Moltbook routes; external prose never grants authority."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from urllib.parse import urlencode

from src.security.http_transport import request_pinned_https, _TransportLifecycleMarker

ORIGIN = "https://www.moltbook.com/api/v1"
CAPABILITY = "work.moltbook.v1"
JOB_KIND = "moltbook_v1"
MAX_RESPONSE = 65536
MAX_REQUEST = 12288
ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
COMMUNITY = re.compile(r"^[a-z0-9][a-z0-9-]{1,29}$")
KEY = re.compile(r"^[A-Za-z0-9_.-]{1,4096}$")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


class MoltbookError(ValueError):
    def __init__(self, code: str, *, status_code=409, retry_after=None):
        super().__init__(code)
        self.code, self.status_code, self.retry_after = code, status_code, retry_after


def identifier(value):
    if type(value) is not str or ID.fullmatch(value) is None:
        raise MoltbookError("moltbook_identifier_invalid", status_code=422)
    return value


def text(value, maximum, *, required=True):
    if type(value) is not str or len(value.encode("utf-8")) > maximum or (required and not value.strip()) or "\x00" in value:
        raise MoltbookError("moltbook_text_bound_invalid", status_code=422)
    return value


def route(operation, fields):
    """Closed route/body mapping, never caller-supplied method or URL."""
    if type(operation) is not str or type(fields) is not dict:
        raise MoltbookError("moltbook_operation_shape_invalid", status_code=422)
    f = dict(fields)
    if operation == "register":
        if set(f) != {"name", "description"} or type(f.get("name")) is not str or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,31}", f["name"]):
            raise MoltbookError("moltbook_registration_invalid", status_code=422)
        text(f["description"], 2048)
        return "POST", "/agents/register", f
    if operation in {"me", "status"} and not f:
        return "GET", "/agents/" + operation, None
    if operation == "community" and set(f) == {"community"}:
        if type(f["community"]) is not str or COMMUNITY.fullmatch(f["community"]) is None:
            raise MoltbookError("moltbook_community_invalid", status_code=422)
        return "GET", "/submolts/" + f["community"], None
    if operation in {"feed", "comments"}:
        allowed = {"sort", "limit", "cursor"} | ({"community"} if operation == "feed" else {"post_id"})
        if set(f) - allowed or not {"sort", "limit"} <= set(f):
            raise MoltbookError("moltbook_read_shape_invalid", status_code=422)
        sorts = {"new", "hot", "top", "rising"} if operation == "feed" else {"new", "best", "old"}
        if type(f["sort"]) is not str or f["sort"] not in sorts or type(f["limit"]) is not int or not 1 <= f["limit"] <= 10:
            raise MoltbookError("moltbook_page_bound_invalid", status_code=422)
        query = {"sort": f["sort"], "limit": f["limit"]}
        if "cursor" in f: query["cursor"] = text(f["cursor"], 512)
        if operation == "feed":
            if "community" in f:
                route("community", {"community": f["community"]})
                query["submolt"] = f["community"]
            path = "/posts"
        else:
            path = "/posts/" + identifier(f.get("post_id")) + "/comments"
        return "GET", path + "?" + urlencode(query), None
    if operation == "post" and set(f) == {"post_id"}:
        return "GET", "/posts/" + identifier(f["post_id"]), None
    if operation == "create_post" and set(f) == {"community", "title", "content"}:
        route("community", {"community": f["community"]})
        title = text(f["title"], 1200)
        if len(title) > 300: raise MoltbookError("moltbook_title_bound_invalid", status_code=422)
        body = {"submolt_name": f["community"], "title": title, "content": text(f["content"], 8192)}
        return "POST", "/posts", body
    if operation == "create_comment" and set(f) in ({"post_id", "content"}, {"post_id", "parent_id", "content"}):
        body = {"content": text(f["content"], 8192)}
        if "parent_id" in f: body["parent_id"] = identifier(f["parent_id"])
        return "POST", "/posts/" + identifier(f["post_id"]) + "/comments", body
    if operation == "verify" and set(f) == {"verification_code", "answer"}:
        code = text(f["verification_code"], 512)
        answer = text(f["answer"], 32)
        if not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,26})\.[0-9]{2}", answer):
            raise MoltbookError("moltbook_manual_answer_invalid", status_code=422)
        return "POST", "/verify", {"verification_code": code, "answer": answer}
    raise MoltbookError("moltbook_operation_not_allowed", status_code=422)


def parse_response(raw):
    if len(raw) > MAX_RESPONSE: raise MoltbookError("moltbook_response_too_large")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    def nonfinite(value): raise ValueError("nonfinite JSON")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
        count = 0
        def bound(item, depth):
            nonlocal count
            count += 1
            if depth > 8 or count > 256: raise ValueError("JSON envelope bound")
            if isinstance(item, dict):
                for key, child in item.items():
                    if len(key.encode()) > 256: raise ValueError("JSON key bound")
                    bound(child, depth + 1)
            elif isinstance(item, list):
                for child in item: bound(child, depth + 1)
        bound(value, 0)
        if not isinstance(value, dict): raise ValueError("JSON object required")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise MoltbookError("moltbook_response_schema_invalid") from None


def safe_content(item):
    if not isinstance(item, dict): raise MoltbookError("moltbook_content_schema_invalid")
    author = item.get("author")
    if not isinstance(author, dict): raise MoltbookError("moltbook_author_unproven")
    result = {"id": identifier(item.get("id")), "author_id": identifier(author.get("id")),
        "content": text(item.get("content"), 8192, required=False)}
    if "title" in item: result["title"] = text(item["title"], 1200, required=False)
    if "parent_id" in item and item["parent_id"] is not None: result["parent_id"] = identifier(item["parent_id"])
    community = item.get("submolt")
    if isinstance(community, dict): result["community"] = text(community.get("name"), 30)
    # An explicit pending/failed verification field can never become a visible
    # publication receipt merely because content creation returned HTTP 2xx.
    if item.get("verification_status") in {"pending", "failed"}:
        result["visibility"] = item["verification_status"]
    elif item.get("verification_status") == "verified": result["visibility"] = "verified"
    else: result["visibility"] = "observed"
    # No undocumented affirmative provider field is required. Preserve
    # explicit negative metadata, while public-list membership is proved
    # separately by the fixed final GET rather than inferred from verification.
    evidence = {}
    for name in ("hidden", "is_hidden", "removed", "is_removed", "deleted", "is_deleted", "is_private"):
        if name in item:
            if type(item[name]) is not bool: raise MoltbookError("moltbook_visibility_unconfirmed")
            evidence[name] = item[name]
    for name in ("visibility", "publication_status", "status"):
        if name in item: evidence[name] = text(item[name], 128, required=False)
    for name in ("deleted_at", "removed_at"):
        if name in item and item[name] is not None: evidence[name] = text(item[name], 128)
    if isinstance(community, dict) and "is_private" in community:
        if type(community["is_private"]) is not bool: raise MoltbookError("moltbook_visibility_unconfirmed")
        evidence["community_is_private"] = community["is_private"]
    result["visibility_evidence"] = evidence
    result["explicitly_hidden"] = (any(evidence.get(name) is True for name in ("hidden", "is_hidden", "removed", "is_removed", "deleted", "is_deleted", "is_private", "community_is_private"))
        or any(evidence.get(name) in {"hidden", "unlisted", "private", "removed", "deleted", "restricted"} for name in ("visibility", "publication_status", "status"))
        or any(name in evidence for name in ("deleted_at", "removed_at")))
    return result


def safe_comments(value):
    roots = value.get("comments")
    if not isinstance(roots, list) or len(roots) > 10: raise MoltbookError("moltbook_comment_roots_bound")
    count = 0
    def visit(item, depth):
        nonlocal count
        count += 1
        if count > 40 or depth > 4: raise MoltbookError("moltbook_comment_tree_bound")
        normalized = safe_content(item)
        children = item.get("replies", [])
        if not isinstance(children, list): raise MoltbookError("moltbook_comment_tree_invalid")
        normalized["replies"] = [visit(child, depth + 1) for child in children]
        return normalized
    return [visit(item, 1) for item in roots]


class MoltbookAdapter:
    def __init__(self, *, transport=None, resolver=None):
        # Explicit constructor seams only; no environment/config network hook.
        self.transport, self.resolver = transport, resolver
        self.marker = _TransportLifecycleMarker()
        self.read_response_receipt = None

    async def call(self, operation, fields, *, key=None, deadline, before_contact=None):
        self.read_response_receipt = None
        method, path, body = route(operation, fields)
        if body is not None and len(canonical(body)) > (4096 if operation == "register" else MAX_REQUEST):
            raise MoltbookError("moltbook_request_too_large", status_code=422)
        headers = {"Accept": "application/json"}
        if operation != "register":
            if type(key) is not str or KEY.fullmatch(key) is None: raise MoltbookError("moltbook_credential_invalid")
            headers["Authorization"] = "Bearer " + key
        # SQLite stores naive UTC. Never interpret it using the host timezone.
        absolute = deadline.replace(tzinfo=deadline.tzinfo or timezone.utc).astimezone(timezone.utc)
        remaining = (absolute - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0: raise MoltbookError("moltbook_original_deadline_expired")
        kwargs = {"method": method, "json_body": body, "headers": headers,
            "timeout_seconds": min(15, remaining), "max_bytes": MAX_RESPONSE,
            "_lifecycle_marker": self.marker, "authority_check": before_contact}
        if self.transport is not None: kwargs["transport"] = self.transport
        if self.resolver is not None: kwargs["resolver"] = self.resolver
        async with asyncio.timeout(remaining):
            response = await request_pinned_https(ORIGIN + path, **kwargs)
        # Only a returned bounded response after awaited transport closure is
        # definitive read evidence. Exceptions during transfer leave no receipt.
        if method == "GET" and self.marker.snapshot()["status"] == "verified":
            self.read_response_receipt = {"operation": operation, "http_status": response.status_code,
                "response_digest": digest(response.content)}
        if response.status_code == 429:
            retry = response.headers.get("retry-after", "")
            seconds = (min(int(retry), 172800) if len(retry) <= 6 else 172800) if retry.isascii() and retry.isdecimal() else 60
            raise MoltbookError("moltbook_rate_limited", retry_after=seconds)
        if not 200 <= response.status_code < 300:
            raise MoltbookError("moltbook_provider_rejected")
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise MoltbookError("moltbook_response_not_json")
        return parse_response(response.content)
