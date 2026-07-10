#!/usr/bin/env python3
"""Fail closed on unsafe Seraph production compose output."""

from __future__ import annotations

import json
import sys


FORBIDDEN_PORTS = {8000, 8001, 8004}


def fail(message: str) -> None:
    raise SystemExit(f"production topology invalid: {message}")


def main() -> None:
    config = json.load(sys.stdin)
    services = config.get("services", {})
    if set(services) != {"ingress", "backend", "vlm-wrapper"}:
        fail("compose must contain exactly ingress, backend, and private VLM wrapper services")
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
    print("compose topology valid: one TLS ingress; backend and VLM wrapper have no Docker publications")


if __name__ == "__main__":
    main()
