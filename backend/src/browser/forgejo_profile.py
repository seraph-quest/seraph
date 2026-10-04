"""One source-pinned Forgejo editor, with secrets only in backend transport.

The constructor TCP transport is an internal local-test seam. No HTTP input or
environment setting can enable production. Canonical native orchestration owns
each current-authority, intent, observation and cleanup callback.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import SimpleCookie
import json
import re
from urllib.parse import urlencode, urlsplit

from config.settings import settings
from src.browser.forgejo_issue_title import (
    ForgejoError, ORIGIN, MAX_CONTACTS, MAX_DOCUMENT_BYTES, MAX_ASSET_BYTES,
    checked_assets, digest, timeline_response, require_issue_identity,
    require_browser_submission,
)
from src.security.http_transport import (
    _TransportLifecycleMarker, default_resolver, request_pinned_https,
)


@dataclass(frozen=True)
class BackendSession:
    value: str = field(repr=False)
    provider_user_id: int
    provider_login: str

    def cookie_header(self):
        return "session=" + self.value


def session_cookie(headers, *, unauthenticated_login_page=False, provisioning_login=False):
    cookie = SimpleCookie()
    sessions = []
    try:
        # HTTPX joins multiple Set-Cookie fields with ', '. Expires commas
        # are not followed by a cookie-name assignment. Parse each unchanged
        # field separately. Only the pinned provisioning login may emit
        # Start's anonymous session followed by RegenerateID's new session.
        for raw in re.split(r", (?=[A-Za-z0-9_-]+=)", headers.get("set-cookie", "")):
            if not raw: continue
            parsed = SimpleCookie(); parsed.load(raw)
            if len(parsed) != 1: raise ValueError("ambiguous cookie field")
            attributes = [part.strip().split("=", 1)[0].lower() for part in raw.split(";")[1:]]
            if len(attributes) != len(set(attributes)): raise ValueError("duplicate cookie attribute")
            for name, value in parsed.items():
                if name == "session":
                    sessions.append(value)
                    if len(sessions) > (2 if provisioning_login else 1):
                        raise ValueError("duplicate cookie")
                elif name in cookie: raise ValueError("duplicate cookie")
                cookie[name] = value
    except Exception: raise ForgejoError("forgejo_session_cookie_invalid") from None
    if any(name not in {"session", "persistent", "lang", "redirect_to"} for name in cookie):
        raise ForgejoError("forgejo_session_cookie_name_changed")
    if "lang" in cookie:
        locale = cookie["lang"]
        if (not provisioning_login or locale.value != "en-US" or locale["domain"]
            or locale["path"] != "/" or not locale["secure"] or not locale["httponly"]
            or locale["samesite"].lower() != "lax" or locale["max-age"] or locale["expires"]):
            raise ForgejoError("forgejo_locale_cookie_changed")
    for name in ("persistent", "redirect_to"):
        if name in cookie and (cookie[name].value or cookie[name]["max-age"] != "0"):
            raise ForgejoError("forgejo_non_session_authority_cookie_blocked")
    if "session" not in cookie:
        if unauthenticated_login_page:
            if not cookie: return None
            # v15.0.9 autoSignIn deletes its unsupported long-term cookie
            # even when absent. Empty deletion is never usable authority.
            if set(cookie) == {"persistent"}:
                deleted = cookie["persistent"]
                if (not deleted.value and deleted["path"] == "/" and not deleted["domain"]
                    and deleted["secure"] and deleted["httponly"]
                    and deleted["samesite"].lower() == "lax"
                    and deleted["max-age"] == "0"):
                    return None
        raise ForgejoError("forgejo_session_cookie_missing")
    for value in sessions:
        if (value["domain"] or value["path"] != "/" or not value["secure"]
            or not value["httponly"] or value["samesite"].lower() != "lax"
            or value["max-age"] or value["expires"]
            or any(value[key] for key in value.keys()
                   if key not in {"domain", "path", "secure", "httponly", "samesite"})
            or not 16 <= len(value.value) <= 256
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value.value)):
            raise ForgejoError("forgejo_session_cookie_scope_changed")
    if len(sessions) == 2 and sessions[0].value == sessions[1].value:
        raise ForgejoError("forgejo_login_session_not_rotated")
    return sessions[-1].value


class ForgejoTitleBrowser:
    def __init__(self, *, local_transport=None, resolver=default_resolver):
        self.local_transport = local_transport
        self.resolver = resolver

    @property
    def production_blocked(self):
        return self.local_transport is None

    def require_available(self):
        if self.production_blocked: raise ForgejoError("forgejo_production_acceptance_required")

    async def provision(self, *, username, password, deadline, check_current, contact, observe):
        self.require_available()
        marker = _TransportLifecycleMarker()
        authorization = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
        async def fetch(operation, path, *, headers=None, body=None):
            await check_current()
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 10: raise ForgejoError("forgejo_original_deadline")
            result = await request_pinned_https(ORIGIN + path, method="POST" if body is not None else "GET",
                headers=headers, form_body=body, resolver=self.resolver, transport=self.local_transport,
                timeout_seconds=min(10, remaining-5), max_bytes=MAX_DOCUMENT_BYTES,
                observe_redirect_response=operation == "provision_login",
                _lifecycle_marker=marker, authority_check=lambda: contact(operation), handoff_check=check_current)
            await observe(operation, result.status_code, digest(result.content), marker.snapshot())
            await check_current()
            if password.encode() in result.content: raise ForgejoError("forgejo_secret_echo_blocked")
            return result
        before = await fetch("provision_identity_before", "/api/v1/user", headers={"Authorization": authorization})
        first = json.loads(before.content)
        if before.status_code != 200 or first.get("login") != username or type(first.get("id")) is not int:
            raise ForgejoError("forgejo_provider_identity_changed")
        login = await fetch("provision_login_page", "/user/login")
        if login.status_code != 200: raise ForgejoError("forgejo_login_not_supported")
        initial_cookie = session_cookie(login.headers, unauthenticated_login_page=True)
        # This is the explicitly consented trusted backend login. Never invent
        # browser Fetch Metadata for it. The title POST later forwards only
        # the actual Chromium Origin/Sec-Fetch-Site/Referer values.
        logged = await fetch("provision_login", "/user/login",
            headers={"Cookie": "session=" + initial_cookie} if initial_cookie else None,
            body=urlencode({"user_name": username, "password": password, "remember": "false"}).encode())
        location = logged.headers.get("location", "")
        if (logged.status_code not in {302, 303} or logged.location_header_count != 1
            or location not in {"/", ORIGIN + "/"}):
            raise ForgejoError("forgejo_login_flow_not_supported")
        cookie = session_cookie(logged.headers, provisioning_login=True)
        if cookie == initial_cookie: raise ForgejoError("forgejo_login_session_not_rotated")
        after = await fetch("provision_identity_after", "/api/v1/user", headers={"Authorization": authorization})
        second = json.loads(after.content)
        if after.status_code != 200 or (second.get("id"), second.get("login")) != (first["id"], username):
            raise ForgejoError("forgejo_provider_identity_changed")
        if marker.snapshot()["status"] != "verified": raise ForgejoError("forgejo_transport_cleanup_unknown")
        return BackendSession(cookie, first["id"], username), marker.snapshot()

    async def submit(self, *, target, session, username, password, asset_manifest, deadline,
                     check_current, contact, observe, cleanup_observer):
        self.require_available()
        checked_assets(asset_manifest)
        if (session.provider_user_id, session.provider_login) != (target.provider_user_id, target.provider_login):
            raise ForgejoError("forgejo_original_session_identity_changed")
        from src.browser.task_runner import BrowserTaskRunner, _BrowserLaunchResources, _BrowserSession
        runner = BrowserTaskRunner(workspace_root=settings.workspace_dir)
        resources = _BrowserLaunchResources()
        marker = _TransportLifecycleMarker()
        authorization = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
        contacts = 0
        mutation_started = False
        save_armed = False
        submission_response = None
        route_failure = None
        navigations = 0
        denied_auxiliary = []
        blocked_requests = []
        failure = None
        output = None
        async def fetch(operation, path, *, api=False, browser_headers=None, body=None, asset=None):
            nonlocal contacts
            await check_current()
            if contacts >= MAX_CONTACTS: raise ForgejoError("forgejo_contact_bound")
            contacts += 1
            remaining = (deadline-datetime.now(timezone.utc)).total_seconds()-10
            if remaining <= 0: raise ForgejoError("forgejo_original_deadline")
            headers = {"Authorization": authorization} if api else {"Cookie": session.cookie_header()}
            if browser_headers:
                for name in ("origin", "sec-fetch-site", "referer", "sec-fetch-mode", "sec-fetch-dest"):
                    if name in browser_headers: headers[name] = browser_headers[name]
            result = await request_pinned_https(ORIGIN + path, method="POST" if body is not None else "GET",
                headers=headers, form_body=body, resolver=self.resolver, transport=self.local_transport,
                timeout_seconds=min(10, remaining), max_bytes=MAX_ASSET_BYTES if asset else MAX_DOCUMENT_BYTES,
                _lifecycle_marker=marker, authority_check=lambda: contact(operation), handoff_check=check_current)
            await observe(operation, result.status_code, digest(result.content), marker.snapshot())
            await check_current()
            if password.encode() in result.content or session.value.encode() in result.content:
                raise ForgejoError("forgejo_secret_echo_blocked")
            if result.status_code != 200 or "location" in result.headers:
                raise ForgejoError("forgejo_provider_response_not_allowed")
            if "set-cookie" in result.headers and body is None:
                raise ForgejoError("forgejo_admitted_session_rotated")
            if asset and (digest(result.content) != asset["sha256"] or len(result.content) != asset["bytes"]):
                raise ForgejoError("forgejo_compiled_asset_changed")
            return result
        async def identity():
            response = await fetch("identity", "/api/v1/user", api=True)
            value = json.loads(response.content)
            if (value.get("id"), value.get("login")) != (target.provider_user_id, target.provider_login):
                raise ForgejoError("forgejo_provider_identity_changed")
        async def source(*, title, initial):
            response = await fetch("issue_readback", target.api_path, api=True)
            value = json.loads(response.content)
            require_issue_identity(value, target, title=title)
            timeline = await fetch("timeline_readback", target.api_path + "/timeline?page=1&limit=21", api=True)
            events = timeline_response(timeline.content, timeline.headers)
            if initial and (value.get("updated_at") != target.updated_at or digest(events) != target.timeline_digest):
                raise ForgejoError("forgejo_original_issue_revision_changed")
            return value, events
        try:
            await identity()
            await source(title=target.old_title, initial=True)
            await check_current()
            async with asyncio.timeout(max(0, (deadline-datetime.now(timezone.utc)).total_seconds()-10)):
                launched = await runner._launch_session(resources)
                await launched.context.close()
                context = await launched.browser.new_context(java_script_enabled=True, accept_downloads=False,
                    service_workers="block", storage_state=None, http_credentials=None, permissions=[])
                resources.context = context
                resources.session = _BrowserSession(launched.browser, context, launched.playwright)
                page = await context.new_page()
                async def route_guard(route, request):
                    nonlocal mutation_started, submission_response, route_failure, navigations
                    path = None
                    try:
                        await check_current()
                        headers = {k.lower(): v for k, v in (await request.all_headers()).items()}
                        parsed = urlsplit(request.url)
                        path = parsed.path + ("?" + parsed.query if parsed.query else "")
                        if (parsed.scheme != "https" or parsed.netloc != "codeberg.org" or parsed.fragment
                            or request.redirected_from is not None or request.frame != page.main_frame
                            or any(k in headers for k in ("authorization", "cookie", "proxy-authorization"))):
                            raise ForgejoError("forgejo_browser_route_changed")
                        if request.method == "POST":
                            require_browser_submission(target, url=request.url, method=request.method,
                                body=request.post_data_buffer, headers=headers)
                            if not save_armed: raise ForgejoError("forgejo_submission_before_reviewed_save")
                            if mutation_started: raise ForgejoError("forgejo_original_submission_not_replayable")
                            await identity()
                            await source(title=target.old_title, initial=True)
                            mutation_started = True
                            submission_response = await fetch("title_submission", path,
                                browser_headers=headers, body=request.post_data_buffer)
                            if json.loads(submission_response.content) != {"title": target.new_title}:
                                raise ForgejoError("forgejo_submission_response_changed")
                            response = submission_response
                        elif request.method == "GET" and path == target.page_path and request.resource_type == "document":
                            if navigations >= 2: raise ForgejoError("forgejo_document_navigation_bound")
                            navigations += 1
                            response = await fetch("issue_document", path, browser_headers=headers)
                            from src.browser.forgejo_bootstrap import validate_document
                            validate_document(response.content, target.provider_login)
                        elif request.method == "GET" and path in asset_manifest and request.resource_type in {"script", "stylesheet", "font"}:
                            response = await fetch("fixed_asset", path, asset=asset_manifest[path])
                        elif request.method == "GET" and (
                            (path == "/assets/js/eventsource.sharedworker.js?v=15.0.9~gitea-1.22.0" and request.resource_type == "script")
                            or (path == target.page_path+"/content-history/overview" and request.resource_type == "fetch")):
                            # Source-pinned optional initializers are denied,
                            # never supplied a fake provider response. The
                            # notification worker cannot execute or contact its
                            # background endpoint. Content history catches its
                            # failed GET independently from the title editor.
                            if len(denied_auxiliary) >= 32: raise ForgejoError("forgejo_auxiliary_request_bound")
                            denied_auxiliary.append("notification_worker" if request.resource_type == "script" else "content_history")
                            await route.abort(); return
                        elif request.method == "GET" and request.resource_type == "image":
                            # Images carry no title authority and are never sent
                            # to the provider; their denial is explicit audit.
                            if len(denied_auxiliary) >= 32: raise ForgejoError("forgejo_auxiliary_request_bound")
                            denied_auxiliary.append("image")
                            await route.abort(); return
                        else:
                            raise ForgejoError("forgejo_browser_request_not_allowed")
                        await check_current()
                        safe_headers = {k: v for k, v in response.headers.items()
                            if k.lower() in {"content-type", "x-content-type-options"}}
                        safe_headers["cache-control"] = "no-store"
                        await route.fulfill(status=response.status_code, headers=safe_headers, body=response.content)
                    except BaseException as exc:
                        route_failure = exc
                        if len(blocked_requests) < 8:
                            blocked_requests.append({"method": request.method,
                                "type": request.resource_type, "url_sha256": digest(request.url.encode()),
                                "same_origin_path": path if path in {target.page_path+"/content-history/overview",
                                    "/assets/js/eventsource.sharedworker.js?v=15.0.9~gitea-1.22.0",
                                    "/assets/css/dropzone.5a752d14.css", "/assets/js/dropzone.8f90b3c1.js"} else None,
                                "reason": getattr(exc, "reason", type(exc).__name__)})
                        await route.abort()
                await context.route("**/*", route_guard)
                context.on("page", lambda other: asyncio.create_task(other.close()) if other != page else None)
                await page.goto(ORIGIN+target.page_path, wait_until="domcontentloaded", timeout=10000)
                await page.locator("body:not(.no-js)").wait_for(timeout=5000)
                if route_failure is not None: raise route_failure
                if await context.cookies(): raise ForgejoError("forgejo_browser_secret_state_present")
                if (await page.locator(".repository.view.issue").count() != 1
                    or await page.locator("#issue-title-editor input").count() != 1
                    or await page.locator("#issue-title-edit-show").count() != 1
                    or await page.locator("#issue-title-editor input").get_attribute("maxlength") != "245"
                    or await page.locator("#issue-title-editor input").input_value() != target.old_title
                    or await page.locator("#issue-title-editor input").get_attribute("data-old-title") != target.old_title
                    or await page.locator("#issue-title-editor .primary.button").get_attribute("data-update-url") != target.page_path+"/title"
                    or await page.locator("details.dropdown .header strong").all_text_contents() != [target.provider_login]):
                    raise ForgejoError("forgejo_editor_layout_or_account_changed")
                await check_current()
                await page.locator("#issue-title-edit-show").click()
                await page.locator("#issue-title-editor input").fill(target.new_title)
                await check_current()
                save_armed = True
                try:
                    async with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
                        await page.locator("#issue-title-editor .primary.button").click()
                except Exception:
                    if route_failure is not None: raise route_failure
                    raise
                await page.locator("body:not(.no-js)").wait_for(timeout=5000)
                if route_failure is not None: raise route_failure
                if submission_response is None or not mutation_started:
                    raise ForgejoError("forgejo_original_submission_response_missing")
                await identity()
                value, events = await source(title=target.new_title, initial=False)
                if navigations != 2 or await page.locator("#issue-title-editor input").input_value() != target.new_title:
                    raise ForgejoError("forgejo_browser_readback_changed")
                html = await page.content()
                if password in html or session.value in html or len(html.encode()) > MAX_DOCUMENT_BYTES:
                    raise ForgejoError("forgejo_browser_secret_echo_blocked")
                matching = [item for item in events if item.get("type") == "change_title"
                    and item.get("old_title") == target.old_title and item.get("new_title") == target.new_title
                    and (item.get("user") or {}).get("id") == target.provider_user_id]
                output = {"schema": "forgejo_issue_title_receipt.v1", "issue_id": target.issue_id,
                    "repository_id": target.repository_id, "issue_index": target.issue_index,
                    "old_title": target.old_title, "new_title": target.new_title, "readback_title": value["title"],
                    "submission_response_sha256": digest(submission_response.content),
                    "browser_dom_sha256": digest(html.encode()), "timeline_sha256": digest(events),
                    "matching_title_event_ids": [item["id"] for item in matching], "contacts": contacts,
                    "denied_auxiliary": denied_auxiliary, "no_learning": True,
                    "degraded_background": "Notification worker and optional content history are denied before provider contact",
                    "provider_cas": False, "production_acceptance": "unverified_local_test_only"}
        except BaseException as exc:
            failure = exc
        finally:
            remaining = max(0, (deadline-datetime.now(timezone.utc)).total_seconds())
            closed = await runner._close_launch_resources_bounded(resources, timeout_seconds=min(10, remaining))
            cleanup = {"status": "verified" if closed and marker.snapshot()["status"] == "verified" else "unknown",
                "browser_closed": closed, "transport": marker.snapshot(), "possible_submission": mutation_started,
                "no_persistent_storage": True, "blocked_requests": blocked_requests}
            await cleanup_observer(cleanup)
        if cleanup["status"] != "verified": raise ForgejoError("forgejo_browser_or_transfer_cleanup_unknown")
        if failure is not None: raise failure
        await check_current()
        return output
