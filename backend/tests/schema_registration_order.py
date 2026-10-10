"""Fresh original application registration proof; no pytest/conftest imports.

Invoked only by the provider-free regression with -I -S -B and explicit existing
cached dependency path. stdout is a closed nonsecret result; files stay private.
"""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys


def main():
    assert sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode
    assert len(sys.argv) == 5
    # Match the original provider-free pytest launcher before cold app imports.
    assert os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP") == "true"
    backend, dependencies, workspace = map(Path, sys.argv[1:4])
    order = sys.argv[4]
    assert order in {"before", "after"}
    assert backend.is_absolute() and dependencies.is_absolute() and workspace.is_absolute()
    # The immutable Root test epoch must contain no credential-bearing dotenv.
    assert not (backend.parent / ".env.dev").exists()
    sys.path[:0] = [str(backend), str(dependencies)]
    contacts = []
    original_socket = socket.socket
    class LocalOnlySocket(original_socket):
        def __init__(self, family=socket.AF_INET, *args, **kwargs):
            if family != socket.AF_UNIX:
                contacts.append("socket")
                raise AssertionError("nonlocal socket denied in schema registration proof")
            super().__init__(family, *args, **kwargs)
    socket.socket = LocalOnlySocket
    # IPv6 is unavailable in the inherited R3 AF_UNIX-only test profile.
    # Avoid urllib3's import-time bind probe; retain every transport denial.
    socket.has_ipv6 = False
    assert socket.has_ipv6 is False
    import httpx
    def deny_sync(*args, **kwargs):
        contacts.append("sync")
        raise AssertionError("provider transport denied")
    async def deny_async(*args, **kwargs):
        contacts.append("async")
        raise AssertionError("provider transport denied")
    httpx.HTTPTransport.handle_request = deny_sync
    httpx.AsyncHTTPTransport.handle_async_request = deny_async
    from config.settings import settings
    settings.workspace_dir = str(workspace)
    settings.operator_auth_secret = "isolated-schema-registration"
    settings.operator_auth_secret_hash = ""
    settings.openrouter_api_key = ""
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from sqlmodel import SQLModel
    from src.db import models
    assert "task_method_active" not in SQLModel.metadata.tables
    expected = {
        ("table", "task_method_active", "task_method_active"),
        ("index", "ix_task_method_active_goal_id", "task_method_active"),
        ("index", "ix_task_method_active_owner_identity_id", "task_method_active"),
        ("index", "sqlite_autoindex_task_method_active_1", "task_method_active"),
        ("index", "sqlite_autoindex_task_method_active_2", "task_method_active"),
    }
    async def execute():
        engine = create_async_engine("sqlite+aiosqlite:///" + str(workspace / "original-schema.sqlite3"))
        try:
            # Importing the cold factory does not load its router until called.
            from src.app import create_app
            assert "task_method_active" not in SQLModel.metadata.tables
            if order == "before":
                create_app()
                assert "task_method_active" in SQLModel.metadata.tables
            async with engine.begin() as connection:
                if order == "after":
                    assert "task_method_active" not in SQLModel.metadata.tables
                await connection.run_sync(SQLModel.metadata.create_all)
                initial = (await connection.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY rowid"))).all()
                if order == "after":
                    assert not any(row[2] == "task_method_active" for row in initial)
            if order == "after":
                create_app()
                assert "task_method_active" in SQLModel.metadata.tables
                async with engine.begin() as connection:
                    await connection.run_sync(SQLModel.metadata.create_all)
                    final = (await connection.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY rowid"))).all()
                assert set(initial) <= set(final)
                assert {(row[0], row[1], row[2]) for row in set(final) - set(initial)} == expected
                assert len(final) - len(initial) == 5
            from src.memory.composition_headers import certify_composition_superset, validate_composition_certificate
            from src.memory.header_bounds import HeaderReadBudget
            factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with factory() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                before = (await db.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY rowid"))).all()
                assert {(row[0], row[1], row[2]) for row in before if row[2] == "task_method_active"} == expected
                connection = await db.connection()
                observed = []
                full_bodies = {"started": 0, "delivered": 0}
                expected_columns = tuple(models.OperatorSession.__table__.columns.keys())
                def no_bodies(_c, _cursor, statement, _parameters, context, _many):
                    sql = " ".join(statement.lower().split())
                    observed.append(sql)
                    assert 'from task_method_active' not in sql and 'from "task_method_active"' not in sql
                    compiled = getattr(context, "compiled", None)
                    source = getattr(compiled, "statement", None)
                    if any(item.get("entity") is models.OperatorSession and item.get("expr") is models.OperatorSession
                           for item in getattr(source, "column_descriptions", ())):
                        full_bodies["started"] += 1
                        raise AssertionError("full_session_body_started_before_metadata_certification")
                def no_delivery(_c, cursor, _statement, _parameters, _context, _many):
                    columns = tuple(column[0] for column in (cursor.description or ()))
                    if len(columns) == len(expected_columns) and set(columns) == set(expected_columns):
                        full_bodies["delivered"] += 1
                        raise AssertionError("full_session_body_delivered_before_metadata_certification")
                event.listen(connection.sync_connection, "before_cursor_execute", no_bodies)
                event.listen(connection.sync_connection, "after_cursor_execute", no_delivery)
                try:
                    certificate = await certify_composition_superset(db, HeaderReadBudget())
                    await validate_composition_certificate(db, certificate)
                    assert observed and not any(ref[0] == "task_method_active" for ref in certificate.rows)
                    assert full_bodies["started"] == full_bodies["delivered"] == 0
                finally:
                    event.remove(connection.sync_connection, "after_cursor_execute", no_delivery)
                    event.remove(connection.sync_connection, "before_cursor_execute", no_bodies)
                assert (await db.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY rowid"))).all() == before
                await db.rollback()
            assert contacts == [], {"blocked_contact_kinds": contacts}
        finally:
            await engine.dispose()
    asyncio.run(execute())
    print(json.dumps({"order": order, "fresh_metadata_absent": True,
                      "five_objects": True, "no_pointer_bodies": True,
                      "provider_contacts": len(contacts)}, sort_keys=True))


if __name__ == "__main__":
    main()
