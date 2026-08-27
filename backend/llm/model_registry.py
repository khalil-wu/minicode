"""MiniCode model-registry facade and adapter construction exports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.services.llm_adapter_factory import (
    build_provider_adapter,
    create_session_llm,
)
from backend.llm.model_runtime import (
    ModelRuntime,
    clear_api_key_cache,
)
from backend.llm.provider_contracts import (
    ModelDefinition,
    ProviderAdapterSpec,
    ProviderDefinition,
    ProviderRegistrationError,
    UnsupportedProviderCapabilityError,
)


class ModelRegistry:
    """Synchronous provider registry exposed to MiniCode extensions."""

    def __init__(self, runtime: ModelRuntime) -> None:
        self.runtime = runtime

    async def refresh(self) -> None:
        await self.runtime.refresh_dynamic_models()

    def get_error(self) -> str | None:
        return self.runtime.get_error()

    def get_all(self) -> list[ModelDefinition]:
        return list(self.runtime.get_models())

    def get_available(self) -> list[ModelDefinition]:
        return list(self.runtime.get_available_snapshot())

    def find(self, provider: str, model_id: str) -> ModelDefinition | None:
        return self.runtime.get_model(provider, model_id)

    def has_configured_auth(self, model: ModelDefinition | str) -> bool:
        return self.runtime.has_configured_auth(model)

    async def get_api_key_and_headers(self, model: ModelDefinition) -> dict[str, Any]:
        try:
            await self.runtime.refresh_provider_auth(model.provider)
            spec = self.runtime.resolve_adapter_spec(model.provider, model.id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "api_key": spec.api_key or None,
            "headers": dict(spec.headers) or None,
            **({"env": dict(spec.env)} if spec.env else {}),
        }

    def get_provider_auth_status(self, provider: str) -> dict[str, Any]:
        return self.runtime.get_provider_auth_status(provider)

    def get_provider(self, provider: str) -> ProviderDefinition | None:
        return self.runtime.get_provider(provider)

    def get_provider_display_name(self, provider: str) -> str:
        definition = self.runtime.get_provider(provider)
        return definition.name if definition is not None else provider

    async def get_provider_auth(self, provider: str) -> dict[str, Any] | None:
        await self.runtime.refresh_provider_auth(provider)
        return self.runtime.resolve_provider_auth(provider)

    async def get_api_key_for_provider(self, provider: str) -> str | None:
        await self.runtime.refresh_provider_auth(provider)
        result = self.runtime.resolve_provider_auth(provider)
        auth = result.get("auth") if isinstance(result, dict) else None
        return (
            str(auth.get("api_key") or "") or None
            if isinstance(auth, dict)
            else None
        )

    def is_using_oauth(self, model: ModelDefinition) -> bool:
        return self.runtime.is_using_oauth(model)

    def register_provider(
        self,
        provider: str,
        config: Mapping[str, Any],
    ) -> None:
        if not isinstance(provider, str) or not provider.strip():
            raise ProviderRegistrationError("Provider id must be a non-empty string")
        if not isinstance(config, Mapping):
            raise ProviderRegistrationError("Provider config must be an object")
        self.runtime.register_provider(provider, config)

    def unregister_provider(self, provider: str) -> None:
        self.runtime.unregister_provider(provider)

    def get_registered_provider_config(self, provider: str) -> dict[str, Any] | None:
        return self.runtime.get_registered_provider_config(provider)

    def get_registered_provider_ids(self) -> tuple[str, ...]:
        return self.runtime.get_registered_provider_ids()


__all__ = [
    "ModelDefinition",
    "ModelRegistry",
    "ModelRuntime",
    "ProviderAdapterSpec",
    "ProviderDefinition",
    "ProviderRegistrationError",
    "UnsupportedProviderCapabilityError",
    "build_provider_adapter",
    "clear_api_key_cache",
    "create_session_llm",
]
