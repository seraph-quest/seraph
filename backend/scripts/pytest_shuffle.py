"""Opt-in pytest plugin: run collected tests in a seeded random order (#1220).

Load it from ``backend/`` with ``-p scripts.pytest_shuffle --shuffle-seed=N``.
The seed is printed in the report header so any order can be replayed with the
same seed. Tests are shuffled across the whole collection, not only within a
class or file, because that is what exposes order dependence. Without
``--shuffle-seed`` the plugin changes nothing.
"""

from __future__ import annotations

import random

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shuffle-seed",
        type=int,
        default=None,
        help="run collected tests in the order given by this seed (printed in the header)",
    )


def pytest_report_header(config: pytest.Config) -> str | None:
    seed = config.getoption("--shuffle-seed")
    return None if seed is None else f"shuffle seed: {seed} (replay with --shuffle-seed={seed})"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    seed = config.getoption("--shuffle-seed")
    if seed is not None:
        random.Random(seed).shuffle(items)
