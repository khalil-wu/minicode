"""Canonical MiniCode control plane for child-agent coordination.

This module owns identity, authorization, tree traversal, target resolution,
waiting, messaging, and interruption. Model providers and external protocol
translators cannot redefine these semantics.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping

from backend.agent.runtime import AgentRuntime
from backend.permissions.context import ToolExecutionContext
from backend.tools.subagent_context import resolve_agent_execution_profile
from backend.tools.subagent_runtime import require_runtime_from_context


AgentTreeScope = Literal[
    "children",
    "descendants",
    "tree",
    "conversation",
    "message",
]
AgentOperationName = Literal["spawn", "observe", "wait", "interrupt", "message"]
CHILD_AGENT_OPTIONS_METADATA_KEY = "_child_agent_options"


@dataclass(frozen=True, slots=True)
class AgentOperationBinding:
    """MiniCode's canonical operation contract for one agent tool."""

    tool_name: str
    operation: AgentOperationName
    scope: AgentTreeScope


_CANONICAL_AGENT_BINDINGS: dict[str, AgentOperationBinding] = {
    "task": AgentOperationBinding("task", "spawn", "children"),
    "task_status": AgentOperationBinding("task_status", "observe", "children"),
    "task_stop": AgentOperationBinding("task_stop", "interrupt", "children"),
    "send_message": AgentOperationBinding("send_message", "message", "message"),
}


def canonical_agent_operation(tool_name: str) -> AgentOperationBinding:
    """Resolve only MiniCode names; vendor aliases are not core capabilities."""

    name = str(tool_name or "").strip()
    try:
        return _CANONICAL_AGENT_BINDINGS[name]
    except KeyError as exc:
        raise ValueError(f"unknown MiniCode agent tool: {name!r}") from exc


def normalize_agent_task_name(raw: Any, *, required: bool) -> str:
    """Normalize one MiniCode task-path segment."""

    value = str(raw or "").strip()
    if not value:
        if required:
            raise ValueError("task_name is required")
        return ""
    if len(value) > 80:
        raise ValueError("task_name must be at most 80 characters")
    if value in {".", ".."} or any(character in value for character in "/\\"):
        raise ValueError("task_name must be one canonical path segment")
    if any(ord(character) < 32 for character in value):
        raise ValueError("task_name cannot contain control characters")
    return value


def normalize_agent_fork_turns(raw: Any, *, default: str) -> str:
    """Normalize MiniCode's context-fork selector."""

    fallback = str(default or "").strip().lower()
    if fallback not in {"none", "all"}:
        raise ValueError("fork_turns default must be 'none' or 'all'")
    value = str(raw if raw is not None else fallback).strip().lower() or fallback
    if value in {"none", "all"}:
        return value
    try:
        count = int(value)
    except ValueError as exc:
        raise ValueError(
            "fork_turns must be 'none', 'all', or a positive integer string"
        ) from exc
    if count <= 0:
        raise ValueError(
            "fork_turns must be 'none', 'all', or a positive integer string"
        )
    return str(count)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        rendered = to_dict()
        if isinstance(rendered, Mapping):
            return dict(rendered)
    try:
        return dict(vars(value))
    except (TypeError, AttributeError):
        return {}


def _agent_id(item: Mapping[str, Any]) -> str:
    return str(item.get("subagent_id") or "").strip()


def _parent_id(item: Mapping[str, Any]) -> str:
    return str(item.get("parent_run_id") or "").strip()


@dataclass(frozen=True, slots=True)
class AgentTarget:
    """One target resolved inside the caller's authorized agent scope."""

    subagent_id: str
    record: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentInterruptOutcome:
    """Canonical interruption result before user-facing rendering."""

    target: AgentTarget
    previous_status: str
    interrupt_status: str


class AgentControlPlane:
    """Canonical ownership and lifecycle projection for one tool caller.

    The caller identity comes from the live ``ToolExecutionContext`` rather
    than model-supplied arguments.  A root run owns its current descendant
    tree; for mailbox delivery it may also reach detached/background workers
    from older runs in the same conversation.  A child may reach its own
    descendants and, when its explicit execution profile permits messaging,
    its siblings/parent mailbox.
    """

    def __init__(
        self,
        context: ToolExecutionContext | None,
        *,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self.context = context
        self.runtime = runtime if runtime is not None else require_runtime_from_context(context)
        metadata = context.metadata if context and isinstance(context.metadata, dict) else {}
        self.metadata = metadata
        raw_actor_id = str(
            metadata.get("run_id")
            or metadata.get("agent_id")
            or (getattr(context, "task_id", "") if context is not None else "")
            or (getattr(context, "session_id", "") if context is not None else "")
        ).strip()
        self.has_actor_identity = bool(raw_actor_id)
        self.actor_id = raw_actor_id
        self.conversation_id = str(
            getattr(context, "conversation_id", "") if context is not None else ""
        ).strip()

    @property
    def actor_record(self) -> Any | None:
        return self.runtime.get_subagent(self.actor_id)

    @property
    def actor_is_subagent(self) -> bool:
        if self.actor_record is not None:
            return True
        # Pending/restored/evicted child contexts may temporarily have no live
        # AgentRecord.  Their persisted execution profile or permission source
        # must still attenuate them; absence from the live registry can never
        # promote a child into an unrestricted root coordinator.
        permission = self.context.permission if self.context is not None else None
        return resolve_agent_execution_profile(permission, self.metadata) is not None

    @property
    def actor_path(self) -> str:
        record = self.actor_record
        if record is not None:
            return str(getattr(record, "agent_path", "") or "").strip()
        run = self.runtime.get_run(self.actor_id)
        return str(getattr(run, "agent_path", "") or "").strip()

    @property
    def execution_profile(self) -> Any | None:
        permission = self.context.permission if self.context is not None else None
        return resolve_agent_execution_profile(permission, self.metadata)

    def _snapshot(self) -> dict[str, Any]:
        payload = self.runtime.list_runs(
            conversation_id=self.conversation_id,
            include_subagents=True,
        )
        return dict(payload) if isinstance(payload, Mapping) else {}

    def conversation_agents(self) -> list[dict[str, Any]]:
        """Return conversation-owned records without ever falling back global."""

        if not self.conversation_id:
            return self.descendants()
        return [
            dict(item)
            for item in self._snapshot().get("subagents", [])
            if isinstance(item, Mapping) and _agent_id(item)
        ]

    def _visible_agents(self) -> list[dict[str, Any]]:
        # Tree authorization is anchored by the caller's concrete parent edge,
        # so it remains safe even when an embedded/test host did not register a
        # top-level AgentRun record (and therefore cannot participate in the
        # conversation-indexed snapshot). Conversation-wide handoff uses the
        # stricter ``conversation_agents`` path instead.
        payload = self.runtime.list_runs(
            conversation_id="",
            include_subagents=True,
        )
        return [
            dict(item)
            for item in payload.get("subagents", [])
            if isinstance(item, Mapping) and _agent_id(item)
        ]

    def children(self) -> list[dict[str, Any]]:
        if not self.has_actor_identity:
            return []
        return [
            item
            for item in self._visible_agents()
            if _parent_id(item) == self.actor_id
        ]

    def descendants(self) -> list[dict[str, Any]]:
        """Traverse the runtime ownership graph, including pending children.

        Canonical paths remain useful labels, but authorization is based on
        parent ownership edges.  A string-prefix test is not an ownership
        boundary and legacy records may not yet carry a canonical path.
        """

        if not self.has_actor_identity:
            return []
        items = self._visible_agents()
        by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_parent[_parent_id(item)].append(item)

        found: list[dict[str, Any]] = []
        queue: deque[str] = deque([self.actor_id])
        visited_owners: set[str] = set()
        visited_agents: set[str] = set()
        while queue:
            owner_id = queue.popleft()
            if owner_id in visited_owners:
                continue
            visited_owners.add(owner_id)
            for item in by_parent.get(owner_id, []):
                subagent_id = _agent_id(item)
                if not subagent_id or subagent_id in visited_agents:
                    continue
                visited_agents.add(subagent_id)
                found.append(item)
                queue.append(subagent_id)
        return found

    def root_run_id(self) -> str:
        """Return the concrete root run that owns the caller's Agent tree.

        MiniCode shares one control plane across a root and all of its children.
        It stores that topology as parent edges, where a nested child's
        parent may itself be a subagent.  Walking those edges is therefore the
        authorization boundary; conversation membership alone is too broad
        because a conversation can contain detached workers from older turns.
        """

        if not self.has_actor_identity:
            return ""
        if not self.actor_is_subagent:
            return self.actor_id

        items = self._visible_agents()
        by_id = {_agent_id(item): item for item in items if _agent_id(item)}
        current = self.actor_id
        visited: set[str] = set()
        while current and current not in visited and len(visited) < 128:
            visited.add(current)
            item = by_id.get(current)
            if item is None:
                record = self.runtime.get_subagent(current)
                item = _mapping(record) if record is not None else None
            if not item:
                return ""
            parent = _parent_id(item)
            if not parent:
                return ""
            if parent in by_id or self.runtime.get_subagent(parent) is not None:
                current = parent
                continue
            # The first owner that is not itself a subagent is the root run.
            # Embedded hosts may not have materialized an AgentRunRecord yet,
            # so the explicit ownership edge is authoritative even then.
            return parent
        return ""

    def tree_agents(self) -> list[dict[str, Any]]:
        """Return every subagent in the caller's concrete root tree."""

        root_run_id = self.root_run_id()
        if not root_run_id:
            return []
        items = self._visible_agents()
        by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_parent[_parent_id(item)].append(item)

        found: list[dict[str, Any]] = []
        queue: deque[str] = deque([root_run_id])
        visited_owners: set[str] = set()
        visited_agents: set[str] = set()
        while queue:
            owner_id = queue.popleft()
            if owner_id in visited_owners:
                continue
            visited_owners.add(owner_id)
            for item in by_parent.get(owner_id, []):
                subagent_id = _agent_id(item)
                if not subagent_id or subagent_id in visited_agents:
                    continue
                visited_agents.add(subagent_id)
                found.append(item)
                queue.append(subagent_id)
        return found

    def root_tree_record(self) -> dict[str, Any] | None:
        root_run_id = self.root_run_id()
        record = self.runtime.get_run(root_run_id) if root_run_id else None
        rendered = _mapping(record) if record is not None else {}
        if rendered:
            return rendered
        if not root_run_id:
            return None
        return {
            "run_id": root_run_id,
            "conversation_id": self.conversation_id,
            "role": "main",
            "status": "running",
            "agent_path": root_run_id,
            "mailbox_epoch": 0,
        }

    @staticmethod
    def _public_path_segment(item: Mapping[str, Any]) -> str:
        for key in ("task_name", "task_id", "teammate_name"):
            value = str(item.get(key) or "").strip()
            if value and value not in {".", ".."} and not any(
                character in value for character in "/\\"
            ):
                return value
        stored_path = str(item.get("agent_path") or "").strip().rstrip("/\\")
        if stored_path:
            candidate = stored_path.replace("\\", "/").rsplit("/", 1)[-1]
            if candidate and candidate not in {".", ".."}:
                return candidate
        return _agent_id(item) or "agent"

    def public_tree_paths(self) -> dict[str, str]:
        """Project opaque runtime paths onto canonical ``/root/...`` names."""

        root_run_id = self.root_run_id()
        items = self.tree_agents()
        by_id = {_agent_id(item): item for item in items if _agent_id(item)}
        cache: dict[str, str] = {}

        def render(subagent_id: str, stack: frozenset[str] = frozenset()) -> str:
            if subagent_id in cache:
                return cache[subagent_id]
            item = by_id.get(subagent_id)
            if item is None or subagent_id in stack:
                return f"/root/{subagent_id}" if subagent_id else "/root"
            parent_id = _parent_id(item)
            if parent_id == root_run_id:
                parent_path = "/root"
            elif parent_id in by_id:
                parent_path = render(parent_id, stack | {subagent_id})
            else:
                parent_path = "/root"
            path = f"{parent_path}/{self._public_path_segment(item)}"
            cache[subagent_id] = path
            return path

        for subagent_id in by_id:
            render(subagent_id)
        return cache

    def public_tree_path(self, item: Mapping[str, Any] | str) -> str:
        subagent_id = (
            str(item or "").strip()
            if isinstance(item, str)
            else _agent_id(item)
        )
        return self.public_tree_paths().get(subagent_id, "")

    def actor_public_tree_path(self) -> str:
        if not self.actor_is_subagent:
            return "/root"
        return self.public_tree_path(self.actor_id) or "/root"

    @staticmethod
    def _validate_agent_name(value: str) -> None:
        if not value:
            raise ValueError("agent_name must not be empty")
        if value == "root":
            raise ValueError("agent_name `root` is reserved")
        if value in {".", ".."}:
            raise ValueError(f"agent_name `{value}` is reserved")
        if "/" in value:
            raise ValueError("agent_name must not contain `/`")
        if not all(
            character.isascii()
            and (character.islower() or character.isdigit() or character == "_")
            for character in value
        ):
            raise ValueError(
                "agent_name must use only lowercase letters, digits, and underscores"
            )

    def normalize_public_tree_path(self, raw_path: Any, *, strict: bool = False) -> str:
        """Resolve a relative or canonical MiniCode agent path.

        Relative references are descendants of the current agent path.
        ``.``/``..`` and filesystem-style sibling traversal are rejected;
        siblings are addressed canonically as ``/root/sibling``.
        """

        try:
            value = str(raw_path or "")
            if not value:
                raise ValueError("agent path must not be empty")
            if value == "/root":
                return value
            if value.startswith("/"):
                stripped = value[1:]
                parts = stripped.split("/")
                if not parts or parts[0] != "root":
                    raise ValueError("absolute agent paths must start with `/root`")
                if stripped.endswith("/"):
                    raise ValueError("absolute agent path must not end with `/`")
                for part in parts[1:]:
                    self._validate_agent_name(part)
                return value

            if value.endswith("/"):
                raise ValueError("relative agent path must not end with `/`")
            for part in value.split("/"):
                self._validate_agent_name(part)
            return f"{self.actor_public_tree_path().rstrip('/')}/{value}"
        except ValueError:
            if strict:
                raise
            return ""

    def siblings(self) -> list[dict[str, Any]]:
        actor = self.actor_record
        parent_run_id = str(getattr(actor, "parent_run_id", "") or "").strip()
        if not parent_run_id:
            return []
        return [
            item
            for item in self._visible_agents()
            if _parent_id(item) == parent_run_id and _agent_id(item) != self.actor_id
        ]

    def agents_for_scope(self, scope: AgentTreeScope) -> list[dict[str, Any]]:
        if scope == "children":
            return self.children()
        if scope == "descendants":
            return self.descendants()
        if scope == "tree":
            return self.tree_agents()
        if scope == "conversation":
            return self.conversation_agents()
        if scope != "message":
            raise ValueError(f"unsupported agent tree scope: {scope}")

        # Root runs coordinate conversation-owned detached/background workers
        # across turn boundaries. Children coordinate only within their root
        # agent tree, including sibling branches, never across conversations.
        if not self.actor_is_subagent:
            merged: dict[str, dict[str, Any]] = {}
            for item in (*self.conversation_agents(), *self.descendants()):
                merged[_agent_id(item)] = item
            return list(merged.values())
        return self.tree_agents()

    def select_agents(
        self,
        *,
        scope: AgentTreeScope,
        include_completed: bool = True,
        background_only: bool = False,
        path_prefix: str = "",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Apply shared scope and lifecycle filters before any result codec.

        Every result projection must select from the same authorized ownership
        graph. Keeping this step here prevents a renderer from falling back to a
        conversation-global or process-global list.
        """

        bounded_limit = None if limit is None else max(0, int(limit))
        if bounded_limit == 0:
            return []
        prefix = str(path_prefix or "").strip().casefold()
        selected: list[dict[str, Any]] = []
        for item in self.agents_for_scope(scope):
            status = str(item.get("status") or "running")
            if not include_completed and status not in {"pending", "running"}:
                continue
            if background_only and not bool(item.get("background", False)):
                continue
            if prefix:
                labels = (
                    str(item.get("agent_path") or "").casefold(),
                    str(item.get("task_name") or "").casefold(),
                    str(item.get("task_id") or "").casefold(),
                )
                if not any(label.startswith(prefix) for label in labels if label):
                    continue
            selected.append(dict(item))
            if bounded_limit is not None and len(selected) >= bounded_limit:
                break
        return selected

    @staticmethod
    def _target_names(item: Mapping[str, Any]) -> set[str]:
        names = {
            str(item.get(key) or "").strip().casefold()
            for key in (
                "subagent_id",
                "agent_path",
                "task_name",
                "task_id",
                "teammate_name",
                "teammate_id",
            )
        }
        return {name for name in names if name}

    def resolve_target(
        self,
        raw_target: Any,
        *,
        scope: AgentTreeScope,
    ) -> AgentTarget | None:
        target = str(raw_target or "")
        if scope != "tree":
            target = target.strip()
        if not target:
            return None
        folded = target.casefold()
        candidates = self.agents_for_scope(scope)
        by_id = {_agent_id(item): item for item in candidates if _agent_id(item)}

        direct = by_id.get(target)
        if direct is not None:
            return AgentTarget(target, direct)

        if scope == "tree":
            public_target = self.normalize_public_tree_path(target)
            if public_target and public_target != "/root":
                public_paths = self.public_tree_paths()
                for subagent_id, public_path in public_paths.items():
                    if public_path.casefold() == public_target.casefold():
                        item = by_id.get(subagent_id)
                        if item is not None:
                            return AgentTarget(subagent_id, item)
            # Opaque persisted paths remain accepted for recovery and
            # compatibility, but leaf-name fallback is intentionally omitted:
            # duplicate task names in separate branches must not route by
            # latest-wins registry state.
            matches = [
                item
                for item in candidates
                if folded
                in {
                    str(item.get("agent_path") or "").strip().casefold(),
                    str(item.get("subagent_id") or "").strip().casefold(),
                }
            ]
            if matches:
                item = matches[0]
                return AgentTarget(_agent_id(item), item)
            return None

        # Resolve human labels only after applying the caller's scope.  The
        # runtime name registry is latest-wins across a conversation; using it
        # before authorization can select an identically named agent in a
        # different subtree.
        matches = [item for item in candidates if folded in self._target_names(item)]
        if matches:
            matches.sort(
                key=lambda item: (
                    int(item.get("started_at") or 0),
                    _agent_id(item),
                ),
                reverse=True,
            )
            item = matches[0]
            return AgentTarget(_agent_id(item), item)

        resolve = getattr(self.runtime, "resolve_subagent_name", None)
        resolved_id = str(resolve(target) if callable(resolve) else "").strip()
        resolved = by_id.get(resolved_id)
        if resolved is not None:
            return AgentTarget(resolved_id, resolved)
        return None

    def owns(self, subagent_id: str, *, recursive: bool) -> bool:
        scope: AgentTreeScope = "descendants" if recursive else "children"
        return self.resolve_target(subagent_id, scope=scope) is not None

    async def wait_for_any(
        self,
        subagent_ids: list[str],
        *,
        timeout_seconds: float,
    ) -> str | None:
        clean_ids = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in subagent_ids
                if str(value or "").strip()
            )
        )
        if not clean_ids:
            return None
        return await self.runtime.wait_for_any_subagent(
            clean_ids,
            max(0.0, float(timeout_seconds)),
        )

    async def wait_for_one(
        self,
        subagent_id: str,
        *,
        timeout_seconds: float,
    ) -> bool:
        wait = getattr(self.runtime, "wait_for_subagent", None)
        if not callable(wait):
            return False
        return bool(
            await wait(
                str(subagent_id or "").strip(),
                max(0.0, float(timeout_seconds)),
            )
        )

    def subagent_snapshot(
        self,
        subagent_id: str,
        *,
        include_result: bool,
    ) -> dict[str, Any] | None:
        snapshot = self.runtime.get_subagent_snapshot(
            str(subagent_id or "").strip(),
            include_result=bool(include_result),
        )
        return dict(snapshot) if isinstance(snapshot, Mapping) else None

    def forget_subagent_result(self, subagent_id: str) -> None:
        self.runtime.forget_subagent_result(str(subagent_id or "").strip())

    def activity_cursor(self) -> int:
        cursor = getattr(self.runtime, "agent_activity_cursor", None)
        return max(0, int(cursor() if callable(cursor) else 0))

    async def wait_for_activity(
        self,
        subagent_ids: list[str],
        *,
        after_seq: int,
        timeout_seconds: float,
        kinds: Iterable[str] | None = None,
    ) -> dict[str, Any] | None:
        wait = getattr(self.runtime, "wait_for_agent_activity", None)
        if not callable(wait):
            completed = await self.wait_for_any(
                subagent_ids,
                timeout_seconds=timeout_seconds,
            )
            return (
                {
                    "seq": after_seq,
                    "kind": "completed",
                    "agent_ids": [completed],
                    "conversation_id": self.conversation_id,
                    "status": "completed",
                }
                if completed
                else None
            )
        activity = await wait(
            subagent_ids,
            conversation_id=self.conversation_id,
            after_seq=after_seq,
            timeout=max(0.0, float(timeout_seconds)),
            kinds=kinds,
        )
        return dict(activity) if isinstance(activity, Mapping) else None

    def interrupt(
        self,
        target: AgentTarget,
        *,
        reason: str = "interrupted",
    ) -> AgentInterruptOutcome:
        previous_status = str(target.record.get("status") or "unknown")
        return AgentInterruptOutcome(
            target=target,
            previous_status=previous_status,
            interrupt_status=str(
                self.runtime.cancel_subagent_task(
                    target.subagent_id,
                    reason=reason,
                )
            ),
        )

    def can_use_operation(self, operation: AgentOperationName) -> bool:
        """Authorize one MiniCode agent action for the current caller."""

        if not self.actor_is_subagent:
            return True
        profile = self.execution_profile
        if operation == "message":
            # Durable transcripts created before execution profiles existed
            # retain their historical parent/sibling mailbox behavior.
            return bool(profile is None or profile.message_coordination)
        if profile is None:
            return False
        if operation == "spawn":
            return str(profile.delegation or "none") != "none"
        if operation in {"observe", "wait", "interrupt"}:
            return bool(profile.agent_lifecycle)
        return False

    def can_message_parent(self) -> bool:
        if not self.actor_is_subagent:
            return False
        return self.can_use_operation("message")

    def resolve_message_target(
        self,
        raw_target: Any,
        *,
        scope: AgentTreeScope = "message",
    ) -> AgentTarget | None:
        if scope == "tree":
            rendered_target = str(raw_target or "")
            if (
                self.normalize_public_tree_path(rendered_target) == "/root"
                or rendered_target == self.root_run_id()
            ):
                root = self.root_tree_record()
                return AgentTarget("parent", root) if root is not None else None
        if not self.actor_is_subagent:
            return self.resolve_target(raw_target, scope=scope)
        if not self.can_use_operation("message"):
            return None
        return self.resolve_target(raw_target, scope=scope)

    def resolve_persisted_message_target(self, raw_target: Any) -> AgentTarget | None:
        """Authorize an exact durable id when its in-memory projection was evicted."""

        target = str(raw_target or "").strip()
        load_persisted = getattr(self.runtime, "load_persisted_subagent", None)
        if not target or not callable(load_persisted):
            return None
        record = load_persisted(target)
        rendered = _mapping(record) if record is not None else {}
        if not rendered or _agent_id(rendered) != target:
            return None
        if self.actor_is_subagent and not self.can_use_operation("message"):
            return None

        parent_id = _parent_id(rendered)
        visited: set[str] = {target}
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            parent_record = self.runtime.get_subagent(parent_id)
            if parent_record is None:
                parent_record = load_persisted(parent_id)
            parent_payload = _mapping(parent_record) if parent_record is not None else {}
            if not parent_payload:
                break
            parent_id = _parent_id(parent_payload)
        root_run_id = parent_id
        root_run = self.runtime.get_run(root_run_id) if root_run_id else None
        root_conversation_id = str(
            getattr(root_run, "conversation_id", "") or ""
        ).strip()
        if not root_conversation_id or root_conversation_id != self.conversation_id:
            return None
        if self.actor_is_subagent and root_run_id != self.root_run_id():
            return None
        return AgentTarget(target, rendered)

    def can_queue_unknown_message_target(self, raw_target: Any) -> bool:
        """Compatibility gate for pre-registration durable mailbox delivery.

        A message can arrive just before a worker's start record is committed.
        Keep that narrow behavior for opaque generated ids only;
        human labels and canonical paths must resolve through the owned tree.
        """

        target = str(raw_target or "").strip()
        if not target.startswith("subagent-") or "/" in target or "\\" in target:
            return False
        if not self.actor_is_subagent:
            return self.has_actor_identity
        return self.can_use_operation("message")

    def record_for(self, subagent_id: str) -> dict[str, Any] | None:
        target = str(subagent_id or "").strip()
        if not target:
            return None
        for item in self._visible_agents():
            if _agent_id(item) == target:
                return item
        record = self.runtime.get_subagent(target)
        rendered = _mapping(record) if record is not None else {}
        return rendered or None


__all__ = [
    "AgentControlPlane",
    "AgentInterruptOutcome",
    "AgentTarget",
    "AgentTreeScope",
    "normalize_agent_fork_turns",
    "normalize_agent_task_name",
]
