from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, TYPE_CHECKING
from urllib.parse import urlsplit

from backend.agent.message import AgentEvent
from backend.cancellation_signal import CancellationSignal
from backend.secret_redaction import redact_secrets
from backend.services.mcp_service import (
    MCPServiceError,
    MCPInventoryServiceError,
    add_mcp_server,
    approve_project_mcp,
    get_mcp_status,
    list_mcp_inventory,
    login_mcp_server,
    logout_mcp_server,
    remove_mcp_server,
    reject_project_mcp,
    restart_mcp_server,
    toggle_mcp_server,
    update_mcp_server,
)
from backend.ws.command_results import emit_command_error

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


def _session_mcp_manager(session: "WebSocketSession") -> Any | None:
    manager = getattr(session, "mcp_manager", None)
    if manager is not None:
        return manager
    from backend.api.routes_health import get_mcp_manager

    return get_mcp_manager()


async def _reload_other_mcp_managers(session: "WebSocketSession") -> None:
    from backend.api import _state

    bootstrap = getattr(_state, "bootstrap", None)
    reload_managers = getattr(bootstrap, "reload_mcp_managers", None)
    if callable(reload_managers):
        await reload_managers(exclude=_session_mcp_manager(session))


class _ProviderOAuthCallbacks:
    """Project MiniCode provider auth interactions onto one owner-scoped WS lane."""

    _PROMPT_TIMEOUT_SECONDS = 300.0
    _TEXT_LIMIT = 8_192
    _URL_LIMIT = 4_096
    _MAX_LINKS = 16
    _MAX_OPTIONS = 64

    def __init__(
        self,
        session: "WebSocketSession",
        conversation_id: str,
        provider: str,
    ) -> None:
        self.session = session
        self.conversation_id = conversation_id
        self.provider = str(provider or "").strip()
        if not self.conversation_id or not self.provider:
            raise ValueError("OAuth callbacks require a conversation owner and provider")
        self._abort_event = asyncio.Event()
        self.signal = CancellationSignal(self._abort_event)
        self._notification_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    @classmethod
    def _text(
        cls,
        value: Any,
        *,
        field: str,
        required: bool = False,
        limit: int | None = None,
    ) -> str:
        if value is None:
            if required:
                raise ValueError(f"OAuth {field} is required")
            return ""
        if not isinstance(value, str):
            raise ValueError(f"OAuth {field} must be a string")
        text = value.strip()
        if required and not text:
            raise ValueError(f"OAuth {field} is required")
        maximum = cls._TEXT_LIMIT if limit is None else limit
        if len(text) > maximum:
            raise ValueError(f"OAuth {field} exceeds {maximum} characters")
        return text

    @classmethod
    def _http_url(cls, value: Any, *, field: str) -> str:
        url = cls._text(value, field=field, required=True, limit=cls._URL_LIMIT)
        if any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
            for character in url
        ):
            raise ValueError(f"OAuth {field} must not contain whitespace or control characters")
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            parsed.port
        except ValueError as exc:
            raise ValueError(f"OAuth {field} must be a valid absolute HTTP(S) URL") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(f"OAuth {field} must be an absolute HTTP(S) URL")
        return url

    @classmethod
    def _display_text(
        cls,
        value: Any,
        *,
        field: str,
        required: bool = False,
        limit: int | None = None,
    ) -> str:
        return redact_secrets(
            cls._text(value, field=field, required=required, limit=limit)
        )

    @staticmethod
    def _positive_number(value: Any, *, field: str) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"OAuth {field} must be a positive number")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"OAuth {field} must be a positive number")
        return int(number) if number.is_integer() else number

    @staticmethod
    def _mapping(payload: Any, *, description: str) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError(f"OAuth {description} must be an object")
        return dict(payload)

    def _assert_owner(self) -> None:
        active = str(getattr(self.session, "active_conversation_id", "") or "").strip()
        if active != self.conversation_id:
            raise RuntimeError("OAuth login owner conversation is no longer active")

    def _schedule_notification(
        self,
        event_type: str,
        data: dict[str, Any],
    ) -> asyncio.Task[None]:
        if self._closed:
            raise RuntimeError("OAuth callback interaction is closed")
        task = asyncio.create_task(
            self._emit(event_type, data),
            name=f"provider-oauth-notify:{self.provider}:{event_type}",
        )
        self._notification_tasks.add(task)
        return task

    def notify(self, payload: Any) -> asyncio.Task[None]:
        raw = self._mapping(payload, description="notification")
        event_type = self._text(raw.get("type"), field="notification type", required=True, limit=64)
        if event_type == "auth_url":
            data = {"url": self._http_url(raw.get("url"), field="url")}
            instructions = self._display_text(
                raw.get("instructions"),
                field="instructions",
            )
            if instructions:
                data["instructions"] = instructions
            return self._schedule_notification("llm.provider.oauth.auth", data)
        if event_type == "device_code":
            data: dict[str, Any] = {
                "userCode": self._text(
                    raw.get("user_code"),
                    field="user_code",
                    required=True,
                    limit=512,
                ),
                "verificationUri": self._http_url(
                    raw.get("verification_uri"),
                    field="verification_uri",
                ),
            }
            for source, target in (
                ("interval_seconds", "intervalSeconds"),
                ("expires_in_seconds", "expiresInSeconds"),
            ):
                value = raw.get(source)
                if value is not None:
                    data[target] = self._positive_number(value, field=source)
            return self._schedule_notification(
                "llm.provider.oauth.device_code",
                data,
            )
        if event_type == "progress":
            return self._schedule_notification(
                "llm.provider.oauth.progress",
                {
                    "message": self._display_text(
                        raw.get("message"),
                        field="message",
                        required=True,
                    )
                },
            )
        if event_type == "info":
            data: dict[str, Any] = {
                "message": self._display_text(raw.get("message"), field="message", required=True),
            }
            raw_links = raw.get("links")
            if raw_links is not None:
                if (
                    not isinstance(raw_links, Sequence)
                    or isinstance(raw_links, (str, bytes, bytearray))
                    or len(raw_links) > self._MAX_LINKS
                ):
                    raise ValueError("OAuth info links must be a bounded array")
                links: list[dict[str, str]] = []
                for index, item in enumerate(raw_links):
                    link = self._mapping(item, description=f"info link {index}")
                    projected = {"url": self._http_url(link.get("url"), field=f"links[{index}].url")}
                    label = self._display_text(
                        link.get("label"),
                        field=f"links[{index}].label",
                        limit=512,
                    )
                    if label:
                        projected["label"] = label
                    links.append(projected)
                data["links"] = links
            return self._schedule_notification("llm.provider.oauth.info", data)
        raise ValueError(f"Unsupported OAuth notification type: {event_type}")

    def _normalize_prompt(self, payload: Any) -> dict[str, Any]:
        raw = self._mapping(payload, description="prompt")
        prompt_type = self._text(
            raw.get("type", "text"),
            field="prompt type",
            required=True,
            limit=32,
        )
        if prompt_type not in {"text", "secret", "select", "manual_code"}:
            raise ValueError(f"Unsupported OAuth prompt type: {prompt_type}")
        message = self._display_text(
            raw.get("message"),
            field="prompt message",
            required=True,
        )
        placeholder = self._display_text(
            raw.get("placeholder"),
            field="placeholder",
            limit=1_024,
        )
        allow_empty_raw = raw.get("allow_empty", False)
        if not isinstance(allow_empty_raw, bool):
            raise ValueError("OAuth allow_empty must be a boolean")
        normalized: dict[str, Any] = {
            "prompt_type": prompt_type,
            "message": message,
            "allow_empty": allow_empty_raw if prompt_type in {"text", "secret", "manual_code"} else False,
            "signal": raw.get("signal"),
        }
        if placeholder:
            normalized["placeholder"] = placeholder
        if prompt_type == "select":
            raw_options = raw.get("options")
            if (
                not isinstance(raw_options, Sequence)
                or isinstance(raw_options, (str, bytes, bytearray))
                or not raw_options
                or len(raw_options) > self._MAX_OPTIONS
            ):
                raise ValueError("OAuth select options must be a non-empty bounded array")
            options: list[dict[str, str]] = []
            seen_ids: set[str] = set()
            for index, item in enumerate(raw_options):
                option = self._mapping(item, description=f"select option {index}")
                option_id = self._text(
                    option.get("id"),
                    field=f"options[{index}].id",
                    required=True,
                    limit=512,
                )
                if option_id in seen_ids:
                    raise ValueError(f"OAuth select option id is duplicated: {option_id}")
                seen_ids.add(option_id)
                projected = {
                    "id": option_id,
                    "label": self._display_text(
                        option.get("label"),
                        field=f"options[{index}].label",
                        required=True,
                        limit=1_024,
                    ),
                }
                description = self._display_text(
                    option.get("description"),
                    field=f"options[{index}].description",
                    limit=2_048,
                )
                if description:
                    projected["description"] = description
                options.append(projected)
            normalized["options"] = options
        return normalized

    @staticmethod
    async def _wait_for_abort_signal(signal: CancellationSignal) -> None:
        await signal.wait()

    async def prompt(self, payload: Any) -> str:
        if self._closed:
            raise RuntimeError("OAuth callback interaction is closed")
        prompt = self._normalize_prompt(payload)
        request_id = f"llm-oauth-{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        future.conversation_id = self.conversation_id  # type: ignore[attr-defined]
        pending = getattr(self.session, "_provider_oauth_pending", None)
        if not isinstance(pending, dict):
            pending = {}
            setattr(self.session, "_provider_oauth_pending", pending)
        self._assert_owner()
        expires_at = int(time.time() * 1000) + int(self._PROMPT_TIMEOUT_SECONDS * 1_000)
        request: dict[str, Any] = {
            "subtype": "provider_auth_prompt",
            "prompt": prompt["message"],
            "provider": self.provider,
            "prompt_type": prompt["prompt_type"],
            "allow_empty": prompt["allow_empty"],
            "allow_custom": prompt["prompt_type"] != "select",
        }
        if prompt.get("placeholder"):
            request["placeholder"] = prompt["placeholder"]
        if prompt.get("options"):
            request["options"] = prompt["options"]
        control_payload = {
            "type": "control_request",
            "request_id": request_id,
            "conversation_id": self.conversation_id,
            "timeout_seconds": self._PROMPT_TIMEOUT_SECONDS,
            "expires_at": expires_at,
            "request": request,
        }
        pending_approvals = getattr(self.session, "_pending_approvals", None)
        if not isinstance(pending_approvals, dict):
            pending_approvals = {}
            setattr(self.session, "_pending_approvals", pending_approvals)
        pending_payloads = getattr(self.session, "_pending_approval_payloads", None)
        if not isinstance(pending_payloads, dict):
            pending_payloads = {}
            setattr(self.session, "_pending_approval_payloads", pending_payloads)
        pending[request_id] = future
        pending_approvals[request_id] = future
        pending_payloads[request_id] = control_payload
        abort_waiters: list[asyncio.Task[None]] = [
            asyncio.create_task(
                self._abort_event.wait(),
                name=f"provider-oauth-login-abort:{request_id}",
            ),
        ]
        if prompt.get("signal") is not None:
            if not isinstance(prompt["signal"], CancellationSignal):
                raise ValueError(
                    "OAuth prompt signal must be a MiniCode CancellationSignal"
                )
            abort_waiters.append(asyncio.create_task(
                self._wait_for_abort_signal(prompt["signal"]),
                name=f"provider-oauth-prompt-abort:{request_id}",
            ))
        try:
            sent = await self.session._send_ws_payload(
                control_payload,
                log_context="llm.provider.oauth.prompt",
            )
            if not sent:
                raise ConnectionError("OAuth prompt could not be delivered to the owning session")
            done, _pending_waiters = await asyncio.wait(
                [future, *abort_waiters],
                timeout=self._PROMPT_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise asyncio.TimeoutError
            if future not in done:
                raise asyncio.CancelledError("OAuth prompt was aborted")
            response = future.result()
            if isinstance(response, dict) and str(response.get("action") or "").lower() in {
                "cancel",
                "deny",
                "reject",
            }:
                await self.session._emit_approval_cancelled_once(
                    [request_id],
                    reason="provider_auth_rejected",
                    conversation_id=self.conversation_id,
                )
                raise PermissionError("OAuth prompt was cancelled by the user")
            if isinstance(response, dict):
                answer = response["answer"] if "answer" in response else response.get("value")
            else:
                answer = response
            if answer is None:
                answer = ""
            if not isinstance(answer, (str, int, float, bool)):
                raise ValueError("OAuth prompt response must be a scalar value")
            answer_text = str(answer)
            if not prompt["allow_empty"] and not answer_text:
                raise ValueError("OAuth prompt response must not be empty")
            if prompt["prompt_type"] == "select":
                option_ids = {option["id"] for option in prompt.get("options", [])}
                if answer_text not in option_ids:
                    raise ValueError("OAuth select response does not match an offered option id")
            await self.session._emit_approval_cancelled_once(
                [request_id],
                reason="provider_auth_resolved",
                conversation_id=self.conversation_id,
            )
            return answer_text
        except asyncio.TimeoutError:
            await self.session._emit_approval_cancelled_once(
                [request_id],
                reason="provider_auth_timeout",
                conversation_id=self.conversation_id,
            )
            raise
        except asyncio.CancelledError:
            await self.session._emit_approval_cancelled_once(
                [request_id],
                reason="provider_auth_cancelled",
                conversation_id=self.conversation_id,
            )
            raise
        except PermissionError:
            # The reject branch above already emitted provider_auth_rejected;
            # re-raise without the duplicate failed terminal event.
            raise
        except Exception:
            await self.session._emit_approval_cancelled_once(
                [request_id],
                reason="provider_auth_failed",
                conversation_id=self.conversation_id,
            )
            raise
        finally:
            for waiter in abort_waiters:
                if not waiter.done():
                    waiter.cancel()
            if abort_waiters:
                await asyncio.gather(*abort_waiters, return_exceptions=True)
            pending.pop(request_id, None)
            pending_approvals.pop(request_id, None)
            pending_payloads.pop(request_id, None)

    async def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        self._assert_owner()
        sent = await self.session._send_ws_payload(
            {
                "type": event_type,
                "conversation_id": self.conversation_id,
                "provider": self.provider,
                **data,
            },
            log_context=event_type,
        )
        if not sent:
            raise ConnectionError(f"OAuth event {event_type} could not be delivered")

    async def drain(self) -> None:
        self._assert_owner()
        await self._drain_notifications(raise_errors=True)
        self._assert_owner()

    async def _drain_notifications(self, *, raise_errors: bool) -> None:
        tasks = tuple(self._notification_tasks)
        self._notification_tasks.clear()
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if not raise_errors:
            return
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                raise result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._abort_event.set()
        await self._drain_notifications(raise_errors=False)


def _mcp_inventory_tasks(session: "WebSocketSession") -> dict[str, asyncio.Task[dict[str, Any]]]:
    tasks = getattr(session, "_mcp_inventory_tasks", None)
    if not isinstance(tasks, dict):
        tasks = {}
        setattr(session, "_mcp_inventory_tasks", tasks)
    return tasks


def _mcp_inventory_cancelled(session: "WebSocketSession") -> set[str]:
    cancelled = getattr(session, "_mcp_inventory_cancelled", None)
    if not isinstance(cancelled, set):
        cancelled = set()
        setattr(session, "_mcp_inventory_cancelled", cancelled)
    return cancelled


def _server_status_entry(servers: Any, name: str) -> dict[str, Any] | None:
    """Find one server's status projection in an ``mcp_status`` server list.

    The connection verdict is the whole point of an MCP control-plane command:
    without it a server that failed to connect produced a bare ``level="info"``
    result indistinguishable from a successful one.
    """

    wanted = str(name or "").strip()
    if not wanted or not isinstance(servers, list):
        return None
    for entry in servers:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip() == wanted:
            return entry
    return None


def _mcp_command_result_data(
    session: "WebSocketSession",
    refreshed: bool,
    *,
    name: str,
    notice: str = "",
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return MCP control-plane feedback without writing into the transcript."""

    data: dict[str, Any] = {"name": name}
    if notice:
        data["notice"] = notice
    if status:
        data["connection_status"] = str(status.get("status") or "")
        if status.get("error"):
            data["connection_error"] = str(status["error"])
    if refreshed:
        return data
    has_active_run = getattr(session, "_has_active_run", None)
    if callable(has_active_run) and has_active_run():
        data["tool_availability"] = "next_turn"
    return data


async def handle_mcp_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    try:
        servers = get_mcp_status(_session_mcp_manager(session))
    except MCPServiceError as exc:
        await emit_command_error(session, "mcp.list", exc)
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    await session._send_event(AgentEvent.command_result("mcp.list", ""))
    return True


async def handle_mcp_inventory_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Run the standard MCP inventory methods only for an explicit UI request."""

    server_name = str(data.get("name") or "").strip()
    operation_id = str(data.get("operation_id") or "").strip()
    if not server_name:
        await emit_command_error(
            session,
            "mcp.inventory.list",
            "MCP server name is required",
            data={"error_code": "invalid_request", "recoverable": False},
        )
        return True
    if not operation_id:
        await emit_command_error(
            session,
            "mcp.inventory.list",
            "MCP inventory operation ID is required",
            data={"error_code": "invalid_request", "recoverable": False},
        )
        return True

    tasks = _mcp_inventory_tasks(session)
    if operation_id in tasks:
        await emit_command_error(
            session,
            "mcp.inventory.list",
            "MCP inventory operation is already running",
            data={
                "operation_id": operation_id,
                "name": server_name,
                "error_code": "operation_conflict",
                "recoverable": True,
            },
        )
        return True

    task = asyncio.create_task(
        list_mcp_inventory(_session_mcp_manager(session), server_name),
        name=f"mcp-inventory:{server_name}:{operation_id}",
    )
    tasks[operation_id] = task
    try:
        inventory = await task
    except asyncio.CancelledError:
        cancelled = _mcp_inventory_cancelled(session)
        if operation_id not in cancelled:
            raise
        await emit_command_error(
            session,
            "mcp.inventory.list",
            "MCP inventory request was cancelled",
            data={
                "operation_id": operation_id,
                "name": server_name,
                "error_code": "cancelled",
                "recoverable": True,
            },
        )
    except MCPInventoryServiceError as exc:
        await emit_command_error(
            session,
            "mcp.inventory.list",
            exc,
            data={
                "operation_id": operation_id,
                "name": server_name,
                "error_code": exc.code,
                "recoverable": exc.recoverable,
                **exc.details,
            },
        )
    except Exception as exc:
        logger.exception("Unexpected MCP inventory failure for %s", server_name)
        await emit_command_error(
            session,
            "mcp.inventory.list",
            exc,
            data={
                "operation_id": operation_id,
                "name": server_name,
                "error_code": "internal_error",
                "recoverable": True,
            },
        )
    else:
        await session._send_event(
            AgentEvent.command_result(
                "mcp.inventory.list",
                "",
                data={"operation_id": operation_id, "inventory": inventory},
            )
        )
    finally:
        if tasks.get(operation_id) is task:
            tasks.pop(operation_id, None)
        _mcp_inventory_cancelled(session).discard(operation_id)
    return True


async def handle_mcp_inventory_cancel(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    operation_id = str(data.get("operation_id") or "").strip()
    server_name = str(data.get("name") or "").strip()
    if not operation_id:
        await emit_command_error(
            session,
            "mcp.inventory.cancel",
            "MCP inventory operation ID is required",
            data={"error_code": "invalid_request", "recoverable": False},
        )
        return True

    task = _mcp_inventory_tasks(session).get(operation_id)
    cancelled = bool(task and not task.done())
    if cancelled and task is not None:
        _mcp_inventory_cancelled(session).add(operation_id)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await session._send_event(
        AgentEvent.command_result(
            "mcp.inventory.cancel",
            "",
            data={
                "operation_id": operation_id,
                "name": server_name,
                "cancelled": cancelled,
            },
        )
    )
    return True


async def _run_mcp_server_command(
    session: "WebSocketSession",
    data: dict[str, Any],
    *,
    command: str,
    invoke: Any,
    reload_other_managers: bool = False,
    catch: tuple[type[BaseException], ...] = (MCPServiceError,),
    error_template: str | None = None,
) -> bool:
    """Shared body for the mcp.{add,remove,restart,update,toggle} commands.

    Every one of them mutates server config, optionally reloads the other
    in-process MCP managers, refreshes the tool registry, then answers with
    an ``mcp_status`` payload plus a per-server command result. Only the
    service call, reload scope, exception surface, and error wording differ;
    keep those differences explicit per command instead of duplicating the
    thirty-line response shape five times.
    """
    try:
        servers = await invoke(_session_mcp_manager(session), data)
        if reload_other_managers:
            await _reload_other_mcp_managers(session)
        refreshed = session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except catch as exc:
        error: BaseException | str = exc
        if error_template is not None:
            error = error_template.format(exc=exc)
        await emit_command_error(session, command, error)
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    name = str(data.get("name", "")).strip()
    await session._send_event(
        AgentEvent.command_result(
            command,
            "",
            data=_mcp_command_result_data(
                session,
                refreshed,
                name=name,
                status=_server_status_entry(servers, name),
            ),
        )
    )
    return True


async def handle_mcp_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _run_mcp_server_command(
        session,
        data,
        command="mcp.add",
        invoke=add_mcp_server,
        reload_other_managers=True,
        catch=(Exception,),
        error_template="Failed to add MCP server: {exc}",
    )


async def handle_mcp_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _run_mcp_server_command(
        session,
        data,
        command="mcp.remove",
        invoke=lambda mgr, payload: remove_mcp_server(mgr, payload.get("name", "")),
        reload_other_managers=True,
    )


async def handle_mcp_restart(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _run_mcp_server_command(
        session,
        data,
        command="mcp.restart",
        invoke=lambda mgr, payload: restart_mcp_server(mgr, payload.get("name", "")),
    )


async def _handle_project_mcp_decision(
    session: "WebSocketSession",
    data: dict[str, Any],
    *,
    command: str,
    approved: bool,
    approve_all: bool = False,
) -> bool:
    from backend.ws.command_scope import resolve_command_scope

    try:
        scope = resolve_command_scope(session, data)
        if approved:
            servers = await approve_project_mcp(
                _session_mcp_manager(session),
                str(data.get("name") or ""),
                workspace_root=scope.workspace_root,
                approve_all=approve_all,
            )
        else:
            servers = await reject_project_mcp(
                _session_mcp_manager(session),
                str(data.get("name") or ""),
                workspace_root=scope.workspace_root,
            )
        refreshed = session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except Exception as exc:
        await emit_command_error(session, command, exc)
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    name = str(data.get("name") or "").strip()
    await session._send_event(
        AgentEvent.command_result(
            command,
            "",
            data=_mcp_command_result_data(
                session,
                refreshed,
                name=name,
                notice=f"Project MCP decision applied to {scope.workspace_root}",
                status=_server_status_entry(servers, name),
            ),
        )
    )
    return True


async def handle_mcp_project_approve(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_project_mcp_decision(
        session,
        data,
        command="mcp.project.approve",
        approved=True,
    )


async def handle_mcp_project_approve_all(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_project_mcp_decision(
        session,
        data,
        command="mcp.project.approve_all",
        approved=True,
        approve_all=True,
    )


async def handle_mcp_project_reject(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_project_mcp_decision(
        session,
        data,
        command="mcp.project.reject",
        approved=False,
    )


# ---------------------------------------------------------------------------
# Env vault handlers
# ---------------------------------------------------------------------------


async def handle_env_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.env_vault_service import list_env_entries

    await session._send_ws_payload(
        {"type": "env.list", "entries": list_env_entries().entries},
        log_context="env.list",
    )
    return True


async def handle_env_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.env_vault_service import EnvVaultServiceError, set_env_entry

    try:
        result = set_env_entry(data)
    except EnvVaultServiceError as exc:
        await emit_command_error(session, "env.set", exc)
        return True
    await session._send_ws_payload(
        {"type": "env.list", "entries": result.entries},
        log_context="env.list",
    )
    await session._send_event(
        AgentEvent.command_result("env.set", "", data={"name": str(data.get("name", "")).strip()})
    )
    return True


async def handle_env_delete(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.env_vault_service import EnvVaultServiceError, delete_env_entry

    try:
        result = delete_env_entry(data)
    except EnvVaultServiceError as exc:
        await emit_command_error(session, "env.delete", exc)
        return True
    await session._send_ws_payload(
        {"type": "env.list", "entries": result.entries},
        log_context="env.list",
    )
    await session._send_event(
        AgentEvent.command_result("env.delete", "", data={"name": str(data.get("name", "")).strip()})
    )
    return True


# ---------------------------------------------------------------------------
# Scheduler handlers
# ---------------------------------------------------------------------------


def _get_scheduler(session):
    """Get the shared TaskScheduler from bootstrap state."""
    from backend.services.scheduler_service import get_scheduler_from_bootstrap

    return get_scheduler_from_bootstrap()


def _scheduler_command_scope(session: "WebSocketSession", data: dict[str, Any]):
    from backend.ws.command_scope import resolve_command_scope

    return resolve_command_scope(session, data)


def _scheduler_snapshot_payload(result: Any, scope: Any) -> dict[str, Any]:
    return scope.apply({"type": "scheduler.list", "tasks": result.tasks, "runs": result.runs})


async def _send_scheduler_snapshot(session: "WebSocketSession", scheduler: Any, *, scope: Any) -> None:
    from backend.services.scheduler_service import list_scheduled_tasks

    result = list_scheduled_tasks(scheduler, workspace_root=scope.workspace_root)
    await session._send_ws_payload(
        _scheduler_snapshot_payload(result, scope),
        log_context="scheduler.list",
    )


async def handle_scheduler_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    scheduler = _get_scheduler(session)
    try:
        scope = _scheduler_command_scope(session, data)
    except ValueError as exc:
        await emit_command_error(session, "scheduler.list", exc)
        return True
    await _send_scheduler_snapshot(session, scheduler, scope=scope)
    return True


async def handle_scheduler_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, add_scheduled_task

    scheduler = _get_scheduler(session)
    try:
        scope = _scheduler_command_scope(session, data)
        result = add_scheduled_task(scheduler, data, workspace_root=scope.workspace_root)
    except (SchedulerServiceError, ValueError) as exc:
        await emit_command_error(session, "scheduler.add", exc)
        return True
    await session._send_ws_payload(
        _scheduler_snapshot_payload(result, scope),
        log_context="scheduler.list",
    )
    await session._send_event(
        AgentEvent.command_result("scheduler.add", "", data={"name": str(data.get("name", "")).strip()})
    )
    return True


async def handle_mcp_update(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _run_mcp_server_command(
        session,
        data,
        command="mcp.update",
        invoke=lambda manager, payload: update_mcp_server(manager, payload),
        reload_other_managers=True,
        catch=(Exception,),
        error_template="Failed to update MCP server: {exc}",
    )


async def handle_mcp_toggle(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    async def invoke(manager: Any, payload: dict[str, Any]) -> Any:
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise MCPServiceError("MCP enabled must be a boolean")
        return await toggle_mcp_server(
            manager,
            str(payload.get("name") or ""),
            enabled,
        )

    return await _run_mcp_server_command(
        session,
        data,
        command="mcp.toggle",
        invoke=invoke,
        reload_other_managers=True,
        catch=(Exception,),
        error_template="Failed to toggle MCP server: {exc}",
    )


async def handle_scheduler_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, remove_scheduled_task

    scheduler = _get_scheduler(session)
    try:
        scope = _scheduler_command_scope(session, data)
        result = remove_scheduled_task(scheduler, data, workspace_root=scope.workspace_root)
    except (SchedulerServiceError, ValueError) as exc:
        await emit_command_error(session, "scheduler.remove", exc)
        return True
    await session._send_ws_payload(
        _scheduler_snapshot_payload(result, scope),
        log_context="scheduler.list",
    )
    await session._send_event(
        AgentEvent.command_result(
            "scheduler.remove",
            "",
            data={"task_id": str(data.get("task_id", "")).strip()},
        )
    )
    return True


async def handle_scheduler_toggle(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, toggle_scheduled_task

    scheduler = _get_scheduler(session)
    try:
        scope = _scheduler_command_scope(session, data)
        result = toggle_scheduled_task(scheduler, data, workspace_root=scope.workspace_root)
    except (SchedulerServiceError, ValueError) as exc:
        await emit_command_error(session, "scheduler.toggle", exc)
        return True
    await session._send_ws_payload(
        _scheduler_snapshot_payload(result, scope),
        log_context="scheduler.list",
    )
    await session._send_event(
        AgentEvent.command_result(
            "scheduler.toggle",
            "",
            data={
                "task_id": str(data.get("task_id", "")).strip(),
                "enabled": bool(data.get("enabled")),
            },
        )
    )
    return True


async def handle_mcp_oauth_login(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    try:
        servers = await login_mcp_server(_session_mcp_manager(session), data.get("name", ""))
        refreshed = session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except (MCPServiceError, KeyError) as exc:
        await emit_command_error(session, "mcp.oauth.login", exc)
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    name = str(data.get("name", "")).strip()
    await session._send_event(
        AgentEvent.command_result(
            "mcp.oauth.login",
            "",
            data=_mcp_command_result_data(
                session,
                refreshed,
                name=name,
                status=_server_status_entry(servers, name),
            ),
        )
    )
    return True


async def handle_mcp_oauth_logout(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    try:
        servers = await logout_mcp_server(_session_mcp_manager(session), data.get("name", ""))
        refreshed = session.refresh_tool_registry_if_mcp_changed(allow_when_busy=False)
    except (MCPServiceError, KeyError) as exc:
        await emit_command_error(session, "mcp.oauth.logout", exc)
        return True
    await session._send_ws_payload(
        {"type": "mcp_status", "servers": servers},
        log_context="mcp_status",
    )
    name = str(data.get("name", "")).strip()
    await session._send_event(
        AgentEvent.command_result(
            "mcp.oauth.logout",
            "",
            data=_mcp_command_result_data(
                session,
                refreshed,
                name=name,
                status=_server_status_entry(servers, name),
            ),
        )
    )
    return True


def _provider_runtime(session: "WebSocketSession") -> Any:
    resolver = getattr(session, "_model_runtime_for_conversation", None)
    runtime = resolver(getattr(session, "active_conversation_id", None)) if callable(resolver) else None
    if runtime is None:
        raise ValueError("当前会话没有可用的 provider runtime")
    return runtime


async def handle_llm_provider_oauth_login(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    provider = str(data.get("provider") or data.get("provider_id") or "").strip()
    requested_conversation_id = str(data.get("conversation_id") or "").strip()
    conversation_id = str(session.active_conversation_id or "").strip()
    callbacks: _ProviderOAuthCallbacks | None = None
    try:
        if not provider:
            raise ValueError("provider is required")
        if not conversation_id:
            raise ValueError("provider login requires an active conversation")
        if requested_conversation_id and requested_conversation_id != conversation_id:
            raise ValueError("provider login conversation does not match the active conversation")
        runtime = _provider_runtime(session)
        callbacks = _ProviderOAuthCallbacks(session, conversation_id, provider)
        result = await runtime.login_provider(
            provider,
            callbacks,
        )
        session._refresh_llm_selection(prefer_config=False)
        await session._send_llm_state()
    except Exception as exc:
        await emit_command_error(session, "llm.provider.oauth.login", exc, data={"provider": provider})
        return True
    finally:
        if callbacks is not None:
            try:
                await callbacks.close()
            except Exception:
                logger.exception(
                    "Failed to close provider OAuth callbacks for %s/%s",
                    conversation_id,
                    provider,
                )
    await session._send_event(
        AgentEvent.command_result("llm.provider.oauth.login", "", data={"provider": provider, **result})
    )
    return True


async def handle_llm_provider_oauth_logout(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    provider = str(data.get("provider") or data.get("provider_id") or "").strip()
    requested_conversation_id = str(data.get("conversation_id") or "").strip()
    conversation_id = str(session.active_conversation_id or "").strip()
    try:
        if not provider:
            raise ValueError("provider is required")
        if not conversation_id:
            raise ValueError("provider logout requires an active conversation")
        if requested_conversation_id and requested_conversation_id != conversation_id:
            raise ValueError("provider logout conversation does not match the active conversation")
        runtime = _provider_runtime(session)
        removed = await runtime.logout_provider(provider)
        session._refresh_llm_selection(prefer_config=False)
        await session._send_llm_state()
    except Exception as exc:
        await emit_command_error(session, "llm.provider.oauth.logout", exc, data={"provider": provider})
        return True
    await session._send_event(
        AgentEvent.command_result("llm.provider.oauth.logout", "", data={"provider": provider, "removed": removed})
    )
    return True


async def handle_llm_provider_oauth_status(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    provider = str(data.get("provider") or data.get("provider_id") or "").strip()
    requested_conversation_id = str(data.get("conversation_id") or "").strip()
    conversation_id = str(session.active_conversation_id or "").strip()
    try:
        if not provider:
            raise ValueError("provider is required")
        if not conversation_id:
            raise ValueError("provider status requires an active conversation")
        if requested_conversation_id and requested_conversation_id != conversation_id:
            raise ValueError("provider status conversation does not match the active conversation")
        status = _provider_runtime(session).get_provider_auth_status(provider)
    except Exception as exc:
        await emit_command_error(session, "llm.provider.oauth.status", exc, data={"provider": provider})
        return True
    await session._send_event(
        AgentEvent.command_result("llm.provider.oauth.status", "", data={"provider": provider, **status})
    )
    return True


async def handle_scheduler_run_now(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, run_scheduled_task_now

    scheduler = _get_scheduler(session)
    try:
        scope = _scheduler_command_scope(session, data)
        result = run_scheduled_task_now(scheduler, data, workspace_root=scope.workspace_root)
    except (SchedulerServiceError, ValueError) as exc:
        await emit_command_error(session, "scheduler.run_now", exc)
        return True
    await session._send_ws_payload(
        _scheduler_snapshot_payload(result, scope),
        log_context="scheduler.list",
    )
    await session._send_event(
        AgentEvent.command_result(
            "scheduler.run_now",
            "",
            data={"task_id": str(data.get("task_id", "")).strip()},
        )
    )
    return True


async def handle_scheduler_retry(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, retry_scheduled_task_run

    scheduler = _get_scheduler(session)
    try:
        scope = _scheduler_command_scope(session, data)
        result = retry_scheduled_task_run(scheduler, data, workspace_root=scope.workspace_root)
    except (SchedulerServiceError, ValueError) as exc:
        await emit_command_error(session, "scheduler.retry", exc)
        return True
    await session._send_ws_payload(
        _scheduler_snapshot_payload(result, scope),
        log_context="scheduler.list",
    )
    await session._send_event(
        AgentEvent.command_result(
            "scheduler.retry",
            "",
            data={"run_id": str(data.get("run_id", "")).strip()},
        )
    )
    return True


async def handle_scheduler_cancel(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.scheduler_service import SchedulerServiceError, cancel_scheduled_task_run

    scheduler = _get_scheduler(session)
    try:
        scope = _scheduler_command_scope(session, data)
        result = cancel_scheduled_task_run(scheduler, data, workspace_root=scope.workspace_root)
    except (SchedulerServiceError, ValueError) as exc:
        await emit_command_error(session, "scheduler.cancel", exc)
        return True
    await session._send_ws_payload(
        _scheduler_snapshot_payload(result, scope),
        log_context="scheduler.list",
    )
    await session._send_event(
        AgentEvent.command_result(
            "scheduler.cancel",
            "",
            data={"run_id": str(data.get("run_id", "")).strip()},
        )
    )
    return True


# ---------------------------------------------------------------------------
# Handler dispatch table
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Any] = {
    "mcp.list": handle_mcp_list,
    "mcp.inventory.list": handle_mcp_inventory_list,
    "mcp.inventory.cancel": handle_mcp_inventory_cancel,
    "mcp.add": handle_mcp_add,
    "mcp.update": handle_mcp_update,
    "mcp.toggle": handle_mcp_toggle,
    "mcp.remove": handle_mcp_remove,
    "mcp.restart": handle_mcp_restart,
    "mcp.oauth.login": handle_mcp_oauth_login,
    "mcp.oauth.logout": handle_mcp_oauth_logout,
    "llm.provider.oauth.login": handle_llm_provider_oauth_login,
    "llm.provider.oauth.logout": handle_llm_provider_oauth_logout,
    "llm.provider.oauth.status": handle_llm_provider_oauth_status,
    "mcp.project.approve": handle_mcp_project_approve,
    "mcp.project.approve_all": handle_mcp_project_approve_all,
    "mcp.project.reject": handle_mcp_project_reject,
    "env.list": handle_env_list,
    "env.set": handle_env_set,
    "env.delete": handle_env_delete,
    "scheduler.list": handle_scheduler_list,
    "scheduler.add": handle_scheduler_add,
    "scheduler.remove": handle_scheduler_remove,
    "scheduler.toggle": handle_scheduler_toggle,
    "scheduler.run_now": handle_scheduler_run_now,
    "scheduler.retry": handle_scheduler_retry,
    "scheduler.cancel": handle_scheduler_cancel,
}
