"""Orchestrator: python -m ingest

  central catalog (core.toml + community.toml)
      -> fetch README + INTERFACE at a pinned commit
      -> validate + sanitise (render.process_module)
      -> data/modules.json + data/contributors.json
         (+ static/css/highlight.css)

Trust asymmetry: an invalid CORE module fails the build (governance); an invalid
COMMUNITY module is skipped and reported. Core tool repos feed only the
contributors view, so a fetch failure there is a warning, never fatal.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import catalog, fetch, render, schemas

ROOT = Path(__file__).resolve().parents[1]
CATALOG_FILES = {
    "core": ROOT / "core.toml",
    "community": ROOT / "community.toml",
}
DATA_DIR = ROOT / "data"
HIGHLIGHT_CSS = ROOT / "static" / "css" / "highlight.css"


def main() -> int:
    cat = catalog.read_catalog(CATALOG_FILES)

    records: dict[str, dict] = {}
    skipped: list[tuple[str, str]] = []

    for entry in cat["modules"]:
        # The id isn't known until .copier-answers.yml is fetched, so identify
        # failures by repo here.
        who = entry["repo"]
        try:
            # Resolve which ref to read the module's files at: an explicit pin in
            # the catalog wins; otherwise auto-resolve to the latest GitHub
            # release; with no usable release, track `main`.
            explicit_ref = entry["ref"]  # str (pinned) or None (auto)
            version = None
            if explicit_ref is not None:
                ref = explicit_ref
            else:
                tag = fetch.latest_release_tag(entry["owner"], entry["name"])
                tag = schemas.validate_ref_tag(tag) if tag else None
                if tag is not None:
                    ref = version = tag
                else:
                    ref = "main"
            files = fetch.fetch_module_files(
                entry["owner"], entry["name"], ref, entry.get("subdir")
            )
            # Contributor credits are "current" info: for an UNPINNED entry read
            # them from the default branch (so credits added after a release still
            # show); a pinned entry reads everything, credits included, at its ref.
            contributors_json = (
                files.get("contributors_json", "") if explicit_ref is not None
                else files.get("contributors_json_head", "")
            )
            record = render.process_module(
                copier_yaml=files["copier_yaml"],
                interface_yaml=files["interface_yaml"],
                readme_md=files["readme_md"],
                contributors_json=contributors_json,
                tier=entry["tier"],
                repo=entry["repo"],
                owner=entry["owner"],
                name=entry["name"],
                subdir=entry.get("subdir"),
                sha=files["sha"],
                updated=files["updated"],
                version=version,
                ref=ref,
                work_in_progress=entry.get("work_in_progress", False),
            )
            if record["id"] in records:
                raise ValueError(f"duplicate module id {record['id']!r} (also from another entry)")
            records[record["id"]] = record
        except Exception as exc:  # noqa: BLE001 - triage by tier
            if entry["tier"] == "core":
                print(f"FATAL: core module {who}: {exc}", file=sys.stderr)
                raise
            skipped.append((who, str(exc)))
            print(f"skip community {who}: {exc}", file=sys.stderr)

    # Core tool repos: not modules, but their contributors join the aggregate.
    # Auxiliary, so a fetch failure is a warning, never fatal.
    tool_sources: list[dict] = []
    for entry in cat["tools"]:
        try:
            # Tool repos feed only the contributors view; they get no release
            # resolution, so read from the pinned ref or default to `main`.
            data = fetch.fetch_repo_contributors(
                entry["owner"], entry["name"], entry["ref"] or "main", entry.get("subdir")
            )
            tool_sources.append({
                "name": entry["name"],
                "url": entry["repo"],
                "description": data["description"][:300],
                "contributors": render.parse_contributors(data["contributors_json"], source=entry["repo"]),
            })
        except Exception as exc:  # noqa: BLE001 - auxiliary, never fatal
            print(f"skip tool repo {entry['repo']}: {exc}", file=sys.stderr)

    contributors = render.aggregate_contributors(records, tool_sources)
    render.write_dataset(records, DATA_DIR)
    render.write_contributors(contributors, DATA_DIR)
    render.write_highlight_css(HIGHLIGHT_CSS)
    print(
        f"wrote {len(records)} modules, "
        f"{len(contributors)} contributors; skipped {len(skipped)} community entries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
