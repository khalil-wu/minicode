from __future__ import annotations

import subprocess
import os
import runpy
from pathlib import Path

import pytest


MOJIBAKE_MARKER_CODEPOINTS = (0xFFFD, 0x9225, 0x93C9, 0x935B, 0x7EDB, 0x6A9A, 0x6A9D, 0x8133)
MOJIBAKE_MARKERS = tuple(chr(codepoint) for codepoint in MOJIBAKE_MARKER_CODEPOINTS)
SOURCE_PREFIXES = ("backend/", "frontend/src.v2/", "desktop/")
# Compiled PE resources are binary data, independently of their source text.
EXCLUDED_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pyc", ".pyo", ".exe")
EXCLUDED_DIR_NAMES = {"__pycache__", "node_modules", "dist", "build", "python-runtime", "release"}
TEXT_SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".cjs", ".mjs", ".json", ".html", ".css", ".md", ".txt"}

# High-risk files that contain user-visible text or prompts
HIGH_RISK_PATTERNS = (
    "backend/tools/",     # Tool descriptions, prompts, display names
    "backend/agent/",     # System prompts, agent instructions
    "backend/commands/",  # Command help text, slash command descriptions
    "backend/skills/",    # Skill prompts and descriptions
    "backend/api/",       # REST payloads and diagnostics text
    "frontend/src.v2/",   # UI text, labels, placeholders
    "desktop/",           # Desktop shell product text
)
REQUIRED_HIGH_RISK_PATTERNS = (
    "backend/tools",
    "backend/agent",
    "backend/commands",
    "backend/skills",
    "backend/api",
    "frontend/src.v2",
)


def test_tracked_source_has_no_common_mojibake_markers() -> None:
    """
    Audit all tracked source files for mojibake markers.

    This extended version covers:
    - Backend Python code (existing)
    - Frontend TypeScript/TSX (existing)
    - Tool descriptions and prompts
    - Agent system prompts
    - Command help text
    - UI strings and labels
    """
    tracked = subprocess.check_output(["git", "ls-files"], text=True, encoding="utf-8")
    offenders: list[str] = []

    for raw_path in tracked.splitlines():
        path = raw_path.replace("\\", "/")
        if not path.startswith(SOURCE_PREFIXES):
            continue
        if path.startswith("frontend/dist/") or path.endswith(".tsbuildinfo"):
            continue
        if path.lower().endswith(EXCLUDED_SUFFIXES):
            continue

        source_path = Path(path)
        if not source_path.exists():
            continue
        content = source_path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in content for marker in MOJIBAKE_MARKERS):
            offenders.append(path)

    assert offenders == []


def test_high_risk_files_have_no_mojibake() -> None:
    """
    Extra audit for high-risk files that contain user-visible text.

    These files are more likely to have encoding issues from copy-paste
    or manual edits, so we scan them even if they're not tracked.
    """
    offenders: list[str] = []

    for pattern in HIGH_RISK_PATTERNS:
        base = Path(pattern)
        if not base.exists():
            continue

        for directory, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in EXCLUDED_DIR_NAMES and not name.startswith("release")
            ]
            for filename in filenames:
                source_path = Path(directory) / filename
                if source_path.suffix.lower() in EXCLUDED_SUFFIXES:
                    continue
                if source_path.suffix.lower() not in TEXT_SOURCE_SUFFIXES:
                    continue

                try:
                    content = source_path.read_text(encoding="utf-8", errors="ignore")
                    if any(marker in content for marker in MOJIBAKE_MARKERS):
                        offenders.append(str(source_path).replace("\\", "/"))
                except Exception:
                    # Skip unreadable files
                    pass

    assert offenders == []


def test_high_risk_mojibake_audit_covers_agent_product_surfaces() -> None:
    configured = {pattern.rstrip("/") for pattern in HIGH_RISK_PATTERNS}
    missing = sorted(set(REQUIRED_HIGH_RISK_PATTERNS) - configured)
    nonexistent = [
        pattern
        for pattern in REQUIRED_HIGH_RISK_PATTERNS
        if not Path(pattern).exists()
    ]

    assert missing == []
    assert nonexistent == []


def test_license_collection_keeps_documents_and_excludes_executable_helpers(tmp_path: Path) -> None:
    collector = runpy.run_path("desktop/scripts/collect-third-party-notices.py")
    licenses = tmp_path / "licenses"
    cache = licenses / "__pycache__"
    cache.mkdir(parents=True)
    primary = tmp_path / "LICENSE"
    apache = licenses / "LICENSE.APACHE"
    authors = licenses / "AUTHORS.txt"
    for document in (primary, apache, authors):
        document.write_text("Complete original license text", encoding="utf-8")
    (tmp_path / "license-update.mjs").write_text("throw new Error('helper');", encoding="utf-8")
    (licenses / "AUTHORS.py").write_text("print('contributors')", encoding="utf-8")
    (cache / "AUTHORS.cpython-311.pyc").write_bytes(b"\xa7\r\r\n\x00\xff")

    assert set(collector["package_notices"](tmp_path)) == {primary, apache, authors}


def test_license_text_preserves_utf8_and_rejects_lossy_decoding(tmp_path: Path) -> None:
    collector = runpy.run_path("desktop/scripts/collect-third-party-notices.py")
    document = tmp_path / "LICENSE.txt"
    original = "Copyright © 2026 — 中文许可 · café\nAll rights reserved."
    document.write_text(original, encoding="utf-8-sig")
    assert collector["notice_text"](document) == original

    document.write_bytes(b"Copyright \xff 2026")
    with pytest.raises(UnicodeDecodeError):
        collector["notice_text"](document)


def test_chromium_notice_restores_verified_canonical_license_punctuation(tmp_path: Path) -> None:
    collector = runpy.run_path("desktop/scripts/collect-third-party-notices.py")
    document = tmp_path / "LICENSES.chromium.html"
    document.write_text(
        '<style>hidden styles</style><pre>Original © 中文 — café\n'
        'terms of the _____ license (the \ufffd[___] License\ufffd), in which case\n'
        '\ufffdMultiple-Licensed\ufffd.  \ufffdMultiple-Licensed\ufffd means that the Initial\n'
        'Portions of this software are copyright \ufffd &lt;year&gt; The FreeType\n'
        'All rights reserved.</pre>',
        encoding="utf-8",
    )

    assert collector["notice_text"](document) == (
        'Original © 中文 — café\n'
        'terms of the _____ license (the "[___] License"), in which case\n'
        '"Multiple-Licensed".  "Multiple-Licensed" means that the Initial\n'
        'Portions of this software are copyright © <year> The FreeType\n'
        'All rights reserved.'
    )
