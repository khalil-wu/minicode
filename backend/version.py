from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re


@lru_cache(maxsize=1)
def get_version() -> str:
    try:
        return version("minicode")
    except PackageNotFoundError:
        pass
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            return match.group(1)
    except OSError:
        pass
    return "0.0.0-dev"


__version__ = get_version()
