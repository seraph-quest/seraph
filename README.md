# Seraph

**A local-first AI guardian. Built to help you make progress—and become more useful over time.**

[Website](https://seraph.quest) · [Documentation](https://docs.seraph.quest) ·
[Quick start](#quick-start) · [Latest release](https://github.com/seraph-quest/seraph/releases/latest) ·
[Community](https://github.com/seraph-quest/seraph/discussions)

Seraph brings your goals, permitted context, research, tasks, and memory into
one operator-controlled workspace. Its direction is a **self-improving guardian**:
an assistant that understands what matters to you, finds useful work, follows
through, and learns from the outcomes you review.

## The Vision: A Guardian That Grows With You

Seraph should feel like a guardian angel. It should understand your goals,
priorities, constraints, and progress; observe the context you permit; and
connect that context with relevant developments, opportunities, and obstacles.
It should research what could help and prepare or perform useful work before
you have to ask, within the authority you have given it.

The ambition is sustained initiative across days, with less setup and repeated
explanation. Imagine Seraph finding a speaking opportunity that fits your talk
goal, surfacing a paper that unblocks something you are learning, or noticing a
project obstacle and preparing a practical response. It should know when to
stay quiet, when to ask, and when it has enough permission to help.

**Self-improving means learning under your control.** Feedback and verified
outcomes should help Seraph refine its suggestions, procedures, and capabilities.
Changes should be inspectable, reviewed, and reversible; successful execution
alone does not authorize learning or broader permissions.

This is the product vision. Today's implementation provides bounded pieces of
that experience; general source discovery, open-ended follow-through, and
measured improvement across unfamiliar tasks are not established capabilities.
The [Project Constitution](docs/implementation/00-project-constitution.md) and
its [architecture decisions](docs/implementation/00-project-constitution.md#locked-decisions)
define the accepted direction and boundaries.

## What You Can Use Today

**Status: Partial.** Seraph has a working guardian foundation and a growing set
of reviewed workflows. The [v2026.10.7 release](docs/implementation/22-release-2026-10-07.md)
adds goal-linked opportunities, reviewed follow-through, explicit feedback
preferences, and an optional NEAR text capability.

| Capability | Current experience |
| --- | --- |
| **Goals and continuity** | Keep goals, tasks, conversation history, and evidence in a canonical workspace. Inspect progress and recover interrupted work without silently renewing expired permissions. |
| **Proactive opportunities** | Assess changes from explicitly selected public source watches against a finite Goal policy. Relevant, cited suggestions appear in the Inbox; low-confidence or irrelevant assessments stay quiet. |
| **Research and useful work** | Preview and accept bounded research, browser checks, evidence reports, repository repair, and selected document workflows through the Work board. Available execution profiles depend on the host and configuration. |
| **Reviewed learning** | Give explicit Helpful / Not helpful feedback. Review a bounded preference recommendation, adopt it separately into memory, and roll it back. These mechanics do not yet establish measured quality improvement. |
| **Tools and extensions** | Extend the runtime with skills, MCP adapters, reviewed procedures, and capability packages. Each retains its own availability, permission, and execution limits. |
| **Context and control** | Use consented desktop context and selected integrations, with visible approvals, artifacts, activity, costs, and recovery state. Optional edges and platform-specific execution have separate readiness checks. |

For exact supported profiles and remaining limits, see
[Development Status](docs/implementation/STATUS.md) and the
[Current App Guide](docs/implementation/12-current-app-guide.md). Local and
intercepted checks establish implementation behavior; they do not establish
live provider quality, broad autonomous usefulness, or superiority over other agents.

### A First Guardian Journey

1. Choose a Goal and explicitly select the public source watches relevant to it.
2. Enable a finite guardian policy with attention and spending limits.
3. When a qualifying source change arrives, inspect its cited opportunity in
   the Inbox—or let Seraph stay quiet when the evidence does not justify attention.
4. Preview and accept a supported browser-check or evidence-report plan in Work.
   Review any required approval, then inspect the result and its sources.
5. Mark the opportunity Helpful or Not helpful. Any resulting memory preference
   requires separate review and adoption; you can reverse it later.

The current loop starts with sources you choose. It does not discover arbitrary
new sources, execute a proposal merely because it is relevant, or treat delivery
as positive feedback.

## What Seraph Is For

These outcomes guide the project; the current scope is described above.

<!-- outcome-use-cases:start -->
- Turn goals into prioritized plans, scheduled work, and evidence-backed progress reviews.
- Monitor consented desktop context and produce searchable summaries that help the operator reflect and recover focus.
- Continuously research operator-selected topics and connect material findings to active goals and decisions.
- Execute bounded software-engineering and knowledge workflows with approvals, artifacts, checkpoints, and audit receipts.
- Continue one trusted conversation across the cockpit, paired voice, and paired messaging surfaces.
<!-- outcome-use-cases:end -->

## Quick Start

For a fresh development checkout, use Python 3.12, [uv](https://docs.astral.sh/uv/),
Node.js 24, and npm. The core runs on a CPU host; no GPU, local model
server, or VLM wrapper is required.

```bash
git clone https://github.com/seraph-quest/seraph.git
cd seraph
uv sync --project backend
npm ci --prefix frontend
cp env.dev.example .env.dev
./manage.sh -e dev local up
./manage.sh -e dev local status
```

Open the [cockpit](http://127.0.0.1:3001) or
[API documentation](http://127.0.0.1:8004/docs). In Settings → **OpenRouter setup**,
configure the server-side key, selected models and allowed upstreams, explicit
cloud consent, and a finite budget. Text, vision, and embedding have separate
purpose controls. Missing configuration blocks the dependent model work while
core readiness remains visible.

For a foreground session, particularly in a managed development shell, use
`./manage.sh -e dev local run` instead of `local up`. Stop the stack with
`./manage.sh -e dev local down`. Use the managed launcher so environment loading
and process ownership stay consistent.

This is a local development setup. See the
[Current App Guide](docs/implementation/12-current-app-guide.md) for configuration,
authentication, optional adapters, troubleshooting, and production deployment requirements.

## Local State, Governed Inference

**Local-first describes ownership of your canonical state, not offline inference.**
Seraph keeps goals, memory, jobs, artifacts, approvals, and audit history in its
own workspace. Ordinary text, vision, and embedding inference uses OpenRouter
with explicit consent, routing, budgets, and a shared serial admission lane.
The optional NEAR HTTPS text capability is disabled by default and requires
separate setup and consent; the provider receives the question in plaintext.

| Layer | Responsibility |
| --- | --- |
| Guardian kernel | Goals, planning, priorities, intervention, and memory coordination |
| Capability runtime | Typed work, durable jobs, approvals, artifacts, checkpoints, and recovery |
| Model fabric | Governed inference routes, capability checks, and cost accounting |
| Interfaces and edges | Browser cockpit, API, and consented local or paired context and communication |

Models supply inference; Seraph owns the runtime and authority. A provider key
does not grant data-sharing or execution permission. Effective routes, queued
work, approvals, and degraded states must remain visible to the operator.

## Find Your Way Around

| Start here | What you will find |
| --- | --- |
| [Current App Guide](docs/implementation/12-current-app-guide.md) | Setup, supported operator journeys, and recovery |
| [Development Status](docs/implementation/STATUS.md) | Shipped and Partial capabilities with their limits |
| [Project Constitution](docs/implementation/00-project-constitution.md) | Product direction, architecture, and accepted decisions |
| [Documentation Contract](docs/implementation/08-docs-contract.md) | How implementation, target, research, and historical material differ |
| [Research](docs/research/00-synthesis.md) | Dated evidence and alternatives |

```text
backend/               APIs, guardian runtime, memory, tools, scheduler, and tests
frontend/              React cockpit, chat, settings, and operator controls
daemon/                macOS observation daemon
docs/implementation/   Constitution, decisions, current behavior, and guides
docs/research/         Evidence and alternatives
docs/docs/             Historical archive
scripts/               Validation and maintenance tools
```

## Community and Contributions

Bring questions and ideas to [GitHub Discussions](https://github.com/seraph-quest/seraph/discussions),
report bugs in [Issues](https://github.com/seraph-quest/seraph/issues), and see the
[Support guide](SUPPORT.md) for help. Follow the [Security policy](SECURITY.md)
when reporting a vulnerability.

To contribute, read [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).
Track substantial work, use a feature or fix branch, validate the relevant
behavior, and obtain independent review before merging. GitHub issues, PRs,
and the Project own current work; implementation docs describe the product.

## License

[MIT](LICENSE)
