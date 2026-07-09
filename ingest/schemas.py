"""Pydantic models — the validation half of the trust boundary."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    field_validator,
)

# A module id is a URL path segment + dict key, so it stays lowercase and
# URL-safe. This mirrors the copier template's `module_code_name` validator
# (`^[a-z][a-z0-9_]*$`); the `module_` prefix is only a naming convention for
# core modules, not enforced here.
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


# ---------------------------------------------------------------------------
# UNTRUSTED: a person entry in .copier-answers.yml (author / maintainer)
# The v1.0 convention lists authors and maintainers as structured people. We
# only surface the display name; the email is validated upstream by copier and
# is not published, so it is merely length-capped here.
# ---------------------------------------------------------------------------
class Person(BaseModel):
    model_config = ConfigDict(extra="ignore")
    given_name: str | None = Field(default=None, max_length=100)
    family_name: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=200)

    @property
    def name(self) -> str | None:
        parts = [p for p in (self.given_name, self.family_name) if p]
        return " ".join(parts) or None


# ---------------------------------------------------------------------------
# UNTRUSTED: .copier-answers.yml
# The module's identity + display metadata. Written by Copier from the module
# template; we read only the fields we surface and ignore the rest (_commit,
# _src_path, github_org, ...). `module_code_name` becomes the module id and is
# used as a URL path segment + dict key, so it is strictly validated. Only the
# v1.0 convention is supported; a module must publish a v1.0-format release (or
# be pinned to a v1.0 ref) to appear in the directory.
# ---------------------------------------------------------------------------
class CopierAnswers(BaseModel):
    model_config = ConfigDict(extra="ignore")
    module_code_name: str = Field(max_length=64)  # -> id
    module_human_name: str = Field(max_length=200)  # -> display name
    module_description: str = Field(max_length=600)  # -> summary
    license: str | None = Field(default=None, max_length=100)
    authors: list[Person] = Field(default_factory=list, max_length=100)
    maintainers: list[Person] = Field(default_factory=list, max_length=100)

    @field_validator("module_code_name")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(
                "module_code_name must be lowercase snake_case (e.g. module_foo)"
            )
        return v

    @property
    def author_names(self) -> list[str]:
        return [p.name for p in self.authors if p.name]

    @property
    def maintainer_names(self) -> list[str]:
        return [p.name for p in self.maintainers if p.name]


# ---------------------------------------------------------------------------
# UNTRUSTED: INTERFACE.yaml
# An input/output documentation structure. `pathvars` groups (e.g.
# snakemake_defaults, user_resources, results) map a variable name to a path
# template + description; `wildcards` maps a wildcard name to a description.
# Keep the security parts: length caps, item caps, and the strip-vs-forbid split.
# ---------------------------------------------------------------------------
_ShortStr = Annotated[str, StringConstraints(max_length=500)]
_LongStr = Annotated[str, StringConstraints(max_length=4000)]
_CONVENTION_RE = re.compile(r"^v?\d+(?:\.\d+)*$")  # e.g. v1.0.0 / 1.2 / v2


class PathVar(BaseModel):
    model_config = ConfigDict(extra="ignore")
    default: _ShortStr
    description: _LongStr | None = None


class Interface(BaseModel):
    """Lenient: unknown keys are stripped. Used for the community tier."""

    model_config = ConfigDict(extra="ignore")
    convention_version: str | None = Field(default=None, max_length=40)
    pathvars: dict[str, dict[str, PathVar]] = Field(default_factory=dict, max_length=50)
    wildcards: dict[str, _LongStr] = Field(default_factory=dict, max_length=100)

    @field_validator("convention_version")
    @classmethod
    def _check_convention(cls, v: str | None) -> str | None:
        if v is not None and not _CONVENTION_RE.match(v):
            raise ValueError("convention_version must look like v1.0.0")
        return v


class InterfaceStrict(Interface):
    """Strict: unknown top-level keys are an error. Used for the core tier."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# UNTRUSTED: .all-contributorsrc (per-repo all-contributors data)
# We read only `contributors`; everything else (projectOwner, files, …) is
# ignored. Each entry is validated and capped; invalid entries are skipped by
# the caller, so a bad file never breaks the build.
# ---------------------------------------------------------------------------
_HANDLE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")  # GitHub login


class Contributor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    login: str = Field(max_length=39)
    name: str | None = Field(default=None, max_length=200)
    avatar_url: HttpUrl | None = None
    profile: HttpUrl | None = None
    contributions: list[Annotated[str, StringConstraints(max_length=40)]] = Field(
        default_factory=list, max_length=40
    )

    @field_validator("login")
    @classmethod
    def _check_login(cls, v: str) -> str:
        if not _HANDLE_RE.match(v):
            raise ValueError("invalid GitHub login")
        return v

    @field_validator("avatar_url")
    @classmethod
    def _check_avatar(cls, v: HttpUrl | None) -> HttpUrl | None:
        if v is not None and v.scheme != "https":
            raise ValueError("avatar_url must be https")
        return v

    @field_validator("profile")
    @classmethod
    def _check_profile(cls, v: HttpUrl | None) -> HttpUrl | None:
        if v is not None and v.scheme not in ("http", "https"):
            raise ValueError("profile must be http(s)")
        return v


# ---------------------------------------------------------------------------
# TRUSTED: one entry in the central catalog (core.toml / community.toml)
# An entry only points at a repo; identity + metadata come from the fetched
# .copier-answers.yml. Which file the entry lives in is its tier (set by the
# loader). The catalog is trusted (reviewed PR) but each entry is still
# shape-validated so a typo can't break the build or smuggle a bad URL.
# ---------------------------------------------------------------------------
Tier = Literal["core", "community"]

# Only github.com for now — fetch.py implements GitHub. Add gitlab.com once
# fetch.py grows a GitLab path.
ALLOWED_GIT_HOSTS = {"github.com"}
_REF_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")


def validate_ref_tag(tag: str) -> str | None:
    """Gate an untrusted git ref/tag name (e.g. a GitHub release `tagName`)
    before it is used as a ref expression or interpolated into a URL.

    Returns the tag unchanged iff it has no leading dash and matches the same
    character allow-list applied to pinned refs, else None. This is the
    trust-boundary check for auto-resolved release tags."""
    if tag.startswith("-") or not _REF_RE.match(tag):
        return None
    return tag


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: HttpUrl
    ref: str | None = None  # None = auto-resolve to the repo's latest release
    subdir: str | None = None
    tier: Tier  # injected by the loader from the source file
    # Curation flag: directory card shows a corner ribbon instead of a release pill
    work_in_progress: bool = False

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, v: HttpUrl) -> HttpUrl:
        if v.scheme != "https":
            raise ValueError("repo URL must be https")
        if v.host not in ALLOWED_GIT_HOSTS:
            raise ValueError(f"repo host {v.host!r} is not on the allowlist")
        return v

    @field_validator("ref")
    @classmethod
    def _check_ref(cls, v: str | None) -> str | None:
        if v is None:  # auto-resolve to the latest release
            return v
        if v.startswith("-") or not _REF_RE.match(v):
            raise ValueError("ref contains disallowed characters")
        return v

    @field_validator("subdir")
    @classmethod
    def _check_subdir(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v.startswith("/") or "\\" in v or ".." in v.split("/"):
            raise ValueError("subdir must be a safe relative path")
        return v
