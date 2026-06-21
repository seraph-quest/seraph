# Docs

Seraph now uses a three-part docs layout:

- `docs/research/` is the canonical target-shape, evidence, and product-thesis surface
- `docs/implementation/` is the canonical shipped-status and strategic implementation surface
- `docs/docs/` is a frozen archive for historical material that is no longer part of the active product contract

Current truth lives in `docs/research/` plus `docs/implementation/`.
History lives in `docs/docs/`.

GitHub owns execution state:

- the GitHub Project is the execution layer
- issues and PRs are the active work-tracking layer
- PR bodies carry branch-specific scope, validation, and review receipts
- docs should not mirror live `Queue`, `Status`, `Code Review`, or `PR` state

The trees are meant to mirror each other without competing:

- research owns target shape, benchmark logic, and superiority logic
- implementation owns shipped truth, benchmark-status translation, superiority delivery, and strategic implementation translation
- `docs/implementation/01-07` are workstreams; `08-10` are cross-cutting mirror docs
- `docs/implementation/00-master-roadmap.md` is the strategic implementation program and completed-program record, not a live kanban

Start with:

- `docs/implementation/00-master-roadmap.md`
- `docs/implementation/STATUS.md`
- `docs/implementation/08-docs-contract.md`
- `docs/implementation/09-benchmark-status.md`
- `docs/implementation/10-superiority-delivery.md`
- `docs/research/00-synthesis.md`

The Docusaurus site now serves:

- `/` from `docs/implementation/`
- `/research` from `docs/research/`
- `/legacy` from `docs/docs/` as the historical archive route

This website is built using [Docusaurus](https://docusaurus.io/), a modern static website generator.

## Installation

```bash
npm ci
```

## Local Development

```bash
npm run start
```

This command starts a local development server and opens up a browser window. Most changes are reflected live without having to restart the server.

## Build

```bash
npm run build
```

This command generates static content into the `build` directory and can be served using any static contents hosting service.

## Deployment

The public docs site deploys through GitHub Pages Actions, not the legacy
`gh-pages` branch flow.

Normal docs changes still land through a PR to `develop`. The public site is
published when `develop` is explicitly promoted to `main` with `docs/**`
changes. The workflow is `.github/workflows/deploy-docs.yml` and runs:

```bash
cd docs
npm ci
npm run build
```

The generated artifact in `docs/build` is uploaded with
`actions/upload-pages-artifact` and deployed with `actions/deploy-pages` to the
`github-pages` environment at `https://docs.seraph.quest`.

Do not use direct `docusaurus deploy` or `gh-pages` branch publication for this
repo. If a docs-only publication is needed outside the normal `develop` to
`main` promotion, use the workflow dispatch on `Deploy Docs`.
