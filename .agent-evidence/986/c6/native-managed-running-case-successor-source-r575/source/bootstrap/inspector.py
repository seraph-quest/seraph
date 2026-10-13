'Real cockpit readback through original frontend; no API payload mocking.'
import asyncio


def assert_actual_inspector(context, http, original, current, *, reconcile_unknown=False):
    from browser_binding import validate
    browser_path, browser_args = validate(context)
    async def run():
        from playwright.async_api import async_playwright
        from client_boundary import install_browser_boundary
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=browser_path,
                                                       headless=True, args=browser_args)
            client = await browser.new_context(service_workers='block')
            try:
                await install_browser_boundary(client, 18241, 13241,
                    {(item['port'], item['path']) for item in context['browser_allowed_paths']})
                cookies = [{'name': c.name, 'value': c.value, 'url': 'http://127.0.0.1:13241',
                            'httpOnly': True, 'sameSite': 'Strict'} for c in http.cookies]
                await client.add_cookies(cookies)
                page = await client.new_page()
                await page.goto('http://127.0.0.1:13241/', wait_until='domcontentloaded')
                # Actual navigation selectors come from independently readback
                # original cockpit, not page injection or a mounted fixture app.
                await page.get_by_role('button', name='Open Work', exact=True).click()
                await page.get_by_text(original['task_title'], exact=True).click()
                pane = page.get_by_label('Original Source recovery readback')
                if current['source_recovery'] is None:
                    assert await pane.count() == 0
                    await page.screenshot(path=context['private_inspector_screenshot'])
                    return
                await pane.wait_for(state='visible', timeout=5000)
                text = await pane.inner_text()
                button = pane.get_by_role('button', name='Reconcile original cleanup', exact=True)
                if current['source_recovery']['public_actions'] == 'reconcile_original_cleanup':
                    assert await button.count() == 1
                    assert await button.is_enabled()
                else:
                    assert await button.count() == 0
                    assert 'Public Source recovery actions are unavailable.' in text
                if reconcile_unknown:
                    assert current['status'] == 'unknown_external_effect'
                    assert current['source_recovery']['physical_hold'] is True
                    assert current['source_recovery']['public_actions'] == 'reconcile_original_cleanup'
                    endpoint = 'http://127.0.0.1:18241/api/workflows/repo-repair/' + http.identifier(original['job_id'])
                    # Observe the actual control request and its authoritative refresh.
                    async with page.expect_response(lambda r: r.url == endpoint and r.request.method == 'GET', timeout=5000) as refreshed:
                        async with page.expect_response(lambda r: r.url == endpoint + '/source-recovery' and r.request.method == 'POST', timeout=5000) as posted:
                            await button.click()
                    response = await posted.value
                    assert response.status == 200
                    assert response.request.post_data_json == {'expected_job_revision': current['revision'], 'action': 'reconcile_original_cleanup'}
                    readback = await refreshed.value
                    assert readback.status == 200
                    actual = await readback.json()
                    assert actual['status'] == 'unknown_external_effect' and actual['no_learning'] is True
                    assert actual['source_recovery']['state'] == 'original_cleanup_committed'
                    assert actual['source_recovery']['physical_hold'] is True
                    assert actual['source_recovery']['public_actions'] == 'unavailable'
                    await button.wait_for(state='detached', timeout=5000)
                assert current['source_recovery']['reason'] in text
                await page.screenshot(path=context['private_inspector_screenshot'])
            finally:
                await client.close()
                await browser.close()
    asyncio.run(run())
