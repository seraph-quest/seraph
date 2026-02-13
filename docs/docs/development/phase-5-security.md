---
sidebar_position: 6
---

# Phase 5 — Security & Extensibility

**Goal**: Build a unified security and extensibility framework. Each security layer unlocks a corresponding extensibility feature — safe skill loading, authenticated MCP servers, community skill registry, and eventually a code extension layer.

**Status**: Planned

**Inspiration**: [IronClaw](https://github.com/nearai/ironclaw) (NEAR AI's OpenClaw-in-Rust rewrite — 9-layer defense-in-depth with WASM sandboxing, capability manifests, credential injection, leak detection, prompt sanitization, and content policy).

**Core principle**: Security and extensibility are the same problem. Without input validation, you can't load untrusted skill files. Without leak detection, you can't use untrusted MCP servers. Without capability permissions, you can't run community plugins. Each wave below adds security layers that unlock the next set of extensibility features.

---

## Architecture Overview

```
Wave 1 — Defense             Wave 2 — Credentials          Wave 3 — Permissions
(protects what exists)       (enables authenticated MCP)    (enables untrusted extensions)

┌────────────────────┐      ┌────────────────────┐        ┌────────────────────┐
│ 5.1 Input          │      │ 5.4 Credential     │        │ 5.6 Capability     │
│     Validation     │      │     Injection      │        │     Permissions    │
├────────────────────┤      ├────────────────────┤        ├────────────────────┤
│ 5.2 Prompt         │      │ 5.5 OAuth 2.1      │        │ 5.7 Skill          │
│     Sanitization   │      │     + DCR          │        │     Permissions    │
├────────────────────┤      └────────────────────┘        └────────────────────┘
│ 5.3 Leak           │
│     Detection      │              Wave 4 — Extensibility
├────────────────────┤              (safely enabled by waves 1-3)
│ 5.3b Content       │
│      Policy        │      ┌────────────────────────────────────┐
└────────────────────┘      │ 5.8  Skill precedence chain        │
                            │ 5.9  Richer requires gating        │
         Unlocks:           │ 5.10 Skill parameters              │
  Safe skill loading        │ 5.11 MCP tool namespacing          │
  Safe MCP tool outputs     │ 5.12 Skill composability (uses)    │
                            │ 5.13 Skill triggers                │
                            └────────────────────────────────────┘
```

---

## Wave 1 — Defense (protects what exists today)

### 5.1 Input Validation

Validate all inputs at system boundaries before they reach the agent or tools.

**Files**:
```
backend/src/security/
  validator.py             # Input validation for tool args, skill files, user messages
```

**Design** (adapted from IronClaw's `validator.rs`):
- Length validation: 1 to 100,000 bytes per input
- Null byte detection and rejection
- Forbidden pattern matching (configurable blocklist)
- Whitespace analysis (warn when >90% whitespace — likely padding attack)
- Repetition detection (>20 consecutive identical characters)
- Recursive validation for structured JSON data (tool arguments)
- Applied at three boundaries:
  - **Skill loading**: validate `.md` file content before parsing YAML
  - **Tool inputs**: validate all arguments before tool execution
  - **Tool outputs**: validate responses before LLM sees them

**IronClaw reference**: `src/safety/validator.rs` — length, null bytes, forbidden patterns, repetition, whitespace analysis.

---

### 5.2 Prompt Sanitization

Detect and neutralize prompt injection attempts in skill files, tool outputs, and MCP server responses.

**Files**:
```
backend/src/security/
  sanitizer.py             # Aho-Corasick injection detector + content escaper
```

**Design** (adapted from IronClaw's `sanitizer.rs`):
- **Aho-Corasick pattern matching** — 18+ hardcoded patterns covering:
  - Instruction overrides: "ignore previous", "forget everything", "disregard above"
  - Role manipulation: "you are now", "act as", "new instructions"
  - System message injection: "system:", "assistant:", "human:"
  - Special tokens: `<|`, `|>`, `[INST]`, `[/INST]`, `<<SYS>>`
  - Code execution indicators: `eval(`, `exec(`, `__import__`
- **Regex detection** — encoded payloads, null byte injection, base64-wrapped instructions
- **Escaping** — critical matches trigger content escaping: special tokens replaced with backslash versions, suspicious lines prepended with `[ESCAPED]`
- Applied at:
  - **Skill loading**: scan skill `.md` body before injecting into system prompt
  - **MCP tool responses**: scan before LLM context
  - **User messages**: lightweight scan (warn only, don't block users)

**IronClaw reference**: `src/safety/sanitizer.rs` — 18 Aho-Corasick patterns + 4 regex patterns + content escaping.

---

### 5.3 Leak Detection on Tool Outputs

Scan all tool responses before they enter LLM context to prevent credential exfiltration.

**Files**:
```
backend/src/security/
  leak_detector.py         # Aho-Corasick pattern scanner for secrets in tool outputs
```

**Design** (adapted from IronClaw's `leak_detector.rs`):
- Aho-Corasick multi-pattern scanner on all tool responses
- **23+ default patterns** detecting:
  - API keys: OpenAI (`sk-`), Anthropic (`sk-ant-`), AWS (`AKIA`), Google (`AIza`)
  - Tokens: GitHub (`ghp_`/`gho_`/`ghs_`), Slack (`xoxb-`/`xoxp-`), Stripe (`sk_live_`/`pk_live_`), Twilio, SendGrid
  - Private keys: PEM (`-----BEGIN`), SSH (`ssh-rsa`/`ssh-ed25519`), EC, DSA
  - Generic: Bearer tokens, auth headers, high-entropy hex strings (40+ chars)
- **Actions per pattern**: Block (reject response entirely), Redact (replace with `[REDACTED]`), Warn (log but pass through)
- Lossy UTF-8 conversion prevents bypass via invalid bytes
- `scan_http_request()` variant validates URLs, headers, and body before HTTP execution
- Configurable via settings API
- Runs synchronously in the tool response pipeline

**IronClaw reference**: `src/safety/leak_detector.rs` — 23 patterns, 4 response actions, bidirectional scanning.

---

### 5.3b Content Safety Policy

Rule-based policy engine that catches dangerous content patterns beyond credential leaks.

**Files**:
```
backend/src/security/
  policy.py                # Rule engine with severity levels and actions
```

**Design** (adapted from IronClaw's `policy.rs`):
- **Severity levels**: Low, Medium, High, Critical
- **Actions**: Warn (log only), Block (reject), Review (flag for user), Sanitize (clean and pass)
- **Default rules**:
  1. System file paths (`/etc/passwd`, `.ssh/`, `.aws/credentials`) — **Block**
  2. Shell injection patterns (`rm -rf`, piped commands with `|`, backtick execution) — **Block**
  3. Base64 decoding patterns suggesting exploit payloads — **Block**
  4. SQL commands (`DROP TABLE`, `DELETE FROM`, `TRUNCATE`) — **Warn**
  5. Excessive URL clustering (10+ consecutive links) — **Warn**
  6. Cryptocurrency key patterns (64-char hex) — **Review**
  7. Obfuscated strings (500+ chars without whitespace) — **Review**
- Applied to both tool inputs and outputs
- Configurable: users can add/remove rules via config file

**IronClaw reference**: `src/safety/policy.rs` — 7 rules, 4 severity levels, 4 actions.

**Wave 1 unlocks**: Safe loading of skill files from untrusted sources. Safe consumption of MCP tool outputs. Foundation for everything that follows.

---

## Wave 2 — Credential Security (enables authenticated MCP)

### 5.4 Credential Injection at Boundaries

MCP tools should never see raw credentials. Auth headers are injected at the transport layer by the host.

**Files**:
```
backend/src/security/
  credentials.py           # Encrypted credential storage + transport-layer injection
```

**Design** (adapted from IronClaw's `credential_injector.rs`):
- MCPManager intercepts outbound requests and injects auth headers
- Credentials stored encrypted in DB (AES-256-GCM), master key in macOS Keychain
- **Injection locations** (matching IronClaw's support):
  - `bearer` — `Authorization: Bearer <token>`
  - `basic` — `Authorization: Basic <base64(user:pass)>`
  - `header` — Custom header with optional prefix (e.g., `X-API-Key: <value>`)
  - `query_param` — URL query parameter
  - `url_path` — Placeholder substitution in URL path
- **Host matching** with wildcard support (`*.github.com`)
- Credential expiration checking
- Credentials never appear in tool arguments or LLM context

**Config** (in `mcp-servers.json`):
```json
{
  "mcpServers": {
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "enabled": true,
      "auth": {
        "type": "bearer",
        "credential_id": "github-pat",
        "host_patterns": ["api.github.com", "api.githubcopilot.com"]
      }
    }
  }
}
```

**IronClaw reference**: `src/tools/wasm/credential_injector.rs` — 5 injection locations, host matching, expiration checking.

---

### 5.5 OAuth 2.1 + Dynamic Client Registration for Hosted MCP

Support hosted MCP servers that require OAuth authentication.

**Files**:
```
backend/src/security/
  oauth.py                 # OAuth 2.1 + PKCE + Dynamic Client Registration
```

**Design** (adapted from IronClaw's `auth.rs`):
- Full OAuth 2.1 + PKCE flow for hosted MCP servers (e.g., `api.githubcopilot.com/mcp/`)
- **Dynamic Client Registration**: discover `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`, register Seraph as public client
- Token refresh with automatic renewal before expiry
- Encrypted token storage in DB via credentials module (5.4)
- Localhost callback listener on ports 9876-9886 for authorization code flow
- Fallback: manual token entry for servers without DCR
- UI: "Connect" button in Settings panel triggers OAuth flow in browser

**IronClaw reference**: `src/tools/mcp/auth.rs` — OAuth 2.1 + PKCE + DCR + local callback + token refresh.

**Wave 2 unlocks**: Secure use of authenticated MCP servers (GitHub, Google, etc.) without credential exposure. Foundation for hosted MCP ecosystem.

---

## Wave 3 — Permission Model (enables untrusted extensions)

### 5.6 Capability-Based Tool Permissions

Per-MCP-server capability declarations that limit what tools can do. Default-deny with explicit opt-in.

**Files**:
```
backend/src/security/
  capabilities.py          # Capability enforcement engine
  allowlist.py             # HTTP endpoint allowlist per server
```

**Design** (adapted from IronClaw's `capabilities_schema.rs` + `allowlist.rs`):

**Capability manifest** per MCP server in `mcp-servers.json`:
```json
{
  "mcpServers": {
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "enabled": true,
      "auth": { "type": "oauth", "credential_id": "github-oauth" },
      "capabilities": {
        "http": {
          "allowlist": [
            {
              "host": "api.github.com",
              "path_prefix": "/repos/",
              "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"]
            }
          ],
          "rate_limit": { "requests_per_minute": 60, "requests_per_hour": 500 },
          "timeout_secs": 30,
          "max_response_bytes": 1048576
        },
        "secrets": {
          "allowed_names": ["github-oauth"]
        },
        "workspace": {
          "allowed_prefixes": ["repos/", "data/"]
        }
      }
    }
  }
}
```

- **Default = deny-all**: no HTTP egress, no secrets access, no workspace access unless declared
- **HTTP allowlist enforcement**: host matching (with wildcards), path prefix, HTTP method restriction, HTTPS enforced for remote servers
- **URL security**: reject userinfo in URLs to prevent attacks like `https://api.openai.com@evil.com/`
- **Rate limiting**: per-server requests per minute/hour
- **Capability enforcement** at the MCPManager transport layer — intercept all tool requests before execution
- **UI**: permission review dialog before activating a new MCP server
- **Settings panel**: capabilities visible per-server, editable by user

**IronClaw reference**: `src/tools/wasm/capabilities_schema.rs` (capability manifest), `src/tools/wasm/allowlist.rs` (HTTP allowlist with host/path/method/HTTPS enforcement).

---

### 5.7 Skill Permission Scoping

Extend SKILL.md to declare what tools and resources a skill is allowed to use. Bridges the security model into the extensibility system.

**Files**:
```
backend/src/skills/
  loader.py                # Extended YAML parsing for permissions block
backend/src/security/
  skill_permissions.py     # Skill-scoped capability enforcement
```

**Design**:
- New optional `permissions` block in skill YAML frontmatter:
  ```yaml
  name: code-review
  description: Review code quality and style
  requires:
    tools: [shell_execute, read_file]
  permissions:
    shell_execute: read_only
    read_file: ["src/**", "tests/**"]
    network: deny
  ```
- **Permission types**:
  - Tool-level: `read_only` (strip write operations), path glob restrictions
  - Network: `allow` / `deny` (whether skill's tool calls can make HTTP requests)
  - Workspace: path prefix restrictions for file operations
- When the agent runs with this skill active, `create_agent()` wraps tools with permission-filtered proxies
- Skills without `permissions` block get full access (backwards compatible)
- Skills from untrusted sources (community registry, wave 4) require `permissions` block

**IronClaw parallel**: IronClaw achieves this via WASM sandbox + `capabilities.json` per tool. Seraph achieves similar isolation via tool-wrapping proxies since WASM is impractical in a Python/Docker architecture.

**Wave 3 unlocks**: Safe use of community-contributed skills and untrusted MCP servers. Foundation for skill registry and code extensions.

---

## Wave 4 — Extensibility Features (safely enabled by waves 1-3)

With security layers in place, these extensibility features can be implemented safely.

### 5.8 Skill Precedence Chain

Load skills from three directories with override priority:

```
data/skills/               # project-bundled (lowest priority)
~/.seraph/skills/          # user-global
workspace/skills/          # workspace-specific (highest priority)
```

**Design**:
- `loader.py` scans all three directories, deduplicates by `name` field with precedence
- Allows users to override bundled skills per-workspace without modifying bundled files
- User-global skills persist across projects
- Input validation (5.1) + prompt sanitization (5.2) applied to all sources

---

### 5.9 Richer `requires` Gating

Expand skill `requires` beyond tool names:

```yaml
requires:
  tools: [shell_execute]
  env: [GITHUB_TOKEN]              # env var must be set
  mcp_servers: [github]            # named MCP server must be connected
```

**Design**:
- `skill_manager.get_active_skills()` checks all requirement types
- Missing env vars or disconnected MCP servers exclude the skill silently
- UI shows unmet requirements in Settings panel skill list

---

### 5.10 Skill Parameters

Allow skills to accept typed input:

```yaml
parameters:
  days:
    type: integer
    default: 7
    description: Number of days to include in standup
  format:
    type: string
    enum: [markdown, json, plain]
    default: markdown
```

**Design**:
- Parameters are metadata in the YAML — the agent interprets them via the prompt
- When user invokes a skill, parameter schema is included in the injected instructions
- No code needed — the LLM reads the schema and asks the user for values or uses defaults
- Input validation (5.1) applied to parameter values

---

### 5.11 MCP Tool Namespacing

Prefix MCP tools with server name to prevent name collisions:

```
github:create_issue
things3:create_task
```

**Design**:
- `mcp_manager.get_tools()` prefixes each tool name with `{server_name}:`
- Agent sees namespaced names, MCPManager strips prefix before dispatch
- Collision detection logs warnings when two servers expose same tool name
- Native tools (from `src/tools/`) keep unprefixed names

---

### 5.12 Skill Composability

Let skills reference other skills via `uses`:

```yaml
name: weekly-review
requires:
  tools: [get_goals, view_soul]
uses: [daily-standup, goal-reflection]
---
First run the daily-standup skill for the past 7 days,
then run goal-reflection, then synthesize into a weekly review.
```

**Design**:
- Loader resolves `uses` as a DAG — if `daily-standup` requires `shell_execute` and it's unavailable, `weekly-review` is also excluded
- Instructions from `uses` skills are available in context (injected before the composing skill)
- Circular dependency detection at load time
- Permissions are the union of all composed skills (most restrictive wins per tool)

---

### 5.13 Skill Triggers

Let skills declare when they should activate:

```yaml
triggers:
  - on: schedule
    cron: "0 9 * * 1"                    # every Monday at 9am
  - on: message_contains
    pattern: "standup|stand-up"
  - on: mcp_event
    server: github
    event: pull_request.opened
```

**Design**:
- `schedule` triggers register APScheduler jobs in `scheduler/engine.py`
- `message_contains` triggers are checked in the WS message handler before agent creation
- `mcp_event` triggers subscribe to MCP server event streams (future MCP spec feature)
- Triggered skills run through the full security pipeline (validation, sanitization, leak detection, permissions)

---

## What We Don't Adopt from IronClaw

**WASM sandboxing** — IronClaw runs tools in WebAssembly containers because it's a Rust project where WASM is natural. For Seraph (Python + Docker), this doesn't make sense. We already have snekbox for shell sandboxing. For MCP tools, the transport-layer enforcement (capability permissions + HTTP allowlist + credential injection + leak detection) achieves equivalent isolation without the complexity of a WASM runtime in Python.

**Rewrite in Rust** — IronClaw exists because OpenClaw's TypeScript/Node.js has inherent security issues (prototype pollution, dynamic typing). Python doesn't share those vulnerability classes, and our Docker-based architecture provides process-level isolation.

**Tool aliasing/indirection** — IronClaw uses alias-only tool invocation (WASM tools call aliases, never real names). This adds complexity that's warranted in a WASM sandbox where tools can call other tools. In Seraph, tools are invoked by the agent (not by other tools), so the attack surface is different.

---

## Implementation Order

### Wave 1 — Defense (protects what exists)
1. **Input validation** (5.1) — lowest effort, foundation for everything
2. **Prompt sanitization** (5.2) — protects against injection in skill files and tool outputs
3. **Leak detection** (5.3) — highest immediate value, protects against accidental secret exposure
4. **Content policy** (5.3b) — catches dangerous patterns beyond credentials

### Wave 2 — Credentials (enables authenticated MCP)
5. **Credential injection** (5.4) — enables secure use of authenticated MCP servers
6. **OAuth 2.1 + DCR** (5.5) — enables hosted MCP servers (GitHub, Google, etc.)

### Wave 3 — Permissions (enables untrusted extensions)
7. **Capability permissions** (5.6) — limits blast radius of untrusted MCP servers
8. **Skill permission scoping** (5.7) — limits blast radius of untrusted skills

### Wave 4 — Extensibility (safely unlocked)
9. **Skill precedence chain** (5.8) — workspace > user > bundled
10. **Richer requires gating** (5.9) — env vars, MCP servers
11. **Skill parameters** (5.10) — typed input for invocable skills
12. **MCP tool namespacing** (5.11) — collision prevention
13. **Skill composability** (5.12) — skills referencing other skills
14. **Skill triggers** (5.13) — schedule, pattern match, events

---

## Verification Checklist

### Wave 1
- [ ] Input validator rejects null bytes, excessive length, repetition patterns
- [ ] Prompt sanitizer detects "ignore previous instructions" in a skill file and escapes it
- [ ] Leak detector catches a test API key (`sk-test123...`) in tool output and redacts it
- [ ] Content policy blocks `rm -rf /` in a shell_execute tool argument
- [ ] All security checks pass with no false positives on normal tool usage

### Wave 2
- [ ] Credentials injected into MCP requests without appearing in tool args or LLM context
- [ ] OAuth flow completes for a hosted MCP server (GitHub), tokens stored encrypted
- [ ] Token refresh works automatically before expiry
- [ ] Credential injection supports bearer, basic, header, query_param locations

### Wave 3
- [ ] MCP server with `http.allowlist` blocks requests to undeclared hosts
- [ ] MCP server without capabilities declaration gets default deny-all
- [ ] Skill with `permissions.network: deny` cannot trigger HTTP calls via tools
- [ ] Skill with `permissions.read_file: ["src/**"]` cannot read files outside `src/`
- [ ] Settings UI shows per-server capabilities and per-skill permissions

### Wave 4
- [ ] Workspace skill overrides bundled skill with same name
- [ ] Skill with `requires.env: [GITHUB_TOKEN]` excluded when env var unset
- [ ] Skill parameters displayed to agent, agent asks user for values
- [ ] MCP tools namespaced as `server:tool_name`, no collision
- [ ] Composed skill excluded when dependency skill's requirements unmet
- [ ] Scheduled skill trigger fires and runs through full security pipeline

---

## Dependencies

### Backend
- `pyahocorasick>=2.0` — Multi-pattern string matching for leak detection + prompt sanitization
- `cryptography>=43.0` — AES-256-GCM credential encryption
- `authlib>=1.3` — OAuth 2.1 + PKCE client implementation

### Existing (no new deps)
- `apscheduler` — Already used; skill triggers (5.13) register jobs
- `smolagents.MCPClient` — Already used; capability enforcement wraps existing transport
- `pyyaml` — Already used; extended skill YAML parsing

---

## Security Pipeline (defense-in-depth flow)

```
User message / MCP tool response / Skill file content
  │
  ▼
Layer 1: Input Validation (5.1)
  Length, null bytes, forbidden patterns, repetition, whitespace
  │
  ▼
Layer 2: Prompt Sanitization (5.2)
  18+ injection patterns, regex detection, content escaping
  │
  ▼
Layer 3: Content Policy (5.3b)
  7 rules: system paths, shell injection, SQL, obfuscation
  │
  ▼
Layer 4: Leak Detection (5.3)
  23+ patterns: API keys, tokens, private keys, high-entropy strings
  │
  ▼
Layer 5: Capability Enforcement (5.6)
  HTTP allowlist, rate limits, workspace path restrictions
  │
  ▼
Layer 6: Credential Injection (5.4)
  Host-boundary only — tools never see secrets
  │
  ▼
Layer 7: Skill Permissions (5.7)
  Per-skill tool restrictions, network policy, path scoping
  │
  ▼
  Safe execution
```

Not all layers apply to every input type. User messages go through layers 1-2 (warn only). Skill files go through 1-2 on load. Tool inputs go through 1, 3, 5-7. Tool outputs go through 1-4.
