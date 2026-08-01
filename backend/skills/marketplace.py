from __future__ import annotations

import asyncio
import copy
import inspect
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from backend.mcp.marketplace import get_marketplace_connectors

USER_SKILLS_DIR = Path.home() / ".agents" / "skills"

OPENAI_SKILLS_CONTENTS_URL = "https://api.github.com/repos/openai/skills/contents/skills/.curated?ref=main"
OPENAI_SKILL_RAW_URL = "https://raw.githubusercontent.com/openai/skills/main/skills/.curated/{name}/SKILL.md"
MCP_REGISTRY_SERVERS_URL = "https://registry.modelcontextprotocol.io/v0.1/servers?limit=10"
MARKETPLACE_CACHE_TTL_SECONDS = 15 * 60
MARKETPLACE_ERROR_CACHE_TTL_SECONDS = 60
MARKETPLACE_HTTP_TIMEOUT_SECONDS = 8.0
MCP_REGISTRY_SOURCE_TIMEOUT_SECONDS = 4.0
OPENAI_SKILLS_CATALOG_TIMEOUT_SECONDS = 7.0
OPENAI_SKILLS_SOURCE_TIMEOUT_SECONDS = 5.0
OPENAI_SKILLS_METADATA_LIMIT = 24
OPENAI_SKILLS_METADATA_CONCURRENCY = 8

FetchJson = Callable[[str], Awaitable[Any] | Any]
FetchText = Callable[[str], Awaitable[str] | str]


CURATED_SKILLS: dict[str, dict[str, Any]] = {
    "github-actions-auditor": {
        "name": "github-actions-auditor",
        "title": "GitHub Actions Auditor",
        "description": "Review GitHub Actions workflows for unsafe permissions, flaky cache steps, and release blockers.",
        "triggers": ["github actions", "workflow", "ci", "release", "actions"],
        "body": """# GitHub Actions Auditor

Use this skill when reviewing GitHub Actions workflows.

Focus on:
- Overly broad `permissions` blocks.
- Unpinned third-party actions.
- Cache keys that can produce flaky or unsafe builds.
- Release jobs that publish without clear approval or provenance.
- Matrix jobs that hide failures behind `continue-on-error`.

When reporting findings, list the workflow file, job name, severity, and a concrete fix.
""",
    },
    "react-ui-reviewer": {
        "name": "react-ui-reviewer",
        "title": "React UI Reviewer",
        "description": "Inspect React component hierarchy, accessibility, empty states, and interaction polish.",
        "triggers": ["react", "ui", "accessibility", "component", "frontend"],
        "body": """# React UI Reviewer

Use this skill when reviewing or improving React UI.

Focus on:
- Clear hierarchy, spacing, and readable density.
- Keyboard navigation and visible focus states.
- Accessible names for icon-only controls.
- Loading, empty, disabled, and error states.
- Avoiding fake controls that do not call real handlers.

Give feedback as specific UI risks with file references and practical fixes.
""",
    },
    "python-refactor-kit": {
        "name": "python-refactor-kit",
        "title": "Python Refactor Kit",
        "description": "Plan safe Python refactors with pytest coverage, dependency checks, and migration notes.",
        "triggers": ["python", "pytest", "refactor", "typing", "migration"],
        "body": """# Python Refactor Kit

Use this skill when planning or executing Python refactors.

Focus on:
- Preserving public behavior with regression tests.
- Keeping modules small and import boundaries clear.
- Avoiding broad rewrites when targeted extraction is safer.
- Running focused pytest commands before broader suites.
- Calling out migration notes when APIs or config shapes change.

Prefer small, reversible changes backed by tests.
""",
    },
}

_MARKETPLACE_CACHE: dict[str, Any] | None = None
_MARKETPLACE_CACHE_EXPIRES_AT = 0.0


async def _maybe_await(value: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _default_fetch_json(url: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json" if "api.github.com" in url else "application/json",
        "User-Agent": "MiniCode/0.2",
    }
    timeout = httpx.Timeout(MARKETPLACE_HTTP_TIMEOUT_SECONDS, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


async def _default_fetch_text(url: str) -> str:
    timeout = httpx.Timeout(MARKETPLACE_HTTP_TIMEOUT_SECONDS, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "MiniCode/0.2"})
        response.raise_for_status()
        return response.text


def _error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message or type(exc).__name__


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_skill_name(skill_name: str) -> str:
    normalized_name = skill_name.strip().lower()
    if not normalized_name or "/" in normalized_name or "\\" in normalized_name or ".." in normalized_name:
        raise ValueError("Skill name is not a safe directory name.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", normalized_name):
        raise ValueError("Skill name is not a safe directory name.")
    return normalized_name


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "mcp-provider"


def _title_from_name(name: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_]+", name) if part) or name


def _parse_skill_frontmatter(content: str, fallback_name: str) -> dict[str, str]:
    metadata: dict[str, str] = {"name": fallback_name, "description": ""}
    if not content.startswith("---"):
        return metadata

    parts = content.split("---", 2)
    if len(parts) < 3:
        return metadata

    for raw_line in parts[1].splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        clean_key = key.strip().lower()
        clean_value = value.strip().strip('"').strip("'")
        if clean_key in {"name", "title", "description"} and clean_value:
            metadata[clean_key] = clean_value

    metadata.setdefault("title", metadata["name"])
    return metadata


def _render_skill_file(entry: dict[str, Any]) -> str:
    return (
        "---\n"
        f"name: {entry['name']}\n"
        f"description: {entry['description']}\n"
        "---\n\n"
        f"{entry['body'].strip()}\n"
    )


def _fallback_marketplace_skills() -> list[dict[str, Any]]:
    return [
        {
            "name": item["name"],
            "title": item["title"],
            "description": item["description"],
            "installed": False,
            "source": "fallback",
            "path": f"skills/.curated/{item['name']}",
        }
        for item in CURATED_SKILLS.values()
    ]


def _openai_directory_skill_entry(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "title": _title_from_name(name),
        "description": f"OpenAI curated Skill: {name}",
        "installed": False,
        "source": "openai",
        "path": f"skills/.curated/{name}",
    }


async def _fetch_openai_curated_skills(
    fetch_json: FetchJson,
    fetch_text: FetchText,
    *,
    enrich_metadata: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    directory_payload = await _maybe_await(fetch_json(OPENAI_SKILLS_CONTENTS_URL))
    if not isinstance(directory_payload, list):
        raise ValueError("OpenAI Skills directory response was not a list.")

    skill_names = [
        str(item.get("name", "")).strip().lower()
        for item in directory_payload
        if isinstance(item, dict) and item.get("type") == "dir" and item.get("name")
    ]
    skill_names = [_safe_skill_name(name) for name in skill_names[:80]]
    skills_by_name = {name: _openai_directory_skill_entry(name) for name in skill_names}
    # The default desktop path stays directory-first so the market opens
    # quickly. Injected loaders (tests or future cached metadata providers)
    # can still enrich a bounded subset without changing the public contract.
    metadata_names = skill_names[:OPENAI_SKILLS_METADATA_LIMIT] if enrich_metadata else []
    semaphore = asyncio.Semaphore(OPENAI_SKILLS_METADATA_CONCURRENCY)

    async def load_skill(name: str) -> dict[str, Any]:
        async with semaphore:
            content = await _maybe_await(fetch_text(OPENAI_SKILL_RAW_URL.format(name=name)))
            metadata = _parse_skill_frontmatter(content, name)
            return {
                "name": name,
                "title": metadata.get("title") or metadata.get("name") or _title_from_name(name),
                "description": metadata.get("description") or f"OpenAI curated Skill: {name}",
                "installed": False,
                "source": "openai",
                "path": f"skills/.curated/{name}",
            }

    metadata_status = "directory-only"
    if metadata_names:
        try:
            enriched_skills = await asyncio.wait_for(
                asyncio.gather(*(load_skill(name) for name in metadata_names), return_exceptions=True),
                timeout=OPENAI_SKILLS_SOURCE_TIMEOUT_SECONDS,
            )
            enriched_count = 0
            for entry in enriched_skills:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    skills_by_name[entry["name"]] = entry
                    enriched_count += 1
            metadata_status = "partial" if enriched_count < len(metadata_names) else "enriched"
        except Exception:
            metadata_status = "directory-only"

    skills = [skills_by_name[name] for name in skill_names]
    return skills, {
        "source": "live",
        "ok": True,
        "count": len(skills),
        "metadata": metadata_status,
    }


def registry_server_to_marketplace_mcp(record: dict[str, Any], installed_names: set[str] | None = None) -> dict[str, Any]:
    installed_names = installed_names or set()
    server = record.get("server") if isinstance(record.get("server"), dict) else record
    name = str(server.get("name") or server.get("title") or "mcp-server")
    title = str(server.get("title") or name)
    provider_name = _slug(name)
    description = str(server.get("description") or "MCP Registry server.")
    version = str(server.get("version") or "")
    remotes = server.get("remotes") if isinstance(server.get("remotes"), list) else []
    remote = next(
        (
            item
            for item in remotes
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and item["url"].startswith(("https://", "http://"))
            and str(item.get("type", "")).lower() in {"streamable-http", "sse", "http"}
        ),
        None,
    )
    repository = server.get("repository") if isinstance(server.get("repository"), dict) else {}
    website_url = str(server.get("websiteUrl") or repository.get("url") or (remote or {}).get("url") or "").strip()
    icons = server.get("icons") if isinstance(server.get("icons"), list) else []
    icon_url = next(
        (
            str(icon.get("src") or "").strip()
            for icon in icons
            if isinstance(icon, dict) and str(icon.get("src") or "").startswith(("https://", "http://"))
        ),
        "",
    )
    config_snippet = (
        {
            "name": provider_name,
            "server": {
                "transport": "http",
                "url": remote["url"],
                "autoStart": False,
                "maxRetries": 3,
            },
        }
        if remote
        else None
    )
    meta = record.get("_meta") if isinstance(record.get("_meta"), dict) else {}
    official = meta.get("io.modelcontextprotocol.registry/official") if isinstance(meta, dict) else None
    official_status = official.get("status") if isinstance(official, dict) else None

    return {
        "name": name,
        "providerName": provider_name,
        "title": title,
        "description": description,
        "version": version,
        "source": "mcp-registry",
        "installed": name in installed_names or provider_name in installed_names,
        "actionable": config_snippet is not None,
        "setup_mode": "remote" if config_snippet else "manual",
        "config_snippet": config_snippet,
        "transport": "http" if remote else "manual",
        "url": str((remote or {}).get("url") or ""),
        "autoStart": False,
        "maxRetries": 3,
        "websiteUrl": website_url,
        "docsUrl": website_url,
        "iconUrl": icon_url,
        "tags": ["MCP", "Registry", "Remote" if config_snippet else "Manual"],
        "official": official_status == "active",
    }


def _dedupe_mcp_registry_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        server = record.get("server") if isinstance(record.get("server"), dict) else record
        name = str(server.get("name") or server.get("title") or "")
        if not name:
            continue
        meta = record.get("_meta") if isinstance(record.get("_meta"), dict) else {}
        official = meta.get("io.modelcontextprotocol.registry/official") if isinstance(meta, dict) else {}
        is_latest = bool(official.get("isLatest")) if isinstance(official, dict) else False
        if name not in deduped or is_latest:
            deduped[name] = record
    return list(deduped.values())


async def _fetch_mcp_registry(fetch_json: FetchJson) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    registry_payload = await _maybe_await(fetch_json(MCP_REGISTRY_SERVERS_URL))
    records = registry_payload.get("servers", []) if isinstance(registry_payload, dict) else []
    if not isinstance(records, list):
        raise ValueError("MCP Registry response did not include a servers list.")
    mcp_entries = [registry_server_to_marketplace_mcp(record) for record in _dedupe_mcp_registry_records(records)]
    return mcp_entries, {"source": "live", "ok": True, "count": len(mcp_entries)}


def _apply_installed_flags(
    payload: dict[str, Any],
    installed_skill_names: set[str],
    installed_mcp_names: set[str],
) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    for skill in result.get("skills", []):
        if isinstance(skill, dict):
            skill["installed"] = str(skill.get("name", "")) in installed_skill_names
    for entry in result.get("mcp", []):
        if isinstance(entry, dict):
            provider_name = str(entry.get("providerName") or "")
            entry["installed"] = str(entry.get("name", "")) in installed_mcp_names or provider_name in installed_mcp_names
    return result


def _merge_curated_connectors(
    registry_entries: list[dict[str, Any]],
    installed_mcp_names: set[str],
) -> list[dict[str, Any]]:
    curated = [
        {
            **entry,
            "source": "curated",
            "providerName": str(entry.get("name") or ""),
            "websiteUrl": str(entry.get("docsUrl") or ""),
            "iconUrl": str(entry.get("iconUrl") or ""),
        }
        for entry in get_marketplace_connectors(sorted(installed_mcp_names))
    ]
    merged = curated[:]
    seen = {str(entry.get("providerName") or entry.get("name") or "").lower() for entry in curated}
    for entry in registry_entries:
        key = str(entry.get("providerName") or entry.get("name") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


async def list_extensions_marketplace(
    installed_names: set[str] | None = None,
    *,
    installed_mcp_names: set[str] | None = None,
    fetch_json: FetchJson | None = None,
    fetch_text: FetchText | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    global _MARKETPLACE_CACHE, _MARKETPLACE_CACHE_EXPIRES_AT

    installed_names = installed_names or set()
    installed_mcp_names = installed_mcp_names or set()
    now = time.monotonic()
    if _MARKETPLACE_CACHE is not None and not force_refresh and now < _MARKETPLACE_CACHE_EXPIRES_AT:
        return _apply_installed_flags(_MARKETPLACE_CACHE, installed_names, installed_mcp_names)

    fetch_text_was_injected = fetch_text is not None
    fetch_json = fetch_json or _default_fetch_json
    fetch_text = fetch_text or _default_fetch_text

    async def get_skills():
        try:
            return await asyncio.wait_for(
                _fetch_openai_curated_skills(
                    fetch_json,
                    fetch_text,
                    enrich_metadata=fetch_text_was_injected,
                ),
                timeout=OPENAI_SKILLS_CATALOG_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            fb = _fallback_marketplace_skills()
            return fb, {"source": "fallback", "ok": False, "count": len(fb), "error": _error_message(exc)}

    async def get_mcp():
        try:
            return await asyncio.wait_for(
                _fetch_mcp_registry(fetch_json),
                timeout=MCP_REGISTRY_SOURCE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return [], {"source": "fallback", "ok": False, "count": 0, "error": _error_message(exc)}

    # These two public sources frequently share a constrained desktop proxy.
    # Fetching them in parallel can make both time out; prioritize the Skill
    # directory, then spend a short bounded window enriching the MCP catalog.
    skills, openai_status = await get_skills()
    mcp, mcp_status = await get_mcp()

    base_payload = {
        "skills": skills,
        "mcp": _merge_curated_connectors(mcp, set()),
        "source_status": {
            "openai_skills": openai_status,
            "mcp_registry": mcp_status,
        },
        "generated_at": _utc_now_iso(),
    }
    _MARKETPLACE_CACHE = copy.deepcopy(base_payload)
    all_sources_ok = all(bool(status.get("ok")) for status in base_payload["source_status"].values())
    cache_ttl = MARKETPLACE_CACHE_TTL_SECONDS if all_sources_ok else MARKETPLACE_ERROR_CACHE_TTL_SECONDS
    _MARKETPLACE_CACHE_EXPIRES_AT = now + cache_ttl
    return _apply_installed_flags(base_payload, installed_names, installed_mcp_names)


def list_curated_skills(installed_names: set[str] | None = None) -> list[dict[str, Any]]:
    installed_names = installed_names or set()
    return [
        {
            **item,
            "installed": item["name"] in installed_names,
        }
        for item in _fallback_marketplace_skills()
    ]


def install_curated_skill(skill_name: str, skills_dir: Path | None = None) -> dict[str, Any]:
    normalized_name = _safe_skill_name(skill_name)
    if normalized_name not in CURATED_SKILLS:
        raise KeyError(f"Skill '{skill_name}' is not in the curated marketplace.")

    entry = CURATED_SKILLS[normalized_name]
    target_root = skills_dir or USER_SKILLS_DIR
    skill_dir = target_root / normalized_name
    skill_file = skill_dir / "SKILL.md"
    if skill_dir.exists():
        raise FileExistsError(f"Skill '{normalized_name}' is already installed.")

    skill_dir.mkdir(parents=True, exist_ok=False)
    skill_file.write_text(_render_skill_file(entry), encoding="utf-8")

    return {
        "installed": True,
        "skill": {
            "name": entry["name"],
            "title": entry["title"],
            "description": entry["description"],
            "installed": True,
            "source": "fallback",
        },
        "path": str(skill_file),
    }


async def install_marketplace_skill(
    skill_name: str,
    skills_dir: Path | None = None,
    *,
    fetch_text: FetchText | None = None,
) -> dict[str, Any]:
    normalized_name = _safe_skill_name(skill_name)
    target_root = skills_dir or USER_SKILLS_DIR
    skill_dir = target_root / normalized_name
    skill_file = skill_dir / "SKILL.md"
    if skill_dir.exists():
        raise FileExistsError(f"Skill '{normalized_name}' is already installed.")

    fetch_text = fetch_text or _default_fetch_text
    source = "openai"
    try:
        content = await _maybe_await(fetch_text(OPENAI_SKILL_RAW_URL.format(name=normalized_name)))
    except Exception:
        if normalized_name not in CURATED_SKILLS:
            raise KeyError(f"Skill '{skill_name}' is not in the OpenAI curated marketplace.")
        content = _render_skill_file(CURATED_SKILLS[normalized_name])
        source = "fallback"

    metadata = _parse_skill_frontmatter(content, normalized_name)
    skill_dir.mkdir(parents=True, exist_ok=False)
    skill_file.write_text(content.rstrip() + "\n", encoding="utf-8")

    return {
        "installed": True,
        "skill": {
            "name": normalized_name,
            "title": metadata.get("title") or normalized_name,
            "description": metadata.get("description") or "",
            "installed": True,
            "source": source,
        },
        "path": str(skill_file),
    }


def remove_user_skill(skill_name: str, skills_dir: Path | None = None) -> dict[str, Any]:
    normalized_name = _safe_skill_name(skill_name)

    target_root = (skills_dir or USER_SKILLS_DIR).resolve()
    skill_dir = (target_root / normalized_name).resolve()
    skill_file = skill_dir / "SKILL.md"
    if target_root not in skill_dir.parents:
        raise ValueError("Skill path is outside the user skills directory.")
    if not skill_file.exists() or not skill_dir.is_dir():
        raise FileNotFoundError(f"Skill '{normalized_name}' is not installed in the user skills directory.")

    shutil.rmtree(skill_dir)
    entry = CURATED_SKILLS.get(normalized_name, {})
    return {
        "removed": True,
        "skill": {
            "name": normalized_name,
            "title": entry.get("title", normalized_name),
            "description": entry.get("description", ""),
            "installed": False,
            "source": "user",
        },
        "path": str(skill_file),
    }
