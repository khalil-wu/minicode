"""Browser automation (CDP) helpers.

Extracted from ``backend/tools/browser_control_tool.py`` so CDP session
management, endpoint/URL validation and JS snippet generation are independent
of the tool class.
"""

from __future__ import annotations

import logging

from backend.permissions.context import ToolExecutionContext
from backend.permissions.network import assess_network_url
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname
import asyncio
import json


logger = logging.getLogger(__name__)


DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"

LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

API_IMAGE_MAX_BASE64_SIZE = 5 * 1024 * 1024


class _CDPSession:
    def __init__(self, websocket: Any) -> None:
        self._websocket = websocket
        self._next_id = 1
        self._events: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        call_id = self._next_id
        self._next_id += 1
        await self._websocket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        while True:
            raw = await self._websocket.recv()
            message = json.loads(raw)
            if message.get("id") != call_id:
                self._remember_event(message)
                continue
            if "error" in message:
                error = message.get("error") if isinstance(message.get("error"), dict) else {}
                raise RuntimeError(str(error.get("message") or error or f"CDP call failed: {method}"))
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    async def drain_events(self, seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + max(0.0, seconds)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            try:
                raw = await asyncio.wait_for(self._websocket.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            self._remember_event(json.loads(raw))

    def events(self, method: str | None = None) -> list[dict[str, Any]]:
        if method is None:
            return list(self._events)
        return [event for event in self._events if event.get("method") == method]

    def _remember_event(self, message: dict[str, Any]) -> None:
        if "method" not in message:
            return
        self._events.append(message)
        if len(self._events) > 200:
            del self._events[: len(self._events) - 200]


class _cdp_session:
    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._websocket: Any | None = None

    async def __aenter__(self) -> _CDPSession:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - dependency comes from uvicorn[standard] in normal installs
            raise RuntimeError("browser_control requires websockets for CDP actions") from exc
        self._websocket = await websockets.connect(self._ws_url, max_size=16 * 1024 * 1024)
        return _CDPSession(self._websocket)

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._websocket is not None:
            await self._websocket.close()



async def _runtime_evaluate(session: _CDPSession, expression: str, *, user_gesture: bool = False) -> dict[str, Any]:
    result = await session.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
            "userGesture": user_gesture,
        },
    )
    if result.get("exceptionDetails"):
        details = result.get("exceptionDetails") if isinstance(result.get("exceptionDetails"), dict) else {}
        text = details.get("text") or details.get("exception", {}).get("description") or "JavaScript evaluation failed"
        raise RuntimeError(str(text))
    return result


def _runtime_value(result: dict[str, Any]) -> Any:
    remote = result.get("result") if isinstance(result, dict) else {}
    if not isinstance(remote, dict):
        return None
    if "value" in remote:
        return remote.get("value")
    if "unserializableValue" in remote:
        return remote.get("unserializableValue")
    return remote.get("description")


def _json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _endpoint_error(raw: str) -> str:
    try:
        _normalize_endpoint(raw)
    except ValueError as exc:
        return str(exc)
    return ""


def _workspace_file_navigation_target(raw_url: str, workspace_root: Any) -> Path | None:
    """Return the workspace HTML file a non-http navigate target refers to.

    The model reasonably asks to open a file it just wrote, either as a bare
    workspace path or as ``file://``.  Chrome cannot be pointed at a local file
    through the preview boundary, so the caller serves it over loopback
    instead.  Only real HTML files inside the active workspace qualify; anything
    else stays a validation error so an unsupported scheme is still refused.
    """

    raw = str(raw_url or "").strip()
    if not raw or not workspace_root:
        return None
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        return None
    if parsed.scheme == "file":
        local = url2pathname(parsed.path)
        if parsed.netloc and parsed.netloc.lower() not in LOOPBACK_HOSTNAMES:
            # A file:// URL with a host is a UNC share, not a workspace file.
            return None
    elif parsed.scheme and len(parsed.scheme) > 1:
        # A real non-file scheme (ftp:, data:, chrome:) is not a workspace path.
        # Single-letter schemes are Windows drive letters (``C:\...``).
        return None
    else:
        local = raw

    try:
        root = Path(str(workspace_root)).expanduser().resolve()
        candidate = Path(local).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    if candidate.suffix.lower() not in {".html", ".htm"} or not candidate.is_file():
        return None
    return candidate


async def _serve_workspace_file_for_navigation(
    target: Path,
    context: ToolExecutionContext | None,
) -> tuple[str, str]:
    """Serve *target* over loopback and return ``(url, error)``.

    Reuses the same owned static preview that ``preview_server`` starts, so the
    resulting origin passes the navigation policy for exactly this conversation.
    """

    from backend.preview.launcher import start_static_preview

    workspace_root = getattr(context, "workspace_root", None)
    try:
        proc = await start_static_preview(
            workspace_root,
            target,
            session_id=str(getattr(context, "session_id", "") or ""),
            conversation_id=str(getattr(context, "conversation_id", "") or ""),
            sandbox_policy=(context.sandbox_policy if context is not None else None),
        )
    except Exception as exc:
        return "", f"Could not serve {target.name} for browser navigation: {exc}"
    url = str(proc.effective_url or "").strip()
    if not url:
        return "", f"Preview server for {target.name} did not report a URL."
    return url, ""


def _is_workspace_html_target(raw_url: str) -> bool:
    """Return whether *raw_url* could name a workspace HTML file.

    ``validate_input`` runs without an execution context, so the workspace
    boundary cannot be applied yet.  This only rejects spellings that can never
    be a local HTML file; ``execute`` performs the authoritative resolution
    against the real workspace root.
    """

    raw = str(raw_url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        candidate = url2pathname(parsed.path)
    elif parsed.scheme and len(parsed.scheme) > 1:
        return False
    else:
        candidate = raw
    return Path(candidate).suffix.lower() in {".html", ".htm"}


async def _resolved_navigation_url(
    raw_url: str,
    context: ToolExecutionContext | None,
) -> tuple[str, str]:
    """Return ``(url, error)`` for a navigate request.

    An http(s) URL passes through untouched.  A workspace HTML file — named
    directly or through ``file://`` — is served over the conversation's own
    loopback preview and replaced by that URL, so the model does not need a
    separate ``preview_server`` round trip to open a file it just wrote.
    """

    url = str(raw_url or "").strip()
    if not url:
        return "", "Missing url for navigate"
    if urlparse(url).scheme in {"http", "https"}:
        return url, ""

    workspace_root = getattr(context, "workspace_root", None)
    target = _workspace_file_navigation_target(url, workspace_root)
    if target is None:
        if not workspace_root:
            return "", (
                "navigate needs an http or https URL: no active workspace is available "
                "to serve a local file."
            )
        return "", (
            f"Cannot navigate to '{url}': it is not an existing HTML file inside the "
            "active workspace."
        )
    return await _serve_workspace_file_for_navigation(target, context)


async def _navigation_policy_error(
    raw_url: str,
    context: ToolExecutionContext | None,
) -> str:
    """Apply the network boundary at the browser navigation side effect.

    Browser navigation is more privileged than ``web_fetch`` because the
    browser may carry cookies, credentials, and origin permissions.  The
    permission checker still owns the user approval decision, but the tool
    itself must not turn a model-supplied URL into an SSRF primitive.  Public
    HTTP(S) targets follow the normal network policy.  Local/private targets
    are only accepted when they are an actively running preview owned by the
    same session and conversation (or an equivalent origin explicitly placed
    in the turn metadata by the preview runtime).
    """

    url = str(raw_url or "").strip()
    if not url:
        return "Missing url for navigate"
    assessment = await asyncio.to_thread(assess_network_url, url)
    if assessment.allowed:
        return ""
    if await asyncio.to_thread(_is_owned_preview_origin, url, context):
        return ""
    return (
        "Browser navigation to a local, private, or unresolved network target "
        "is blocked unless it belongs to the active conversation preview. "
        f"{assessment.reason}"
    )


def _is_owned_preview_origin(
    url: str,
    context: ToolExecutionContext | None,
) -> bool:
    """Return whether *url* matches a preview origin owned by this turn."""

    conversation_id = str(getattr(context, "conversation_id", "") or "").strip()
    session_id = str(getattr(context, "session_id", "") or "").strip()
    if not conversation_id:
        return False

    candidates: list[str] = []
    metadata = getattr(context, "metadata", None)
    if isinstance(metadata, dict):
        raw_origins = metadata.get("preview_origins")
        if isinstance(raw_origins, (list, tuple, set)):
            candidates.extend(str(item).strip() for item in raw_origins if str(item).strip())
        for key in ("preview_url", "preview_origin"):
            value = str(metadata.get(key) or "").strip()
            if value:
                candidates.append(value)

    try:
        from backend.preview.launcher import preview_url_is_owned

        if preview_url_is_owned(
            url,
            session_id=session_id,
            conversation_id=conversation_id,
            workspace_root=getattr(context, "workspace_root", None),
            extra_urls=tuple(candidates),
        ):
            return True
    except Exception:
        # A preview registry failure must not weaken the browser boundary.
        return False

    requested = _url_origin(url)
    return bool(requested and any(requested == _url_origin(candidate) for candidate in candidates))


def _url_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme.lower(), parsed.hostname.casefold().rstrip("."), port


def _normalize_endpoint(raw: str) -> str:
    value = (raw or DEFAULT_CDP_ENDPOINT).strip()
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    if parsed.scheme != "http":
        raise ValueError("cdp_endpoint must use http")
    host = parsed.hostname or ""
    if not _is_loopback_host(host):
        raise ValueError("cdp_endpoint must be localhost, 127.0.0.1, or ::1")
    port = parsed.port or 9222
    netloc = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"
    return f"http://{netloc}{parsed.path.rstrip('/')}"


def _validate_ws_url(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme not in {"ws", "wss"}:
        raise RuntimeError("CDP target did not include a websocket URL")
    if parsed.scheme == "wss":
        raise RuntimeError("Remote secure CDP websocket endpoints are not supported")
    if not _is_loopback_host(parsed.hostname or ""):
        raise RuntimeError("CDP websocket URL must be loopback")
    return raw


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _endpoint_url(endpoint: str, path: str) -> str:
    return f"{endpoint.rstrip('/')}/{path.lstrip('/')}"


def _format_discovery(endpoint: str, version: dict[str, Any], targets: list[dict[str, Any]]) -> str:
    lines = [
        f"CDP endpoint: {endpoint}",
        f"Browser: {version.get('Browser') or version.get('browser') or 'unknown'}",
        f"Protocol-Version: {version.get('Protocol-Version') or version.get('protocolVersion') or 'unknown'}",
        "",
        _format_targets(targets),
    ]
    return "\n".join(lines).strip()


def _format_targets(targets: list[dict[str, Any]]) -> str:
    if not targets:
        return "No browser targets found."
    lines = [f"{len(targets)} browser target(s):"]
    for index, target in enumerate(targets, 1):
        target_id = str(target.get("id") or "")
        target_type = str(target.get("type") or "")
        title = str(target.get("title") or "").strip()
        url = str(target.get("url") or "").strip()
        lines.append(f"{index}. {target_id} [{target_type}] {title}".rstrip())
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)


def _format_console_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No console events captured during the wait window."
    lines: list[str] = []
    for event in events:
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        if method == "Runtime.consoleAPICalled":
            level = str(params.get("type") or "log")
            args = params.get("args") if isinstance(params.get("args"), list) else []
            text = " ".join(_remote_arg_text(arg) for arg in args if isinstance(arg, dict)).strip()
            lines.append(f"[{level}] {text}")
        elif method == "Log.entryAdded":
            entry = params.get("entry") if isinstance(params.get("entry"), dict) else {}
            level = str(entry.get("level") or "log")
            text = str(entry.get("text") or "")
            url = str(entry.get("url") or "")
            line = entry.get("lineNumber")
            location = f" ({url}:{line})" if url and line is not None else ""
            lines.append(f"[{level}] {text}{location}")
    return "\n".join(lines) if lines else "No console log entries found."


def _format_network_events(events: list[dict[str, Any]]) -> tuple[str, int]:
    """Format request metadata without exposing headers, cookies, or bodies."""

    methods: dict[str, str] = {}
    lines: list[str] = []
    for event in events:
        method = str(event.get("method") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        request_id = str(params.get("requestId") or "")
        if method == "Network.requestWillBeSent":
            request = params.get("request") if isinstance(params.get("request"), dict) else {}
            methods[request_id] = str(request.get("method") or "GET")
        elif method == "Network.responseReceived":
            response = params.get("response") if isinstance(params.get("response"), dict) else {}
            status = int(response.get("status") or 0)
            url = str(response.get("url") or "")
            resource_type = str(params.get("type") or "other")
            lines.append(f"[{status or 'ERR'}] {methods.get(request_id, 'GET')} {resource_type} {url}".rstrip())
        elif method == "Network.loadingFailed":
            error = str(params.get("errorText") or "request failed")
            lines.append(f"[ERR] {methods.get(request_id, 'GET')} {error}")
    if not lines:
        return "No network requests captured during the wait window.", 0
    return "\n".join(lines[-100:]), len(lines)


def _remote_arg_text(arg: dict[str, Any]) -> str:
    if "value" in arg:
        value = arg.get("value")
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return str(arg.get("description") or arg.get("type") or "")


def _stringify_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _max_chars(args: dict[str, Any], default: int) -> int:
    try:
        return int(args.get("max_chars") or default)
    except (TypeError, ValueError):
        return default


def _validate_int_range(payload: dict[str, Any], key: str, minimum: int, maximum: int) -> str:
    value = payload.get(key)
    if value in (None, ""):
        return ""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return f"{key} must be an integer"
    if parsed < minimum or parsed > maximum:
        return f"{key} must be between {minimum} and {maximum}"
    return ""


def _validate_float(payload: dict[str, Any], key: str) -> str:
    try:
        float(payload.get(key))
    except (TypeError, ValueError):
        return f"{key} must be a number"
    return ""


def _bool_arg(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _selector_point_js(selector: str) -> str:
    return r"""
((selector) => {
  const el = document.querySelector(selector);
  if (!el) return { ok: false, error: `Element not found: ${selector}` };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const rect = el.getBoundingClientRect();
  if (!rect || rect.width === 0 || rect.height === 0) {
    return { ok: false, error: `Element is not visible: ${selector}` };
  }
  return {
    ok: true,
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.value || '').trim().slice(0, 120)
  };
})
""" + f"({json.dumps(selector)})"


def _focus_selector_js(selector: str, clear: bool) -> str:
    return r"""
((selector, clear) => {
  const el = document.querySelector(selector);
  if (!el) return { ok: false, error: `Element not found: ${selector}` };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  el.focus();
  if (clear) {
    if ('value' in el) {
      el.value = '';
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      el.textContent = '';
    }
  }
  return { ok: true, tag: el.tagName.toLowerCase() };
})
""" + f"({json.dumps(selector)}, {json.dumps(bool(clear))})"


def _scroll_selector_js(selector: str) -> str:
    return r"""
((selector) => {
  const el = document.querySelector(selector);
  if (!el) return { ok: false, error: `Element not found: ${selector}` };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  return { ok: true, scrollX: window.scrollX, scrollY: window.scrollY };
})
""" + f"({json.dumps(selector)})"


def _scroll_by_js(delta_x: float, delta_y: float) -> str:
    return (
        "window.scrollBy({ left: "
        f"{json.dumps(delta_x)}, top: {json.dumps(delta_y)}, behavior: 'instant' }});"
        "({ scrollX: window.scrollX, scrollY: window.scrollY })"
    )


_DOM_SNAPSHOT_JS = r"""
(() => {
  const maxDepth = 5;
  const maxChildren = 40;
  const attrNames = ['id', 'class', 'role', 'aria-label', 'name', 'type', 'href', 'placeholder'];

  function compactText(value, limit) {
    return String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  }

  function attrsFor(el) {
    const attrs = {};
    for (const name of attrNames) {
      if (el.hasAttribute && el.hasAttribute(name)) {
        attrs[name] = el.getAttribute(name);
      }
    }
    return attrs;
  }

  function snapshot(node, depth) {
    if (!node || depth > maxDepth) return null;
    if (node.nodeType === Node.TEXT_NODE) {
      const text = compactText(node.textContent, 120);
      return text ? { type: 'text', text } : null;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return null;
    const rect = node.getBoundingClientRect ? node.getBoundingClientRect() : { width: 0, height: 0 };
    const children = [];
    for (const child of Array.from(node.childNodes || []).slice(0, maxChildren)) {
      const item = snapshot(child, depth + 1);
      if (item) children.push(item);
    }
    return {
      type: 'element',
      tag: node.tagName.toLowerCase(),
      attrs: attrsFor(node),
      visible: Boolean(rect.width || rect.height),
      text: compactText(node.innerText, 160),
      children
    };
  }

  return snapshot(document.body || document.documentElement, 0);
})()
"""

