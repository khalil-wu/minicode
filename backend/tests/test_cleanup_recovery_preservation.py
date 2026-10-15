from __future__ import annotations

import shutil
import os
import subprocess
from pathlib import Path

import pytest


def test_cleanup_preserves_recovery_and_backup_files(tmp_path, record_property):
    if os.name == "nt":
        git_executable = shutil.which("git")
        bash = Path(git_executable).resolve().parent.parent / "bin/bash.exe" if git_executable else None
    else:
        bash = shutil.which("bash")
    record_property("cleanup_interpreter", str(bash) if bash is not None else "")
    if bash is None or (isinstance(bash, Path) and not bash.is_file()):
        pytest.skip("Git Bash/bash is not available")
    repository = Path(__file__).resolve().parents[2]
    shutil.copyfile(repository / "scripts/clean.sh", tmp_path / "clean.sh")
    recovery = ("draft.py.swp", "source.py.bak", "notes.txt~")
    for name in (*recovery, "source.py", "keep.log"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "generated.pyc").write_bytes(b"owned cache")
    result = subprocess.run([bash, "./clean.sh"], input="n\nn\n", cwd=tmp_path, text=True, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert all((tmp_path / name).read_text(encoding="utf-8") == name for name in recovery)
    assert (tmp_path / "source.py").exists() and (tmp_path / "keep.log").exists()
    assert not cache.exists()
