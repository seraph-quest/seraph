"""Bounded metadata SQL checks; no test issues a programme selection or grant."""
import ast
import inspect
import sqlite3
from types import SimpleNamespace

import pytest

from src.memory import composition_headers as headers
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError


def identity_sql(stage):
    tree = ast.parse(inspect.getsource(headers.preflight_programme_identity_component))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_descriptor_metadata_rows":
            keywords = {item.arg: item.value for item in node.keywords}
            if ast.literal_eval(keywords["appearance"]) == (stage,):
                return ast.literal_eval(keywords["bounded_query"])
    raise AssertionError("original metadata stage missing")


def projected(stage, columns, rows, suffix):
    # The CTE supplies metadata only to the exact original SQL projection.
    query = identity_sql(stage).replace(suffix, "FROM fixture")
    placeholders = ",".join("(" + ",".join("?" for _ in columns) + ")" for _ in rows)
    quoted = ",".join('"' + name + '"' for name in columns)
    statement = "WITH fixture(" + quoted + ") AS (VALUES " + placeholders + ") " + query
    with sqlite3.connect(":memory:") as connection:
        return connection.execute(statement, tuple(item for row in rows for item in row)).fetchall()


@pytest.mark.parametrize("name,origin,unique,accepted", [
    ("sqlite_autoindex_operator_identities_1", "pk", 1, True),
    ("unknown_index", "pk", 1, False),
    ("x" * 129, "pk", 1, False),
    ("sqlite_autoindex_operator_identities_1", "c", 1, False),
    ("sqlite_autoindex_operator_identities_1", "pk", 0, False),
])
def test_identity_index_projection_retains_rejecting_rows(name, origin, unique, accepted):
    rows = projected("identity-indexes", ("seq", "name", "unique", "origin", "partial"),
        [(0, name, unique, origin, 0)], "FROM pragma_index_list(?) LIMIT 2")
    assert len(rows) == 1
    assert headers._metadata_size(rows) <= headers._metadata_upper(2, (20, 40, 20, 4, 20))
    assert headers._identity_index_matches(rows) is accepted


@pytest.mark.parametrize("second_name,coll,accepted", [
    (None, "BINARY", True),
    ("id", "BINARY", False),
    ("unknown", "BINARY", False),
    ("x" * 129, "BINARY", False),
    (None, "NOCASE", False),
    (None, "\u2603" * 128, False),
])
def test_identity_parts_projection_preserves_null_distinction(second_name, coll, accepted):
    rows = projected("identity-parts", ("seqno", "cid", "name", "desc", "coll", "key"),
        [(0, 0, "id", 0, "BINARY", 1), (1, -1, second_name, 0, coll, 0)],
        "FROM pragma_index_xinfo(?) LIMIT 3")
    assert len(rows) == 2
    assert headers._metadata_size(rows) <= headers._metadata_upper(3, (20, 20, 4, 20, 8, 20))
    assert headers._identity_parts_match(rows) is accepted
    if second_name is not None:
        assert rows[1][2] is not None


def test_identity_key_projection_has_no_external_caller_permission():
    budget = HeaderReadBudget()
    descriptor = SimpleNamespace(table="operator_identities")
    before = budget.remaining
    assert headers._original_programme_identity_key_upper(None, descriptor, budget, "a" * 32, budget) is None
    assert headers._original_programme_identity_key_upper(None, descriptor, budget, "a" * 32, None) is None
    assert budget.remaining == before


def protected_identity_suffix():
    tree = ast.parse(inspect.getsource(headers._discover))
    for branch in tree.body[0].body:
        if (isinstance(branch, ast.If) and isinstance(branch.test, ast.Compare)
                and isinstance(branch.test.left, ast.Name)
                and branch.test.left.id == "identity_key_upper"):
            assignment = next(item for item in branch.body if isinstance(item, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "suffix" for target in item.targets))
            return "".join(item.value if isinstance(item, ast.Constant) else "id"
                for item in assignment.value.values)
    raise AssertionError("original protected Identity predicate absent")


@pytest.mark.parametrize("collation,other", [("RTRIM", "a" * 32 + " " * 128),
    ("NOCASE", "A" * 32)])
def test_identity_declared_collation_cannot_expand_protected_key(collation, other):
    # Real altered-column collation with a distinct explicit BINARY unique
    # index demonstrates why index_xinfo alone cannot price implicit WHERE.
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE fixture(id TEXT COLLATE " + collation + " NOT NULL)")
        connection.execute("CREATE UNIQUE INDEX exact_key ON fixture(id COLLATE BINARY)")
        connection.executemany("INSERT INTO fixture VALUES(?)", [("a" * 32,), (other,)])
        assert len(connection.execute("SELECT id FROM fixture WHERE id=? LIMIT 2", ("a" * 32,)).fetchall()) == 2
        selected = connection.execute("SELECT id FROM fixture" + protected_identity_suffix()
            + " LIMIT 2", ("a" * 32,)).fetchall()
        assert selected == [("a" * 32,)]
        assert headers._metadata_size(selected) <= headers._metadata_upper(2, (194,))
        assert "id COLLATE BINARY=?" in inspect.getsource(headers.read_programme_identity)


@pytest.mark.parametrize("collation,other", [("RTRIM", "a" * 32 + " " * 128),
    ("NOCASE", "A" * 32)])
def test_pointer_declared_collation_cannot_expand_unique_scope(collation, other):
    tree = ast.parse(inspect.getsource(headers.read_current_method_pointer))
    query = next(ast.literal_eval(call.args[1]) for call in ast.walk(tree)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        and call.func.id == "_metadata_rows"
        and any(item.arg == "appearance" and ast.literal_eval(item.value) == ("task-method-pointer-scope",)
            for item in call.keywords))
    with sqlite3.connect(":memory:") as connection:
        connection.execute("CREATE TABLE task_method_active(id TEXT NOT NULL, owner_identity_id TEXT COLLATE "
            + collation + " NOT NULL, goal_id TEXT NOT NULL, goal_revision INTEGER NOT NULL, family TEXT NOT NULL)")
        connection.execute("CREATE UNIQUE INDEX exact_scope ON task_method_active("
            "owner_identity_id COLLATE BINARY,goal_id COLLATE BINARY,goal_revision COLLATE BINARY,family COLLATE BINARY)")
        connection.executemany("INSERT INTO task_method_active VALUES(?,?,?,?,?)",
            [("selected", "a" * 32, "goal", 1, "research"), ("other", other, "goal", 1, "research")])
        scope = ("a" * 32, "goal", 1, "research")
        assert len(connection.execute("SELECT id FROM task_method_active WHERE owner_identity_id=? "
            "AND goal_id=? AND goal_revision=? AND family=? LIMIT 2", scope).fetchall()) == 2
        selected = connection.execute(query, scope).fetchall()
        assert len(selected) == 1 and selected[0][0] == "selected"
        assert headers._metadata_size(selected) <= headers._metadata_upper(1, (3074, 20))
        assert "WHERE id COLLATE BINARY=? LIMIT 2" in inspect.getsource(headers.read_current_method_pointer)


def test_identity_width_gate_uses_only_original_postproof_callsite():
    tree = ast.parse(inspect.getsource(headers.preflight_programme_identity_component))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "_discover"]
    assert len(calls) == 1
    call = calls[0]
    assert [node.id for node in call.args] == ["connection", "descriptor", "budget"]
    assert next(item.value.id for item in call.keywords if item.arg == "key") == "identity"
    keyword = next(item.value for item in call.keywords if item.arg == "_header_budget")
    assert isinstance(keyword, ast.Attribute) and keyword.value.id == "common33" and keyword.attr == "metadata_budget"
    source = inspect.getsource(headers._original_programme_identity_key_upper)
    assert ".f_locals" not in source.replace("frame.f_locals", "")
    assert "preflight_programme_identity_component.__code__" in source


def numeric_key_tail():
    # Exercise the pinned arithmetic tail without inventing an authenticated
    # caller, selection, certificate or Source. The separate AST test binds
    # this exact tail to the original protected query's ordering.
    function = ast.parse(inspect.getsource(headers._original_programme_identity_key_upper)).body[0]
    start = next(index for index, node in enumerate(function.body)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == "_metadata_precharge")
    function.name = "numeric_tail"
    function.args = ast.arguments(posonlyargs=[], args=[ast.arg(arg=name)
        for name in ("connection", "budget", "key")], kwonlyargs=[], kw_defaults=[], defaults=[])
    function.body = function.body[start:]
    payments = []
    namespace = {"HeaderBoundsError": HeaderBoundsError,
        "_metadata_precharge": lambda connection, budget, upper, appearance: payments.append((upper, appearance))}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
        "original-key-numeric-tail", "exec"), namespace)
    return namespace["numeric_tail"], payments


@pytest.mark.parametrize("key,octets", [("a" * 512, 512), ("é" * 256, 512),
    ("𐀀" * 128, 512), ("\ud800" * 170, 510)])
def test_identity_numeric_key_tail_preserves_existing_unicode_bound(key, octets):
    tail, payments = numeric_key_tail()
    assert tail(None, None, key) == 6 * octets + 2
    assert payments[0][0] == 65
    assert sum(upper for upper, _ in payments) == 65 + (0 if key.isascii() else 24 * len(key))


@pytest.mark.parametrize("key", ["a" * 513, "é" * 257, "𐀀" * 129])
def test_identity_numeric_key_tail_rejects_overflow_before_query(key):
    tail, payments = numeric_key_tail()
    with pytest.raises(HeaderBoundsError, match="header_request_bound"):
        tail(None, None, key)
    assert payments[0][0] == 65
    if len(key) > 512:
        assert len(payments) == 1


def test_identity_prequery_bound_precedes_original_locator_sql():
    function = ast.parse(inspect.getsource(headers._discover)).body[0]
    gate = next(index for index, node in enumerate(function.body)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_original_programme_identity_key_upper")
    query = next(index for index, node in enumerate(function.body)
        if isinstance(node, ast.Assign) and any(isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name) and call.func.id == "_metadata_rows"
            for call in ast.walk(node)))
    assert gate < query
    helper = ast.parse(inspect.getsource(headers._original_programme_identity_key_upper)).body[0]
    assert any(isinstance(node, ast.Raise) for node in ast.walk(helper))
    assert "encode" not in inspect.getsource(headers._original_programme_identity_key_upper)
