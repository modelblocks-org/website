"""Load the central module catalog (core.toml + community.toml).

Two hand-curated lists: first-party modules in core.toml, third-party ones in
community.toml. There is no org scanning — a module appears on the site only when
its entry is added to one of these files, via reviewed PR. Which file an entry
lives in IS its tier; tier drives validation strictness and the trust policy in
the orchestrator. Keeping the tiers in separate files lets them carry different
review rules (e.g. stricter CODEOWNERS on core.toml).

core.toml may also carry a `tools` list: first-party repos that are NOT modules
(no INTERFACE.yaml) but whose contributors count toward the project-wide
contributors page. They are not shown in the module directory grid.

An entry only points at a repo (+ optional ref/subdir): a module's id and
metadata come from the .copier-answers.yml fetched from that repo. The catalog
files are TRUSTED (reviewed), but we still shape-validate each entry with
CatalogEntry so a typo can't break the build or smuggle a bad URL into fetch.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

from .fetch import parse_repo_url
from .schemas import CatalogEntry


def _entry_fields(item: object, path: Path) -> dict:
    """A catalog entry is a bare repo URL string, or an inline table for
    ref/subdir."""
    if isinstance(item, str):
        return {"repo": item}
    if isinstance(item, dict):
        if "tier" in item:
            raise ValueError(
                f"{path.name}: entries must not set 'tier' — it is implied by the file"
            )
        return dict(item)
    raise ValueError(
        f"{path.name}: each entry must be a repo URL string or an inline table"
    )


def read_catalog(catalog_files: dict[str, Path]) -> dict[str, list[dict]]:
    """Load every catalog file into `{"modules": [...], "tools": [...]}`.

    `catalog_files` maps tier -> path. A missing file is allowed. Repos must be
    unique across ALL lists (a community entry can't shadow a core one, and a
    repo can't be both a module and a tool).
    """
    modules: list[dict] = []
    tools: list[dict] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    def add(item: object, tier: str, path: Path, dest: list[dict]) -> None:
        entry = CatalogEntry.model_validate({**_entry_fields(item, path), "tier": tier})
        key = (str(entry.repo), entry.ref, entry.subdir)
        if key in seen:
            raise ValueError(f"duplicate catalog entry: {key}")
        seen.add(key)
        owner, name = parse_repo_url(str(entry.repo))
        dest.append({
            "repo": str(entry.repo),
            "ref": entry.ref,
            "subdir": entry.subdir,
            "tier": entry.tier,
            "owner": owner,
            "name": name,
            "work_in_progress": entry.work_in_progress,
        })

    for tier, path in catalog_files.items():
        if not path.exists():
            continue
        with path.open("rb") as fh:
            data = tomllib.load(fh)

        raw = data.get("modules")
        if raw is None:
            if "module" in data:
                raise ValueError(f"{path.name}: use a `modules = [...]` array, not [[module]] tables")
            raw = []
        if not isinstance(raw, list):
            raise ValueError(f"{path.name} must define a `modules` array")
        for item in raw:
            add(item, tier, path, modules)

        # `tools` (non-module core repos) are only honoured in the core file.
        raw_tools = data.get("tools", [])
        if not isinstance(raw_tools, list):
            raise ValueError(f"{path.name} `tools` must be an array")
        if raw_tools and tier != "core":
            raise ValueError(f"{path.name}: `tools` is only allowed in the core catalog")
        for item in raw_tools:
            add(item, tier, path, tools)

    return {"modules": modules, "tools": tools}
