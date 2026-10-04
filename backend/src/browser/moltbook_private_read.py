"""One fixed private Home document; provider instructions never become actions.

Production is deliberately blocked pending the separately authorized account,
identity-linkage and delivery-effect acceptance gate in ADR018. A constructor
transport is solely the existing server-internal local-test seam, never HTTP
input or environment configuration. The browser still really navigates the
guarded document and its actual source is independently compared with transport.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
import uuid

from config.settings import settings
from src.integrations.moltbook import MoltbookError, canonical, digest
from src.security.http_transport import (
    _TransportLifecycleMarker, default_resolver, request_pinned_https,
)
from src.security.site_policy import evaluate_site_access

CAPABILITY = "browser.moltbook-private-home.v1"
JOB_KIND = "moltbook_private_home_v1"
OPERATION = "private_home"
ORIGIN = "https://www.moltbook.com"
HOME_URL = ORIGIN + "/api/v1/home"
ME_URL = ORIGIN + "/api/v1/agents/me"
PROFILE = "seraph.moltbook.private-home.v1"
ARTIFACT_TYPE = "moltbook_private_browser_read"
MAX_RESPONSE = 65536


def policy_digest():
    return digest({"profile": PROFILE, "origin": ORIGIN, "paths": [ME_URL, HOME_URL],
        "allowlist": settings.browser_site_allowlist,
        "blocklist": settings.browser_site_blocklist,
        "max_contacts": 3, "home_contacts": 1, "bookkeeping": "one_due_delivery"})


def require_policy(expected):
    if policy_digest() != expected or not evaluate_site_access(HOME_URL).allowed:
        raise MoltbookError("moltbook_private_site_policy_changed")


def root_auth_digest(owner, root):
    captured = getattr(owner, "authenticated_token_hash", None)
    if not captured or captured != root.token_hash:
        raise MoltbookError("moltbook_private_authenticated_root_changed", status_code=403)
    # Persist a namespaced one-way binding, never the reusable request token
    # or its authentication lookup hash in public authority/checkpoint fields.
    return digest(["seraph.moltbook.private-root-auth.v1", captured])


def parse_document(raw: bytes):
    if len(raw) > MAX_RESPONSE:
        raise MoltbookError("moltbook_private_response_bound")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: raise ValueError("duplicate")
            value[key] = item
        return value
    def invalid(_): raise ValueError("nonfinite")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
        nodes = 0
        def walk(value, depth=0):
            nonlocal nodes
            nodes += 1
            if nodes > 1024 or depth > 8: raise ValueError("bound")
            if isinstance(value, dict):
                for key, item in value.items():
                    if len(key.encode()) > 256: raise ValueError("key")
                    walk(item, depth+1)
            elif isinstance(value, list):
                for item in value: walk(item, depth+1)
        walk(result)
        if not isinstance(result, dict): raise ValueError("shape")
        return result
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise MoltbookError("moltbook_private_document_invalid") from None


def field_text(value, maximum):
    if (type(value) is not str or not value or len(value.encode()) > maximum
        or any(ord(char) < 32 and char not in "\n\t" for char in value)):
        raise MoltbookError("moltbook_private_field_invalid")
    return value


def field_integer(value, *, signed=False):
    if type(value) is not int or not (-2147483648 if signed else 0) <= value <= 2147483647:
        raise MoltbookError("moltbook_private_count_invalid")
    return value


def agent_identity(value):
    agent = value.get("agent")
    if not isinstance(agent, dict): raise MoltbookError("moltbook_private_identity_invalid")
    try:
        identifier = str(uuid.UUID(agent["id"]))
        if identifier != agent["id"]: raise ValueError()
        return identifier, field_text(agent["name"], 128)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MoltbookError("moltbook_private_identity_invalid") from None


def cited_projection(home, *, account_name, raw_digest, dom_digest, observed_at):
    account, activity = home.get("your_account"), home.get("activity_on_your_posts")
    if not isinstance(account, dict) or type(activity) is not list or len(activity) > 10:
        raise MoltbookError("moltbook_private_home_schema_invalid")
    if account.get("name") != account_name:
        raise MoltbookError("moltbook_private_home_identity_changed")
    citations = []
    def cite(pointer, value):
        citations.append({"pointer": pointer, "value_sha256": digest(value), "source_id": "h"})
        return value
    try:
        selected = {"your_account": {
            "name": cite("/your_account/name", field_text(account["name"],128)),
            "karma": cite("/your_account/karma", field_integer(account["karma"],signed=True)),
            "unread_notification_count": cite("/your_account/unread_notification_count",
                field_integer(account["unread_notification_count"]))}, "activity_on_your_posts": []}
        for index, item in enumerate(activity):
            if not isinstance(item, dict): raise MoltbookError("moltbook_private_home_schema_invalid")
            pointer = f"/activity_on_your_posts/{index}"
            row = {}
            for key, maximum in (("post_title",512),("submolt_name",30),("preview",2048)):
                row[key] = cite(pointer+"/"+key, field_text(item[key],maximum))
            post_id = str(uuid.UUID(item["post_id"]))
            if post_id != item["post_id"]: raise ValueError()
            row["post_id"] = cite(pointer+"/post_id", post_id)
            row["new_notification_count"] = cite(pointer+"/new_notification_count",
                field_integer(item["new_notification_count"]))
            timestamp = field_text(item["latest_at"],32)
            if datetime.fromisoformat(timestamp.replace("Z","+00:00")).tzinfo is None: raise ValueError()
            row["latest_at"] = cite(pointer+"/latest_at", timestamp)
            commenters = item["latest_commenters"]
            if type(commenters) is not list or len(commenters) > 4: raise ValueError()
            row["latest_commenters"] = [cite(pointer+f"/latest_commenters/{i}",field_text(v,128))
                for i,v in enumerate(commenters)]
            selected["activity_on_your_posts"].append(row)
        if len(canonical(citations)) > 16384 or len(canonical(selected)) > 65536: raise ValueError()
        return selected, citations
    except (KeyError, ValueError, TypeError, AttributeError):
        raise MoltbookError("moltbook_private_home_schema_invalid") from None


def validate_projection(payload):
    """Exact one-source membership and every literal value citation."""
    try:
        source = payload["source"]
        if (set(source) != {"id","observed_at","response_sha256","browser_source_sha256",
            "browser_dom_sha256","canonical_json_sha256","equal_transport_and_browser_source"}
            or source["id"] != "h" or source["equal_transport_and_browser_source"] is not True):
            raise ValueError()
        for key in ("response_sha256","browser_source_sha256","browser_dom_sha256","canonical_json_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}",source[key]): raise ValueError()
        if datetime.fromisoformat(source["observed_at"]).tzinfo is None: raise ValueError()
        data,citations = cited_projection(payload["data"],account_name=payload["data"]["your_account"]["name"],
            raw_digest=source["response_sha256"],dom_digest=source["browser_source_sha256"],observed_at=source["observed_at"])
        if data != payload["data"] or citations != payload["citations"]:
            raise ValueError()
        if len(canonical({"source":source,"citations":citations})) > 16384: raise ValueError()
    except (KeyError,ValueError,TypeError,AttributeError):
        raise MoltbookError("moltbook_private_citation_source_invalid") from None


class MoltbookPrivateBrowserReader:
    def __init__(self, *, local_transport=None, resolver=default_resolver):
        self.local_transport = local_transport
        self.resolver = resolver

    @property
    def production_blocked(self):
        return self.local_transport is None

    def require_available(self):
        if self.production_blocked:
            raise MoltbookError("moltbook_private_production_acceptance_required")

    async def read(self, *, credential, expected_id, expected_name, deadline,
                   check_current, contact, observe, cleanup_observer):
        self.require_available()
        from src.browser.task_runner import BrowserTaskRunner, _BrowserLaunchResources
        runner = BrowserTaskRunner(workspace_root=settings.workspace_dir)
        resources = _BrowserLaunchResources()
        marker = _TransportLifecycleMarker()
        failure = None
        home_response = None
        navigations = 0
        route_failure = None
        output = None

        async def fetch(operation, url):
            await check_current()
            remaining = (deadline-datetime.now(timezone.utc)).total_seconds()-10
            if remaining <= 0: raise MoltbookError("moltbook_private_deadline")
            response = await request_pinned_https(url, headers={"Authorization": "Bearer "+credential,
                "Accept": "application/json"}, resolver=self.resolver, transport=self.local_transport,
                timeout_seconds=min(10,remaining), max_bytes=MAX_RESPONSE, _lifecycle_marker=marker,
                authority_check=lambda: contact(operation), handoff_check=check_current)
            # A complete actual transfer is audit even if current authority
            # changes before projection/adoption. No raw response is persisted.
            await observe(operation, response.status_code, digest(response.content), marker.snapshot())
            await check_current()
            if ("set-cookie" in response.headers or response.status_code != 200
                or response.headers.get("content-type","").split(";",1)[0].strip().lower() != "application/json"):
                raise MoltbookError("moltbook_private_response_not_allowed")
            parsed = parse_document(response.content)
            if credential.encode() in response.content or credential.encode() in canonical(parsed):
                raise MoltbookError("moltbook_private_secret_echo_blocked")
            return response

        try:
            before = await fetch("me_before", ME_URL)
            if agent_identity(parse_document(before.content)) != (expected_id, expected_name):
                raise MoltbookError("moltbook_private_agent_identity_changed")
            await check_current()
            remaining = (deadline-datetime.now(timezone.utc)).total_seconds()-10
            if remaining <= 0: raise MoltbookError("moltbook_private_deadline")
            async with asyncio.timeout(remaining):
                session = await runner._launch_session(resources)
                context = session.context
                page = await context.new_page()
                async def route_guard(route, request):
                    nonlocal home_response, navigations, route_failure
                    try:
                        headers = await request.all_headers()
                        if (request.url != HOME_URL or request.method != "GET"
                            or request.resource_type != "document" or request.frame != page.main_frame
                            or request.redirected_from is not None or navigations != 0
                            or any(k.lower() in {"authorization","cookie","proxy-authorization"} for k in headers)):
                            raise MoltbookError("moltbook_private_browser_request_blocked")
                        navigations += 1
                        home_response = await fetch("home", HOME_URL)
                        await check_current()
                        await route.fulfill(status=200, headers={"content-type":"application/json",
                            "cache-control":"no-store", "x-content-type-options":"nosniff",
                            "content-security-policy":"default-src 'none'; frame-ancestors 'none'"},
                            body=home_response.content)
                    except BaseException as exc:
                        route_failure = exc
                        await route.abort()
                await context.route("**/*", route_guard)
                def deny_page(new_page):
                    if new_page != page: asyncio.create_task(new_page.close())
                context.on("page",deny_page)
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=min(remaining,30)*1000)
                if route_failure is not None: raise route_failure
                if home_response is None or navigations != 1 or page.url != HOME_URL:
                    raise MoltbookError("moltbook_private_document_unproven")
                await check_current()
                # Chrome renders JSON into a real PRE source node. Accept only
                # one node, then compare its independently parsed exact content.
                if await page.locator("body > pre").count() != 1:
                    raise MoltbookError("moltbook_private_browser_source_unrecognized")
                source = await page.locator("body > pre").text_content()
                source_bytes = (source or "").encode()
                if len(source_bytes) > MAX_RESPONSE or credential.encode() in source_bytes:
                    raise MoltbookError("moltbook_private_browser_source_bound")
                document = parse_document(source_bytes)
                if canonical(document) != canonical(parse_document(home_response.content)):
                    raise MoltbookError("moltbook_private_browser_source_changed")
                dom_html = await page.content()
                if credential in dom_html or len(dom_html.encode()) > 2*MAX_RESPONSE:
                    raise MoltbookError("moltbook_private_browser_dom_bound")
                await check_current()
                after = await fetch("me_after", ME_URL)
                if agent_identity(parse_document(after.content)) != (expected_id, expected_name):
                    raise MoltbookError("moltbook_private_agent_identity_changed")
                observed_at = datetime.now(timezone.utc).isoformat()
                data, citations = cited_projection(document, account_name=expected_name,
                    raw_digest=digest(home_response.content),dom_digest=digest(source_bytes),observed_at=observed_at)
                output = {"schema":"seraph.moltbook.private-browser-read.v1", "profile":PROFILE,
                    "data":data, "citations":citations, "source":{"id":"h","observed_at":observed_at,
                        "response_sha256":digest(home_response.content),
                        "browser_source_sha256":digest(source_bytes), "browser_dom_sha256":digest(dom_html.encode()),
                        "canonical_json_sha256":digest(document), "equal_transport_and_browser_source":True},
                    "no_learning":True, "trust":"private_external_untrusted_literal",
                    "effects":"one_home_read_may_deliver_due_briefing_and_access_bookkeeping",
                    "discarded":"role_instructions_unrelated_activity_and_suggested_actions",
                    "production_acceptance":"unverified_local_test_only"}
                validate_projection(output)
        except BaseException as exc:
            failure = exc
        finally:
            remaining = max(0,(deadline-datetime.now(timezone.utc)).total_seconds())
            clean = await runner._close_launch_resources_bounded(resources,timeout_seconds=min(10,remaining))
            cleanup = {"status":"verified" if clean and marker.snapshot()["status"] == "verified" else "unknown",
                "browser_closed":clean, "transport":marker.snapshot(),
                "launch_attempted":resources.launch_attempted, "no_persistent_storage":True}
            await cleanup_observer(cleanup)
        if cleanup["status"] != "verified":
            raise MoltbookError("moltbook_private_cleanup_unknown")
        if failure is not None:
            if isinstance(failure, (MoltbookError,asyncio.CancelledError)): raise failure
            if route_failure is not None and isinstance(route_failure,MoltbookError): raise route_failure
            raise MoltbookError("moltbook_private_browser_or_transfer_failed") from None
        await check_current()
        return output
