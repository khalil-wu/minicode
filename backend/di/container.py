"""
Simple Dependency Injection Container for MiniCode.

Manages lifecycle of core components and reduces parameter passing.
"""
from __future__ import annotations

from typing import Any, Callable, TypeVar, Generic

T = TypeVar('T')


class DIContainer:
    """
    Simple dependency injection container.

    Usage:
        container = DIContainer()
        container.register('llm', lambda: AnthropicAdapter(...))
        container.register('tool_registry', lambda: ToolRegistry())

        llm = container.resolve('llm')
        registry = container.resolve('tool_registry')
    """

    def __init__(self):
        self._factories: dict[str, Callable[[], Any]] = {}
        self._singletons: dict[str, Any] = {}
        self._singleton_keys: set[str] = set()

    def register(
        self,
        key: str,
        factory: Callable[[], T],
        singleton: bool = True,
    ) -> None:
        """
        Register a factory for creating instances.

        Args:
            key: Unique identifier for the component
            factory: Callable that creates the component
            singleton: If True, instance is cached and reused
        """
        self._factories[key] = factory
        if singleton:
            self._singleton_keys.add(key)

    def resolve(self, key: str) -> Any:
        """
        Resolve a component by key.

        Args:
            key: Component identifier

        Returns:
            The component instance

        Raises:
            KeyError: If key not registered
        """
        if key not in self._factories:
            raise KeyError(f"Component '{key}' not registered in container")

        # Return cached singleton if available
        if key in self._singleton_keys:
            if key not in self._singletons:
                self._singletons[key] = self._factories[key]()
            return self._singletons[key]

        # Create new instance for non-singletons
        return self._factories[key]()

    def register_instance(self, key: str, instance: Any) -> None:
        """
        Register an existing instance (always singleton).

        Args:
            key: Component identifier
            instance: The instance to register
        """
        self._factories[key] = lambda: instance
        self._singletons[key] = instance
        self._singleton_keys.add(key)

    def clear(self) -> None:
        """Clear all registrations (useful for testing)."""
        self._factories.clear()
        self._singletons.clear()
        self._singleton_keys.clear()

    def has(self, key: str) -> bool:
        """Check if a component is registered."""
        return key in self._factories


# Global container instance
_global_container: DIContainer | None = None


def get_container() -> DIContainer:
    """Get the global DI container instance."""
    global _global_container
    if _global_container is None:
        _global_container = DIContainer()
    return _global_container


def reset_container() -> None:
    """Reset the global container (useful for testing)."""
    global _global_container
    if _global_container is not None:
        _global_container.clear()
    _global_container = None
