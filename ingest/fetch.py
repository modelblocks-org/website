"""GitHub file fetching via the GraphQL API.

There is NO org scanning — modules come from the hand-curated catalog. This
module only resolves a ref to a commit and fetches a module's README.md +
INTERFACE.yaml in one query. Requires a GITHUB_TOKEN env var (the Actions token
is fine for public repos; use a PAT for private ones; GraphQL always needs auth).
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import requests

GITHUB_GRAPHQL = "https://api.github.com/graphql"
HTTP_TIMEOUT = 30


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise RuntimeError("GITHUB_TOKEN environment variable is required")
    return tok


def _graphql(query: str, variables: dict) -> dict:
    resp = requests.post(
        GITHUB_GRAPHQL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"bearer {_token()}"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GitHub GraphQL error: {payload['errors']}")
    return payload["data"]


def parse_repo_url(url: str) -> tuple[str, str]:
    """https://github.com/owner/name(.git) -> (owner, name)."""
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"cannot parse owner/name from {url!r}")
    name = parts[1]
    name = name.removesuffix(".git")
    return parts[0], name


_RELEASE_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    latestRelease { tagName }
  }
}
"""


def latest_release_tag(owner: str, name: str) -> str | None:
    """Return the repo's latest GitHub release tag, or None if it has none.

    GitHub's `latestRelease` already excludes drafts and prereleases and picks
    the most recent published release, so no semver sorting is needed here. The
    returned tag name is UNTRUSTED — the caller must validate it (see
    schemas.validate_ref_tag) before using it as a ref or in a URL.

    NOTE: releases are repo-wide, so subdir (monorepo) modules sharing a repo
    resolve to the same release; pin an explicit `ref` per entry if that matters.
    """
    data = _graphql(_RELEASE_QUERY, {"owner": owner, "name": name})
    repo = data["repository"]
    if repo is None:
        raise RuntimeError(f"repository {owner}/{name} not found")
    release = repo.get("latestRelease")
    if not release:
        return None
    return release.get("tagName") or None


_FILES_QUERY = """
query($owner: String!, $name: String!, $ref: String!, $copier: String!,
      $readme: String!, $iface: String!, $contrib: String!, $contribHead: String!) {
  repository(owner: $owner, name: $name) {
    rev:        object(expression: $ref)        { ... on Commit { oid committedDate } }
    copier:     object(expression: $copier)     { ... on Blob { text isTruncated } }
    readme:     object(expression: $readme)     { ... on Blob { text isTruncated } }
    iface:      object(expression: $iface)      { ... on Blob { text isTruncated } }
    contrib:    object(expression: $contrib)    { ... on Blob { text isTruncated } }
    contribHead: object(expression: $contribHead) { ... on Blob { text isTruncated } }
  }
}
"""


def fetch_module_files(owner: str, name: str, ref: str, subdir: str | None) -> dict:
    """Resolve ref->sha and fetch a module's files in one query.

    .copier-answers.yml (identity/metadata) and INTERFACE.yaml are required;
    README.md and .all-contributorsrc are optional. `.all-contributorsrc` is
    fetched both at `ref` (`contributors_json`) and at the default branch
    (`contributors_json_head`) so the caller can keep credits current for
    unpinned modules whose release predates the file.

    NOTE: blobs are read at `ref` while the sha comes from the same query, so
    there is a tiny TOCTOU window. For strict pinning, resolve the sha first and
    pass it back as `ref`. Caching by sha can also be added here.
    """
    base = f"{subdir.rstrip('/')}/" if subdir else ""
    data = _graphql(
        _FILES_QUERY,
        {
            "owner": owner,
            "name": name,
            "ref": ref,
            "copier": f"{ref}:{base}.copier-answers.yml",
            "readme": f"{ref}:{base}README.md",
            "iface": f"{ref}:{base}INTERFACE.yaml",
            "contrib": f"{ref}:{base}.all-contributorsrc",
            "contribHead": f"HEAD:{base}.all-contributorsrc",
        },
    )
    repo = data["repository"]
    if repo is None:
        raise RuntimeError(f"repository {owner}/{name} not found")
    rev = repo["rev"]
    if not rev:
        raise RuntimeError(f"ref {ref!r} not found in {owner}/{name}")
    copier = repo["copier"]
    if not copier:
        raise RuntimeError(".copier-answers.yml not found")
    if copier.get("isTruncated"):
        raise RuntimeError(".copier-answers.yml too large (truncated by API)")
    iface = repo["iface"]
    if not iface:
        raise RuntimeError("INTERFACE.yaml not found")
    if iface.get("isTruncated"):
        raise RuntimeError("INTERFACE.yaml too large (truncated by API)")
    readme = repo["readme"] or {}
    if readme.get("isTruncated"):
        raise RuntimeError("README.md too large (truncated by API)")
    contrib = repo["contrib"] or {}
    if contrib.get("isTruncated"):
        raise RuntimeError(".all-contributorsrc too large (truncated by API)")
    contrib_head = repo["contribHead"] or {}
    if contrib_head.get("isTruncated"):
        raise RuntimeError(
            ".all-contributorsrc (default branch) too large (truncated by API)"
        )
    return {
        "sha": rev["oid"],
        "updated": rev["committedDate"],
        "copier_yaml": copier["text"],
        "interface_yaml": iface["text"],
        "readme_md": readme.get("text", ""),
        "contributors_json": contrib.get("text", ""),
        "contributors_json_head": contrib_head.get("text", ""),
    }


_REPO_QUERY = """
query($owner: String!, $name: String!, $contrib: String!) {
  repository(owner: $owner, name: $name) {
    description
    contrib: object(expression: $contrib) { ... on Blob { text isTruncated } }
  }
}
"""


def fetch_repo_contributors(
    owner: str, name: str, ref: str, subdir: str | None
) -> dict:
    """Fetch a non-module repo's description + .all-contributorsrc (both may be
    absent). Used for core tool repos, which feed only the contributors view."""
    base = f"{subdir.rstrip('/')}/" if subdir else ""
    data = _graphql(
        _REPO_QUERY,
        {
            "owner": owner,
            "name": name,
            "contrib": f"{ref}:{base}.all-contributorsrc",
        },
    )
    repo = data["repository"]
    if repo is None:
        raise RuntimeError(f"repository {owner}/{name} not found")
    contrib = repo["contrib"] or {}
    if contrib.get("isTruncated"):
        raise RuntimeError(".all-contributorsrc too large (truncated by API)")
    return {
        "description": repo.get("description") or "",
        "contributors_json": contrib.get("text", ""),
    }
