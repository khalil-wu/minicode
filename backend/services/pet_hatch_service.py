"""The local Hatch Pet bundle and MiniCode-owned pet publication."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from pathlib import Path

from backend.agent.markdown_scopes import get_minicode_config_home_dir
from backend.atomic_io import atomic_write_text
from backend.config import PROJECT_ROOT, SETTINGS_FILE, _update_settings_json, get_image_generation_settings, load_config_layer_stack
from backend.hooks.runtime import raise_if_config_change_blocked, run_config_change_hook
from backend.runtime_env import sanitized_subprocess_env

BUNDLED_HATCH_DIR = PROJECT_ROOT / "bundled-skills" / "hatch-pet"


def hatch_runtime() -> dict:
    import PIL

    return {"python": sys.executable, "pillow_version": PIL.__version__,
            "skill_directory": str(get_minicode_config_home_dir() / "skills" / ".bundled" / "hatch-pet"),
            "pet_directory": str(get_minicode_config_home_dir() / "pets")}


async def prepare_hatch_pet(skill_manager, *, provider: str | None = None) -> dict:
    from backend.async_cleanup import to_thread_cancel_safe

    image = get_image_generation_settings(provider, settings_data=load_config_layer_stack(cwd=None).effective_config())
    if not image["enabled"]:
        raise ValueError(image.get("reason") or "An image channel must be configured before creating a pet.")
    target = Path(hatch_runtime()["skill_directory"])
    if not target.exists():
        await to_thread_cancel_safe(shutil.copytree, BUNDLED_HATCH_DIR, target)
    skill_path = (target / "SKILL.md").resolve()
    hook = await run_config_change_hook(source="user_settings", file_path=str(SETTINGS_FILE))
    raise_if_config_change_blocked(hook, source="user_settings", file_path=str(SETTINGS_FILE))

    def enable(settings):
        skills = settings.setdefault("skills", {})
        entries = skills.setdefault("config", [])
        entries[:] = [entry for entry in entries if entry.get("path") != str(skill_path)]
        entries.append({"path": str(skill_path), "enabled": True})

    _update_settings_json(enable)
    snapshot = skill_manager.snapshot(None, config_stack=load_config_layer_stack(cwd=None))
    skills = snapshot.list_all()
    selected = next((skill for skill in skills if Path(skill["path"]).resolve() == skill_path), None)
    if selected is None:
        raise ValueError("Hatch Pet is unavailable in the current local Skill configuration.")
    return {"skill": {"name": selected["name"], "path": selected["path"]}, "skills": skills,
            "prompt": "Use $hatch-pet to help me create a new pet.", "runtime": hatch_runtime()}


def publish_hatched_pet(files: dict[str, bytes]) -> dict:
    request = json.loads(files["pet_request.json"])
    pet_id = request["pet_id"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", pet_id):
        raise ValueError("Pet id must use lowercase letters, digits and hyphens.")
    atlas_bytes = files["final/spritesheet-extended.webp"]
    with TemporaryDirectory(prefix="minicode-pet-validate-") as temporary:
        atlas = Path(temporary) / "spritesheet.webp"
        atlas.write_bytes(atlas_bytes)
        validated = subprocess.run([sys.executable, str(BUNDLED_HATCH_DIR / "scripts" / "validate_atlas.py"),
            str(atlas), "--require-v2", "--chroma-key", request["chroma_key"]["hex"]],
            capture_output=True, text=True, encoding="utf-8", env=sanitized_subprocess_env())
    if validated.returncode:
        raise ValueError(validated.stdout or validated.stderr)
    review = json.loads(files["qa/review.json"])
    if review["errors"]:
        raise ValueError("Pet frame review still contains errors: " + "; ".join(review["errors"]))
    chroma = json.loads(files["qa/chroma-despill-extended.json"])
    if not chroma["ok"]:
        raise ValueError("Pet chroma cleanup has not passed.")
    semantics = json.loads(files["qa/direction-semantics.json"])
    directions = semantics["directions"]
    expected_labels = {"000", "022.5", "045", "067.5", "090", "112.5", "135", "157.5", "180", "202.5", "225", "247.5", "270", "292.5", "315", "337.5"}
    if len(directions) != 16 or {direction["label"] for direction in directions} != expected_labels \
            or any(direction["verdict"] not in {"pass", "warning"} for direction in directions):
        raise ValueError("All 16 pet look directions require a passing or reviewed-warning visual verdict.")
    blind = json.loads(files["qa/direction-blind-validation.json"])
    if not blind["ok"]:
        resolution = json.loads(files["qa/blind-review-resolution.json"])
        if resolution["severity"] != "minor" or resolution["decision"] != "accept":
            raise ValueError("Pet blind direction review has not passed.")
    destination = get_minicode_config_home_dir() / "pets" / pet_id
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "spritesheet.webp").write_bytes(atlas_bytes)
    manifest = {"id": pet_id, "displayName": request["display_name"], "description": request["description"],
                "spriteVersionNumber": 2, "spritesheetPath": "spritesheet.webp"}
    atomic_write_text(destination / "pet.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {"pet_id": f"custom:{pet_id}", "path": str(destination), "manifest": manifest}
