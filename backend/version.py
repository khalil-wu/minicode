from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


@lru_cache(maxsize=1)
def get_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if pyproject.is_file():
        with pyproject.open("rb") as stream:
            return tomllib.load(stream)["project"]["version"]
    try:
        return version("minicode")
    except PackageNotFoundError:
        pass
    return "0.0.0-dev"


__version__ = get_version()
