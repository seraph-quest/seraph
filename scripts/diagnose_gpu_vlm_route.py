#!/usr/bin/env python
"""Diagnose Seraph's direct route to the GPU-hosted VLM wrapper."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx


def _default_base_url() -> str:
    return (os.getenv("SERAPH_VLM_BASE_URL") or os.getenv("LOCAL_VLM_BASE_URL") or "").strip().rstrip("/")


def _default_model() -> str:
    model = (os.getenv("LOCAL_VLM_MODEL") or os.getenv("LOCAL_MODEL") or "").strip()
    return model.removeprefix("openai/")


def _default_api_key() -> str:
    return (
        os.getenv("SERAPH_VLM_API_KEY")
        or os.getenv("LOCAL_VLM_API_KEY")
        or os.getenv("LOCAL_LLM_API_KEY")
        or ""
    ).strip()


def _is_direct_route_candidate(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host not in {"", "localhost", "127.0.0.1", "::1"}


def _endpoint_result(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    result: dict[str, Any] = {
        "ok": 200 <= response.status_code < 400,
        "status_code": response.status_code,
    }
    if isinstance(payload, dict):
        for key in ("status", "backend_status", "model", "queued", "active", "workers", "background_workers"):
            if key in payload:
                result[key] = payload[key]
        queue = payload.get("queue")
        if isinstance(queue, dict):
            result["queue"] = {
                "queued": queue.get("queued"),
                "active": queue.get("active"),
                "workers": queue.get("workers"),
                "background_workers": queue.get("background_workers"),
            }
    return result


def _get_json(client: httpx.Client, url: str) -> dict[str, Any]:
    try:
        return _endpoint_result(client.get(url))
    except httpx.TimeoutException:
        return {"ok": False, "status_code": None, "error": "timeout"}
    except httpx.ConnectError:
        return {"ok": False, "status_code": None, "error": "connect_error"}
    except httpx.HTTPError:
        return {"ok": False, "status_code": None, "error": "http_error"}


def _post_chat(client: httpx.Client, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = client.post(
            base_url.rstrip("/") + "/v1/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with exactly: GPU VLM OK"}],
                "temperature": 0,
                "max_tokens": 16,
            },
        )
    except httpx.TimeoutException:
        return {"ok": False, "status_code": None, "error": "timeout"}
    except httpx.ConnectError:
        return {"ok": False, "status_code": None, "error": "connect_error"}
    except httpx.HTTPError:
        return {"ok": False, "status_code": None, "error": "http_error"}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    content = ""
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = str(message.get("content") or "").strip()
    return {
        "ok": 200 <= response.status_code < 400 and content == "GPU VLM OK",
        "status_code": response.status_code,
        "content": content[:80],
    }


def _post_analyze_file(client: httpx.Client, base_url: str, model: str, api_key: str, image_path: Path) -> dict[str, Any]:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with image_path.open("rb") as image_file:
            response = client.post(
                base_url.rstrip("/") + "/v1/analyze-file",
                headers=headers,
                data={
                    "model": model,
                    "prompt": (
                        "Return JSON only with keys summary, activity_type, confidence. "
                        "Describe the screenshot in one concise sentence."
                    ),
                },
                files={"file": (image_path.name, image_file, "application/octet-stream")},
            )
    except FileNotFoundError:
        return {"ok": False, "status_code": None, "error": "image_not_found"}
    except httpx.TimeoutException:
        return {"ok": False, "status_code": None, "error": "timeout"}
    except httpx.ConnectError:
        return {"ok": False, "status_code": None, "error": "connect_error"}
    except httpx.HTTPError:
        return {"ok": False, "status_code": None, "error": "http_error"}
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return {
        "ok": 200 <= response.status_code < 400,
        "status_code": response.status_code,
        "payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=_default_base_url(), help="VLM wrapper base URL, for example http://192.168.1.26:8001")
    parser.add_argument("--model", default=_default_model(), help="Raw wrapper/backend model id")
    parser.add_argument(
        "--api-key",
        default=_default_api_key(),
        help="Optional wrapper API key; never printed, but env vars are preferred because CLI args can leak via shell history or process listings",
    )
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--skip-chat", action="store_true", help="Only check wrapper health endpoints")
    parser.add_argument("--image", type=Path, default=None, help="Optional screenshot path for /v1/analyze-file")
    parser.add_argument(
        "--allow-non-direct-base-url",
        action="store_true",
        help="Allow loopback or localhost base URLs for diagnostic bridge checks; receipts are marked non-direct",
    )
    args = parser.parse_args()

    base_url = str(args.base_url or "").strip().rstrip("/")
    model = str(args.model or "").strip()
    if not base_url:
        raise SystemExit("missing --base-url or SERAPH_VLM_BASE_URL")
    if not model and not args.skip_chat:
        raise SystemExit("missing --model or LOCAL_VLM_MODEL/LOCAL_MODEL")
    direct_route_candidate = _is_direct_route_candidate(base_url)
    if not direct_route_candidate and not args.allow_non_direct_base_url:
        print(
            json.dumps(
                {
                    "base_url": base_url,
                    "direct_route_candidate": False,
                    "reachable": False,
                    "validation_ok": False,
                    "error": "non_direct_base_url",
                    "hint": "Use SERAPH_VLM_BASE_URL=http://192.168.1.26:8001 for direct-route receipts, or pass --allow-non-direct-base-url for diagnostic bridge checks.",
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 3

    with httpx.Client(timeout=max(args.timeout_seconds, 0.25)) as client:
        result: dict[str, Any] = {
            "base_url": base_url,
            "direct_route_candidate": direct_route_candidate,
            "model": model,
            "api_key_configured": bool(args.api_key),
            "health": _get_json(client, base_url + "/health"),
            "backend_health": _get_json(client, base_url + "/health/backend"),
            "queue_status": _get_json(client, base_url + "/queue/status"),
        }
        result["reachable"] = bool(
            result["health"].get("ok")
            and result["backend_health"].get("ok")
            and result["queue_status"].get("ok")
        )
        if not args.skip_chat:
            result["chat"] = _post_chat(client, base_url, model, args.api_key)
        if args.image is not None:
            result["analyze_file"] = _post_analyze_file(client, base_url, model, args.api_key, args.image)
        result["validation_ok"] = bool(
            result["reachable"]
            and result["direct_route_candidate"]
            and (args.skip_chat or result.get("chat", {}).get("ok"))
            and (args.image is None or result.get("analyze_file", {}).get("ok"))
        )

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("validation_ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
