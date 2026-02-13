---
sidebar_position: 6
---

# Phase 5 — Security

**Goal**: Harden MCP server integration with credential management, leak detection, OAuth flows, and capability-based permissions. Ensure Seraph can safely connect to untrusted or hosted MCP servers without exposing secrets or granting excessive access.

**Status**: Planned

**Inspiration**: IronClaw research (OAuth 2.1 + DCR + credential injection + leak detection + WASM sandbox).

---

## 5.1 Credential Injection at Boundaries

MCP tools should never see raw credentials. Auth headers are injected at the transport layer.

**Files**:
```
backend/src/security/
  credentials.py         # Encrypted credential storage + injection
```

**Design**:
- MCPManager intercepts outbound requests and injects auth headers
- Credentials stored encrypted in DB (AES-256-GCM), master key in macOS Keychain
- Pattern: tool declares host needs → MCPManager maps host → injects Bearer/API-key header
- Credentials never appear in tool arguments or LLM context

**Config** (in `mcp-servers.json`):
```json
{
  "mcpServers": {
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "enabled": true,
      "building": "tower",
      "auth": {
        "type": "bearer",
        "credential_id": "github-pat"
      }
    }
  }
}
```

---

## 5.2 Leak Detection on Tool Outputs

Scan all tool responses before they enter LLM context to prevent credential leakage.

**Files**:
```
backend/src/security/
  leak_detector.py       # Aho-Corasick pattern scanner for tool outputs
```

**Design**:
- Aho-Corasick multi-pattern scanner on all tool responses
- Default patterns: OpenAI keys, Anthropic keys, AWS keys, GitHub tokens, PEM keys, high-entropy hex strings
- Actions per pattern: Block (reject response), Redact (replace with `[REDACTED]`), Warn (log only)
- Configurable via settings API
- Runs synchronously in the tool response pipeline (before LLM sees the output)

---

## 5.3 OAuth 2.1 + Dynamic Client Registration for Hosted MCP

Support hosted MCP servers that require OAuth authentication.

**Files**:
```
backend/src/security/
  oauth.py               # OAuth 2.1 + PKCE + Dynamic Client Registration
```

**Design**:
- Support hosted MCP servers (e.g., `api.githubcopilot.com/mcp/`) with full OAuth 2.1 + PKCE flow
- Dynamic Client Registration: discover `/.well-known/oauth-protected-resource`, register Seraph as public client
- Token refresh with automatic renewal before expiry
- Encrypted token storage in DB
- Localhost callback listener for authorization code flow
- UI: "Connect" button in Settings panel triggers OAuth flow in browser

---

## 5.4 Capability-Based Tool Permissions

Per-MCP-server capability declarations that limit what tools can do.

**Files**:
```
backend/src/security/
  capabilities.py        # Capability enforcement engine
```

**Design**:
- Per-MCP-server capability declarations in `mcp-servers.json`:
  ```json
  {
    "capabilities": {
      "allowed_hosts": ["api.github.com"],
      "allowed_paths": ["/repos/*"],
      "can_write": false,
      "can_network": true
    }
  }
  ```
- Default = read-only, no network egress
- Capability enforcement at the MCPManager transport layer
- UI: permission review dialog before activating a new MCP server
- Capabilities visible in Settings panel per-server

---

## Implementation Order

1. **Leak detection** (5.2) — lowest effort, highest immediate value, protects against accidental secret exposure
2. **Credential injection** (5.1) — enables secure use of authenticated MCP servers
3. **Capability permissions** (5.4) — limits blast radius of untrusted MCP servers
4. **OAuth 2.1 + DCR** (5.3) — enables hosted MCP servers (GitHub, etc.)

---

## Verification Checklist

- [ ] Leak detector catches a test API key in tool output and redacts it
- [ ] Credentials injected into MCP requests without appearing in tool args or LLM context
- [ ] MCP server with `can_write: false` blocks write operations
- [ ] OAuth flow completes for a hosted MCP server, tokens stored encrypted
- [ ] Settings UI shows per-server capabilities and connection status
- [ ] All security checks pass with no false positives on normal tool usage

---

## Dependencies

### Backend
- `pyahocorasick>=2.0` — Multi-pattern string matching for leak detection
- `cryptography>=43.0` — AES-256-GCM credential encryption
- `authlib>=1.3` — OAuth 2.1 + PKCE client implementation
