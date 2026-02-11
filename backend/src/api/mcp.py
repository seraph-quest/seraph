"""MCP server management API — CRUD + test endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.tools.mcp_manager import mcp_manager

router = APIRouter()

VALID_BUILDINGS = {"house-1", "church", "house-2", "forge", "tower", "clock", "mailbox"}


class AddServerRequest(BaseModel):
    name: str
    url: str
    building: str | None = None
    description: str = ""
    enabled: bool = True


class UpdateServerRequest(BaseModel):
    enabled: bool | None = None
    building: str | None = None
    url: str | None = None
    description: str | None = None


@router.get("/mcp/servers")
async def list_servers():
    """List all configured MCP servers with status."""
    return {"servers": mcp_manager.get_config()}


@router.post("/mcp/servers", status_code=201)
async def add_server(req: AddServerRequest):
    """Add a new MCP server."""
    if req.name in mcp_manager._config:
        raise HTTPException(status_code=409, detail=f"Server '{req.name}' already exists")
    if req.building and req.building not in VALID_BUILDINGS:
        raise HTTPException(status_code=400, detail=f"Invalid building. Choose from: {sorted(VALID_BUILDINGS)}")
    mcp_manager.add_server(
        name=req.name,
        url=req.url,
        building=req.building,
        description=req.description,
        enabled=req.enabled,
    )
    return {"status": "created", "name": req.name}


@router.put("/mcp/servers/{name}")
async def update_server(name: str, req: UpdateServerRequest):
    """Update an MCP server (enable/disable, change building, etc.)."""
    updates = req.model_dump(exclude_none=True)
    if "building" in updates and updates["building"] not in VALID_BUILDINGS:
        raise HTTPException(status_code=400, detail=f"Invalid building. Choose from: {sorted(VALID_BUILDINGS)}")
    if not mcp_manager.update_server(name, **updates):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"status": "updated", "name": name}


@router.delete("/mcp/servers/{name}")
async def remove_server(name: str):
    """Remove an MCP server from config."""
    if not mcp_manager.remove_server(name):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"status": "removed", "name": name}


@router.post("/mcp/servers/{name}/test")
async def test_server(name: str):
    """Test connection to an MCP server. Connects, lists tools, disconnects."""
    config = mcp_manager._config.get(name)
    if not config:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    url = config["url"]

    try:
        from smolagents import MCPClient
        client = MCPClient({"url": url, "transport": "streamable-http"})
        tools = client.get_tools()
        tool_names = [t.name for t in tools]
        client.disconnect()
        return {"status": "ok", "tool_count": len(tools), "tools": tool_names}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Connection failed: {e}")
