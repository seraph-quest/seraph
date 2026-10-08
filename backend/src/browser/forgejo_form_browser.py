"""Two exact native no-script forms. Secrets exist only at backend handoff."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
import uuid
from urllib.parse import urlsplit

from config.settings import settings
from src.browser.forgejo_forms import (FormDocument, MAX_FORM_CONTACTS, checked_content,
    encoded_controls, exact_readback, form_destination, strict_json)
from src.browser.forgejo_form_grammar import (NATIVE_SNAPSHOT, ordinary_selector,
    typed_slots, validate_native, validate_native_handoff)
from src.browser.forgejo_issue_title import (ForgejoError, ORIGIN, MAX_DOCUMENT_BYTES,
    checked_segment, checked_title, positive_id, digest)
from src.security.http_transport import _TransportLifecycleMarker, request_pinned_https


class ForgejoFormBrowser:
    def __init__(self, browser):
        self.browser = browser

    async def inspect_exact(self, *, target, exact_id, username, password, deadline,
                            check_current, contact, observe, cleanup_observer):
        """Fresh acknowledged GET-only observation; original liability is untouched."""
        self.browser.require_available()
        positive_id(exact_id)
        marker=_TransportLifecycleMarker()
        authorization="Basic "+base64.b64encode((username+":"+password).encode()).decode()
        async def get(operation,path):
            await check_current()
            descriptor={"request_id":uuid.uuid4().hex,"method":"GET","operation":operation,"path_digest":digest(path.encode())}
            remaining=(deadline-datetime.now(timezone.utc)).total_seconds()-5
            if remaining<=0:raise ForgejoError("forgejo_original_deadline")
            result=await request_pinned_https(ORIGIN+path,headers={"Authorization":authorization},
                resolver=self.browser.resolver,transport=self.browser.local_transport,
                timeout_seconds=min(10,remaining),max_bytes=MAX_DOCUMENT_BYTES,_lifecycle_marker=marker,
                authority_check=lambda:contact(operation,descriptor),handoff_check=check_current)
            await observe(operation,result.status_code,digest(result.content),marker.snapshot(),descriptor["request_id"])
            await check_current()
            if (result.status_code!=200 or "location" in result.headers or "set-cookie" in result.headers
                or result.headers.get("content-type","").split(";",1)[0].strip().lower()!="application/json"
                or password.encode() in result.content):
                raise ForgejoError("forgejo_exact_id_observation_blocked")
            return strict_json(result.content)
        try:
            actor=await get("form_observation_actor","/api/v1/user")
            if type(actor.get("id")) is not int or (actor["id"],actor.get("login"))!=(target["provider_user_id"],username):
                raise ForgejoError("forgejo_provider_identity_changed")
            path=(f'/api/v1/repos/{target["owner"]}/{target["repository"]}/issues/{exact_id}'
                if target["profile"]=="forgejo.issue-create.v1"
                else f'/api/v1/repos/{target["owner"]}/{target["repository"]}/issues/comments/{exact_id}')
            result=exact_readback(await get("form_observation_readback",path),target,exact_id)
            return {"observation_only":True,"original_unknown":True,"original_capacity_released":False,
                "exact_id":exact_id,"observed_body":result["body"],"observed_title":result.get("title"),
                "attribution_uncertain":True,"no_learning":True}
        finally:
            await cleanup_observer({"status":marker.snapshot()["status"],"browser_closed":True,
                "launch_attempted":False,"transport":marker.snapshot(),"possible_submission":False})

    async def transact(self, *, profile, fields, session, username, password, expected_user_id,
                       deadline, check_current, contact, observe, cleanup_observer,
                       target=None, document=None, retain_source=None, retain_destination=None):
        self.browser.require_available()
        if profile not in {"forgejo.issue-create.v1", "forgejo.issue-comment.v1"}:
            raise ForgejoError("forgejo_reviewed_profile_required")
        owner, repository = fields["owner"], fields["repository"]
        checked_segment(owner); checked_segment(repository)
        content = checked_content(fields["content"])
        title = checked_title(fields["title"]) if profile == "forgejo.issue-create.v1" else ""
        issue_index = positive_id(fields["issue_index"]) if profile == "forgejo.issue-comment.v1" else None
        page_path = f"/{owner}/{repository}/issues/" + (str(issue_index) if issue_index else "new")
        post_path = page_path + ("/comments" if issue_index else "")
        issue_api_path = f"/api/v1/repos/{owner}/{repository}/issues/{issue_index}" if issue_index else None
        form_id = "comment-form" if issue_index else "new-issue"
        preparing = target is None
        authorization = "Basic " + base64.b64encode((username + ":" + password).encode()).decode()
        if (session.provider_user_id, session.provider_login) != (expected_user_id, username):
            raise ForgejoError("forgejo_original_session_identity_changed")
        marker = _TransportLifecycleMarker()
        contacts = 0
        started = False
        closing = False
        route_failure = None
        response = None
        handlers = set()
        from src.browser.task_runner import BrowserTaskRunner, _BrowserLaunchResources, _BrowserSession
        runner = BrowserTaskRunner(workspace_root=settings.workspace_dir)
        resources = _BrowserLaunchResources()
        cleanup = None

        async def current():
            if closing: raise ForgejoError("forgejo_browser_cleanup_started")
            await check_current()
            if closing: raise ForgejoError("forgejo_browser_cleanup_started")

        async def fetch(operation, path, *, api=False, body=None, native_headers=None):
            nonlocal contacts
            await current()
            if contacts >= (MAX_FORM_CONTACTS if preparing else 2):
                raise ForgejoError("forgejo_form_contact_bound")
            contacts += 1
            descriptor = {"request_id": uuid.uuid4().hex, "operation": operation,
                          "method": "GET" if body is None else "POST", "path_digest": digest(path.encode())}
            if body is not None: descriptor["body_digest"] = digest(body)
            headers = {"Authorization": authorization} if api else {"Cookie": session.cookie_header()}
            if native_headers:
                for name in ("origin", "referer", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"):
                    if name in native_headers: headers[name] = native_headers[name]
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds() - 10
            if remaining <= 0: raise ForgejoError("forgejo_original_deadline")
            result = await request_pinned_https(ORIGIN + path, method=descriptor["method"],
                headers=headers, form_body=body, resolver=self.browser.resolver,
                transport=self.browser.local_transport, timeout_seconds=min(10, remaining),
                max_bytes=MAX_DOCUMENT_BYTES, _lifecycle_marker=marker,
                authority_check=lambda: contact(operation, descriptor), handoff_check=current)
            await observe(operation, result.status_code, digest(result.content), marker.snapshot(), descriptor["request_id"])
            await current()
            if password.encode() in result.content or session.value.encode() in result.content:
                raise ForgejoError("forgejo_secret_echo_blocked")
            if result.status_code != 200 or "location" in result.headers or "set-cookie" in result.headers:
                raise ForgejoError("forgejo_form_response_unknown" if started else "forgejo_form_source_blocked")
            if (api or body is not None) and result.headers.get("content-type", "").split(";",1)[0].strip().lower()!="application/json":
                raise ForgejoError("forgejo_form_response_type_changed")
            return result

        try:
            if preparing:
                actor = strict_json((await fetch("form_actor", "/api/v1/user", api=True)).content)
                if type(actor.get("id")) is not int or (actor["id"], actor.get("login")) != (expected_user_id, username):
                    raise ForgejoError("forgejo_provider_identity_changed")
                repo = strict_json((await fetch("form_repository", f"/api/v1/repos/{owner}/{repository}", api=True)).content)
                positive_id(repo.get("id"))
                if (repo.get("full_name") != owner + "/" + repository
                    or type((repo.get("owner") or {}).get("id")) is not int
                    or (repo.get("owner") or {}).get("id") != expected_user_id):
                    raise ForgejoError("forgejo_selected_owned_repository_required")
                issue = None
                if issue_index:
                    issue = strict_json((await fetch("form_issue", issue_api_path, api=True)).content)
                    positive_id(issue.get("id"))
                    if (type(issue.get("number")) is not int or issue["number"] != issue_index
                        or issue.get("pull_request") is not None or type((issue.get("repository") or {}).get("id")) is not int
                        or (issue.get("repository") or {}).get("id") != repo["id"]):
                        raise ForgejoError("forgejo_ordinary_issue_required")
                source = await fetch("form_document", page_path)
                if "text/html" not in source.headers.get("content-type", ""):
                    raise ForgejoError("forgejo_native_document_type_changed")
                document = source.content
                slots = typed_slots(owner=owner, repository=repository, username=username,
                    provider_user_id=expected_user_id, avatar_url=actor.get("avatar_url"), profile=profile,
                    issue_index=issue_index, issue_title=issue.get("title") if issue else None)
                parser = FormDocument(form_id=form_id, action=post_path, profile=profile, slots=slots)
                parser.feed(document.decode("utf-8", errors="strict")); parser.close()
                controls = parser.reviewed_controls(title=title, content=content)
                target = {"profile": profile, "owner": owner, "repository": repository,
                    "provider_user_id": expected_user_id, "provider_login": username, "repository_id": repo["id"],
                    "issue_id": issue["id"] if issue else None, "issue_index": issue_index,
                    "title": title, "content": content, "page_path": page_path, "post_path": post_path,
                    "issue_api_path": issue_api_path, "page_digest": digest(document),
                    "form_identity": digest(parser.form_attributes), "controls": [[name, value] for name, value in controls],
                    "field_digests": {name: digest(value.encode()) for name, value in controls},
                    "encoded_body_digest": digest(encoded_controls(controls)), "submit_node": form_id + ":ordinary-primary",
                    "expected_destination": page_path if issue_index else f"/{owner}/{repository}/issues/{{positive_index}}",
                    "readback_contract": "numeric-basic-api-full-literal.v1", "single_post": True}
                target["source_slots"] = slots
                target["fixed_source_signature"] = parser.source_signature
                from src.browser.interaction_contracts import FormTransaction
                target["form_transaction"]=FormTransaction(profile_ref=profile,
                    **{key:target[key] for key in ("page_digest","form_identity","field_digests","submit_node",
                        "expected_destination","readback_contract","encoded_body_digest")}).model_dump()
            else:
                if (type(document) is not bytes or len(document) > MAX_DOCUMENT_BYTES
                    or digest(document) != target["page_digest"] or target["profile"] != profile
                    or target["owner"] != owner or target["repository"] != repository
                    or target["content"] != content or target["title"] != title):
                    raise ForgejoError("forgejo_original_form_source_changed")
                slots = target["source_slots"]
                if (slots["repository_path"] != f"/{owner}/{repository}"
                    or slots["username"] != username or slots["provider_user_id"] != str(expected_user_id)
                    or (issue_index is not None and slots["issue_index"] != str(issue_index))):
                    raise ForgejoError("forgejo_original_form_source_changed")
                parser = FormDocument(form_id=form_id, action=post_path, profile=profile, slots=slots)
                parser.feed(document.decode("utf-8", errors="strict")); parser.close()
                controls = parser.reviewed_controls(title=title, content=content)
                if (digest(parser.form_attributes) != target["form_identity"]
                    or digest(encoded_controls(controls)) != target["encoded_body_digest"]
                    or parser.source_signature != target["fixed_source_signature"]):
                    raise ForgejoError("forgejo_original_form_body_changed")
            await current()
            launched = await runner._launch_session(resources)
            await launched.context.close()
            context = await launched.browser.new_context(java_script_enabled=False, accept_downloads=False,
                service_workers="block", storage_state=None, http_credentials=None, permissions=[])
            resources.context = context
            resources.session = _BrowserSession(launched.browser, context, launched.playwright)
            page = await context.new_page()
            loaded = False
            armed = False

            async def route_guard(route, request):
                nonlocal loaded, started, response, route_failure
                task = asyncio.current_task(); handlers.add(task)
                try:
                    await current()
                    headers = {key.lower(): value for key, value in (await request.all_headers()).items()}
                    parsed = urlsplit(request.url)
                    if (parsed.scheme != "https" or parsed.netloc != "codeberg.org" or parsed.query or parsed.fragment
                        or request.redirected_from is not None or request.frame != page.main_frame
                        or any(key in headers for key in ("cookie", "authorization", "proxy-authorization"))):
                        await route.abort(); return
                    if request.method == "GET" and request.url == ORIGIN + page_path and not loaded:
                        loaded = True
                        await route.fulfill(status=200, headers={"content-type": "text/html; charset=utf-8", "cache-control": "no-store"}, body=document)
                    elif request.method == "POST":
                        body = encoded_controls(controls)
                        if (preparing or not armed or started or request.url != ORIGIN + post_path
                            or request.post_data_buffer != body or digest(body) != target["encoded_body_digest"]):
                            raise ForgejoError("forgejo_exact_native_submission_changed")
                        validate_native_handoff(headers, page_path=page_path,
                            frame_url=request.frame.url, page_url=page.url)
                        await current()
                        started = True
                        response = await fetch("form_submission", post_path, body=body, native_headers=headers)
                        await route.fulfill(status=200, headers={"content-type": "application/json", "cache-control": "no-store"}, body=response.content)
                    else:
                        await route.abort()
                except BaseException as exc:
                    route_failure = exc
                    try: await route.abort()
                    except Exception:
                        if not closing: raise
                finally:
                    handlers.discard(task)

            await context.route("**/*", route_guard)
            await context.route_web_socket("**/*", lambda socket: socket.close())
            context.on("page", lambda other: asyncio.create_task(other.close()) if other != page else None)
            await page.goto(ORIGIN + page_path, wait_until="domcontentloaded", timeout=10000)
            if route_failure is not None: raise route_failure
            if (await context.cookies() or await page.locator("details.dropdown .header strong").all_text_contents() != [username]
                or await page.locator("#" + form_id).count() != 1):
                raise ForgejoError("forgejo_native_account_or_form_changed")
            form = page.locator("#" + form_id)
            selector = ordinary_selector(profile)
            if await form.locator(selector).count() != 1:
                raise ForgejoError("forgejo_exact_submitter_missing")
            before = await form.evaluate(NATIVE_SNAPSHOT, selector)
            validate_native(before, profile=profile, slots=slots)
            if title: await form.locator('[name="title"]').fill(title)
            await form.locator('textarea[name="content"]').fill(content)
            after = await form.evaluate(NATIVE_SNAPSHOT, selector)
            validate_native(after, profile=profile, slots=slots, title=title, content=content)
            native_controls=after["successful"]
            if (not isinstance(native_controls,list)
                or any(not isinstance(item,list) or len(item)!=2 or any(type(value) is not str for value in item)
                       for item in native_controls)):
                raise ForgejoError("forgejo_native_successful_controls_changed")
            native_controls=[(name,value.replace("\r\n","\n").replace("\r","\n").replace("\n","\r\n"))
                             for name,value in native_controls]
            if native_controls!=controls or digest(encoded_controls(native_controls))!=target["encoded_body_digest"]:
                raise ForgejoError("forgejo_native_successful_controls_changed")
            await current()
            if preparing:
                target["native_signature"] = digest(after)
                target["ordinary_submitter_signature"] = digest(after["ordinary_submitters"][0])
                source_ref = await retain_source(document)
                return {"target": target, "source_ref": source_ref, "native_prepared": True,
                    "notifications": "Ordinary Forgejo issue/comment notifications and history may be created",
                    "no_learning": True}
            armed = True
            if (digest(after) != target["native_signature"]
                or digest(after["ordinary_submitters"][0]) != target["ordinary_submitter_signature"]
                or await form.locator(selector).count() != 1):
                raise ForgejoError("forgejo_original_native_signature_changed")
            try:
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=10000):
                    await form.locator(selector).click(timeout=5000)
            except Exception as exc:
                raise ForgejoError("forgejo_original_form_response_missing") from (route_failure or exc)
            if route_failure is not None: raise route_failure
            if not started or response is None: raise ForgejoError("forgejo_original_form_response_missing")
            exact_id = form_destination(response.content, profile=profile, owner=owner, repository=repository, issue_index=issue_index)
            await retain_destination(exact_id)
            read_path = (f"/api/v1/repos/{owner}/{repository}/issues/{exact_id}" if not issue_index
                         else f"/api/v1/repos/{owner}/{repository}/issues/comments/{exact_id}")
            readback_response = await fetch("form_readback", read_path, api=True)
            readback = exact_readback(strict_json(readback_response.content), target, exact_id)
            return {"schema": "forgejo_exact_form_receipt.v1", "profile": profile,
                "exact_id": exact_id, "numeric_id": readback["id"], "repository_id": target["repository_id"],
                "issue_index": exact_id if not issue_index else issue_index,
                "readback_body": readback["body"], "readback_title": readback.get("title"),
                "encoded_body_digest": target["encoded_body_digest"], "page_digest": target["page_digest"],
                "single_post": True, "no_learning": True}
        finally:
            closing = True
            closed = resources is None
            if resources is not None:
                remaining = max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
                closed = await runner._close_launch_resources_bounded(resources, timeout_seconds=min(10, remaining))
            drained = not handlers
            if handlers:
                try:
                    async with asyncio.timeout(3): await asyncio.gather(*tuple(handlers), return_exceptions=True)
                    drained = not handlers
                except BaseException: drained = False
            cleanup = {"status": "verified" if closed and drained and marker.snapshot()["status"] == "verified" else "unknown",
                "browser_closed": closed, "transport": marker.snapshot(), "possible_submission": started,
                "route_handlers_drained": drained, "no_persistent_storage": True}
            await cleanup_observer(cleanup,resources)
            if cleanup["status"] != "verified": raise ForgejoError("forgejo_browser_or_transfer_cleanup_unknown")
