<h1 align="center">Seraph</h1>

<p align="center">
  <strong>Your goals. A guardian that grows with you.</strong><br />
  Building a local-first, self-improving AI guardian.
</p>

<p align="center">
  <a href="https://github.com/seraph-quest/seraph/actions/workflows/test.yml?query=branch%3Adevelop"><img src="https://github.com/seraph-quest/seraph/actions/workflows/test.yml/badge.svg?branch=develop" alt="Tests on develop" /></a>
  <a href="backend/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white" alt="Python 3.12" /></a>
  <a href="frontend/package.json"><img src="https://img.shields.io/badge/React-19-149ECA?logo=react&logoColor=white" alt="React 19" /></a>
  <a href="https://github.com/seraph-quest/seraph/releases/latest"><img src="https://img.shields.io/github/v/release/seraph-quest/seraph?color=8B5CF6" alt="Latest release" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-22C55E" alt="MIT License" /></a>
</p>

<p align="center">
  <a href="https://seraph.quest">Website</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="https://docs.seraph.quest">Documentation</a> ·
  <a href="#the-self-improving-guardian-vision">Vision</a> ·
  <a href="https://github.com/seraph-quest/seraph/discussions">Community</a>
</p>

Seraph brings goals, permitted context, research, tasks, and memory into one
workspace. Give useful work a place to continue: review opportunities in your
Inbox, follow progress on the Work board, and help Seraph learn what helps you.

<div align="center">
  <video src="https://github.com/user-attachments/assets/b4624170-0982-475e-b1ad-709a92a21f24" width="900" controls></video>
</div>

<p align="center">
  <a href="https://github.com/user-attachments/assets/b4624170-0982-475e-b1ad-709a92a21f24"><strong>Watch the workspace demo</strong></a><br />
  <sub>Recorded on an earlier cockpit build. See the <a href="docs/implementation/12-current-app-guide.md">Current App Guide</a> for today's interface.</sub>
</p>

---

## Seraph Today

**Status: Partial — usable workflows, with the broader guardian vision still in development.**

| | What you can do |
| --- | --- |
| 🎯 **Keep goals in view** | Bring goals, tasks, conversations, and evidence together; inspect progress and recover interrupted work. |
| 🔎 **Find relevant signals** | Assess changes from public source watches you select. Review cited opportunities in the Inbox, with attention and spending limits. |
| 🛠️ **Turn evidence into work** | Preview and accept bounded research, browser checks, reports, repository repair, and selected document workflows. |
| 🌱 **Teach it what helps** | Give explicit Helpful / Not helpful feedback, review preference recommendations, and separately adopt or roll back memory changes. |
| 🧩 **Extend your workspace** | Use skills, MCP adapters, reviewed procedures, and capability packages, plus optional consented desktop context and integrations. |

Available workflows depend on permissions, configuration, and host support.
See [Development Status](docs/implementation/STATUS.md) for exact limits and
[v2026.10.7 release notes](docs/implementation/22-release-2026-10-07.md) for the
latest guardian and feedback features.

## Quick Start

**Python 3.12 · [uv](https://docs.astral.sh/uv/) · Node.js 24 · npm**

The core runs on a CPU host. No GPU or local model server is required.

For a fresh development checkout:

```bash
git clone https://github.com/seraph-quest/seraph.git
cd seraph
uv sync --project backend --locked --group dev
npm ci --prefix frontend
cp env.dev.example .env.dev
./manage.sh -e dev local up
./manage.sh -e dev local status
```

Open the **[cockpit](http://127.0.0.1:3001)** or
[API documentation](http://127.0.0.1:8004/docs). The core starts without a provider
key. To enable model work, open **Settings → OpenRouter setup** and configure
your key, models, allowed upstreams, explicit cloud consent, and a finite budget.
Text, vision, and embedding have separate controls.

Use `./manage.sh -e dev local run` for a foreground session, particularly in
managed development shells. Stop with `./manage.sh -e dev local down`.
Preserve your existing `.env.dev` when returning to an installed workspace.

<details>
<summary><strong>Setup, data, and inference</strong></summary>

Local-first means Seraph owns your canonical goals, memory, jobs, artifacts,
and history. It does not mean inference is offline. Ordinary text, vision,
and embedding use governed OpenRouter routes. The optional NEAR HTTPS text
capability is disabled by default, requires separate setup and consent, and
sends provider-readable plaintext.

The managed launcher owns environment loading and process lifecycle.
This quick start is for local development; see the
[Current App Guide](docs/implementation/12-current-app-guide.md) for
authentication, optional adapters, recovery, and production requirements.

</details>

## The Self-Improving Guardian Vision

Seraph should feel like a guardian angel: understand your goals, priorities,
constraints, and progress; connect permitted context with relevant developments;
and prepare or perform useful work before you ask, within the authority you give it.

Imagine a speaking opportunity that fits your talk goal, a paper that unblocks
what you are learning, or a project obstacle caught early with a practical
response ready to review. The ambition is useful initiative across days—with
less setup, repeated explanation, and unnecessary interruption.

**Self-improving means learning under your control.** Explicit feedback and
verified outcomes should improve suggestions, procedures, and capabilities
through reviewed, reversible changes. Today's learning is bounded; generalized
source discovery, open-ended follow-through, and measured improvement across
unfamiliar tasks remain ambitions. Success alone does not authorize learning
or broader permissions.

The [Project Constitution](docs/implementation/00-project-constitution.md)
defines this direction and its accepted boundaries.

<details>
<summary><strong>Try the current guardian loop</strong></summary>

1. Choose a Goal, select relevant public source watches, and enable a finite
   guardian policy with attention and spending limits.
2. Inspect a cited opportunity when a qualifying change arrives. Low-confidence
   or irrelevant assessments stay quiet.
3. Preview and accept a supported browser-check or evidence-report plan in Work.
   Review any required approval, then inspect the result and its sources.
4. Mark the opportunity Helpful or Not helpful. Review any resulting preference
   separately before adopting it into memory; you can reverse it later.

The current loop starts with sources you choose. Relevance alone does not
authorize execution, and delivery does not count as positive feedback.

</details>

## What We're Building Toward

<!-- outcome-use-cases:start -->
- Turn goals into prioritized plans, scheduled work, and evidence-backed progress reviews.
- Monitor consented desktop context and produce searchable summaries that help the operator reflect and recover focus.
- Continuously research operator-selected topics and connect material findings to active goals and decisions.
- Execute bounded software-engineering and knowledge workflows with approvals, artifacts, checkpoints, and audit receipts.
- Continue one trusted conversation across the cockpit, paired voice, and paired messaging surfaces.
<!-- outcome-use-cases:end -->

## Explore and Contribute

| For | Start here |
| --- | --- |
| **Running Seraph** | [Current App Guide](docs/implementation/12-current-app-guide.md) · [Support](SUPPORT.md) |
| **Understanding the project** | [Development Status](docs/implementation/STATUS.md) · [Project Constitution](docs/implementation/00-project-constitution.md) |
| **Building with us** | [Contributing](CONTRIBUTING.md) · [Agent guidelines](AGENTS.md) · [Documentation Contract](docs/implementation/08-docs-contract.md) |
| **Questions and ideas** | [GitHub Discussions](https://github.com/seraph-quest/seraph/discussions) · [Issues](https://github.com/seraph-quest/seraph/issues) |

The current runtime is Python/FastAPI with a React cockpit. The accepted
[all-plugin Cordis architecture](docs/implementation/decisions/026-all-plugin-cordis-architecture.md)
is **Planned** migration work.

Contributions that make the guardian more useful are welcome. Track substantial
work, use a feature or fix branch, validate the behavior, and obtain independent
review before merging. Report vulnerabilities through the [Security policy](SECURITY.md).

---

<p align="center">
  Released under the <a href="LICENSE">MIT License</a>.
</p>
