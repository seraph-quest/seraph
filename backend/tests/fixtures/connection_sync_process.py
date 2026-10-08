"""Disposable C4 race participant; deny sockets before application imports."""
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
from contextlib import asynccontextmanager

for name in list(os.environ):
    if any(marker in name.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
        os.environ.pop(name, None)
os.environ["WORKSPACE_DIR"] = sys.argv[2]
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
denied = []
original_connect = socket.socket.connect


def guarded_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        denied.append("network")
        raise PermissionError("Disposable sync participant denies all sockets")
    return original_connect(sock, address)


socket.socket.connect = guarded_connect
socket.socket.connect_ex = guarded_connect
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from src.db.engine import override_session_factory
from src.integrations.connection_sync import ConnectionSyncService, SyncError, SyncRequest
from src.integrations.gmail_read import GmailReadError
from src.work_board.contracts import WorkBoardOwner


async def run():
    engine = create_async_engine(sys.argv[1], connect_args={"check_same_thread": False})
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    service = ConnectionSyncService()
    mode = sys.argv[4] if len(sys.argv) > 4 else "compete"
    if mode == "unknown-owner":
        import httpx
        from src.integrations import connection_sync as sync
        from src.integrations.gmail_read import GoogleGmailReadonlyAdapter
        async def interrupted(req):
            raise httpx.ReadTimeout("fixture old callback owner contact interrupted")
        transport = httpx.MockTransport(interrupted)
        sync.GoogleGmailReadonlyAdapter = lambda connection, **kwargs: GoogleGmailReadonlyAdapter(connection, transport=transport, resolver=lambda *_: ["8.8.8.8"], **kwargs)
    if mode == "crash-before-effect":
        original_authority = service._authority
        async def terminate_before_effect(*args, **kwargs):
            if kwargs.get("lease") is not None:
                os._exit(17)
            return await original_authority(*args, **kwargs)
        service._authority = terminate_before_effect
    await service.start()
    try:
        with override_session_factory(factory):
            try:
                await service.synchronize(WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass"), SyncRequest.model_validate_json(Path(sys.argv[3]).read_text()), authenticated_token_hash="fixture-only")
            except GmailReadError as exc:
                print(json.dumps({"code": exc.code, "external_contacts": len(denied)}), flush=True)
            else:
                raise AssertionError("The competing process contacted a reserved connection")
    finally:
        await service.stop()
        await engine.dispose()


asyncio.run(run())
