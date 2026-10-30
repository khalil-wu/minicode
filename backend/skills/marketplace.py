from __future__ import annotations

from backend.async_cleanup import to_thread_cancel_safe

import asyncio
import copy
import inspect
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from backend.atomic_io import file_mutation_locks
from backend.agent.markdown_scopes import get_minicode_config_home_dir
from backend.plugins.materializer import materialize_source
from backend.skills.frontmatter import parse_skill_frontmatter

USER_SKILLS_DIR = get_minicode_config_home_dir() / "skills"
_SKILL_DIRECTORY_NAME = re.compile(r"(?!.*\.\.)[a-z0-9][a-z0-9._-]*")

PUBLIC_SKILLS_CATALOG = Path(__file__).parent / "public-catalog.json"
MCP_REGISTRY_SERVERS_URL = "https://registry.modelcontextprotocol.io/v0.1/servers?limit=10"
MARKETPLACE_CACHE_TTL_SECONDS = 15 * 60
MARKETPLACE_ERROR_CACHE_TTL_SECONDS = 60
MARKETPLACE_HTTP_TIMEOUT_SECONDS = 8.0
MCP_REGISTRY_SOURCE_TIMEOUT_SECONDS = 4.0

FetchJson = Callable[[str], Awaitable[Any] | Any]


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


def _error_message(exc: Exception) -> str:
    message = str(exc).strip()
    return message or type(exc).__name__


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_skill_name(skill_name: str) -> str:
    normalized_name = skill_name.strip().lower()
    if not _SKILL_DIRECTORY_NAME.fullmatch(normalized_name):
        raise ValueError("Skill name is not a safe directory name.")
    return normalized_name


def user_skill_uninstall_name(skill_path: str) -> str | None:
    """Return the identity accepted by DELETE for a MiniCode-owned install."""
    skill_file = Path(skill_path).absolute()
    directory = skill_file.parent
    if (directory.parent.resolve() != USER_SKILLS_DIR.resolve()
            or not _SKILL_DIRECTORY_NAME.fullmatch(directory.name)
            or directory.is_symlink() or skill_file.is_symlink()):
        return None
    return directory.name


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "mcp-provider"


def bundled_skills_marketplace(installed_names: set[str] | None = None) -> dict[str, Any]:
    catalog = json.loads(PUBLIC_SKILLS_CATALOG.read_text(encoding="utf-8"))
    installed = installed_names or set()
    skills = [{**skill, "installed": skill["name"] in installed} for skill in catalog["skills"]]
    return {"skills": skills, "source_status": {"openai_skills": {
        "source": "bundled", "ok": True, "count": len(skills), "revision": catalog["revision"],
    }}, "generated_at": _utc_now_iso()}


def bundled_skill_detail(skill_name: str) -> dict[str, str]:
    from backend.skills.loader import SkillLoader

    name = _safe_skill_name(skill_name)
    catalog = json.loads(PUBLIC_SKILLS_CATALOG.read_text(encoding="utf-8"))
    if not any(skill["name"] == name for skill in catalog["skills"]):
        raise KeyError(name)
    raw = (PUBLIC_SKILLS_CATALOG.parent / "curated" / name / "SKILL.md").read_text(encoding="utf-8-sig")
    return {"content": SkillLoader._extract_body(raw), "raw_content": raw}


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
    transport = "sse" if remote and str(remote.get("type", "")).lower() == "sse" else "http"
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
                "transport": transport,
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
        "transport": transport if remote else "manual",
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
    force_refresh: bool = False,
) -> dict[str, Any]:
    global _MARKETPLACE_CACHE, _MARKETPLACE_CACHE_EXPIRES_AT

    installed_names = installed_names or set()
    installed_mcp_names = installed_mcp_names or set()
    bundled = bundled_skills_marketplace()
    network_enabled = fetch_json is not None or str(
        os.environ.get("MINICODE_ENABLE_NETWORK_MARKETPLACE", "1")
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not network_enabled:
        return _apply_installed_flags({
            "skills": bundled["skills"],
            "mcp": [],
            "source_status": {
                "openai_skills": bundled["source_status"]["openai_skills"],
                "mcp_registry": {"source": "disabled", "ok": True, "count": 0, "reason": "network marketplace disabled"},
            },
            "generated_at": _utc_now_iso(),
        }, installed_names, installed_mcp_names)
    now = time.monotonic()
    if _MARKETPLACE_CACHE is not None and not force_refresh and now < _MARKETPLACE_CACHE_EXPIRES_AT:
        return _apply_installed_flags(_MARKETPLACE_CACHE, installed_names, installed_mcp_names)

    fetch_json = fetch_json or _default_fetch_json

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

    mcp, mcp_status = await get_mcp()

    base_payload = {
        "skills": bundled["skills"],
        "mcp": mcp,
        "source_status": {
            "openai_skills": bundled["source_status"]["openai_skills"],
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
) -> dict[str, Any]:
    normalized_name = _safe_skill_name(skill_name)
    target_root = skills_dir or USER_SKILLS_DIR
    skill_dir = target_root / normalized_name
    skill_file = skill_dir / "SKILL.md"
    metadata: dict[str, Any] = {}

    def validate_bundle(root: Path) -> None:
        metadata.update(parse_skill_frontmatter(
            (root / "SKILL.md").read_text(encoding="utf-8-sig"), normalized_name,
        ))

    def install_bundle() -> None:
        # Share the mutation boundary with local import/removal. Materialize the
        # complete directory: skills can depend on scripts, assets and agents/.
        with file_mutation_locks([skill_file]):
            if skill_dir.exists() or skill_dir.is_symlink():
                raise FileExistsError(f"Skill '{normalized_name}' is already installed.")
            materialize_source(
                {"source": "github", "repo": "openai/skills", "sha": json.loads(PUBLIC_SKILLS_CATALOG.read_text(encoding="utf-8"))["revision"],
                 "path": f"skills/.curated/{normalized_name}"},
                skill_dir,
                overwrite=False,
                validate=validate_bundle,
                timeout_seconds=90,
            )

    await to_thread_cancel_safe(install_bundle)

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
    metadata = parse_skill_frontmatter(skill_file.read_text(encoding="utf-8-sig"), source_dir.name)
    normalized_name = _safe_skill_name(metadata.get("name") or source_dir.name)
    target_root = (skills_dir or USER_SKILLS_DIR).resolve()
    target_dir = target_root / normalized_name
    if target_root not in target_dir.parents:
        raise ValueError("技能路径不在 MiniCode 技能目录内。")
    with file_mutation_locks([target_dir / "SKILL.md"]):
        if target_dir.exists() or target_dir.is_symlink():
            raise FileExistsError(f"Skill '{normalized_name}' is already installed.")
        materialize_source(
            {"source": "directory", "path": str(source_dir)},
            target_dir,
            overwrite=False,
            copy_local_directory=True,
        )
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
