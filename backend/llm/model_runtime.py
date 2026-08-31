"""Conversation-owned MiniCode model/provider runtime.

Each conversation generation owns an isolated runtime, validates queued
provider registrations, and publishes the complete generation atomically.
Retiring an old generation cannot remove a same-named provider from the
currently published generation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from backend.config import (
    get_anthropic_settings,
    get_custom_settings,
    get_openai_settings,
    get_provider_model_metadata,
    resolve_context_window_details,
)
from backend.llm.reasoning_effort import normalize_reasoning_effort
from backend.llm.model_selection import (
    apply_model_thinking_level,
    clamp_model_thinking_level,
    config_with_model_budget,
    default_model_thinking_level,
    model_thinking_levels,
)
from backend.llm.provider_contracts import (
    ModelDefinition,
    ProviderAdapterSpec,
    ProviderDefinition,
    ProviderRegistrationError,
    UnsupportedProviderCapabilityError,
)
from backend.llm.model_runtime_definitions import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_OUTPUT_TOKENS,
    SUPPORTED_REASONING_LEVELS,
    _AttributeMapping,
    _DEFAULT_MODELS_CONFIG_FILE,
    _EXTENSION_OVERRIDE_UNSET,
    _MAX_MODELS_CONFIG_BYTES,
    _ProviderAuthContext,
    _ProviderModelsStore,
    _as_mapping,
    _call_with_optional_signal,
    clear_api_key_cache,
    _clean_text,
    _config_value_env_names,
    _config_value_is_configured,
    _declared_boolean,
    _declared_finite_number,
    _extension_model_extra,
    _finite_number,
    _merge_headers,
    _merge_model_cost,
    _minicode_network_allowed,
    _normalize_api,
    _provider_member,
    _reject_duplicate_json_keys,
    _reject_noncanonical_fields,
    _resolved_config_environment,
    _signal_is_aborted,
    _strip_json_comments,
    _validate_model_cost,
    _validate_model_input,
    _validate_thinking_level_map,
    _validated_header_pair,
    _api_key_config,
    _base_model,
    _load_base_providers,
    _matching_model_definition,
    _normalize_api_key_credential,
    _normalize_auth_check,
    _normalize_model_auth,
    _normalize_oauth_credentials,
    _normalize_provider_env,
    _oauth_config,
    _oauth_method,
    _provider_credential_payload,
    resolve_config_value,
)


class ModelRuntime:
    """Provider composition owner for one conversation extension generation."""

    def __init__(
        self,
        *,
        on_change: Callable[["ModelRuntime", str, str], Any] | None = None,
        models_store: Any | None = None,
        models_path: str | Path | None = None,
        provider_configs: Mapping[str, Any] | None = None,
    ) -> None:
        self._extension_providers: dict[str, dict[str, Any]] = {}
        self._refreshed_extension_models: dict[str, tuple[Any, ...]] = {}
        self._on_change = on_change
        self._active = True
        self._revision = 0
        self._dynamic_refresh_epoch = 0
        self._dynamic_refresh_task: asyncio.Task[None] | None = None
        self._dynamic_refresh_guard = threading.RLock()
        self._errors: dict[str, str] = {}
        self._composition_errors: dict[str, str] = {}
        self._availability_error: str | None = None
        self._config_error: str | None = None
        self._resolved_api_key_auth: dict[str, dict[str, Any] | None] = {}
        self._api_key_auth_status: dict[str, dict[str, Any] | None] = {}
        self._resolved_oauth_auth: dict[str, dict[str, Any]] = {}
        self._resolved_oauth_credential: dict[str, dict[str, Any]] = {}
        # Model modifiers use the credential captured by the most recent
        # provider refresh so catalog publication remains transactional.
        self._oauth_model_credentials: dict[str, dict[str, Any]] = {}
        self._oauth_refresh_locks: dict[str, asyncio.Lock] = {}
        self._api_key_resolution_locks: dict[str, asyncio.Lock] = {}
        self._provider_generations: dict[str, int] = {}
        self._auth_lock_guard = threading.RLock()
        if models_store is None:
            from backend.llm.provider_models import ProviderModelsStorage

            models_store = ProviderModelsStorage()
        self._models_store = models_store
        self._base_providers = self._load_base_providers()
        self._models_path = (
            Path(models_path).expanduser().resolve(strict=False)
            if models_path is not None
            else _DEFAULT_MODELS_CONFIG_FILE
        )
        self._model_configs_from_memory = provider_configs is not None
        if provider_configs is None:
            self._model_configs = self._load_model_configs_file(self._models_path)
        else:
            self._model_configs = self._normalize_model_configs(
                provider_configs,
                source="provider_configs",
            )
        self._available_snapshot: tuple[ModelDefinition, ...] = ()
        self._refresh_available_snapshot(apply_filters=False)

    @property
    def active(self) -> bool:
        return self._active

    @property
    def revision(self) -> int:
        return self._revision

    def assert_active(self) -> None:
        if not self._active:
            raise RuntimeError("Model runtime belongs to a retired extension generation")

    def retire(self) -> None:
        self._active = False
        self._on_change = None
        self._resolved_api_key_auth.clear()
        self._api_key_auth_status.clear()
        self._resolved_oauth_auth.clear()
        self._resolved_oauth_credential.clear()
        self._oauth_model_credentials.clear()


    def _base_provider(self, provider_id: str) -> Any | None:
        clean_id = _clean_text(provider_id)
        return self._base_providers.get(clean_id)

    def _oauth_provider(self, provider_id: str) -> Any | None:
        clean_id = _clean_text(provider_id)
        extension = self._extension_providers.get(clean_id, {})
        configured = _oauth_config(extension)
        if configured is not None:
            return configured
        base = self._base_provider(clean_id)
        direct = _provider_member(base, "oauth")
        if direct is not None:
            return direct
        auth = _provider_member(base, "auth")
        return _provider_member(auth, "oauth")


    def _api_key_provider(self, provider_id: str) -> Any | None:
        clean_id = _clean_text(provider_id)
        extension = self._extension_providers.get(clean_id, {})
        configured = _api_key_config(extension)
        if configured is not None:
            return configured
        base = self._base_provider(clean_id)
        auth = _provider_member(base, "auth")
        return _provider_member(auth, "api_key")

    def _has_provider_auth_capability(self, provider_id: str) -> bool:
        if self._oauth_provider(provider_id) is not None:
            return True
        if self._api_key_provider(provider_id) is not None:
            return True
        extension = self._extension_providers.get(provider_id, {})
        config = self._model_configs.get(provider_id, {})
        if "api_key" in extension:
            return True
        if "api_key" in config:
            return True
        if provider_id in self._extension_providers or provider_id in self._model_configs:
            # Pi composes the default interactive API-key method for every
            # declarative/extension provider that is not OAuth-only.
            return True
        # MiniCode's settings-backed builtins expose the standard interactive
        # API-key method even when no key is currently configured.
        return provider_id in self._base_providers


    _auth_method = staticmethod(_oauth_method)
    _base_model = staticmethod(_base_model)

    @classmethod
    def _load_base_providers(cls) -> dict[str, dict[str, Any]]:
        return _load_base_providers(
            openai=get_openai_settings(),
            anthropic=get_anthropic_settings(),
            custom=get_custom_settings(),
        )

    def _provider_lock(self, provider_id: str, *, oauth: bool) -> asyncio.Lock:
        clean_id = _clean_text(provider_id)
        locks = self._oauth_refresh_locks if oauth else self._api_key_resolution_locks
        with self._auth_lock_guard:
            lock = locks.get(clean_id)
            if lock is None:
                lock = asyncio.Lock()
                locks[clean_id] = lock
            return lock

    def _provider_generation(self, provider_id: str) -> int:
        return int(self._provider_generations.get(_clean_text(provider_id), 0))

    def _bump_provider_generation(self, provider_id: str) -> int:
        clean_id = _clean_text(provider_id)
        generation = self._provider_generation(clean_id) + 1
        self._provider_generations[clean_id] = generation
        return generation

    def _assert_provider_generation(self, provider_id: str, generation: int) -> None:
        self.assert_active()
        if self._provider_generation(provider_id) != int(generation):
            raise RuntimeError(
                f'Provider "{_clean_text(provider_id)}" changed during an auth operation'
            )

    def _stored_credential(self, provider_id: str) -> dict[str, Any] | None:
        from backend.llm.provider_auth import ProviderCredentialCorruptError

        clean_id = _clean_text(provider_id)
        try:
            value = self._provider_auth_storage().get(clean_id)
        except ProviderCredentialCorruptError as exc:
            # The provider is unusable, but "unauthenticated" is the wrong story:
            # record why so get_error() can report it instead of silently
            # offering the user a fresh login.
            self._errors[clean_id] = str(exc) or type(exc).__name__
            return None
        return dict(value) if isinstance(value, Mapping) else None


    async def _modify_stored_credential(
        self,
        provider_id: str,
        modifier: Callable[[dict[str, Any] | None], Any],
    ) -> dict[str, Any] | None:
        storage = self._provider_auth_storage()
        modify = getattr(storage, "modify", None)
        if callable(modify):
            result = modify(_clean_text(provider_id), modifier)
            if inspect.isawaitable(result):
                result = await result
            return dict(result) if isinstance(result, Mapping) else None

        # Compatibility for embedders with the pre-CredentialStore test/store
        # surface.  The runtime lock preserves correct in-process semantics;
        # production ProviderAuthStorage supplies the cross-process transaction.
        clean_id = _clean_text(provider_id)
        async with self._provider_lock(clean_id, oauth=True):
            current = self._stored_credential(clean_id)
            next_value = modifier(current)
            if inspect.isawaitable(next_value):
                next_value = await next_value
            if next_value is None:
                return current
            if not isinstance(next_value, Mapping):
                raise ProviderRegistrationError(
                    "Credential modifier must return a credential object"
                )
            storage.set(clean_id, dict(next_value))
            return dict(next_value)

    def is_using_oauth(self, model: ModelDefinition | str) -> bool:
        provider_id = model.provider if isinstance(model, ModelDefinition) else str(model)
        credentials = self._stored_credential(provider_id)
        return (
            self._oauth_provider(provider_id) is not None
            and isinstance(credentials, Mapping)
            and credentials.get("type") == "oauth"
        )

    def _provider_auth_storage(self) -> Any:
        storage = getattr(self, "_auth_storage", None)
        if storage is None:
            from backend.llm.provider_auth import ProviderAuthStorage

            storage = ProviderAuthStorage()
            self._auth_storage = storage
        return storage


    def _configured_api_key_credential(
        self,
        provider_id: str,
        stored: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if isinstance(stored, Mapping) and stored.get("type") == "api_key":
            return {
                "type": "api_key",
                **({"key": str(stored.get("key"))} if stored.get("key") is not None else {}),
                **(
                    {"env": _normalize_provider_env(
                        stored.get("env"),
                        source="Stored API-key credential",
                    )}
                    if stored.get("env") is not None
                    else {}
                ),
            }
        raw_key = self._raw_api_key(_clean_text(provider_id))
        extension = self._extension_providers.get(_clean_text(provider_id), {})
        config = self._model_configs.get(_clean_text(provider_id), {})
        declared = "api_key" in extension or "api_key" in config
        if not declared:
            return None
        if not raw_key or not _config_value_is_configured(raw_key):
            return None
        key = resolve_config_value(
            raw_key,
            description=f'API key for provider "{_clean_text(provider_id)}"',
            use_command_cache=True,
        )
        return {
            "type": "api_key",
            "key": key,
            **(
                {"env": environment}
                if (environment := _resolved_config_environment([raw_key]))
                else {}
            ),
        }

    def _normalize_api_key_result(
        self,
        provider_id: str,
        value: Any,
        credential_env: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ProviderRegistrationError("API-key resolve must return an auth result object")
        raw_auth = value.get("auth")
        auth = _normalize_model_auth(
            raw_auth,
            source="API-key resolve auth",
            allow_empty=True,
        )
        environment = _normalize_provider_env(
            credential_env,
            source="API-key credential",
        )
        environment.update(_normalize_provider_env(
            value.get("env"),
            source="API-key resolve",
        ))
        configured_headers = self._resolve_provider_headers(
            _clean_text(provider_id),
            environment,
        )
        raw_headers = auth.get("headers")
        headers = (
            {str(key): str(item) for key, item in raw_headers.items()}
            if isinstance(raw_headers, Mapping)
            else {}
        )
        headers = _merge_headers(headers, configured_headers)
        if self._auth_header_enabled(_clean_text(provider_id)):
            api_key = str(auth.get("api_key") or "")
            if not api_key:
                raise ProviderRegistrationError("auth_header requires a resolved API key")
            headers = _merge_headers(
                headers,
                {"Authorization": f"Bearer {api_key}"},
            )
        if headers:
            auth["headers"] = headers
        else:
            auth.pop("headers", None)
        source = value.get("source")
        return {
            "auth": auth,
            **({"env": environment} if environment else {}),
            **({"source": str(source)} if isinstance(source, str) and source else {}),
        }


    async def refresh_provider_auth(
        self,
        provider_id: str | None = None,
        *,
        signal: Any | None = None,
        publish_snapshot: bool = True,
    ) -> None:
        """Resolve modern Pi API-key auth into a generation-owned request cache."""

        self.assert_active()
        if _signal_is_aborted(signal):
            return
        provider_ids = (
            (_clean_text(provider_id),)
            if provider_id is not None
            else tuple(
                dict.fromkeys(
                    [
                        *self._base_providers,
                        *self._model_configs,
                        *self._extension_providers,
                    ]
                )
            )
        )
        for clean_id in provider_ids:
            if _signal_is_aborted(signal):
                return
            stored = self._stored_credential(clean_id)
            if isinstance(stored, Mapping) and stored.get("type") == "oauth":
                # A stored credential owns the provider.  Resolve/refresh OAuth
                # through the CredentialStore transaction and never fall back
                # to ambient API-key auth for the same provider.
                await self.refresh_oauth_credentials(
                    clean_id,
                    signal=signal,
                    publish_snapshot=publish_snapshot,
                )
                self._resolved_api_key_auth.pop(clean_id, None)
                self._api_key_auth_status.pop(clean_id, None)
                continue
            self._resolved_oauth_auth.pop(clean_id, None)
            self._resolved_oauth_credential.pop(clean_id, None)
            api_key_provider = self._api_key_provider(clean_id)
            if api_key_provider is None:
                continue
            resolve = self._auth_method(api_key_provider, "resolve")
            if not callable(resolve):
                raise ProviderRegistrationError(
                    f'Provider "{clean_id}" API-key auth does not expose resolve'
                )
            async with self._provider_lock(clean_id, oauth=False):
                provider_generation = self._provider_generation(clean_id)
                self._assert_provider_generation(clean_id, provider_generation)
                stored = self._stored_credential(clean_id)
                credential = self._configured_api_key_credential(clean_id, stored)
                explicit_env = (
                    credential.get("env")
                    if isinstance(credential, Mapping)
                    and isinstance(credential.get("env"), Mapping)
                    else None
                )
                context = _ProviderAuthContext(explicit_env)
                input_value = _AttributeMapping(ctx=context)
                if credential is not None:
                    input_value["credential"] = _AttributeMapping(credential)
                check = self._auth_method(api_key_provider, "check")
                if callable(check):
                    checked = check(input_value)
                    if inspect.isawaitable(checked):
                        checked = await checked
                    self._assert_provider_generation(clean_id, provider_generation)
                    status = _normalize_auth_check(checked)
                    if status is None:
                        self._resolved_api_key_auth[clean_id] = None
                        self._api_key_auth_status[clean_id] = None
                        continue
                else:
                    status = None
                resolved = resolve(input_value)
                if inspect.isawaitable(resolved):
                    resolved = await resolved
                self._assert_provider_generation(clean_id, provider_generation)
                if _signal_is_aborted(signal):
                    return
                normalized = self._normalize_api_key_result(
                    clean_id,
                    resolved,
                    explicit_env,
                )
                self._resolved_api_key_auth[clean_id] = normalized
                if normalized is None:
                    self._api_key_auth_status[clean_id] = None
                    continue
                self._api_key_auth_status[clean_id] = status or {
                    "type": "api_key",
                    **(
                        {"source": str(normalized.get("source"))}
                        if normalized.get("source")
                        else {}
                    ),
                }
        if _signal_is_aborted(signal):
            return
        if publish_snapshot:
            try:
                self._refresh_available_snapshot(apply_filters=True)
            except Exception as exc:
                self._availability_error = str(exc) or type(exc).__name__
                raise
            self._availability_error = None

    def _resolve_modern_api_key_sync(self, provider_id: str) -> dict[str, Any] | None:
        clean_id = _clean_text(provider_id)
        if clean_id in self._resolved_api_key_auth:
            cached = self._resolved_api_key_auth[clean_id]
            return dict(cached) if isinstance(cached, Mapping) else None
        provider = self._api_key_provider(clean_id)
        if provider is None:
            return None
        resolve = self._auth_method(provider, "resolve")
        if not callable(resolve):
            raise ProviderRegistrationError(
                f'Provider "{clean_id}" API-key auth does not expose resolve'
            )
        stored = self._stored_credential(clean_id)
        if stored is not None and stored.get("type") == "oauth":
            return None
        credential = self._configured_api_key_credential(clean_id, stored)
        explicit_env = (
            credential.get("env")
            if isinstance(credential, Mapping)
            and isinstance(credential.get("env"), Mapping)
            else None
        )
        context = _ProviderAuthContext(explicit_env)
        input_value = _AttributeMapping(ctx=context)
        if credential is not None:
            input_value["credential"] = _AttributeMapping(credential)
        check = self._auth_method(provider, "check")
        status: dict[str, Any] | None = None
        if callable(check):
            checked = check(input_value)
            if inspect.isawaitable(checked):
                close = getattr(checked, "close", None)
                if callable(close):
                    close()
                raise ProviderRegistrationError(
                    f'Provider "{clean_id}" requires asynchronous auth checking; '
                    "refresh provider auth before constructing its adapter"
                )
            self.assert_active()
            status = _normalize_auth_check(checked)
            if status is None:
                self._resolved_api_key_auth[clean_id] = None
                self._api_key_auth_status[clean_id] = None
                return None
        resolved = resolve(input_value)
        if inspect.isawaitable(resolved):
            close = getattr(resolved, "close", None)
            if callable(close):
                close()
            raise ProviderRegistrationError(
                f'Provider "{clean_id}" requires asynchronous auth resolution; '
                "refresh provider auth before constructing its adapter"
            )
        self.assert_active()
        normalized = self._normalize_api_key_result(
            clean_id,
            resolved,
            explicit_env,
        )
        self._resolved_api_key_auth[clean_id] = normalized
        self._api_key_auth_status[clean_id] = (
            status
            or {
                "type": "api_key",
                **(
                    {"source": normalized.get("source")}
                    if normalized and normalized.get("source")
                    else {}
                ),
            }
            if normalized is not None
            else None
        )
        return dict(normalized) if isinstance(normalized, Mapping) else None

    async def login_provider(
        self,
        provider_id: str,
        callbacks: Any,
        *,
        auth_type: str = "oauth",
    ) -> dict[str, Any]:
        self.assert_active()
        clean_id = _clean_text(provider_id)
        provider_generation = self._provider_generation(clean_id)
        normalized_type = _clean_text(auth_type).lower()
        if normalized_type not in {"oauth", "api_key"}:
            raise ProviderRegistrationError(
                "Provider login type must be 'oauth' or 'api_key'"
            )
        if normalized_type == "api_key":
            provider = self._api_key_provider(clean_id)
            login = (
                self._auth_method(provider, "login")
                if provider is not None
                else None
            )
            if callable(login):
                credential = login(callbacks)
                if inspect.isawaitable(credential):
                    credential = await credential
            else:
                # Pi composes a default secret prompt for every provider that
                # is not OAuth-only. Built-in MiniCode providers have the same
                # API-key capability even though their legacy settings do not
                # carry a nested auth.api_key object.
                oauth_only = (
                    self._oauth_provider(clean_id) is not None
                    and provider is None
                    and not self._raw_api_key(clean_id)
                )
                prompt = getattr(callbacks, "prompt", None)
                if oauth_only or not callable(prompt):
                    raise ProviderRegistrationError(
                        f'Provider "{provider_id}" does not expose API-key login'
                    )
                key = prompt(
                    _AttributeMapping(
                        type="secret",
                        message="Enter API key",
                    )
                )
                if inspect.isawaitable(key):
                    key = await key
                if not isinstance(key, str) or not key:
                    raise ProviderRegistrationError(
                        f'Provider "{provider_id}" API-key login returned an empty key'
                    )
                credential = {"type": "api_key", "key": key}
            self._assert_provider_generation(clean_id, provider_generation)
            drain = getattr(callbacks, "drain", None)
            if callable(drain):
                drained = drain()
                if inspect.isawaitable(drained):
                    await drained
            self._assert_provider_generation(clean_id, provider_generation)
            payload = _normalize_api_key_credential(clean_id, credential)

            async def publish_api_key(
                _current: dict[str, Any] | None,
            ) -> dict[str, Any]:
                self._assert_provider_generation(clean_id, provider_generation)
                return dict(payload)

            stored = await self._modify_stored_credential(
                clean_id,
                publish_api_key,
            )
            self._assert_provider_generation(clean_id, provider_generation)
            if not stored or stored.get("type") != "api_key":
                raise ProviderRegistrationError(
                    f'Provider "{clean_id}" API-key credential was not persisted'
                )
            latest = _provider_credential_payload(
                self._stored_credential(clean_id)
            )
            if latest != _provider_credential_payload(stored):
                raise ProviderRegistrationError(
                    f'Provider "{clean_id}" API-key credential changed during login'
                )
            self._resolved_oauth_auth.pop(clean_id, None)
            self._resolved_oauth_credential.pop(clean_id, None)
            self._oauth_model_credentials.pop(clean_id, None)
            self._resolved_api_key_auth.pop(clean_id, None)
            self._api_key_auth_status.pop(clean_id, None)
            # Pi follows every login with a model refresh. Besides resolving
            # availability, this runs base/extension refresh_models callbacks
            # and updates any credential-dependent model projection.
            await self.refresh_dynamic_models(
                allow_network=_minicode_network_allowed(),
                force=True,
            )
            self._changed(
                clean_id,
                "api-key-login",
                refresh_snapshot=False,
            )
            self.assert_active()
            return {"type": "api_key"}

        provider = self._oauth_provider(clean_id)
        login = _oauth_method(provider, "login") if provider is not None else None
        if not callable(login):
            raise ProviderRegistrationError(f'Provider "{provider_id}" does not expose OAuth login')
        credentials = login(callbacks)
        if inspect.isawaitable(credentials):
            credentials = await credentials
        self._assert_provider_generation(clean_id, provider_generation)
        drain = getattr(callbacks, "drain", None)
        if callable(drain):
            drained = drain()
            if inspect.isawaitable(drained):
                await drained
        self._assert_provider_generation(clean_id, provider_generation)
        payload = _normalize_oauth_credentials(clean_id, credentials)
        resolved_auth = await self._derive_oauth_auth(clean_id, provider, payload)
        self._assert_provider_generation(clean_id, provider_generation)

        async def publish(_current: dict[str, Any] | None) -> dict[str, Any]:
            self._assert_provider_generation(clean_id, provider_generation)
            return dict(payload)

        stored = await self._modify_stored_credential(clean_id, publish)
        self._assert_provider_generation(clean_id, provider_generation)
        if not stored or stored.get("type") != "oauth":
            raise ProviderRegistrationError(
                f'Provider "{clean_id}" OAuth credential was not persisted'
            )
        latest = _provider_credential_payload(
            self._stored_credential(clean_id)
        )
        if latest != _provider_credential_payload(stored):
            raise ProviderRegistrationError(
                f'Provider "{clean_id}" OAuth credential changed during login'
            )
        self._resolved_oauth_auth[clean_id] = dict(resolved_auth)
        self._resolved_oauth_credential[clean_id] = dict(latest or {})
        self._resolved_api_key_auth.pop(clean_id, None)
        self._api_key_auth_status.pop(clean_id, None)
        await self.refresh_dynamic_models(
            allow_network=_minicode_network_allowed(),
            force=True,
        )
        self._changed(
            clean_id,
            "oauth-login",
            refresh_snapshot=False,
        )
        return {"type": "oauth", "expires": payload.get("expires")}

    async def refresh_oauth_credentials(
        self,
        provider_id: str,
        *,
        signal: Any | None = None,
        publish_snapshot: bool = True,
    ) -> bool:
        self.assert_active()
        if _signal_is_aborted(signal):
            return False
        clean_id = _clean_text(provider_id)
        provider_generation = self._provider_generation(clean_id)
        provider = self._oauth_provider(clean_id)
        refresh = _oauth_method(provider, "refresh") if provider is not None else None
        credentials = self._stored_credential(clean_id)
        if not credentials or credentials.get("type") != "oauth" or not callable(refresh):
            self._resolved_oauth_auth.pop(clean_id, None)
            self._resolved_oauth_credential.pop(clean_id, None)
            return False

        refreshed = False

        async def refresh_if_expired(
            current: dict[str, Any] | None,
        ) -> dict[str, Any] | None:
            nonlocal refreshed
            self.assert_active()
            if not current or current.get("type") != "oauth":
                return None
            provider_credentials = _provider_credential_payload(current) or {}
            try:
                expires = float(provider_credentials.get("expires") or 0)
            except (TypeError, ValueError, OverflowError):
                expires = 0.0
            if math.isfinite(expires) and time.time() * 1000 < expires:
                return None
            if _signal_is_aborted(signal):
                return None
            updated = _call_with_optional_signal(refresh, provider_credentials, signal)
            if inspect.isawaitable(updated):
                updated = await updated
            self._assert_provider_generation(clean_id, provider_generation)
            payload = _normalize_oauth_credentials(clean_id, updated)
            refreshed = True
            return payload

        post = await self._modify_stored_credential(clean_id, refresh_if_expired)
        self._assert_provider_generation(clean_id, provider_generation)
        if _signal_is_aborted(signal):
            return refreshed
        canonical = _provider_credential_payload(post)
        if not canonical or canonical.get("type") != "oauth":
            self._resolved_oauth_auth.pop(clean_id, None)
            self._resolved_oauth_credential.pop(clean_id, None)
            return False
        resolved_auth = await self._derive_oauth_auth(clean_id, provider, canonical)
        self._assert_provider_generation(clean_id, provider_generation)
        latest = _provider_credential_payload(
            self._stored_credential(clean_id)
        )
        if latest != canonical:
            self._resolved_oauth_auth.pop(clean_id, None)
            self._resolved_oauth_credential.pop(clean_id, None)
            return False
        self._resolved_oauth_auth[clean_id] = dict(resolved_auth)
        self._resolved_oauth_credential[clean_id] = dict(canonical)
        self._resolved_api_key_auth.pop(clean_id, None)
        self._api_key_auth_status.pop(clean_id, None)
        if refreshed:
            if _signal_is_aborted(signal):
                return refreshed
            if publish_snapshot:
                try:
                    self._refresh_available_snapshot(apply_filters=True)
                except Exception as exc:
                    self._availability_error = str(exc) or type(exc).__name__
                    self._changed(
                        clean_id,
                        "oauth-refresh",
                        refresh_snapshot=False,
                    )
                    raise
                self._availability_error = None
            self._changed(
                clean_id,
                "oauth-refresh",
                refresh_snapshot=False,
            )
        return refreshed

    async def logout_provider(self, provider_id: str) -> bool:
        self.assert_active()
        clean_id = _clean_text(provider_id)
        self._bump_provider_generation(clean_id)
        storage = self._provider_auth_storage()
        delete_serialized = getattr(storage, "delete_serialized", None)
        if callable(delete_serialized):
            removed = delete_serialized(clean_id)
            if inspect.isawaitable(removed):
                removed = await removed
            removed = bool(removed)
        else:
            async with self._provider_lock(clean_id, oauth=True):
                removed = bool(storage.delete(clean_id))
        self._resolved_oauth_auth.pop(clean_id, None)
        self._resolved_oauth_credential.pop(clean_id, None)
        self._oauth_model_credentials.pop(clean_id, None)
        self._resolved_api_key_auth.pop(clean_id, None)
        self._api_key_auth_status.pop(clean_id, None)
        if removed:
            self._changed(clean_id, "oauth-logout")
        return removed

    async def _derive_oauth_auth(
        self,
        provider_id: str,
        provider: Any,
        credentials: Mapping[str, Any],
    ) -> dict[str, Any]:
        credential_environment = _normalize_provider_env(
            credentials.get("env"),
            source="OAuth credential",
        ) if credentials.get("env") is not None else {}
        to_auth = _oauth_method(provider, "to_auth")
        if callable(to_auth):
            resolved = to_auth({
                key: value
                for key, value in credentials.items()
                if key != "_minicode_auth"
            })
            if inspect.isawaitable(resolved):
                resolved = await resolved
            auth = _normalize_model_auth(
                resolved,
                source="OAuth to_auth",
                allow_empty=True,
            )
            raw_headers = auth.get("headers")
            headers = (
                {str(key): str(value) for key, value in raw_headers.items()}
                if isinstance(raw_headers, Mapping)
                else {}
            )
            headers = _merge_headers(
                headers,
                self._resolve_provider_headers(
                    _clean_text(provider_id),
                    credential_environment,
                ),
            )
            if self._auth_header_enabled(_clean_text(provider_id)):
                api_key = str(auth.get("api_key") or "")
                if not api_key:
                    raise ProviderRegistrationError(
                        "auth_header requires a resolved API key"
                    )
                headers = _merge_headers(
                    headers,
                    {"Authorization": f"Bearer {api_key}"},
                )
            if headers:
                auth["headers"] = headers
            else:
                auth.pop("headers", None)
            return auth

        get_api_key = _oauth_method(provider, "get_api_key")
        if not callable(get_api_key):
            raise ProviderRegistrationError(
                f'Provider "{provider_id}" OAuth exposes neither to_auth nor get_api_key'
            )
        api_key = get_api_key({
            key: value
            for key, value in credentials.items()
            if key != "_minicode_auth"
        })
        if inspect.isawaitable(api_key):
            api_key = await api_key
        if not str(api_key or ""):
            raise ProviderRegistrationError("OAuth get_api_key returned no request credential")
        headers = self._resolve_provider_headers(
            _clean_text(provider_id),
            credential_environment,
        )
        if self._auth_header_enabled(_clean_text(provider_id)):
            headers = _merge_headers(
                headers,
                {"Authorization": f"Bearer {api_key}"},
            )
        return {
            "api_key": str(api_key),
            **({"headers": headers} if headers else {}),
        }

    def cache_identity(self, provider_id: str, model_id: str) -> tuple[Any, ...]:
        return (id(self), self._revision, provider_id, model_id)


    def _normalize_model_configs(
        self,
        providers: Mapping[str, Any],
        *,
        source: str,
    ) -> dict[str, dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        for raw_provider_id, raw_config in providers.items():
            provider_id = _clean_text(raw_provider_id)
            if not provider_id:
                raise ProviderRegistrationError(
                    f"{source} provider ids must not be empty"
                )
            config = _as_mapping(
                raw_config,
                description=f'{source} provider "{provider_id}"',
            )
            self._validate_model_config(provider_id, config)
            normalized[provider_id] = config
        return normalized

    def _load_model_configs_file(self, path: Path) -> dict[str, dict[str, Any]]:
        self._config_error = None
        try:
            if not path.exists():
                return {}
            size = path.stat().st_size
            if size > _MAX_MODELS_CONFIG_BYTES:
                raise ProviderRegistrationError(
                    f"models.json exceeds {_MAX_MODELS_CONFIG_BYTES} bytes"
                )
            raw = path.read_text(encoding="utf-8")
        except Exception as exc:
            self._config_error = (
                f"Failed to load models.json: {str(exc) or type(exc).__name__}\n\n"
                f"File: {path}"
            )
            return {}

        def reject_non_json_number(value: str) -> None:
            raise ValueError(f"invalid JSON number constant: {value}")

        try:
            parsed = json.loads(
                _strip_json_comments(raw),
                parse_constant=reject_non_json_number,
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except Exception as exc:
            self._config_error = (
                f"Failed to parse models.json: {str(exc) or type(exc).__name__}\n\n"
                f"File: {path}"
            )
            return {}

        try:
            if not isinstance(parsed, Mapping):
                raise ProviderRegistrationError("models.json root must be an object")
            providers = parsed.get("providers")
            if not isinstance(providers, Mapping):
                raise ProviderRegistrationError(
                    "models.json providers must be an object"
                )
            return self._normalize_model_configs(
                providers,
                source="models.json",
            )
        except Exception as exc:
            self._config_error = (
                f"Invalid models.json schema:\n"
                f"  - {str(exc) or type(exc).__name__}\n\n"
                f"File: {path}"
            )
            return {}

    def _validate_model_config(
        self,
        provider_id: str,
        config: Mapping[str, Any],
    ) -> None:
        _reject_noncanonical_fields(
            config,
            {
                "apiKey": "api_key",
                "authHeader": "auth_header",
                "baseUrl": "base_url",
                "modelOverrides": "model_overrides",
            },
            source=f"Provider {provider_id}",
        )
        for key in ("name", "base_url", "api", "api_key"):
            if key in config and (
                not isinstance(config[key], str) or not str(config[key]).strip()
            ):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}: {key} must be a non-empty string"
                )
        raw_headers = config.get("headers")
        if raw_headers is not None and not isinstance(raw_headers, Mapping):
            raise ProviderRegistrationError(
                f"Provider {provider_id}: headers must be an object"
            )
        if isinstance(raw_headers, Mapping):
            for key, value in raw_headers.items():
                _validated_header_pair(key, value, source=f"Provider {provider_id}")
        if "auth_header" in config:
            _declared_boolean(
                config.get("auth_header"),
                field=f"Provider {provider_id}: auth_header",
            )

        raw_models = config.get("models")
        if raw_models is not None and (
            not isinstance(raw_models, Sequence)
            or isinstance(raw_models, (str, bytes, bytearray))
        ):
            raise ProviderRegistrationError(
                f"Provider {provider_id}: models must be an array"
            )
        if isinstance(raw_models, Sequence) and not isinstance(
            raw_models, (str, bytes, bytearray)
        ):
            for index, raw_model in enumerate(raw_models):
                model = _as_mapping(
                    raw_model,
                    description=f"Provider {provider_id} models[{index}]",
                )
                _reject_noncanonical_fields(
                    model,
                    {
                        "baseUrl": "base_url",
                        "contextWindow": "context_window",
                        "maxTokens": "max_tokens",
                        "thinkingLevelMap": "thinking_level_map",
                    },
                    source=f"Provider {provider_id}.models[{index}]",
                )
                model_id = model.get("id")
                if not isinstance(model_id, str) or not model_id:
                    raise ProviderRegistrationError(
                        f"Provider {provider_id}, model {index}: id must be a non-empty string"
                    )
                model_label = model_id
                for key in ("name", "api", "base_url"):
                    if key in model and (
                        not isinstance(model[key], str) or not model[key]
                    ):
                        raise ProviderRegistrationError(
                            f"Provider {provider_id}, model {model_label}: "
                            f"{key} must be a non-empty string"
                        )
                _validate_thinking_level_map(
                    model.get("thinking_level_map"),
                    field=(
                        f"Provider {provider_id}, model {model_label}: "
                        "thinking_level_map"
                    ),
                )
                _validate_model_input(
                    model.get("input"),
                    field=f"Provider {provider_id}, model {model_label}: input",
                )
                _validate_model_cost(
                    model.get("cost"),
                    field=f"Provider {provider_id}, model {model_label}: cost",
                    partial=False,
                )
                if "reasoning" in model:
                    _declared_boolean(
                        model.get("reasoning"),
                        field=f"Provider {provider_id}, model {model_label}: reasoning",
                    )
                for key in ("context_window", "max_tokens"):
                    if key in model:
                        _finite_number(
                            model[key],
                            field=f"Provider {provider_id}, model {model_label}: {key}",
                        )
                model_headers = model.get("headers")
                if model_headers is not None and not isinstance(
                    model_headers, Mapping
                ):
                    raise ProviderRegistrationError(
                        f"Provider {provider_id}, model {model_label}: headers must be an object"
                    )
                if isinstance(model_headers, Mapping):
                    for key, value in model_headers.items():
                        _validated_header_pair(
                            key,
                            value,
                            source=f"Provider {provider_id}, model {model_label}",
                        )

        raw_overrides = config.get("model_overrides")
        if raw_overrides is None:
            return
        if not isinstance(raw_overrides, Mapping):
            raise ProviderRegistrationError(
                f"Provider {provider_id}: model_overrides must be an object"
            )
        for raw_model_id, raw_override in raw_overrides.items():
            model_id = _clean_text(raw_model_id)
            if not model_id:
                raise ProviderRegistrationError(
                    f"Provider {provider_id}: model_overrides keys must not be empty"
                )
            override = _as_mapping(
                raw_override,
                description=f"Provider {provider_id}, model {model_id} override",
            )
            _reject_noncanonical_fields(
                override,
                {
                    "contextWindow": "context_window",
                    "maxTokens": "max_tokens",
                    "thinkingLevelMap": "thinking_level_map",
                },
                source=f"Provider {provider_id}.model_overrides.{model_id}",
            )
            if "name" in override and (
                not isinstance(override["name"], str)
                or not override["name"].strip()
            ):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}, model {model_id}: override name must be non-empty"
                )
            if "reasoning" in override:
                _declared_boolean(
                    override["reasoning"],
                    field=f"Provider {provider_id}, model {model_id}: override reasoning",
                )
            _validate_thinking_level_map(
                override.get("thinking_level_map"),
                field=(
                    f"Provider {provider_id}, model {model_id}: "
                    "override thinking_level_map"
                ),
            )
            _validate_model_input(
                override.get("input"),
                field=f"Provider {provider_id}, model {model_id}: override input",
            )
            _validate_model_cost(
                override.get("cost"),
                field=f"Provider {provider_id}, model {model_id}: override cost",
                partial=True,
            )
            for key in ("context_window", "max_tokens"):
                if key in override:
                    _finite_number(
                        override[key],
                        field=f"Provider {provider_id}, model {model_id}: override {key}",
                    )
            raw_headers = override.get("headers")
            if raw_headers is not None and not isinstance(raw_headers, Mapping):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}, model {model_id}: override headers must be an object"
                )
            if isinstance(raw_headers, Mapping):
                for key, value in raw_headers.items():
                    _validated_header_pair(
                        key,
                        value,
                        source=f"Provider {provider_id}, model {model_id} override",
                    )

    def refresh(self, *, publish_snapshot: bool = True) -> None:
        """Reload settings/models configuration for this runtime generation.

        Ordinary callers publish the callback-free availability projection
        immediately.  A dynamic model refresh is a wider transaction: it must
        reload configuration first, run provider auth/refresh/filter hooks, and
        only then replace the published availability snapshot.  Suppressing
        the provisional publication there prevents a failed filter from
        overwriting the last known-good filtered catalog.
        """

        self.assert_active()
        refreshed = self._load_base_providers()
        model_configs = (
            self._model_configs
            if self._model_configs_from_memory
            else self._load_model_configs_file(self._models_path)
        )
        if (
            refreshed == self._base_providers
            and model_configs == self._model_configs
        ):
            return
        self._base_providers = refreshed
        self._model_configs = model_configs
        self._changed(
            "*",
            "refresh",
            refresh_snapshot=publish_snapshot,
        )

    async def refresh_dynamic_models(
        self,
        *,
        allow_network: bool | None = None,
        force: bool = False,
        signal: Any | None = None,
    ) -> None:
        """Coalesce readers and serialize forced Pi model refresh mutations."""

        self.assert_active()
        if _signal_is_aborted(signal):
            return
        loop = asyncio.get_running_loop()
        with self._dynamic_refresh_guard:
            pending = self._dynamic_refresh_task
            if pending is not None and pending.done():
                pending = None
                self._dynamic_refresh_task = None
            if pending is not None and not force:
                task = pending
            else:

                async def queued_refresh(
                    after: asyncio.Task[None] | None,
                ) -> None:
                    if after is not None:
                        try:
                            await asyncio.shield(after)
                        except asyncio.CancelledError:
                            pass
                        except Exception as exc:
                            # The queued refresh remains ordered after the
                            # failed mutation, but the failure cannot vanish:
                            # callers must see that the previous availability
                            # transaction did not commit.
                            self._record_availability_failure(exc)
                    await self._run_dynamic_models_refresh(
                        allow_network=allow_network,
                        force=force,
                        signal=signal,
                    )

                task = loop.create_task(queued_refresh(pending if force else None))
                self._dynamic_refresh_task = task

                def clear_tracked(done: asyncio.Task[None]) -> None:
                    with self._dynamic_refresh_guard:
                        if self._dynamic_refresh_task is done:
                            self._dynamic_refresh_task = None

                task.add_done_callback(clear_tracked)
        await asyncio.shield(task)

    async def _run_dynamic_models_refresh(
        self,
        *,
        allow_network: bool | None,
        force: bool,
        signal: Any | None,
    ) -> None:
        """Execute one serialized settings/provider refresh transaction."""

        self.assert_active()
        if _signal_is_aborted(signal):
            return
        effective_allow_network = (
            _minicode_network_allowed()
            if allow_network is None
            else bool(allow_network)
        )
        # Configuration reload is part of this serialized refresh transaction.
        # Do not publish its unfiltered provisional view before provider auth,
        # model refresh, and filter callbacks have had a chance to succeed.
        self.refresh(publish_snapshot=False)
        self._dynamic_refresh_epoch += 1
        refresh_epoch = self._dynamic_refresh_epoch
        provider_ids = tuple(
            dict.fromkeys(
                [
                    *self._base_providers,
                    *self._model_configs,
                    *self._extension_providers,
                ]
            )
        )
        for provider_id in provider_ids:
            if _signal_is_aborted(signal):
                return
            current = self._extension_providers.get(provider_id, {})
            base_callback = _provider_member(
                self._base_provider(provider_id),
                "refresh_models",
            )
            extension_callback = current.get("refresh_models")
            extension_oauth = _oauth_config(current)
            oauth_modifier = (
                _oauth_method(extension_oauth, "modify_models")
                if extension_oauth is not None
                else None
            )
            auth_error: str | None = None
            stored_before_auth = self._stored_credential(provider_id)
            # Pi's offline refresh uses a stored OAuth credential as-is. It
            # does not rotate an expired token merely because an extension or
            # provider registration requested a cache/model refresh.
            offline_stored_oauth = bool(
                not effective_allow_network
                and isinstance(stored_before_auth, Mapping)
                and stored_before_auth.get("type") == "oauth"
            )
            if not offline_stored_oauth:
                try:
                    await self.refresh_provider_auth(
                        provider_id,
                        signal=signal,
                        publish_snapshot=False,
                    )
                except Exception as exc:
                    if not self._active:
                        raise
                    if (
                        refresh_epoch != self._dynamic_refresh_epoch
                        or _signal_is_aborted(signal)
                    ):
                        return
                    auth_error = str(exc) or type(exc).__name__
                    self._errors[provider_id] = auth_error
            if _signal_is_aborted(signal):
                # OAuth/API-key resolution owns the same refresh transaction.
                # Once it observes cancellation, do not invoke extension code
                # that could ignore the signal and mutate its model store.
                return
            if (
                not callable(base_callback)
                and not callable(extension_callback)
                and not callable(oauth_modifier)
            ):
                continue
            try:
                credential = _provider_credential_payload(
                    self._stored_credential(provider_id)
                )
                if credential is None:
                    resolved_api_key = self._resolved_api_key_auth.get(provider_id)
                    if isinstance(resolved_api_key, Mapping):
                        raw_auth = resolved_api_key.get("auth")
                        raw_auth = raw_auth if isinstance(raw_auth, Mapping) else {}
                        raw_env = resolved_api_key.get("env")
                        credential = {
                            "type": "api_key",
                            **(
                                {"key": str(raw_auth.get("api_key"))}
                                if raw_auth.get("api_key") is not None
                                else {}
                            ),
                            **(
                                {"env": dict(raw_env)}
                                if isinstance(raw_env, Mapping) and raw_env
                                else {}
                            ),
                        }
                if credential is None and auth_error is None:
                    continue
                refresh_context = _AttributeMapping(
                    **(
                        {"credential": _AttributeMapping(credential)}
                        if credential is not None
                        else {}
                    ),
                    store=_ProviderModelsStore(
                        self._models_store,
                        provider_id,
                    ),
                    allow_network=(
                        effective_allow_network if auth_error is None else False
                    ),
                    force=bool(force),
                    signal=signal,
                )
                # Pi composes refresh mutations in layer order. Native/builtin
                # refresh may update its own synchronous getModels view; an
                # extension refresh then publishes a replacement list.
                if callable(base_callback):
                    base_result = base_callback(refresh_context)
                    if inspect.isawaitable(base_result):
                        await base_result
                    self.assert_active()
                    if (
                        refresh_epoch != self._dynamic_refresh_epoch
                        or _signal_is_aborted(signal)
                    ):
                        return

                if callable(extension_callback):
                    refreshed = extension_callback(refresh_context)
                    if inspect.isawaitable(refreshed):
                        refreshed = await refreshed
                    self.assert_active()
                    if (
                        refresh_epoch != self._dynamic_refresh_epoch
                        or _signal_is_aborted(signal)
                    ):
                        return
                    if refreshed is None:
                        stored_models = await refresh_context.store.read()
                        refreshed = (
                            stored_models.get("models")
                            if isinstance(stored_models, Mapping)
                            else None
                        )
                    if isinstance(refreshed, Mapping):
                        refreshed = refreshed.get("models")
                    if refreshed is not None:
                        if not isinstance(refreshed, Sequence) or isinstance(
                            refreshed,
                            (str, bytes, bytearray),
                        ):
                            raise ProviderRegistrationError(
                                f"Provider {provider_id}: refresh_models must return a models array"
                            )
                        candidate = dict(current)
                        candidate["models"] = list(refreshed)
                        self._validate_registration(provider_id, candidate)
                        self._compose_models_strict(
                            provider_id,
                            extension_override=candidate,
                        )
                        self._refreshed_extension_models[provider_id] = tuple(
                            refreshed
                        )

                # Legacy extension OAuth projects models from the credential
                # captured by this refresh context. Reading storage during
                # getModels would make the synchronous catalog change behind
                # the runtime's back and diverge from Pi's composed closure.
                if (
                    callable(oauth_modifier)
                    and isinstance(credential, Mapping)
                    and credential.get("type") == "oauth"
                ):
                    self._oauth_model_credentials[provider_id] = dict(credential)
                else:
                    self._oauth_model_credentials.pop(provider_id, None)
            except Exception as exc:
                if not self._active:
                    raise
                if (
                    refresh_epoch != self._dynamic_refresh_epoch
                    or _signal_is_aborted(signal)
                ):
                    return
                if auth_error is None:
                    self._errors[provider_id] = str(exc) or type(exc).__name__
                continue
            if auth_error is None:
                self._errors.pop(provider_id, None)
            self._changed(
                provider_id,
                "refresh_models",
                refresh_snapshot=False,
            )

        if _signal_is_aborted(signal) or refresh_epoch != self._dynamic_refresh_epoch:
            return
        try:
            # Publish one global filtered snapshot only after every provider's
            # auth, base refresh, extension refresh, and OAuth projection has
            # completed. A failing filter clears the published availability;
            # stale models must not remain selectable after refresh failure.
            self._refresh_available_snapshot(apply_filters=True)
        except Exception as exc:
            self._record_availability_failure(exc)
            return
        self._availability_error = None

    def _changed(
        self,
        provider_id: str,
        action: str,
        *,
        refresh_snapshot: bool = True,
    ) -> None:
        if refresh_snapshot:
            # Registration/settings/auth mutations publish a callback-free
            # provisional view. Explicit auth/model refreshes then atomically
            # replace it with a filtered view after all callbacks succeed.
            self._refresh_available_snapshot(apply_filters=False)
        self._revision += 1
        callback = self._on_change
        if callback is not None:
            callback(self, provider_id, action)

    def _validate_registration(
        self,
        provider_id: str,
        config: Mapping[str, Any],
    ) -> None:
        _reject_noncanonical_fields(
            config,
            {
                "apiKey": "api_key",
                "authHeader": "auth_header",
                "baseUrl": "base_url",
                "filterModels": "filter_models",
                "refreshModels": "refresh_models",
            },
            source=f"Provider {provider_id}",
        )
        raw_auth = config.get("auth")
        if isinstance(raw_auth, Mapping):
            _reject_noncanonical_fields(
                raw_auth,
                {"apiKey": "api_key"},
                source=f"Provider {provider_id}.auth",
            )
        provider_api = (
            _normalize_api(config.get("api"))
            if config.get("api")
            else ""
        )
        headers = config.get("headers")
        if headers is not None and not isinstance(headers, Mapping):
            raise ProviderRegistrationError(
                f"Provider {provider_id}: headers must be an object"
            )
        if isinstance(headers, Mapping):
            for key, value in headers.items():
                _validated_header_pair(
                    key,
                    value,
                    source=f"Provider {provider_id}",
                )
        raw_auth_header = config.get("auth_header")
        if raw_auth_header is not None:
            _declared_boolean(
                raw_auth_header,
                field=f"Provider {provider_id}: auth_header",
            )
        refresh_models = config.get("refresh_models")
        if refresh_models is not None and not callable(refresh_models):
            raise ProviderRegistrationError(
                f"Provider {provider_id}: refresh_models must be callable"
            )
        filter_models = config.get("filter_models")
        if filter_models is not None and not callable(filter_models):
            raise ProviderRegistrationError(
                f"Provider {provider_id}: filter_models must be callable"
            )
        api_key_auth = _api_key_config(config)
        if api_key_auth is not None:
            resolve = self._auth_method(api_key_auth, "resolve")
            if not callable(resolve):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}: auth.api_key.resolve must be callable"
                )
            for method_name in ("check", "login"):
                method = self._auth_method(api_key_auth, method_name)
                if method is not None and not callable(method):
                    raise ProviderRegistrationError(
                        f"Provider {provider_id}: auth.api_key.{method_name} must be callable"
                    )
        oauth = _oauth_config(config)
        if oauth is not None:
            if isinstance(oauth, Mapping):
                _reject_noncanonical_fields(
                    oauth,
                    {
                        "getApiKey": "get_api_key",
                        "modifyModels": "modify_models",
                        "refreshToken": "refresh",
                        "toAuth": "to_auth",
                    },
                    source=f"Provider {provider_id}.auth.oauth",
                )
            login = self._auth_method(oauth, "login")
            refresh = self._auth_method(oauth, "refresh")
            to_auth = self._auth_method(oauth, "to_auth")
            get_api_key = self._auth_method(oauth, "get_api_key")
            if not callable(login):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}: OAuth login must be callable"
                )
            if not callable(refresh):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}: OAuth refresh must be callable"
                )
            if not callable(to_auth) and not callable(get_api_key):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}: OAuth requires to_auth or get_api_key"
                )
            modifier = self._auth_method(oauth, "modify_models")
            if modifier is not None and not callable(modifier):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}: OAuth modify_models must be callable"
                )
        provider_base_models = self._base_models(provider_id)
        base_models = {model.id: model for model in provider_base_models}
        raw_models = config.get("models")
        if raw_models is None:
            return
        if not isinstance(raw_models, Sequence) or isinstance(
            raw_models, (str, bytes, bytearray)
        ):
            raise ProviderRegistrationError(
                f"Provider {provider_id}: models must be an array"
            )
        provider_base_url = _clean_text(config.get("base_url"))
        for raw_model in raw_models:
            model = _as_mapping(raw_model, description="provider model")
            _reject_noncanonical_fields(
                model,
                {
                    "baseUrl": "base_url",
                    "contextWindow": "context_window",
                    "maxContextWindow": "max_context_window",
                    "maxTokens": "max_tokens",
                    "thinkingLevelMap": "thinking_level_map",
                },
                source=f"Provider {provider_id}.models",
            )
            model_id = _clean_text(model.get("id"))
            if not model_id:
                raise ProviderRegistrationError(
                    f"Provider {provider_id}: model id must not be empty"
                )
            defaults = base_models.get(model_id)
            api = (
                _normalize_api(model.get("api"))
                if model.get("api")
                else provider_api or (defaults.api if defaults is not None else "")
            )
            if not api:
                raise ProviderRegistrationError(
                    f'Provider {provider_id}, model {model_id}: no "api" specified. '
                    "Set it at provider or model level."
                )
            base_url = (
                _clean_text(model.get("base_url"))
                or provider_base_url
                or (defaults.base_url if defaults is not None else "")
            )
            if not base_url:
                raise ProviderRegistrationError(
                    f'Provider {provider_id}: "base_url" is required when defining custom models.'
                )
            model_headers = model.get("headers")
            if model_headers is not None and not isinstance(model_headers, Mapping):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}, model {model_id}: headers must be an object"
                )
            if isinstance(model_headers, Mapping):
                for key, value in model_headers.items():
                    _validated_header_pair(
                        key,
                        value,
                        source=f"Provider {provider_id}, model {model_id}",
                    )
            _declared_finite_number(
                model.get("max_context_window"),
                field=f"Provider {provider_id}, model {model_id}: max_context_window",
            )
            context_window = _declared_finite_number(
                model.get("context_window"),
                field=f"Provider {provider_id}, model {model_id}: context_window",
            )
            max_tokens = _declared_finite_number(
                model.get("max_tokens"),
                field=f"Provider {provider_id}, model {model_id}: max_tokens",
            )
            if (
                context_window is not None
                and max_tokens is not None
                and max_tokens > context_window
            ):
                raise ProviderRegistrationError(
                    f"Provider {provider_id}, model {model_id}: "
                    "max_tokens must not exceed context_window"
                )
            _declared_boolean(
                model.get("reasoning") if "reasoning" in model else None,
                field=f"Provider {provider_id}, model {model_id}: reasoning",
            )

    def register_provider(self, provider_id: str, config: Any) -> None:
        self.assert_active()
        clean_id = _clean_text(provider_id)
        if not clean_id:
            raise ProviderRegistrationError("Provider id must not be empty")
        incoming = _as_mapping(config, description=f"Provider {clean_id} config")
        self._validate_registration(clean_id, incoming)
        previous = self._extension_providers.get(clean_id, {})
        effective = dict(previous)
        for key, value in incoming.items():
            # None represents an omitted field during partial re-registration.
            if value is not None:
                effective[str(key)] = value
        self._validate_registration(clean_id, effective)
        self._bump_provider_generation(clean_id)
        self._refreshed_extension_models.pop(clean_id, None)
        self._extension_providers[clean_id] = effective
        self._resolved_oauth_auth.pop(clean_id, None)
        self._resolved_oauth_credential.pop(clean_id, None)
        self._oauth_model_credentials.pop(clean_id, None)
        self._resolved_api_key_auth.pop(clean_id, None)
        self._api_key_auth_status.pop(clean_id, None)
        self._errors.pop(clean_id, None)
        self._composition_errors.pop(clean_id, None)
        self._changed(clean_id, "register")

    def unregister_provider(self, provider_id: str) -> None:
        self.assert_active()
        clean_id = _clean_text(provider_id)
        self._bump_provider_generation(clean_id)
        removed = self._extension_providers.pop(clean_id, None)
        self._refreshed_extension_models.pop(clean_id, None)
        self._resolved_oauth_auth.pop(clean_id, None)
        self._resolved_oauth_credential.pop(clean_id, None)
        self._oauth_model_credentials.pop(clean_id, None)
        self._resolved_api_key_auth.pop(clean_id, None)
        self._api_key_auth_status.pop(clean_id, None)
        self._oauth_refresh_locks.pop(clean_id, None)
        self._api_key_resolution_locks.pop(clean_id, None)
        self._errors.pop(clean_id, None)
        self._composition_errors.pop(clean_id, None)
        if removed is not None:
            self._changed(clean_id, "unregister")

    def _base_models(self, provider_id: str) -> tuple[ModelDefinition, ...]:
        base = self._base_providers.get(provider_id)
        if not isinstance(base, Mapping):
            return ()
        models = base.get("models")
        return tuple(models) if isinstance(models, tuple) else ()

    def _model_from_config_definition(
        self,
        provider_id: str,
        definition: Mapping[str, Any],
        provider_config: Mapping[str, Any],
        defaults: ModelDefinition | None,
    ) -> ModelDefinition:
        model_id = _clean_text(definition.get("id"))
        api_value = definition.get("api") or provider_config.get("api") or (
            defaults.api if defaults is not None else ""
        )
        if not api_value:
            raise ProviderRegistrationError(
                f'Provider {provider_id}, model {model_id}: no "api" specified'
            )
        api = _normalize_api(api_value)
        base_url = (
            _clean_text(definition.get("base_url"))
            or _clean_text(provider_config.get("base_url"))
            or (defaults.base_url if defaults is not None else "")
        )
        if not base_url:
            raise ProviderRegistrationError(
                f'Provider {provider_id}: "base_url" is required when defining custom models'
            )
        context_window = (
            _finite_number(
                definition.get("context_window"),
                field=f"Provider {provider_id}, model {model_id}: context_window",
            )
            if definition.get("context_window") is not None
            else DEFAULT_CONTEXT_WINDOW
        )
        if context_window <= 0:
            raise ProviderRegistrationError(
                f"Provider {provider_id}, model {model_id}: invalid context_window"
            )
        if float(context_window).is_integer():
            context_window = int(context_window)
        max_context_window = context_window
        max_tokens = (
            _finite_number(
                definition.get("max_tokens"),
                field=f"Provider {provider_id}, model {model_id}: max_tokens",
            )
            if definition.get("max_tokens") is not None
            else DEFAULT_MAX_OUTPUT_TOKENS
        )
        if max_tokens <= 0:
            raise ProviderRegistrationError(
                f"Provider {provider_id}, model {model_id}: invalid max_tokens"
            )
        if max_tokens > context_window:
            raise ProviderRegistrationError(
                f"Provider {provider_id}, model {model_id}: "
                "max_tokens must not exceed context_window"
            )
        if float(max_tokens).is_integer():
            max_tokens = int(max_tokens)
        raw_input = definition.get("input")
        input_types = (
            tuple(str(item) for item in raw_input)
            if isinstance(raw_input, Sequence)
            and not isinstance(raw_input, (str, bytes, bytearray))
            else ("text",)
        )
        raw_cost = definition.get("cost")
        raw_thinking_map = definition.get("thinking_level_map")
        return ModelDefinition(
            provider=provider_id,
            id=model_id,
            name=_clean_text(definition.get("name")) or model_id,
            api=api,
            base_url=base_url,
            reasoning=_declared_boolean(
                definition.get("reasoning") if "reasoning" in definition else None,
                field=f"Provider {provider_id}, model {model_id}: reasoning",
            ),
            thinking_level_map=(
                dict(raw_thinking_map)
                if isinstance(raw_thinking_map, Mapping)
                else None
            ),
            input=input_types or ("text",),
            cost=(
                dict(raw_cost)
                if isinstance(raw_cost, Mapping)
                else {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
            ),
            context_window=context_window,
            context_window_source="models_json",
            context_window_verified=True,
            max_context_window=max_context_window,
            max_context_window_source="models_json",
            max_context_window_verified=True,
            max_tokens=max_tokens,
            max_output_tokens=max_tokens,
            max_output_tokens_source="models_json",
            max_output_tokens_verified=True,
        )

    def _apply_model_config(
        self,
        provider_id: str,
        base_models: tuple[ModelDefinition, ...],
        config: Mapping[str, Any] | None,
    ) -> tuple[ModelDefinition, ...]:
        if config is None:
            return base_models
        provider_base_url = _clean_text(config.get("base_url"))
        raw_models = config.get("models")
        raw_overrides = config.get("model_overrides")
        has_models = bool(
            isinstance(raw_models, Sequence)
            and not isinstance(raw_models, (str, bytes, bytearray))
            and len(raw_models) > 0
        )
        has_overrides = bool(
            isinstance(raw_overrides, Mapping) and len(raw_overrides) > 0
        )
        if (
            not has_models
            and not provider_base_url
            and config.get("headers") is None
            and not has_overrides
            and config.get("api_key") is None
            and "auth_header" not in config
        ):
            raise ProviderRegistrationError(
                f'Provider {provider_id}: must specify "base_url", "headers", '
                '"api_key", "auth_header", "model_overrides", or "models"'
            )
        models = [
            replace(
                model,
                base_url=provider_base_url or model.base_url,
            )
            for model in base_models
        ]
        if raw_models is None:
            return tuple(models)
        for raw_definition in raw_models:
            definition = _as_mapping(
                raw_definition,
                description=f"Provider {provider_id} models.json model",
            )
            model_id = _clean_text(definition.get("id"))
            existing_index = next(
                (index for index, item in enumerate(models) if item.id == model_id),
                -1,
            )
            defaults = models[existing_index] if existing_index >= 0 else None
            projected = self._model_from_config_definition(
                provider_id,
                definition,
                config,
                defaults,
            )
            if existing_index >= 0:
                models[existing_index] = projected
            else:
                models.append(projected)
        return tuple(models)


    def _configured_model_headers(
        self,
        provider_id: str,
        model_id: str,
        *,
        extension: Mapping[str, Any] | None,
    ) -> dict[str, str]:
        config = self._model_configs.get(provider_id, {})
        raw_overrides = config.get("model_overrides")
        override = (
            raw_overrides.get(model_id)
            if isinstance(raw_overrides, Mapping)
            else None
        )
        definition = _matching_model_definition(config.get("models"), model_id)
        extension_model = _matching_model_definition(
            extension.get("models") if isinstance(extension, Mapping) else None,
            model_id,
        )
        headers: dict[str, str] = {}
        for source in (override, definition, extension_model):
            raw_headers = source.get("headers") if isinstance(source, Mapping) else None
            if isinstance(raw_headers, Mapping):
                headers = _merge_headers(headers, raw_headers)
        return headers

    def _apply_model_overrides(
        self,
        provider_id: str,
        models: tuple[ModelDefinition, ...],
        *,
        extension: Mapping[str, Any] | None,
    ) -> tuple[ModelDefinition, ...]:
        config = self._model_configs.get(provider_id, {})
        raw_overrides = config.get("model_overrides")
        overrides = raw_overrides if isinstance(raw_overrides, Mapping) else {}
        result: list[ModelDefinition] = []
        for model in models:
            raw_override = overrides.get(model.id)
            override = (
                _as_mapping(
                    raw_override,
                    description=f"Provider {provider_id}, model {model.id} override",
                )
                if raw_override is not None
                else {}
            )
            thinking_level_map = model.thinking_level_map
            raw_thinking_map = override.get("thinking_level_map")
            if isinstance(raw_thinking_map, Mapping):
                thinking_level_map = {
                    **dict(thinking_level_map or {}),
                    **dict(raw_thinking_map),
                }
            raw_input = override.get("input")
            input_types = (
                tuple(str(item) for item in raw_input)
                if isinstance(raw_input, Sequence)
                and not isinstance(raw_input, (str, bytes, bytearray))
                else model.input
            )
            context_window = (
                _finite_number(
                    override.get("context_window"),
                    field=(
                        f"Provider {provider_id}, model {model.id}: "
                        "override context_window"
                    ),
                )
                if override.get("context_window") is not None
                else model.context_window
            )
            if float(context_window).is_integer():
                context_window = int(context_window)
            max_context_window = max(model.max_context_window, context_window)
            max_tokens = (
                _finite_number(
                    override.get("max_tokens"),
                    field=f"Provider {provider_id}, model {model.id}: override max_tokens",
                )
                if override.get("max_tokens") is not None
                else model.max_tokens
            )
            if float(max_tokens).is_integer():
                max_tokens = int(max_tokens)
            configured_headers = self._configured_model_headers(
                provider_id,
                model.id,
                extension=extension,
            )
            result.append(
                replace(
                    model,
                    name=(
                        _clean_text(override.get("name"))
                        if override.get("name") is not None
                        else model.name
                    ),
                    reasoning=(
                        _declared_boolean(
                            override.get("reasoning"),
                            field=f"Provider {provider_id}, model {model.id}: override reasoning",
                        )
                        if "reasoning" in override
                        else model.reasoning
                    ),
                    thinking_level_map=thinking_level_map,
                    input=input_types,
                    cost=_merge_model_cost(
                        model.cost,
                        override.get("cost")
                        if isinstance(override.get("cost"), Mapping)
                        else None,
                    ),
                    context_window=context_window,
                    context_window_source=(
                        "models_json_override"
                        if override.get("context_window") is not None
                        else model.context_window_source
                    ),
                    context_window_verified=(
                        True
                        if override.get("context_window") is not None
                        else model.context_window_verified
                    ),
                    max_context_window=max_context_window,
                    max_tokens=max_tokens,
                    max_output_tokens=max_tokens,
                    max_output_tokens_source=(
                        "models_json_override"
                        if override.get("max_tokens") is not None
                        else model.max_output_tokens_source
                    ),
                    max_output_tokens_verified=(
                        True
                        if override.get("max_tokens") is not None
                        else model.max_output_tokens_verified
                    ),
                    headers=_merge_headers(model.headers, configured_headers),
                )
            )
        return tuple(result)

    def _compose_models_strict(
        self,
        provider_id: str,
        *,
        extension_override: Any = _EXTENSION_OVERRIDE_UNSET,
        apply_oauth_modifier: bool = True,
        apply_model_overrides: bool = True,
    ) -> tuple[ModelDefinition, ...]:
        base_models = self._base_models(provider_id)
        extension = (
            self._extension_providers.get(provider_id)
            if extension_override is _EXTENSION_OVERRIDE_UNSET
            else extension_override
        )
        if (
            extension_override is _EXTENSION_OVERRIDE_UNSET
            and extension is not None
            and provider_id in self._refreshed_extension_models
        ):
            extension = {
                **extension,
                "models": list(self._refreshed_extension_models[provider_id]),
            }
        config = self._model_configs.get(provider_id)
        base_models = self._apply_model_config(provider_id, base_models, config)

        def finalize(
            models: tuple[ModelDefinition, ...],
        ) -> tuple[ModelDefinition, ...]:
            projected = (
                self._apply_oauth_model_modifier(provider_id, models)
                if apply_oauth_modifier and extension is not None
                else models
            )
            if not apply_model_overrides:
                return projected
            return self._apply_model_overrides(
                provider_id,
                projected,
                extension=extension,
            )

        if extension is None:
            return finalize(base_models)
        raw_models = extension.get("models")
        provider_base_url = _clean_text(extension.get("base_url"))
        if raw_models is None:
            if not provider_base_url:
                return finalize(base_models)
            return finalize(tuple(
                ModelDefinition(
                    **{
                        **model.__dict__,
                        "base_url": provider_base_url,
                    }
                )
                for model in base_models
            ))
        base_by_id = {model.id: model for model in base_models}
        provider_api = (
            _normalize_api(extension.get("api"))
            if extension.get("api")
            else ""
        )
        result: list[ModelDefinition] = []
        for raw_model in raw_models:
            model = _as_mapping(raw_model, description="provider model")
            model_id = _clean_text(model.get("id"))
            defaults = base_by_id.get(model_id)
            api = (
                _normalize_api(model.get("api"))
                if model.get("api")
                else provider_api or (defaults.api if defaults is not None else "")
            )
            base_url = (
                _clean_text(model.get("base_url"))
                or provider_base_url
                or (defaults.base_url if defaults is not None else "")
            )
            raw_input = model.get("input")
            input_types = (
                tuple(_clean_text(item) for item in raw_input if _clean_text(item))
                if isinstance(raw_input, Sequence)
                and not isinstance(raw_input, (str, bytes, bytearray))
                else defaults.input if defaults is not None else ("text",)
            )
            raw_context_window = model.get("context_window")
            declared_context_window = _declared_finite_number(
                raw_context_window,
                field=f"Provider {provider_id}, model {model_id}: context_window",
            )
            if declared_context_window is not None:
                context_window = declared_context_window
                context_window_source = "extension"
                context_window_verified = True
            elif defaults is not None and defaults.context_window > 0:
                context_window = defaults.context_window
                context_window_source = defaults.context_window_source
                context_window_verified = defaults.context_window_verified
            else:
                context_resolution = resolve_context_window_details(model_id)
                context_window = (
                    context_resolution.tokens
                    if context_resolution.source != "fallback"
                    else DEFAULT_CONTEXT_WINDOW
                )
                context_window_source = (
                    context_resolution.source
                    if context_resolution.source != "fallback"
                    else "extension_default"
                )
                context_window_verified = context_resolution.source != "fallback"
            raw_max_context_window = model.get("max_context_window")
            declared_max_context_window = _declared_finite_number(
                raw_max_context_window,
                field=f"Provider {provider_id}, model {model_id}: max_context_window",
            )
            if declared_max_context_window is not None:
                max_context_window = declared_max_context_window
                max_context_window_source = "extension"
                max_context_window_verified = True
            elif defaults is not None and defaults.max_context_window > 0:
                max_context_window = defaults.max_context_window
                max_context_window_source = defaults.max_context_window_source
                max_context_window_verified = defaults.max_context_window_verified
            else:
                max_context_window = context_window
                max_context_window_source = context_window_source
                max_context_window_verified = context_window_verified
            raw_max_tokens = model.get("max_tokens")
            declared_max_tokens = _declared_finite_number(
                raw_max_tokens,
                field=f"Provider {provider_id}, model {model_id}: max_tokens",
            )
            if declared_max_tokens is not None:
                max_tokens = declared_max_tokens
                max_output_tokens_source = "extension"
                max_output_tokens_verified = True
            elif defaults is not None and (
                defaults.max_tokens > 0 or defaults.max_output_tokens > 0
            ):
                max_tokens = defaults.max_tokens or defaults.max_output_tokens
                max_output_tokens_source = defaults.max_output_tokens_source
                max_output_tokens_verified = defaults.max_output_tokens_verified
            else:
                max_tokens = DEFAULT_MAX_OUTPUT_TOKENS
                max_output_tokens_source = "extension_default"
                max_output_tokens_verified = False
            if declared_max_tokens is not None and max_tokens > context_window:
                raise ProviderRegistrationError(
                    f"Provider {provider_id}, model {model_id}: "
                    "max_tokens must not exceed context_window"
                )
            if declared_max_tokens is None and max_tokens > context_window:
                max_tokens = context_window
                max_output_tokens_source = "context_window_clamp"
                max_output_tokens_verified = True
            raw_headers = model.get("headers")
            headers = (
                {str(key): str(value) for key, value in raw_headers.items()}
                if isinstance(raw_headers, Mapping)
                else {}
            )
            raw_cost = model.get("cost")
            raw_thinking_map = model.get("thinking_level_map")
            result.append(
                ModelDefinition(
                    provider=provider_id,
                    id=model_id,
                    name=_clean_text(model.get("name")) or model_id,
                    api=api,
                    base_url=base_url,
                    reasoning=_declared_boolean(
                        model.get("reasoning") if "reasoning" in model else None,
                        field=f"Provider {provider_id}, model {model_id}: reasoning",
                    ),
                    thinking_level_map=(
                        dict(raw_thinking_map)
                        if isinstance(raw_thinking_map, Mapping)
                        else None
                    ),
                    input=input_types or ("text",),
                    cost=dict(raw_cost) if isinstance(raw_cost, Mapping) else {},
                    context_window=context_window,
                    context_window_source=context_window_source,
                    context_window_verified=context_window_verified,
                    max_context_window=max_context_window,
                    max_context_window_source=max_context_window_source,
                    max_context_window_verified=max_context_window_verified,
                    max_tokens=max_tokens,
                    max_output_tokens=max_tokens,
                    max_output_tokens_source=max_output_tokens_source,
                    max_output_tokens_verified=max_output_tokens_verified,
                    reasoning_effort_levels=(
                        defaults.reasoning_effort_levels
                        if defaults is not None
                        else ()
                    ),
                    default_reasoning_effort=(
                        defaults.default_reasoning_effort
                        if defaults is not None
                        else ""
                    ),
                    default_reasoning_summary=(
                        defaults.default_reasoning_summary
                        if defaults is not None
                        else ""
                    ),
                    headers=headers,
                    extra=_extension_model_extra(model),
                )
            )
        return finalize(tuple(result))

    def _composed_models(
        self,
        provider_id: str,
        *,
        extension_override: Any = _EXTENSION_OVERRIDE_UNSET,
        apply_oauth_modifier: bool = True,
    ) -> tuple[ModelDefinition, ...]:
        try:
            models = self._compose_models_strict(
                provider_id,
                extension_override=extension_override,
                apply_oauth_modifier=apply_oauth_modifier,
                apply_model_overrides=True,
            )
        except Exception as exc:
            self._composition_errors[provider_id] = (
                str(exc) or type(exc).__name__
            )
            if extension_override is not _EXTENSION_OVERRIDE_UNSET:
                raise
            return ()
        self._composition_errors.pop(provider_id, None)
        return tuple(models)

    def _apply_oauth_model_modifier(
        self,
        provider_id: str,
        models: tuple[ModelDefinition, ...],
    ) -> tuple[ModelDefinition, ...]:
        extension_config = self._extension_providers.get(provider_id, {})
        oauth = _oauth_config(extension_config)
        credentials = (
            self._oauth_model_credentials.get(provider_id)
            if oauth is not None
            else None
        )
        modifier = _oauth_method(oauth, "modify_models") if oauth is not None else None
        if (
            credentials is None
            or credentials.get("type") != "oauth"
            or not callable(modifier)
        ):
            return models
        try:
            projected_credentials = {
                key: value
                for key, value in credentials.items()
                if key != "_minicode_auth"
            }
            modified = modifier(
                [model.to_extension_dict() for model in models],
                projected_credentials,
            )
            if not isinstance(modified, Sequence) or isinstance(modified, (str, bytes, bytearray)):
                raise ProviderRegistrationError("OAuth modify_models must return a models array")
            extension = dict(extension_config)
            extension["models"] = list(modified)
            self._validate_registration(provider_id, extension)
            return self._compose_models_strict(
                provider_id,
                extension_override=extension,
                apply_oauth_modifier=False,
                apply_model_overrides=False,
            )
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            self._errors[provider_id] = message
            # OAuth model projection is part of the selected provider's model
            # contract. Publishing the unmodified base list would silently
            # change the catalog after an extension callback failed.
            raise ProviderRegistrationError(
                f"Provider {provider_id}: OAuth model projection failed: {message}"
            ) from exc

    def get_models(self, provider_id: str | None = None) -> tuple[ModelDefinition, ...]:
        self.assert_active()
        if provider_id is not None:
            return self._composed_models(_clean_text(provider_id))
        provider_ids = list(
            dict.fromkeys(
                [
                    *self._base_providers,
                    *self._model_configs,
                    *self._extension_providers,
                ]
            )
        )
        return tuple(
            model
            for provider in provider_ids
            for model in self._composed_models(provider)
        )

    def get_model(self, provider_id: str, model_id: str) -> ModelDefinition | None:
        clean_model = _clean_text(model_id)
        return next(
            (
                model
                for model in self.get_models(_clean_text(provider_id))
                if model.id == clean_model
            ),
            None,
        )

    def _raw_api_key(self, provider_id: str) -> str:
        extension = self._extension_providers.get(provider_id, {})
        if "api_key" in extension:
            return str(extension.get("api_key") or "")
        config = self._model_configs.get(provider_id, {})
        if "api_key" in config:
            return str(config.get("api_key") or "")
        base = self._base_providers.get(provider_id, {})
        return str(base.get("api_key") or "")

    def _raw_provider_headers(self, provider_id: str) -> dict[str, Any]:
        base = self._base_provider(provider_id)
        raw_base_headers = _provider_member(base, "headers")
        headers = (
            dict(raw_base_headers)
            if isinstance(raw_base_headers, Mapping)
            else {}
        )
        config = self._model_configs.get(provider_id, {})
        raw_config_headers = config.get("headers")
        if isinstance(raw_config_headers, Mapping):
            headers = _merge_headers(headers, raw_config_headers)
        extension = self._extension_providers.get(provider_id, {})
        raw_headers = extension.get("headers")
        if isinstance(raw_headers, Mapping):
            headers = _merge_headers(headers, raw_headers)
        return headers

    def _auth_header_enabled(self, provider_id: str) -> bool:
        extension = self._extension_providers.get(provider_id, {})
        extension_value = extension.get("auth_header")
        if extension_value is not None:
            return _declared_boolean(
                extension_value,
                field=f"Provider {provider_id}: auth_header",
            )
        config = self._model_configs.get(provider_id, {})
        config_value = config.get("auth_header")
        return (
            _declared_boolean(
                config_value,
                field=f"Provider {provider_id}: auth_header",
            )
            if config_value is not None
            else False
        )

    def _resolve_provider_headers(
        self,
        provider_id: str,
        environment: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in self._raw_provider_headers(provider_id).items():
            name, raw_value = _validated_header_pair(
                key,
                value,
                source=f'Provider "{provider_id}"',
            )
            resolved = resolve_config_value(
                raw_value,
                description=f'provider "{provider_id}" header "{name}"',
                use_command_cache=True,
                environment=environment,
            )
            name, resolved = _validated_header_pair(
                name,
                resolved,
                source=f'Provider "{provider_id}"',
            )
            headers[name] = resolved
        return headers

    def _provider_auth_status_source(
        self,
        provider_id: str,
        raw_key: str,
    ) -> tuple[str | None, str | None]:
        extension = self._extension_providers.get(provider_id, {})
        extension_key_declared = "api_key" in extension
        if extension_key_declared:
            if raw_key.startswith("!"):
                return "fallback", None
            env_names = _config_value_env_names(raw_key)
            if env_names:
                return "environment", ", ".join(env_names)
            return "fallback", None
        config = self._model_configs.get(provider_id, {})
        config_key_declared = "api_key" in config
        if config_key_declared:
            if raw_key.startswith("!"):
                return "models_json_command", None
            env_names = _config_value_env_names(raw_key)
            if env_names:
                return "environment", ", ".join(env_names)
            return "models_json_key", None

        env_name = {
            "anthropic": "ANTHROPIC_API_KEY",
            "custom": "CUSTOM_API_KEY",
            "openai": "OPENAI_API_KEY",
        }.get(provider_id)
        if env_name and str(os.getenv(env_name) or "").strip() == raw_key:
            return "environment", env_name
        return "stored", None

    def has_configured_auth(self, provider_or_model: str | ModelDefinition) -> bool:
        provider_id = (
            provider_or_model.provider
            if isinstance(provider_or_model, ModelDefinition)
            else _clean_text(provider_or_model)
        )
        credentials = self._stored_credential(provider_id)
        if credentials is not None:
            credential_type = credentials.get("type")
            if credential_type == "oauth":
                return self._oauth_provider(provider_id) is not None
            if credential_type == "api_key":
                if self._api_key_provider(provider_id) is not None:
                    if provider_id in self._api_key_auth_status:
                        return self._api_key_auth_status[provider_id] is not None
                    if provider_id in self._resolved_api_key_auth:
                        return self._resolved_api_key_auth[provider_id] is not None
                return bool(str(credentials.get("key") or ""))
            return False
        if self._api_key_provider(provider_id) is not None:
            if provider_id in self._api_key_auth_status:
                return self._api_key_auth_status[provider_id] is not None
            if provider_id in self._resolved_api_key_auth:
                return self._resolved_api_key_auth[provider_id] is not None
        raw_key = self._raw_api_key(provider_id)
        if not raw_key:
            return False
        return _config_value_is_configured(raw_key)

    def _compute_available(
        self,
        provider_id: str | None = None,
        *,
        apply_filters: bool,
        apply_model_modifiers: bool,
    ) -> tuple[ModelDefinition, ...]:
        provider_ids = (
            (_clean_text(provider_id),)
            if provider_id is not None
            else tuple(provider.id for provider in self.get_providers())
        )
        available: list[ModelDefinition] = []
        for clean_id in provider_ids:
            models = self._composed_models(
                clean_id,
                apply_oauth_modifier=apply_model_modifiers,
            )
            extension = self._extension_providers.get(clean_id, {})
            # Deliberately base-provider only: a registered extension config must
            # not be able to cull the composed catalog. See
            # test_extension_filter_models_is_not_promoted_to_composed_provider_filter.
            filter_models = _provider_member(
                self._base_provider(clean_id),
                "filter_models",
            )
            if apply_filters and callable(filter_models):
                credential = _provider_credential_payload(
                    self._stored_credential(clean_id)
                )
                filtered = filter_models(
                    [model.to_extension_dict() for model in models],
                    credential,
                )
                if inspect.isawaitable(filtered):
                    close = getattr(filtered, "close", None)
                    if callable(close):
                        close()
                    raise ProviderRegistrationError(
                        f"Provider {clean_id}: filter_models must be synchronous"
                    )
                if filtered is not None:
                    if not isinstance(filtered, Sequence) or isinstance(
                        filtered,
                        (str, bytes, bytearray),
                    ):
                        raise ProviderRegistrationError(
                            f"Provider {clean_id}: filter_models must return a models array"
                        )
                    candidate = dict(extension)
                    candidate["models"] = list(filtered)
                    self._validate_registration(clean_id, candidate)
                    models = self._compose_models_strict(
                        clean_id,
                        extension_override=candidate,
                        apply_oauth_modifier=False,
                        apply_model_overrides=False,
                    )
            available.extend(models)
        return tuple(available)

    def _refresh_available_snapshot(self, *, apply_filters: bool) -> None:
        self._available_snapshot = self._compute_available(
            apply_filters=apply_filters,
            apply_model_modifiers=apply_filters,
        )

    def _record_availability_failure(self, exc: BaseException) -> None:
        self._availability_error = str(exc) or type(exc).__name__
        self._available_snapshot = ()

    def get_available(self, provider_id: str | None = None) -> tuple[ModelDefinition, ...]:
        try:
            available = self._compute_available(
                provider_id,
                apply_filters=True,
                apply_model_modifiers=True,
            )
        except Exception as exc:
            if provider_id is None:
                self._record_availability_failure(exc)
            raise
        if provider_id is None:
            self._available_snapshot = available
            self._availability_error = None
        return available

    def get_available_snapshot(self) -> tuple[ModelDefinition, ...]:
        return tuple(self._available_snapshot)

    def get_provider(self, provider_id: str) -> ProviderDefinition | None:
        clean_id = _clean_text(provider_id)
        models = self.get_models(clean_id)
        base = self._base_providers.get(clean_id, {})
        config = self._model_configs.get(clean_id, {})
        extension = self._extension_providers.get(clean_id, {})
        if (
            clean_id in self._composition_errors
            and clean_id not in self._base_providers
        ):
            return None
        if not models and not base and not config and not extension:
            return None
        return ProviderDefinition(
            id=clean_id,
            name=_clean_text(extension.get("name"))
            or _clean_text(config.get("name"))
            or _clean_text(base.get("name"))
            or clean_id,
            base_url=_clean_text(extension.get("base_url"))
            or _clean_text(config.get("base_url"))
            or _clean_text(base.get("base_url")),
            models=models,
            configured=self.has_configured_auth(clean_id),
            source=(
                "extension"
                if clean_id in self._extension_providers
                else "models_json"
                if clean_id in self._model_configs
                else "settings"
            ),
        )

    def get_providers(self) -> tuple[ProviderDefinition, ...]:
        provider_ids = tuple(
            dict.fromkeys(
                [
                    *self._base_providers,
                    *self._model_configs,
                    *self._extension_providers,
                ]
            )
        )
        return tuple(
            provider
            for provider_id in provider_ids
            if (provider := self.get_provider(provider_id)) is not None
        )

    def get_registered_provider_config(self, provider_id: str) -> dict[str, Any] | None:
        config = self._extension_providers.get(_clean_text(provider_id))
        return dict(config) if config is not None else None

    def get_registered_provider_ids(self) -> tuple[str, ...]:
        return tuple(self._extension_providers)

    def get_provider_auth_status(self, provider_id: str) -> dict[str, Any]:
        clean_id = _clean_text(provider_id)
        oauth = self._oauth_provider(clean_id)
        api_key_provider = self._api_key_provider(clean_id)
        credentials = self._stored_credential(clean_id)
        if credentials is not None and credentials.get("type") == "oauth":
            return {
                "configured": oauth is not None,
                "source": "oauth",
                "oauth_supported": oauth is not None,
            }
        if api_key_provider is not None:
            status = self._api_key_auth_status.get(clean_id)
            if clean_id in self._api_key_auth_status:
                return {
                    "configured": status is not None,
                    **(
                        {"source": str(status.get("source"))}
                        if isinstance(status, Mapping) and status.get("source")
                        else {}
                    ),
                    "oauth_supported": oauth is not None,
                }
            if credentials is not None and credentials.get("type") == "api_key":
                return {
                    "configured": bool(str(credentials.get("key") or "")),
                    "source": "stored",
                    "oauth_supported": oauth is not None,
                }
        raw_key = self._raw_api_key(clean_id)
        configured = bool(raw_key and _config_value_is_configured(raw_key))
        if not configured:
            return {"configured": False, "oauth_supported": oauth is not None}
        source, label = self._provider_auth_status_source(clean_id, raw_key)
        return {
            "configured": True,
            **({"source": source} if source else {}),
            **({"label": label} if label else {}),
            "oauth_supported": oauth is not None,
        }

    def resolve_provider_auth(self, provider_id: str) -> dict[str, Any] | None:
        """Resolve Pi's provider-level ``AuthResult`` without model headers."""

        self.assert_active()
        clean_id = _clean_text(provider_id)
        provider = self.get_provider(clean_id)
        if provider is None:
            return None
        credentials = self._stored_credential(clean_id)
        oauth = self._oauth_provider(clean_id)
        if credentials is not None and credentials.get("type") == "oauth":
            if oauth is None:
                return None
            cached_auth = self._resolved_oauth_auth.get(clean_id)
            cached_credential = self._resolved_oauth_credential.get(clean_id)
            current_credential = _provider_credential_payload(credentials)
            if (
                isinstance(cached_auth, Mapping)
                and isinstance(cached_credential, Mapping)
                and dict(cached_credential) == current_credential
            ):
                auth = dict(cached_auth)
                if isinstance(auth.get("headers"), Mapping):
                    auth["headers"] = dict(auth["headers"])
                return {"auth": auth, "source": "oauth"}
            self._resolved_oauth_auth.pop(clean_id, None)
            self._resolved_oauth_credential.pop(clean_id, None)
            stored_auth = credentials.get("_minicode_auth")
            if isinstance(stored_auth, Mapping):
                credential_environment = _normalize_provider_env(
                    credentials.get("env"),
                    source="OAuth credential",
                ) if credentials.get("env") is not None else {}
                auth = _normalize_model_auth(
                    stored_auth,
                    source="Stored OAuth auth",
                    allow_empty=True,
                )
                raw_headers = auth.get("headers")
                headers = (
                    {str(key): str(value) for key, value in raw_headers.items()}
                    if isinstance(raw_headers, Mapping)
                    else {}
                )
                configured_headers = self._resolve_provider_headers(
                    clean_id,
                    credential_environment,
                )
                headers = _merge_headers(headers, configured_headers)
                if self._auth_header_enabled(clean_id):
                    api_key = str(auth.get("api_key") or "")
                    if not api_key:
                        raise ProviderRegistrationError(
                            "auth_header requires a resolved API key"
                        )
                    headers = _merge_headers(
                        headers,
                        {"Authorization": f"Bearer {api_key}"},
                    )
                if headers:
                    auth["headers"] = headers
                else:
                    auth.pop("headers", None)
                return {"auth": auth, "source": "oauth"}
            to_auth = _oauth_method(oauth, "to_auth")
            if callable(to_auth):
                raise ProviderRegistrationError(
                    f'Provider "{clean_id}" requires asynchronous OAuth auth derivation; '
                    "refresh provider auth before constructing its adapter"
                )
            get_api_key = _oauth_method(oauth, "get_api_key")
            if not callable(get_api_key):
                return None
            api_key = get_api_key({
                key: value
                for key, value in credentials.items()
                if key != "_minicode_auth"
            })
            if inspect.isawaitable(api_key):
                raise ProviderRegistrationError("OAuth get_api_key must be synchronous")
            if not str(api_key or ""):
                raise ProviderRegistrationError(
                    "OAuth get_api_key returned no request credential"
                )
            credential_environment = _normalize_provider_env(
                credentials.get("env"),
                source="OAuth credential",
            ) if credentials.get("env") is not None else {}
            headers = self._resolve_provider_headers(
                clean_id,
                credential_environment,
            )
            if self._auth_header_enabled(clean_id):
                headers = _merge_headers(
                    headers,
                    {"Authorization": f"Bearer {api_key}"},
                )
            auth: dict[str, Any] = {"api_key": str(api_key or "")}
            if headers:
                auth["headers"] = headers
            return {"auth": auth, "source": "oauth"}
        if credentials is not None and credentials.get("type") not in {"api_key"}:
            return None
        if self._api_key_provider(clean_id) is not None:
            return self._resolve_modern_api_key_sync(clean_id)
        if credentials is not None and credentials.get("type") == "api_key":
            api_key = str(credentials.get("key") or "")
            environment = _normalize_provider_env(
                credentials.get("env"),
                source="Stored API-key credential",
            )
            headers = self._resolve_provider_headers(clean_id, environment)
            if self._auth_header_enabled(clean_id):
                if not api_key:
                    raise ProviderRegistrationError(
                        "auth_header requires a resolved API key"
                    )
                headers = _merge_headers(
                    headers,
                    {"Authorization": f"Bearer {api_key}"},
                )
            auth: dict[str, Any] = {}
            if api_key:
                auth["api_key"] = api_key
            if headers:
                auth["headers"] = headers
            return {
                "auth": auth,
                **({"env": environment} if environment else {}),
                "source": "stored credential",
            }
        raw_key = self._raw_api_key(clean_id)
        if not raw_key or not _config_value_is_configured(raw_key):
            return None
        api_key = resolve_config_value(
            raw_key,
            description=f'API key for provider "{clean_id}"',
            use_command_cache=True,
        )
        headers = self._resolve_provider_headers(clean_id)
        auth_header = self._auth_header_enabled(clean_id)
        if auth_header:
            headers = _merge_headers(
                headers,
                {"Authorization": f"Bearer {api_key}"},
            )
        environment = _resolved_config_environment([raw_key])
        status = self.get_provider_auth_status(clean_id)
        auth: dict[str, Any] = {"api_key": api_key}
        if headers:
            auth["headers"] = headers
        return {
            "auth": auth,
            **({"env": environment} if environment else {}),
            "source": (
                "stored credential"
                if status.get("source") == "stored"
                else "configured API key"
            ),
        }

    def get_error(self) -> str | None:
        errors: list[str] = []
        if self._config_error:
            errors.append(self._config_error)
        errors.extend(
            f'Provider "{provider}": {message}'
            for provider, message in self._composition_errors.items()
        )
        errors.extend(
            f'Provider "{provider}": {message}'
            for provider, message in self._errors.items()
            if provider not in self._composition_errors
            or message != self._composition_errors[provider]
        )
        if self._availability_error:
            errors.append(f"Availability refresh: {self._availability_error}")
        return "\n\n".join(errors) or None

    def resolve_adapter_spec(
        self,
        provider_id: str,
        model_id: str,
    ) -> ProviderAdapterSpec:
        self.assert_active()
        clean_provider = _clean_text(provider_id)
        model = self.get_model(clean_provider, model_id)
        if model is None:
            raise ProviderRegistrationError(
                f"Unknown model '{clean_provider}/{_clean_text(model_id)}'"
            )
        provider_auth = self.resolve_provider_auth(clean_provider)
        auth = (
            dict(provider_auth.get("auth"))
            if isinstance(provider_auth, Mapping)
            and isinstance(provider_auth.get("auth"), Mapping)
            else {}
        )
        api_key = str(auth.get("api_key") or "")
        raw_auth_headers = auth.get("headers")
        headers: dict[str, str] = (
            {str(key): str(value) for key, value in raw_auth_headers.items()}
            if isinstance(raw_auth_headers, Mapping)
            else {}
        )
        environment = _normalize_provider_env(
            provider_auth.get("env") if isinstance(provider_auth, Mapping) else None,
            source="Resolved provider auth",
        )
        if provider_auth is None:
            headers = self._resolve_provider_headers(clean_provider)
        # Header templates may reference environment variables. Those values
        # belong to the header they render, not to the provider's ``env``
        # contract, so merging header material into it would widen the
        # credential material returned by ModelRegistry.
        header_environment = dict(environment)
        header_environment.update(
            _resolved_config_environment(
                [
                    *self._raw_provider_headers(clean_provider).values(),
                    *model.headers.values(),
                ],
                environment,
            )
        )
        model_headers: dict[str, str] = {}
        for key, value in model.headers.items():
            name, raw_value = _validated_header_pair(
                key,
                value,
                source=f'Model "{clean_provider}/{model.id}"',
            )
            resolved = resolve_config_value(
                raw_value,
                description=(
                    f'model "{clean_provider}/{model.id}" header "{name}"'
                ),
                use_command_cache=True,
                environment=header_environment,
            )
            name, resolved = _validated_header_pair(
                name,
                resolved,
                source=f'Model "{clean_provider}/{model.id}"',
            )
            model_headers[name] = resolved
        headers = _merge_headers(headers, model_headers)
        auth_header = self._auth_header_enabled(clean_provider)
        if auth_header:
            if not api_key:
                raise ProviderRegistrationError(
                    "auth_header requires a resolved API key"
                )
            headers = _merge_headers(
                headers,
                {"Authorization": f"Bearer {api_key}"},
            )
        base = self._base_providers.get(clean_provider, {})
        extension_defined = (
            clean_provider in self._extension_providers
            or clean_provider in self._model_configs
        )
        base_model_metadata = get_provider_model_metadata(base, model.id)
        selected_reasoning_levels = (
            tuple(model.reasoning_effort_levels)
            if extension_defined
            else tuple(base_model_metadata["reasoning_effort_levels"])
        )
        resolved_base_url = (
            _clean_text(auth.get("base_url")) or model.base_url
        )
        request_model = (
            replace(model, base_url=resolved_base_url)
            if resolved_base_url != model.base_url
            else model
        )
        return ProviderAdapterSpec(
            provider_id=clean_provider,
            model_id=model.id,
            api=model.api,
            api_key=api_key,
            base_url=resolved_base_url,
            headers=headers,
            env=environment,
            proxy_mode=_clean_text(base.get("proxy_mode")) or "inherit",
            auth_header=auth_header,
            max_tokens=model.max_tokens,
            model=request_model,
            small_fast_model=_clean_text(base.get("small_fast_model")) or model.id,
            reasoning_effort=_clean_text(base.get("reasoning_effort")),
            responses_reasoning_summary=_clean_text(
                base.get("responses_reasoning_summary")
            )
            or "off",
            thinking_budget=(
                int(base.get("thinking_budget") or 0) or None
            ),
            prompt_cache_retention=_clean_text(
                base.get("prompt_cache_retention")
            ),
            reasoning_effort_levels=selected_reasoning_levels,
            context_window=model.context_window,
            context_window_source=model.context_window_source,
            context_window_verified=model.context_window_verified,
            max_context_window=model.max_context_window,
            max_context_window_source=model.max_context_window_source,
            max_context_window_verified=model.max_context_window_verified,
            max_output_tokens=model.max_output_tokens,
            max_output_tokens_source=model.max_output_tokens_source,
            max_output_tokens_verified=model.max_output_tokens_verified,
            default_reasoning_effort=model.default_reasoning_effort,
            default_reasoning_summary=model.default_reasoning_summary,
            extension_defined=extension_defined,
        )

    def provider_payload(
        self,
        provider_id: str,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        provider = self.get_provider(provider_id)
        if provider is None:
            return {}
        model = self.get_model(provider_id, model_id) if model_id else None
        base = self._base_providers.get(provider_id, {})
        extension_defined = (
            provider_id in self._extension_providers
            or provider_id in self._model_configs
        )
        metadata = (
            get_provider_model_metadata(base, model.id)
            if model is not None and not extension_defined
            else {
                "reasoning_effort_levels": [],
                "context_window": model.context_window if model is not None else 0,
                "context_window_source": (
                    model.context_window_source if model is not None else ""
                ),
                "context_window_verified": (
                    model.context_window_verified if model is not None else False
                ),
                "max_context_window": (
                    model.max_context_window if model is not None else 0
                ),
                "max_context_window_source": (
                    model.max_context_window_source if model is not None else ""
                ),
                "max_context_window_verified": (
                    model.max_context_window_verified if model is not None else False
                ),
                "max_output_tokens": (
                    model.max_output_tokens if model is not None else 0
                ),
                "max_output_tokens_source": (
                    model.max_output_tokens_source if model is not None else ""
                ),
                "max_output_tokens_verified": (
                    model.max_output_tokens_verified if model is not None else False
                ),
                "default_reasoning_effort": (
                    model.default_reasoning_effort if model is not None else ""
                ),
                "default_reasoning_summary": (
                    model.default_reasoning_summary if model is not None else ""
                ),
            }
        )
        levels = list(metadata["reasoning_effort_levels"])
        configured_effort = _clean_text(base.get("reasoning_effort")).lower()
        wire_api = model.api if model is not None else ""
        normalized_wire_api = {
            "anthropic-messages": "anthropic",
            "openai-responses": "responses",
            "openai-completions": "chat",
        }.get(wire_api, wire_api)
        effective_effort = normalize_reasoning_effort(
            model.id if model is not None else "",
            normalized_wire_api,
            configured_effort,
            levels,
            metadata["default_reasoning_effort"],
        )
        return {
            "provider_id": provider.id,
            "display_name": provider.name,
            "base_url": provider.base_url,
            "wire_api": wire_api,
            "proxy_mode": _clean_text(base.get("proxy_mode")) or "inherit",
            "models_source": provider.source,
            "reasoning_effort": configured_effort,
            "configured_reasoning_effort": configured_effort,
            "effective_reasoning_effort": effective_effort,
            "reasoning_effort_supported": bool(levels),
            "reasoning_effort_levels": levels,
            "context_window": metadata["context_window"],
            "context_window_source": str(metadata["context_window_source"]),
            "context_window_verified": bool(metadata["context_window_verified"]),
            "max_context_window": metadata["max_context_window"],
            "max_context_window_source": str(metadata["max_context_window_source"]),
            "max_context_window_verified": bool(metadata["max_context_window_verified"]),
            "max_output_tokens": metadata["max_output_tokens"],
            "max_output_tokens_source": str(metadata["max_output_tokens_source"]),
            "max_output_tokens_verified": bool(metadata["max_output_tokens_verified"]),
            "default_reasoning_effort": str(metadata["default_reasoning_effort"]),
            "default_reasoning_summary": str(metadata["default_reasoning_summary"]),
        }


__all__ = [
    "ModelDefinition",
    "ModelRuntime",
    "SUPPORTED_REASONING_LEVELS",
    "ProviderAdapterSpec",
    "ProviderDefinition",
    "ProviderRegistrationError",
    "UnsupportedProviderCapabilityError",
    "apply_model_thinking_level",
    "clear_api_key_cache",
    "clamp_model_thinking_level",
    "config_with_model_budget",
    "default_model_thinking_level",
    "model_thinking_levels",
    "resolve_config_value",
]

