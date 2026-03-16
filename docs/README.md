# Docs

Seraph now uses a three-part docs layout:

- `docs/implementation/` is the canonical shipped-status surface
- `docs/research/` is the canonical design and product-thesis surface
- `docs/docs/` is retained as legacy/reference material

The Docusaurus site now serves:

- `/` from `docs/implementation/`
- `/research` from `docs/research/`
- `/legacy` from `docs/docs/`

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
