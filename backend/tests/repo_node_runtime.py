"""Operator-selected Node24/TypeScript fixtures for actual repository tests.

These tests never download or fall back to an ambient ``node``. The fixed
runtime is supplied by ``SERAPH_TEST_NODE_RUNTIME``; TypeScript comes from
``SERAPH_TEST_TYPESCRIPT_ROOT`` or the checkout's existing frontend install.
Missing inputs skip the Node parameter with an explicit reason.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


def required_node24_runtime() -> Path:
    configured = os.environ.get("SERAPH_TEST_NODE_RUNTIME", "")
    if not configured:
        pytest.skip("Actual Node24 repository fixture requires SERAPH_TEST_NODE_RUNTIME (no ambient fallback)")
    node = Path(configured)
    if not node.is_absolute() or not node.is_file():
        pytest.skip("SERAPH_TEST_NODE_RUNTIME must name an existing absolute Node24 executable")
    version = subprocess.run([str(node), "--version"], capture_output=True, text=True,
                             timeout=5, env={"PATH": "/usr/bin:/bin"}).stdout.strip()
    if not version.startswith("v24."):
        pytest.skip("Actual Node repository fixture requires Node24; found " + (version or "no version"))
    return node


def required_typescript_root() -> Path:
    configured = os.environ.get("SERAPH_TEST_TYPESCRIPT_ROOT")
    root = Path(configured) if configured else Path(__file__).resolve().parents[2] / "frontend/node_modules/typescript"
    if not (root / "package.json").is_file():
        pytest.skip("Actual Node build fixture requires an installed TypeScript; set SERAPH_TEST_TYPESCRIPT_ROOT")
    return root
