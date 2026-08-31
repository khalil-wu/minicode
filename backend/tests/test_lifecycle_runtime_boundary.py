from types import SimpleNamespace

from backend.agent.lifecycle_observer import (
    resolve_lifecycle_runtime,
)
from backend.agent.query_engine import AgentSession
from backend.agent.run_context import RunContext


def test_resolver_prefers_explicit_run_context() -> None:
    session_runtime = object()
    metadata_runtime = object()

    resolved = resolve_lifecycle_runtime(
        session_context=SimpleNamespace(
            lifecycle_runtime=session_runtime,
        ),
        run_context=RunContext(lifecycle_runtime=metadata_runtime),
    )

    assert resolved is metadata_runtime


def test_resolver_uses_explicit_run_context_when_session_has_no_runtime() -> None:
    runtime = object()

    resolved = resolve_lifecycle_runtime(
        session_context=SimpleNamespace(lifecycle_runtime=None),
        run_context=RunContext(lifecycle_runtime=runtime),
    )

    assert resolved is runtime


def test_resolver_returns_none_without_a_typed_owner() -> None:
    assert resolve_lifecycle_runtime() is None


def test_agent_session_exposes_canonical_lifecycle_runtime_field() -> None:
    assert "lifecycle_runtime" in AgentSession.__dataclass_fields__
    assert "extension_runner" not in AgentSession.__dataclass_fields__
