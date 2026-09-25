"""External checks for the historical file-read snapshot failures."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


workspace = Path.cwd().resolve()
sys.path.insert(0, str(workspace))

import backend
import backend.tools.read_file as read_module
from backend.artifact.store import ArtifactStore
from backend.atomic_io import canonical_file_path_key
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.file_tools_common import content_hash
from backend.tools.read_file import ReadFileTool
from backend.workspace.file_state_cache import FileStateCache


if not Path(backend.__file__).resolve().is_relative_to(workspace):
    raise RuntimeError("oracle imported backend outside the task checkout")


class SnapshotOracle(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = FileStateCache()
        cache_patch = patch.object(read_module, "get_global_file_cache", lambda: self.cache)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        self.context = ToolExecutionContext(
            permission=PermissionContext(mode="bypass"), workspace_root=self.root,
        )
        self.reader = ReadFileTool(ArtifactStore(storage_dir=self.root / "artifacts"))

    def read(self, path: Path, **arguments):
        return asyncio.run(self.reader.execute({"file_path": path.name, **arguments}, self.context))

    def test_displayed_content_and_edit_hash_share_one_snapshot(self):
        for focused in (False, True):
            with self.subTest(focused=focused):
                path = self.root / "sample.txt"
                old = "before\noriginal tail\n"
                new = "after\na different tail\n"
                path.write_text(old, encoding="utf-8")
                self.cache.invalidate(path)
                self.context.metadata.clear()
                original_open = Path.open
                changed = False

                class ReplaceAfterRead:
                    def __init__(self, handle):
                        self.handle = handle

                    def __getattr__(self, name):
                        return getattr(self.handle, name)

                    def __iter__(self):
                        return iter(self.handle)

                    def __enter__(self):
                        self.handle.__enter__()
                        return self

                    def __exit__(self, *args):
                        nonlocal changed
                        result = self.handle.__exit__(*args)
                        if not changed:
                            changed = True
                            path.write_text(new, encoding="utf-8")
                        return result

                def racing_open(target, mode="r", *args, **kwargs):
                    handle = original_open(target, mode, *args, **kwargs)
                    return ReplaceAfterRead(handle) if target == path and mode.startswith("r") else handle

                arguments = {"start_line": 1, "end_line": 1} if focused else {}
                with patch.object(Path, "open", racing_open):
                    result = self.read(path, **arguments)
                key = canonical_file_path_key(path)
                if result.is_error:
                    self.assertIn("changed", result.content.lower())
                    self.assertIn("retry", result.content.lower())
                    self.assertNotIn(key, self.context.metadata.get("_read_file_hashes", {}))
                else:
                    snapshot = old if "before" in result.content else new
                    self.assertIn("before" if snapshot == old else "after", result.content)
                    self.assertEqual(self.context.metadata["_read_file_hashes"][key], content_hash(snapshot))
                cached = self.cache.get(path)
                if cached is not None:
                    self.assertEqual(cached.content, new)

    def test_atomic_replacement_invalidates_equal_size_equal_mtime_cache(self):
        path = self.root / "sample.txt"
        path.write_bytes(b"before")
        self.assertFalse(self.read(path).is_error)
        original = path.stat()
        replacement = self.root / "replacement.txt"
        replacement.write_bytes(b"after!")
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        replacement.replace(path)

        self.assertIsNone(self.cache.get(path))
        self.assertIn("after!", self.read(path).content)

    def test_non_text_range_cannot_bypass_file_size_limit(self):
        path = self.root / "oversized.pdf"
        path.write_bytes(b"x" * 64)
        with patch.object(read_module, "MAX_FILE_READ_BYTES", 32):
            result = self.read(path, start_line=1, end_line=1)
        self.assertTrue(result.is_error)
        self.assertIn("too large", result.content)

    def test_unicode_separator_stays_within_its_file_line(self):
        path = self.root / "sample.txt"
        text = "first\u2028still first\nsecond\n"
        path.write_text(text, encoding="utf-8")
        result = self.read(path, start_line=1, end_line=1)
        self.assertFalse(result.is_error, result.content)
        self.assertIn("still first", result.content)
        self.assertNotIn("second", result.content)
        self.assertEqual(
            self.context.metadata["_read_file_hashes"][canonical_file_path_key(path)],
            content_hash(text),
        )

    def test_partial_utf8_range_does_not_grant_whole_file_edit_hash(self):
        path = self.root / "sample.txt"
        path.write_bytes(b"first\n" + b"x" * 9000 + b"\xff")
        result = self.read(path, start_line=1, end_line=1)
        self.assertFalse(result.is_error, result.content)
        self.assertIn("first", result.content)
        self.assertNotIn(
            canonical_file_path_key(path), self.context.metadata.get("_read_file_hashes", {}),
        )


if __name__ == "__main__":
    unittest.main()
