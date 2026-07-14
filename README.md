# Modelblocks.org

The website at [www.modelblocks.org](https://www.modelblocks.org/).

- Two manually curated lists (`core.toml`, `community.toml`) name the module repos to show in the directory.
- A Python **ingest** fetches each repo's metadata, README, and interface files, sanitises them, and writes `data/*.json`.
- **Hugo** builds the site from that data.
- GitHub Actions rebuilds and deploys nightly and on every push.

The ingest pipeline was developed with the assistance of Claude Code.

How to use:

```bash
pixi install

# The ingest script needs a GITHUB_TOKEN
export GITHUB_TOKEN="$(gh auth token)"

# Runs ingest, then the local live preview
pixi run serve
```

To add a module to the directory, add its repo URL to `core.toml` or `community.toml`.
