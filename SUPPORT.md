# Support

Use this file to route questions to the right place before opening an issue or pull request.

## Setup and Usage

Start with:

- [`README.md`](README.md)
- [`docs/implementation/STATUS.md`](docs/implementation/STATUS.md)
- [`docs/implementation/00-master-roadmap.md`](docs/implementation/00-master-roadmap.md)

For local development:

```bash
cp env.dev.example .env.dev
./manage.sh -e dev local up
```

## Where to Ask What

- Setup problem or runtime regression: open a bug report
- Product, usage, or roadmap question: use [GitHub Discussions](https://github.com/seraph-quest/seraph/discussions)
- Security-sensitive issue: follow [`SECURITY.md`](SECURITY.md)
- Contribution/process question: read [`CONTRIBUTING.md`](CONTRIBUTING.md)

## Good Context To Include

When asking for help, include:

- what you expected to happen
- what actually happened
- the branch or commit you are on
- relevant logs, screenshots, or reproduction steps
- whether you are using the local runtime, Docker runtime, or macOS daemon path
