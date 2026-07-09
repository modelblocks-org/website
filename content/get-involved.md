---
title: Get involved
---

ModelBlocks is an open project and your contributions are welcome.

## Governance

Will be added shortly - stay tuned.

## Add a community module

Open a pull request adding your repository's URL to the `community.toml` catalog:

```toml
modules = [
  ...,
  "https://github.com/your-org/module_your_thing",
]
```

The module's details like name, summary, and interface definition are read automatically from the repository's `.copier-answers.yml`, `README.md`, and `INTERFACE.yaml`.
Community modules are marked as such in the directory.

## Get credited

Each module repo tracks its own contributors with [all-contributors](https://allcontributors.org/).
Add yourself (or others) in your module repo and you'll appear on the project-wide [contributors page](/contributors/), flagged with every module you've contributed to.

## Chat

Join the discussion on our [Zulip chat]({{< param zulip_url >}}) to ask questions, propose modules, and coordinate work.

## Code & docs

- [Source and issues]({{< param github_url >}})
- [Documentation]({{< param docs_url >}})
