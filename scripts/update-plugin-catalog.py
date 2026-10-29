"""Capture the public upstream catalog without installing or executing plugins."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin

import httpx

REPOSITORY = "https://github.com/openai/plugins.git"


async def capture_catalog(revision: str) -> dict:
    origin = f"https://raw.githubusercontent.com/openai/plugins/{revision}/"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={"User-Agent": "MiniCode catalog updater"}) as client:
        async def read(relative: str) -> dict:
            response = await client.get(origin + relative)
            response.raise_for_status()
            return response.json()

        catalog = await read(".agents/plugins/marketplace.json")
        semaphore = asyncio.Semaphore(8)

        async def plugin(entry: dict) -> dict:
            root = entry["source"]["path"].removeprefix("./")
            async with semaphore:
                manifest = await read(root + "/.codex-plugin/plugin.json")
            interface = manifest.get("interface", {})
            result = {**entry, "id": entry["name"] + "@" + catalog["name"],
                "version": manifest.get("version", ""), "description": manifest.get("description", ""),
                "interface": interface, "manifest": manifest,
                "source": {"source": "git", "url": REPOSITORY, "sha": revision,
                    "path": root, "sparse_paths": [root]}}
            for field in ("displayName", "shortDescription", "longDescription", "developerName", "defaultPrompt", "capabilities"):
                if field in interface:
                    result[field] = interface[field]
            for field, target in (("logo", "iconUrl"), ("logoDark", "iconDarkUrl")):
                if interface.get(field):
                    result[target] = urljoin(origin + root + "/", interface[field])
            result["previewImageUrls"] = [urljoin(origin + root + "/", value) for value in interface.get("screenshots", [])]
            result["manifest_sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
            return result

        return {"name": catalog["name"], "directory": "public", "isBuiltIn": True,
            "source": {"source": "git", "url": REPOSITORY}, "revision": revision,
            "status": "ready", "interface": {"displayName": "OpenAI"},
            "plugins": await asyncio.gather(*(plugin(entry) for entry in catalog["plugins"]))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("revision")
    parser.add_argument("--output", type=Path, default=Path(__file__).parents[1] / "backend/plugins/public-catalog.json")
    args = parser.parse_args()
    catalog = asyncio.run(capture_catalog(args.revision))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"revision": catalog["revision"], "plugins": len(catalog["plugins"]), "output": str(args.output)}))
