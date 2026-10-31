"""Git index and attribute helpers for byte-exact worktree recovery."""
from __future__ import annotations


RAW_ATTRIBUTES = b"* -text -filter -ident -working-tree-encoding\n"
IN_PROGRESS_GIT_STATE = frozenset({
    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
    "rebase-merge", "rebase-apply", "sequencer", "BISECT_LOG", "BISECT_START",
})


def index_entries(output: str) -> list[tuple[str, str, str]]:
    entries = []
    for item in output.split("\0"):
        if item:
            header, name = item.split("\t", 1)
            mode, sha, _stage = header.split(" ")
            entries.append((mode, sha, name))
    return entries


def object_alternate(path: str) -> bytes:
    """Encode a Git alternate path, including Git's C-style quoting."""
    raw = path.encode("utf-8")
    escaped = bytearray(b'"')
    for byte in raw:
        if byte in (ord('"'), ord("\\")):
            escaped.extend((ord("\\"), byte))
        elif byte < 32 or byte == 127:
            escaped.extend(f"\\{byte:03o}".encode("ascii"))
        else:
            escaped.append(byte)
    escaped.extend(b'"\n')
    return bytes(escaped)


def recovery_mode(mode: str, previous: str | None, *, filemode: bool, symlinks: bool) -> str:
    if mode in {"100644", "100755"}:
        if not symlinks and previous == "120000":
            return previous
        if not filemode and previous in {"100644", "100755"}:
            return previous
    return mode
