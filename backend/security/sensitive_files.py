from __future__ import annotations

from pathlib import Path


SENSITIVE_FILE_NAMES = {
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
# Dotenv files are conventionally suffixed per environment (.env.staging,
# .env.ci, .env.local.bak). Enumerating known variants leaks every name nobody
# thought of, so match the family by prefix instead.
SENSITIVE_FILE_PREFIXES = {".env"}
SENSITIVE_FILE_EXACT_EXCEPTIONS = {
    ".env.dist",
    ".env.example",
    ".env.sample",
    ".env.template",
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
    if name in SENSITIVE_FILE_EXACT_EXCEPTIONS:
        return False
    if name in SENSITIVE_FILE_NAMES:
        return True
    if any(name == prefix or name.startswith(f"{prefix}.") for prefix in SENSITIVE_FILE_PREFIXES):
        return True
    if any(name.endswith(suffix) for suffix in SENSITIVE_FILE_SUFFIXES):
        return True
    return any(part.lower() in SENSITIVE_PATH_PARTS for part in path.parts)


def is_protected_write_path(path: Path) -> bool:
    name = path.name.lower()
    if name in PROTECTED_WRITE_FILE_NAMES:
        return True
    return any(part.lower() in PROTECTED_WRITE_PATH_PARTS for part in path.parts)
