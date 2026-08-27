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

from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.agent.markdown_scopes import get_minicode_config_home_dir

USER_SKILLS_DIR = get_minicode_config_home_dir() / "skills"

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
                "auto_start": False,
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
        "provider_name": provider_name,
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
        "auto_start": False,
        "website_url": website_url,
        "docs_url": website_url,
        "icon_url": icon_url,
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
            provider_name = str(entry.get("provider_name") or "")
            entry["installed"] = str(entry.get("name", "")) in installed_mcp_names or provider_name in installed_mcp_names
    return result


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
    # Network catalogs are opt-in. MiniCode should remain deterministic and
    # must not import another user's extension marketplace state by default.
    network_enabled = fetch_json is not None or fetch_text is not None or str(
        os.environ.get("MINICODE_ENABLE_NETWORK_MARKETPLACE") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not network_enabled:
        return _apply_installed_flags({
            "skills": [],
            "mcp": [],
            "source_status": {
                "openai_skills": {"source": "disabled", "ok": True, "count": 0, "reason": "network marketplace disabled"},
                "mcp_registry": {"source": "disabled", "ok": True, "count": 0, "reason": "network marketplace disabled"},
            },
            "generated_at": _utc_now_iso(),
        }, installed_names, installed_mcp_names)
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
            return [], {"source": "unavailable", "ok": False, "count": 0, "error": _error_message(exc)}

    async def get_mcp():
        try:
            return await asyncio.wait_for(
                _fetch_mcp_registry(fetch_json),
                timeout=MCP_REGISTRY_SOURCE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # No local connector catalog exists as a fallback.  Reporting one
            # here would falsely imply that manually-installable MCP entries
            # remain available when the Registry cannot be reached.
            return [], {"source": "unavailable", "ok": False, "count": 0, "error": _error_message(exc)}

    # These two public sources frequently share a constrained desktop proxy.
    # Fetching them in parallel can make both time out; prioritize the Skill
    # directory, then spend a short bounded window enriching the MCP catalog.
    skills, openai_status = await get_skills()
    mcp, mcp_status = await get_mcp()

    base_payload = {
        "skills": skills,
        "mcp": mcp,
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
    content = await _maybe_await(fetch_text(OPENAI_SKILL_RAW_URL.format(name=normalized_name)))

    metadata = _parse_skill_frontmatter(content, normalized_name)
    with file_mutation_locks([skill_file]):
        if skill_dir.exists() or skill_dir.is_symlink():
            raise FileExistsError(f"Skill '{normalized_name}' is already installed.")
        skill_dir.mkdir(parents=True, exist_ok=False)
        try:
            atomic_write_text(skill_file, content.rstrip() + "\n", encoding="utf-8")
        except Exception:
            shutil.rmtree(skill_dir, ignore_errors=True)
            raise

    return {
        "installed": True,
        "skill": {
            "name": normalized_name,
            "title": metadata.get("title") or normalized_name,
            "description": metadata.get("description") or "",
            "installed": True,
            "source": "openai",
        },
        "path": str(skill_file),
    }


def import_local_skill(source_path: str | Path, skills_dir: Path | None = None) -> dict[str, Any]:
    """Copy a local SKILL.md directory into MiniCode's private skill root."""
    source = Path(source_path).expanduser().resolve()
    if source.is_file() and source.name.casefold() == "skill.md":
        source_dir = source.parent
    elif source.is_dir():
        source_dir = source
    else:
        raise FileNotFoundError("本地技能必须是包含 SKILL.md 的文件夹或 SKILL.md 文件。")
    skill_file = source_dir / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        raise ValueError("本地技能目录必须包含真实的 SKILL.md 文件。")
    for candidate in source_dir.rglob("*"):
        if candidate.is_symlink():
            raise ValueError("本地技能不能包含符号链接。")
    metadata = _parse_skill_frontmatter(skill_file.read_text(encoding="utf-8"), source_dir.name)
    normalized_name = _safe_skill_name(metadata.get("name") or source_dir.name)
    target_root = (skills_dir or USER_SKILLS_DIR).resolve()
    target_dir = target_root / normalized_name
    if target_root not in target_dir.parents:
        raise ValueError("技能路径不在 MiniCode 技能目录内。")
    if target_dir.exists():
        raise FileExistsError(f"Skill '{normalized_name}' is already installed.")
    with file_mutation_locks([target_dir / "SKILL.md"]):
        if target_dir.exists():
            raise FileExistsError(f"Skill '{normalized_name}' is already installed.")
        target_dir.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
    return {
        "installed": True,
        "skill": {
            "name": normalized_name,
            "title": metadata.get("title") or normalized_name,
            "description": metadata.get("description") or "",
            "installed": True,
            "source": "minicode-user",
        },
        "path": str(target_dir / "SKILL.md"),
    }


def remove_user_skill(skill_name: str, skills_dir: Path | None = None) -> dict[str, Any]:
    normalized_name = _safe_skill_name(skill_name)

    target_root = (skills_dir or USER_SKILLS_DIR).resolve()
    # Keep the final path unresolved so a malicious pre-existing symlink can
    # be rejected before any recursive removal follows it.
    skill_dir = target_root / normalized_name
    skill_file = skill_dir / "SKILL.md"
    if target_root not in skill_dir.parents:
        raise ValueError("Skill path is outside the user skills directory.")
    with file_mutation_locks([skill_file]):
        if skill_dir.is_symlink() or not skill_file.exists() or not skill_dir.is_dir():
            raise FileNotFoundError(f"Skill '{normalized_name}' is not installed in the user skills directory.")
        if skill_file.is_symlink():
            raise ValueError("Skill file cannot be a symbolic link.")
        shutil.rmtree(skill_dir)
    return {
        "removed": True,
        "skill": {
            "name": normalized_name,
            "title": normalized_name,
            "description": "",
            "installed": False,
            "source": "user",
        },
        "path": str(skill_file),
    }
