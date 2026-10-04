import httpx
import pytest
from datetime import datetime, timedelta, timezone

from src.browser.forgejo_issue_title import ForgejoError
from src.browser.forgejo_profile import ForgejoTitleBrowser
from src.security.http_transport import request_pinned_https, PinnedTransportError


@pytest.mark.asyncio
@pytest.mark.parametrize("opt,status,accepted", [(False, 302, False), (True, 302, True),
                                               (True, 303, True), (True, 301, False), (True, 307, False)])
async def test_fixed_login_redirect_observation_never_follows(opt, status, accepted):
    calls = []
    async def handler(request):
        calls.append(request.url)
        return httpx.Response(status, headers={"location": "/"}, content=b"bounded redirect")
    args = dict(method="POST", form_body=b"user_name=local", resolver=lambda h, p: ["1.1.1.1"],
                transport=httpx.MockTransport(handler), observe_redirect_response=opt)
    if accepted:
        result = await request_pinned_https("https://codeberg.org/user/login", **args)
        assert result.status_code == status and result.location_header_count == 1
    else:
        with pytest.raises(PinnedTransportError):
            await request_pinned_https("https://codeberg.org/user/login", **args)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("url,opt", [("https://example.org/user/login", True),
                                     ("https://codeberg.org/other", True),
                                     ("https://codeberg.org/user/login", 1)])
async def test_login_redirect_opt_in_cannot_expand_site_or_route_or_coerce_boolean(url, opt):
    async def handler(request): raise AssertionError("must reject before contact")
    with pytest.raises(PinnedTransportError):
        await request_pinned_https(url, method="POST", form_body=b"x=y", resolver=lambda h, p: ["1.1.1.1"],
                                  transport=httpx.MockTransport(handler), observe_redirect_response=opt)


@pytest.mark.asyncio
@pytest.mark.parametrize("locations", [["https://evil.example/"], ["/user/change-password"], ["/", "/"]])
async def test_profile_rejects_cross_origin_alternate_flow_and_ambiguous_login_locations(locations):
    calls = []
    async def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v1/user": return httpx.Response(200, json={"id": 1, "login": "local"})
        if request.method == "GET": return httpx.Response(200, content=b"actual login shape not used by this unit")
        headers = [("location", value) for value in locations]
        headers.append(("set-cookie", "session=" + "a" * 32 + "; Path=/; Secure; HttpOnly; SameSite=Lax"))
        return httpx.Response(302, headers=headers)
    async def noop(*args): pass
    browser = ForgejoTitleBrowser(local_transport=httpx.MockTransport(handler), resolver=lambda h, p: ["1.1.1.1"])
    with pytest.raises(ForgejoError, match="login_flow_not_supported"):
        await browser.provision(username="local", password="private-unit-password",
            deadline=datetime.now(timezone.utc)+timedelta(seconds=120), check_current=noop, contact=noop, observe=noop)
    assert calls == [("GET", "/api/v1/user"), ("GET", "/user/login"), ("POST", "/user/login")]
