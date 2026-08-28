from __future__ import annotations

import asyncio
import base64
import json
import os
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx

from backend.permissions.context import ToolExecutionContext
from backend.permissions.network import assess_network_url
from backend.tools.base import (
    TOOL_SIDE_EFFECT_EXTERNAL,
    TOOL_SIDE_EFFECT_NONE,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
    truncate_tool_result,
)
from backend.tools.contracts import ToolSpec

from backend.tools.browser_support import (
    _bool_arg,
    _cdp_session,
    _endpoint_error,
    _endpoint_url,
    _focus_selector_js,
    _format_console_events,
    _format_discovery,
    _format_network_events,
    _format_targets,
    _is_workspace_html_target,
    _json_dict,
    _json_list,
    _max_chars,
    _navigation_policy_error,
    _normalize_endpoint,
    _resolved_navigation_url,
    _runtime_evaluate,
    _runtime_value,
    _scroll_by_js,
    _scroll_selector_js,
    _selector_point_js,
    _stringify_value,
    _validate_float,
    _validate_int_range,
    _validate_ws_url,

    DEFAULT_CDP_ENDPOINT,
    LOOPBACK_HOSTNAMES,
    API_IMAGE_MAX_BASE64_SIZE,)

class BrowserControlTool(BaseTool):
    """Chrome DevTools Protocol control for local browser sessions."""

    name = "browser_control"
    description = (
        "Inspect or control a local Chrome/Edge DevTools Protocol endpoint. "
        "Supports discovery, navigation, screenshots, DOM/text/html inspection, "
        "waiting for selectors, console capture, clicking, typing, scrolling, "
        "key presses, and JavaScript evaluation. The cdp_endpoint must be local "
        "loopback; remote endpoints are rejected."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    open_world = True
    result_kind = "browser"
    activity_kind = "genericTool"
    display_label = "Browser"
    max_result_chars = None

    _READ_ACTIONS = {
        "discover",
        "list_targets",
        "screenshot",
        "get_url",
        "get_text",
        "get_html",
        "get_dom",
        "wait_for_element",
        "get_console_logs",
        "get_network_logs",
    }
    _PRIVATE_READ_ACTIONS = {
        "screenshot",
        "get_text",
        "get_html",
        "get_dom",
        "wait_for_element",
        "get_console_logs",
        "get_network_logs",
    }
    _WRITE_ACTIONS = {"navigate", "click", "type", "press_key", "scroll", "evaluate"}
    _ACTIONS = _READ_ACTIONS | _WRITE_ACTIONS
    _MAX_TARGETS = 30
    _MAX_RESULT_CHARS = 20_000

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="browser.cdp",
            toolset="browser",
            exposure="deferred",
            required_args=("action",),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(self._ACTIONS),
                        "description": "Browser action to perform.",
                    },
                    "cdp_endpoint": {
                        "type": "string",
                        "description": "Local DevTools endpoint, default http://127.0.0.1:9222.",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "Optional page target id. Defaults to the first page target.",
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "HTTP or HTTPS URL to navigate to when action='navigate'. "
                            "Serve workspace HTML first with preview_server(action='start', path='<file>.html')."
                        ),
                    },
                    "selector": {
                        "type": "string",
                        "description": "CSS selector for click/type/scroll/wait_for_element.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type when action='type'.",
                    },
                    "key": {
                        "type": "string",
                        "description": "Keyboard key for action='press_key', e.g. Enter, Tab, Escape.",
                    },
                    "expression": {
                        "type": "string",
                        "description": "JavaScript expression for action='evaluate'. Requires confirmation.",
                    },
                    "x": {"type": "number", "description": "Viewport x coordinate for coordinate click."},
                    "y": {"type": "number", "description": "Viewport y coordinate for coordinate click."},
                    "delta_x": {"type": "number", "description": "Horizontal scroll delta. Defaults to 0."},
                    "delta_y": {"type": "number", "description": "Vertical scroll delta. Defaults to 600."},
                    "wait_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5000,
                        "description": "Optional wait after navigation or while collecting console events.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 30000,
                        "description": "Timeout for wait_for_element. Defaults to 5000.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200000,
                        "description": "Maximum characters returned for text/html/dom/evaluate/log actions.",
                    },
                    "clear": {
                        "type": "boolean",
                        "description": "Clear the target element before typing.",
                    },
                },
                "required": ["action"],
            },
        )

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        payload = args or {}
        action = str(payload.get("action") or "").strip().lower()
        if action not in self._ACTIONS:
            return f"action must be one of: {', '.join(sorted(self._ACTIONS))}"
        endpoint_error = _endpoint_error(str(payload.get("cdp_endpoint") or DEFAULT_CDP_ENDPOINT))
        if endpoint_error:
            return endpoint_error

        wait_error = _validate_int_range(payload, "wait_ms", 0, 5000)
        if wait_error:
            return wait_error
        timeout_error = _validate_int_range(payload, "timeout_ms", 0, 30000)
        if timeout_error:
            return timeout_error
        max_chars_error = _validate_int_range(payload, "max_chars", 1, 200000)
        if max_chars_error:
            return max_chars_error

        if action == "navigate":
            raw_url = str(payload.get("url") or "").strip()
            if not raw_url:
                return "Missing url for navigate"
            parsed = urlparse(raw_url)
            # A workspace HTML target (``file://`` URL or a plain path) is a
            # valid navigation request: ``execute`` serves it over loopback
            # first. Only a genuinely unusable scheme is rejected here.
            if parsed.scheme not in {"http", "https"} and not _is_workspace_html_target(raw_url):
                return (
                    "navigate url must use http or https, or name a workspace HTML file "
                    "(that file is served over loopback automatically)."
                )
        elif action == "click":
            selector = str(payload.get("selector") or "").strip()
            has_xy = payload.get("x") not in (None, "") and payload.get("y") not in (None, "")
            if not selector and not has_xy:
                return "click requires selector or x/y coordinates"
            if has_xy:
                coord_error = _validate_float(payload, "x") or _validate_float(payload, "y")
                if coord_error:
                    return coord_error
        elif action == "type":
            if payload.get("text") is None:
                return "Missing text for type"
        elif action == "press_key":
            if not str(payload.get("key") or "").strip():
                return "Missing key for press_key"
        elif action == "scroll":
            for key in ("delta_x", "delta_y"):
                if payload.get(key) not in (None, ""):
                    coord_error = _validate_float(payload, key)
                    if coord_error:
                        return coord_error
        elif action == "evaluate":
            if not str(payload.get("expression") or "").strip():
                return "Missing expression for evaluate"
        elif action == "wait_for_element":
            if not str(payload.get("selector") or "").strip():
                return "Missing selector for wait_for_element"
        return ""

    def check_permission(self, args: dict[str, Any] | None = None, context=None) -> PermissionLevel | None:
        action = str((args or {}).get("action") or "").strip().lower()
        if action in self._WRITE_ACTIONS or action in self._PRIVATE_READ_ACTIONS:
            return PermissionLevel.CONFIRM
        return PermissionLevel.AUTO

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        action = str((args or {}).get("action") or "").strip().lower()
        return action not in self._WRITE_ACTIONS

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        action = str((args or {}).get("action") or "").strip().lower()
        return TOOL_SIDE_EFFECT_EXTERNAL if action in self._WRITE_ACTIONS else TOOL_SIDE_EFFECT_NONE

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        validation = self.validate_input(args)
        if validation:
            return self._error_result(validation)

        action = str(args.get("action") or "").strip().lower()
        if action == "navigate":
            # A workspace HTML file (or a file:// URL pointing at one) is a
            # legitimate navigation intent, not a policy violation. Serve it
            # over the owned loopback preview and continue with that URL so the
            # model does not have to discover preview_server on its own.
            resolved_url, resolve_error = await _resolved_navigation_url(
                str(args.get("url") or "").strip(),
                context,
            )
            if resolve_error:
                return self._error_result(resolve_error)
            args = {**args, "url": resolved_url}
            navigation_error = await _navigation_policy_error(resolved_url, context)
            if navigation_error:
                return self._error_result(navigation_error)
        endpoint = _normalize_endpoint(str(args.get("cdp_endpoint") or DEFAULT_CDP_ENDPOINT))
        try:
            embedded_endpoint = str(os.environ.get("MINICODE_EMBEDDED_BROWSER_ENDPOINT") or "").strip()
            if embedded_endpoint and not str(args.get("cdp_endpoint") or "").strip():
                return await self._execute_embedded(embedded_endpoint, action, args, context)
            if action == "discover":
                version, targets = await self._discover(endpoint)
                return ToolResult(
                    content=truncate_tool_result(_format_discovery(endpoint, version, targets), self._MAX_RESULT_CHARS),
                    result_kind=self.result_kind,
                    display_summary=f"CDP: {len(targets)} target(s)",
                )
            if action == "list_targets":
                targets = await self._list_targets(endpoint)
                return ToolResult(
                    content=truncate_tool_result(_format_targets(targets), self._MAX_RESULT_CHARS),
                    result_kind=self.result_kind,
                    display_summary=f"Browser targets: {len(targets)}",
                )
            if action == "navigate":
                return await self._navigate(endpoint, args)
            if action == "screenshot":
                return await self._screenshot(endpoint, args, context)
            if action == "get_url":
                return await self._get_url(endpoint, args)
            if action == "get_text":
                return await self._get_text(endpoint, args)
            if action == "get_html":
                return await self._get_html(endpoint, args)
            if action == "get_dom":
                return await self._get_dom(endpoint, args)
            if action == "wait_for_element":
                return await self._wait_for_element(endpoint, args)
            if action == "get_console_logs":
                return await self._get_console_logs(endpoint, args)
            if action == "get_network_logs":
                return await self._get_network_logs(endpoint, args)
            if action == "click":
                return await self._click(endpoint, args)
            if action == "type":
                return await self._type(endpoint, args)
            if action == "press_key":
                return await self._press_key(endpoint, args)
            if action == "scroll":
                return await self._scroll(endpoint, args)
            if action == "evaluate":
                return await self._evaluate(endpoint, args)
        except httpx.HTTPError as exc:
            return self._error_result(f"CDP HTTP request failed: {exc}")
        except TimeoutError as exc:
            return self._error_result(str(exc) or "CDP operation timed out")
        except RuntimeError as exc:
            return self._error_result(str(exc))
        except OSError as exc:
            return self._error_result(f"CDP connection failed: {exc}")

        return self._error_result(f"Unsupported action: {action}")

    async def _execute_embedded(
        self,
        endpoint: str,
        action: str,
        args: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> ToolResult:
        token = str(os.environ.get("MINICODE_EMBEDDED_BROWSER_TOKEN") or "")
        conversation_id = str(getattr(context, "conversation_id", "") or "").strip()
        if not conversation_id:
            return self._error_result("Embedded browser commands require a conversation owner")
        payload = {key: value for key, value in args.items() if key != "cdp_endpoint"}
        payload["conversation_id"] = conversation_id
        async with httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                f"{endpoint.rstrip('/')}/v1/command",
                headers={"authorization": f"Bearer {token}"},
                json=payload,
            )
        result = _json_dict(response.json())
        if response.status_code >= 400 or result.get("ok") is False:
            return self._error_result(str(result.get("error") or f"Embedded browser command failed: HTTP {response.status_code}"))

        targets = _json_list(result.get("targets"))
        target = _json_dict(result.get("target"))
        if action == "discover":
            lines = [
                f"Browser: {result.get('browser') or 'MiniCode Embedded Browser'}",
                f"Targets: {len(targets)}",
                *_format_targets(targets).splitlines(),
            ]
            return ToolResult(content=truncate_tool_result("\n".join(lines), self._MAX_RESULT_CHARS), result_kind=self.result_kind, display_summary=f"内置浏览器：{len(targets)} 个页面")
        if action == "list_targets":
            return ToolResult(content=truncate_tool_result(_format_targets(targets), self._MAX_RESULT_CHARS), result_kind=self.result_kind, display_summary=f"浏览器页面：{len(targets)}")
        if action == "screenshot":
            data = str(result.get("data") or "")
            if not data:
                return self._error_result("Embedded browser returned no screenshot data")
            if len(data) > API_IMAGE_MAX_BASE64_SIZE:
                return self._error_result("Embedded browser screenshot exceeds the 5 MiB API image limit")
            raw_size = len(base64.b64decode(data, validate=True))
            artifact_store = getattr(context, "artifact_store", None) if context else None
            artifact_id = artifact_store.save(
                f"data:{result.get('mimeType') or 'image/png'};base64,{data}",
                source="browser_control.embedded_screenshot",
                type="image_base64",
                preview_lines=1,
            ) if artifact_store is not None else None
            content = [
                "Screenshot captured.",
                f"Target: {target.get('id') or ''} {target.get('title') or ''}".rstrip(),
                f"URL: {target.get('url') or ''}",
                f"PNG bytes: {raw_size}",
            ]
            if artifact_id:
                content.append(f"Artifact: {artifact_id}")
            return ToolResult(content="\n".join(content), artifact_id=artifact_id, artifact_preview=f"PNG screenshot, {raw_size} bytes" if artifact_id else None, result_kind=self.result_kind, display_summary="浏览器截图")
        if action == "navigate":
            return ToolResult(content=f"Navigation requested.\nTarget: {target.get('id') or ''} {target.get('title') or ''}\nURL: {target.get('url') or args.get('url') or ''}", result_kind=self.result_kind, display_summary="浏览器已导航")
        if action == "get_url":
            return ToolResult(content=f"Target: {target.get('id') or ''}\nTitle: {target.get('title') or ''}\nURL: {target.get('url') or ''}", result_kind=self.result_kind, display_summary="浏览器地址")
        if action == "wait_for_element":
            if not result.get("value"):
                return self._error_result(str(result.get("error") or "Element was not found"))
            return ToolResult(content=f"Element found: {args.get('selector') or ''}", result_kind=self.result_kind, display_summary="已找到页面元素")
        if action == "click":
            return ToolResult(content=f"Clicked {result.get('value') or args.get('selector') or 'coordinates'}", result_kind=self.result_kind, display_summary="浏览器点击")
        if action == "type":
            return ToolResult(content=f"Typed {result.get('value') or 0} character(s)", result_kind=self.result_kind, display_summary="浏览器输入")
        if action == "press_key":
            return ToolResult(content=f"Pressed key: {result.get('value') or args.get('key') or ''}", result_kind=self.result_kind, display_summary="浏览器按键")
        value = result.get("value")
        content = json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else _stringify_value(value)
        labels = {
            "get_text": "浏览器文本",
            "get_html": "浏览器 HTML",
            "get_dom": "浏览器 DOM",
            "get_console_logs": "浏览器控制台",
            "get_network_logs": "浏览器网络记录",
            "scroll": "浏览器滚动",
            "evaluate": "浏览器执行结果",
        }
        return ToolResult(
            content=truncate_tool_result(content, _max_chars(args, self._MAX_RESULT_CHARS)),
            result_kind=self.result_kind,
            display_summary=labels.get(action, "浏览器操作完成"),
        )

    async def _discover(self, endpoint: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        async with httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=False) as client:
            version_resp = await client.get(_endpoint_url(endpoint, "/json/version"))
            version_resp.raise_for_status()
            targets_resp = await client.get(_endpoint_url(endpoint, "/json/list"))
            targets_resp.raise_for_status()
        version = _json_dict(version_resp.json())
        targets = _json_list(targets_resp.json())
        return version, targets[: self._MAX_TARGETS]

    async def _list_targets(self, endpoint: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=False) as client:
            response = await client.get(_endpoint_url(endpoint, "/json/list"))
            response.raise_for_status()
        return _json_list(response.json())[: self._MAX_TARGETS]

    async def _navigate(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        nav_url = str(args.get("url") or "").strip()
        wait_ms = int(args.get("wait_ms") or 0)
        async with _cdp_session(target_ws) as session:
            await session.call("Page.enable")
            result = await session.call("Page.navigate", {"url": nav_url})
            if wait_ms > 0:
                await session.drain_events(wait_ms / 1000)
        lines = [
            "Navigation requested.",
            f"Target: {target.get('id')} {target.get('title') or ''}".rstrip(),
            f"URL: {nav_url}",
        ]
        if isinstance(result, dict) and result.get("loaderId"):
            lines.append(f"loaderId: {result.get('loaderId')}")
        if isinstance(result, dict) and result.get("errorText"):
            lines.append(f"errorText: {result.get('errorText')}")
        return ToolResult(content="\n".join(lines), result_kind=self.result_kind, display_summary="Browser navigated")

    async def _screenshot(
        self,
        endpoint: str,
        args: dict[str, Any],
        context: ToolExecutionContext | None,
    ) -> ToolResult:
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        async with _cdp_session(target_ws) as session:
            await session.call("Page.enable")
            result = await session.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        data = str((result or {}).get("data") or "")
        if not data:
            return self._error_result("CDP returned no screenshot data")
        if len(data) > API_IMAGE_MAX_BASE64_SIZE:
            return self._error_result("CDP screenshot exceeds the 5 MiB API image limit")
        try:
            raw_size = len(base64.b64decode(data, validate=True))
        except Exception:
            raw_size = 0
        artifact_store = getattr(context, "artifact_store", None) if context else None
        artifact_id = None
        if artifact_store is not None:
            artifact_id = artifact_store.save(
                f"data:image/png;base64,{data}",
                source="browser_control.screenshot",
                type="image_base64",
                preview_lines=1,
            )
        content = [
            "Screenshot captured.",
            f"Target: {target.get('id')} {target.get('title') or ''}".rstrip(),
            f"URL: {target.get('url') or ''}",
            f"PNG bytes: {raw_size}",
        ]
        if artifact_id:
            content.append(f"Artifact: {artifact_id}")
        else:
            content.append(f"Base64 chars: {len(data)}")
        return ToolResult(
            content="\n".join(content),
            artifact_id=artifact_id,
            artifact_preview=f"PNG screenshot, {raw_size} bytes" if artifact_id else None,
            result_kind=self.result_kind,
            display_summary="Browser screenshot",
        )

    async def _get_url(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        lines = [
            f"Target: {target.get('id')}",
            f"Title: {target.get('title') or ''}",
            f"URL: {target.get('url') or ''}",
        ]
        return ToolResult(content="\n".join(lines), result_kind=self.result_kind, display_summary="Browser URL")

    async def _get_text(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        max_chars = _max_chars(args, self._MAX_RESULT_CHARS)
        value = await self._evaluate_readonly(
            endpoint,
            args,
            f"String(document.body ? document.body.innerText : '').slice(0, {max_chars})",
        )
        content = _stringify_value(value)
        return ToolResult(
            content=truncate_tool_result(content, max_chars),
            result_kind=self.result_kind,
            display_summary="Browser text",
        )

    async def _get_html(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        max_chars = _max_chars(args, self._MAX_RESULT_CHARS)
        value = await self._evaluate_readonly(
            endpoint,
            args,
            f"String(document.documentElement ? document.documentElement.outerHTML : '').slice(0, {max_chars})",
        )
        content = _stringify_value(value)
        return ToolResult(
            content=truncate_tool_result(content, max_chars),
            result_kind=self.result_kind,
            display_summary="Browser HTML",
        )

    async def _get_dom(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        value = await self._evaluate_readonly(endpoint, args, _DOM_SNAPSHOT_JS)
        content = json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value
        return ToolResult(
            content=truncate_tool_result(content, _max_chars(args, self._MAX_RESULT_CHARS)),
            result_kind=self.result_kind,
            display_summary="Browser DOM",
        )

    async def _wait_for_element(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "").strip()
        timeout_ms = int(args.get("timeout_ms") or 5000)
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        expression = f"Boolean(document.querySelector({json.dumps(selector)}))"
        async with _cdp_session(target_ws) as session:
            await session.call("Runtime.enable")
            while True:
                found = bool(_runtime_value(await _runtime_evaluate(session, expression)))
                if found:
                    return ToolResult(
                        content=f"Element found: {selector}",
                        result_kind=self.result_kind,
                        display_summary="Element found",
                    )
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.2)
        return self._error_result(f"Timed out waiting for element: {selector}")

    async def _get_console_logs(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        wait_ms = int(args.get("wait_ms") or 250)
        async with _cdp_session(target_ws) as session:
            await session.call("Runtime.enable")
            try:
                await session.call("Log.enable")
            except RuntimeError:
                pass
            if wait_ms > 0:
                await session.drain_events(wait_ms / 1000)
            events = session.events("Runtime.consoleAPICalled") + session.events("Log.entryAdded")
        content = _format_console_events(events)
        return ToolResult(
            content=truncate_tool_result(content, _max_chars(args, self._MAX_RESULT_CHARS)),
            result_kind=self.result_kind,
            display_summary=f"Console logs: {len(events)}",
        )

    async def _get_network_logs(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        wait_ms = int(args.get("wait_ms") or 250)
        async with _cdp_session(target_ws) as session:
            await session.call("Network.enable")
            if wait_ms > 0:
                await session.drain_events(wait_ms / 1000)
            events = session.events()
        content, count = _format_network_events(events)
        return ToolResult(
            content=truncate_tool_result(content, _max_chars(args, self._MAX_RESULT_CHARS)),
            result_kind=self.result_kind,
            display_summary=f"Network requests: {count}",
        )

    async def _click(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "").strip()
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        async with _cdp_session(target_ws) as session:
            await session.call("Runtime.enable")
            if selector:
                point = _runtime_value(await _runtime_evaluate(session, _selector_point_js(selector), user_gesture=True))
                if not isinstance(point, dict) or not point.get("ok"):
                    return self._error_result(str((point or {}).get("error") or f"Element not found: {selector}"))
                x = float(point.get("x") or 0)
                y = float(point.get("y") or 0)
            else:
                x = float(args.get("x"))
                y = float(args.get("y"))
            await session.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            await session.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        target_text = selector if selector else f"{x:g},{y:g}"
        return ToolResult(content=f"Clicked {target_text}", result_kind=self.result_kind, display_summary="Browser click")

    async def _type(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "").strip()
        text = str(args.get("text") or "")
        clear = _bool_arg(args.get("clear"))
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        async with _cdp_session(target_ws) as session:
            await session.call("Runtime.enable")
            if selector:
                focus_result = _runtime_value(await _runtime_evaluate(session, _focus_selector_js(selector, clear), user_gesture=True))
                if not isinstance(focus_result, dict) or not focus_result.get("ok"):
                    return self._error_result(str((focus_result or {}).get("error") or f"Element not found: {selector}"))
            await session.call("Input.insertText", {"text": text})
        target_text = selector or "active element"
        return ToolResult(content=f"Typed {len(text)} character(s) into {target_text}", result_kind=self.result_kind, display_summary="Browser type")

    async def _press_key(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        key = str(args.get("key") or "").strip()
        text = key if len(key) == 1 else ""
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        async with _cdp_session(target_ws) as session:
            await session.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "text": text})
            await session.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})
        return ToolResult(content=f"Pressed key: {key}", result_kind=self.result_kind, display_summary="Browser key")

    async def _scroll(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        selector = str(args.get("selector") or "").strip()
        delta_x = float(args.get("delta_x") or 0)
        delta_y = float(args.get("delta_y") if args.get("delta_y") not in (None, "") else 600)
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        async with _cdp_session(target_ws) as session:
            await session.call("Runtime.enable")
            if selector:
                value = _runtime_value(await _runtime_evaluate(session, _scroll_selector_js(selector), user_gesture=True))
                if isinstance(value, dict) and not value.get("ok"):
                    return self._error_result(str(value.get("error") or f"Element not found: {selector}"))
            else:
                value = _runtime_value(await _runtime_evaluate(session, _scroll_by_js(delta_x, delta_y), user_gesture=True))
        content = _stringify_value(value)
        return ToolResult(content=content, result_kind=self.result_kind, display_summary="Browser scroll")

    async def _evaluate(self, endpoint: str, args: dict[str, Any]) -> ToolResult:
        expression = str(args.get("expression") or "")
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        async with _cdp_session(target_ws) as session:
            await session.call("Runtime.enable")
            value = _runtime_value(await _runtime_evaluate(session, expression, user_gesture=False))
        content = _stringify_value(value)
        return ToolResult(
            content=truncate_tool_result(content, _max_chars(args, self._MAX_RESULT_CHARS)),
            result_kind=self.result_kind,
            display_summary="Browser evaluate",
        )

    async def _evaluate_readonly(self, endpoint: str, args: dict[str, Any], expression: str) -> Any:
        target = await self._select_target(endpoint, str(args.get("target_id") or "").strip())
        target_ws = _validate_ws_url(str(target.get("webSocketDebuggerUrl") or ""))
        async with _cdp_session(target_ws) as session:
            await session.call("Runtime.enable")
            return _runtime_value(await _runtime_evaluate(session, expression))

    async def _select_target(self, endpoint: str, target_id: str) -> dict[str, Any]:
        targets = await self._list_targets(endpoint)
        pages = [target for target in targets if str(target.get("type") or "") == "page"]
        if target_id:
            for target in targets:
                if str(target.get("id") or "") == target_id:
                    if str(target.get("type") or "") != "page":
                        raise RuntimeError(f"Target is not a page: {target_id}")
                    return target
            raise RuntimeError(f"Browser target not found: {target_id}")
        if not pages:
            raise RuntimeError("No page targets available from CDP")
        return pages[0]

