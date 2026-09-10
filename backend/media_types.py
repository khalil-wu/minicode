"""Media types for files served or presented by MiniCode, independent of host apps."""
from __future__ import annotations

from mimetypes import MimeTypes
from pathlib import Path

# A private database uses Python's standard types without Windows registry
# overrides (which can map SVG to the unrenderable "image/svg"). These additions
# cover MiniCode's document, media, and web preview formats missing in Python 3.11.
_types = MimeTypes()
for extension, media_type in {
    ".7z": "application/x-7z-compressed",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".epub": "application/epub+zip",
    ".flac": "audio/flac",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".js": "text/javascript",
    ".jsx": "text/jsx",
    ".m4a": "audio/mp4",
    ".md": "text/markdown",
    ".mdx": "text/markdown",
    ".mjs": "text/javascript",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ogg": "audio/ogg",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "application/rtf",
    ".ts": "text/typescript",
    ".tsx": "text/tsx",
    ".webm": "video/webm",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}.items():
    _types.add_type(media_type, extension)


def media_type_for_path(path: str | Path) -> str:
    # Work with a literal basename: URL query/fragment parsing must not change
    # filenames such as "logo#1.svg" or "report%20.pdf".
    extension = Path(path).suffix.lower()
    return _types.guess_type(f"file{extension}")[0] or "application/octet-stream"
