"""Source file-URI operations, independent from the controller's OS.

URI bytes and native names are separate domains. Decoding before splitting
would turn a percent-encoded separator into a different path.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import PurePosixPath, PureWindowsPath
import re
from urllib.parse import quote, quote_from_bytes, unquote_to_bytes, urlsplit, urlunsplit


_DRIVE = re.compile(r"[A-Za-z](?::|%3[Aa])\Z")
_BAD_PREFIX = "file:///%00/bad/path/"
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _decode(segment: str) -> str:
    try:
        return unquote_to_bytes(segment).decode("utf-8")
    except UnicodeDecodeError:
        return segment


def _opaque_bytes(uri: str) -> bytes | None:
    if not uri.startswith(_BAD_PREFIX):
        return None
    encoded = uri[len(_BAD_PREFIX):]
    if not encoded or "/" in encoded:
        return None
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except ValueError:
        return None
    return raw if base64.urlsafe_b64encode(raw).decode().rstrip("=") == encoded else None


def _opaque(native: str, convention: str) -> str:
    raw = native.encode("utf-16-le", errors="surrogatepass") if convention == "windows" else native.encode("utf-8")
    return _BAD_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _component(value: str) -> str:
    return quote(value, safe="!$&'()*+,-.:;=@_~[]")


@dataclass(frozen=True, slots=True, eq=False)
class ExecutorPath:
    uri: str
    platform_os: str | None

    @classmethod
    def parse(cls, value: str, *, platform_os: str | None = None) -> ExecutorPath:
        parsed = urlsplit(value.replace("\\", "/"))
        if (parsed.scheme != "file" or not parsed.path.startswith("/") or "?" in value or "#" in value
                or parsed.username is not None or parsed.password is not None or parsed.port is not None):
            raise ValueError("Executor paths must be absolute file URIs without credentials, ports, queries or fragments")
        host = parsed.hostname or ""
        host = "" if host.lower() == "localhost" else host.lower()
        path = quote(parsed.path, safe="%/:@!$&'()*+,-.;=_~[]")
        segments: list[str] = []
        input_segments = path.split("/")[1:]
        for index, segment in enumerate(input_segments):
            dot = unquote_to_bytes(segment)
            if dot == b".":
                if index == len(input_segments) - 1:
                    segments.append("")
                continue
            if dot == b"..":
                anchor = int(not host and bool(segments) and bool(re.fullmatch(r"[A-Za-z]:", segments[0])))
                if len(segments) > anchor:
                    segments.pop()
                if index == len(input_segments) - 1:
                    segments.append("")
            else:
                segments.append(segment)
        first = next((index for index, segment in enumerate(segments) if segment), None)
        if not host and first is not None and _DRIVE.fullmatch(segments[first]):
            segments[first] = segments[first][0].upper() + segments[first][1:]
        uri = urlunsplit(("file", host, "/" + "/".join(segments), "", ""))
        if b"\0" in unquote_to_bytes(urlsplit(uri).path) and _opaque_bytes(uri) is None:
            raise ValueError("Null bytes are not allowed in executor paths")
        return cls(uri, platform_os)

    @classmethod
    def from_native(cls, value: str | bytes, *, convention: str, platform_os: str | None = None) -> ExecutorPath:
        """Source from_abs_path: retain unrepresentable native names opaquely."""
        if convention == "posix":
            raw = value if isinstance(value, bytes) else value.encode("utf-8", errors="surrogateescape")
            if not raw.startswith(b"/"):
                raise ValueError("An absolute POSIX path is required")
            if b"\0" not in raw:
                candidate = cls.parse("file://" + quote_from_bytes(raw, safe="/:@!$&'()*+,-.;=_~[]"), platform_os=platform_os)
                if candidate.convention == convention:
                    return candidate
            encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
            return cls.parse(_BAD_PREFIX + encoded, platform_os=platform_os)
        if convention != "windows" or not isinstance(value, str) or not re.match(r"[A-Za-z]:[\\/]|[\\/]{2}", value):
            raise ValueError("An absolute Windows path is required")
        if "\0" in value or any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            return cls.parse(_opaque(value, convention), platform_os=platform_os)
        try:
            candidate = cls.resolve(value, cwd_uri="file:///C:/", platform_os=platform_os)
        except ValueError:
            # Source from_abs_path preserves native namespaces/hosts that
            # cannot be represented as an ordinary URL; it never changes them.
            return cls.parse(_opaque(value, convention), platform_os=platform_os)
        return candidate

    @property
    def convention(self) -> str | None:
        opaque = _opaque_bytes(self.uri)
        if opaque is not None:
            if opaque.startswith(b"/") and not opaque.startswith(b"/\0"):
                return "posix"
            if len(opaque) % 2 == 0:
                native = opaque.decode("utf-16-le", errors="surrogatepass")
                if re.match(r"[A-Za-z]:|[\\/]{2}", native):
                    return "windows"
            return None
        parsed = urlsplit(self.uri)
        first = next((part for part in parsed.path.split("/") if part), "")
        return "windows" if parsed.netloc or _DRIVE.fullmatch(first) else "posix"

    @classmethod
    def resolve(cls, value: str, *, cwd_uri: str, platform_os: str | None,
                user_home_uri: str | None = None) -> ExecutorPath:
        if "\0" in value:
            raise ValueError("Null bytes are not allowed in executor paths")
        if urlsplit(value).scheme.lower() == "file":
            return cls.parse(value, platform_os=platform_os)
        base = cls.parse(cwd_uri, platform_os=platform_os)
        convention = base.convention
        if convention is None:
            raise ValueError("The executor cwd has no native path convention")
        windows = convention == "windows"
        if value == "~" or value.startswith("~/") or windows and value.startswith("~\\"):
            if user_home_uri is None:
                raise ValueError("The executor did not report a user home directory")
            return cls.resolve(value[1:].lstrip("/\\" if windows else "/"), cwd_uri=user_home_uri, platform_os=platform_os)
        if not value:
            return base
        if not windows and re.match(r"[A-Za-z]:[\\/]|[\\/]{2}", value):
            raise ValueError("A Windows absolute path does not belong to this POSIX executor cwd")
        parsed = urlsplit(base.uri)
        parts = [part for part in parsed.path.split("/") if part]
        host = parsed.netloc
        absolute = False
        native = value.replace("\\", "/") if windows else value
        if windows and native.startswith(("//?/", "//./")):
            if native.startswith("//?/") and re.match(r"[A-Za-z]:/", native[4:]):
                native = native[4:]
            elif native.startswith("//?/UNC/"):
                native = "//" + native[8:]
            else:
                return cls.parse(_opaque(value, convention), platform_os=platform_os)
        if windows and re.match(r"[A-Za-z]:/", native):
            host, parts, absolute = "", [], True
        elif windows and native.startswith("//"):
            components = native[2:].split("/")
            if len(components) < 2 or components[0] in {"", ".", ".."} or components[1] in {"", ".", ".."}:
                return cls.parse(_opaque(value, convention), platform_os=platform_os)
            host, parts, native, absolute = components[0], [], "/".join(components[1:]), True
        elif windows and re.match(r"[A-Za-z]:", native):
            if not parts or not _DRIVE.fullmatch(parts[0]) or parts[0][0].lower() != native[0].lower():
                raise ValueError("The executor has no current directory for this other drive")
            native = native[2:]
        elif native.startswith("/"):
            parts = parts[:1] if windows else []
            absolute = not windows
        if _opaque_bytes(base.uri) is not None and not absolute:
            raise ValueError("An opaque executor cwd cannot resolve a relative path")
        anchor = int(windows)
        for part in native.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if len(parts) > anchor:
                    parts.pop()
            else:
                parts.append(_component(part))
        trailing = absolute and (native.endswith("/") or windows and not host and len(parts) == 1)
        path = "/" + "/".join(parts) + ("/" if trailing and parts else "")
        result = cls.parse(urlunsplit(("file", host, path, "", "")), platform_os=platform_os)
        if absolute and result.convention != convention:
            return cls.parse(_opaque(value, convention), platform_os=platform_os)
        return result

    def _segments(self) -> list[str]:
        return [part for part in urlsplit(self.uri).path.split("/") if part]

    def _identity(self):
        if self.convention == "windows" and _opaque_bytes(self.uri) is None:
            parts = self._segments()
            if not any(b"/" in unquote_to_bytes(part) or b"\\" in unquote_to_bytes(part) for part in parts):
                return urlsplit(self.uri).netloc, unquote_to_bytes(urlsplit(self.uri).path).lower()
        return self.uri

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ExecutorPath) and self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    @property
    def native(self) -> str:
        opaque = _opaque_bytes(self.uri)
        if opaque is not None:
            return opaque.decode("utf-16-le" if self.convention == "windows" else "utf-8", errors="replace")
        parsed = urlsplit(self.uri)
        decoded = unquote_to_bytes(parsed.path).decode("utf-8", errors="replace")
        if self.convention == "windows":
            native = ("//" + parsed.netloc + decoded if parsed.netloc else decoded.lstrip("/")).replace("/", "\\")
            return native + "\\" if re.fullmatch(r"[A-Za-z]:", native) else native
        return decoded

    def native_bytes(self) -> bytes:
        if self.convention != "posix":
            raise ValueError("Native byte paths require a POSIX executor")
        return _opaque_bytes(self.uri) or unquote_to_bytes(urlsplit(self.uri).path)

    def to_local_path(self):
        """Lossless conversion only at the actual selected local executor."""
        from pathlib import Path

        convention = "windows" if os.name == "nt" else "posix"
        if self.convention != convention:
            raise ValueError("A foreign executor path cannot become a controller-local path")
        opaque = _opaque_bytes(self.uri)
        if convention == "posix":
            return Path(os.fsdecode(self.native_bytes()))
        if opaque is not None:
            native = opaque.decode("utf-16-le", errors="surrogatepass")
            if not re.match(r"[A-Za-z]:[\\/]|[\\/]{2}", native):
                raise ValueError("The opaque Windows path is not absolute")
            return Path(native)
        parts = self._segments()
        if any(b"/" in unquote_to_bytes(part) or b"\\" in unquote_to_bytes(part) for part in parts):
            raise ValueError("Encoded Windows path separators cannot reach native conversion")
        unquote_to_bytes(urlsplit(self.uri).path).decode("utf-8")
        return Path(self.native)

    @property
    def pure(self) -> PurePosixPath | PureWindowsPath:
        return PureWindowsPath(self.native) if self.convention == "windows" else PurePosixPath(self.native)

    @property
    def name(self) -> str:
        parts = self._segments()
        return _decode(parts[-1]) if parts and _opaque_bytes(self.uri) is None else ""

    @property
    def lexical_depth(self) -> int | None:
        if _opaque_bytes(self.uri) is not None:
            return None
        parts = self._segments()
        if any(b"/" in unquote_to_bytes(part) or self.convention == "windows" and b"\\" in unquote_to_bytes(part)
                for part in parts):
            return None
        return len(parts) - int(self.convention == "windows")

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.name).suffix

    @property
    def parent(self) -> ExecutorPath:
        # Python callers terminate ancestor scans with root.parent == root.
        parts = self._segments()
        if _opaque_bytes(self.uri) is not None or len(parts) <= int(self.convention == "windows"):
            return self
        parsed = urlsplit(self.uri)
        return self.parse(urlunsplit(("file", parsed.netloc, "/" + "/".join(parts[:-1]), "", "")), platform_os=self.platform_os)

    def joinpath(self, value: str) -> ExecutorPath:
        return self.resolve(value, cwd_uri=self.uri, platform_os=self.platform_os)

    def is_relative_to(self, root: ExecutorPath) -> bool:
        if self == root:
            return True
        if (_opaque_bytes(self.uri) is not None or _opaque_bytes(root.uri) is not None
                or urlsplit(self.uri).netloc != urlsplit(root.uri).netloc or self.convention != root.convention):
            return False
        parts, roots = self._segments(), root._segments()
        decoded = [unquote_to_bytes(part) for part in (*parts, *roots)]
        if any(b"/" in part or self.convention == "windows" and b"\\" in part for part in decoded):
            return False
        if self.convention == "windows":
            return len(parts) >= len(roots) and all(a.translate(_ASCII_FOLD) == b.translate(_ASCII_FOLD)
                or _decode(a).translate(_ASCII_FOLD) == _decode(b).translate(_ASCII_FOLD) for a, b in zip(parts, roots))
        return len(parts) >= len(roots) and all(unquote_to_bytes(a) == unquote_to_bytes(b) for a, b in zip(parts, roots))

    def relative_to(self, root: ExecutorPath) -> PurePosixPath | PureWindowsPath:
        if not self.is_relative_to(root):
            raise ValueError("Executor path is not within this URI root")
        text = "/".join(_decode(part) for part in self._segments()[len(root._segments()):])
        return PureWindowsPath(text) if self.convention == "windows" else PurePosixPath(text)

    def __str__(self) -> str:
        return self.uri
