import pytest

from src.browser.forgejo_issue_title import (
    ForgejoError, TitleTarget, bounded_timeline, checked_assets, checked_title,
    require_browser_submission, require_issue_identity,
)


def target():
    return TitleTarget("operator", "fixture", 5, 19, 2, 7, "operator",
                       "Old summary", "Precise summary", "2026-10-04T04:00:00Z", "a" * 64)


@pytest.mark.parametrize("value", ["", " leading", "trailing ", "@person", "issue #1",
                                  "https://example.org", "line\nbreak", "x\u202e", "é" * 123])
def test_title_rejects_unapproved_normalization_cross_references_and_byte_overflow(value):
    with pytest.raises(ForgejoError): checked_title(value)


def test_submission_requires_actual_same_origin_metadata_and_exact_body():
    t = target()
    headers = {"origin": "https://codeberg.org", "sec-fetch-site": "same-origin",
               "referer": "https://codeberg.org" + t.page_path,
               "content-type": "application/x-www-form-urlencoded;charset=UTF-8"}
    require_browser_submission(t, url="https://codeberg.org" + t.page_path + "/title",
                               method="POST", body=t.title_body, headers=headers)
    for key, bad in [("origin", "https://evil.example"), ("sec-fetch-site", "none"),
                     ("cookie", "session=forbidden"), ("referer", "https://codeberg.org/")]:
        with pytest.raises(ForgejoError):
            require_browser_submission(t, url="https://codeberg.org" + t.page_path + "/title",
                                       method="POST", body=t.title_body, headers={**headers, key: bad})
    with pytest.raises(ForgejoError):
        require_browser_submission(t, url="https://codeberg.org" + t.page_path + "/title",
                                   method="POST", body=t.title_body + b"&other=x", headers=headers)


def test_title_bytes_use_browser_urlsearchparams_encoding():
    t = target()
    altered = TitleTarget(**{**vars(t), "new_title": "Summary * ~ é"})
    assert altered.title_body == b"title=Summary+*+%7E+%C3%A9"


def test_numeric_identity_is_not_issue_index_and_pr_is_excluded():
    t = target()
    value = {"id": 19, "number": 2, "title": t.new_title,
             "repository": {"id": 5, "full_name": "operator/fixture"}}
    require_issue_identity(value, t, title=t.new_title)
    for mutation in ({"id": 2}, {"id": True}, {"pull_request": {}}, {"number": 19}):
        with pytest.raises(ForgejoError): require_issue_identity({**value, **mutation}, t, title=t.new_title)


def test_complete_timeline_overflow_never_becomes_positive_truncated_proof():
    assert len(bounded_timeline([{"id": i} for i in range(1, 21)])) == 20
    with pytest.raises(ForgejoError): bounded_timeline([{"id": i} for i in range(1, 22)])
    with pytest.raises(ForgejoError): bounded_timeline([{"id": 1}, {"id": 1}])


def test_assets_are_finite_exact_digest_inventory_not_wildcard():
    assert len(checked_assets({"/assets/js/index.js?v=fixed": {"sha256": "b" * 64, "bytes": 400}})) == 64
    for path in ("https://evil.example/a", "/assets/../secret", "/not-assets/a"):
        with pytest.raises(ForgejoError): checked_assets({path: {"sha256": "b" * 64, "bytes": 400}})


def test_lazy_login_page_cookie_never_allows_authenticated_cookie_absence():
    from src.browser.forgejo_profile import session_cookie
    assert session_cookie({}, unauthenticated_login_page=True) is None
    with pytest.raises(ForgejoError): session_cookie({})
    deleted = "persistent=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0"
    assert session_cookie({"set-cookie": deleted}, unauthenticated_login_page=True) is None
    with pytest.raises(ForgejoError): session_cookie({"set-cookie": deleted})
    with pytest.raises(ForgejoError):
        session_cookie({"set-cookie": deleted.replace("persistent=", "persistent=active")},
                       unauthenticated_login_page=True)
    for value in ("session=abcdefghijklmnop; Path=/; Secure; SameSite=Lax",
                  "session=abcdefghijklmnop; Path=/; HttpOnly; SameSite=Lax",
                  "session=abcdefghijklmnop; Domain=evil.example; Path=/; Secure; HttpOnly; SameSite=Lax"):
        with pytest.raises(ForgejoError): session_cookie({"set-cookie": value})


def test_pinned_provisioning_rotation_is_not_general_duplicate_cookie_acceptance():
    from src.browser.forgejo_profile import session_cookie
    scope = "; Path=/; Secure; HttpOnly; SameSite=Lax"
    first = "session=" + "a" * 16 + scope
    second = "session=" + "b" * 16 + scope
    locale = "lang=en-US" + scope
    pair = first + ", " + second
    assert session_cookie({"set-cookie": pair + ", " + locale}, provisioning_login=True) == "b" * 16
    assert session_cookie({"set-cookie": second}, provisioning_login=True) == "b" * 16
    for value in (pair, second + ", " + locale):
        with pytest.raises(ForgejoError): session_cookie({"set-cookie": value})
    for value in (first + ", " + first, pair + ", " + second,
                  pair.replace("Path=/", "Path=/elsewhere", 1),
                  pair + "; Max-Age=86400", pair + ", " + locale.replace("en-US", "fr-FR"),
                  pair + ", " + locale + ", " + locale,
                  pair + ", other=secret" + scope):
        with pytest.raises(ForgejoError): session_cookie({"set-cookie": value}, provisioning_login=True)
