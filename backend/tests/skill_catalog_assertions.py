"""Assert the Source absolute/aliased locator contract against the real file."""
from pathlib import Path
import re


def assert_skill_locator(catalog: str, name: str, expected: Path) -> None:
    roots = dict(re.findall(r"- `(r\d+)` = `([^`]+)`", catalog))
    locator = re.search(r"- " + re.escape(name) + r":.*\(file: ([^)]+)\)", catalog).group(1)
    prefix, separator, rest = locator.partition("/")
    resolved = roots[prefix] + separator + rest if prefix in roots else locator
    assert Path(resolved).resolve() == expected.resolve()
