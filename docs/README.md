# Seraph Documentation

The documentation has four distinct owners:

| Surface | Authority |
| --- | --- |
| `implementation/` | Constitution, ADRs, shipped/partial truth, and durable operator contracts |
| `research/` | Evidence, alternatives, and target research; never shipping proof |
| `docs/` (`/legacy`) | Archived history that may contradict the current contract |
| GitHub Project/issues/PRs | Live execution, review, and integration state |

Start here:

1. [Project Constitution](implementation/00-project-constitution.md)
2. [Current App Guide](implementation/12-current-app-guide.md)
3. [Development Status](implementation/STATUS.md)
4. [Documentation Contract](implementation/08-docs-contract.md)
5. [Research Synthesis](research/00-synthesis.md)

Public entry points include the [published docs](https://docs.seraph.quest),
[Contributing guide](../CONTRIBUTING.md), [Support guide](../SUPPORT.md), and
[Security policy](../SECURITY.md). Ask product and usage questions in
[GitHub Discussions](https://github.com/seraph-quest/seraph/discussions); use
[tracked issues](https://github.com/seraph-quest/seraph/issues) for bugs and
planned work, then follow the review contract in [`AGENTS.md`](../AGENTS.md).

The public site serves implementation docs at `/`, research at `/research`, and
the historical archive at `/legacy`. The active entry points use the status
capability vocabulary **Shipped**, **Partial**, **Experimental**, **Planned**,
**Deprecated**, and **Excluded**. Target, Research, Archived, and Blocked are
document/decision states. An open branch or deterministic fixture does not make
a capability Shipped.

## Validate

```bash
python3 scripts/check_docs_contract.py
python3 scripts/check_strategy_claims.py
cd docs && npm run typecheck && npm run build
```

The contract check validates required owners, ADRs, navigation, status
vocabulary, links between canonical entry points, and forbidden contradictions
in those entry points. Docusaurus treats broken links as build failures.

## Local Site

```bash
cd docs
npm ci
npm run start
```

## Publication

The public site is published through `.github/workflows/deploy-docs.yml` when
documentation on `main` is deployed to GitHub Pages. Normal changes land through
review into `develop`; direct `docusaurus deploy` and the old `gh-pages` branch
flow are unsupported. A docs-only publication outside normal promotion uses the
`Deploy Docs` workflow dispatch.

Generated `build/`, `.docusaurus/`, and `node_modules/` content is not an active
documentation source and should not be edited.
