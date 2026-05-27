"""Agent-loop domain policies package.

This package contains the four named policy interfaces and their default
implementations that the agent loop core consults for domain-specific
decisions. Each policy is defined in its own module:

- realtime_search: RealtimePrefetchPlan, RealtimeSearchPolicy, DefaultRealtimeSearchPolicy
- reflection: ReflectionDecision, ReflectionPolicy, DefaultReflectionPolicy
- stream_retry: StreamRetryDecision, StreamRetryPolicy, DefaultStreamRetryPolicy
- grounded_reply: GroundedReplyPolicy, DefaultGroundedReplyPolicy
"""

from __future__ import annotations

# Placeholder imports — the actual classes are populated by later tasks (3–6).
# Using try/except so the package is importable even while modules are stubs.

try:
    from .realtime_search import (
        RealtimePrefetchPlan,
        RealtimeSearchPolicy,
        DefaultRealtimeSearchPolicy,
    )
except (ImportError, SyntaxError):
    pass

try:
    from .reflection import (
        ReflectionDecision,
        ReflectionPolicy,
        DefaultReflectionPolicy,
    )
except (ImportError, SyntaxError):
    pass

try:
    from .stream_retry import (
        StreamRetryDecision,
        StreamRetryPolicy,
        DefaultStreamRetryPolicy,
    )
except (ImportError, SyntaxError):
    pass

try:
    from .grounded_reply import (
        GroundedReplyPolicy,
        DefaultGroundedReplyPolicy,
    )
except (ImportError, SyntaxError):
    pass

__all__ = [
    # Realtime search
    "RealtimePrefetchPlan",
    "RealtimeSearchPolicy",
    "DefaultRealtimeSearchPolicy",
    # Reflection
    "ReflectionDecision",
    "ReflectionPolicy",
    "DefaultReflectionPolicy",
    # Stream retry
    "StreamRetryDecision",
    "StreamRetryPolicy",
    "DefaultStreamRetryPolicy",
    # Grounded reply
    "GroundedReplyPolicy",
    "DefaultGroundedReplyPolicy",
]
