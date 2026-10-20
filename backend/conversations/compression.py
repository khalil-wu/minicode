"""Lossless single-frame zstd representation of cold conversation history."""
from __future__ import annotations

from contextlib import contextmanager
import io
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO, ContextManager, Iterator

import zstandard

from backend.atomic_io import file_mutation_locks
from .models import CONVERSATION_ID_PATTERN

_CHUNK_SIZE = 16 * 1024
_HISTORY_SUFFIXES = (".transcript.jsonl", ".snapshot.json", ".projection.jsonl")


class HistoryCompressionError(OSError):
    """A stored history frame cannot be decoded completely."""


def plain_history_path(path: Path) -> Path:
    return path.with_suffix("") if path.suffix == ".zst" else path


def compressed_history_path(path: Path) -> Path:
    return Path(str(plain_history_path(path)) + ".zst")


def is_history_path(path: Path) -> bool:
    return plain_history_path(path).name.endswith(_HISTORY_SUFFIXES)


def is_history_temporary(path: Path) -> bool:
    if not path.name.startswith(".") or path.suffix != ".tmp":
        return False
    if not CONVERSATION_ID_PATTERN.fullmatch(path.name[1:].partition(".")[0]):
        return False
    return any(path.name[1:].partition(marker)[0].endswith(_HISTORY_SUFFIXES)
        for marker in (".compress-", ".decompress-") if marker in path.name)


def history_exists(path: Path) -> bool:
    plain = plain_history_path(path)
    return plain.exists() or is_history_path(plain) and compressed_history_path(plain).exists()


def physical_history_path(path: Path) -> Path:
    plain = plain_history_path(path)
    return plain if not is_history_path(plain) or plain.exists() else compressed_history_path(plain)


def history_stat(path: Path) -> os.stat_result:
    return physical_history_path(path).stat()


def _decode_frame(path: Path, output: BinaryIO | None) -> int:
    """Verify EOF/checksum/content-size before a decoded representation is used."""
    try:
        with path.open("rb") as source:
            encoded = source.read(18)
            expected = zstandard.get_frame_parameters(encoded).content_size
            decoder = zstandard.ZstdDecompressor().decompressobj()
            total = 0
            while encoded:
                if decoder.eof:
                    raise HistoryCompressionError(f"Unexpected data after the history frame: {path}")
                decoded = decoder.decompress(encoded)
                if decoder.unused_data:
                    raise HistoryCompressionError(f"Unexpected data after the history frame: {path}")
                total += len(decoded)
                if output is not None:
                    output.write(decoded)
                encoded = source.read(_CHUNK_SIZE)
            if not decoder.eof:
                raise HistoryCompressionError(f"Compressed history frame is truncated: {path}")
            if expected != zstandard.CONTENTSIZE_UNKNOWN and total != expected:
                raise HistoryCompressionError(f"Compressed history source size does not match its frame: {path}")
            return total
    except zstandard.ZstdError as exc:
        raise HistoryCompressionError(f"Cannot decode compressed history {path}: {exc}") from exc


def read_history_bytes(path: Path) -> bytes:
    plain = plain_history_path(path)
    with file_mutation_locks([plain]):
        if plain.exists():
            return plain.read_bytes()
        output = io.BytesIO()
        _decode_frame(compressed_history_path(plain), output)
        return output.getvalue()


@contextmanager
def open_history_binary(path: Path) -> Iterator[BinaryIO]:
    """Read indexed byte ranges without materializing cold history on disk.

    Transcript indices address decoded bytes in ascending order. The codec's
    forward seek discards preceding output, preserving those exact offsets.
    Full snapshot/recovery reads use read_history_bytes for complete EOF proof.
    """
    plain = plain_history_path(path)
    with file_mutation_locks([plain]):
        if plain.exists():
            with plain.open("rb") as handle:
                yield handle
        else:
            compressed = compressed_history_path(plain)
            try:
                with compressed.open("rb") as source:
                    with zstandard.ZstdDecompressor().stream_reader(source) as handle:
                        yield handle
            except zstandard.ZstdError as exc:
                raise HistoryCompressionError(f"Cannot decode compressed history {compressed}: {exc}") from exc


def _preserve_metadata(path: Path, source_stat: os.stat_result) -> None:
    os.chmod(path, stat.S_IMODE(source_stat.st_mode))
    os.utime(path, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))


def materialize_history_for_append(path: Path) -> Path:
    """Publish plain before removing cold bytes under the repository writer."""
    plain = plain_history_path(path)
    compressed = compressed_history_path(plain)
    with file_mutation_locks([plain]):
        if plain.exists():
            # A crash may leave both representations. The existing plain file
            # is authoritative and must never be overwritten by an older frame.
            compressed.unlink(missing_ok=True)
            return plain
        if not compressed.exists():
            return plain
        source_stat = compressed.stat()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=plain.parent, prefix=f".{plain.name}.decompress-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                _decode_frame(compressed, output)
                output.flush()
                _preserve_metadata(temporary, source_stat)
                os.fsync(output.fileno())
            os.replace(temporary, plain)
            compressed.unlink()
            return plain
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _file_state(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode


def compress_history_file(path: Path, *, cutoff_ns: int, publication_lock: ContextManager[bool]) -> bool:
    """Encode/verify off the writer lock, then publish only the same cold file."""
    if path.suffix == ".zst":
        return False
    try:
        before = path.stat()
    except FileNotFoundError:
        return False
    if before.st_mtime_ns > cutoff_ns or not stat.S_ISREG(before.st_mode):
        return False
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.compress-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            with path.open("rb") as source:
                zstandard.ZstdCompressor(level=3, write_content_size=True, write_checksum=True).copy_stream(
                    source, output, size=before.st_size)
            output.flush()
            _decode_frame(temporary, None)
            os.fsync(output.fileno())
        with publication_lock as current_owner, file_mutation_locks([path]):
            if not current_owner:
                return False
            try:
                current = path.stat()
            except FileNotFoundError:
                return False
            if _file_state(current) != _file_state(before):
                return False
            # Keep unpublished temporary timestamps current during encoding.
            # The stale-temp cleaner owns this same conversation lock.
            with temporary.open("r+b") as staged:
                _preserve_metadata(temporary, before)
                os.fsync(staged.fileno())
            # Plain wins while both files exist, including after a crash between
            # these two publications. A later append clears the redundant frame.
            os.replace(temporary, compressed_history_path(path))
            path.unlink()
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
