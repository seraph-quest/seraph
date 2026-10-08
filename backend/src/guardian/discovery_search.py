"""Fixed DDG HTML search transport; no alternate backend or URL authority."""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
from urllib.parse import parse_qs, urlencode, urlsplit

from src.security.http_transport import request_pinned_https, parse_public_https_url

SEARCH_URL = "https://html.duckduckgo.com/html/"


class DiscoverySearchBlocked(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _Results(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.current = None
        self.depth = 0
        self.no_results = False
        self.captcha = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = set(values.get("class", "").split())
        if "captcha" in str(values.get("id", "")).lower() or values.get("id") in {"challenge-form", "anomaly-modal"} or "anomaly-modal" in classes:
            self.captcha = True
        if "no-results" in classes or "result--no-result" in classes:
            self.no_results = True
        if tag == "a" and "result__a" in classes:
            if self.current is not None or not values.get("href"):
                raise DiscoverySearchBlocked("search_markup_drift")
            self.current = {"href": values["href"], "title": []}
            self.depth = 1
        elif self.current is not None:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.current is not None:
            self.depth -= 1
            if self.depth == 0:
                if tag != "a":
                    raise DiscoverySearchBlocked("search_markup_drift")
                self.results.append((self.current["href"], " ".join("".join(self.current["title"]).split())))
                self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current["title"].append(data)


def _result_url(value):
    parsed = urlsplit(value)
    if value.startswith("//"):
        parsed = urlsplit("https:" + value)
    if parsed.hostname in {"duckduckgo.com", "html.duckduckgo.com"} and parsed.path == "/l/":
        try:
            values = parse_qs(parsed.query, strict_parsing=True)
        except ValueError:
            raise DiscoverySearchBlocked("search_redirect_wrapper_invalid") from None
        if len(values.get("uddg", [])) != 1:
            raise DiscoverySearchBlocked("search_redirect_wrapper_invalid")
        value = values["uddg"][0]
    try:
        parse_public_https_url(value)
    except ValueError:
        raise DiscoverySearchBlocked("search_result_url_unsupported") from None
    return value


def parse_search_html(raw: bytes):
    if not 0 < len(raw) <= 524288:
        raise DiscoverySearchBlocked("search_response_byte_cap")
    try:
        page = _Results()
        page.feed(raw.decode("utf-8", errors="strict"))
        page.close()
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, DiscoverySearchBlocked):
            raise
        raise DiscoverySearchBlocked("search_markup_drift") from None
    if page.captcha:
        raise DiscoverySearchBlocked("search_captcha")
    if page.current is not None or (not page.results and not page.no_results) or (page.results and page.no_results):
        raise DiscoverySearchBlocked("search_markup_drift")
    records = []
    for href, title in page.results:
        if not title or len(title.encode()) > 1024:
            raise DiscoverySearchBlocked("search_title_unsupported")
        records.append((_result_url(href), title))
    return records


class DiscoverySearch:
    def __init__(self, *, resolver=None, transport=None, locale="us-en"):
        if locale not in {"us-en", "uk-en", "pl-pl"}:
            raise ValueError("unsupported fixed search locale")
        self.locale, self.resolver, self.transport = locale, resolver, transport

    async def search(self, queries, *, run_id, max_results=15, remaining_seconds=20, authority_check):
        if (not isinstance(queries, list) or not 1 <= len(queries) <= 3
                or type(max_results) is not int or not 1 <= max_results <= 15):
            raise DiscoverySearchBlocked("search_limits_invalid")
        for query in queries:
            if not isinstance(query, str) or not query.strip() or len(query.encode()) > 2048 or any(ord(c) < 32 for c in query):
                raise DiscoverySearchBlocked("search_query_invalid")
        combined, seen = [], set()
        for query in queries:
            remaining = remaining_seconds() if callable(remaining_seconds) else remaining_seconds
            if remaining <= 0:
                raise DiscoverySearchBlocked("search_deadline_expired")
            kwargs = {"transport": self.transport}
            if self.resolver is not None:
                kwargs["resolver"] = self.resolver
            response = await request_pinned_https(SEARCH_URL, method="POST",
                form_body=urlencode({"q": query, "b": "", "kl": self.locale}).encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"},
                timeout_seconds=min(20, remaining), max_bytes=524288,
                authority_check=authority_check, **kwargs)
            if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0].lower() != "text/html":
                raise DiscoverySearchBlocked("search_response_unsupported")
            for url, title in parse_search_html(response.content):
                if url in seen:
                    continue
                seen.add(url)
                if len(combined) < max_results:
                    combined.append({"result_id": hashlib.sha256(url.encode()).hexdigest()[:32],
                        "exact_url": url, "title": title, "observed_at": datetime.now(timezone.utc).isoformat()})
        return {"run_id": str(run_id), "query_digest": hashlib.sha256("\n".join(queries).encode()).hexdigest(), "results": combined}
