"""Validate existing Memory DDL; never create, repair or infer authority."""
from sqlalchemy import text
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateIndex, PrimaryKeyConstraint, UniqueConstraint

from src.memory.header_bounds import HeaderBoundsError, MEMORY_DESCRIPTORS, _MEMORY_MODELS


def _sql_normalized(value):
    # Expected and observed strings are fixed bounded schema, not private data.
    return " ".join(value.split()).replace('"', '').lower()


async def validate_memory_schema(db, budget):
    from src.memory.header_bounds import HeaderReadBudget
    from src.workspace.accounting_witness import CompositionSessionGuard
    if type(budget) is not HeaderReadBudget:
        raise HeaderBoundsError("memory_closure_request_invalid")
    guard = db.info.get("composition_guard")
    if type(guard) is not CompositionSessionGuard:
        raise HeaderBoundsError("memory_retention_writer_unavailable")
    with guard._retention_reads(budget):
        await _validate_memory_schema(db, budget)


async def _validate_memory_schema(db, budget):
    from src.memory.composition_headers import _metadata_cost
    for name, model in _MEMORY_MODELS.items():
        descriptor = MEMORY_DESCRIPTORS[name]
        rows = list(await db.execute(text(
            "SELECT CASE WHEN typeof(name)='text' THEN CASE WHEN octet_length(name)<=128 THEN name END END,"
            "CASE WHEN typeof(type)='text' THEN CASE WHEN octet_length(type)<=128 THEN type END END,"
            "\"notnull\",pk,CASE WHEN dflt_value IS NULL THEN 1 ELSE 0 END "
            "FROM pragma_table_info(:name) LIMIT :limit"),
            {"name": name, "limit": len(descriptor.columns) + 1}))
        budget.debit(_metadata_cost([list(row) for row in rows]), appearance=("retention-table", name))
        columns = model.__table__.columns
        if len(rows) != len(columns) or {row[0] for row in rows} != set(columns.keys()):
            raise HeaderBoundsError("memory_retained_schema_changed")
        expected_pk = tuple(column.name for column in model.__table__.primary_key.columns)
        observed_pk = tuple(row[0] for row in sorted(rows, key=lambda row: row[3]) if row[3])
        if observed_pk != expected_pk:
            raise HeaderBoundsError("memory_retained_schema_changed")
        for column_name, sql_type, notnull, _pk, no_default in rows:
            column = columns[column_name]
            if (sql_type != column.type.compile(dialect=dialect()).upper()
                    or notnull != int(not column.nullable) or no_default != 1
                    or column.server_default is not None):
                raise HeaderBoundsError("memory_retained_schema_changed")
        rowid_table = list(await db.execute(text(
            "SELECT wr FROM pragma_table_list WHERE schema='main' AND name=:name LIMIT 2"), {"name": name}))
        budget.debit(_metadata_cost([list(row) for row in rowid_table]), appearance=("retention-layout", name))
        if len(rowid_table) != 1 or rowid_table[0][0] != 0:
            raise HeaderBoundsError("memory_retained_schema_changed")
        expected_fks = set()
        for constraint in model.__table__.foreign_key_constraints:
            for position, element in enumerate(constraint.elements):
                expected_fks.add((position, element.column.table.name, element.parent.name, element.column.name,
                                  constraint.onupdate or "NO ACTION", constraint.ondelete or "NO ACTION", "NONE"))
        actual_fks = list(await db.execute(text(
            "SELECT seq,CASE WHEN octet_length(\"table\")<=128 THEN \"table\" END,"
            "CASE WHEN octet_length(\"from\")<=128 THEN \"from\" END,"
            "CASE WHEN octet_length(\"to\")<=128 THEN \"to\" END,"
            "CASE WHEN octet_length(on_update)<=16 THEN on_update END,"
            "CASE WHEN octet_length(on_delete)<=16 THEN on_delete END,"
            "CASE WHEN octet_length(match)<=16 THEN match END "
            "FROM pragma_foreign_key_list(:name) LIMIT :limit"),
            {"name": name, "limit": len(expected_fks) + 1}))
        budget.debit(_metadata_cost([list(row) for row in actual_fks]), appearance=("retention-fk", name))
        if len(actual_fks) != len(expected_fks) or {tuple(row) for row in actual_fks} != expected_fks:
            raise HeaderBoundsError("memory_retained_foreign_key_changed")
        named = {index.name: index for index in model.__table__.indexes}
        automatic = {tuple(column.name for column in constraint.columns):
                     ("pk" if isinstance(constraint, PrimaryKeyConstraint) else "u")
                     for constraint in model.__table__.constraints
                     if isinstance(constraint, (PrimaryKeyConstraint, UniqueConstraint)) and constraint.columns}
        indices = list(await db.execute(text(
            "SELECT CASE WHEN octet_length(name)<=128 THEN name END,\"unique\",origin,partial "
            "FROM pragma_index_list(:name) LIMIT :limit"),
            {"name": name, "limit": len(named) + len(automatic) + 1}))
        budget.debit(_metadata_cost([list(row) for row in indices]), appearance=("retention-index-list", name))
        if len(indices) != len(named) + len(automatic):
            raise HeaderBoundsError("memory_retained_index_changed")
        seen = set()
        for index_name, unique, origin, partial in indices:
            if type(index_name) is not str:
                raise HeaderBoundsError("memory_retained_index_changed")
            info = list(await db.execute(text(
                "SELECT CASE WHEN octet_length(name)<=128 THEN name END,"
                "CASE WHEN octet_length(coll)<=16 THEN coll END,desc,key,cid "
                "FROM pragma_index_xinfo(:name) LIMIT :limit"),
                {"name": index_name, "limit": len(columns) + 2}))
            budget.debit(_metadata_cost([list(row) for row in info]),
                         appearance=("retention-index-columns", name, index_name))
            keys = tuple(row[0] for row in info if row[3] == 1)
            if (not info or len(info) != len(keys) + 1 or any(row[1] != "BINARY" or row[2] != 0 for row in info)
                    or tuple(info[-1]) != (None, "BINARY", 0, 0, -1)):
                raise HeaderBoundsError("memory_retained_index_changed")
            if origin in {"pk", "u"}:
                if unique != 1 or partial != 0 or automatic.get(keys) != origin or keys in seen:
                    raise HeaderBoundsError("memory_retained_index_changed")
                seen.add(keys)
                continue
            index = named.get(index_name)
            if index is None or origin != "c" or unique != int(index.unique):
                raise HeaderBoundsError("memory_retained_index_changed")
            expected_sql = str(CreateIndex(index).compile(dialect=dialect()))
            headers = list(await db.execute(text(
                "SELECT typeof(sql),octet_length(sql) FROM sqlite_master "
                "WHERE type='index' AND name=:name LIMIT 2"), {"name": index_name}))
            budget.debit(_metadata_cost([list(row) for row in headers]),
                         appearance=("retention-index-ddl-header", name, index_name))
            if (len(headers) != 1 or headers[0][0] != "text"
                    or type(headers[0][1]) is not int or not 0 <= headers[0][1] <= 8192):
                raise HeaderBoundsError("memory_retained_index_changed")
            octets = headers[0][1]
            budget.debit(6 * octets + 2, appearance=("retention-index-ddl", name, index_name))
            bodies = list(await db.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=:name LIMIT 2"),
                {"name": index_name}))
            if len(bodies) != 1 or type(bodies[0][0]) is not str:
                raise HeaderBoundsError("memory_retained_index_changed")
            actual_sql = bodies[0][0]
            if (len(actual_sql.encode("utf-8")) > octets
                    or _sql_normalized(actual_sql) != _sql_normalized(expected_sql)
                    or partial != int(index.dialect_options["sqlite"].get("where") is not None)):
                raise HeaderBoundsError("memory_retained_index_changed")
            seen.add(index_name)
        if len(seen) != len(indices):
            raise HeaderBoundsError("memory_retained_index_changed")
