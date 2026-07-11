# Seraph

Seraph is a local-first proactive guardian: an operator-controlled system that
understands your goals, observes permitted context, proposes and executes useful
work, remembers outcomes, and explains what it is doing.

Seraph is not the operator's goal. It adapts its behavior and capabilities to
help with the operator's goals. The canonical product and architecture contract
is the [Project Constitution](docs/implementation/00-project-constitution.md).

## Status

**Partial:** the current `develop` baseline includes a FastAPI backend, React
cockpit and settings UI, goals and memory foundations, scheduler jobs, tools and
workflows, screen observation storage, reports, model routing, approvals, audit
surfaces, and extension adapters.

The accepted target decision and [Epic #736](https://github.com/seraph-quest/seraph/issues/736) move
the authenticated Seraph core to the GPU server for LAN access, makes the Mac a
paired observation/interface edge, unifies local and remote inference routes,
and replaces command-backed external-agent paths with Seraph-owned capabilities.
These target changes are not shipped merely because they are documented.

Use these sources in order:

1. [Project Constitution](docs/implementation/00-project-constitution.md) — what Seraph is and the locked target architecture
2. [Current App Guide](docs/implementation/12-current-app-guide.md) — how to run the current baseline
3. [Development Status](docs/implementation/STATUS.md) — detailed shipped and partial capability truth
4. [Documentation Contract](docs/implementation/08-docs-contract.md) — ownership and status rules
5. [Research Synthesis](docs/research/00-synthesis.md) — evidence and options, not shipped claims

## What Seraph Is For

<!-- outcome-use-cases:start -->
- Turn goals into prioritized plans, scheduled work, and evidence-backed progress reviews.
- Monitor consented desktop context and produce searchable summaries that help the operator reflect and recover focus.
- Continuously research operator-selected topics and connect material findings to active goals and decisions.
- Execute bounded software-engineering and knowledge workflows with approvals, artifacts, checkpoints, and audit receipts.
- Continue one trusted conversation across the cockpit, paired voice, and paired messaging surfaces.
<!-- outcome-use-cases:end -->

Each surface must show what is active, what model route is effective, what is
queued, what requires approval, and what is degraded.

## Architecture

Seraph has four product layers:

| Layer | Owns |
| --- | --- |
| Guardian kernel | Goals, policy, planning, priority, intervention, memory coordination, and audit |
| Capability runtime | Typed execution, durable jobs, artifacts, checkpoints, approvals, and scheduling |
| Model fabric | Inference through local models, OpenRouter, or generic OpenAI-compatible APIs |
| Interfaces and edges | Browser cockpit, API, paired Mac edge, voice, and paired messaging |

Models provide inference; they do not become the agent runtime. Seraph owns its
capabilities and does not depend on Codex CLI, Claude Code, or another coding
agent to operate. See the constitution's five
[architecture decisions](docs/implementation/00-project-constitution.md#locked-decisions).

## Current Development Topology

The current workspace is on GPU host `jupyter` (`192.168.1.26`):

```text
GPU host jupyter
  Seraph frontend/backend + canonical data
    -> private VLM wrapper
    -> private GPU model server
Mac paired edge target (#749)
  screenshot capture -> future authenticated Seraph upload API
```

The Mac must not call internal ports 8000, 8001, or 8004. Its screenshot push
is planned in #749; no screenshot upload API is currently shipped.

Runtime traffic uses private HTTP/service routes. This workspace is already on
`jupyter`, so administration is direct and does not use `ssh jupyter`.

## Quick Start

```bash
cp env.dev.example .env.dev
# Configure a local, OpenRouter, or generic OpenAI-compatible inference route.

./manage.sh -e dev local up
./manage.sh -e dev local status
```

Open `http://127.0.0.1:3001` for the cockpit and
`http://127.0.0.1:8004/docs` for the API.

For a foreground session, especially from a managed development shell:

```bash
./manage.sh -e dev local run
```

Stop with:

```bash
./manage.sh -e dev local down
```

Do not start the backend or frontend directly for normal development; the
managed launcher loads the expected environment and reports owned process state.
See the [Current App Guide](docs/implementation/12-current-app-guide.md) for VLM
health probes, runtime status, Docker mode, and known transitional routes.

## Repository Map

```text
backend/               FastAPI APIs, guardian/agent runtime, tools, memory,
                       scheduler, observer, workflows, and tests
frontend/              React cockpit, settings, chat, and operator state
daemon/                Current macOS observation daemon
docs/implementation/   Constitution, ADRs, shipped truth, and operator contracts
docs/research/         Evidence, alternatives, and dated comparisons
docs/docs/             Historical archive; may contradict the current contract
scripts/               Validation and maintenance tools
```

## Development Contract

Read [AGENTS.md](AGENTS.md) before changing the repository. In particular:

- track non-trivial work in GitHub before completion;
- branch from the required integration base and never commit directly to
  `develop` or `main`;
- keep runtime truth operator-visible and preserve one-GPU serial priority;
- use focused tests and operational receipts appropriate to the change;
- require independent Critic/Contrarian review for PR-sized work; and
- update `docs/implementation/` when shipped behavior or durable contracts change.

## Contributing And Getting Help

Use the [documentation site](https://docs.seraph.quest),
[Contributing guide](CONTRIBUTING.md), [Support guide](SUPPORT.md), and
[Security policy](SECURITY.md). Ask product and usage questions in
[GitHub Discussions](https://github.com/seraph-quest/seraph/discussions); use
[issues](https://github.com/seraph-quest/seraph/issues) for tracked bugs and
work. Epic #736 is the current product-reset program.

The current published release is available from
[GitHub Releases](https://github.com/seraph-quest/seraph/releases/latest), with
[release notes](docs/implementation/21-release-2026-07-04.md). The repository
also has an uploaded social-preview asset used by the docs site. The
[workspace demo](https://github.com/user-attachments/assets/b4624170-0982-475e-b1ad-709a92a21f24)
shows an earlier cockpit build; use the Current App Guide for current UI truth.

## Documentation Classes

- **Shipped/Partial:** verified behavior on `develop`, owned by
  `docs/implementation/`.
- **Target (decision/document state):** accepted architecture intent, owned by
  the constitution and ADRs; it is not a capability lifecycle state.
- **Research:** evidence or options under evaluation in `docs/research/`.
- **Archived:** historical context under `docs/docs/`.

The GitHub Project, issues, and pull requests own live execution state. Docs do
not mirror the queue.

## License

MIT
