"""Real SQLite equivalence checks; no fabricated Source/certificate owner."""
import ast
import sqlite3
from pathlib import Path

import pytest


TARGETS = (
    ('work_board_tasks', 'ix_work_board_tasks_task_id', 29),
    ('workflow_run_states', 'ix_workflow_run_states_run_identity', 37),
)


def _queries():
    path = Path(__file__).resolve().parents[1] / 'src/memory/composition_headers.py'
    module = ast.parse(path.read_text())
    node = next(n for n in module.body if isinstance(n, ast.FunctionDef)
                and n.name == '_validate_locator')
    constants = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
                 and isinstance(n.value, str)]
    new = next(s for s in constants if s.startswith('SELECT CASE WHEN name COLLATE BINARY=?'))
    old = next(s for s in constants if s.startswith('SELECT CASE WHEN octet_length(name)<=128')
               and 'origin IN' in s)
    return old, new


def _accepted(rows, name):
    matches = [r for r in rows if r[0] == name]
    return len(matches) == 1 and tuple(matches[0][1:]) == (1, 'c', 0)


@pytest.mark.parametrize('table,target,width', TARGETS)
@pytest.mark.parametrize('hostile', ['unrelated', 'é'*64, '😀'*40, 'x'*129,
                                    'name"with\\quotes'])
def test_real_sqlite_ignored_index_name_projection(table, target, width, hostile):
    old, new = _queries()
    db = sqlite3.connect(':memory:')
    try:
        db.execute(f'CREATE TABLE "{table}"(id INTEGER PRIMARY KEY, locator TEXT, spare TEXT)')
        db.execute(f'CREATE UNIQUE INDEX "{target}" ON "{table}"(locator COLLATE BINARY)')
        quoted = hostile.replace('"', '""')
        db.execute(f'CREATE INDEX "{quoted}" ON "{table}"(spare)')
        original = db.execute(old, (table, 49)).fetchall()
        projected = db.execute(new, (target, table, 49)).fetchall()
        assert len(original) == len(projected) == 2
        assert _accepted(original, target) is _accepted(projected, target) is True
        assert all(len(r) == 4 for r in projected)
        assert sorted(r[1:] for r in original) == sorted(r[1:] for r in projected)
        assert all(r[0] is None or r[0] == target for r in projected)
        assert width == len(target)+2
    finally:
        db.close()


@pytest.mark.parametrize('table,target,width', TARGETS)
@pytest.mark.parametrize('actual_unique', [False, True])
def test_real_sqlite_target_uniqueness_outcome_preserved(table, target, width, actual_unique):
    old, new = _queries()
    db = sqlite3.connect(':memory:')
    try:
        db.execute(f'CREATE TABLE "{table}"(locator TEXT)')
        unique = 'UNIQUE ' if actual_unique else ''
        db.execute(f'CREATE {unique}INDEX "{target}" ON "{table}"(locator)')
        assert _accepted(db.execute(old, (table, 49)).fetchall(), target) is actual_unique
        assert _accepted(db.execute(new, (target, table, 49)).fetchall(), target) is actual_unique
    finally:
        db.close()


@pytest.mark.parametrize('table,target,width', TARGETS)
def test_real_sqlite_wrong_or_missing_target_remains_denied(table, target, width):
    old, new = _queries()
    db = sqlite3.connect(':memory:')
    try:
        db.execute(f'CREATE TABLE "{table}"(locator TEXT)')
        db.execute(f'CREATE UNIQUE INDEX "{target}_unexpected" ON "{table}"(locator)')
        assert not _accepted(db.execute(old, (table, 49)).fetchall(), target)
        assert not _accepted(db.execute(new, (target, table, 49)).fetchall(), target)
    finally:
        db.close()
