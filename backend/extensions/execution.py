"""Bind shared extension handlers to the query that is invoking them."""
from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.agent.run_context import RunContext
    from backend.extensions.runtime import ExtensionRunner


class BoundExtensionRunner:
    def __init__(self, runner: ExtensionRunner, run_context: RunContext):
        self.runner = runner
        self.run_context = run_context

    def for_execution(self, run_context: RunContext) -> BoundExtensionRunner:
        return BoundExtensionRunner(self.runner, run_context)

    def __getattr__(self, name):
        value = getattr(self.runner, name)
        if not inspect.iscoroutinefunction(value):
            return value

        async def invoke(*args, **kwargs):
            # Each hook call enters and exits in the same asyncio task. A
            # generator consumer or cancellation drain cannot reset its token
            # from another task while the QueryEngine stream is suspended.
            with self.runner.runtime.execution_scope(self.run_context):
                return await value(*args, **kwargs)

        return invoke
