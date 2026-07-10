"""Bounded backend-container probe for private GPU model and VLM routes."""

from __future__ import annotations

import json
import os
import urllib.request


def probe(name: str, url: str, key: str = "") -> None:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - configured private route
        if not 200 <= response.status < 300:
            raise RuntimeError(f"{name} returned HTTP {response.status}")
        response.read(4096)
    print(json.dumps({"route": name, "status": "reachable"}))


model_base = os.environ["LOCAL_LLM_API_BASE"].rstrip("/")
vlm_base = os.environ["SERAPH_VLM_BASE_URL"].rstrip("/")
probe("gpu_model", f"{model_base}/models", os.getenv("LOCAL_LLM_API_KEY", ""))
probe("vlm_wrapper", f"{vlm_base}/health", os.getenv("SERAPH_VLM_API_KEY", ""))
probe("vlm_backend", f"{vlm_base}/health/backend", os.getenv("SERAPH_VLM_API_KEY", ""))
