"""Model runtime definitions and validation helpers.

Extracted from ``backend/llm/model_runtime.py`` so provider registration
rules, canonical-field validation and the small attribute-mapping helpers are
independent of the ModelRuntime class that orchestrates them.
"""

from __future__ import annotations

from backend.config import STATE_ROOT
from backend.llm.model_selection import REASONING_LEVEL_ORDER
from backend.llm.provider_contracts import (
    ProviderRegistrationError,
    TokenNumber,
    UnsupportedProviderCapabilityError,
)
from collections.abc import (
    Mapping,
    Sequence,
)
from dataclasses import replace
from pathlib import Path
from typing import Any
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading


_SUPPORTED_APIS = {
    "anthropic-messages": "anthropic-messages",
    "openai-responses": "openai-responses",
    "openai-completions": "openai-completions",
}
_COMMAND_RESULT_CACHE: dict[str, str | None] = {}
_COMMAND_RESULT_LOCK = threading.RLock()
_MAX_CONFIG_COMMAND_OUTPUT_BYTES = 64 * 1024
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_PREFIX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEFAULT_CONTEXT_WINDOW = 128_000
DEFAULT_MAX_OUTPUT_TOKENS = 16_384
SUPPORTED_REASONING_LEVELS = REASONING_LEVEL_ORDER
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_EXTENSION_OVERRIDE_UNSET = object()
_DEFAULT_MODELS_CONFIG_FILE = STATE_ROOT / ".minicode" / "models.json"
_MAX_MODELS_CONFIG_BYTES = 4 * 1024 * 1024
_MODEL_COST_RATE_KEYS = ("input", "output", "cacheRead", "cacheWrite")
_MODEL_COST_TIER_KEYS = frozenset({"inputTokensAbove", *_MODEL_COST_RATE_KEYS})
class _AttributeMapping(dict[str, Any]):
    """Mapping that also mirrors JavaScript-style property access in tests/bridges."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _ProviderAuthContext(_AttributeMapping):
    """MiniCode provider auth context backed by the process environment."""

    def __init__(self, explicit_env: Mapping[str, Any] | None = None) -> None:
        self._explicit_env = {
            str(key): str(value)
            for key, value in (explicit_env or {}).items()
            if isinstance(key, str) and isinstance(value, str)
        }
        super().__init__(env=self.env, file_exists=self.file_exists)

    async def env(self, name: str) -> str | None:
        clean_name = str(name or "")
        if clean_name in self._explicit_env:
            explicit = self._explicit_env[clean_name]
            if explicit.strip():
                return explicit
        value = os.getenv(clean_name)
        if value is None:
            return None
        rendered = str(value)
        return rendered if rendered.strip() else None

    async def file_exists(self, path: str) -> bool:
        try:
            return Path(str(path or "")).expanduser().exists()
        except (OSError, RuntimeError, ValueError):
            return False

class _ProviderModelsStore(_AttributeMapping):
    """Provider-scoped view over MiniCode's persistent model catalog."""

    def __init__(self, backend: Any, provider_id: str) -> None:
        self._backend = backend
        self._provider_id = provider_id
        super().__init__(read=self.read, write=self.write, delete=self.delete)

    async def read(self) -> dict[str, Any] | None:
        read = getattr(self._backend, "read", None)
        if not callable(read):
            raise ProviderRegistrationError(
                "refresh_models store backend does not expose read"
            )
        entry = read(self._provider_id)
        if inspect.isawaitable(entry):
            entry = await entry
        return dict(entry) if isinstance(entry, Mapping) else None

    async def write(self, entry: Any) -> None:
        if not isinstance(entry, Mapping):
            raise ProviderRegistrationError(
                "refresh_models store.write requires an object"
            )
        write = getattr(self._backend, "write", None)
        if not callable(write):
            raise ProviderRegistrationError(
                "refresh_models store backend does not expose write"
            )
        result = write(self._provider_id, dict(entry))
        if inspect.isawaitable(result):
            await result

    async def delete(self) -> None:
        delete = getattr(self._backend, "delete", None)
        if not callable(delete):
            raise ProviderRegistrationError(
                "refresh_models store backend does not expose delete"
            )
        result = delete(self._provider_id)
        if inspect.isawaitable(result):
            await result


def _as_mapping(value: Any, *, description: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(value):
            result = asdict(value)
            if isinstance(result, dict):
                return result
    except (TypeError, ValueError):
        pass
    if hasattr(value, "__dict__"):
        return {
            str(key): item
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    raise ProviderRegistrationError(f"{description} must be an object")


_MODEL_KNOWN_KEYS = frozenset(
    {
        "provider",
        "id",
        "name",
        "api",
        "baseUrl",
        "base_url",
        "reasoning",
        "thinkingLevelMap",
        "thinking_level_map",
        "input",
        "cost",
        "contextWindow",
        "context_window",
        "maxContextWindow",
        "max_context_window",
        "maxTokens",
        "max_tokens",
        "headers",
    }
)


def _extension_model_extra(model: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in model.items()
        if str(key) not in _MODEL_KNOWN_KEYS
    }


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _reject_noncanonical_fields(
    value: Mapping[str, Any],
    aliases: Mapping[str, str],
    *,
    source: str,
) -> None:
    for alias, canonical in aliases.items():
        if alias in value:
            raise ProviderRegistrationError(
                f"{source}.{alias} is not supported; use {canonical}"
            )


def _merge_headers(
    base: Mapping[str, Any] | None,
    override: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Merge HTTP headers case-insensitively with the override winning."""

    merged: dict[str, str] = {
        str(key): str(value)
        for key, value in (base or {}).items()
    }
    for raw_name, raw_value in (override or {}).items():
        name = str(raw_name)
        folded = name.casefold()
        for existing in tuple(merged):
            if existing.casefold() == folded:
                merged.pop(existing, None)
        merged[name] = str(raw_value)
    return merged


def _provider_member(value: Any, *names: str) -> Any:
    """Read one provider member from a mapping or Python object."""

    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _merge_model_cost(
    base: Mapping[str, Any] | None,
    override: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply the exact Pi model override cost fields, including tier replacement."""

    merged = dict(base or {})
    if override is None:
        return merged
    for key in ("input", "output", "cacheRead", "cacheWrite", "tiers"):
        if key in override and override[key] is not None:
            merged[key] = override[key]
    return merged


def _strip_json_comments(value: str) -> str:
    """Strip Pi models.json comments/trailing commas without touching strings."""

    if value.startswith("\ufeff"):
        # JSON.parse accepts source text after the host strips a UTF-8 BOM.
        # Preserve character offsets for diagnostics by replacing it with
        # whitespace rather than shortening the input.
        value = " " + value[1:]
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(value):
        character = value[index]
        if in_string:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            output.append(character)
            index += 1
            continue
        if character == "/" and index + 1 < len(value):
            marker = value[index + 1]
            if marker == "/":
                output.extend("  ")
                index += 2
                while index < len(value) and value[index] not in "\r\n":
                    output.append(" ")
                    index += 1
                continue
            if marker == "*":
                output.extend("  ")
                index += 2
                terminated = False
                while index < len(value):
                    if index + 1 < len(value) and value[index : index + 2] == "*/":
                        output.extend("  ")
                        index += 2
                        terminated = True
                        break
                    output.append(value[index] if value[index] in "\r\n" else " ")
                    index += 1
                if not terminated:
                    raise ValueError("unterminated block comment in models.json")
                continue
        output.append(character)
        index += 1
    stripped = "".join(output)

    # Pi's models.json loader also accepts trailing commas. Replace only a
    # comma whose next non-whitespace token closes an array/object; preserving
    # character positions keeps JSON parser diagnostics useful.
    normalized = list(stripped)
    index = 0
    in_string = False
    escaped = False
    while index < len(normalized):
        character = normalized[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            index += 1
            continue
        if character == '"':
            in_string = True
            index += 1
            continue
        if character == ",":
            lookahead = index + 1
            while lookahead < len(normalized) and normalized[lookahead].isspace():
                lookahead += 1
            if lookahead < len(normalized) and normalized[lookahead] in "}]":
                normalized[index] = " "
        index += 1
    return "".join(normalized)


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = item
    return result


def _validated_header_pair(
    name: Any,
    value: Any,
    *,
    source: str,
) -> tuple[str, str]:
    if not isinstance(name, str) or not name.strip():
        raise ProviderRegistrationError(
            f"{source} header names must be non-empty strings"
        )
    if not isinstance(value, str):
        raise ProviderRegistrationError(f"{source} header values must be strings")
    if any(character in name or character in value for character in ("\r", "\n", "\0")):
        raise ProviderRegistrationError(
            f"{source} headers must not contain control separators"
        )
    return name, value


def _declared_positive_integer(value: Any, *, field: str) -> int:
    """Validate an optional Pi numeric declaration without lossy coercion."""

    if value is None:
        return 0
    if isinstance(value, bool):
        raise ProviderRegistrationError(f"{field} must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        parsed = int(value)
    else:
        raise ProviderRegistrationError(f"{field} must be a positive integer")
    if parsed <= 0 or parsed > MAX_SAFE_INTEGER:
        raise ProviderRegistrationError(
            f"{field} must be between 1 and {MAX_SAFE_INTEGER}"
        )
    return parsed


def _declared_finite_number(
    value: Any,
    *,
    field: str,
) -> TokenNumber | None:
    """Validate an optional Pi model limit without lossy coercion."""

    if value is None:
        return None
    return _declared_positive_integer(value, field=field)


def _declared_boolean(value: Any, *, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ProviderRegistrationError(f"{field} must be a boolean")
    return value


def _finite_number(value: Any, *, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ProviderRegistrationError(f"{field} must be a finite number")
    return float(value)


def _validate_thinking_level_map(value: Any, *, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ProviderRegistrationError(f"{field} must be an object")
    for raw_level, mapped in value.items():
        level = _clean_text(raw_level)
        # TypeBox objects keep additional properties unless explicitly closed.
        # Pi therefore validates the seven declared levels but preserves any
        # extension-owned keys untouched.
        if level in SUPPORTED_REASONING_LEVELS and (
            mapped is not None and not isinstance(mapped, str)
        ):
            raise ProviderRegistrationError(
                f"{field} contains an invalid thinking-level entry"
            )


def _validate_model_input(value: Any, *, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise ProviderRegistrationError(f"{field} must be an array")
    if any(item not in {"text", "image"} for item in value):
        raise ProviderRegistrationError(
            f"{field} may contain only 'text' and 'image'"
        )


def _validate_model_cost(
    value: Any,
    *,
    field: str,
    partial: bool,
) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ProviderRegistrationError(f"{field} must be an object")
    if not partial:
        missing = [key for key in _MODEL_COST_RATE_KEYS if key not in value]
        if missing:
            raise ProviderRegistrationError(
                f"{field} is missing required field(s): {', '.join(missing)}"
            )
    for key in _MODEL_COST_RATE_KEYS:
        if key in value:
            _finite_number(value[key], field=f"{field}.{key}")
    tiers = value.get("tiers")
    if tiers is None:
        return
    if not isinstance(tiers, Sequence) or isinstance(
        tiers,
        (str, bytes, bytearray),
    ):
        raise ProviderRegistrationError(f"{field}.tiers must be an array")
    for index, raw_tier in enumerate(tiers):
        if not isinstance(raw_tier, Mapping):
            raise ProviderRegistrationError(
                f"{field}.tiers[{index}] must be an object"
            )
        missing_tier = [
            key for key in _MODEL_COST_TIER_KEYS if key not in raw_tier
        ]
        if missing_tier:
            raise ProviderRegistrationError(
                f"{field}.tiers[{index}] is missing required field(s): "
                f"{', '.join(sorted(missing_tier))}"
            )
        for key in _MODEL_COST_TIER_KEYS:
            _finite_number(raw_tier[key], field=f"{field}.tiers[{index}].{key}")


def _normalize_api(value: Any) -> str:
    raw = _clean_text(value).lower()
    if not raw:
        return ""
    resolved = _SUPPORTED_APIS.get(raw)
    if resolved is None:
        raise UnsupportedProviderCapabilityError(
            f"Unsupported provider api '{raw}'. MiniCode currently maps only "
            "anthropic-messages, openai-responses, and openai-completions."
        )
    return resolved


def _parse_template(value: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    literal: list[str] = []

    def flush() -> None:
        if literal:
            parts.append(("literal", "".join(literal)))
            literal.clear()

    index = 0
    while index < len(value):
        if value[index] != "$":
            literal.append(value[index])
            index += 1
            continue
        if index + 1 >= len(value):
            literal.append("$")
            index += 1
            continue
        next_char = value[index + 1]
        if next_char in {"$", "!"}:
            literal.append(next_char)
            index += 2
            continue
        if next_char == "{":
            end = value.find("}", index + 2)
            if end < 0:
                literal.append("$")
                index += 1
                continue
            name = value[index + 2 : end]
            if _ENV_NAME.fullmatch(name):
                flush()
                parts.append(("env", name))
            else:
                literal.append(value[index : end + 1])
            index = end + 1
            continue
        match = _ENV_PREFIX.match(value, index + 1)
        if match is not None:
            flush()
            parts.append(("env", match.group(0)))
            index = match.end()
            continue
        literal.append("$")
        index += 1
    flush()
    return parts


def _execute_config_command(command_config: str, *, use_cache: bool) -> str | None:
    if use_cache:
        with _COMMAND_RESULT_LOCK:
            if command_config in _COMMAND_RESULT_CACHE:
                return _COMMAND_RESULT_CACHE[command_config]
    command = command_config[1:]
    result: str | None = None
    if command.strip():
        kwargs: dict[str, Any] = {
            "text": True,
            "timeout": 10,
            "check": False,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = int(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        executable: str | None = None
        use_stdin = False
        if os.name == "nt":
            candidates = [
                Path(root) / "Git" / "bin" / "bash.exe"
                for root in (
                    os.getenv("ProgramFiles", ""),
                    os.getenv("ProgramFiles(x86)", ""),
                )
                if root
            ]
            executable = next(
                (str(path) for path in candidates if path.is_file()),
                None,
            ) or shutil.which("bash.exe") or shutil.which("bash")
            if executable:
                normalized = executable.replace("/", "\\").casefold()
                use_stdin = bool(
                    re.match(
                        r"^[a-z]:\\windows\\(?:system32|systransport)\\bash\.exe$",
                        normalized,
                    )
                )
        else:
            executable = (
                "/bin/bash"
                if Path("/bin/bash").is_file()
                else shutil.which("bash") or shutil.which("sh")
            )
        try:
            with tempfile.TemporaryFile(mode="w+b") as output_file:
                if executable:
                    completed = subprocess.run(
                        [executable, "-s" if use_stdin else "-c", *(() if use_stdin else (command,))],
                        input=command if use_stdin else None,
                        stdout=output_file,
                        shell=False,
                        **kwargs,
                    )
                else:
                    # ``!command`` is a provider configuration primitive, not a
                    # request to let the platform choose an arbitrary shell.  A
                    # missing Bash executable must fail closed; falling back to
                    # ``shell=True`` would silently change both the interpreter
                    # and the command-injection boundary on Windows.
                    completed = None
                if completed is not None and completed.returncode == 0:
                    output_file.seek(0)
                    raw_output = output_file.read(
                        _MAX_CONFIG_COMMAND_OUTPUT_BYTES + 1
                    )
                    if len(raw_output) <= _MAX_CONFIG_COMMAND_OUTPUT_BYTES:
                        output = raw_output.decode(
                            "utf-8",
                            errors="replace",
                        ).strip()
                        result = output or None
        except (OSError, subprocess.SubprocessError):
            result = None
    if use_cache:
        with _COMMAND_RESULT_LOCK:
            _COMMAND_RESULT_CACHE[command_config] = result
    return result


def resolve_config_value(
    value: Any,
    *,
    description: str,
    use_command_cache: bool = False,
    environment: Mapping[str, Any] | None = None,
) -> str:
    """Port Pi's env/template/``!command`` provider value resolution."""

    raw = str(value if value is not None else "")
    if raw.startswith("!"):
        resolved = _execute_config_command(raw, use_cache=use_command_cache)
        if resolved is None:
            raise ProviderRegistrationError(
                f"Failed to resolve {description} from configured shell command"
            )
        return resolved
    output: list[str] = []
    missing: list[str] = []
    for kind, item in _parse_template(raw):
        if kind == "literal":
            output.append(item)
            continue
        explicit = environment.get(item) if isinstance(environment, Mapping) else None
        env_value = explicit if explicit is not None else os.getenv(item)
        if not env_value:
            missing.append(item)
        else:
            output.append(env_value)
    if missing:
        noun = "variable" if len(missing) == 1 else "variables"
        raise ProviderRegistrationError(
            f"Failed to resolve {description} from environment {noun}: "
            f"{', '.join(dict.fromkeys(missing))}"
        )
    return "".join(output)


def clear_api_key_cache() -> None:
    with _COMMAND_RESULT_LOCK:
        _COMMAND_RESULT_CACHE.clear()


def _config_value_is_configured(
    value: str,
    environment: Mapping[str, Any] | None = None,
) -> bool:
    if value.startswith("!"):
        return bool(value[1:].strip())
    return all(
        kind != "env"
        or bool(
            environment.get(item)
            if isinstance(environment, Mapping) and item in environment
            else os.getenv(item)
        )
        for kind, item in _parse_template(value)
    )


def _config_value_env_names(value: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item
            for kind, item in _parse_template(str(value if value is not None else ""))
            if kind == "env"
        )
    )


def _resolved_config_environment(
    values: Sequence[Any],
    explicit: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    environment: dict[str, str] = {}
    for value in values:
        for name in _config_value_env_names(value):
            explicit_value = explicit.get(name) if isinstance(explicit, Mapping) else None
            resolved = explicit_value if explicit_value is not None else os.getenv(name)
            if resolved:
                environment[name] = resolved
    return environment


def _signal_is_aborted(signal: Any) -> bool:
    if signal is None:
        return False
    aborted = getattr(signal, "aborted", False)
    if callable(aborted):
        aborted = aborted()
    if bool(aborted):
        return True
    is_set = getattr(signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _call_with_optional_signal(callback: Any, value: Any, signal: Any) -> Any:
    """Invoke a provider callback that may or may not accept an abort signal.

    The documented contract is ``callback(value)``; the signal is an optional
    second positional argument. Signature inspection picks the arity, because
    calling with two arguments and retrying on ``TypeError`` would run a
    callback's side effects twice whenever the callback itself raised it.
    """

    try:
        parameters = list(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return callback(value)
    positional = [
        parameter
        for parameter in parameters
        if parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    accepts_signal = len(positional) >= 2 or any(
        parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    )
    return callback(value, signal) if accepts_signal else callback(value)


def _minicode_network_allowed() -> bool:
    value = os.environ.get("MINICODE_OFFLINE")
    if value is None:
        return True
    return str(value).strip().lower() in {"", "0", "false", "no", "off"}


