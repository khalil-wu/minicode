from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from backend.skills.marketplace import install_marketplace_skill, remove_user_skill


def test_marketplace_install_is_create_only_under_concurrency(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    content = "---\nname: review\ndescription: Review code.\n---\nUse the review workflow.\n"
    barrier = threading.Barrier(6)
    successes: list[dict] = []
    failures: list[type[BaseException]] = []
    result_lock = threading.Lock()

    async def install() -> dict:
        return await install_marketplace_skill(
            "review",
            skills_dir,
            fetch_text=lambda _url: content,
        )

    def worker() -> None:
        barrier.wait()
        try:
            result = asyncio.run(install())
        except BaseException as exc:  # capture all worker outcomes for assertion
            with result_lock:
                failures.append(type(exc))
        else:
            with result_lock:
                successes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(successes) == 1
    assert failures == [FileExistsError] * 5
    assert (skills_dir / "review" / "SKILL.md").read_text(encoding="utf-8") == content


def test_marketplace_remove_rejects_symlinked_skill_directory(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    outside = tmp_path / "outside"
    skills_dir.mkdir()
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside", encoding="utf-8")
    try:
        (skills_dir / "review").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable in this environment: {exc}")

    with pytest.raises(FileNotFoundError):
        remove_user_skill("review", skills_dir)

    assert (outside / "SKILL.md").read_text(encoding="utf-8") == "outside"
