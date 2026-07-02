"""Untrusted bytes -> trusted data.

This module is the SINGLE funnel for untrusted content. Everything an attacker
might control (INTERFACE.yaml, README.md) is parsed and neutralised here, and
nothing downstream (Hugo) ever reasons about trust again. Unit-test this module
against a folder of deliberately malicious fixtures.
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import nh3
import yaml
from markdown_it import MarkdownIt
from pygments import highlight as pygments_highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from pydantic import ValidationError

from .schemas import Contributor, CopierAnswers, Interface, InterfaceStrict

MAX_YAML_BYTES = 64 * 1024
MAX_README_BYTES = 512 * 1024
MAX_CONTRIB_BYTES = 512 * 1024

# The extended description is the README intro: everything before the templated
# "## About" heading. If that heading is missing, fall back to the first N lines.
_ABOUT_RE = re.compile(r"^##\s+about\b", re.IGNORECASE)
_LEADING_H1_RE = re.compile(r"^#\s+.*$")
INTRO_FALLBACK_LINES = 20


def safe_yaml_load(raw: str) -> Any:
    """yaml.safe_load (never yaml.load) + a size cap against alias-expansion DoS."""
    if len(raw.encode("utf-8")) > MAX_YAML_BYTES:
        raise ValueError("INTERFACE.yaml exceeds size limit")
    return yaml.safe_load(raw)


# --- markdown -> safe HTML ---------------------------------------------------
# Highlight at ingest with class-based Pygments output, THEN sanitise. The class
# names come from our highlighter (the author only picks the fence language), so
# allowing `class` on code/span/pre/div is safe: the attacker controls the code
# TEXT, never the emitted class names. This keeps highlighting a generic
# sanitiser would otherwise strip.
_FORMATTER = HtmlFormatter(cssclass="highlight")


def _highlight(code: str, lang: str, _attrs: str) -> str:
    try:
        lexer = get_lexer_by_name(lang) if lang else guess_lexer(code)
    except (ClassNotFound, ValueError):
        return ""  # fall back to markdown-it's escaped code block
    return pygments_highlight(code, lexer, _FORMATTER)


# html=True lets raw HTML through markdown-it — notably the <p><img></p> hero
# blocks READMEs use — so it reaches nh3. nh3 is the real security boundary: it
# strips every tag/attr/URL-scheme not on the allow-lists below and removes
# comments. (Hugo's goldmark "unsafe" still stays OFF — that is a separate path.)
_md = MarkdownIt(
    "commonmark",
    {"html": True, "linkify": True, "highlight": _highlight},
)

_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "blockquote", "pre", "code", "span", "div", "br", "hr",
    "ul", "ol", "li",
    "strong", "em", "del", "sub", "sup", "kbd", "samp", "var",
    "a", "img",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td",
}

_ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "loading", "width", "height"},
    "code": {"class"},
    "span": {"class"},
    "pre": {"class"},
    "div": {"class"},
}


def extract_intro(markdown_text: str) -> str:
    """Return the README intro: text before the '## About' heading.

    Falls back to the first INTRO_FALLBACK_LINES lines when there is no About
    heading. A leading top-level '# title' is dropped (the module name from
    .copier-answers.yml is already the page title, so it would just duplicate).
    """
    lines = markdown_text.splitlines()
    cut = next((i for i, ln in enumerate(lines) if _ABOUT_RE.match(ln.strip())), None)
    intro = lines[:cut] if cut is not None else lines[:INTRO_FALLBACK_LINES]

    # Drop a single leading H1 title (skip any blank lines before it).
    start = next((i for i, ln in enumerate(intro) if ln.strip()), len(intro))
    if start < len(intro) and _LEADING_H1_RE.match(intro[start]):
        intro = intro[start + 1:]

    return "\n".join(intro).strip()


# Rewrite relative <img> sources to absolute, commit-pinned raw URLs so images a
# README references (e.g. ./figures/x.png) actually load on the directory site.
# Done BEFORE nh3 so it only ever sees absolute https URLs (which pass the scheme
# allow-list); absolute URLs are left untouched by urljoin.
_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc=)(["\'])(.*?)\2', re.IGNORECASE | re.DOTALL)


def _absolutize_images(raw_html: str, base_url: str) -> str:
    return _IMG_SRC_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{urljoin(base_url, m.group(3))}{m.group(2)}",
        raw_html,
    )


def render_readme(markdown_text: str, *, image_base_url: str | None = None) -> str:
    """The ONLY path untrusted README markdown takes. Returns safe HTML.

    `image_base_url` is the raw URL of the README's directory; when given,
    relative image sources are resolved against it so they load off-repo.
    """
    if len(markdown_text.encode("utf-8")) > MAX_README_BYTES:
        raise ValueError("README.md exceeds size limit")
    raw_html = _md.render(markdown_text)
    if image_base_url:
        raw_html = _absolutize_images(raw_html, image_base_url)
    return nh3.clean(
        raw_html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},  # drops javascript:/data:
        link_rel="nofollow noopener noreferrer ugc",  # nh3 adds rel for us
        strip_comments=True,
    )


# --- path templates: highlight the wildcards they reference -----------------
_WILDCARD_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


def highlight_path(default: str, wildcards: dict[str, str]) -> str:
    """Escape a path template to safe HTML, wrapping each `{wildcard}` token that
    is defined in INTERFACE.yaml's `wildcards` in a span carrying its description
    (shown on hover). All untrusted text is HTML-escaped; only our own fixed
    markup is added, so the result is safe to render verbatim."""
    out: list[str] = []
    last = 0
    for m in _WILDCARD_RE.finditer(default):
        out.append(html.escape(default[last:m.start()]))
        token = html.escape(m.group(0))
        desc = wildcards.get(m.group(1))
        if desc is not None:
            out.append(f'<span class="wc" title="{html.escape(desc, quote=True)}">{token}</span>')
        else:
            out.append(token)
        last = m.end()
    out.append(html.escape(default[last:]))
    return "".join(out)


# --- contributors (.all-contributorsrc) -------------------------------------
def parse_contributors(rc_json: str, source: str = "") -> list[dict[str, Any]]:
    """Validate a module's .all-contributorsrc into a list of contributor dicts.

    Returns [] when the file is absent/empty/malformed. Individual invalid
    entries are skipped (never fatal) — contributor data is auxiliary and must
    not break a build. Discards are logged to stderr (tagged with `source`) so a
    typo can't silently drop everyone without a trace.
    """
    def warn(msg: str) -> None:
        where = f" for {source}" if source else ""
        print(f"warn: .all-contributorsrc{where}: {msg}", file=sys.stderr)

    if not rc_json or not rc_json.strip():
        return []  # absent/empty is normal — no warning
    if len(rc_json.encode("utf-8")) > MAX_CONTRIB_BYTES:
        warn("exceeds size limit; no contributors parsed")
        return []
    try:
        data = json.loads(rc_json)
    except json.JSONDecodeError as exc:
        warn(f"invalid JSON ({exc}); no contributors parsed")
        return []
    sort_alphabetically = (
        data.get("contributorsSortAlphabetically") is True
        if isinstance(data, dict) else False
    )
    raw = data.get("contributors") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        warn("no 'contributors' list found; no contributors parsed")
        return []

    out: list[dict[str, Any]] = []
    skipped = 0
    for entry in raw[:2000]:
        try:
            c = Contributor.model_validate(entry)
        except ValidationError:
            skipped += 1
            continue  # skip a bad entry, keep the rest
        out.append({
            "login": c.login,
            "name": c.name or c.login,
            "avatar_url": str(c.avatar_url) if c.avatar_url else None,
            "profile": str(c.profile) if c.profile else None,
            "contributions": c.contributions,
        })
    if skipped:
        warn(f"skipped {skipped} invalid contributor "
             f"{'entry' if skipped == 1 else 'entries'}")
    if sort_alphabetically:
        out.sort(
            key=lambda c: (
                (c["name"] or "").casefold(),
                c["name"] or "",
                c["login"].lower(),
            )
        )
    return out


def aggregate_contributors(
    module_records: dict[str, dict],
    tool_sources: list[dict] | None = None,
) -> list[dict[str, Any]]:
    """Merge contributors from modules AND core tool repos into one de-duplicated,
    project-wide list.

    De-dupes by GitHub login (case-insensitive); unions contribution types across
    every source. Records which **modules** (by id, internal links) and which
    **tools** (by {name, url}, external links) each person is listed under. Sorted
    by total source count (desc) then login.
    """
    merged: dict[str, dict[str, Any]] = {}

    def slot(c: dict) -> dict:
        key = c["login"].lower()
        agg = merged.get(key)
        if agg is None:
            agg = merged[key] = {
                "login": c["login"], "name": c["name"],
                "avatar_url": c["avatar_url"], "profile": c["profile"],
                "contributions": set(), "modules": set(), "tools": {},
            }
        agg["contributions"].update(c["contributions"])
        agg["avatar_url"] = agg["avatar_url"] or c["avatar_url"]
        agg["profile"] = agg["profile"] or c["profile"]
        return agg

    for module_id, rec in module_records.items():
        for c in rec.get("contributors", []):
            slot(c)["modules"].add(module_id)

    for tool in tool_sources or []:
        for c in tool.get("contributors", []):
            slot(c)["tools"][tool["url"]] = tool["name"]  # dedupe by url

    result = []
    for v in merged.values():
        tools = sorted(({"name": n, "url": u} for u, n in v["tools"].items()),
                       key=lambda t: t["name"])
        result.append({
            **v,
            "contributions": sorted(v["contributions"]),
            "modules": sorted(v["modules"]),
            "tools": tools,
        })
    result.sort(key=lambda c: (-(len(c["modules"]) + len(c["tools"])), c["login"].lower()))
    return result


# --- per-module record + dataset emit ---------------------------------------
def process_module(
    *,
    copier_yaml: str,
    interface_yaml: str,
    readme_md: str,
    contributors_json: str = "",
    tier: Literal["core", "community"],
    repo: str,
    owner: str,
    name: str,
    subdir: str | None,
    sha: str,
    updated: str,
    version: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    """Build one trusted record. Raises on invalid input; caller sets policy.

    Identity + display metadata come from .copier-answers.yml; the extended
    description from the README intro; the I/O structure from INTERFACE.yaml.
    """
    copier = CopierAnswers.model_validate(safe_yaml_load(copier_yaml) or {})

    model_cls = InterfaceStrict if tier == "core" else Interface
    iface = model_cls.model_validate(safe_yaml_load(interface_yaml) or {})

    dump = iface.model_dump()
    pathvars, wildcards = dump["pathvars"], dump["wildcards"]
    # Pre-render each path template with its wildcards highlighted + described.
    for group in pathvars.values():
        for var in group.values():
            var["default_html"] = highlight_path(var["default"], wildcards)

    # Raw URL of the README's directory, pinned to the commit, for resolving
    # relative image sources.
    image_base = f"https://raw.githubusercontent.com/{owner}/{name}/{sha}/"
    if subdir:
        image_base += subdir.strip("/") + "/"

    # Link to the release page, built from the already-validated tag (we
    # construct it rather than trusting an API-returned URL string).
    version_url = f"{repo}/releases/tag/{version}" if version else None

    # Major of the interface-convention version (validated to v?\d+... upstream).
    cv = iface.convention_version
    convention_major = cv.lstrip("vV").split(".")[0] if cv else None

    return {
        "id": copier.module_short_name,
        "name": copier.module_long_name,
        "summary": copier.module_description,
        "license": copier.license,
        "authors": [copier.author] if copier.author else [],
        "description_html": render_readme(extract_intro(readme_md), image_base_url=image_base),
        "pathvars": pathvars,
        "wildcards": wildcards,
        "convention_version": cv,
        "convention_major": convention_major,
        "contributors": parse_contributors(contributors_json, source=repo),
        "tier": tier,
        "repo": repo,
        "sha": sha,
        "updated": updated,
        "version": version,
        "version_url": version_url,
        "ref": ref,
    }


def write_dataset(records: dict[str, dict], data_dir: Path) -> None:
    """Write data/modules.json — the file the Hugo content adapter consumes."""
    out = data_dir / "modules.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_contributors(contributors: list[dict], data_dir: Path) -> None:
    """Write data/contributors.json — the project-wide, de-duplicated list."""
    out = data_dir / "contributors.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(contributors, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_highlight_css(path: Path) -> None:
    """Emit the Pygments stylesheet once; the base layout links it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_FORMATTER.get_style_defs(".highlight"), encoding="utf-8")
