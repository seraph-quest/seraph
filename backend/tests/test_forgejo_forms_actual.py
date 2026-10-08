"""Actual unmodified signed Forgejo fixture; no canonical success substitutions."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from tests.test_forgejo_native_actual import ActualLoopback
from src.browser.forgejo_forms import FormDocument
from src.browser.forgejo_issue_title import digest
from src.browser.forgejo_form_grammar import (NATIVE_SNAPSHOT, ordinary_selector,
    typed_slots, validate_native)


@pytest.mark.asyncio
async def test_actual_form_source_preflight():
    credential_file = os.environ.get("FORGEJO_TEST_CREDENTIAL_FILE")
    if not credential_file: pytest.skip("requires reviewed confined signed local Forgejo 15.0.9")
    credentials = json.loads(Path(credential_file).read_text())
    bootstrap = json.loads((Path(credential_file).parent/'bootstrap.json').read_text())
    from src.browser.forgejo_profile import ForgejoTitleBrowser
    import httpx
    from playwright.async_api import async_playwright
    calls = []
    async def current(): pass
    async def contact(operation, descriptor): calls.append(descriptor)
    async def observe(*args): pass
    browser = ForgejoTitleBrowser(local_transport=ActualLoopback(int(os.environ["FORGEJO_TEST_TCP_PORT"])), resolver=lambda h,p:["1.1.1.1"])
    session, cleanup = await browser.provision(username=credentials["user_name"],password=credentials["password"],
        deadline=datetime.now(timezone.utc)+timedelta(seconds=120),check_current=current,contact=contact,observe=observe)
    assert cleanup["status"] == "verified" and len(calls) == 4
    async with httpx.AsyncClient(base_url='http://127.0.0.1:'+os.environ["FORGEJO_TEST_TCP_PORT"],
        headers={"Host":"codeberg.org","Cookie":session.cookie_header()},trust_env=False,follow_redirects=False) as client:
        results=[]
        captured=[]
        for profile,path,form_id,action in (
            ("forgejo.issue-create.v1","/seraph_fixture1013/fixture1013/issues/new","new-issue","/seraph_fixture1013/fixture1013/issues/new"),
            ("forgejo.issue-comment.v1","/seraph_fixture1013/fixture1013/issues/1","comment-form","/seraph_fixture1013/fixture1013/issues/1/comments")):
            async with client.stream('GET',path) as streamed:
                assert streamed.status_code==200,streamed.status_code
                chunks=[];size=0
                async for chunk in streamed.aiter_bytes():
                    size+=len(chunk)
                    assert size<=524288,'Actual signed fixture HTML exceeds declared limit'
                    chunks.append(chunk)
                response=httpx.Response(streamed.status_code,headers=streamed.headers,content=b''.join(chunks))
            source_path=Path(credential_file).parent/(profile+'.html')
            source_path.write_bytes(response.content);os.chmod(source_path,0o600)
            # Preserve the signed server's complete HTML and Chromium's native
            # form ownership before asking our profile parser to accept it.
            async with async_playwright() as playwright:
                native=await playwright.chromium.launch(executable_path=str(Path(credential_file).parent.parent/'forgejo-chromium-confined.py'),headless=True)
                context=await native.new_context(java_script_enabled=False)
                try:
                    async def route(request):
                        if request.request.url=='https://codeberg.org'+path and request.request.is_navigation_request():
                            await request.fulfill(status=200,body=response.content,headers={'Content-Type':'text/html; charset=utf-8'})
                        else:await request.abort()
                    await context.route('**/*',route)
                    page=await context.new_page()
                    await page.goto('https://codeberg.org'+path,wait_until='domcontentloaded',timeout=15000)
                    selector = ordinary_selector(profile)
                    form = page.locator('#'+form_id)
                    assert await form.count() == 1
                    actual=await form.evaluate(NATIVE_SNAPSHOT, selector)
                    dom_path=Path(credential_file).parent/(profile+'.native-dom.json')
                    dom_path.write_text(json.dumps(actual,indent=2));os.chmod(dom_path,0o600)
                    scripts=actual['active_scripts']
                    script_path=Path(credential_file).parent/(profile+'.native-script-inventory.json')
                    script_path.write_text(json.dumps(scripts,indent=2));os.chmod(script_path,0o600)
                    # Assertions concern captured native facts, not product parser
                    # acceptance. Both complete raw/DOM/script records are saved first.
                    assert actual['action'] == 'https://codeberg.org'+action
                    assert actual['method'] == 'post'
                    assert len(actual['controls']) == (29 if form_id == 'new-issue' else 21)
                    assert len(scripts) == 4  # lexical comment fifth is inert template content
                    assert await form.locator(selector).count() == 1
                    assert len(actual['ordinary_submitters']) == 1
                    submitter = actual['ordinary_submitters'][0]
                    assert submitter['owner'] == actual['form']
                    assert submitter['tag'] == 'BUTTON' and submitter['type'] == 'submit'
                    assert submitter['name'] == '' and submitter['disabled'] is False
                    assert not {name for name, value in submitter['attributes']} & {
                        'form','formaction','formmethod','formenctype','formtarget'}
                    expected = ([['title',''],['content',''],['ref',''],['edit_mode','true'],
                        ['search',''],['label_ids',''],['milestone_id',''],['project_id',''],
                        ['assignee_ids','']] if form_id == 'new-issue' else [['content','']])
                    assert actual['successful'] == expected
                    if form_id == 'new-issue':
                        assert len(actual['redirect_controls']) == 1
                        redirect = actual['redirect_controls'][0]
                        assert redirect['owner'] is None and redirect['value'] == ''
                        assert redirect['type'] == 'hidden'
                finally:
                    await context.close();await native.close()
            assert b"_csrf" not in response.content
            captured.append((profile,form_id,action,response.text,actual))
        for profile,form_id,action,source,actual in captured:
            actor=bootstrap['actor']
            slots=typed_slots(owner=credentials['user_name'],repository='fixture1013',username=credentials['user_name'],
                provider_user_id=actor['id'],avatar_url=actor['avatar_url'],profile=profile,
                issue_index=1 if form_id=='comment-form' else None,
                issue_title=bootstrap['issue']['title'] if form_id=='comment-form' else None)
            document=FormDocument(form_id=form_id,action=action,profile=profile,slots=slots)
            document.feed(source);document.close()
            validate_native(actual,profile=profile,slots=slots)
            controls=document.reviewed_controls(title="Exact prepared title",content="Exact prepared body")
            results.append({"profile":profile,"controls":controls,"form_attributes":document.form_attributes,
                "fixed_source_signature":document.source_signature,"native_signature":digest(actual)})
        path=Path(credential_file).parent/'actual-source-controls.json'
        path.write_text(json.dumps(results,indent=2));os.chmod(path,0o600)
