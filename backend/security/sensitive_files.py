from __future__ import annotations

from pathlib import Path


SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.development",
    ".env.production",
    ".env.test",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
}
SENSITIVE_FILE_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
SENSITIVE_PATH_PARTS = {".ssh", ".aws", ".azure", ".gnupg"}

PROTECTED_WRITE_FILE_NAMES = {
    ".gitconfig",
    ".gitmodules",
    ".mcp.json",
    ".claude.json",
    ".codex.json",
    "settings.json",
    "settings.local.json",
}
PROTECTED_WRITE_PATH_PARTS = {".git", ".claude", ".codex"}


def is_sensitive_file(path: Path) -> bool:
    name = path.name.lower()
    if name in SENSITIVE_FILE_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in SENSITIVE_FILE_SUFFIXES):
        return True
    return any(part.lower() in SENSITIVE_PATH_PARTS for part in path.parts)


def is_protected_write_path(path: Path) -> bool:
    name = path.name.lower()
    if name in PROTECTED_WRITE_FILE_NAMES:
        return True
    return any(part.lower() in PROTECTED_WRITE_PATH_PARTS for part in path.parts)
