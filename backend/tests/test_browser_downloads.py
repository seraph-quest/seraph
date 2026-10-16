"""C5.3 (#1014): quarantined public downloads and pre-contact upload refusal."""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import io
import os
import stat
import threading
from pathlib import Path
from typing import Any
import zipfile

import pytest

from src.browser import downloads as dl
from src.browser.downloads import (
    DOCX, XLSX, DownloadError, DownloadGrant, DownloadQuarantine, OwnedDownloadTransport,
    checked_mime, download_public_files, safe_filename,
)

OWNER = {"principal_id": "operator-1", "session_id": "session-1"}
OTHER = {"principal_id": "operator-2", "session_id": "session-2"}


def _ooxml(part: str, *extra: tuple[str, str], types: str = "<Types/>") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr(part, "<x/>")
        for name, body in extra:
            archive.writestr(name, body)
    return buffer.getvalue()


_HYPERLINK_RELS = ('<Relationships><Relationship Id="r1" TargetMode="External" Target="https://example.com" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"/>'
                   '</Relationships>')
_TEMPLATE_RELS = ('<Relationships><Relationship Id="r1" TargetMode="External" Target="https://evil.example/t.dotm" '
                  'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate"/>'
                  '</Relationships>')


class _Server:
    """Loopback-only fixture origin that counts every contact per path."""

    def __init__(self, routes: dict[str, tuple[int, dict[str, str], bytes]]):
        self.hits: dict[str, int] = {}
        hits, table = self.hits, routes

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                hits[f"{self.command} {self.path}"] = hits.get(f"{self.command} {self.path}", 0) + 1
                status, headers, body = table.get(self.path, (404, {}, b""))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if "Content-Length" not in headers and "Transfer-Encoding" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if headers.get("Transfer-Encoding") == "chunked":
                    for start in range(0, len(body), 1 << 20):
                        chunk = body[start:start + (1 << 20)]
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(body)

            do_GET = do_POST = do_PUT = _serve

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.handle_error = lambda *args: None  # client aborts on oversized bodies
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return self.base + path

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    routes = {
        "/report.csv": (200, {"Content-Type": "text/csv"}, b"a,b\n1,2\n"),
        "/r": (302, {"Location": "/report.csv"}, b""),
        "/escape": (302, {"Location": "/outside.csv"}, b""),
        "/outside.csv": (200, {"Content-Type": "text/csv"}, b"x\n"),
        "/spoof.pdf": (200, {"Content-Type": "application/pdf"}, b"not,a,pdf\n"),
        "/big.txt": (200, {"Content-Type": "text/plain"}, b"a" * (dl.MAX_FILE_BYTES + 1)),
        "/big-chunked.txt": (200, {"Content-Type": "text/plain", "Transfer-Encoding": "chunked"},
                             b"a" * (dl.MAX_FILE_BYTES + 1)),
        "/page": (200, {"Content-Type": "text/html"}, b"<html><form method=post enctype=multipart/form-data>"
                  b"<input type=file name=f></form></html>"),
        "/notes.docx": (200, {"Content-Type": DOCX}, _ooxml("word/document.xml")),
        "/gz.txt": (200, {"Content-Type": "text/plain", "Content-Encoding": "gzip"}, b"\x1f\x8b"),
    }
    fixture = _Server(routes)
    yield fixture
    fixture.close()


class _Allowed:
    allowed = True


def _grant(urls, redirect_urls=()):
    """Offline fixture grant: admits the plain-HTTP 127.0.0.1 fixture origin only."""
    return DownloadGrant.model_validate({"urls": list(urls), "redirect_urls": list(redirect_urls)},
                                        context={"allow_local_fixture": True})


def _loopback(host, port):
    return ["127.0.0.1"]


def _transport(grant, **kwargs):
    # Loopback fixture origin only; the configured site policy is exercised separately.
    return OwnedDownloadTransport(grant, allow_local_fixture=True, site_policy=lambda url: _Allowed, **kwargs)


# ---------------------------------------------------------------- content policy

@pytest.mark.parametrize("data,claimed,name,expected", [
    (b"%PDF-1.7\n", "application/pdf", "a.pdf", "application/pdf"),
    (_ooxml("word/document.xml"), DOCX, "a.docx", DOCX),
    (_ooxml("xl/workbook.xml"), "application/octet-stream", "a.xlsx", XLSX),
    (b"a,b\n", "text/csv; charset=utf-8", "a.csv", "text/csv"),
    (b"hello\n", "", "a.txt", "text/plain"),
    (_ooxml("word/document.xml", ("word/_rels/document.xml.rels", _HYPERLINK_RELS)), DOCX, "a.docx", DOCX),
])
def test_allowlisted_content_is_identified_from_bytes(data, claimed, name, expected):
    assert checked_mime(data, claimed, name) == expected
    assert safe_filename(name, expected) == name


@pytest.mark.parametrize("data,claimed,name,code", [
    (b"a,b\n", "application/pdf", "a.pdf", "download_mime_mismatch"),
    (b"%PDF-1.7", DOCX, "a.docx", "download_mime_mismatch"),
    (b"\x7fELF\x02\x01", "application/octet-stream", "a.txt", "download_executable_refused"),
    (b"MZ\x90\x00", "text/plain", "a.txt", "download_executable_refused"),
    (b"#!/bin/sh\nrm -rf /\n", "text/plain", "a.txt", "download_executable_refused"),
    (b"<html><script>x</script>", "text/plain", "a.txt", "download_content_unsupported"),
    (b"PK\x03\x04garbage", DOCX, "a.docx", "download_content_unsupported"),
    (_ooxml("other.xml"), DOCX, "a.docx", "download_content_unsupported"),
    (b"\x00\x01binary", "text/plain", "a.txt", "download_content_unsupported"),
    (_ooxml("word/document.xml", ("word/vbaProject.bin", "x")), DOCX, "a.docx", "download_active_content_refused"),
    (_ooxml("xl/workbook.xml", ("xl/embeddings/oleObject1.bin", "x")), XLSX, "a.xlsx",
     "download_active_content_refused"),
    (_ooxml("xl/workbook.xml", ("xl/externalLinks/externalLink1.xml", "<x/>")), XLSX, "a.xlsx",
     "download_active_content_refused"),
    (_ooxml("word/document.xml", ("word/activeX/activeX1.xml", "<x/>")), DOCX, "a.docx",
     "download_active_content_refused"),
    (_ooxml("word/document.xml", types="<Types><Default ContentType='application/vnd.ms-word.document.macroEnabled.main+xml'/></Types>"),
     DOCX, "a.docx", "download_active_content_refused"),
    (_ooxml("word/document.xml", ("word/_rels/settings.xml.rels", _TEMPLATE_RELS)), DOCX, "a.docx",
     "download_active_content_refused"),
    ("\ufeff<html><body>x</body></html>".encode(), "text/plain", "a.txt", "download_content_unsupported"),
    ("\ufeff  <script>alert(1)</script>".encode(), "text/plain", "a.txt", "download_content_unsupported"),
    (b"name,value\nx,<iframe src=//evil>\n", "text/csv", "a.csv", "download_content_unsupported"),
    (b"a,b\n<img src=x onerror=alert(1)>\n", "text/csv", "a.csv", "download_content_unsupported"),
    (b"%!PS-Adobe-3.0\n", "text/plain", "a.txt", "download_content_unsupported"),
    (b"{\\rtf1\\ansi x}", "text/plain", "a.txt", "download_content_unsupported"),
    ("\ufeff#!/bin/sh\n".encode(), "text/plain", "a.txt", "download_executable_refused"),
    (b"\xcf\xfa\xed\xfe\x07", "application/octet-stream", "a.txt", "download_executable_refused"),
])
def test_mime_spoof_and_unexpected_content_are_refused(data, claimed, name, code):
    with pytest.raises(DownloadError) as error:
        checked_mime(data, claimed, name)
    assert error.value.code == code


@pytest.mark.parametrize("name,code", [
    ("../escape.csv", "download_filename_unsafe"), ("dir/a.csv", "download_filename_unsafe"),
    ("..\\a.csv", "download_filename_unsafe"), ("/etc/passwd.csv", "download_filename_unsafe"),
    (".bashrc.csv", "download_filename_unsafe"), ("a\x00.csv", "download_filename_unsafe"),
    ("", "download_filename_unsafe"), ("report.csv.exe", "download_extension_mismatch"),
    ("report.sh", "download_extension_mismatch"),
])
def test_path_escape_and_disguised_names_are_refused(name, code):
    with pytest.raises(DownloadError) as error:
        safe_filename(name, "text/csv")
    assert error.value.code == code


# ---------------------------------------------------------------- quarantine

def test_quarantine_is_read_only_inspectable_owner_scoped_and_revocable(tmp_path):
    quarantine = DownloadQuarantine(tmp_path)
    ref = quarantine.admit("job-1", owner=OWNER, source_url="https://files.example/r",
                           final_url="https://files.example/a.csv", redirect_chain=["https://files.example/r"],
                           claimed_type="text/csv", suggested_filename="a.csv", data=b"a,b\n")
    assert ref.permission == "read_only_quarantined" and ref.mime == "text/csv" and ref.size == 4
    [metadata] = quarantine.list("job-1")
    stored = tmp_path / metadata["file_path"]
    mode = stored.stat().st_mode
    assert stat.S_IMODE(mode) == 0o400 and not mode & 0o111
    assert stat.S_IMODE(stored.parent.stat().st_mode) == 0o700
    assert metadata["source"] == {"requested_url": "https://files.example/r",
                                  "final_url": "https://files.example/a.csv",
                                  "redirect_chain": ["https://files.example/r"], "claimed_type": "text/csv"}
    assert metadata["artifact_record"]["content_sha256"] == ref.digest
    view = quarantine.inspect(ref.artifact_id, owner=OWNER)
    assert view["executable"] is False and view["executed"] is False
    assert view["preview"] == {"kind": "text", "text": "a,b\n", "truncated": False, "untrusted_content": True}
    with pytest.raises(DownloadError, match="download_not_found"):
        quarantine.inspect(ref.artifact_id, owner=OTHER)
    with pytest.raises(DownloadError, match="download_not_found"):
        quarantine.revoke(ref.artifact_id, owner=OTHER)
    assert quarantine.revoke(ref.artifact_id, owner=OWNER) is True
    assert not stored.exists() and quarantine.list("job-1") == []
    with pytest.raises(DownloadError, match="download_not_found"):
        quarantine.inspect(ref.artifact_id, owner=OWNER)


def test_quarantine_limits_files_and_bytes_and_detects_tamper(tmp_path):
    quarantine = DownloadQuarantine(tmp_path)
    kwargs = dict(owner=OWNER, source_url="u", final_url="u", redirect_chain=[], claimed_type="text/plain")
    with pytest.raises(DownloadError, match="download_oversized"):
        quarantine.admit("job-1", suggested_filename="a.txt", data=b"a" * (dl.MAX_FILE_BYTES + 1), **kwargs)
    first = quarantine.admit("job-1", suggested_filename="a.txt", data=b"one", **kwargs)
    quarantine.admit("job-1", suggested_filename="b.txt", data=b"two", **kwargs)
    with pytest.raises(DownloadError, match="download_file_limit"):
        quarantine.admit("job-1", suggested_filename="c.txt", data=b"three", **kwargs)
    [stored] = [tmp_path / m["file_path"] for m in quarantine.list("job-1") if m["ref"]["digest"] == first.digest]
    os.chmod(stored, 0o600)
    stored.write_bytes(b"owe")  # same size, different bytes
    with pytest.raises(DownloadError, match="download_readback_mismatch"):
        quarantine.inspect(first.artifact_id, owner=OWNER)


_ADMIT = dict(owner=OWNER, source_url="u", final_url="u", redirect_chain=[], claimed_type="text/plain")


def _job_path(root, job_id="job-1"):
    return root / dl.QUARANTINE_PREFIX / hashlib.sha256(job_id.encode()).hexdigest()[:32]


@pytest.mark.parametrize("component", ["artifacts", "browser-downloads", "quarantine", "job"])
def test_quarantine_refuses_symlinked_components(tmp_path, component):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    path = {"artifacts": tmp_path / "artifacts", "browser-downloads": tmp_path / "artifacts/browser-downloads",
            "quarantine": tmp_path / dl.QUARANTINE_PREFIX, "job": _job_path(tmp_path)}[component]
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.symlink_to(target)
    with pytest.raises(DownloadError, match="download_quarantine_untrusted"):
        DownloadQuarantine(tmp_path).admit("job-1", suggested_filename="a.txt", data=b"x", **_ADMIT)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("component,mode", [("artifacts", 0o777), ("browser-downloads", 0o755),
                                            ("quarantine", 0o750), ("job", 0o777)])
def test_quarantine_refuses_shared_components(tmp_path, component, mode):
    DownloadQuarantine(tmp_path).admit("job-1", suggested_filename="a.txt", data=b"x", **_ADMIT)
    path = {"artifacts": tmp_path / "artifacts", "browser-downloads": tmp_path / "artifacts/browser-downloads",
            "quarantine": tmp_path / dl.QUARANTINE_PREFIX, "job": _job_path(tmp_path)}[component]
    os.chmod(path, mode)
    with pytest.raises(DownloadError, match="download_quarantine_untrusted"):
        DownloadQuarantine(tmp_path).admit("job-1", suggested_filename="b.txt", data=b"y", **_ADMIT)


def _forge(tmp_path, mutate):
    quarantine = DownloadQuarantine(tmp_path)
    ref = quarantine.admit("job-1", suggested_filename="a.txt", data=b"secret", **_ADMIT)
    sidecar = _job_path(tmp_path) / f"{ref.artifact_id}.json"
    metadata = json.loads(sidecar.read_text())
    mutate(metadata)
    os.chmod(sidecar, 0o600)
    sidecar.write_text(json.dumps(metadata))
    return quarantine, ref


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(file_path="../../../../etc/passwd"),
    lambda m: m.update(owner_principal_id="operator-2"),
    lambda m: m.update(file_path="artifacts/browser-downloads/quarantine/" + "0" * 32 + "/x-a.txt"),
    lambda m: m.update(filename="../a.txt"),
    lambda m: m.update(job_id="job-2"),
    lambda m: m["ref"].update(size=1),
])
def test_forged_sidecar_is_refused_without_reading_outside_the_quarantine(tmp_path, mutate):
    quarantine, ref = _forge(tmp_path, mutate)
    with pytest.raises(DownloadError, match="download_quarantine_untrusted"):
        quarantine.inspect(ref.artifact_id, owner=OWNER)
    with pytest.raises(DownloadError, match="download_quarantine_untrusted"):
        quarantine.revoke(ref.artifact_id, owner=OWNER)


def test_resealed_sidecar_pointing_elsewhere_is_still_refused(tmp_path):
    # Even with a recomputed self-digest the path must equal the one derived
    # from the job, digest and filename, and the artifact id must match.
    def reseal(metadata):
        metadata.pop("sidecar_digest")
        metadata["file_path"] = "artifacts/other.txt"
        metadata["sidecar_digest"] = dl._canonical_digest(metadata)
    quarantine, ref = _forge(tmp_path, reseal)
    with pytest.raises(DownloadError, match="download_quarantine_untrusted"):
        quarantine.inspect(ref.artifact_id, owner=OWNER)


def test_data_file_replaced_by_symlink_is_refused(tmp_path):
    quarantine = DownloadQuarantine(tmp_path)
    ref = quarantine.admit("job-1", suggested_filename="a.txt", data=b"secret", **_ADMIT)
    [metadata] = quarantine.list("job-1")
    stored = tmp_path / metadata["file_path"]
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    stored.unlink()
    stored.symlink_to(outside)
    with pytest.raises(DownloadError, match="download_quarantine_untrusted"):
        quarantine.inspect(ref.artifact_id, owner=OWNER)


def test_concurrent_admissions_never_exceed_two_files_and_leave_no_orphans(tmp_path):
    import concurrent.futures
    quarantine = DownloadQuarantine(tmp_path)

    def admit(index):
        try:
            return quarantine.admit("job-1", suggested_filename=f"f{index}.txt", data=f"v{index}".encode(), **_ADMIT)
        except DownloadError as exc:
            return exc.code
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = list(pool.map(admit, range(8)))
    assert sum(isinstance(item, dl.BrowserFileRef) for item in results) == 2
    assert results.count("download_file_limit") == 6
    names = sorted(path.name for path in _job_path(tmp_path).iterdir())
    assert len(names) == 4 and sum(name.endswith(".json") for name in names) == 2


def test_failed_sidecar_write_leaves_no_orphan_file(tmp_path, monkeypatch):
    quarantine = DownloadQuarantine(tmp_path)
    real = dl._write_new

    def failing(dir_fd, name, payload):
        if name.endswith(".json"):
            raise OSError("disk full")
        return real(dir_fd, name, payload)
    monkeypatch.setattr(dl, "_write_new", failing)
    with pytest.raises(OSError):
        quarantine.admit("job-1", suggested_filename="a.txt", data=b"x", **_ADMIT)
    assert list(_job_path(tmp_path).iterdir()) == []


# ---------------------------------------------------------------- owned transport

async def test_transport_follows_only_granted_redirects(server):
    grant = _grant(urls=[server.url("/r")], redirect_urls=[server.url("/report.csv")])
    fetched = await _transport(grant).fetch(server.url("/r"))
    assert fetched.data == b"a,b\n1,2\n" and fetched.redirect_chain == [server.url("/r")]
    assert fetched.final_url == server.url("/report.csv") and fetched.filename == "report.csv"


async def test_redirect_outside_grant_aborts_before_contacting_the_target(server):
    grant = _grant(urls=[server.url("/escape")])
    transport = _transport(grant)
    with pytest.raises(DownloadError, match="download_redirect_outside_grant"):
        await transport.fetch(server.url("/escape"))
    assert server.hits.get("GET /outside.csv") is None
    assert transport.contacts == [server.url("/escape")]


@pytest.mark.parametrize("path", ["/big.txt", "/big-chunked.txt"])
async def test_oversized_bytes_abort_before_adoption(server, path):
    grant = _grant(urls=[server.url(path)])
    with pytest.raises(DownloadError, match="download_oversized"):
        await _transport(grant).fetch(server.url(path))


async def test_url_outside_grant_is_never_contacted(server):
    transport = _transport(_grant(urls=[server.url("/report.csv")]))
    with pytest.raises(DownloadError, match="download_url_outside_grant"):
        await transport.fetch(server.url("/outside.csv"))
    assert transport.contacts == [] and server.hits == {}


async def test_external_and_private_sockets_are_denied_outside_fixture_mode(server):
    grant = _grant([server.url("/report.csv"), "https://intranet.example/a.csv"])
    transport = OwnedDownloadTransport(grant, site_policy=lambda url: _Allowed)
    with pytest.raises(DownloadError, match="download_https_required"):
        await transport.fetch(server.url("/report.csv"))
    for address in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "fd00::1"):
        transport.resolver = lambda host, port, _ip=address: [_ip]
        with pytest.raises(DownloadError, match="download_private_address_denied"):
            await transport.fetch("https://intranet.example/a.csv")
    transport.resolver = lambda host, port: ["93.184.216.34", "10.0.0.5"]  # one private answer is enough
    with pytest.raises(DownloadError, match="download_private_address_denied"):
        await transport.fetch("https://intranet.example/a.csv")
    assert server.hits == {} and transport.contacts == []


async def test_site_policy_and_encoded_bodies_are_refused(server, monkeypatch):
    class Denied:
        allowed = False
    grant = _grant(urls=[server.url("/report.csv"), server.url("/gz.txt")])
    transport = OwnedDownloadTransport(grant, allow_local_fixture=True, site_policy=lambda url: Denied)
    with pytest.raises(DownloadError, match="download_site_policy_blocked"):
        await transport.fetch(server.url("/report.csv"))
    assert server.hits == {}
    with pytest.raises(DownloadError, match="download_encoding_unsupported"):
        await _transport(grant).fetch(server.url("/gz.txt"))


async def test_fetch_connects_to_the_resolved_address_with_the_original_host(server):
    seen = []
    url = server.url("/report.csv").replace("127.0.0.1", "files.localtest")
    # A named fixture host over HTTP is outside even the fixture grant rules; construct it directly.
    grant = DownloadGrant.model_construct(urls=[url], redirect_urls=[])
    transport = _transport(grant, resolver=lambda host, port: seen.append((host, port)) or ["127.0.0.1"])
    fetched = await transport.fetch(url)
    assert seen == [("files.localtest", server.httpd.server_port)] and fetched.data == b"a,b\n1,2\n"


def test_grant_rejects_credentials_fragments_and_overbroad_requests():
    for bad in (["https://user:pw@files.example/a.csv"], ["https://files.example/a.csv#x"], ["file:///etc/passwd"],
                ["http://files.example/a.csv"], ["https://files.example:8443/a.csv"],
                ["https://files.example/a\r\n.csv"], ["https://files.example/../a.csv"],
                ["https://files.example/a.csv", "https://files.example/a.csv"],
                ["http://127.0.0.1:8000/a.csv"],  # the loopback fixture origin is never admitted by the API
                ["https://a.example/1", "https://a.example/2", "https://a.example/3"]):
        with pytest.raises(ValueError):
            DownloadGrant(urls=bad)
    assert DownloadGrant(urls=["https://files.example/a.csv"]).urls == ["https://files.example/a.csv"]


# ---------------------------------------------------------------- browser task (offline fake browser)

class _FakeRequest:
    def __init__(self, url, method="GET", body=None):
        self.url, self.method, self.post_data_buffer = url, method, body


class _FakeRoute:
    def __init__(self, request):
        self.request, self.aborted, self.fulfilled = request, None, None

    async def abort(self, code):
        self.aborted = code

    async def fulfill(self, **kwargs):
        self.fulfilled = kwargs


class _FakeDownload:
    def __init__(self, url):
        self.url = url


class _FakePage:
    def __init__(self, context):
        self.context, self.handlers, self.url = context, {}, "about:blank"

    def on(self, event, handler):
        self.handlers[event] = handler

    async def goto(self, url, **_):
        route = _FakeRoute(_FakeRequest(url))
        await self.context.handler(route)
        self.context.routes.append(route)
        if route.fulfilled and "attachment" in route.fulfilled["headers"]["content-disposition"]:
            self.handlers["download"](_FakeDownload(url))
            raise RuntimeError("Download is starting")


class _FakeContext:
    def __init__(self):
        self.handler, self.routes, self.closed, self.options = None, [], False, None

    async def route(self, pattern, handler):
        assert pattern == "**/*"
        self.handler = handler

    async def new_page(self):
        self.page = _FakePage(self)
        return self.page

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.context = _FakeContext()
        self.closed = False

    async def new_context(self, **options):
        self.context.options = options
        return self.context

    async def close(self):
        self.closed = True


async def test_browser_download_adopts_granted_file_and_rejects_uploads_before_contact(server, tmp_path):
    browser = _FakeBrowser()
    grant = _grant(urls=[server.url("/r"), server.url("/notes.docx")], redirect_urls=[server.url("/report.csv")])
    transport = _transport(grant)
    outcome = await download_public_files(grant, job_id="job-1", owner=OWNER, quarantine=DownloadQuarantine(tmp_path),
                                          browser_launcher=lambda: browser, transport=transport)
    assert [ref.mime for ref in outcome.files] == ["text/csv", DOCX] and outcome.blocked == []
    assert browser.context.options["java_script_enabled"] is False
    assert browser.context.options["permissions"] == []
    assert browser.closed and browser.context.closed
    # Browser-delivered bytes are always an attachment, never a rendered document.
    assert all(r.fulfilled["headers"]["content-type"] == "application/octet-stream" for r in browser.context.routes)

    # An upload (POST/PUT body or any non-GET) is aborted before the owned transport contacts anything.
    before = (list(transport.contacts), dict(server.hits))
    for request in (_FakeRequest(server.url("/r"), "POST", b"file-bytes"), _FakeRequest(server.url("/r"), "PUT"),
                    _FakeRequest(server.url("/r"), "GET", b"smuggled")):
        route = _FakeRoute(request)
        await browser.context.handler(route)
        assert route.aborted == "blockedbyclient" and route.fulfilled is None
    assert (transport.contacts, server.hits) == before
    assert not any(key.startswith(("POST", "PUT")) for key in server.hits)


async def test_browser_download_blocks_spoof_escape_and_html_without_adoption(server, tmp_path):
    browser = _FakeBrowser()
    grant = _grant(urls=[server.url("/spoof.pdf"), server.url("/escape")])
    quarantine = DownloadQuarantine(tmp_path)
    outcome = await download_public_files(grant, job_id="job-2", owner=OWNER, quarantine=quarantine,
                                          browser_launcher=lambda: browser, transport=_transport(grant))
    assert outcome.files == []
    assert {item["code"] for item in outcome.blocked} == {"download_mime_mismatch", "download_redirect_outside_grant"}
    assert quarantine.list("job-2") == [] and server.hits.get("GET /outside.csv") is None
    page_grant = _grant(urls=[server.url("/page")])
    outcome = await download_public_files(page_grant, job_id="job-3", owner=OWNER, quarantine=quarantine,
                                          browser_launcher=_FakeBrowser, transport=_transport(page_grant))
    assert outcome.files == [] and outcome.blocked[0]["code"] == "download_content_unsupported"
    assert not any(key.startswith("POST") for key in server.hits)


# ---------------------------------------------------------------- interaction grammar

@pytest.mark.parametrize("kind", sorted(__import__("src.browser.interaction_contracts",
                                                   fromlist=["UPLOAD_ACTION_KINDS"]).UPLOAD_ACTION_KINDS))
def test_profiled_interaction_refuses_upload_actions_before_any_page_event(kind):
    from pydantic import ValidationError
    from src.browser.interaction_contracts import BrowserActionV2
    with pytest.raises(ValidationError, match="browser_upload_unsupported"):
        BrowserActionV2.model_validate({"kind": kind, "expected_page_revision": "0" * 64})


async def test_opened_file_chooser_blocks_every_later_action_without_touching_the_page():
    from src.browser.interaction_contracts import BrowserActionV2, InteractionError
    from src.browser.task_runner import ProfiledInteractionPage
    intents: list[dict] = []

    async def authority():
        return None

    async def intent(value):
        intents.append(value)

    page = ProfiledInteractionPage(authority=authority, intent=intent, result=intent)
    page.file_chooser_opened = True
    page.page = None  # any page access would raise a different error
    with pytest.raises(InteractionError) as error:
        await page.apply(BrowserActionV2(kind="click", locator_ref="node-" + "0" * 32,
                                         expected_page_revision="0" * 64))
    assert error.value.code == "browser_upload_unsupported"


# ---------------------------------------------------------------- real Chromium (explicit integration proof)

@pytest.mark.skipif(os.environ.get("SERAPH_RUN_REAL_BROWSER") != "1",
                    reason="real Chromium download is an explicit integration proof")
async def test_real_chromium_downloads_into_quarantine_and_never_contacts_uploads(server, tmp_path):
    from playwright.async_api import async_playwright

    class Handle:
        async def new_context(self, **kwargs):
            return await self.browser.new_context(**kwargs)

        async def close(self):
            await self.browser.close()
            await self.playwright.stop()

    async def launcher():
        handle = Handle()
        handle.playwright = await async_playwright().start()
        handle.browser = await handle.playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        return handle

    grant = _grant(urls=[server.url("/r"), server.url("/page")], redirect_urls=[server.url("/report.csv")])
    quarantine = DownloadQuarantine(tmp_path)
    outcome = await asyncio.wait_for(download_public_files(
        grant, job_id="job-real", owner=OWNER, quarantine=quarantine, browser_launcher=launcher,
        transport=_transport(grant)), 90)
    assert [ref.mime for ref in outcome.files] == ["text/csv"]
    assert [item["code"] for item in outcome.blocked] == ["download_content_unsupported"]
    view = quarantine.inspect(outcome.files[0].artifact_id, owner=OWNER)
    assert view["file_mode"] == "0o400" and view["executable"] is False and view["preview"]["text"] == "a,b\n1,2\n"
    assert not any(key.startswith(("POST", "PUT")) for key in server.hits)


# ---------------------------------------------------------------- API (offline, authenticated operator)

async def test_download_api_adopts_inspects_and_revokes_under_the_browser_lane(server, tmp_path, monkeypatch, async_db):
    import httpx
    from fastapi import FastAPI
    from config.settings import settings
    from src.api import auth, browser as browser_api
    from src.auth.middleware import OperatorAuthMiddleware
    from src.browser.task_lane import try_acquire_browser_task_lane

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "download-root")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    auth._reset_login_throttle_for_tests()
    real = dl.download_public_files
    lanes = []

    async def offline(grant, **kwargs):
        lanes.append(kwargs["lane"].acquired and kwargs["witness"] is not None)
        return await real(grant, browser_launcher=_FakeBrowser, transport=_transport(grant), **kwargs)

    class FixtureGrant(DownloadGrant):
        def __init__(self, **data):
            self.__pydantic_validator__.validate_python(data, self_instance=self,
                                                        context={"allow_local_fixture": True})

    monkeypatch.setattr(dl, "download_public_files", offline)
    monkeypatch.setattr(dl, "DownloadGrant", FixtureGrant)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(browser_api.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"origin": "http://localhost:3001"}) as client:
        assert (await client.post("/api/auth/login", json={"password": "download-root"})).status_code == 200
        body = {"request_key": "0b5c1f3e-1d2a-4c3b-9e8f-7a6b5c4d3e2f", "urls": [server.url("/r")],
                "redirect_urls": [server.url("/report.csv")]}
        response = await client.post("/api/browser/downloads", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert lanes == [True] and result["cleanup_verified"] is True and result["blocked"] == []
        replay = (await client.post("/api/browser/downloads", json=body)).json()
        assert replay["replayed"] is True and replay["files"] == result["files"] and lanes == [True]
        [ref] = result["files"]
        assert ref["mime"] == "text/csv" and ref["permission"] == "read_only_quarantined"
        lane = try_acquire_browser_task_lane(tmp_path)  # released after the task
        assert lane is not None
        lane.release()
        view = (await client.get(f"/api/browser/downloads/{ref['artifact_id']}")).json()
        assert view["executed"] is False and view["preview"]["text"] == "a,b\n1,2\n"
        monkeypatch.setattr(dl, "DownloadGrant", DownloadGrant)
        assert (await client.post("/api/browser/downloads", json={**body, "urls": ["file:///etc/passwd"]})
                ).json()["detail"]["code"] == "download_grant_invalid"
        assert (await client.post("/api/browser/downloads", json=body)).json()["detail"]["code"] \
            == "download_grant_invalid"  # the loopback fixture origin is never admitted by the API
        assert (await client.post("/api/browser/downloads", json={**body, "upload": "x"})).status_code == 422
        assert (await client.delete(f"/api/browser/downloads/{ref['artifact_id']}")).json() == {"revoked": True}
        assert (await client.get(f"/api/browser/downloads/{ref['artifact_id']}")).status_code == 404


# ---------------------------------------------------------------- lane, cancellation, replay

async def _blocking_launcher():
    """The launcher never returns a handle: the launch result is unknown."""
    await asyncio.Event().wait()


class _HangingCloseBrowser(_FakeBrowser):
    def __init__(self):
        super().__init__()
        self.context.close = self._hang

    async def _hang(self):
        await asyncio.Event().wait()


def _fresh_lane(root):
    from src.browser.task_lane import try_acquire_browser_task_lane
    return try_acquire_browser_task_lane(root)


async def _run(tmp_path, launcher, grant, **options):
    return await dl.run_download_request(grant, job_id="browser-download-x", owner=OWNER, workspace_root=tmp_path,
                                         quarantine=DownloadQuarantine(tmp_path), browser_launcher=launcher,
                                         transport=_transport(grant), **options)


async def test_successful_request_confirms_positive_cleanup_and_frees_the_lane(server, tmp_path):
    grant = _grant([server.url("/r")], [server.url("/report.csv")])
    result = await _run(tmp_path, _FakeBrowser, grant)
    assert result["cleanup_verified"] is True and result["replayed"] is False
    lane = _fresh_lane(tmp_path)
    assert lane is not None
    lane.release()


@pytest.mark.parametrize("phase", ["launch", "close"])
async def test_cancel_during_launch_or_close_keeps_the_lane_quarantined(server, tmp_path, phase):
    from src.browser.task_lane import browser_task_lane_wait_reason
    grant = _grant([server.url("/r")], [server.url("/report.csv")])
    launcher = _blocking_launcher if phase == "launch" else _HangingCloseBrowser
    task = asyncio.create_task(_run(tmp_path, launcher, grant))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _fresh_lane(tmp_path) is None
    assert browser_task_lane_wait_reason(tmp_path) == "browser_cleanup_required"
    # The interrupted request is never re-run by a replay.
    with pytest.raises(DownloadError, match="download_request_outcome_unknown"):
        await _run(tmp_path, _FakeBrowser, grant)


async def test_unverified_close_quarantines_the_lane(server, tmp_path):
    from src.browser.task_lane import browser_task_lane_wait_reason

    class FailingClose(_FakeBrowser):
        async def close(self):
            raise RuntimeError("driver lost")
    grant = _grant([server.url("/r")], [server.url("/report.csv")])
    result = await _run(tmp_path, FailingClose, grant)
    assert result["cleanup_verified"] is False
    assert browser_task_lane_wait_reason(tmp_path) == "browser_cleanup_required"


async def test_overall_deadline_bounds_the_browser_work(server, tmp_path):
    grant = _grant([server.url("/r")], [server.url("/report.csv")])
    with pytest.raises(DownloadError, match="download_deadline_exceeded"):
        await _run(tmp_path, _blocking_launcher, grant, deadline_seconds=0.2)
    with pytest.raises(DownloadError, match="download_deadline_exceeded"):
        await _run(tmp_path, _FakeBrowser, grant)  # recorded failure replays without contact


async def test_replay_of_a_request_key_returns_the_outcome_without_recontact(server, tmp_path):
    grant = _grant([server.url("/r")], [server.url("/report.csv")])
    first = await _run(tmp_path, _FakeBrowser, grant)
    hits = dict(server.hits)
    second = await _run(tmp_path, _blocking_launcher, grant)
    assert second["replayed"] is True and second["files"] == first["files"] and server.hits == hits
    with pytest.raises(DownloadError, match="download_request_key_body_changed"):
        await _run(tmp_path, _FakeBrowser, _grant([server.url("/notes.docx")]))
    with pytest.raises(DownloadError, match="download_not_found"):
        await dl.run_download_request(grant, job_id="browser-download-x", owner=OTHER, workspace_root=tmp_path,
                                      quarantine=DownloadQuarantine(tmp_path))
    assert server.hits == hits


async def test_busy_lane_refuses_before_any_contact(server, tmp_path):
    grant = _grant([server.url("/r")], [server.url("/report.csv")])
    holder = _fresh_lane(tmp_path)
    try:
        with pytest.raises(DownloadError, match="browser_task_lane_busy"):
            await _run(tmp_path, _FakeBrowser, grant)
    finally:
        holder.release()
    assert server.hits == {} and DownloadQuarantine(tmp_path).job_record("browser-download-x") is None
