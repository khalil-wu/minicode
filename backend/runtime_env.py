"""Environment helpers for subprocess boundaries."""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

# MiniCode treats host-owned launch context as non-inheritable regardless of the
# user's shell-environment policy. MiniCode's two desktop bearer tokens are the
# same kind of runtime-owned capability and therefore share this boundary.
_NON_INHERITABLE_ENV_NAMES = frozenset(
    {
        "OPENAI_FEDERATION_RULE_ID",
        "OPENAI_IDENTITY_TOKEN_FILE",
        "MINICODE_RUNTIME_TOKEN",
        "MINICODE_EMBEDDED_BROWSER_TOKEN",
    }
)

_SENSITIVE_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
    }
)

_SENSITIVE_ENV_SUFFIXES = (
    "_API_KEY",
    "_AUTH_TOKEN",
    "_ACCESS_TOKEN",
    "_TOKEN",
    "_PASSWORD",
    "_PASSWD",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_SECRET",
    "_SECRET_KEY",
)

_UNIX_CORE_ENV_NAMES = frozenset(
    {
        "PATH",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOGNAME",
        "USER",
    }
)

_WINDOWS_CORE_ENV_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SHELL",
        "COMSPEC",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "USERNAME",
        "USERDOMAIN",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "PROGRAMDATA",
        "LOCALAPPDATA",
        "APPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "POWERSHELL",
        "PWSH",
    }
)

# MiniCode only enables this scrub set when its GitHub Actions wrapper marks
# the run as exposed to untrusted workflow content. GH_TOKEN/GITHUB_TOKEN are
# deliberately not included upstream because user tooling still needs them.
_MINICODE_GHA_SUBPROCESS_SCRUB = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "MINICODE_OAUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_CLIENT_SECRET",
        "AZURE_CLIENT_CERTIFICATE_PATH",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
        "ACTIONS_RUNTIME_TOKEN",
        "ACTIONS_RUNTIME_URL",
        "ALL_INPUTS",
        "OVERRIDE_GITHUB_TOKEN",
        "DEFAULT_WORKFLOW_TOKEN",
        "SSH_SIGNING_KEY",
    }
)

# Environment names that can change interpreter loading, shell startup, path
# resolution, or Git/SSH configuration.  They are never accepted as explicit
# user/model overrides.  A small inherited subset (PATH/PATHEXT/COMSPEC) is
# retained because removing it would make ordinary subprocess lookup fail;
# loader/startup/configuration variables are stripped at every subprocess
# boundary.
_UNSAFE_ENV_EXACT = {
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "BASH_ENV",
    "ENV",
    "IFS",
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "NODE_OPTIONS",
    "NODE_PATH",
    "GIT_SSH_COMMAND",
    "GIT_SSH",
    "SSH_ASKPASS",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "PROMPT_COMMAND",
    "PS4",
    "RUBYOPT",
    "PERL5OPT",
}
_UNSAFE_ENV_PREFIXES = ("LD_", "DYLD_", "GIT_CONFIG_")


class ShellEnvironmentPolicyError(ValueError):
    """Raised when a MiniCode shell-environment policy is malformed."""


@dataclass(frozen=True)
class ShellEnvironmentPolicy:
    """MiniCode environment derivation for model-reachable shells.

    The ordering follows industry best practices for shell environment setup,
    ``shell_environment::populate_env`` implementation: inherit, default
    excludes, custom excludes, configured values, then include-only filters.
    """

    inherit: str = "all"
    ignore_default_excludes: bool = True
    exclude: tuple[str, ...] = ()
    set_values: Mapping[str, str] = field(default_factory=dict)
    include_only: tuple[str, ...] = ()
    use_profile: bool = False

    def __post_init__(self) -> None:
        inherit = str(self.inherit or "all").strip().lower()
        if inherit not in {"all", "core", "none"}:
            raise ShellEnvironmentPolicyError(
                "shell_environment_policy.inherit must be all, core, or none"
            )
        object.__setattr__(self, "inherit", inherit)
        object.__setattr__(self, "exclude", tuple(str(item) for item in self.exclude))
        object.__setattr__(
            self,
            "include_only",
            tuple(str(item) for item in self.include_only),
        )
        object.__setattr__(
            self,
            "set_values",
            _validated_env_mapping(self.set_values, reject_process_control=False),
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | "ShellEnvironmentPolicy" | None,
    ) -> "ShellEnvironmentPolicy":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ShellEnvironmentPolicyError(
                "shell_environment_policy must be a table/object"
            )

        allowed_fields = {
            "inherit",
            "ignore_default_excludes",
            "exclude",
            "set",
            "include_only",
            "filters",
            "experimental_use_profile",
        }
        unknown = sorted(str(key) for key in value if str(key) not in allowed_fields)
        if unknown:
            raise ShellEnvironmentPolicyError(
                "unknown shell_environment_policy field(s): " + ", ".join(unknown)
            )

        inherit = value.get("inherit", "all")
        if not isinstance(inherit, str):
            raise ShellEnvironmentPolicyError(
                "shell_environment_policy.inherit must be a string"
            )
        ignore_default_excludes = value.get("ignore_default_excludes", True)
        if not isinstance(ignore_default_excludes, bool):
            raise ShellEnvironmentPolicyError(
                "shell_environment_policy.ignore_default_excludes must be a boolean"
            )
        use_profile = value.get("experimental_use_profile", False)
        if not isinstance(use_profile, bool):
            raise ShellEnvironmentPolicyError(
                "shell_environment_policy.experimental_use_profile must be a boolean"
            )

        filters = value.get("filters")
        if filters is not None and (
            "exclude" in value or "include_only" in value
        ):
            raise ShellEnvironmentPolicyError(
                "cannot mix shell_environment_policy.filters with exclude or include_only"
            )
        if filters is not None:
            if not isinstance(filters, Mapping):
                raise ShellEnvironmentPolicyError(
                    "shell_environment_policy.filters must be a table/object"
                )
            seen_patterns: set[str] = set()
            exclude_values: list[str] = []
            include_values: list[str] = []
            for raw_pattern, raw_action in filters.items():
                if not isinstance(raw_pattern, str) or not isinstance(raw_action, str):
                    raise ShellEnvironmentPolicyError(
                        "shell_environment_policy.filters must map string patterns to include or exclude"
                    )
                folded = raw_pattern.casefold()
                if folded in seen_patterns:
                    raise ShellEnvironmentPolicyError(
                        f"duplicate shell environment filter {raw_pattern!r} ignoring case"
                    )
                seen_patterns.add(folded)
                action = raw_action.strip().lower()
                if action == "exclude":
                    exclude_values.append(raw_pattern)
                elif action == "include":
                    include_values.append(raw_pattern)
                else:
                    raise ShellEnvironmentPolicyError(
                        f"shell environment filter {raw_pattern!r} must be include or exclude"
                    )
        else:
            exclude_values = list(
                _string_sequence(value.get("exclude"), "shell_environment_policy.exclude")
            )
            include_values = list(
                _string_sequence(
                    value.get("include_only"),
                    "shell_environment_policy.include_only",
                )
            )

        raw_set = value.get("set", {})
        if not isinstance(raw_set, Mapping):
            raise ShellEnvironmentPolicyError(
                "shell_environment_policy.set must be a table/object of string values"
            )
        set_values = _validated_env_mapping(
            raw_set,
            reject_process_control=False,
        )
        return cls(
            inherit=inherit,
            ignore_default_excludes=ignore_default_excludes,
            exclude=tuple(exclude_values),
            set_values=set_values,
            include_only=tuple(include_values),
            use_profile=use_profile,
        )


def shell_subprocess_env(
    policy: Mapping[str, Any] | ShellEnvironmentPolicy | None = None,
    extra: Mapping[str, str] | None = None,
    *,
    allow_process_control_overrides: bool = False,
) -> dict[str, str]:
    """Build a MiniCode shell environment and apply launch overrides."""

    resolved = ShellEnvironmentPolicy.from_mapping(policy)
    inherited = list(os.environ.items())
    if resolved.inherit == "none":
        env: dict[str, str] = {}
    elif resolved.inherit == "core":
        core = _WINDOWS_CORE_ENV_NAMES if os.name == "nt" else _UNIX_CORE_ENV_NAMES
        env = {key: value for key, value in inherited if key.upper() in core}
    else:
        env = dict(inherited)

    if not resolved.ignore_default_excludes:
        env = {
            key: value
            for key, value in env.items()
            if not _matches_any_env_pattern(key, ("*KEY*", "*SECRET*", "*TOKEN*"))
        }
    if resolved.exclude:
        env = {
            key: value
            for key, value in env.items()
            if not _matches_any_env_pattern(key, resolved.exclude)
        }
    env.update(resolved.set_values)
    if resolved.include_only:
        env = {
            key: value
            for key, value in env.items()
            if _matches_any_env_pattern(key, resolved.include_only)
        }

    if extra:
        env.update(
            _validated_env_mapping(
                extra,
                reject_process_control=not allow_process_control_overrides,
            )
        )
    _scrub_non_inheritable_env(env)
    _apply_minicode_actions_scrub(env)
    if os.name == "nt" and not _contains_env_name(env, "PATHEXT"):
        env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    return env


def mcp_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    inherited_names: tuple[str, ...] = (),
) -> dict[str, str]:
    """Build the local MiniCode MCP stdio environment.

    Local MCP servers inherit only the platform core plus explicitly named
    variables and literal config overrides. This is deliberately separate from
    the model shell policy and from MiniCode's full-inheritance hook boundary.
    """

    core = _WINDOWS_CORE_ENV_NAMES if os.name == "nt" else frozenset(
        {
            "HOME",
            "LOGNAME",
            "PATH",
            "SHELL",
            "USER",
            "__CF_USER_TEXT_ENCODING",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
            "TZ",
        }
    )
    requested = {str(name).upper() for name in inherited_names if str(name)}
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in core or key.upper() in requested
    }
    if extra:
        env.update(_validated_env_mapping(extra, reject_process_control=False))
    _scrub_non_inheritable_env(env)
    _apply_minicode_actions_scrub(env)
    if os.name == "nt" and not _contains_env_name(env, "PATHEXT"):
        env["PATHEXT"] = ".COM;.EXE;.BAT;.CMD"
    return env


def sanitized_subprocess_env(
    extra: Mapping[str, str] | None = None,
    *,
    allow: set[str] | None = None,
) -> dict[str, str]:
    """Return an inherited environment without MiniCode/provider secrets.

    This is the host-subprocess boundary used by hooks, terminals, git, LSP,
    and helper commands. Model shell execution has its separate, configurable
    MiniCode ``shell_subprocess_env`` policy; this facade preserves
    MiniCode's stricter historical contract for ambient provider credentials.
    Explicit, validated ``extra`` values remain opt-in and are applied after
    inherited secrets are removed.
    """

    allowed = {str(name).upper() for name in (allow or set())}
    env = {
        key: value
        for key, value in shell_subprocess_env().items()
        if key.upper() in allowed or not _is_sensitive_env_name(key)
    }
    if extra:
        env.update(_validated_env_mapping(extra, reject_process_control=True))
    _scrub_non_inheritable_env(env)
    return env


def sanitize_env_overrides(extra: Mapping[str, str] | None) -> dict[str, str]:
    """Drop invalid or process-injection environment overrides.

    Configuration, preview launchers, terminal sessions, MCP children, and
    tool calls all eventually pass through this helper.  Invalid values are
    ignored rather than merged into a child environment; command-tool input
    validation still returns a user-facing error before invoking it.
    """

    return _validated_env_mapping(extra, reject_process_control=True, drop_invalid=True)


def sanitized_git_env(cwd: str | Path | None = None) -> dict[str, str]:
    """Return a sanitized environment for git subprocesses.

    Drops inherited GIT_DIR/GIT_WORK_TREE so git resolves the repo from cwd.
    Repo discovery may ascend (a workspace that is a subdirectory of a repo
    must find its enclosing repository) but stops at the user's home directory
    so a stray ~/.git never masquerades as the workspace repo.
    """
    env = sanitized_subprocess_env()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    try:
        env["GIT_CEILING_DIRECTORIES"] = str(Path.home())
    except OSError:
        pass
    return env


def is_unsafe_env_override_name(name: str) -> bool:
    normalized = str(name or "").upper()
    return normalized in _UNSAFE_ENV_EXACT or any(
        normalized.startswith(prefix) for prefix in _UNSAFE_ENV_PREFIXES
    )


def _is_sensitive_env_name(name: str) -> bool:
    normalized = str(name or "").upper()
    return normalized in _SENSITIVE_ENV_NAMES or normalized.endswith(
        _SENSITIVE_ENV_SUFFIXES
    )


def _valid_env_name(name: str) -> bool:
    if not name or len(name) > 128:
        return False
    if not ("A" <= name[0] <= "Z" or "a" <= name[0] <= "z" or name[0] == "_"):
        return False
    return all(
        "A" <= char <= "Z"
        or "a" <= char <= "z"
        or "0" <= char <= "9"
        or char == "_"
        for char in name
    )


def _string_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) for item in value
    ):
        raise ShellEnvironmentPolicyError(f"{field_name} must be an array of strings")
    return tuple(value)


def _validated_env_mapping(
    extra: Mapping[str, Any] | None,
    *,
    reject_process_control: bool,
    drop_invalid: bool = False,
) -> dict[str, str]:
    if not extra:
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_value in extra.items():
        key = str(raw_key or "")
        invalid = not _valid_env_name(key) or (
            reject_process_control and is_unsafe_env_override_name(key)
        )
        if not isinstance(raw_value, str):
            invalid = True
            value = ""
        else:
            value = raw_value
            invalid = invalid or "\x00" in value or len(value) > 32_768
        if invalid:
            if drop_invalid:
                continue
            raise ShellEnvironmentPolicyError(
                f"Invalid environment variable override: {key[:128] or '<empty>'}"
            )
        result[key] = value
    return result


def _matches_any_env_pattern(name: str, patterns: tuple[str, ...]) -> bool:
    folded = name.casefold()
    return any(fnmatchcase(folded, pattern.casefold()) for pattern in patterns)


def _contains_env_name(env: Mapping[str, str], name: str) -> bool:
    return any(key.casefold() == name.casefold() for key in env)


def _scrub_non_inheritable_env(env: dict[str, str]) -> None:
    for key in tuple(env):
        if key.upper() in _NON_INHERITABLE_ENV_NAMES:
            env.pop(key, None)


def _env_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _apply_minicode_actions_scrub(env: dict[str, str]) -> None:
    if not _env_truthy(os.environ.get("MINICODE_SUBPROCESS_ENV_SCRUB")):
        return
    blocked = {
        *(_MINICODE_GHA_SUBPROCESS_SCRUB),
        *(f"INPUT_{name}" for name in _MINICODE_GHA_SUBPROCESS_SCRUB),
    }
    for key in tuple(env):
        if key.upper() in blocked:
            env.pop(key, None)


_console_utf8_configured = False


def ensure_utf8_console_logging(level: int = logging.INFO) -> None:
    """Force UTF-8 stdout/stderr and a UTF-8 log handler. Idempotent.

    On Windows the default console codepage (e.g. cp936) garbles the Chinese log
    messages this codebase emits, even though the underlying strings are valid
    UTF-8. Reconfiguring the streams to UTF-8 fixes the display without touching
    any data. Safe to call once at process startup.
    """
    global _console_utf8_configured
    if _console_utf8_configured:
        return
    _console_utf8_configured = True

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    root = logging.getLogger()
    # Only install our own handler when nothing else has — otherwise we would
    # double-log alongside a pre-existing handler (e.g. rich/uvicorn). The
    # stream reconfigure above is what actually fixes mojibake; an existing
    # handler writing to the now-UTF-8 stream renders Chinese correctly too.
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
