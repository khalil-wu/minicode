"""Build the public catalog and its logos from a pinned upstream checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.mcp.manager import _MCP_SERVER_FIELDS, _PLUGIN_MCP_DISPLAY_FIELDS
from backend.plugins.manifest import _manifest_mcp_server_names, plugin_skill_sources
from backend.skills.loader import SkillLoader

REPOSITORY = "https://github.com/openai/plugins.git"


def git_revision(root: Path) -> str:
    if (root / ".git").exists():
        return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    return (root.parent / "plugins.sha").read_text(encoding="utf8").strip()


def remote_revision(url: str) -> str:
    return subprocess.check_output(["git", "ls-remote", url, "HEAD"], text=True).split()[0]


def github_raw(url: str, revision: str, path: str) -> bytes:
    repo = urlparse(url).path.strip("/").removesuffix(".git")
    with urllib.request.urlopen(f"https://raw.githubusercontent.com/{repo}/{revision}/{path}", timeout=30) as response:
        return response.read()


def capture_catalog(root: Path, revision: str, icon_dir: Path) -> dict:
    if git_revision(root) != revision:
        raise ValueError("The checkout revision does not match the requested revision")
    catalog = json.loads((root / ".agents/plugins/marketplace.json").read_text(encoding="utf8"))
    loader = SkillLoader()
    result = []
    icon_dir.mkdir(parents=True, exist_ok=True)
    for entry in catalog["plugins"]:
        source = entry["source"]
        name = entry["name"]
        if source["source"] == "local":
            plugin_root = root / source["path"]
            manifest_path = plugin_root / ".codex-plugin/plugin.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf8"))
            read_asset = lambda path: (plugin_root / path).read_bytes()
            install_source = {"source": "git", "url": REPOSITORY, "sha": revision,
                "path": source["path"].removeprefix("./"), "sparse_paths": [source["path"].removeprefix("./")]}
        else:
            url = source["url"]
            external_revision = remote_revision(url)
            subpath = source.get("path", "").strip("/")
            prefix = f"{subpath}/" if subpath else ""
            manifest = json.loads(github_raw(url, external_revision, prefix + ".codex-plugin/plugin.json"))
            read_asset = lambda path: github_raw(url, external_revision, prefix + path.removeprefix("./"))
            plugin_root = None
            install_source = {"source": "git", "url": url, "sha": external_revision}
            if subpath:
                install_source["path"] = subpath
                install_source["sparse_paths"] = [subpath]

        interface = dict(manifest.get("interface") or {})
        interface.update(entry.get("interface") or {})
        interface["category"] = entry.get("category") or interface.get("category")
        for field in ("website", "privacyPolicy", "termsOfService"):
            if field + "URL" in interface:
                interface[field + "Url"] = interface[field + "URL"]
        prompts = interface.get("defaultPrompt", [])
        prompts = [prompts] if isinstance(prompts, str) else prompts
        prompts = [" ".join(value.split()) for value in prompts if isinstance(value, str)]
        prompts = [value for value in prompts if value and len(value) <= 128][:3]
        item = {"id": f"{name}@{catalog['name']}", "name": name, "source": install_source,
            "policy": entry.get("policy", {}), "category": entry.get("category"),
            "version": manifest.get("version", ""), "description": manifest.get("description", ""),
            "interface": interface, "defaultPrompt": prompts}
        for field, target in (("logo", "iconAsset"), ("logoDark", "iconDarkAsset")):
            if interface.get(field):
                contents = read_asset(interface[field])
                filename = hashlib.sha256(contents).hexdigest()[:20] + Path(interface[field]).suffix.lower()
                (icon_dir / filename).write_bytes(contents)
                item[target] = f"apps/plugin-logos/{filename}"
        for field, target in (("skills", "skills"), ("mcpServers", "mcpServers"), ("apps", "apps"), ("hooks", "hooks")):
            if field in manifest:
                item[target] = manifest[field]
        if plugin_root is not None:
            mcp_path = plugin_root / ".mcp.json"
            if mcp_path.is_file():
                servers = json.loads(mcp_path.read_text(encoding="utf8"))
                servers = servers.get("mcpServers", servers)
                unsupported = sorted({key for server in servers.values() if isinstance(server, dict)
                    for key in server if key not in _MCP_SERVER_FIELDS and key not in _PLUGIN_MCP_DISPLAY_FIELDS and key != "type"})
                unsupported_oauth = sorted({key for server in servers.values() if isinstance(server, dict)
                    for key in (server.get("oauth") or {}) if key not in {"client_id", "callback_port"}})
                if unsupported or unsupported_oauth:
                    item["runtime_blocker"] = "Unsupported MCP configuration: " + ", ".join([*unsupported, *["oauth." + key for key in unsupported_oauth]])
                item["mcpServers"] = servers
                item["mcp_server_names"] = _manifest_mcp_server_names(manifest_path, plugin_id=item["id"])
            roots, direct = plugin_skill_sources(plugin_root, [manifest_path])
            skills = [loader._parse_frontmatter(path, "plugin") for directory in roots if directory.is_dir()
                for path in loader.skill_files_for_root(directory, direct_children=direct)]
            item["skill_summaries"] = [{"name": skill.name, "displayName": skill.display_name,
                "description": skill.short_description or skill.description}
                for skill in skills if skill is not None and (not skill.products or "codex" in skill.products)]
            if manifest.get("apps") and not item["skill_summaries"] and not item.get("mcp_server_names"):
                item["runtime_blocker"] = "This plugin only declares a ChatGPT app, which MiniCode does not provide"
        result.append(item)
    return {"name": catalog["name"], "directory": "public", "isBuiltIn": True,
        "source": {"source": "git", "url": REPOSITORY}, "revision": revision,
        "status": "ready", "interface": catalog.get("interface") or {"displayName": "OpenAI"}, "plugins": result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("revision")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "backend/plugins/public-catalog.json")
    parser.add_argument("--icons", type=Path, default=ROOT / "frontend/public/apps/plugin-logos")
    args = parser.parse_args()
    if args.source:
        catalog = capture_catalog(args.source, args.revision, args.icons)
    else:
        with tempfile.TemporaryDirectory(prefix="minicode-public-catalog-") as temp:
            checkout = Path(temp) / "plugins"
            subprocess.run(["git", "init", str(checkout)], check=True)
            subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", REPOSITORY], check=True)
            subprocess.run(["git", "-C", str(checkout), "fetch", "--depth", "1", "origin", args.revision], check=True)
            subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"], check=True)
            catalog = capture_catalog(checkout, args.revision, args.icons)
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(json.dumps({"revision": catalog["revision"], "plugins": len(catalog["plugins"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
