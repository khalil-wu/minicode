"""Create independent task directories following Codex desktop 26.1007.2314.0."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import re
import stat
from uuid import uuid4


@dataclass(frozen=True)
class ProjectlessDirectory:
    cwd: str
    output_directory: str
    browser_root: str


def projectless_developer_instructions(context: dict[str, str]) -> str:
    """Codex desktop src-BJv5Mk7n.js::Ee additional developer instructions."""
    output = context["output_directory"]
    return "\n".join([
        "### Projectless Chat",
        f"This projectless thread starts in a generated directory under {context['browser_root']}.",
        "The generated directory name is only a filesystem identifier. Do not infer the user's language, locale, or preferences from its name or path, even if it resembles a language code such as 'ru'.",
        "Prefer answering inline in chat unless using local files would make the result more useful.",
        f"Use work/ for intermediate files, scratch analysis, scripts, drafts, and temporary assets. Use {output} only for user-facing deliverables that should appear as outputs.",
        f"When referring to saved deliverables in the final response, link only files from {output}.",
        "Do not write directly in the home directory unless the user explicitly asks.",
    ])


def create_projectless_directory(base: str, prompt: str, *, date: datetime | None = None) -> ProjectlessDirectory:
    requested = Path(base).expanduser()
    if not requested.is_absolute():
        raise ValueError("Projectless chat folder must be absolute on the execution host.")
    directory = requested.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    day = directory / (date or datetime.now()).strftime("%Y-%m-%d")
    day.mkdir(exist_ok=True)
    metadata = day.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or (
        os.name == "nt" and metadata.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    ):
        raise ValueError("Projectless date directory must be a real directory.")
    words = re.findall(r"[a-z0-9]+", prompt.lower())
    slug = "-".join(words[:6])[:80] if words else "new-chat"
    for index in range(105):
        name = slug if index == 0 else f"{slug}-{index + 1}" if index < 100 else f"{slug}-{uuid4()}"
        task_directory = day / name
        try:
            task_directory.mkdir()
        except FileExistsError:
            continue
        outputs = task_directory / "outputs"
        outputs.mkdir()
        (task_directory / "work").mkdir()
        from backend.workspace.trust import trust_created_task_directory

        trust_created_task_directory(task_directory)
        return ProjectlessDirectory(str(task_directory), str(outputs), str(directory))
    raise RuntimeError("Unable to create a unique projectless thread directory.")
