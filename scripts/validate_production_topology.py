#!/usr/bin/env python3
"""Fail closed on unsafe Seraph production compose output."""

from __future__ import annotations

import json
import re
import sys


FORBIDDEN_PORTS = {8000, 8001, 8004}


def fail(message: str) -> None:
    raise SystemExit(f"production topology invalid: {message}")


def main() -> None:
    config = json.load(sys.stdin)
    services = config.get("services", {})
    if set(services) != {"ingress", "backend", "vlm-wrapper", "gpu-model"}:
        fail("compose must contain exactly ingress, backend, VLM wrapper, and GPU model services")
    published: list[tuple[str, int, int]] = []
    for name, service in services.items():
        for port in service.get("ports", []) or []:
            target = int(port["target"])
            published_port = int(port["published"])
            published.append((name, target, published_port))
            if target in FORBIDDEN_PORTS or published_port in FORBIDDEN_PORTS:
                fail(f"{name} publishes forbidden inference/backend port {published_port}:{target}")
    if len(published) != 1 or published[0][0] != "ingress" or published[0][1] != 443:
        fail("exactly one ingress TLS port targeting 443 must be published")
    backend = services["backend"]
    environment = backend.get("environment", {})
    required = {
        "DEPLOYMENT_ENVIRONMENT": "production",
        "OPERATOR_AUTH_COOKIE_SECURE": "true",
        "OPERATOR_AUTH_BACKEND_WORKERS": "1",
        "OPERATOR_AUTH_TRUSTED_PROXY_IPS": "172.30.0.10",
    }
    for key, value in required.items():
        if str(environment.get(key, "")).lower() != value:
            fail(f"{key} must equal {value}")
    if backend.get("ports"):
        fail("backend must not publish a host port")
    if services["vlm-wrapper"].get("ports"):
        fail("VLM wrapper must not publish a host port")
    if services["gpu-model"].get("ports"):
        fail("GPU model must not publish a host port")
    if environment.get("LOCAL_LLM_API_BASE") != "http://gpu-model:8000/v1" or environment.get("SERAPH_VLM_BACKEND_URL") != "http://gpu-model:8000/v1":
        fail("backend model routes must use private gpu-model service")
    if services["vlm-wrapper"].get("environment", {}).get("VLM_BASE_URL") != "http://gpu-model:8000/v1":
        fail("VLM wrapper backend must use private gpu-model service")
    devices = services["gpu-model"].get("deploy", {}).get("resources", {}).get("reservations", {}).get("devices", [])
    if not any(device.get("driver") == "nvidia" and "gpu" in (device.get("capabilities") or []) for device in devices):
        fail("GPU model must reserve an NVIDIA GPU")
    model = services["gpu-model"]
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-fA-F]{64}", str(model.get("image", ""))):
        fail("GPU model image must be pinned by registry RepoDigest")
    model_mounts = model.get("volumes", []) or []
    if len(model_mounts) != 1 or model_mounts[0].get("target") != "/models" or model_mounts[0].get("read_only") is not True:
        fail("GPU model artifact directory must be one read-only /models bind mount")
    command = model.get("command", []) or []
    for flag in ("--model", "--mmproj", "--alias", "--ctx-size", "--n-gpu-layers"):
        if flag not in command: fail(f"GPU model command missing {flag}")
    print("compose topology valid: one TLS ingress; backend, VLM wrapper, and GPU model are private")


if __name__ == "__main__":
    main()
