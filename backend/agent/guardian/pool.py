"""Source reusable trunk and ephemeral forks from the last committed review."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

from backend.agent.guardian.contract import GuardianReviewError
from backend.agent.guardian.context import GuardianCheckpointRecoveryNeeded, GuardianReviewerReuseKey, checkpoint_in_history


@dataclass
class _Reviewer:
    key: GuardianReviewerReuseKey
    runtime: Any
    busy: bool = False
    cursor: Any | None = None
    committed_context: Any | None = None
    completed_reviews: int = 0
    fresh_parent_checkpoint: bool = False
    transcript_source: str | None = None
    reviewer_compaction_count: int = 0


class GuardianReviewerPool:
    def __init__(self, runtime_factory):
        self.runtime_factory = runtime_factory
        self.trunk: _Reviewer | None = None
        self.selection_lock = asyncio.Lock()
        self.active_tasks: set[asyncio.Task] = set()
        self.closed = False

    def _spawn(self, arguments):
        # This is Source's fallible reviewer startup boundary. Startup errors
        # are reported as review failures, never as approval or a human answer.
        try:
            return self.runtime_factory(**arguments)
        except GuardianReviewError:
            raise
        except Exception as exc:
            raise GuardianReviewError("prompt_build", str(exc)) from exc

    async def review(self, *, key: GuardianReviewerReuseKey, runtime_arguments: dict[str, Any], prompt_factory, cancel_event, parent_context,
        deadline: float, force_fresh: bool = False, preserve_parent_history: bool = False):
        task = asyncio.current_task()
        self.active_tasks.add(task)
        reviewer = None
        ephemeral = False
        old = None
        try:
            async with self.selection_lock:
                if self.closed:
                    raise GuardianReviewError("cancelled")
                trunk = self.trunk
                if trunk is not None and preserve_parent_history:
                    key = replace(key, parent_history_version=trunk.key.parent_history_version)
                if trunk is not None and (trunk.key != key or force_fresh) and not trunk.busy:
                    self.trunk = None
                    old = trunk
                    trunk = None
                if trunk is None:
                    reviewer = _Reviewer(key, self._spawn(runtime_arguments), busy=True,
                        fresh_parent_checkpoint=runtime_arguments.get("parent_checkpoint") is not None)
                    self.trunk = reviewer
                elif not trunk.busy and trunk.key == key and not force_fresh:
                    reviewer = trunk
                    reviewer.busy = True
                else:
                    ephemeral = True
                    seed = trunk.committed_context if trunk.key == key and not force_fresh else None
                    if runtime_arguments.get("parent_checkpoint") is not None and trunk.cursor is None:
                        # Source rejects a compacted fork's lossy transcript
                        # in favor of the captured parent checkpoint.
                        seed = None
                    fork_arguments = {**runtime_arguments, "seed_context": seed}
                    reviewer = _Reviewer(key, self._spawn(fork_arguments), busy=True,
                        cursor=trunk.cursor if seed is not None else None,
                        completed_reviews=trunk.completed_reviews if seed is not None else 0,
                        fresh_parent_checkpoint=seed is None and runtime_arguments.get("parent_checkpoint") is not None,
                        transcript_source=trunk.transcript_source if seed is not None else None,
                        reviewer_compaction_count=trunk.reviewer_compaction_count if seed is not None else 0)
                    reviewer.runtime.completed_reviews = reviewer.completed_reviews
            if old is not None:
                await old.runtime.aclose()
                old = None
            checkpoint = runtime_arguments.get("parent_checkpoint")
            fresh = reviewer.fresh_parent_checkpoint
            reviewer.fresh_parent_checkpoint = False
            try:
                source = "loaded_parent_checkpoint" if checkpoint_in_history(reviewer.runtime.context_builder._history, checkpoint) else "retained"
                previous_compactions = reviewer.runtime.context_builder._compaction_count
                cursor = reviewer.cursor if reviewer.transcript_source in {None, source} and previous_compactions == reviewer.reviewer_compaction_count else None
                reviewer.runtime.context_builder.followup_reminder = reviewer.completed_reviews > 0
                prompt = prompt_factory(cursor, reviewer.runtime.context_builder)
                text = await reviewer.runtime.run(prompt, cancel_event=cancel_event, parent_context=parent_context, deadline=deadline)
            except BaseException as error:
                # A failed/dropped attempt cannot publish partially admitted
                # evidence as a completed fork snapshot.
                if not ephemeral:
                    async with self.selection_lock:
                        if self.trunk is reviewer:
                            self.trunk = None
                if not ephemeral:
                    await reviewer.runtime.aclose()
                if isinstance(error, GuardianReviewError) and error.kind == "input_budget" and checkpoint is not None and not fresh:
                    raise GuardianCheckpointRecoveryNeeded() from error
                if isinstance(error, GuardianCheckpointRecoveryNeeded) and fresh:
                    raise GuardianReviewError("input_budget", "the fresh parent checkpoint and minimum review context exceed the reviewer input budget") from error
                raise
            if not ephemeral:
                reviewer.cursor = prompt.cursor if reviewer.runtime.context_builder._compaction_count == previous_compactions else None
                reviewer.reviewer_compaction_count = reviewer.runtime.context_builder._compaction_count
                reviewer.completed_reviews += 1
                reviewer.committed_context = reviewer.runtime.committed_context()
                reviewer.transcript_source = source
            return text
        finally:
            if old is not None:
                await old.runtime.aclose()
            if reviewer is not None:
                reviewer.busy = False
                if ephemeral:
                    await reviewer.runtime.aclose()
            self.active_tasks.discard(task)

    async def aclose(self):
        self.closed = True
        current = asyncio.current_task()
        tasks = tuple(task for task in self.active_tasks if task is not current and not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self.selection_lock:
            trunk, self.trunk = self.trunk, None
        if trunk is not None:
            await trunk.runtime.aclose()
