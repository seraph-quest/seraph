"""Shared skip-or-fail policy for real repository isolation tests (#1065).

Hosts that cannot provide the bubblewrap sandbox skip these tests, except where
``SERAPH_REQUIRE_REPO_ISOLATION_TESTS=1`` (set by CI once it has verified
bwrap works on the runner): there a missing sandbox is a failure, so CI cannot
silently stop exercising the isolated path.
"""
from __future__ import annotations

import os

import pytest


def isolation_unavailable(reason: object) -> None:
    message = f"host cannot isolate: {reason}"
    if os.environ.get("SERAPH_REQUIRE_REPO_ISOLATION_TESTS") == "1":
        pytest.fail(message + " (SERAPH_REQUIRE_REPO_ISOLATION_TESTS=1)")
    pytest.skip(message)
