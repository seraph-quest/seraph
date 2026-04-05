---
title: 14. Seraph Memory SOTA Roadmap
---

# 14. Seraph Memory SOTA Roadmap

## Goal

Define a research-backed plan for turning Seraph's memory system from a solid foundation into a genuinely best-in-class guardian memory stack.

This document answers:

- what Hermes and OpenClaw each get right about memory
- what recent memory research changes the design target
- where Seraph's current memory stack is structurally limited
- what target architecture Seraph should build instead
- how to implement that target in realistic phases inside this repo

## Scope And Clarification

In this document, `Hermes` means the **Nous Research Hermes Agent** memory system documented in early 2026, not Seraph's internal `Hermes Session Memory` extension pack.

This file is about **memory architecture**, not only retrieval quality. For Seraph, the real objective is:

- better recall
- better continuity
- better intervention timing
- better adaptation to the human
- lower online token cost
- stronger provenance and safer forgetting

## Executive Summary

Seraph should not try to beat Hermes by making one bigger prompt block, and it should not try to beat OpenClaw by only adding better search knobs.

The winning design is:

1. keep a **Hermes-like bounded memory layer** for cheap, always-on context
2. add an **OpenClaw-like hybrid retrieval layer** for precise recall
3. adopt a **LightMem-style layered pipeline** with online filtering and offline consolidation
4. add a **MemoryBank-style reinforcement and decay model** so memory quality improves over time instead of only growing
5. add a **MemGPT-style tiered memory manager** so Seraph explicitly decides what stays in prompt, what stays searchable, and what stays archived
6. make the core memory unit a **typed, source-backed claim/event/entity record**, not a flat text blob
7. add a **Hermes-style additive memory-provider adapter layer** so Seraph can keep a guardian-first canonical memory model while still augmenting retrieval, user modeling, or consolidation with pluggable external memory systems

If Seraph does that well, it can surpass both reference systems because it has one advantage they do not use as deeply: observer context, project state, intervention outcomes, and guardian policy can all feed the memory system.

## Evidence Base

### Official product/system references

- Hermes Agent memory docs:
  - https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/
- OpenClaw memory docs:
  - https://github.com/openclaw/openclaw/blob/main/docs/concepts/memory.md

### Primary research sources

- MemoryBank: Enhancing Large Language Models with Long-Term Memory
  - arXiv:2305.10250
  - https://arxiv.org/abs/2305.10250
- MemGPT: Towards LLMs as Operating Systems
  - submitted October 12, 2023; revised February 12, 2024
  - arXiv:2310.08560
  - https://arxiv.org/abs/2310.08560
- Evaluating Very Long-Term Conversational Memory of LLM Agents
  - submitted February 27, 2024
  - arXiv:2402.17753
  - https://arxiv.org/abs/2402.17753
- LightMem: Lightweight and Efficient Memory-Augmented Generation
  - submitted October 21, 2025; revised February 28, 2026
  - arXiv:2510.18866
  - https://arxiv.org/abs/2510.18866

### Current Seraph implementation surfaces reviewed

- `backend/src/memory/vector_store.py`
- `backend/src/memory/consolidator.py`
- `backend/src/memory/soul.py`
- `backend/src/guardian/state.py`
- `backend/src/guardian/world_model.py`
- `backend/src/tools/session_search_tool.py`
- `backend/src/agent/session.py`
- `backend/config/settings.py`
- `docs/research/02-human-model-and-memory.md`

## What Hermes Gets Right

Hermes is not trying to build the deepest possible memory graph. It is trying to build **fast, bounded, useful memory that actually stays in use**.

Key strengths:

- two sharply defined stores:
  - `MEMORY.md` for environment, project, and agent notes
  - `USER.md` for user identity and preferences
- both stores are small enough to remain prompt-friendly
- the memory block is frozen at session start, which preserves prefix-cache efficiency
- memory edits are agent-managed and persisted immediately
- `session_search` is clearly separated from persistent memory
- the docs are explicit about what should be saved versus skipped
- the memory layer is security-scanned because it is prompt-injected
- Hermes now also ships seven additive external memory provider plugins, so the bounded built-in layer is no longer its whole memory story

The important lesson for Seraph is not "copy two markdown files." The lesson is:

- bounded memory should be a first-class product surface
- always-on memory should be curated, not auto-expanded without limit
- active-session performance matters
- memory and session recall should be separate tools with separate cost profiles
- external memory should be additive and pluggable rather than forcing one monolithic backend to carry every retrieval and user-modeling job

That means the next structural memory follow-through for Seraph is not only “more memory internals.” It is also:

- a provider-neutral memory adapter boundary
- safe mapping between canonical guardian memory and optional external providers
- keeping Seraph’s typed guardian memory authoritative even when external providers assist with retrieval, user modeling, or consolidation

## What OpenClaw Gets Right

OpenClaw's memory direction is stronger on retrieval engineering than Hermes.

Key strengths called out in the official docs:

- semantic vector index over memory notes
- optional hybrid search combining lexical and vector retrieval
- MMR diversity reranking
- temporal decay
- optional advanced sidecar retrieval backends
- pre-compaction "memory flush" so durable memory is written before context collapses

The important lesson for Seraph is:

- memory quality depends on write timing as much as retrieval
- semantic recall alone is not enough
- lexical, temporal, and diversity-aware signals materially help
- memory should be updated at lifecycle boundaries, not only after the whole session

## What Recent Research Changes

### MemoryBank

MemoryBank's lasting contribution is not the exact architecture. It is the idea that long-term memory should be **reinforced, forgotten, and updated over time**.

What matters for Seraph:

- memory entries should have reinforcement strength
- stale, unconfirmed, or contradicted memory should decay
- memory should not be treated as permanent truth once written

### MemGPT

MemGPT frames memory as **explicit tier management**. That is highly relevant to Seraph.

What matters for Seraph:

- fast prompt memory and slow external memory must be treated as different tiers
- the system should decide what gets promoted or paged into context
- context pressure is a memory-management problem, not only a summarization problem

### LoCoMo

LoCoMo is useful because it shows that long-context and naive RAG are still weak on:

- temporal reasoning
- causal reasoning
- multi-session continuity
- remembering who did what when

For Seraph, this means "bigger context" is not the answer. Seraph needs structured episodic memory with time, source, and entity links.

### LightMem

LightMem is the most directly applicable current template.

What matters for Seraph:

- three-stage memory is effective:
  - sensory filtering
  - short-term topical organization
  - long-term offline consolidation
- offline consolidation dramatically lowers online cost
- memory architecture should be measured on both quality and efficiency

## Current Seraph Assessment

Seraph already has a real memory stack, but it is still a first-generation one.

### What exists today

- a `soul.md` identity record
- a LanceDB vector table for long-term text memories
- a background consolidation pass after sessions
- a token-aware conversation summarizer
- a bounded recall summary built from soul plus todos
- session search over prior conversations
- a guardian world model that tries to consume memory-derived signals

### Current strengths

- Seraph already thinks in terms of guardian state and world model, not only retrieval
- Seraph has observer context and intervention feedback, which are excellent future memory inputs
- Seraph already distinguishes some memory categories
- Seraph already has a place where memory affects downstream decision-making

### Current architectural limitations

#### 1. Flat memory records

The vector store schema is only:

- `id`
- `text`
- `category`
- `source_session_id`
- `vector`
- `created_at`

That is too thin for SOTA memory. It cannot represent:

- confidence
- provenance beyond one session id
- affected entity or project
- whether the item is an event or stable fact
- contradiction state
- importance or reinforcement
- validity interval
- user-facing privacy sensitivity

#### 2. One-shot extraction writer

The main writer path is one extraction prompt over the last 30 messages. It returns lists of strings in a few coarse categories. That is better than nothing, but it is not enough for durable high-quality memory.

Current missing steps:

- entity linking
- merge/update logic
- contradiction detection
- reinforcement scoring
- project/thread assignment
- event extraction
- observer/tool-output fusion

#### 3. Soul is not a structured model

The soul file is useful as a product artifact, but it is currently a markdown overwrite surface rather than a structured user-model projection.

The target should be:

- structured underlying model
- markdown or prompt view generated from that model

#### 4. Retrieval is mostly single-shot semantic recall

`build_guardian_state()` mainly issues one memory search from the current user message and turns the result into lines of text.

That misses several important retrieval modes:

- "what happened last week?"
- "what do we know about this collaborator?"
- "what commitments are still open?"
- "what intervention style has worked recently?"
- "what project is this likely about?"

#### 5. Session search is still a narrow text search lane

The current session search is useful, but it is not yet:

- FTS-based
- hybrid lexical/semantic
- event-aware
- summarization-aware
- thread- or entity-aware

#### 6. World-model slots are underfed

The world model already has slots like:

- `collaborators`
- `recurring_obligations`
- `project_timeline`
- `active_routines`

But the memory writer does not reliably generate those categories, so the world model is often trying to reason from sparse hints instead of well-formed structured memory.

## Design Principles For Seraph Memory V2

### 1. Memory should optimize behavior, not storage volume

The right test is not "did Seraph store more things?" The right test is:

- did Seraph interrupt better?
- did Seraph remember commitments correctly?
- did Seraph adapt to the user's real preferences?
- did Seraph make fewer repeated mistakes?

### 2. Every memory should have a type

At minimum, Seraph should distinguish:

- `episodic_event`
- `semantic_fact`
- `preference`
- `goal_or_commitment`
- `routine`
- `constraint`
- `collaborator`
- `project`
- `timeline_milestone`
- `intervention_outcome`
- `communication_preference`
- `tool_or_workflow_lesson`

### 3. Every durable memory should have provenance

No durable item should exist without:

- source session/message ids or source event ids
- creation time
- last confirmation time
- writer path
- confidence

### 4. Memory should support update, not only append

Seraph needs first-class:

- create
- merge
- strengthen
- weaken
- contradict
- supersede
- archive
- forget

### 5. Online and offline memory work should be separated

Online path:

- fast
- bounded
- low-latency
- focused on current thread and immediate recall

Offline path:

- richer extraction
- cross-session synthesis
- contradiction cleanup
- timeline building
- pattern learning

## Target Architecture

## Layer 1: Bounded Working Memory

This is Seraph's always-on session-start snapshot.

It should include only the highest-value, low-volatility state:

- user identity summary
- stable communication preferences
- active projects
- active collaborators
- open commitments
- known recurring constraints
- current thread summary
- top routines and intervention guidance

This layer should stay compact and deterministic. Hermes is right here.

Implementation rule:

- build this snapshot from structured state
- do not make it the source of truth
- regenerate it at session start and on explicit refresh boundaries

## Layer 2: Episodic Memory

This stores timestamped events and observations:

- conversation events
- commitments made
- decisions made
- tasks completed
- meetings referenced
- observed screen/project transitions
- important tool outcomes
- interventions sent and feedback received

This layer must be:

- time-aware
- source-aware
- queryable by entity, project, and thread

This is what LoCoMo exposes as difficult and what Seraph needs for real continuity.

## Layer 3: Semantic Memory

This stores generalized stable knowledge about the human and their environment:

- preferences
- project facts
- collaborator relationships
- recurring obligations
- routines
- known constraints
- durable values and goals

This layer should be synthesized from episodic evidence plus direct user statements.

## Layer 4: Procedural Memory

This stores what kinds of actions work.

Examples:

- direct interruption during meetings is poorly received
- async native delivery works better for blocked-state nudges
- daily planning nudges are effective in the morning but not late evening
- user responds better to brief literal phrasing than reflective framing

Seraph already has the beginning of this in guardian feedback learning. That should become a first-class memory layer instead of an isolated signal.

## Layer 5: Soul / Narrative Projection

Keep `soul.md`, but change its role.

The soul should become:

- a human-readable narrative projection of the structured model
- editable with review controls
- not the only durable source of identity data

In other words:

- structured user model underneath
- soul as curated export and operator-facing reflection layer

## Retrieval Architecture

Seraph should stop doing one generic memory search for every situation.

### Retrieval planner

Before retrieval, classify the request:

- thread continuity
- factual user preference
- project continuity
- historical event lookup
- commitment status
- intervention policy
- general recall

Then route to one or more retrievers.

### Retrieval modes

#### 1. Bounded snapshot retrieval

Use for:

- most normal turns
- low-latency response setup
- prefix-cache friendly context

#### 2. Episodic search

Use:

- FTS5 lexical search over prior sessions and extracted events
- vector retrieval over episodic summaries
- time filters
- project/entity filters
- recency decay

#### 3. Semantic retrieval

Use:

- entity- and category-aware retrieval
- confidence-aware ranking
- contradiction-aware filtering
- reinforcement-weighted ranking

#### 4. Procedural retrieval

Use:

- guardian feedback outcomes
- successful and unsuccessful intervention history
- context-conditioned policy hints

### Ranking formula

Seraph does not need the exact same formula as OpenClaw, but it should combine:

- semantic relevance
- lexical relevance
- recency
- reinforcement strength
- confidence
- source diversity
- entity/project match
- contradiction penalty

MMR should be used to avoid returning five near-duplicates.

## Write / Consolidation Pipeline

### Stage A: Sensory filtering

On each important turn or event boundary, create cheap candidate memory units from:

- user messages
- assistant commitments
- tool outputs
- observer snapshots
- intervention outcomes

Discard low-value noise quickly.

This is the LightMem lesson.

### Stage B: Online short-term organization

Maintain a rolling session-local working set:

- active thread summary
- current project guesses
- named entities mentioned this session
- candidate commitments
- candidate preferences

This layer is not yet durable unless promoted.

### Stage C: Offline long-term consolidation

At session end, near compaction, and on periodic background jobs:

- merge related candidates
- detect contradictions
- create or update semantic records
- write episodic timeline entries
- update procedural memory from intervention outcomes
- regenerate bounded snapshot sources
- refresh the soul projection if needed

### Stage D: Reinforcement and decay

Every time a memory is:

- reused successfully
- reconfirmed by the user
- contradicted
- ignored for a long period

update its strength score.

This prevents immortal low-quality memory.

## Proposed Data Model

Seraph should keep SQLite plus LanceDB, but give them clearer roles.

### SQLite as source of truth

Use SQLite and SQLModel tables for:

- typed memory metadata
- entities
- edges/relationships
- episodic events
- source links
- FTS indexes
- reinforcement and contradiction state

### LanceDB as vector index

Use LanceDB only for:

- vector embeddings
- ANN search
- optional multimodal embeddings later

### Recommended tables

- `memory_entities`
  - people, projects, orgs, routines, locations, channels
- `memory_items`
  - one typed durable memory record
- `memory_item_sources`
  - links from each durable item to sessions, messages, observer events, audit events
- `memory_events`
  - episodic timeline units
- `memory_edges`
  - relations such as `works_on`, `blocked_by`, `collaborates_with`, `prefers`, `supersedes`
- `memory_feedback`
  - reinforcement, contradiction, confirmation, archival actions
- `memory_snapshots`
  - generated bounded prompt snapshots and soul projections

### Recommended `memory_items` fields

- `id`
- `kind`
- `subject_entity_id`
- `project_entity_id`
- `thread_id`
- `canonical_text`
- `summary_text`
- `confidence`
- `importance`
- `reinforcement`
- `contradicted`
- `superseded_by`
- `valid_from`
- `valid_to`
- `first_seen_at`
- `last_seen_at`
- `last_confirmed_at`
- `privacy_level`
- `status`
- `embedding_id`

## Concrete Implementation Plan

## Phase 0: Measurement First

Before major rewrites, define memory evals.

Add:

- recall QA over prior sessions
- commitment continuity evals
- collaborator and project recall evals
- contradiction cleanup evals
- intervention adaptation evals
- latency and token-cost measurements

Suggested location:

- `backend/src/evals/memory/`
- `backend/tests/test_memory_*.py`

Without this, Seraph can add complexity without proving improvement.

## Phase 1: Introduce structured memory metadata

Keep current vector search alive, but add structured tables.

Implementation steps:

1. Add new SQLModel tables in `backend/src/db/models.py`
2. Add migration support for the new tables
3. Create `backend/src/memory/repository.py` for CRUD and query logic
4. Keep `vector_store.py` temporarily as the vector backend
5. Make every new memory insert write both:
   - structured row in SQLite
   - vector row in LanceDB

Exit criteria:

- old flows still work
- new memory can carry provenance, confidence, and typed kind

## Phase 2: Replace coarse categories with typed memory kinds

Expand beyond the current categories.

Implementation steps:

1. Define enums or string constants in a new `backend/src/memory/types.py`
2. Update consolidation output schema
3. Update retrieval filters and guardian-state grouping
4. Map old `fact/preference/pattern/goal/reflection` into richer kinds during migration

Important note:

The current world model already expects richer categories. This phase should align the writer and the consumer.

## Phase 3: Build entity extraction and linking

Seraph needs explicit entities.

Implementation steps:

1. Add `backend/src/memory/entity_linker.py`
2. Extract people, projects, organizations, routines, and channels from:
   - user messages
   - session summaries
   - observer project signals
   - audit/tool outcomes
3. Link memory items to these entities
4. Add conservative merge rules so "OpenAI API project" and "the API project" can resolve to one entity when confidence is high

Exit criteria:

- project continuity and collaborator recall stop relying on plain text matching

## Phase 4: Build hybrid retrieval

This is the OpenClaw lesson applied to Seraph's richer model.

Implementation steps:

1. Add FTS5-backed lexical search for:
   - session messages
   - episodic event summaries
   - semantic memory texts
2. Add a retrieval planner in `backend/src/memory/retrieval_planner.py`
3. Add specialized retrievers:
   - `episodic_retriever.py`
   - `semantic_retriever.py`
   - `procedural_retriever.py`
   - `bounded_snapshot_retriever.py`
4. Add reranking with:
   - recency decay
   - MMR
   - entity/project match boosts
   - contradiction penalty

Exit criteria:

- `build_guardian_state()` uses retrieval plans instead of one generic query

## Phase 5: Rebuild consolidation around multi-stage memory writing

The current `consolidator.py` should evolve into a pipeline.

Recommended split:

- `backend/src/memory/pipeline/capture.py`
- `backend/src/memory/pipeline/extract.py`
- `backend/src/memory/pipeline/link.py`
- `backend/src/memory/pipeline/merge.py`
- `backend/src/memory/pipeline/strengthen.py`
- `backend/src/memory/pipeline/projectors.py`

Implementation behavior:

- capture candidate facts, events, commitments, preferences, routines
- attach provenance
- compare against existing memory
- merge or supersede when appropriate
- update reinforcement and contradiction state
- only then write durable memory

Exit criteria:

- durable memory becomes updatable, not append-only

## Phase 6: Move soul to a projected view

Do not delete `soul.md`. Change its role.

Implementation steps:

1. Add structured identity/profile records to SQLite
2. Generate `soul.md` from those records plus curated narrative sections
3. Preserve manual editing, but add review or reconciliation logic so manual edits become structured updates rather than raw overwrite

Exit criteria:

- soul remains human-readable
- structured memory becomes the canonical substrate

## Phase 7: Add compaction and milestone memory flushes

This is the OpenClaw lesson that Seraph should adopt quickly.

Trigger flushes on:

- near context compression
- workflow completion
- explicit task completion
- session end
- significant observer state transitions

Implementation steps:

1. add flush hooks around conversation compaction and workflow lifecycle
2. run a lightweight durable-write pass
3. write only high-salience items

Exit criteria:

- important commitments are less likely to be lost before session end

## Phase 8: Add procedural memory from intervention outcomes

This is Seraph's biggest chance to be better than the reference systems.

Implementation steps:

1. turn existing guardian feedback signals into explicit procedural memory rows
2. condition them on context:
   - user state
   - interruption mode
   - channel
   - urgency
   - time of day
3. retrieve them in guardian-state synthesis and delivery planning

Exit criteria:

- timing and phrasing decisions improve from actual outcomes, not only hand-written rules

## Phase 9: Add decay, contradiction, and archive policies

Implementation steps:

1. define memory strengths and thresholds
2. lower strength when a memory goes stale or is contradicted
3. archive low-confidence or superseded memory
4. keep archived items searchable for forensic debugging but excluded from default recall

Exit criteria:

- Seraph stops accumulating unbounded stale memory debt

## Recommended File-Level Changes

### Existing files to refactor

- `backend/src/memory/vector_store.py`
  - narrow this into a pure vector backend
- `backend/src/memory/consolidator.py`
  - replace one large prompt path with staged pipeline calls
- `backend/src/memory/soul.py`
  - convert into read/write projection helpers plus reconciliation logic
- `backend/src/guardian/state.py`
  - replace single generic retrieval with retrieval planner and structured bundle assembly
- `backend/src/tools/session_search_tool.py`
  - switch from plain SQL `LIKE` search to FTS5 plus semantic summary path
- `backend/src/agent/session.py`
  - add richer session/event indexing helpers

### New modules to add

- `backend/src/memory/types.py`
- `backend/src/memory/repository.py`
- `backend/src/memory/entity_linker.py`
- `backend/src/memory/retrieval_planner.py`
- `backend/src/memory/retrievers/`
- `backend/src/memory/pipeline/`
- `backend/src/memory/snapshots.py`
- `backend/src/memory/decay.py`
- `backend/src/memory/merge.py`

## Retrieval Context Shape For Agents

Instead of only injecting:

- `Relevant memories:`

Seraph should inject a structured bundle like:

- bounded snapshot
- active commitments
- active collaborators
- project continuity
- recent relevant episodes
- known user preferences
- learned communication guidance
- confidence and contradiction warnings

This should make the agent's prompt less noisy and more legible.

## Safety, Privacy, And Trust

To be world-class, the memory system must also be safer.

Requirements:

- memory sensitivity labels
- prompt-injection scanning for prompt-injected memory surfaces
- secret-pattern scanning before durable writes
- explicit rules for what should never enter long-term memory
- user-facing inspection and deletion tools
- audit trail for memory creation and mutation

Missing today:

- first-class delete/update/forget surface for long-term vector memory
- contradiction state
- privacy classes on memory records

## Evaluation Plan

Seraph should not call the new memory system better until it wins on measured outcomes.

### Core eval buckets

- recall accuracy
  - preferences, collaborators, projects, routines, commitments
- temporal accuracy
  - who/what/when questions over prior sessions
- contradiction handling
  - outdated preference replacement
- guardian adaptation
  - phrasing/timing/channel adjustments after feedback
- efficiency
  - prompt token cost
  - retrieval latency
  - consolidation cost

### Suggested benchmark mix

- external:
  - LoCoMo-style long-conversation memory tasks
- internal:
  - Seraph-native guardian continuity tasks
  - project handoff tasks
  - repeated interrupted-work tasks
  - intervention acceptance and rejection loops

## Non-Goals

This roadmap does not recommend:

- storing every message forever in prompt-visible memory
- replacing all existing Seraph memory with one giant knowledge graph immediately
- relying only on embeddings as the solution
- trying to solve multimodal memory before typed textual memory is solid

## Proposed Build Order

If implementation starts now, the highest-leverage order is:

1. evals
2. structured metadata tables
3. richer memory kinds
4. entity linking
5. hybrid retrieval
6. multi-stage consolidation
7. soul projection
8. compaction flush
9. procedural memory learning
10. decay and contradiction cleanup

This order preserves current functionality while moving toward a much stronger architecture.

## Bottom Line

Hermes shows that bounded curated memory is right.
OpenClaw shows that hybrid retrieval and lifecycle-triggered writes are right.
MemoryBank, MemGPT, LoCoMo, and LightMem show that tiering, decay, episodic structure, and offline consolidation are necessary.

Seraph can beat all of them if it builds:

- bounded prompt memory
- typed episodic and semantic memory
- procedural guardian memory from outcomes
- retrieval planning instead of one generic search
- reinforcement, contradiction, and decay
- evals tied to real guardian behavior

That is the path from "persistent memory" to real guardian intelligence.
