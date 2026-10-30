"""Generate the bundled curated Skill index from an openai/skills checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from backend.skills.frontmatter import parse_skill_frontmatter


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    checkout = parser.parse_args().checkout.resolve()
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    curated = checkout / "skills" / ".curated"
    icons = REPO / "frontend/public/apps/skill-logos"
    notices = REPO / "backend/skills/licenses"
    previews = REPO / "backend/skills/curated"
    icons.mkdir(parents=True, exist_ok=True)
    notices.mkdir(parents=True, exist_ok=True)
    entries = []
    license_rows = []
    for directory in sorted(path for path in curated.iterdir() if path.is_dir()):
        metadata = parse_skill_frontmatter((directory / "SKILL.md").read_text(encoding="utf-8-sig"), directory.name)
        preview = previews / directory.name
        preview.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(directory / "SKILL.md", preview / "SKILL.md")
        interface_file = directory / "agents/openai.yaml"
        interface = (yaml.safe_load(interface_file.read_text(encoding="utf-8")) or {}).get("interface", {}) if interface_file.is_file() else {}
        entry = {
            "name": directory.name,
            "title": interface.get("display_name") or metadata["name"],
            "description": metadata["description"],
            "shortDescription": interface.get("short_description") or "",
            "source": "openai",
            "path": f"skills/.curated/{directory.name}",
        }
        icon_name = interface.get("icon_large") or interface.get("icon_small")
        if icon_name:
            icon = (directory / icon_name).resolve()
            icon.relative_to(directory.resolve())
            digest = hashlib.sha256(icon.read_bytes()).hexdigest()[:20]
            target = icons / f"{digest}{icon.suffix.lower()}"
            shutil.copyfile(icon, target)
            entry["iconAsset"] = f"apps/skill-logos/{target.name}"
        license_file = next(path for path in directory.iterdir() if path.is_file() and path.name.lower().startswith("license"))
        digest = hashlib.sha256(license_file.read_bytes()).hexdigest()[:16]
        target = notices / f"LICENSE-{digest}.txt"
        shutil.copyfile(license_file, target)
        license_rows.append(f"- `{directory.name}`: `backend/skills/licenses/{target.name}`")
        entries.append(entry)
    payload = {"source": "https://github.com/openai/skills", "revision": revision, "skills": entries}
    (REPO / "backend/skills/public-catalog.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notice = ("# Public Skill catalog\n\n"
        f"The catalog metadata, preview instructions and declared icons come from `openai/skills` at `{revision}`. "
        "Each Skill retains its own license; the corresponding bundled notice is listed below. "
        "The catalog does not install or enable any Skill.\n\n" + "\n".join(license_rows) + "\n")
    (REPO / "backend/skills/PUBLIC_SKILLS_NOTICE.md").write_text(notice, encoding="utf-8")
    print(f"Generated {len(entries)} Skills from {revision}.")


if __name__ == "__main__":
    main()
