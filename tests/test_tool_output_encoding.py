# Both call sites converge on the shared decoder (backend.subprocesses).
from backend.subprocesses import decode_process_output as decode_git_output
from backend.subprocesses import decode_process_output as decode_search_output


def test_git_tool_output_decode_replaces_invalid_bytes() -> None:
    assert decode_git_output(b"ok\xff\n") == "ok\ufffd\n"
    assert decode_git_output(None) == ""


def test_search_tool_output_decode_replaces_invalid_bytes() -> None:
    assert decode_search_output(b"ripgrep\xff\n") == "ripgrep\ufffd\n"
    assert decode_search_output(None) == ""
