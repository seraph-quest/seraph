"""Quarantine lane selection for backend tests (#1220).

A test proven intermittent by the triage step in ``AGENTS.md`` ("Flaky test
triage") may carry ``@pytest.mark.quarantine(issue=NNNN)`` as a decorator on that
one test function. ``scripts/check_test_determinism.py`` requires the issue
number, rejects class, module and other placements, and caps the list.

Load from ``backend/`` with ``-p scripts.pytest_quarantine --quarantine=MODE``:

``exclude``
    Blocking lanes. A quarantined test is reported as skipped with its issue.
``only``
    The non-blocking quarantine lane. Everything else is deselected, so a
    quarantined test still runs and reports on every pull request.

Without ``--quarantine`` (a plain local ``pytest``) quarantined tests run like
any other test. There is no calendar expiry: a test leaves quarantine only
through a fix that removes the marker and passes the stress job 50 of 50.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--quarantine",
        choices=("exclude", "only"),
        default=None,
        help="exclude: skip quarantined tests (blocking lanes); only: run nothing else",
    )


def quarantine_issue(item: pytest.Item) -> object | None:
    marker = item.get_closest_marker("quarantine")
    if marker is None:
        return None
    issue = marker.kwargs.get("issue", marker.args[0] if marker.args else None)
    if issue is None or str(issue).lstrip("#").strip() == "":
        raise pytest.UsageError(f"{item.nodeid}: pytest.mark.quarantine requires issue=<number>")
    return issue


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    mode = config.getoption("--quarantine")
    if mode is None:
        return
    keep: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        issue = quarantine_issue(item)
        if mode == "exclude":
            if issue is not None:
                item.add_marker(pytest.mark.skip(
                    reason=f"quarantined (#{str(issue).lstrip('#')}); runs in the non-blocking quarantine lane"
                ))
            keep.append(item)
        elif issue is not None:
            keep.append(item)
        else:
            dropped.append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = keep
