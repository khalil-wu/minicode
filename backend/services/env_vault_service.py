from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class EnvVaultServiceError(ValueError):
    """User-recoverable environment vault operation failure."""


@dataclass
class EnvVaultResult:
    entries: list[dict[str, str]]


def list_env_entries(vault: Any | None = None) -> EnvVaultResult:
    vault = vault or _new_vault()
    return EnvVaultResult(entries=list(vault.list_names()))


def set_env_entry(data: dict[str, Any], vault: Any | None = None) -> EnvVaultResult:
    vault = vault or _new_vault()
    name = _required_name(data.get("name"))
    value = data.get("value")
    if value is None:
        raise EnvVaultServiceError("Variable value is required")
    description = str(data.get("description", ""))
    scope = str(data.get("scope", "global"))
    vault.set(name, str(value), description=description, scope=scope)
    return list_env_entries(vault)


def delete_env_entry(data: dict[str, Any], vault: Any | None = None) -> EnvVaultResult:
    vault = vault or _new_vault()
    name = _required_name(data.get("name"))
    if not vault.delete(name):
        raise EnvVaultServiceError(f"Variable '{name}' not found")
    return list_env_entries(vault)


def _required_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise EnvVaultServiceError("Variable name is required")
    return name


def _new_vault() -> Any:
    from backend.vault import EnvVault

    return EnvVault()
