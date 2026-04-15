"""
WebSocket 消息路由与会话管理（DESIGN.md §10）。

职责：
  - 管理 WebSocket 连接的生命周期
  - 路由前端消息（user_message / approval / interrupt / load_skill）
  - 将 Agent Loop 的事件流序列化为 JSON 推送前端
  - 会话隔离：每个 WebSocket 连接对应一个独立的 Agent 会话
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from backend.agent.context import ContextBuilder
from backend.agent.loop import run_agent_loop
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig
from backend.llm.base import LLMAdapter
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class WebSocketSession:
    """一个 WebSocket 会话，包含完整的 Agent 运行时。"""

    def __init__(
        self,
        session_id: str,
        websocket: WebSocket,
        llm: LLMAdapter,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        config: AppConfig,
        skill_manager: Any | None = None,
        skill_executor: Any | None = None,
        rag_pipeline: Any | None = None,
        memory_manager: Any | None = None,
    ) -> None:
        self.session_id = session_id
        self.ws = websocket
        self.llm = llm
        self.tool_registry = tool_registry
        self.permission_checker = permission_checker
        self.config = config
        self.skill_manager = skill_manager
        self.skill_executor = skill_executor

        # 会话级资源
        self.artifact_store = ArtifactStore()
        self.context_builder = ContextBuilder(
            token_budget=config.token_budget,
            agent_settings=config.agent,
            skill_executor=skill_executor,
            rag_pipeline=rag_pipeline,
            memory_manager=memory_manager,
        )

        # 审批状态
        self._pending_approvals: dict[str, asyncio.Future] = {}
        self._interrupted = False

    async def handle(self) -> None:
        """主消息循环。"""
        try:
            while True:
                raw = await self.ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await self._send_event(
                        AgentEvent.error("无效的 JSON 消息", recoverable=True)
                    )
                    continue

                command = UserCommand.from_ws_message(msg)
                await self._handle_command(command)

        except WebSocketDisconnect:
            logger.info("会话 %s 断开连接", self.session_id)
        except Exception as exc:
            logger.error("会话 %s 异常: %s", self.session_id, exc, exc_info=True)
        finally:
            self.artifact_store.clear()

    async def _handle_command(self, command: UserCommand) -> None:
        """分发前端命令。"""
        if command.type == "user_message":
            content = command.data.get("content", "")
            if content:
                await self._run_agent(content)

        elif command.type == "approval":
            tool_call_id = command.data.get("tool_call_id", "")
            if tool_call_id in self._pending_approvals:
                future = self._pending_approvals.pop(tool_call_id)
                future.set_result(command.data)

        elif command.type == "interrupt":
            self._interrupted = True

        elif command.type == "load_skill":
            skill_name = command.data.get("skill_name", "")
            if self.skill_manager and skill_name:
                success = self.skill_manager.activate(skill_name)
                if success:
                    await self._send_event(
                        AgentEvent(
                            type="skill_activated",
                            data={"skill_name": skill_name, "description": f"已激活 Skill: {skill_name}"},
                        )
                    )
                else:
                    await self._send_event(
                        AgentEvent.error(f"Skill '{skill_name}' 加载失败", recoverable=True)
                    )
            else:
                await self._send_event(
                    AgentEvent.error("Skills 系统未初始化或缺少 skill_name", recoverable=True)
                )

    async def _run_agent(self, user_message: str) -> None:
        """运行 Agent Loop 并流式推送事件。"""
        self._interrupted = False

        async for event in run_agent_loop(
            user_message=user_message,
            llm=self.llm,
            tool_registry=self.tool_registry,
            artifact_store=self.artifact_store,
            permission_checker=self.permission_checker,
            agent_settings=self.config.agent,
            token_budget=self.config.token_budget,
            context_builder=self.context_builder,
            approval_handler=self._approval_handler,
            skill_manager=self.skill_manager,
        ):
            if self._interrupted:
                await self._send_event(
                    AgentEvent.error(
                        "用户中断了当前生成",
                        recoverable=True,
                        error_type="budget",
                    )
                )
                break

            await self._send_event(event)

    async def _approval_handler(self, tool_call_id: str) -> dict[str, Any]:
        """等待前端审批结果。"""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_approvals[tool_call_id] = future

        try:
            result = await asyncio.wait_for(future, timeout=300)  # 5 分钟超时
            return result
        except asyncio.TimeoutError:
            return {"action": "reject", "guidance": "审批超时（5分钟）"}

    async def _send_event(self, event: AgentEvent) -> None:
        """发送事件到前端。"""
        try:
            await self.ws.send_json(event.to_ws_message())
        except Exception as exc:
            logger.error("发送事件失败: %s", exc)


class WebSocketManager:
    """管理所有活跃的 WebSocket 会话。"""

    def __init__(self) -> None:
        self._sessions: dict[str, WebSocketSession] = {}

    async def connect(
        self,
        websocket: WebSocket,
        llm: LLMAdapter,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        config: AppConfig,
        skill_manager: Any | None = None,
        skill_executor: Any | None = None,
        rag_pipeline: Any | None = None,
        memory_manager: Any | None = None,
    ) -> WebSocketSession:
        """接受 WebSocket 连接，创建会话。"""
        await websocket.accept()

        session_id = f"session_{uuid.uuid4().hex[:8]}"
        session = WebSocketSession(
            session_id=session_id,
            websocket=websocket,
            llm=llm,
            tool_registry=tool_registry,
            permission_checker=permission_checker,
            config=config,
            skill_manager=skill_manager,
            skill_executor=skill_executor,
            rag_pipeline=rag_pipeline,
            memory_manager=memory_manager,
        )
        self._sessions[session_id] = session
        logger.info("新会话: %s", session_id)
        return session

    def disconnect(self, session_id: str) -> None:
        """断开会话。"""
        self._sessions.pop(session_id, None)

    @property
    def active_count(self) -> int:
        return len(self._sessions)
