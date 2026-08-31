"""Trust boundary for executable extensions.

MiniCode performs a two-pass project-trust decision before loading project
code.  MiniCode's Python runtime keeps the same important invariant: a module
is checked (and its path canonicalised) *before* ``exec_module`` runs.  A
project extension is not executable merely because it is discoverable; the
caller must explicitly mark the project trusted.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .types import ExtensionScope, ExtensionTrustError


def _canonical(path: Path) -> Path:
    """Resolve a path without silently accepting a missing target."""

    return path.expanduser().resolve(strict=False)


def _case_key(path: Path) -> str:
    # Match the host filesystem: Windows folds case, POSIX does not. Folding
    # unconditionally merges two executable roots that are distinct on Linux.
    return os.path.normcase(str(path))


@dataclass(frozen=True)
class TrustDecision:
    allowed: bool
    scope: ExtensionScope
    trusted: bool
    reason: str = ""
    resolved_path: Path | None = None


@dataclass
class ExtensionTrustPolicy:
    """Explicit policy used by the loader before importing executable code.

    ``project_root`` is intentionally separate from ``cwd``.  A caller may
    run in a subdirectory while still treating the repository root as the
    trust boundary.  ``allowed_roots`` can contain managed/user extension
    roots; a path outside all roots is classified as ``external`` and denied
    unless ``allow_external`` is explicitly enabled.
    """

    cwd: Path | str | None = None
    project_root: Path | str | None = None
    allowed_roots: Iterable[Path | str] = field(default_factory=tuple)
    managed_roots: Iterable[Path | str] = field(default_factory=tuple)
    user_roots: Iterable[Path | str] = field(default_factory=tuple)
    builtin_roots: Iterable[Path | str] = field(default_factory=tuple)
    temporary_roots: Iterable[Path | str] = field(default_factory=tuple)
    project_trusted: bool = False
    allow_project: bool | None = None
    allow_external: bool = False
    allow_user: bool = True
    allow_managed: bool = True
    allow_builtin: bool = True
    allow_temporary: bool = True
    reject_symlinks: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "_cwd", _canonical(Path(self.cwd or Path.cwd())))
        object.__setattr__(
            self,
            "_project_root",
            _canonical(Path(self.project_root or self.cwd or Path.cwd())),
        )
        object.__setattr__(self, "_allowed", self._normalise(self.allowed_roots))
        object.__setattr__(self, "_managed", self._normalise(self.managed_roots))
        object.__setattr__(self, "_user", self._normalise(self.user_roots))
        object.__setattr__(self, "_builtin", self._normalise(self.builtin_roots))
        object.__setattr__(self, "_temporary", self._normalise(self.temporary_roots))

    @staticmethod
    def _normalise(values: Iterable[Path | str]) -> tuple[Path, ...]:
        result: list[Path] = []
        seen: set[str] = set()
        for raw in values:
            try:
                path = _canonical(Path(raw))
            except (TypeError, ValueError, OSError):
                continue
            key = _case_key(path)
            if key in seen:
                continue
            seen.add(key)
            result.append(path)
        return tuple(result)

    @property
    def effective_project_allowed(self) -> bool:
        return (
            self.project_trusted
            if self.allow_project is None
            else bool(self.allow_project)
        )

    def _has_symlink_component(self, path: Path) -> bool:
        if not self.reject_symlinks:
            return False
        # Inspect components from the nearest existing ancestor down to the
        # file.  ``Path.resolve`` alone would hide a symlink escape.
        current = path.expanduser().absolute()
        missing: list[str] = []
        while not current.exists() and current != current.parent:
            missing.append(current.name)
            current = current.parent
        while True:
            try:
                if current.is_symlink():
                    return True
                stat_result = current.lstat()
                file_attributes = int(
                    getattr(stat_result, "st_file_attributes", 0) or 0
                )
                reparse_flag = int(
                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    or 0
                )
                if reparse_flag and file_attributes & reparse_flag:
                    # Windows junctions and other reparse points are not
                    # consistently reported by pathlib.is_symlink().  MiniCode's
                    # materialized plugin boundary rejects them for the same
                    # path-redirection reason.
                    return True
            except OSError:
                return True
            if current == current.parent:
                break
            current = current.parent
        # The missing tail cannot contain a symlink yet.  Keep the variable to
        # make the intent explicit and avoid a future implementation assuming
        # ``exists`` was required for every component.
        del missing
        return False

    def _under_any(self, path: Path, roots: tuple[Path, ...]) -> bool:
        return any(path.is_relative_to(root) for root in roots)

    def classify(
        self, path: Path | str, *, source_scope: ExtensionScope | None = None
    ) -> ExtensionScope:
        candidate = _canonical(Path(path))
        if source_scope is not None:
            return source_scope
        if self._under_any(candidate, self._builtin):
            return "builtin"
        if self._under_any(candidate, self._managed):
            return "managed"
        if self._under_any(candidate, self._user):
            return "user"
        if self._under_any(candidate, self._temporary):
            return "temporary"
        if candidate.is_relative_to(self._project_root) or candidate.is_relative_to(self._cwd):
            return "project"
        if self._under_any(candidate, tuple(self._allowed)):
            # Explicit allowed roots are user-style roots unless a more
            # specific category was supplied above.
            return "user"
        return "external"

    def check(
        self,
        path: Path | str,
        *,
        source_scope: ExtensionScope | None = None,
        require_exists: bool = True,
    ) -> TrustDecision:
        raw = Path(path).expanduser()
        resolved = _canonical(raw)
        if source_scope is not None and source_scope not in {
            "builtin",
            "managed",
            "user",
            "project",
            "temporary",
            "external",
        }:
            return TrustDecision(
                False,
                "external",
                False,
                f"invalid extension scope: {source_scope}",
                resolved,
            )
        scope = self.classify(resolved, source_scope=source_scope)

        if source_scope is not None and source_scope in {
            "builtin",
            "managed",
            "user",
            "temporary",
        }:
            expected_roots = {
                "builtin": self._builtin,
                "managed": self._managed,
                "user": (*self._user, *self._allowed),
                "temporary": self._temporary,
            }[source_scope]
            # Provenance is assigned by the host, but it must still be backed
            # by a concrete configured root.  An empty root list cannot mean
            # "trust any path claiming to be builtin/user/managed".
            if not expected_roots or not self._under_any(resolved, expected_roots):
                return TrustDecision(
                    False,
                    scope,
                    False,
                    f"declared extension scope '{source_scope}' does not match its path",
                    resolved,
                )
        if source_scope == "project" and not (
            resolved.is_relative_to(self._project_root)
            or resolved.is_relative_to(self._cwd)
        ):
            return TrustDecision(
                False,
                scope,
                False,
                "declared project extension is outside the project root",
                resolved,
            )

        if require_exists and not resolved.is_file():
            return TrustDecision(
                False,
                scope,
                False,
                f"extension path does not exist or is not a file: {resolved}",
                resolved,
            )
        if self._has_symlink_component(raw):
            return TrustDecision(
                False,
                scope,
                False,
                f"extension path contains a symbolic link: {raw}",
                resolved,
            )

        allowed = {
            "builtin": self.allow_builtin,
            "managed": self.allow_managed,
            "user": self.allow_user,
            "project": self.effective_project_allowed,
            "temporary": self.allow_temporary,
            "external": self.allow_external,
        }[scope]
        if not allowed:
            if scope == "project":
                reason = "project is not trusted; refusing to execute project extension"
            elif scope == "external":
                reason = "extension path is outside trusted roots"
            else:
                reason = f"{scope} extensions are disabled by policy"
            return TrustDecision(False, scope, False, reason, resolved)

        return TrustDecision(True, scope, True, "", resolved)

    def assert_allowed(
        self,
        path: Path | str,
        *,
        source_scope: ExtensionScope | None = None,
        require_exists: bool = True,
    ) -> TrustDecision:
        decision = self.check(
            path, source_scope=source_scope, require_exists=require_exists
        )
        if not decision.allowed:
            raise ExtensionTrustError(decision.reason)
        return decision


__all__ = ["ExtensionTrustPolicy", "TrustDecision"]
