"""#1008 acceptance: guardian goal research through replaceable Cordis roles.

Real Auth, SQLite, durable jobs, programme authority, artifact owners and the
real reviewed Cordis child (research service, goal-plan provider, guardian
consumer and source-format providers). Public search/source HTTP and model
inference use fixed in-memory transport responses; Python socket connects and
DNS are denied, and the child has no network configuration.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import socket

import httpx
import pytest
from fastapi import FastAPI

from tests.cordis_research import needs_cordis, package_variant, research_host
from tests.test_inference_accounting import accounting_db, setup_configuration  # noqa: F401
from tests.test_research_native_vertical import ResponseBytes, real_auth  # noqa: F401
from src.auth.middleware import OperatorAuthMiddleware
from src.workflows.job_runtime import DurableJobRepository

pytestmark = [needs_cordis, pytest.mark.asyncio]

HTML = b"<html><head><style>p{}</style></head><body><h1>Release 2</h1><p>Shipped &amp; dated.</p><script>x()</script></body></html>"
RSS = (b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Release feed</title>'
       b'<item><title>Version 2 ships</title><link>https://example.com/v2</link><pubDate>Tue, 13 Oct 2026 10:00:00 GMT</pubDate>'
       b'<description>&lt;p&gt;Faster sync.&lt;/p&gt;</description></item></channel></rss>')
ATOM = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Release log</title><entry><title>Entry one</title>'
        b'<link href="https://example.com/e1"/><updated>2026-10-14T00:00:00Z</updated><summary>Dated entry</summary></entry></feed>')
# ~250 KiB sources (below max_source_bytes 262144) whose normalized text fits the 64 KiB snapshot.
BIG_HTML = (b"<html><head><script>" + b"var a = 1;" * 24_400 + b"</script></head><body><h1>Release 2</h1>"
            + b'<div class="c"><span></span></div>' * 400 + b"<p>Shipped &amp; dated.</p></body></html>")
BIG_RSS = (b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Release feed</title>'
           + b"".join(b"<item><title>Version %d ships</title><link>https://example.com/v%d</link><description><![CDATA["
                      % (i, i) + b'<div class="layout"><span class="x"></span></div>' * 129 + b"<p>Dated.</p>]]></description></item>"
                      for i in range(40)) + b"</channel></rss>")
# Feeds served as generic XML (#1083): a BOM-prefixed RSS under application/xml and
# an Atom feed with xhtml content under text/xml.
XHTML_ATOM = (b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Release log</title><entry><title>Entry one</title>'
              b'<link href="https://example.com/e1"/><updated>2026-10-14T00:00:00Z</updated><content type="xhtml">'
              b'<div xmlns="http://www.w3.org/1999/xhtml"><p>Dated</p><p>entry</p></div></content></entry></feed>')
FORMATS = {"html": ("text/html", HTML), "rss": ("application/rss+xml", RSS), "atom": ("application/atom+xml", ATOM),
           "xml_rss": ("application/xml", b"\xef\xbb\xbf" + RSS), "text_xml_atom": ("text/xml", XHTML_ATOM),
           "big_html": ("text/html", BIG_HTML), "big_rss": ("application/rss+xml", BIG_RSS)}


def _deny_sockets(monkeypatch):
    def denied(*_args, **_kwargs):
        raise RuntimeError("external transport denied in #1008 acceptance tests")
    for target, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"), (socket, "create_connection"),
                         (socket, "getaddrinfo")):
        monkeypatch.setattr(target, name, denied)


class Fixture:
    """Fixed in-memory public search/source and model responses; records every contact."""

    def __init__(self, source):
        self.source, self.gets, self.posts, self.calls, self.frames = source, [], [], [], []

    def observe(self, frame):
        self.frames.append((frame["method"], json.loads(json.dumps(frame["payload"]))))

    def methods(self):
        return [method for method, _ in self.frames]


@asynccontextmanager
async def discovery(accounting_db, monkeypatch, fixture, *, package_root=None, hooks=None):  # noqa: F811
    from config.settings import settings
    from src.api import auth, goals, model_fabric_settings
    from src.guardian.discovery_search import DiscoverySearch
    from src.guardian.goal_discovery import GoalDiscoveryService
    from src.guardian.goal_programmes import goal_programme_service
    from src.model_fabric.configuration import write_model_fabric_configuration
    root, _, factory = accounting_db
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30), profiles=(), policies=()),
        egress_revision=configured.egress_revision + 1))
    monkeypatch.setattr(settings, "browser_site_allowlist", "example.com")
    jobs = DurableJobRepository()
    await jobs.configure_inference_accounting(1000)

    class ModelBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            body = json.loads(request.content)
            answer = "CANARY_OK"
            if len(body["messages"]) > 1:
                fixture.calls.append(body)
                supplied = json.loads(body["messages"][1]["content"])["untrusted_public_data"]
                if supplied.get("task") == "plan_queries":
                    answer = json.dumps({"queries": ["public product release evidence"]})
                elif "manifest_ref" in supplied:
                    answer = json.dumps({"selected_result_ids": [supplied["results"][0]["result_id"]]})
                else:
                    source = supplied["untrusted_quoted_sources"][0]
                    answer = json.dumps({"schema_version": 1, "perspective": "Prepare a public Goal discovery brief",
                        "claims": [{"text": "The public fixture describes the selected release.",
                            "citations": [{key: source[key] for key in ("source_id", "first_line", "last_line", "span_sha256")}]}],
                        "uncertainty": ["Fixture execution is not a semantic truth claim."], "contradictions": [], "no_learning": True})
            payload = {"id": "fixture-1008", "choices": [{"message": {"role": "assistant", "content": answer}}],
                       "usage": {"cost": "0.000002", "prompt_tokens": 10, "completion_tokens": 10}}
            return httpx.Response(200, request=request, headers={"content-type": "application/json"},
                                  stream=ResponseBytes(json.dumps(payload).encode()))

    class PublicBoundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.scheme == "https" and "Authorization" not in request.headers and "Cookie" not in request.headers
            if request.method == "POST":
                fixture.posts.append(str(request.url))
                raw, mime = b'<html><a class="result__a" href="https://example.com/release">Release evidence</a></html>', "text/html"
            else:
                fixture.gets.append(str(request.url))
                mime, raw = fixture.source
            return httpx.Response(200, request=request, headers={"content-type": mime, "content-length": str(len(raw))},
                                  stream=ResponseBytes(raw))

    original_client = httpx.AsyncClient

    def clients(**kwargs):
        kwargs.setdefault("transport", ModelBoundary())
        return original_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    _deny_sockets(monkeypatch)

    def resolver(host, port):
        assert host in {"html.duckduckgo.com", "example.com"} and port == 443
        return ["93.184.216.34"]
    transport = PublicBoundary()
    async with research_host(jobs, package_root=package_root, observe=hooks or fixture.observe) as host:
        service = GoalDiscoveryService(jobs=jobs, search=DiscoverySearch(resolver=resolver, transport=transport),
                                       resolver=resolver, transport=transport, cordis=host)
        monkeypatch.setattr("src.guardian.goal_discovery.goal_discovery_service", service)
        from src.work_board.dispatcher import _dispatcher
        monkeypatch.setattr(_dispatcher, "goal_discovery", service)
        app = FastAPI()
        app.add_middleware(OperatorAuthMiddleware)
        app.include_router(auth.router, prefix="/api/auth")
        app.include_router(goals.router, prefix="/api")
        app.include_router(model_fabric_settings.router, prefix="/api")
        await goal_programme_service.start()
        await service.start()
        try:
            async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                       headers={"origin": "http://localhost:3001"}) as client:
                assert (await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})).status_code == 200
                enrolled = await client.post("/api/auth/ownership/enroll")
                for capability in ("text", "latency_ms", "health"):
                    canary = await client.post("/api/settings/model-fabric/canary", json={
                        "profile_id": "openrouter", "capability": capability, "timeout_seconds": 30})
                    assert canary.json()["outcome"] == "passed", canary.text
                goal_id = (await client.post("/api/goals", json={"title": "PRIVATE_GOAL_TITLE_CANARY",
                                                                  "description": "PRIVATE_GOAL_DESCRIPTION_CANARY"})).json()["id"]
                request = {"expected_goal_revision": 1, "expected_grant_revision": 0,
                           "public_brief": "Track public product release evidence", "budget": {"max_inference_microusd": 500}}
                base = f"/api/goals/{goal_id}/programmes"
                preview = await client.post(base + "/preview", json=request)
                programme = (await client.post(base + "/accept", json={**request, "review_digest": preview.json()["review_digest"],
                    "public_web_acknowledged": True, "local_artifacts_acknowledged": True,
                    "inference_ceiling_acknowledged": True})).json()
                assert (await client.post("/api/auth/logout")).status_code == 204

                async def revoke(revoked=True):
                    from src.db.models import OperatorIdentity
                    async with factory.accounting_sessions() as db:
                        identity = await db.get(OperatorIdentity, enrolled.json()["operator_identity_id"])
                        identity.revoked_at = datetime.now(timezone.utc) if revoked else None

                reader = {"ready": False}

                async def inspect():
                    # Fresh login plus explicit read-only owner recovery selects local inspection only.
                    if not reader["ready"]:
                        assert (await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})).status_code == 200
                        selection = {"selections": [{"kind": "goal", "record_id": goal_id}]}
                        preview = await client.post("/api/auth/ownership/recovery/preview", json=selection)
                        confirmed = await client.post("/api/auth/ownership/recovery/confirm", json={**selection,
                            "preview_digest": preview.json()["preview_digest"], "idempotency_key": "research-1008-reader",
                            "acknowledge_read_only": True})
                        assert confirmed.status_code == 200, confirmed.text
                        reader["ready"] = True
                    return await client.get(base + "/discovery")
                yield {"service": service, "jobs": jobs, "host": host, "goal_id": goal_id, "programme": programme,
                       "revoke": revoke, "inspect": inspect, "root": root, "client": client, "base": base}
        finally:
            await service.stop()
            await goal_programme_service.stop()


def _admit(env):
    return env["service"].admit(goal_id=env["goal_id"], programme_id=env["programme"]["id"], grant_revision=1)


async def _artifacts(env, job_id):
    from src.workflows.research_sources import physical_discovery_inputs
    return await physical_discovery_inputs(env["jobs"], job_id)


# ---------------------------------------------------------------- 1 + 2: journey, two formats, one consumer

@pytest.mark.parametrize("fmt", ["html", "rss", "atom", "xml_rss", "text_xml_atom"])
async def test_goal_plan_job_cordis_source_canonical_artifact_readback(accounting_db, real_auth, monkeypatch, fmt):  # noqa: F811
    fixture = Fixture(FORMATS[fmt])
    async with discovery(accounting_db, monkeypatch, fixture) as env:
        job = await _admit(env)
        assert job["status"] == "queued"
        # Planning provider -> research service -> Python validation, one planning job.
        assert fixture.methods() == ["artifacts.read", "artifacts.stage"]
        from src.runtime_plugins.dispatch import read_context
        from src.runtime_plugins.ownership import RuntimeCompositionBinding
        plan_job_id = "goal-discovery-plan:" + job["job_id"].split(":", 1)[1]
        plan_job = await env["jobs"].get_job(plan_job_id)
        assert plan_job["status"] == "succeeded" and "no_learning" in plan_job["result"]["summary"]
        async with env["jobs"]._session() as db:
            plan_run, discovery_run = await env["jobs"]._fetch(db, plan_job_id), await env["jobs"]._fetch(db, job["job_id"])
            sealed = read_context(plan_run)["result"]
        assert job["declared_authority"]["plan_ref"] == sealed["plan_ref"]
        # Plan provider, source providers, config and composition are pinned by the reviewed build/profile.
        for run in (plan_run, discovery_run):
            binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
            assert binding.host_composition_digest == env["host"].reviewed.composition_digest
            assert binding.host_package_digest == env["host"].reviewed.package_digest
        assert "seraph.source-extraction.v1" in [item.runtime_domain for item in binding.dependency_vector]
        staged_plan = json.loads(fixture.frames[1][1]["content"])
        assert staged_plan["steps"][2]["capability_id"] == "source.public-extract.v1"
        done = await env["service"].run(job["job_id"])
        assert done["status"] == "succeeded", done
        # Guardian consumer: request, job, raw source (one chunk + end chunk), snapshot stage + readback, final job.
        assert fixture.methods()[2:] == ["artifacts.read", "tasks.inspect", "artifacts.read", "artifacts.read",
                                         "artifacts.stage", "artifacts.read", "tasks.inspect"]
        assert len(fixture.gets) == 1 and len(fixture.posts) == 1 and len(fixture.calls) == 3
        witness = await _artifacts(env, job["job_id"])
        snapshot = next(a for a in witness.artifacts.values() if a["kind"] == "snapshot")["parsed"]
        expected = {"html": ["Release 2", "Shipped & dated."],
                    "rss": ["Feed: Release feed", "Item: Version 2 ships", "Link: https://example.com/v2",
                            "Published: Tue, 13 Oct 2026 10:00:00 GMT", "Summary: Faster sync."],
                    "atom": ["Feed: Release log", "Item: Entry one", "Link: https://example.com/e1",
                             "Published: 2026-10-14T00:00:00Z", "Summary: Dated entry"]}
        expected = {**expected, "xml_rss": expected["rss"], "text_xml_atom": expected["atom"]}[fmt]
        assert snapshot.lines == expected and snapshot.mime == FORMATS[fmt][0]
        request = json.loads(next(a for a in witness.artifacts.values() if a["kind"] == "extraction")["content"])
        reviewed = env["host"].reviewed
        assert request["pins"]["source_provider_digest"] == reviewed.plugin_digests["seraph.source-extraction.v1"]
        assert request["pins"]["composition_digest"] == reviewed.composition_digest
        raw = json.loads(next(a for a in witness.artifacts.values() if a["kind"] == "raw_source")["content"])
        assert set(raw) == {"schema_version", "source_slot", "media_type", "body"}  # no URL, path or credential
        brief = next(a for a in witness.artifacts.values() if a["kind"] == "brief")["parsed"]
        assert brief["coverage"]["outcome_state"] == "findings" and brief["coverage"]["no_learning"] is True
        assert brief["findings"][0]["evidence_status"] == "mechanically_verified"
        history = await env["inspect"]()
        assert history.status_code == 200, history.text
        assert history.json()["research_runtime"]["state"] == "ready"
        assert history.json()["planning"][0]["status"] == "succeeded"
        assert "PRIVATE_GOAL" not in json.dumps(fixture.frames)
        # Canonical artifact readback through the existing owner.
        readback = await env["client"].get(f"{env['base']}/{env['programme']['id']}/discovery/{job['job_id']}/brief")
        assert readback.status_code == 200, readback.text
        assert readback.json()["physical_readback"] is True and readback.json()["brief"]["findings"]
        # An exact re-admission reuses the occurrence and never re-plans or re-fetches.
        assert (await _admit(env))["job_id"] == job["job_id"]
        assert len(fixture.frames) == 9 and len(fixture.gets) == 1
        if fmt == "html":
            # The planning projection reads only this Goal's programmes for the
            # current UTC day: yesterday's plan is not carried into tomorrow.
            from datetime import timedelta
            from src.guardian.goal_programmes import goal_programme_service
            today = goal_programme_service._clock()
            monkeypatch.setattr(goal_programme_service, "_clock", lambda: today + timedelta(days=1))
            tomorrow = await env["inspect"]()
            assert tomorrow.status_code == 200, tomorrow.text
            assert tomorrow.json()["planning"] == [] and tomorrow.json()["current_day_only"] is True
            assert any(run["job_id"] == job["job_id"] for run in tomorrow.json()["runs"])


@pytest.mark.parametrize("fmt", ["big_html", "big_rss"])
async def test_sources_up_to_max_source_bytes_cross_in_bounded_chunks(accounting_db, real_auth, monkeypatch, fmt):  # noqa: F811
    from src.runtime_plugins.protocol import MAX_FRAME
    body = FORMATS[fmt][1]
    assert 250 * 1024 < len(body) <= 262144
    fixture = Fixture(FORMATS[fmt])
    async with discovery(accounting_db, monkeypatch, fixture) as env:
        job = await _admit(env)
        done = await env["service"].run(job["job_id"])
        assert done["status"] == "succeeded", done
        witness = await _artifacts(env, job["job_id"])
        raw = next(a for a in witness.artifacts.values() if a["kind"] == "raw_source")
        assert json.loads(raw["content"])["body"].encode() == body
        reads = [payload for method, payload in fixture.frames if method == "artifacts.read"
                 and payload["artifact_ref"] == raw["reference"].artifact_id]
        # Bounded 64 KiB chunks at increasing offsets, ending with an empty chunk; no frame-limit change.
        assert len(reads) >= 5 and all(item["max_bytes"] == 65536 for item in reads)
        assert [item["offset"] for item in reads] == sorted(item["offset"] for item in reads) and reads[0]["offset"] == 0
        assert MAX_FRAME == 1_048_576
        snapshot = next(a for a in witness.artifacts.values() if a["kind"] == "snapshot")["parsed"]
        assert snapshot.lines[-1] in {"Shipped & dated.", "Summary: Dated."}


# ---------------------------------------------------------------- 2: config replacement, pinned old jobs

def _html_as_plain_text(profile):
    spec = next(item for item in profile["plugins"] if item["id"] == "seraph.source-extraction.v1")
    spec["config"] = {"formats": {"text/html": "plain-text"}}


async def test_profile_replacement_needs_no_consumer_edit(accounting_db, real_auth, monkeypatch, tmp_path):  # noqa: F811
    variant = package_variant(tmp_path / "variant", _html_as_plain_text)
    fixture = Fixture(FORMATS["html"])
    async with discovery(accounting_db, monkeypatch, fixture, package_root=variant) as env:
        assert env["host"].reviewed.profile["plugins"][-3]["config"] == {"formats": {"text/html": "plain-text"}}
        job = await _admit(env)
        done = await env["service"].run(job["job_id"])
        assert done["status"] == "succeeded", done
        witness = await _artifacts(env, job["job_id"])
        snapshot = next(a for a in witness.artifacts.values() if a["kind"] == "snapshot")["parsed"]
        # Same consumer and TS code; only the reviewed profile selects another provider.
        assert snapshot.lines == [HTML.decode()]
        pins = json.loads(next(a for a in witness.artifacts.values() if a["kind"] == "extraction")["content"])["pins"]
        assert pins["source_provider_digest"] == env["host"].reviewed.plugin_digests["seraph.source-extraction.v1"]


async def test_replacement_affects_only_future_admissions(accounting_db, real_auth, monkeypatch, tmp_path):  # noqa: F811
    from src.guardian.goal_discovery import ResearchRuntimeBlocked
    variant = package_variant(tmp_path / "variant", _html_as_plain_text)
    fixture = Fixture(FORMATS["html"])
    async with discovery(accounting_db, monkeypatch, fixture) as env:
        old = await _admit(env)
        async with research_host(env["jobs"], package_root=variant, observe=fixture.observe) as replaced:
            assert replaced.reviewed.composition_digest != env["host"].reviewed.composition_digest
            assert (replaced.reviewed.plugin_digests["seraph.source-extraction.v1"]
                    != env["host"].reviewed.plugin_digests["seraph.source-extraction.v1"])
            env["service"].cordis = replaced
            frames = len(fixture.frames)
            # The admitted job keeps its provider/config pin: blocked before any inference or contact.
            with pytest.raises(ResearchRuntimeBlocked, match="stale_output"):
                await env["service"].run(old["job_id"])
            blocked = await env["jobs"].get_job(old["job_id"])
            assert blocked["status"] == "blocked" and blocked["failure_reason"] == "stale_output"
            assert fixture.gets == [] and fixture.posts == [] and fixture.calls == [] and len(fixture.frames) == frames
            assert (await _admit(env))["job_id"] == old["job_id"]  # never re-planned under the replacement
            assert len(fixture.frames) == frames


# ---------------------------------------------------------------- 3: rejected content and stale output

@pytest.mark.parametrize("case", ["malformed", "billion_laughs", "external_entity", "custom_entity", "oversized"])
async def test_malformed_dtd_entity_and_oversized_feeds_are_rejected(accounting_db, real_auth, monkeypatch, case):  # noqa: F811
    body = {
        "malformed": b"<rss><channel><title>open</channel></rss>",
        "billion_laughs": (b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">'
                           b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;">]><rss><channel><title>&lol3;</title></channel></rss>'),
        "external_entity": b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><title>&x;</title></channel></rss>',
        "custom_entity": b"<rss><channel><title>&secret;</title></channel></rss>",
        "oversized": b"<rss><channel>" + b"<item><title>x</title></item>" * 65 + b"</channel></rss>",
    }[case]
    reason = {"malformed": "source_feed_malformed", "billion_laughs": "source_feed_dtd_forbidden",
              "external_entity": "source_feed_dtd_forbidden", "custom_entity": "source_feed_entity_forbidden",
              "oversized": "source_output_oversize"}[case]
    fixture = Fixture(("application/rss+xml", body))
    async with discovery(accounting_db, monkeypatch, fixture) as env:
        job = await _admit(env)
        done = await env["service"].run(job["job_id"])
        assert done["status"] == "degraded", done
        witness = await _artifacts(env, job["job_id"])
        assert not any(a["kind"] == "snapshot" for a in witness.artifacts.values())
        brief = next(a for a in witness.artifacts.values() if a["kind"] == "brief")["parsed"]
        assert brief["coverage"]["status"] == "sources_unavailable" and brief["findings"] == []
        assert brief["coverage"]["unavailable"][0]["reason"] == reason
        assert "artifacts.stage" not in fixture.methods()[2:]
        assert len(fixture.gets) == 1 and len(fixture.calls) == 2  # no brief inference over rejected input


async def test_output_under_another_provider_configuration_is_stale(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.guardian.goal_discovery import ResearchRuntimeBlocked
    fixture = Fixture(FORMATS["html"])
    async with discovery(accounting_db, monkeypatch, fixture) as env:
        job = await _admit(env)
        # The job's pinned config differs from the configuration the provider reports.
        monkeypatch.setattr("src.runtime_plugins.composition.plugin_config_digest", lambda *_: "f" * 64)
        with pytest.raises(ResearchRuntimeBlocked, match="stale_output"):
            await env["service"].run(job["job_id"])
        after = await env["jobs"].get_job(job["job_id"])
        assert after["status"] == "blocked" and after["failure_reason"] == "stale_output"
        assert not any(a["kind"] == "snapshot" for a in (await _artifacts(env, job["job_id"])).artifacts.values())
        history = await env["inspect"]()
        run = next(item for item in history.json()["runs"] if item["job_id"] == job["job_id"])
        assert run["research_blocked_reason"] == "stale_output" and run["outstanding_held"] is True


# ---------------------------------------------------------------- 4: revocation and restart

@pytest.mark.parametrize("phase", ["planning", "execution", "adoption", "restart_planning", "restart_execution"])
async def test_revocation_or_restart_never_duplicates_work_or_restores_authority(accounting_db, real_auth, monkeypatch, phase):  # noqa: F811
    from src.guardian.goal_discovery import ResearchRuntimeBlocked
    fixture = Fixture(FORMATS["html"])
    state = {"env": None, "fired": False}

    async def hook(frame):
        fixture.observe(frame)
        env, method = state["env"], frame["method"]
        planning = len(fixture.frames) <= 2
        trigger = {"planning": planning and method == "artifacts.read",
                   "restart_planning": planning and method == "artifacts.read",
                   "execution": not planning and method == "tasks.inspect",
                   "restart_execution": not planning and method == "tasks.inspect"}.get(phase, False)
        if trigger and not state["fired"]:
            state["fired"] = True
            if phase.startswith("restart"):
                env["host"].process.kill()  # the owned child dies mid-invocation
            else:
                await env["revoke"]()

    async with discovery(accounting_db, monkeypatch, fixture, hooks=hook) as env:
        state["env"] = env
        if phase == "adoption":
            from src.guardian import goal_discovery
            original = goal_discovery.write_discovery_output

            async def revoke_then_write(jobs, job_id, owner, fence, kind, value, slot=0):
                if kind == "snapshot" and not state["fired"]:
                    state["fired"] = True
                    await env["revoke"]()
                return await original(jobs, job_id, owner, fence, kind, value, slot)
            monkeypatch.setattr(goal_discovery, "write_discovery_output", revoke_then_write)
        plan_job_id = None
        if phase in {"planning", "restart_planning"}:
            with pytest.raises(ResearchRuntimeBlocked):
                await _admit(env)
            from src.db.models import WorkflowRunState
            from sqlalchemy import select
            async with env["jobs"]._session() as db:
                rows = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind.in_(
                    ("goal_discovery_planning_v1", "goal_public_discovery_v1"))))).scalars())
            assert [row.job_kind for row in rows] == ["goal_discovery_planning_v1"]  # no discovery job admitted
            plan_job_id = rows[0].run_identity
            assert rows[0].status in {"blocked", "running"}
        else:
            job = await _admit(env)
            with pytest.raises(ResearchRuntimeBlocked):
                await env["service"].run(job["job_id"])
            after = await env["jobs"].get_job(job["job_id"])
            assert after["status"] in {"blocked", "running"}
            assert not any(a["kind"] == "snapshot" for a in (await _artifacts(env, job["job_id"])).artifacts.values())
        counts = (len(fixture.frames), len(fixture.gets), len(fixture.calls))
        held = await env["jobs"].get_job(plan_job_id or job["job_id"])
        # Revoked authority cannot even record a block: the attempt stays held for recovery.
        assert (held["status"], held["failure_reason"]) == {
            "planning": ("blocked", "native_programme_authority_changed"), "execution": ("running", None),
            "adoption": ("running", None), "restart_planning": ("blocked", "research_runtime_unavailable"),
            "restart_execution": ("blocked", "research_runtime_unavailable")}[phase]
        if phase.startswith("restart"):
            await env["host"].stop()
            assert await env["host"].start()  # new boot nonce; no old invocation scope survives
        else:
            await env["revoke"](revoked=False)  # restoring the identity never revives the original attempt
        if plan_job_id is not None:
            with pytest.raises(ResearchRuntimeBlocked, match="programme_planning_requires_recovery"):
                await _admit(env)
            assert (await env["jobs"].get_job(plan_job_id))["status"] in {"blocked", "running"}
        else:
            assert (await _admit(env))["job_id"] == job["job_id"]
            await env["service"].tick()
            assert (await env["jobs"].get_job(job["job_id"]))["status"] == after["status"]
        assert (len(fixture.frames), len(fixture.gets), len(fixture.calls)) == counts


# ---------------------------------------------------------------- 5: missing provider is visible, no fallback

@pytest.mark.parametrize("missing", ["goal_plan", "rss_format", "host"])
async def test_missing_provider_is_an_operator_visible_block_without_fallback(accounting_db, real_auth, monkeypatch, tmp_path, missing):  # noqa: F811
    from src.guardian.goal_discovery import ResearchRuntimeBlocked

    def mutate(profile):
        if missing == "goal_plan":
            profile["plugins"] = [spec for spec in profile["plugins"] if spec["id"] != "seraph.goal-plan.v1"]
        else:
            spec = next(item for item in profile["plugins"] if item["id"] == "seraph.source-extraction.v1")
            spec["config"]["formats"].pop("application/rss+xml")
    variant = package_variant(tmp_path / "variant", mutate) if missing != "host" else None
    fixture = Fixture(FORMATS["rss"])
    async with discovery(accounting_db, monkeypatch, fixture, package_root=variant) as env:
        if missing == "host":
            await env["host"].stop()
            with pytest.raises(ResearchRuntimeBlocked, match="research_runtime_unavailable"):
                await _admit(env)
            runtime = (await env["inspect"]()).json()["research_runtime"]
            assert runtime["state"] == "blocked" and runtime["reason"] == "research_runtime_unavailable" and runtime["recovery"]
            assert fixture.frames == [] and fixture.calls == [] and fixture.gets == []
            return
        if missing == "goal_plan":
            with pytest.raises(ResearchRuntimeBlocked, match="research_plan_provider_unavailable"):
                await _admit(env)
            history = (await env["inspect"]()).json()
            assert history["research_runtime"]["reason"] == "research_plan_provider_unavailable"
            assert history["planning"][0]["status"] == "blocked"
            assert history["planning"][0]["blocked_reason"] == "research_plan_provider_unavailable"
            assert history["planning"][0]["recovery"] and history["runs"] == []
            assert fixture.frames == [] and fixture.calls == [] and fixture.gets == []
            return
        job = await _admit(env)
        with pytest.raises(ResearchRuntimeBlocked, match="source_format_provider_unavailable"):
            await env["service"].run(job["job_id"])
        history = (await env["inspect"]()).json()
        run = next(item for item in history["runs"] if item["job_id"] == job["job_id"])
        assert run["status"] == "blocked" and run["research_blocked_reason"] == "source_format_provider_unavailable"
        assert run["recovery"] and run["outstanding_held"] is True
        assert not any(a["kind"] == "snapshot" for a in (await _artifacts(env, job["job_id"])).artifacts.values())
        assert len(fixture.calls) == 2  # no brief inference; no silent Python extraction fallback


# ---------------------------------------------------------------- critic fixes

async def test_unreviewed_child_reason_is_recorded_as_a_fixed_code(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.guardian.goal_discovery import ResearchRuntimeBlocked
    fixture = Fixture(FORMATS["html"])
    async with discovery(accounting_db, monkeypatch, fixture) as env:
        original = env["host"].request_service

        async def odd_planner(method, payload, *, original_scope):
            if method == "research.buildPlan":
                return {"status": "blocked", "reason_code": "research.Plan:x", "memory_status": "no_learning"}
            return await original(method, payload, original_scope=original_scope)
        monkeypatch.setattr(env["host"], "request_service", odd_planner)
        with pytest.raises(ResearchRuntimeBlocked, match="research_provider_blocked"):
            await _admit(env)
        history = (await env["inspect"]()).json()
        assert history["planning"][0]["blocked_reason"] == "research_provider_blocked"
        assert "research.Plan" not in json.dumps(history)


async def test_html_output_differing_from_the_python_normalizer_rejects_only_its_slot(accounting_db, real_auth, monkeypatch):  # noqa: F811
    fixture = Fixture(FORMATS["html"])
    async with discovery(accounting_db, monkeypatch, fixture) as env:
        job = await _admit(env)
        # Python's reference now disagrees with the provider's text: never adopted, and only this slot degrades.
        monkeypatch.setattr("src.guardian.source_watch.normalize_source_text", lambda *_a, **_k: "Different text")
        done = await env["service"].run(job["job_id"])
        assert done["status"] == "degraded", done
        witness = await _artifacts(env, job["job_id"])
        assert not any(a["kind"] == "snapshot" for a in witness.artifacts.values())
        brief = next(a for a in witness.artifacts.values() if a["kind"] == "brief")["parsed"]
        assert [item["reason"] for item in brief["coverage"]["unavailable"]] == ["source_output_mismatch"]


async def test_snapshot_too_large_to_store_rejects_only_its_slot(accounting_db, real_auth, monkeypatch):  # noqa: F811
    # 4096 short lines: normalized text fits 64 KiB, but the PublicSnapshotV1 JSON does not.
    fixture = Fixture(("text/plain", "\n".join(["abcdefghijklmno"] * 4096).encode()))
    async with discovery(accounting_db, monkeypatch, fixture) as env:
        job = await _admit(env)
        done = await env["service"].run(job["job_id"])
        assert done["status"] == "degraded", done
        witness = await _artifacts(env, job["job_id"])
        assert not any(a["kind"] == "snapshot" for a in witness.artifacts.values())
        brief = next(a for a in witness.artifacts.values() if a["kind"] == "brief")["parsed"]
        assert brief["coverage"]["unavailable"] == [{"result_id": brief["coverage"]["unavailable"][0]["result_id"],
                                                     "reason": "source_output_oversize"}]
