"""Tests for local Gemma runtime profile verification receipts."""

from __future__ import annotations

import json

from src.local_runtime_profile_verifier import latest_local_runtime_profile_proof, verify_local_runtime_profiles
from src.local_runtime_profiles import (
    local_runtime_chat_payload,
    local_runtime_profile_form_fields,
    local_runtime_profile_headers,
)


def test_local_runtime_profile_contract_includes_screenshot_fast_controls(monkeypatch):
    monkeypatch.setattr("src.local_runtime_profiles.settings.local_vlm_timeout_seconds", 17)

    fields = local_runtime_profile_form_fields("screenshot_fast")
    headers = local_runtime_profile_headers("screenshot_fast")
    payload = local_runtime_chat_payload("screenshot_fast", model="gemma-test")

    assert fields["runtime_profile"] == "screenshot_fast"
    assert fields["runtime_path"] == "screenshot_image_analysis"
    assert fields["priority"] == "background"
    assert fields["reasoning"] == "off"
    assert json.loads(fields["profile_options"])["chat_template_kwargs"] == {"enable_thinking": False}
    assert headers["X-Seraph-Runtime-Profile"] == "screenshot_fast"
    assert headers["X-Seraph-Reasoning"] == "off"
    assert payload["model"] == "gemma-test"
    assert payload["temperature"] == 0.0
    assert payload["reasoning"] is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


async def test_verify_local_runtime_profiles_writes_receipt(tmp_path, monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, *, status_code=200, payload=None, text=None):
            self.status_code = status_code
            self._payload = payload if payload is not None else {"status": "ok"}
            self.text = text if text is not None else json.dumps(self._payload)
            self.is_success = status_code < 400

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError("bad status")

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, *, headers):
            calls.append({"method": "GET", "url": url, "headers": headers})
            return FakeResponse(payload={"status": "ok", "version": "test"})

        async def post(self, url, *, headers, json):
            calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
            profile = json["metadata"]["runtime_profile"]
            if profile == "screenshot_fast":
                payload = {
                    "choices": [{"message": {"content": '{"profile":"screenshot_fast","ok":true}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            else:
                payload = {
                    "choices": [
                        {
                            "message": {
                                "content": "PROFILE_OK",
                                "reasoning_content": "structured verifier marker",
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 6},
                }
            return FakeResponse(payload=payload)

    monkeypatch.setattr("src.local_runtime_profile_verifier.httpx.AsyncClient", FakeAsyncClient)
    receipt = await verify_local_runtime_profiles(
        base_url="http://127.0.0.1:8000/v1",
        model="unsloth/gemma-test",
        api_key="secret",
        output_dir=tmp_path,
    )

    assert receipt["schema_version"] == "seraph.local_runtime_profiles.proof.v1"
    assert isinstance(receipt["profile_contract_sha256"], str)
    assert len(receipt["profile_contract_sha256"]) == 64
    assert receipt["conclusion"]["profile_requests_completed"] is True
    assert receipt["conclusion"]["per_request_reasoning_control"] == "verified"
    assert receipt["conclusion"]["safe_for_single_backend_profile_routing"] is True
    assert receipt["profiles"][0]["request"]["headers"]["Authorization"] == "Bearer [redacted]"
    assert receipt["profiles"][0]["request"]["payload"]["reasoning"] is False
    assert receipt["profiles"][1]["request"]["payload"]["reasoning"] is True
    path = tmp_path / receipt["receipt_path"].split("/")[-1]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["sha256"] == receipt["sha256"]
    assert any(call["method"] == "POST" and call["json"]["metadata"]["runtime_profile"] == "chat_thinking" for call in calls)


async def test_verify_local_runtime_profiles_marks_ambiguous_without_structured_reasoning(tmp_path, monkeypatch):
    class FakeResponse:
        status_code = 200
        is_success = True

        def __init__(self, payload):
            self._payload = payload
            self.text = json.dumps(payload)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, *, headers):
            return FakeResponse({"status": "ok"})

        async def post(self, url, *, headers, json):
            return FakeResponse({"choices": [{"message": {"content": "OK"}}]})

    monkeypatch.setattr("src.local_runtime_profile_verifier.httpx.AsyncClient", FakeAsyncClient)

    receipt = await verify_local_runtime_profiles(
        base_url="http://127.0.0.1:8000/v1",
        model="unsloth/gemma-test",
        output_dir=tmp_path,
    )

    assert receipt["conclusion"]["profile_requests_completed"] is True
    assert receipt["conclusion"]["per_request_reasoning_control"] == "ambiguous_no_visible_reasoning"
    assert receipt["conclusion"]["safe_for_single_backend_profile_routing"] is False
    assert "ambiguous" in " ".join(receipt["conclusion"]["notes"])


async def test_verify_local_runtime_profiles_rejects_visible_screenshot_thought_channel(tmp_path, monkeypatch):
    class FakeResponse:
        status_code = 200
        is_success = True

        def __init__(self, payload):
            self._payload = payload
            self.text = json.dumps(payload)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, *, headers):
            return FakeResponse({"status": "ok"})

        async def post(self, url, *, headers, json):
            profile = json["metadata"]["runtime_profile"]
            if profile == "screenshot_fast":
                content = '<|channel>thought\n<channel|>{"profile":"screenshot_fast","ok":true}'
            else:
                content = "PROFILE_OK"
            return FakeResponse({"choices": [{"message": {"content": content, "reasoning_content": "marker"}}]})

    monkeypatch.setattr("src.local_runtime_profile_verifier.httpx.AsyncClient", FakeAsyncClient)

    receipt = await verify_local_runtime_profiles(
        base_url="http://127.0.0.1:8000/v1",
        model="unsloth/gemma-test",
        output_dir=tmp_path,
    )

    assert receipt["profiles"][0]["response"]["reasoning_markers"]["visible_reasoning"] is True
    assert receipt["conclusion"]["per_request_reasoning_control"] == "failed"
    assert receipt["conclusion"]["safe_for_single_backend_profile_routing"] is False


async def test_verify_local_runtime_profiles_rejects_structured_screenshot_reasoning(tmp_path, monkeypatch):
    class FakeResponse:
        status_code = 200
        is_success = True

        def __init__(self, payload):
            self._payload = payload
            self.text = json.dumps(payload)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, *, headers):
            return FakeResponse({"status": "ok"})

        async def post(self, url, *, headers, json):
            profile = json["metadata"]["runtime_profile"]
            message = {"content": "PROFILE_OK", "reasoning_content": "thinking marker"}
            if profile == "screenshot_fast":
                message = {
                    "content": '{"profile":"screenshot_fast","ok":true}',
                    "reasoning_content": "screenshot path should not expose this",
                }
            return FakeResponse({"choices": [{"message": message}]})

    monkeypatch.setattr("src.local_runtime_profile_verifier.httpx.AsyncClient", FakeAsyncClient)

    receipt = await verify_local_runtime_profiles(
        base_url="http://127.0.0.1:8000/v1",
        model="unsloth/gemma-test",
        output_dir=tmp_path,
    )

    assert receipt["profiles"][0]["response"]["reasoning_markers"]["structured_reasoning_field"] is True
    assert receipt["conclusion"]["per_request_reasoning_control"] == "failed"
    assert receipt["conclusion"]["safe_for_single_backend_profile_routing"] is False
    assert "structured reasoning" in " ".join(receipt["conclusion"]["notes"])


async def test_latest_local_runtime_profile_proof_rejects_config_mismatch(tmp_path, monkeypatch):
    class FakeResponse:
        status_code = 200
        is_success = True

        def __init__(self, payload):
            self._payload = payload
            self.text = json.dumps(payload)

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, url, *, headers):
            return FakeResponse({"status": "ok"})

        async def post(self, url, *, headers, json):
            profile = json["metadata"]["runtime_profile"]
            if profile == "screenshot_fast":
                message = {"content": '{"profile":"screenshot_fast","ok":true}'}
            else:
                message = {"content": "PROFILE_OK", "reasoning_content": "structured marker"}
            return FakeResponse({"choices": [{"message": message}]})

    monkeypatch.setattr("src.local_runtime_profile_verifier.httpx.AsyncClient", FakeAsyncClient)

    await verify_local_runtime_profiles(
        base_url="http://127.0.0.1:8000/v1",
        model="unsloth/gemma-test",
        output_dir=tmp_path,
    )

    matching = latest_local_runtime_profile_proof(
        receipt_dir=tmp_path,
        expected_base_url="http://127.0.0.1:8000/v1",
        expected_model="unsloth/gemma-test",
    )
    mismatched = latest_local_runtime_profile_proof(
        receipt_dir=tmp_path,
        expected_base_url="http://127.0.0.1:9000/v1",
        expected_model="unsloth/other-gemma",
    )

    assert matching["status"] == "safe"
    assert matching["safe_for_single_backend_profile_routing"] is True
    assert len(str(matching["profile_contract_sha256"])) == 64
    assert mismatched["status"] == "unsafe"
    assert mismatched["safe_for_single_backend_profile_routing"] is False
    assert "base URL" in " ".join(mismatched["notes"])
    assert "model" in " ".join(mismatched["notes"])
