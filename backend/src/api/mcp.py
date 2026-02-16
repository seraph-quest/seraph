"""MCP server management API — CRUD + test endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.tools.mcp_manager import mcp_manager

router = APIRouter()


class AddServerRequest(BaseModel):
    name: str
    url: str
    description: str = ""
    enabled: bool = True
    headers: dict[str, str] | None = None


class UpdateServerRequest(BaseModel):
    enabled: bool | None = None
    url: str | None = None
    description: str | None = None
    headers: dict[str, str] | None = None


class SetTokenRequest(BaseModel):
    token: str


@router.get("/mcp/servers")
async def list_servers():
    """List all configured MCP servers with status."""
    return {"servers": mcp_manager.get_config()}


@router.post("/mcp/servers", status_code=201)
async def add_server(req: AddServerRequest):
    """Add a new MCP server."""
    if req.name in mcp_manager._config:
        raise HTTPException(status_code=409, detail=f"Server '{req.name}' already exists")
    mcp_manager.add_server(
        name=req.name,
        url=req.url,
        description=req.description,
        enabled=req.enabled,
        headers=req.headers,
    )
    return {"status": "created", "name": req.name}


@router.put("/mcp/servers/{name}")
async def update_server(name: str, req: UpdateServerRequest):
    """Update an MCP server (enable/disable, etc.)."""
    updates = req.model_dump(exclude_none=True)
    if not mcp_manager.update_server(name, **updates):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"status": "updated", "name": name}


@router.delete("/mcp/servers/{name}")
async def remove_server(name: str):
    """Remove an MCP server from config."""
    if not mcp_manager.remove_server(name):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"status": "removed", "name": name}


@router.post("/mcp/servers/{name}/token")
async def set_server_token(name: str, req: SetTokenRequest):
    """Set auth token for an MCP server. Reconnects if enabled."""
    if not mcp_manager.set_token(name, req.token):
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    configs = mcp_manager.get_config()
    entry = next((c for c in configs if c["name"] == name), None)
    return {"status": "updated", "server": entry}


@router.post("/mcp/servers/{name}/test")
async def test_server(name: str):
    """Test connection to an MCP server. Connects, lists tools, disconnects."""
    config = mcp_manager._config.get(name)
    if not config:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    url = config["url"]

    # Check for unresolved env vars before attempting connection
    raw_headers = config.get("headers")
    missing_vars = mcp_manager._check_unresolved_vars(raw_headers)
    if missing_vars:
        return {
            "status": "auth_required",
            "message": f"Missing environment variables: {', '.join(missing_vars)}",
            "missing_env_vars": missing_vars,
        }

    try:
        from smolagents import MCPClient
        params: dict = {"url": url, "transport": "streamable-http"}
        if raw_headers:
            params["headers"] = {
                k: mcp_manager._resolve_env_vars(v) for k, v in raw_headers.items()
            }
        client = MCPClient(params, structured_output=False)
        tools = client.get_tools()
        tool_names = [t.name for t in tools]
        client.disconnect()
        return {"status": "ok", "tool_count": len(tools), "tools": tool_names}
    except Exception as e:
        exc_str = str(e).lower()
        if any(kw in exc_str for kw in ("401", "403", "unauthorized", "forbidden")):
            return {"status": "auth_failed", "message": f"Authentication failed: {e}"}
        raise HTTPException(status_code=502, detail=f"Connection failed: {e}")
