---
sidebar_position: 3
---

# Research: Recursive Delegation Architecture for Seraph

**Date**: 2026-02-13
**Status**: Implemented (depth-1 delegation behind feature flag, Phase 4.4)
**References**: [Recursive Language Models (arXiv:2512.24601)](https://arxiv.org/pdf/2512.24601), [Smolagents Multi-Agent Docs](https://smolagents.org/docs/orchestrate-a-multi-agent-system-%F0%9F%A4%96%F0%9F%A4%9D%F0%9F%A4%96/)

---

## 1. Core Idea

The paper proposes that an LLM agent should **never execute tools in the root context**. Instead, it should decompose the user's request and delegate all tool execution to subagents. Subagents can recursively delegate further. This creates a tree of isolated execution contexts, each with a focused scope and minimal context pollution.

**Current Seraph model** (single flat agent):
```
User → [Orchestrator + ALL Tools] → Response
```

**Proposed model** (recursive delegation):
```
User → [Orchestrator (no tools)] → delegates to →
    [Specialist A (domain tools)] → executes tools → returns result
    [Specialist B (domain tools)] → may delegate further →
        [Worker C (leaf tools)] → executes → returns
    ← Orchestrator synthesizes final response
```

---

## 2. Why This Matters for Seraph

### Current Pain Points
1. **Growing tool surface**: Seraph has ~12 built-in tools + N MCP tools. As MCP servers are added (Things3, GitHub, calendar, etc.), the root agent's tool list grows unboundedly. LLMs degrade with too many tools — they hallucinate tool names, pick wrong tools, or miss relevant ones.
2. **Context pollution**: Tool outputs (web page content, file contents, shell output) consume root context window tokens. A `browse_webpage` result can be 4K+ tokens, leaving less room for reasoning.
3. **Monolithic failure**: If `shell_execute` times out or `browse_webpage` errors, the root agent's chain of thought is disrupted. It must recover in the same context where it was reasoning about the user's intent.
4. **No specialization**: Every tool call uses the same temperature (0.7), same max_steps (10), same instructions. A web research task has different needs than a soul introspection task.

### Expected Benefits
1. **Context isolation**: Root agent stays focused on understanding intent and synthesizing responses. Tool outputs never enter its context window.
2. **Error isolation**: A failed web search in a subagent returns a clean error summary, not a stack trace polluting the orchestrator.
3. **Tool scaling**: Each specialist only sees its 2-4 tools. The orchestrator sees specialists (as managed agents), not raw tools. Adding an MCP server adds tools to one specialist, not to the root.
4. **Tunable specialization**: Research specialist can have temp=0.3, creative specialist temp=0.8. Memory specialist gets max_steps=3, research specialist gets max_steps=8.
5. **Better observability**: Each delegation creates a clear audit trail — "orchestrator delegated to memory_specialist which called view_soul".

---

## 3. Smolagents Built-in Support

Smolagents has **first-class managed agent support** — no custom framework needed.

### How It Works

```python
# Managed agents are just regular ToolCallingAgent instances
specialist = ToolCallingAgent(
    tools=[web_search, browse_webpage],
    model=model,
    name="web_researcher",
    description="Searches the web and reads pages to find information.",
    max_steps=5,
)

# Parent agent receives them via managed_agents parameter
orchestrator = ToolCallingAgent(
    tools=[],                          # No tools! Delegation only.
    model=model,
    managed_agents=[specialist],       # Specialists appear as callable tools
    max_steps=8,
)

# When orchestrator runs, its prompt includes:
# "You can also give tasks to team members.
#  - web_researcher: Searches the web and reads pages..."
#
# The LLM calls them like tools: web_researcher(task="Find info about X")
```

### Key Implementation Details (from smolagents source)

| Aspect | How It Works |
|--------|--------------|
| **Discovery** | `tools_and_managed_agents` property merges tools + agents into one dict |
| **Invocation** | `execute_tool_call()` detects if target is an agent, calls `agent(task=...)` |
| **Agent `__call__`** | Wraps task with `managed_agent.task` prompt, runs full ReAct loop, formats output with `managed_agent.report` template |
| **Nesting** | Any `MultiStepAgent` can have `managed_agents`, which can themselves have managed agents — arbitrary depth |
| **Context** | Each managed agent gets its own context window. Only the final report flows back to the parent. |
| **Naming** | Must be valid Python identifiers, unique within parent |

### What We Get for Free
- Prompt injection for specialist descriptions
- Automatic task wrapping and report formatting
- Step-by-step execution within each agent
- Error handling ("team member" error messages)
- Run summaries if `provide_run_summary=True`

### What We Need to Build
- Specialist agent factory functions (tool grouping, instructions, tuning)
- Streaming bridge (subagent steps → WS messages)
- Depth control (smolagents doesn't enforce max recursion)
- Frontend adaptation (multi-level step display)

---

## 4. Proposed Specialist Domains

Based on Seraph's current tool set and usage patterns:

### Tier 1 Specialists (always available)

| Specialist | Tools | Purpose | Tuning |
|-----------|-------|---------|--------|
| `memory_keeper` | `view_soul`, `update_soul` | Identity introspection, soul file updates | temp=0.5, max_steps=3 |
| `goal_planner` | `create_goal`, `update_goal`, `get_goals`, `get_goal_progress` | Goal CRUD and progress analysis | temp=0.4, max_steps=5 |
| `web_researcher` | `web_search`, `browse_webpage` | Information gathering from the internet | temp=0.3, max_steps=8 |
| `file_worker` | `read_file`, `write_file`, `fill_template`, `shell_execute` | File operations and code execution | temp=0.3, max_steps=6 |

### Tier 2 Specialists (dynamic, based on available MCP tools)

| Specialist | Tools | When Available |
|-----------|-------|----------------|
| `mcp_{server_name}` | All tools from that MCP server | When MCP server is enabled |

Each enabled MCP server becomes its own specialist. This solves the tool scaling problem — adding a GitHub MCP server creates a `mcp_github` specialist with all GitHub tools, visible to the orchestrator as a single "team member."

### Orchestrator (root agent)

- **Tools**: None (or minimal: perhaps `final_answer` only)
- **Managed agents**: All Tier 1 + Tier 2 specialists
- **Instructions**: Enriched with soul context, memory context, conversation history, and active skills (as today)
- **Temperature**: 0.7 (same as current)
- **Max steps**: 8 (reduced from 10 — each "step" is now a delegation, not a tool call)
- **Role**: Understand user intent, decide which specialist(s) to invoke, synthesize their results into a coherent response

---

## 5. Recursion Depth Analysis

### Depth 1: Orchestrator → Specialists (execute tools directly)

```
Orchestrator (no tools)
  └─ Specialist (has tools, executes them)
```

**Pros**: Simple, easy to stream, low latency overhead
**Cons**: No further decomposition. A "research and write a report" task forces one specialist to do both.
**Verdict**: Sufficient for most Seraph interactions. This should be the **default**.

### Depth 2: Orchestrator → Specialists → Workers

```
Orchestrator (no tools)
  └─ Specialist (may have tools AND sub-specialists)
      └─ Worker (leaf, has tools)
```

**Pros**: Complex multi-step tasks can be decomposed further
**Cons**: Higher latency, harder to stream, more tokens spent on delegation overhead
**When useful**: MCP server with 20+ tools (e.g., full GitHub API) could internally delegate to sub-specialists

### Depth 3+: Diminishing returns

**Pros**: Theoretically elegant
**Cons**: Each level adds ~500ms+ latency (LLM call for delegation decision) + ~200-500 tokens overhead. Context loss compounds. Debugging becomes very difficult.
**Verdict**: Almost certainly overkill for Seraph's use case.

### Recommendation: **Max depth 2, default depth 1**

- Orchestrator → Specialists is the standard path (depth 1)
- MCP specialists with 10+ tools MAY internally delegate to sub-groups (depth 2)
- Hard cap at depth 2 — enforce via configuration, not by trusting the LLM
- Implementation: pass `depth` parameter to specialist factory, only allow `managed_agents` when `depth < max_depth`

---

## 6. Streaming Challenges

This is the **hardest part** of the implementation. Currently, Seraph streams agent steps directly to the frontend:

```python
# Current: flat step stream
for step in agent.run(message, stream=True):
    if isinstance(step, ToolCall):
        ws.send({"type": "step", "content": f"Calling {name}..."})
    elif isinstance(step, FinalAnswerStep):
        ws.send({"type": "final", "content": step.output})
```

With delegation, the orchestrator's steps are **delegation calls**, not tool calls. The actual tool execution happens inside managed agents, which have their own step streams that smolagents handles internally.

### Options

**Option A: Opaque delegation (simplest)**
- Stream orchestrator steps only
- When orchestrator calls a specialist, show "Delegating to web_researcher..."
- When specialist returns, show result summary
- User doesn't see individual tool calls inside specialists
- **Pro**: Minimal frontend changes. **Con**: Less transparent.

**Option B: Flattened step stream (most transparent)**
- Hook into managed agent execution to capture inner steps
- Forward inner steps to WS with metadata: `{specialist: "web_researcher", step: 2}`
- Frontend groups steps by specialist
- **Pro**: Full visibility. **Con**: Requires hooking into smolagents internals or custom agent subclass.

**Option C: Two-level stream (balanced)**
- Orchestrator steps stream as today (delegation = step)
- Each specialist's final report is streamed as a step with specialist attribution
- Individual tool calls within specialists are logged but not streamed to frontend
- **Pro**: Clean UX. **Con**: Tool-triggered magic effects won't fire for inner tool calls.

### Recommendation: **Option C initially, migrate to B later**

Option C preserves the current UX (magic effects trigger when orchestrator delegates, which looks like "casting"), and doesn't require deep smolagents subclassing. We can add inner-step streaming later.

### Frontend Animation Impact

Current animation state machine: `THINKING → tool detected → CASTING + MagicEffect → SPEAKING → IDLE`

With delegation, a "tool call" becomes a specialist invocation. The mapping changes:

| Event | Current | Proposed |
|-------|---------|----------|
| Agent starts | THINKING | THINKING |
| Tool call detected | CASTING | — |
| Delegation detected | — | CASTING (specialist name → effect) |
| Specialist returns | — | Continue THINKING or next CASTING |
| Final answer | SPEAKING → IDLE | SPEAKING → IDLE |

The `toolParser.ts` regex patterns would need updating to detect delegation patterns instead of (or in addition to) raw tool names. The tool → building mapping in `animationStateMachine.ts` could map specialist names to buildings instead.

---

## 7. Impact on Existing Seraph Components

### Must Change

| Component | Current | After |
|-----------|---------|-------|
| `agent/factory.py` | `create_agent()` returns single agent with all tools | `create_orchestrator()` returns agent with managed specialists |
| `api/ws.py` | Streams flat steps from one agent | Streams orchestrator steps (delegations + final) |
| `api/chat.py` | Same as ws.py but blocking | Same change |
| `native_tools/loader.py` | `discover_tools()` returns flat list | Also needs `discover_tools_by_domain()` for specialist grouping |
| `config/settings.py` | Agent settings (max_steps, timeout) | Per-specialist settings or specialist config |

### May Change

| Component | Impact |
|-----------|--------|
| `tools/mcp_manager.py` | MCP tools grouped into per-server specialists |
| `agent/onboarding.py` | Could remain flat (only 4 tools, delegation is overhead) |
| `agent/strategist.py` | Could remain flat (only 3 tools, delegation is overhead) |
| `skills/manager.py` | Skills may need to target specialists, not the root agent |
| Frontend `toolParser.ts` | Detect specialist delegation instead of raw tool names |
| Frontend `animationStateMachine.ts` | Map specialists → buildings/effects |
| Frontend `chatStore.ts` / `MessageBubble` | Display specialist attribution on steps |

### Should NOT Change

| Component | Why |
|-----------|-----|
| `agent/context_window.py` | Orchestrator still needs conversation history |
| `memory/` | Memory injection stays at orchestrator level |
| `observer/` | Context awareness stays at orchestrator level |
| `scheduler/` | Jobs invoke the orchestrator, not specialists directly |
| `db/` | Message storage unchanged |

---

## 8. Cost and Latency Analysis

### Current (single agent)
```
User message → 1 LLM call (reasoning) → N tool calls → 1 LLM call (synthesis) → response
Total: 2 + N×tool_latency LLM calls
```

### With delegation (depth 1)
```
User message → 1 LLM call (orchestrator decides) → M specialist delegations:
  each specialist: 1 LLM call (reasoning) → K tool calls → 1 LLM call (report)
→ 1 LLM call (orchestrator synthesizes)
Total: 2 + M×(2 + K×tool_latency) LLM calls
```

### Overhead per delegation
- ~1-2 additional LLM calls per specialist invocation
- ~200-500 tokens for delegation prompt + report formatting
- ~500ms-2s additional latency per delegation round-trip

### Mitigation
- **Parallel delegation**: If orchestrator delegates to 2+ specialists, they could run concurrently (smolagents doesn't support this natively — would need custom implementation)
- **Shallow paths**: Most user messages need 0-1 tool calls. For simple "how are my goals?" → `goal_planner(task="summarize current goals")` adds 1 extra LLM call.
- **Skip delegation for simple tasks**: Orchestrator could have a small set of "fast path" tools (e.g., `view_soul`) for trivial requests. This violates the pure model but is pragmatic.

### Estimated impact
- **Simple queries** (no tools): ~0ms overhead (no delegation needed, orchestrator answers directly)
- **Single-tool queries**: +1-3s latency (one delegation round-trip)
- **Multi-tool complex tasks**: +2-5s latency but better quality due to focused specialist contexts
- **Token cost**: ~20-40% increase for tool-using queries

---

## 9. Skill System Integration

Skills (`data/skills/*.md`) currently inject instructions into the root agent's prompt. With delegation:

### Option A: Skills stay at orchestrator level
- Skills inject into orchestrator instructions
- Orchestrator interprets skill context when deciding which specialist to delegate to
- **Pro**: No change to skill system. **Con**: Specialist doesn't see skill instructions.

### Option B: Skills target specific specialists
- Skill frontmatter gets new field: `target_specialist: web_researcher`
- Skill instructions inject into that specialist's prompt only
- **Pro**: Focused context. **Con**: More complex skill system.

### Option C: Skills inject into both
- Orchestrator sees skill for delegation decisions
- Relevant specialist sees skill for execution details
- **Pro**: Best of both. **Con**: Token duplication.

### Recommendation: **Option A initially**
Keep it simple. Skills give high-level behavioral directives that the orchestrator can relay as part of the delegation task description. If we find specialists need direct skill context, migrate to Option B.

---

## 10. Migration Strategy

### Phase 1: Specialist Factory (low risk, no behavior change)
- Create `agent/specialists.py` with factory functions for each specialist domain
- Group tools by domain
- Write tests for specialist creation
- **No integration yet** — just building blocks

### Phase 2: Orchestrator Factory (medium risk)
- Create `create_orchestrator()` in `agent/factory.py` alongside existing `create_agent()`
- Orchestrator uses managed_agents from Phase 1
- Feature-flagged: `settings.use_delegation: bool = False`
- When flag is off, current `create_agent()` path is used
- When flag is on, `create_orchestrator()` path is used
- **Allows A/B comparison**

### Phase 3: Streaming Adaptation (medium risk)
- Modify WS handler to detect delegation steps vs tool steps
- Implement Option C streaming (two-level)
- Update frontend toolParser for specialist detection
- Magic effect mapping for specialists

### Phase 4: MCP Specialist Autogeneration (low risk)
- `mcp_manager.py` generates one specialist per enabled MCP server
- Orchestrator dynamically receives MCP specialists
- Tool count explosion → specialist count (manageable)

### Phase 5: Depth 2 for Large MCP Servers (optional)
- MCP servers with 10+ tools get internal sub-specialists
- Configurable per-server via `mcp-servers.json`

---

## 11. Open Questions

1. **Should the orchestrator have ANY tools?** Pure model says no. Pragmatic model gives it `view_soul` for identity-aware responses without delegation overhead. What's the right tradeoff?

2. **How to handle specialist failures?** If `web_researcher` times out, should orchestrator retry? Delegate to a different specialist? Return partial results?

3. **Parallel delegation**: Smolagents runs managed agents sequentially. For "search the web AND check my goals", could we run specialists concurrently? This would require a custom orchestrator loop.

4. **Onboarding and strategist**: These already-specialized agents have 3-4 tools. Should they adopt delegation too, or stay flat? (Recommendation: stay flat — delegation overhead isn't justified for &lt;5 tools.)

5. **Token budget distribution**: If orchestrator has 12K token budget for history, and delegates to a specialist that also needs history context... does the specialist get a separate budget? Or does the orchestrator pass a summary?

6. **Observability**: How do we log/display the full delegation tree for debugging? Each specialist's steps are internal to smolagents. Do we need to subclass to capture them?

7. **Cost tracking**: With 2-3x more LLM calls per interaction, how do we track per-user token spend? Should we add cost awareness to the orchestrator's decision-making?

---

## 12. Recommendation

**Implement depth-1 delegation behind a feature flag.**

The smolagents framework gives us managed agent support for free. The main work is:
1. Grouping tools into specialist domains (~2 days)
2. Creating the orchestrator factory (~1 day)
3. Adapting WS streaming for delegation steps (~2 days)
4. Frontend updates for specialist-level step display (~1 day)
5. Tests (~1 day)

Start with the feature flag approach (Phase 2 above) so we can compare delegation vs flat agent quality and latency. If delegation proves beneficial, make it the default and remove the flag. If not, we've learned something valuable with minimal disruption.

**Do NOT attempt depth 2+ until depth 1 is proven.** The overhead compounds and the debugging complexity increases non-linearly.
