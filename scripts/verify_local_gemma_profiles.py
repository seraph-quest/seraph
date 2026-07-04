#!/usr/bin/env python
"""Verify Seraph local Gemma runtime profiles against the configured gateway."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.local_runtime_profile_verifier import verify_local_runtime_profiles_sync


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL, for example http://127.0.0.1:8000/v1")
    parser.add_argument("--model", default=None, help="Model id to send in verification requests")
    parser.add_argument("--api-key", default=None, help="Optional API key; omitted for keyless local gateways")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for sanitized verification receipts")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args()

    receipt = verify_local_runtime_profiles_sync(
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        output_dir=args.output_dir,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps({
        "receipt_path": receipt["receipt_path"],
        "sha256": receipt["sha256"],
        "conclusion": receipt["conclusion"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
