# Docs

Seraph now uses a three-part docs layout:

- `docs/implementation/` is the canonical shipped-status surface
- `docs/research/` is the canonical design and product-thesis surface
- `docs/docs/` is a frozen archive for historical material that is no longer part of the active product contract

The trees are meant to mirror each other:

- research owns target shape, benchmark logic, and superiority logic
- implementation owns shipped truth, benchmark-status translation, superiority delivery, and the live PR queue
- `docs/implementation/01-07` are workstreams; `08-10` are cross-cutting mirror docs

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
yarn
```

## Local Development

```bash
yarn start
```

This command starts a local development server and opens up a browser window. Most changes are reflected live without having to restart the server.

## Build

```bash
yarn build
```

This command generates static content into the `build` directory and can be served using any static contents hosting service.

## Deployment

Using SSH:

```bash
USE_SSH=true yarn deploy
```

Not using SSH:

```bash
GIT_USER=<Your GitHub username> yarn deploy
```

If you are using GitHub pages for hosting, this command is a convenient way to build the website and push to the `gh-pages` branch.
