"""Original stopped composition owner; no service/provider execution."""
import asyncio
import importlib.util
import hashlib
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from src.db.engine import get_session as ORIGINAL_SESSION
from tests.test_inference_accounting import accounting_db
from tests.test_native_memory_report_source_vertical import NODE

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("workspace_composition_cli", ROOT / "backend/workspace_cli.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)
PACKAGE = Path("/home/pawel/repos/seraph/.agent-worktrees/986-a1-services/runtime/cordis")


@pytest_asyncio.fixture
async def cli_db(accounting_db):
    root, pooled_engine, _ = accounting_db
    # The schema is genuinely constructed by the original fixture. Never reuse
    # its pytest-loop connections in the CLI's independent asyncio.run loop.
    await pooled_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}",
        connect_args={"timeout": 5}, poolclass=NullPool)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield root, engine, factory
    finally:
        await engine.dispose()


def test_unconfirmed_local_command_does_not_create_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "absent"))
    args = CLI._parser().parse_args(["--local", "composition-initialize"])
    with pytest.raises(CLI.WorkspaceLifecycleError, match="confirm"):
        CLI._run(args)
    assert not (tmp_path / "absent").exists()


def test_local_flag_cannot_retarget_other_lifecycle_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    args = CLI._parser().parse_args(["--local", "status"])
    with pytest.raises(CLI.WorkspaceLifecycleError, match="unsupported"):
        CLI._run(args)


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [None, "repeat", "partial", "active", "source_invalid", "missing_db_retained"])
async def test_original_stopped_composition_command(cli_db, monkeypatch, after):
    from config.settings import settings
    from src.db import engine as original
    from src.db.engine import override_session_factory
    from src.runtime_plugins import composition
    from src.runtime_plugins.ownership import DOMAINS, bind_invocation, CompositionBindingError
    from src.workspace.production import (ProductionWorkspace, runtime_workspace_owner,
        read_lifecycle_receipt, ProductionWorkspaceError)
    root, engine, factory = cli_db
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setattr(original, "get_session", ORIGINAL_SESSION)
    monkeypatch.setattr(original, "engine", engine)
    monkeypatch.setattr(original, "_db_path", str(root / "seraph.db"))
    monkeypatch.setattr(original, "async_session_factory", factory)
    monkeypatch.setenv("WORKSPACE_DIR", str(root))
    # Forward the ORIGINAL validator to the real already-reviewed stock package.
    # No constructed ReviewedComposition or fake digest/row is supplied.
    checked = composition.reviewed_composition
    monkeypatch.setattr(composition, "reviewed_composition",
        lambda: checked(node_path=NODE, root=PACKAGE))
    args = CLI._parser().parse_args(["--local", "composition-initialize", "--confirm"])
    with override_session_factory(factory):
        if after == "active":
            with runtime_workspace_owner(root):
                with pytest.raises(ProductionWorkspaceError):
                    await asyncio.to_thread(CLI._run, args)
            async with factory() as db:
                assert await db.scalar(text("SELECT COUNT(*) FROM runtime_composition_states")) == 0
            return
        if after == "source_invalid":
            monkeypatch.setattr(composition, "reviewed_composition",
                lambda: checked(node_path=NODE, root=root / "missing-package"))
            with pytest.raises(composition.CompositionBlocked):
                await asyncio.to_thread(CLI._run, args)
            async with factory() as db:
                assert await db.scalar(text("SELECT COUNT(*) FROM runtime_composition_states")) == 0
            return
        receipt = await asyncio.to_thread(CLI._run, args)
        assert receipt["status"] == "ready" and receipt["composition_domains"] == 14
        workspace = ProductionWorkspace(host_root=root)
        durable = read_lifecycle_receipt(workspace)
        assert isinstance(durable["runtime_composition"], dict)
        async with original.get_session() as db:
            rows = (await db.execute(text("SELECT runtime_domain FROM runtime_composition_states"))).scalars().all()
            assert set(rows) == set(DOMAINS) and len(rows) == 14
            binding = await bind_invocation(db, method="memory.retrieve", native_branch="artifact",
                reviewed_composition=checked(node_path=NODE, root=PACKAGE))
            assert binding.origin_method == "memory.retrieve"
        if after is None:
            return
        if after == "partial":
            async with engine.begin() as connection:
                await connection.execute(text("DELETE FROM runtime_composition_states WHERE runtime_domain='seraph.memory.v1'"))
        if after == "missing_db_retained":
            await engine.dispose()
            (root / "seraph.db").rename(root / "retained-seraph.db")
        paths = [q for q in (root / "seraph.db", root / "seraph.db-wal") if q.exists()]
        physical = {str(q): hashlib.sha256(q.read_bytes()).hexdigest() for q in paths}
        before = read_lifecycle_receipt(workspace)["runtime_composition"]
        with pytest.raises((CompositionBindingError, ProductionWorkspaceError, CLI.WorkspaceLifecycleError)):
            await asyncio.to_thread(CLI._run, args)
        assert read_lifecycle_receipt(workspace)["runtime_composition"] == before
        assert {str(q): hashlib.sha256(q.read_bytes()).hexdigest() for q in paths} == physical
        if after == "missing_db_retained":
            assert not (root / "seraph.db").exists()
            return
        async with factory() as db:
            assert await db.scalar(text("SELECT COUNT(*) FROM runtime_composition_states")) == (13 if after == "partial" else 14)


@pytest.mark.asyncio
@pytest.mark.parametrize("package_available", [True, False])
async def test_fully_absent_database_original_initialization(tmp_path, monkeypatch, package_available):
    from config.settings import settings
    from src.db import engine as original
    from src.db.engine import override_session_factory
    from src.runtime_plugins import composition
    from src.runtime_plugins.ownership import DOMAINS, bind_invocation
    from src.workspace.production import ProductionWorkspace, read_lifecycle_receipt

    root = tmp_path / "fresh-workspace"
    root.mkdir(mode=0o700)
    database = root / "seraph.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}",
        connect_args={"timeout": 5}, poolclass=NullPool)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setattr(original, "engine", engine)
    monkeypatch.setattr(original, "_db_path", str(database))
    monkeypatch.setattr(original, "async_session_factory", factory)
    monkeypatch.setenv("WORKSPACE_DIR", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "fresh-lifecycle"))
    checked = composition.reviewed_composition
    monkeypatch.setattr(composition, "reviewed_composition", lambda: checked(
        node_path=NODE, root=PACKAGE if package_available else root / "missing-package"))
    args = CLI._parser().parse_args(["--local", "composition-initialize", "--confirm"])
    assert not database.exists()
    try:
        with override_session_factory(factory):
            if not package_available:
                with pytest.raises(composition.CompositionBlocked):
                    await asyncio.to_thread(CLI._run, args)
                assert not database.exists()
                assert not (root / "seraph.db-wal").exists()
                assert not (read_lifecycle_receipt(ProductionWorkspace(host_root=root)) or {}).get("runtime_composition")
                return
            # No create_all/SQL seed: the command calls ORIGINAL init_db itself.
            receipt = await asyncio.to_thread(CLI._run, args)
            assert receipt["status"] == "ready" and receipt["composition_domains"] == 14
            assert database.is_file()
            durable = read_lifecycle_receipt(ProductionWorkspace(host_root=root))
            assert isinstance(durable["runtime_composition"], dict)
            async with original.get_session() as db:
                rows = (await db.execute(text("SELECT runtime_domain FROM runtime_composition_states"))).scalars().all()
                assert set(rows) == set(DOMAINS) and len(rows) == 14
                binding = await bind_invocation(db, method="memory.retrieve", native_branch="artifact",
                    reviewed_composition=checked(node_path=NODE, root=PACKAGE))
                assert binding.origin_method == "memory.retrieve"
    finally:
        await engine.dispose()


def test_managed_command_routes_through_existing_cli():
    source = (ROOT / "manage.sh").read_text()
    assert 'initialize_runtime_composition local "$@"' in source
    assert 'initialize_runtime_composition production "$@"' in source
    assert '"$interpreter" -B "$SCRIPT_DIR/backend/workspace_cli.py"' in source
    assert '--local composition-initialize "$@"' in source
