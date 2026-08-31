"""Safe marketplace/source materialization shared by plugin lifecycle APIs.

The three reference runtimes keep two states separate:

* a declared marketplace/source (durable metadata), and
* a staged, validated materialization (the only thing the loader may read).

This module ports that boundary without running package install scripts.  Git
sources are cloned with a non-interactive, depth-limited checkout; archives
are extracted with strict path checks; npm sources use ``npm pack
--ignore-scripts`` and are treated as tar archives.  Every replacement is
staged below the caller-owned root and activated with one atomic rename.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from backend.atomic_io import atomic_write_text
from backend.workspace.path_filters import is_windows_reserved_path

from .identity import is_valid_identifier


class MaterializationError(ValueError):
    """A source could not be parsed, fetched, or safely activated."""


MAX_MARKETPLACE_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_MATERIALIZED_BYTES = 200 * 1024 * 1024
MAX_MATERIALIZED_FILES = 100_000
_GIT_SAFE_BARE_REPOSITORY_CONFIG = "safe.bareRepository=explicit"
_GIT_SSH_COMMAND_CONFIG = "core.sshCommand=ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"


class _MarketplaceRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Apply the same remote-host policy before every redirect connection."""

    def redirect_request(  # type: ignore[no-untyped-def]
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        previous_url = str(getattr(req, "full_url", "") or "")
        resolved_url = urllib.parse.urljoin(previous_url, str(newurl))
        previous_scheme = urllib.parse.urlparse(previous_url).scheme.casefold()
        next_scheme = urllib.parse.urlparse(resolved_url).scheme.casefold()
        if previous_scheme == "https" and next_scheme != "https":
            raise MaterializationError("marketplace download refused an HTTPS downgrade redirect")
        _validate_remote_url(resolved_url, resolve_host=True)
        return super().redirect_request(req, fp, code, msg, headers, resolved_url)


@dataclass(frozen=True)
class MarketplaceSource:
    kind: str
    locator: str
    ref: str = ""
    subpath: str = ""
    sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"source": self.kind}
        if self.kind in {"directory", "file"}:
            payload["path"] = self.locator
        elif self.kind in {"github"}:
            payload["repo"] = self.locator
        elif self.kind == "npm":
            payload["package"] = self.locator
        else:
            payload["url"] = self.locator
        if self.ref:
            payload["ref"] = self.ref
        if self.subpath:
            payload["path"] = self.subpath
        if self.sha:
            payload["sha"] = self.sha
        return payload


@dataclass(frozen=True)
class MaterializedSource:
    path: Path
    source: MarketplaceSource
    resolved_ref: str = ""
    resolved_sha: str = ""
    reused_local: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "source": self.source.to_dict(),
            "resolved_ref": self.resolved_ref,
            "resolved_sha": self.resolved_sha,
            "reused_local": self.reused_local,
        }


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def parse_marketplace_source(
    source: str | Mapping[str, Any],
    *,
    base_dir: Path | None = None,
) -> MarketplaceSource:
    """Parse MiniCode marketplace source descriptors deterministically."""

    if isinstance(source, Mapping):
        kind = str(source.get("source") or source.get("source_type") or "").strip().lower()
        if kind in {"local", "directory"}:
            raw_path = str(source.get("path") or "").strip()
            if not raw_path:
                raise MaterializationError("directory marketplace source requires path")
            path = Path(raw_path).expanduser()
            if base_dir is not None and not path.is_absolute():
                path = Path(base_dir) / path
            return MarketplaceSource("directory", str(path.resolve()), ref=str(source.get("ref") or ""))
        if kind == "file":
            raw_path = str(source.get("path") or "").strip()
            if not raw_path:
                raise MaterializationError("file marketplace source requires path")
            path = Path(raw_path).expanduser()
            if base_dir is not None and not path.is_absolute():
                path = Path(base_dir) / path
            return MarketplaceSource("file", str(path.resolve()))
        if kind in {"github", "git", "git-subdir"}:
            locator = str(source.get("repo") or source.get("url") or "").strip()
            if not locator:
                raise MaterializationError(f"{kind} marketplace source requires repo/url")
            _validate_untrusted_component(locator, "git locator")
            ref = str(source.get("ref") or source.get("ref_name") or "").strip()
            if ref:
                _validate_untrusted_component(ref, "git ref")
            if kind == "github" and "/" in locator and not locator.startswith(("http://", "https://", "git@", "ssh://")):
                locator = f"https://github.com/{locator.rstrip('/')}.git"
            return MarketplaceSource(
                "git",
                _normalize_git_url(locator),
                ref=ref,
                subpath=str(source.get("path") or "").strip(),
                sha=str(source.get("sha") or "").strip(),
            )
        if kind in {"url", "http", "https"}:
            locator = str(source.get("url") or "").strip()
            if not locator:
                raise MaterializationError("url marketplace source requires url")
            ref = str(source.get("ref") or source.get("ref_name") or "").strip()
            _validate_untrusted_component(locator, "remote locator")
            if ref:
                _validate_untrusted_component(ref, "git ref")
            if _looks_like_archive(locator):
                return MarketplaceSource("url", locator, ref=ref)
            return MarketplaceSource(
                "git",
                _normalize_git_url(locator),
                ref=ref,
                subpath=str(source.get("path") or "").strip(),
                sha=str(source.get("sha") or "").strip(),
            )
        if kind == "npm":
            package = str(source.get("package") or source.get("name") or "").strip()
            if not package:
                raise MaterializationError("npm marketplace source requires package")
            _validate_untrusted_component(package, "npm package")
            ref = str(source.get("version") or source.get("ref") or "").strip()
            if ref:
                _validate_untrusted_component(ref, "npm version")
            return MarketplaceSource("npm", package, ref=ref)
        raise MaterializationError(f"unsupported marketplace source type: {kind or '<missing>'}")

    raw = str(source or "").strip()
    if not raw:
        raise MaterializationError("marketplace source must not be empty")
    if _looks_like_local_path(raw):
        path = Path(raw).expanduser()
        if base_dir is not None and not path.is_absolute():
            path = Path(base_dir) / path
        resolved = path.resolve()
        return MarketplaceSource("directory" if resolved.is_dir() else "file", str(resolved))
    if raw.startswith("npm:"):
        package = raw[4:].strip()
        _validate_untrusted_component(package, "npm package")
        return MarketplaceSource("npm", package)
    if _looks_like_github_shorthand(raw):
        base, ref = _split_ref(raw)
        return MarketplaceSource("git", f"https://github.com/{base}.git", ref=ref or "")
    if raw.startswith(("git@", "ssh://")):
        base, ref = _split_ref(raw)
        _validate_untrusted_component(base, "git locator")
        if ref:
            _validate_untrusted_component(ref, "git ref")
        return MarketplaceSource("git", base, ref=ref or "")
    if raw.startswith(("http://", "https://")):
        base, ref = _split_ref(raw)
        # Archive URLs are downloaded/extracted; other HTTP(S) URLs follow
        # MiniCode's git marketplace convention.
        if _looks_like_archive(base):
            return MarketplaceSource("url", base, ref=ref or "")
        _validate_untrusted_component(base, "git locator")
        if ref:
            _validate_untrusted_component(ref, "git ref")
        return MarketplaceSource("git", _normalize_git_url(base), ref=ref or "")
    # Treat a bare npm package as npm only when it has package-like syntax;
    # otherwise fail closed instead of guessing a shell command.
    if _looks_like_npm_package(raw):
        _validate_untrusted_component(raw, "npm package")
        return MarketplaceSource("npm", raw)
    raise MaterializationError("invalid marketplace source format")


def materialize_source(
    source: MarketplaceSource | str | Mapping[str, Any],
    destination: Path,
    *,
    source_base: Path | None = None,
    command_runner: CommandRunner | None = None,
    urlopen: Callable[..., Any] | None = None,
    timeout_seconds: float = 30.0,
    overwrite: bool = True,
    validate: Callable[[Path], None] | None = None,
    after_activate: Callable[[MaterializedSource], None] | None = None,
) -> MaterializedSource:
    """Stage, validate, activate, and commit one source transactionally.

    ``validate`` runs against the isolated staged root before ``destination``
    changes. ``after_activate`` runs while the previous destination is still
    recoverable; an exception from it rolls the filesystem activation back.
    Local directory sources are caller-owned, so they are validated and
    committed in place without copying them into the materialization cache.
    """

    parsed = source if isinstance(source, MarketplaceSource) else parse_marketplace_source(source, base_dir=source_base)
    destination = Path(destination).expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_destination(destination)
    recover_materialization_artifacts(destination)

    if parsed.kind == "directory":
        path = Path(parsed.locator).expanduser().resolve()
        if not path.is_dir():
            raise MaterializationError(f"local marketplace directory does not exist: {path}")
        if validate is not None:
            validate(path)
        result = MaterializedSource(path, parsed, reused_local=True)
        if after_activate is not None:
            after_activate(result)
        return result
    if parsed.kind == "file":
        path = Path(parsed.locator).expanduser().resolve()
        if not path.is_file():
            raise MaterializationError(f"local marketplace archive does not exist: {path}")
        _assert_archive_file_size(path)
        staged = _stage_dir(destination.parent)
        try:
            _extract_archive(path, staged)
            root = _assert_materialized_root(_normalize_extracted_root(staged))
            if validate is not None:
                validate(root)
            return _activate(
                root,
                destination,
                parsed,
                overwrite=overwrite,
                after_activate=after_activate,
            )
        finally:
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)

    staged = _stage_dir(destination.parent)
    try:
        if parsed.kind == "git":
            _validate_git_locator(parsed.locator, resolve_host=command_runner is None)
            _clone_git(parsed, staged, command_runner=command_runner, timeout_seconds=timeout_seconds)
            root = staged
            if parsed.subpath:
                normalized_subpath = parsed.subpath.replace("\\", "/")
                relative_subpath = (
                    Path()
                    if all(part in {"", "."} for part in normalized_subpath.split("/"))
                    else _safe_archive_member(parsed.subpath)
                )
                root = (staged / relative_subpath).resolve()
                try:
                    root.relative_to(staged.resolve())
                except ValueError as exc:
                    raise MaterializationError("git source subpath escapes repository") from exc
                if not root.is_dir():
                    raise MaterializationError(f"git source subpath does not exist: {parsed.subpath}")
        elif parsed.kind == "url":
            _validate_remote_url(parsed.locator, resolve_host=urlopen is None)
            archive = staged / "download"
            _download(parsed.locator, archive, urlopen=urlopen, timeout_seconds=timeout_seconds)
            _extract_archive(archive, staged / "extract")
            root = _normalize_extracted_root(staged / "extract")
        elif parsed.kind == "npm":
            _pack_npm(parsed, staged, command_runner=command_runner, timeout_seconds=timeout_seconds)
            root = _normalize_extracted_root(staged / "extract")
        else:  # pragma: no cover - parser exhaustiveness guard
            raise MaterializationError(f"unsupported source kind: {parsed.kind}")
        root = _assert_materialized_root(root)
        if validate is not None:
            validate(root)
        return _activate(
            root,
            destination,
            parsed,
            overwrite=overwrite,
            after_activate=after_activate,
        )
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def _activate(
    source_root: Path,
    destination: Path,
    source: MarketplaceSource,
    *,
    overwrite: bool,
    after_activate: Callable[[MaterializedSource], None] | None = None,
) -> MaterializedSource:
    source_root = _assert_materialized_root(source_root)
    if destination.exists() and not overwrite:
        raise MaterializationError(f"destination already exists: {destination}")
    staged_copy = destination.parent / f".{destination.name}.{uuid4().hex}.activate"
    backup = destination.parent / f".{destination.name}.{uuid4().hex}.backup"
    activated = False
    try:
        shutil.copytree(source_root, staged_copy, symlinks=False)
        sha = _git_head(staged_copy) if (staged_copy / ".git").is_dir() else ""
        result = MaterializedSource(destination, source, resolved_ref=source.ref, resolved_sha=sha)
        # MiniCode writes activation metadata into the staged tree so readers can
        # never observe a new root without matching provenance.
        _write_provenance(staged_copy, result)
        if destination.exists():
            destination.replace(backup)
        staged_copy.replace(destination)
        activated = True
        if after_activate is not None:
            after_activate(result)
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        return result
    except Exception as exc:
        rollback_error: Exception | None = None
        if activated and destination.exists():
            try:
                shutil.rmtree(destination)
            except Exception as rollback_exc:  # pragma: no cover - platform/filesystem failure
                rollback_error = rollback_exc
        if staged_copy.exists():
            shutil.rmtree(staged_copy, ignore_errors=True)
        if backup.exists() and not destination.exists():
            try:
                backup.replace(destination)
            except Exception as rollback_exc:  # pragma: no cover - platform/filesystem failure
                rollback_error = rollback_error or rollback_exc
        if rollback_error is not None:
            raise MaterializationError(
                f"{exc}; failed to restore previous materialization at {destination}: {rollback_error}"
            ) from exc
        raise


def _assert_materialized_root(root: Path) -> Path:
    if root.is_symlink():
        raise MaterializationError("materialized source root is a symbolic link")
    resolved = root.resolve()
    if not resolved.is_dir():
        raise MaterializationError(f"materialized source root does not exist: {resolved}")
    total_entries = 0
    total_bytes = 0
    pending = [resolved]
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise MaterializationError(f"unable to inspect materialized source: {directory}: {exc}") from exc
        with entries:
            for entry in entries:
                total_entries += 1
                if total_entries > MAX_MATERIALIZED_FILES:
                    raise MaterializationError(
                        f"materialized source exceeds the {MAX_MATERIALIZED_FILES} entry limit"
                    )
                relative = Path(entry.path).relative_to(resolved)
                if is_windows_reserved_path(relative):
                    raise MaterializationError(f"materialized source contains a reserved path: {relative}")
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise MaterializationError(f"unable to inspect materialized source entry: {relative}: {exc}") from exc
                if entry.is_symlink():
                    raise MaterializationError(f"materialized source contains a symbolic link: {relative}")
                file_attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
                reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
                if reparse_flag and file_attributes & reparse_flag:
                    raise MaterializationError(f"materialized source contains a reparse point: {relative}")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise MaterializationError(f"materialized source contains a special entry: {relative}")
                total_bytes += max(0, int(metadata.st_size))
                if total_bytes > MAX_MATERIALIZED_BYTES:
                    raise MaterializationError(
                        f"materialized source exceeds the {MAX_MATERIALIZED_BYTES} byte limit"
                    )
    return resolved


def _clone_git(source: MarketplaceSource, destination: Path, *, command_runner: CommandRunner | None, timeout_seconds: float) -> None:
    runner = command_runner or _default_command_runner
    _validate_untrusted_component(source.locator, "git locator")
    if source.ref:
        _validate_untrusted_component(source.ref, "git ref")
    if source.sha:
        _validate_untrusted_component(source.sha, "git revision")
        if not _is_full_git_sha(source.sha):
            raise MaterializationError("git revision must be a full 40-character hexadecimal SHA")
    ref_is_sha = _is_full_git_sha(source.ref)
    if ref_is_sha and source.sha and source.ref.casefold() != source.sha.casefold():
        raise MaterializationError("git ref SHA and pinned revision do not match")
    pinned_sha = source.sha or (source.ref if ref_is_sha else "")
    branch_ref = "" if ref_is_sha else source.ref
    destination.parent.mkdir(parents=True, exist_ok=True)
    clone_args = _git_args("clone", "--depth", "1", "--no-tags")
    if branch_ref:
        # MiniCode asks clone to resolve the requested branch/tag directly,
        # avoiding a default-branch clone followed by a second network fetch.
        clone_args.extend(["--branch", branch_ref])
    clone_args.extend(["--", source.locator, str(destination)])
    _run_command(
        runner,
        clone_args,
        timeout_seconds=timeout_seconds,
    )
    actual = _git_head(destination, command_runner=runner, timeout_seconds=timeout_seconds)
    if pinned_sha and not branch_ref and actual.casefold() != pinned_sha.casefold():
        # MiniCode treats a full SHA as an already-resolved remote revision.  A
        # shallow default clone may not contain it, so fetch exactly that
        # revision and detach at FETCH_HEAD without widening repository history.
        _run_command(
            runner,
            _git_args(
                "-C",
                str(destination),
                "fetch",
                "--depth",
                "1",
                "--no-tags",
                "origin",
                pinned_sha,
            ),
            timeout_seconds=timeout_seconds,
        )
        _run_command(
            runner,
            _git_args("-C", str(destination), "checkout", "--detach", "FETCH_HEAD"),
            timeout_seconds=timeout_seconds,
        )
        actual = _git_head(destination, command_runner=runner, timeout_seconds=timeout_seconds)
    if pinned_sha and actual.casefold() != pinned_sha.casefold():
        raise MaterializationError(f"git revision mismatch: expected {pinned_sha}, got {actual or '<unknown>'}")


def _pack_npm(source: MarketplaceSource, staged: Path, *, command_runner: CommandRunner | None, timeout_seconds: float) -> None:
    runner = command_runner or _default_command_runner
    package_dir = staged / "npm"
    package_dir.mkdir(parents=True, exist_ok=True)
    spec = source.locator + (f"@{source.ref}" if source.ref else "")
    _run_command(
        runner,
        [
            "npm",
            "pack",
            "--ignore-scripts",
            "--json",
            "--pack-destination",
            str(package_dir),
            "--",
            spec,
        ],
        timeout_seconds=timeout_seconds,
    )
    archives = sorted(package_dir.glob("*.tgz"))
    if not archives:
        raise MaterializationError("npm pack produced no archive")
    _assert_archive_file_size(archives[-1])
    _extract_archive(archives[-1], staged / "extract")


def _download(url: str, destination: Path, *, urlopen: Callable[..., Any] | None, timeout_seconds: float) -> None:
    if urlopen is None:
        opener: Callable[..., Any] = urllib.request.build_opener(_MarketplaceRedirectHandler()).open
    else:
        opener = urlopen
    try:
        response = opener(url, timeout=timeout_seconds)
        with response:
            final_url_getter = getattr(response, "geturl", None)
            if callable(final_url_getter):
                final_url = str(final_url_getter() or "").strip()
                if final_url:
                    _validate_remote_url(final_url, resolve_host=urlopen is None)
            header_value = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
            if header_value:
                try:
                    content_length = int(header_value)
                except ValueError:
                    pass
                else:
                    if content_length > MAX_MARKETPLACE_ARCHIVE_BYTES:
                        raise MaterializationError(
                            f"marketplace archive exceeds the {MAX_MARKETPLACE_ARCHIVE_BYTES} byte limit"
                        )
            with destination.open("wb") as output:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_MARKETPLACE_ARCHIVE_BYTES:
                        raise MaterializationError(
                            f"marketplace archive exceeds the {MAX_MARKETPLACE_ARCHIVE_BYTES} byte limit"
                        )
                    output.write(chunk)
    except Exception as exc:
        raise MaterializationError(f"failed to download marketplace source: {exc}") from exc


def _extract_archive(archive: Path, destination: Path) -> None:
    _assert_archive_file_size(archive)
    destination.mkdir(parents=True, exist_ok=True)
    declared_bytes = 0
    extracted_bytes = 0
    total_entries = 0
    if zipfile.is_zipfile(archive):
        try:
            with zipfile.ZipFile(archive) as handle:
                for info in handle.infolist():
                    total_entries += 1
                    _enforce_archive_entry_limit(total_entries)
                    relative = _safe_archive_member(info.filename)
                    target = destination / relative
                    unix_mode = (int(info.external_attr) >> 16) & 0xFFFF
                    entry_kind = stat.S_IFMT(unix_mode)
                    if info.is_dir():
                        if entry_kind not in {0, stat.S_IFDIR}:
                            raise MaterializationError(f"archive member has inconsistent type: {info.filename}")
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if entry_kind not in {0, stat.S_IFREG}:
                        raise MaterializationError(f"archive member is not a regular file: {info.filename}")
                    declared_size = max(0, int(info.file_size))
                    declared_bytes = _checked_archive_total(declared_bytes, declared_size)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with handle.open(info) as source, target.open("wb") as output:
                        copied = _copy_archive_stream(
                            source,
                            output,
                            remaining=MAX_MATERIALIZED_BYTES - extracted_bytes,
                            member_name=info.filename,
                        )
                    if copied != declared_size:
                        raise MaterializationError(
                            f"archive member size mismatch for {info.filename}: declared {declared_size}, wrote {copied}"
                        )
                    extracted_bytes += copied
        except (OSError, RuntimeError, EOFError, zipfile.BadZipFile) as exc:
            raise MaterializationError(f"failed to extract zip marketplace archive: {exc}") from exc
        return
    try:
        with tarfile.open(archive, mode="r:*") as handle:
            for member in handle:
                total_entries += 1
                _enforce_archive_entry_limit(total_entries)
                relative = _safe_archive_member(member.name)
                target = destination / relative
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise MaterializationError(f"archive member is not a regular file: {member.name}")
                declared_size = max(0, int(member.size))
                declared_bytes = _checked_archive_total(declared_bytes, declared_size)
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = handle.extractfile(member)
                if stream is None:
                    raise MaterializationError(f"unable to read archive member: {member.name}")
                with stream, target.open("wb") as output:
                    copied = _copy_archive_stream(
                        stream,
                        output,
                        remaining=MAX_MATERIALIZED_BYTES - extracted_bytes,
                        member_name=member.name,
                    )
                if copied != declared_size:
                    raise MaterializationError(
                        f"archive member size mismatch for {member.name}: declared {declared_size}, wrote {copied}"
                    )
                extracted_bytes += copied
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise MaterializationError("source is not a supported zip/tar archive") from exc


def _assert_archive_file_size(archive: Path) -> None:
    try:
        size = archive.stat().st_size
    except OSError as exc:
        raise MaterializationError(f"unable to inspect marketplace archive: {archive}: {exc}") from exc
    if size > MAX_MARKETPLACE_ARCHIVE_BYTES:
        raise MaterializationError(
            f"marketplace archive exceeds the {MAX_MARKETPLACE_ARCHIVE_BYTES} byte limit"
        )


def _enforce_archive_entry_limit(total_entries: int) -> None:
    if total_entries > MAX_MATERIALIZED_FILES:
        raise MaterializationError(
            f"marketplace archive exceeds the {MAX_MATERIALIZED_FILES} entry limit"
        )


def _checked_archive_total(current: int, entry_size: int) -> int:
    next_total = current + entry_size
    if next_total > MAX_MATERIALIZED_BYTES:
        raise MaterializationError(
            f"marketplace archive expands beyond the {MAX_MATERIALIZED_BYTES} byte limit"
        )
    return next_total


def _copy_archive_stream(source: Any, output: Any, *, remaining: int, member_name: str) -> int:
    copied = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return copied
        next_total = copied + len(chunk)
        if next_total > remaining:
            raise MaterializationError(
                f"archive member {member_name} expands beyond the {MAX_MATERIALIZED_BYTES} byte limit"
            )
        output.write(chunk)
        copied = next_total


def _normalize_extracted_root(root: Path) -> Path:
    root = root.resolve()
    children = [path for path in root.iterdir() if path.name not in {"__MACOSX"}]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return root


def _write_provenance(destination: Path, materialized: MaterializedSource) -> None:
    payload = {
        "schema_version": 1,
        "source": materialized.source.to_dict(),
        "resolved_ref": materialized.resolved_ref,
        "resolved_sha": materialized.resolved_sha,
        "materialized_at": time.time(),
    }
    path = destination / ".marketplace-source.json"
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate_remote_url(url: str, *, resolve_host: bool = True) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise MaterializationError("remote marketplace source must use an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise MaterializationError("remote marketplace source must not embed credentials")
    if parsed.fragment:
        raise MaterializationError("remote marketplace source must not contain a URL fragment")
    host = parsed.hostname.casefold().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise MaterializationError("remote marketplace source resolves to a local host")
    if not resolve_host:
        return
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise MaterializationError(f"unable to resolve remote marketplace host: {host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            raise MaterializationError("remote marketplace source resolves to a private address")


def _is_full_git_sha(value: str) -> bool:
    text = str(value or "").strip()
    return len(text) == 40 and all(char in "0123456789abcdefABCDEF" for char in text)


def _git_args(*args: str) -> list[str]:
    """Build the stable non-interactive Git command prefix used by MiniCode."""

    return [
        "git",
        "-c",
        _GIT_SAFE_BARE_REPOSITORY_CONFIG,
        "-c",
        _GIT_SSH_COMMAND_CONFIG,
        *args,
    ]


def _validate_untrusted_component(value: str, label: str) -> None:
    """Reject option/control-character injection before argv construction."""

    text = str(value or "")
    if not text:
        raise MaterializationError(f"{label} must not be empty")
    if text.startswith("-"):
        raise MaterializationError(f"{label} must not start with '-'")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise MaterializationError(f"{label} contains control characters")


def _git_remote_host(locator: str) -> str:
    value = str(locator or "").strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme:
        if parsed.scheme.lower() not in {"http", "https", "ssh"}:
            raise MaterializationError("git marketplace source must use HTTP(S), SSH, or scp-like syntax")
        if parsed.password is not None:
            raise MaterializationError("git marketplace source must not embed a password")
        if parsed.scheme.lower() in {"http", "https"} and parsed.username is not None:
            raise MaterializationError("HTTP(S) git marketplace source must not embed credentials")
        if parsed.fragment:
            raise MaterializationError("git marketplace source must not contain a URL fragment")
        host = parsed.hostname or ""
    else:
        # Git's scp-like form is ``[user@]host:path``.  A local path or a
        # Windows drive is intentionally not accepted for a remote source.
        if _looks_like_local_path(value) or ":" not in value:
            raise MaterializationError("git marketplace source must be a remote URL")
        if "#" in value:
            raise MaterializationError("git marketplace source must not contain a URL fragment")
        prefix = value.split(":", 1)[0]
        host = prefix.rsplit("@", 1)[-1]
    host = str(host or "").strip().casefold().rstrip(".")
    if not host:
        raise MaterializationError("git marketplace source host is required")
    return host


def _validate_git_locator(locator: str, *, resolve_host: bool = True) -> None:
    _validate_untrusted_component(locator, "git locator")
    host = _git_remote_host(locator)
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".local"):
        raise MaterializationError("git marketplace source resolves to a local host")
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        addresses = set()
    if not addresses and resolve_host:
        try:
            addresses = {
                str(ipaddress.ip_address(info[4][0]))
                for info in socket.getaddrinfo(host, 22, type=socket.SOCK_STREAM)
                if info[4]
            }
        except OSError as exc:
            raise MaterializationError(f"unable to resolve git marketplace host: {host}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            raise MaterializationError("git marketplace source resolves to a private address")


def _git_head(
    path: Path,
    *,
    command_runner: CommandRunner | None = None,
    timeout_seconds: float = 10.0,
) -> str:
    runner = command_runner or _default_command_runner
    try:
        result = runner(
            _git_args("-C", str(path), "rev-parse", "HEAD"),
            timeout=timeout_seconds,
        )
    except Exception:
        return ""
    return str(getattr(result, "stdout", "") or "").strip() if result.returncode == 0 else ""


def _command_environment() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["npm_config_ignore_scripts"] = "true"
    return env


def _default_command_runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    env = _command_environment()
    return subprocess.run(args, capture_output=True, text=True, check=False, env=env, **kwargs)


def _run_command(runner: CommandRunner, args: list[str], *, timeout_seconds: float) -> None:
    try:
        result = runner(args, timeout=timeout_seconds)
    except Exception as exc:
        raise MaterializationError(f"failed to run {args[0]}: {exc}") from exc
    if result.returncode != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        raise MaterializationError(f"{args[0]} failed: {stderr or result.returncode}")


def _stage_dir(parent: Path) -> Path:
    path = Path(tempfile.mkdtemp(prefix=".marketplace-stage-", dir=str(parent)))
    return path


def recover_materialization_artifacts(destination: Path) -> dict[str, int]:
    """Recover an interrupted activation for one exact destination.

    ``_activate`` deliberately uses sibling ``.activate`` and ``.backup``
    directories.  A process crash can occur after the old destination was
    moved aside but before the new tree was renamed into place.  On the next
    operation, restore the newest backup when the destination is absent; when
    a destination is present, the activation committed and only stale
    siblings need removal.  Names are matched exactly to avoid touching an
    unrelated cache entry.
    """

    destination = Path(destination).expanduser().absolute()
    parent = destination.parent
    if not parent.is_dir():
        return {"restored": 0, "removed": 0}
    prefix = f".{destination.name}."

    def matching(suffix: str) -> list[Path]:
        result: list[Path] = []
        for candidate in parent.iterdir():
            name = candidate.name
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            # UUIDs are generated by _activate.  Keep the boundary strict so
            # a user-created hidden directory cannot be mistaken for a
            # recovery artifact merely by sharing the suffix.
            token = name[len(prefix) : -len(suffix)]
            if len(token) != 32 or any(char not in "0123456789abcdef" for char in token.lower()):
                continue
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            result.append(candidate)
        return result

    activates = matching(".activate")
    backups = matching(".backup")
    removed = 0
    restored = 0

    def remove_artifact(candidate: Path) -> None:
        nonlocal removed
        if candidate.is_dir() and not candidate.is_symlink():
            shutil.rmtree(candidate, ignore_errors=False)
        elif candidate.is_file():
            candidate.unlink()
        else:
            return
        removed += 1

    if destination.exists():
        # The new tree is visible, so its activation committed.  Retaining a
        # backup here only wastes disk and can confuse a later recovery scan.
        for candidate in (*activates, *backups):
            remove_artifact(candidate)
        return {"restored": restored, "removed": removed}

    if backups:
        # There should normally be one backup.  Use mtime only to make a
        # manually interrupted sequence deterministic, then remove the rest.
        backups.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        backups[0].replace(destination)
        restored = 1
        for candidate in (*backups[1:], *activates):
            remove_artifact(candidate)
        return {"restored": restored, "removed": removed}

    # No previous destination can be recovered.  Staged activation trees are
    # safe to discard because they were never made visible at the destination.
    for candidate in activates:
        remove_artifact(candidate)
    return {"restored": restored, "removed": removed}


def _assert_safe_destination(path: Path) -> None:
    if is_windows_reserved_path(path):
        raise MaterializationError("destination contains a reserved path segment")


def _safe_archive_member(value: str) -> Path:
    raw = str(value or "").replace("\\", "/")
    if raw.startswith("/") or raw.startswith("~"):
        raise MaterializationError(f"archive path escapes destination: {value}")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise MaterializationError(f"archive path escapes destination: {value}")
    if any(":" in part for part in parts):
        raise MaterializationError(f"archive path contains a drive prefix: {value}")
    relative = Path(*parts)
    if is_windows_reserved_path(relative):
        raise MaterializationError(f"archive path contains a reserved segment: {value}")
    return relative


def _normalize_git_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    if value.startswith("https://github.com/") and not value.endswith(".git"):
        return f"{value}.git"
    return value


def _split_ref(value: str) -> tuple[str, str | None]:
    if "#" in value:
        base, ref = value.rsplit("#", 1)
        return base, ref.strip() or None
    if "://" not in value and not value.startswith(("git@", "ssh://")) and "@" in value:
        base, ref = value.rsplit("@", 1)
        return base, ref.strip() or None
    return value, None


def _looks_like_local_path(value: str) -> bool:
    return (
        Path(value).is_absolute()
        or value in {".", ".."}
        or value.startswith(("./", ".\\", "../", "..\\", "~/", "~\\"))
        or (len(value) >= 3 and value[1] == ":" and value[2] in {"/", "\\"})
        or value.startswith(("\\\\",))
    )


def _looks_like_github_shorthand(value: str) -> bool:
    parts = value.split("/", 2)
    return len(parts) == 2 and all(part and all(char.isalnum() or char in "-_.@" for char in part) for part in parts)


def _looks_like_archive(value: str) -> bool:
    lower = str(value or "").casefold().split("?", 1)[0].split("#", 1)[0]
    return lower.endswith((".zip", ".tar", ".tgz", ".tar.gz", ".tar.bz2", ".tar.xz"))


def _looks_like_npm_package(value: str) -> bool:
    if value.startswith("@"):
        return "/" in value and all(char.isalnum() or char in "-_.@/" for char in value)
    return "/" not in value and all(char.isalnum() or char in "-_.@" for char in value)


def is_safe_marketplace_name(value: str) -> bool:
    return is_valid_identifier(value, "local")
