"""Load the OS credential backend when a credential operation needs it."""

from __future__ import annotations

import os
from types import ModuleType


def load_credential_backend() -> tuple[ModuleType, tuple[type[Exception], ...], type[Exception]]:
    # Importing keyring discovers platform support. Keep that work outside
    # configuration, tool catalog and empty credential-index reads.
    import keyring
    from keyring.errors import KeyringError, PasswordDeleteError

    errors: tuple[type[Exception], ...] = (KeyringError,)
    if os.name == "nt":
        import pywintypes

        errors += (pywintypes.error,)
    return keyring, errors, PasswordDeleteError
