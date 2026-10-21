"""Collect the notices shipped by MiniCode's actual installed dependencies."""
from __future__ import annotations

from email.parser import Parser
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess


NON_NOTICE_SUFFIXES = {".py", ".pyc", ".pyo", ".js", ".cjs", ".mjs"}


class LicenseHtmlText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignored -= 1
        if tag in {"p", "div", "pre", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def notice_file(path: Path) -> bool:
    return path.suffix.casefold() not in NON_NOTICE_SUFFIXES and path.name.casefold().startswith(
        ("license", "licence", "notice", "copying", "copyright", "third_party_notice", "thirdpartynotice")
    )


def notice_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig").strip()
    if path.suffix.casefold() == ".html":
        parser = LicenseHtmlText()
        parser.feed(text)
        text = "".join(parser.parts).strip()
        if path.name == "LICENSES.chromium.html":
            # Electron's bundled Chromium notices contain three damaged canonical
            # lines. Restore their original punctuation from SPDX MPL-1.1,
            # NPL-1.1 and FTL; remove when the upstream notice fixes these lines.
            # https://github.com/spdx/license-list-data/tree/main/text
            for damaged, original in (
                ('the \ufffd[___] License\ufffd', 'the "[___] License"'),
                ('\ufffdMultiple-Licensed\ufffd.  \ufffdMultiple-Licensed\ufffd', '"Multiple-Licensed".  "Multiple-Licensed"'),
                ("Portions of this software are copyright \ufffd <year> The FreeType", "Portions of this software are copyright © <year> The FreeType"),
            ):
                text = text.replace(damaged, original)
    return text


def package_notices(directory: Path) -> list[Path]:
    files = [file for file in directory.iterdir() if file.is_file() and notice_file(file)]
    for folder in (directory / "licenses", directory / "LICENSES"):
        if folder.is_dir():
            files.extend(file for file in folder.rglob("*") if file.is_file()
                and "__pycache__" not in file.parts and file.suffix.casefold() not in NON_NOTICE_SUFFIXES)
    return sorted(set(files), key=lambda file: str(file).casefold())


def node_notices(project: Path):
    lock_path = project / "node_modules" / ".package-lock.json"
    if not lock_path.exists():
        return
    lock = json.loads(lock_path.read_text(encoding="utf8"))
    for relative, entry in sorted(lock["packages"].items()):
        if entry.get("dev") or entry.get("link"):
            continue
        directory = project / relative
        # npm's installed lock also lists optional binaries for other hosts;
        # those directories are absent and are not distributed in this app.
        if entry.get("optional") and not directory.is_dir():
            continue
        metadata = json.loads((directory / "package.json").read_text(encoding="utf8"))
        name = f"{metadata['name']} {metadata['version']}"
        license_value = metadata.get("license", entry.get("license", ""))
        if isinstance(license_value, dict):
            license_value = license_value.get("type", "")
        files = package_notices(directory)
        yield name, f"License: {license_value}\n" + "\n\n".join(notice_text(file) for file in files)


def collect_notices(repository: Path) -> tuple[str, int]:
    sections = []
    for project in (repository / "frontend", repository / "desktop", repository / "desktop/language-services"):
        sections.extend(node_notices(project))
    runtime = repository / "desktop/python-runtime"
    if runtime.is_dir():
        sections.append(("Python runtime", notice_text(runtime / "LICENSE.txt")))
        for directory in sorted((runtime / "Lib/site-packages").glob("*.dist-info")):
            metadata = Parser().parsestr((directory / "METADATA").read_text(encoding="utf8"))
            title = f"{metadata['Name']} {metadata['Version']}"
            license_value = metadata.get("License-Expression") or metadata.get("License") or ""
            sections.append((title, f"License: {license_value}\n" + "\n\n".join(notice_text(file) for file in package_notices(directory))))
    electron = repository / "desktop/node_modules/electron/dist"
    for name in ("LICENSE", "LICENSES.chromium.html"):
        file = electron / name
        if file.is_file():
            sections.append((f"Electron / Chromium — {name}", notice_text(file)))
    tracked = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=repository, text=True, encoding="utf8")
    for relative in sorted(set(tracked.splitlines())):
        file = Path(relative)
        if any(part in {"tests", "output", ".tmp", "node_modules", "python-runtime"} for part in file.parts) or not notice_file(file):
            continue
        if file.name == "THIRD_PARTY_NOTICES.txt" and file.parent == Path("desktop"):
            continue
        sections.append((f"Bundled resource — {relative}", notice_text(repository / file)))
    seen = {}
    output = ["MiniCode — Open Source Licenses and Third-Party Notices",
        "Collected from the dependency versions and resource notices included in this checkout.\n"]
    for title, text in sections:
        text = text.strip()
        output.extend(["=" * 78, title, "=" * 78])
        if text in seen:
            output.append(f"License text: see the complete notice under {seen[text]}.\n")
        else:
            seen[text] = title
            output.append(text + "\n")
    return "\n".join(output), len(sections)


if __name__ == "__main__":
    repository = Path(__file__).resolve().parents[2]
    text, count = collect_notices(repository)
    destination = repository / "desktop/THIRD_PARTY_NOTICES.txt"
    destination.write_text(text, encoding="utf8", newline="\n")
    print(f"Wrote {count} dependency/resource notices to {destination}")
