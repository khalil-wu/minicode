"""Regression tests for streamed tool-call argument accumulation.

DeepSeek (and other OpenAI-compatible gateways) stream a tool call as a first
delta carrying `id` + `name` with empty arguments, followed by deltas that carry
only argument fragments keyed by `index` (no `id`, no `name`). The accumulator
must merge these into a single call; an earlier id-keyed implementation split
them, producing a call with empty args (which then failed required-arg
validation, e.g. read_file "missing file_path").
"""

from backend.llm.openai_adapter import _ToolCallAccumulator


def test_split_id_then_args_only_deltas_merge():
    """First delta: id+name, empty args. Later deltas: args only, index only."""
    acc = _ToolCallAccumulator()
    acc.feed({"id": "call_abc", "function": {"name": "read_file", "arguments": ""}}, 0)
    acc.feed({"id": "", "function": {"arguments": '{"file'}}, 0)
    acc.feed({"id": "", "function": {"arguments": '_path": '}}, 0)
    acc.feed({"id": "", "function": {"arguments": '"src/foo.py"}'}}, 0)

    events = acc.finalize()
    assert len(events) == 1
    assert events[0].id == "call_abc"
    assert events[0].name == "read_file"
    assert events[0].arguments == {"file_path": "src/foo.py"}


def test_parallel_calls_on_distinct_indices():
    acc = _ToolCallAccumulator()
    acc.feed({"id": "c1", "function": {"name": "read_file", "arguments": ""}}, 0)
    acc.feed({"id": "c2", "function": {"name": "grep_files", "arguments": ""}}, 1)
    acc.feed({"id": "", "function": {"arguments": '{"file_path":"a.py"}'}}, 0)
    acc.feed({"id": "", "function": {"arguments": '{"pattern":"foo"}'}}, 1)

    events = acc.finalize()
    assert len(events) == 2
    assert events[0].arguments == {"file_path": "a.py"}
    assert events[1].arguments == {"pattern": "foo"}


def test_single_delta_full_args():
    acc = _ToolCallAccumulator()
    acc.feed({"id": "x", "function": {"name": "run_command", "arguments": '{"command":"ls"}'}}, 0)

    events = acc.finalize()
    assert len(events) == 1
    assert events[0].arguments == {"command": "ls"}


def test_complete_arguments_ignore_repeated_provider_snapshot():
    """A compatibility gateway may repeat a completed argument document."""
    acc = _ToolCallAccumulator()
    arguments = '{"content":"hello","file_path":"demo.txt"}'
    acc.feed({"id": "x", "function": {"name": "write_file", "arguments": arguments}}, 0)
    acc.feed({"id": "", "function": {"arguments": arguments}}, 0)

    events = acc.finalize()

    assert len(events) == 1
    assert events[0].arguments == {"content": "hello", "file_path": "demo.txt"}
    assert events[0].arguments_repaired is False


def test_incomplete_arguments_are_not_guessed_when_snapshot_repeats():
    acc = _ToolCallAccumulator()
    truncated = '{"content":"unfinished'
    acc.feed({"id": "x", "function": {"name": "write_file", "arguments": truncated}}, 0)
    acc.feed({"id": "", "function": {"arguments": truncated}}, 0)

    events = acc.finalize()

    assert len(events) == 1
    assert events[0].arguments_repaired is True


def test_index_reused_for_second_sequential_call():
    """A different id on an already-seen index is a new (sequential) call."""
    acc = _ToolCallAccumulator()
    acc.feed({"id": "a", "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'}}, 0)
    acc.feed({"id": "b", "function": {"name": "read_file", "arguments": '{"file_path":"b.py"}'}}, 0)

    events = acc.finalize()
    assert len(events) == 2
    assert {e.arguments["file_path"] for e in events} == {"a.py", "b.py"}
