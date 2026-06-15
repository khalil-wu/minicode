"""
Workflow 脚本引擎 - Python 实现

受 Claude Code Workflow 启发，提供 agent/parallel/pipeline/phase/log API。
"""

from __future__ import annotations

import asyncio
import time
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class WorkflowContext:
    """Workflow 执行上下文"""
    llm: Any
    tool_registry: Any
    artifact_store: Any
    permission_checker: Any
    agent_settings: Any
    token_budget: Any
    emit_event: Optional[Callable] = None
    args: dict[str, Any] = None
    budget_tracker: Optional[Any] = None

    # 运行时状态
    current_phase: str = ""
    agent_count: int = 0
    max_agents: int = 1000

    def __post_init__(self):
        if self.args is None:
            self.args = {}


class WorkflowEngine:
    """Workflow 脚本引擎

    提供 Claude Code 风格的编排 API：
    - agent(prompt, opts) - 生成子 Agent
    - parallel(thunks) - 并行执行（barrier）
    - pipeline(items, *stages) - 流水线（无 barrier）
    - phase(title) - 进度分组
    - log(message) - 进度消息
    - workflow(name, args) - 嵌套 Workflow
    """

    def __init__(self, context: WorkflowContext):
        self.ctx = context
        self._phase_stack: list[str] = []
        self._agent_results: dict[str, Any] = {}

    async def run_script(self, script: str, meta: dict[str, Any]) -> Any:
        """运行 Workflow 脚本

        Args:
            script: Python 脚本代码
            meta: 元数据 (name, description, phases)

        Returns:
            脚本的返回值
        """
        # 构建安全的命名空间
        namespace = {
            # API
            'agent': self._agent_wrapper,
            'parallel': self._parallel_wrapper,
            'pipeline': self._pipeline_wrapper,
            'phase': self._phase_wrapper,
            'log': self._log_wrapper,
            'workflow': self._workflow_wrapper,
            'args': self.ctx.args,

            # 内置模块（受限）
            'asyncio': asyncio,
            'json': __import__('json'),
            'time': __import__('time'),

            # 禁用危险操作
            '__builtins__': self._safe_builtins(),
        }

        try:
            # 执行脚本
            exec(script, namespace)

            # 如果脚本定义了主函数，调用它
            if 'main' in namespace and callable(namespace['main']):
                result = namespace['main']()
                if asyncio.iscoroutine(result):
                    return await result
                return result

            # 否则返回最后一个表达式的值（如果有）
            return namespace.get('result')

        except Exception as exc:
            logger.error(f"Workflow script execution failed: {exc}", exc_info=True)
            raise

    # ─────────────────────────────────────────────────────────────
    # API 实现
    # ─────────────────────────────────────────────────────────────

    async def _agent_wrapper(
        self,
        prompt: str,
        schema: Optional[dict] = None,
        label: Optional[str] = None,
        phase: Optional[str] = None,
        model: Optional[str] = None,
        isolation: Optional[str] = None,
        agentType: Optional[str] = None,
    ) -> Any:
        """生成一个子 Agent 并返回结果

        Args:
            prompt: Agent 的任务提示
            schema: JSON Schema（如果提供，强制结构化输出）
            label: 显示标签
            phase: 显式指定 phase（用于 parallel/pipeline 内部）
            model: 模型覆盖（sonnet/opus/haiku）
            isolation: 隔离模式（worktree）
            agentType: Agent 类型（explore/plan/implement）

        Returns:
            - 有 schema: 返回验证后的结构化对象
            - 无 schema: 返回 Agent 的文本输出
        """
        # 检查 Agent 数量限制
        if self.ctx.agent_count >= self.ctx.max_agents:
            raise RuntimeError(
                f"Workflow agent limit reached: {self.ctx.max_agents}"
            )

        self.ctx.agent_count += 1
        agent_id = f"wf-agent-{self.ctx.agent_count}"

        # 确定当前 phase
        current_phase = phase or self.ctx.current_phase or "default"

        # 发送 subagent_start 事件
        if self.ctx.emit_event:
            await self.ctx.emit_event(
                "subagent.start",
                {
                    "subagent_id": agent_id,
                    "role": agentType or "general-purpose",
                    "prompt": label or prompt[:100],
                    "phase": current_phase,
                }
            )

        # 调用 TaskTool 执行
        from backend.tools.agent_tools import TaskTool

        task_tool = TaskTool(
            llm_provider=lambda: self.ctx.llm,
            tool_registry_provider=lambda: self.ctx.tool_registry,
            artifact_store=self.ctx.artifact_store,
            permission_checker_provider=lambda: self.ctx.permission_checker,
            agent_settings_provider=lambda: self.ctx.agent_settings,
            token_budget_provider=lambda: self.ctx.token_budget,
        )

        # 构建执行上下文
        from backend.permissions.context import ToolExecutionContext
        tool_ctx = ToolExecutionContext(
            session_id="workflow",
            conversation_id="workflow",
            task_id=agent_id,
            emit_event=self.ctx.emit_event,
            metadata={"phase": current_phase},
        )

        # 执行 Agent
        result = await task_tool._run_single_subtask(
            description=label or prompt[:60],
            prompt=prompt,
            agent_type=agentType or "general-purpose",
            context=tool_ctx,
            timeout_seconds=300.0,
        )

        # 发送 subagent_done 事件
        if self.ctx.emit_event:
            await self.ctx.emit_event(
                "subagent.done",
                {
                    "subagent_id": agent_id,
                    "summary": result.content[:500] if result.content else "",
                    "duration_ms": result.duration_ms,
                    "phase": current_phase,
                }
            )

        # 如果有 schema，解析结构化输出
        if schema:
            import json
            try:
                # 从结果中提取 JSON
                content = result.content
                # 简单的 JSON 提取（假设 Agent 返回 JSON）
                if '{' in content and '}' in content:
                    start = content.index('{')
                    end = content.rindex('}') + 1
                    json_str = content[start:end]
                    parsed = json.loads(json_str)

                    # 简单的 schema 验证（只检查必需字段）
                    if 'required' in schema:
                        for field in schema['required']:
                            if field not in parsed:
                                raise ValueError(f"Missing required field: {field}")

                    return parsed
                else:
                    raise ValueError("No JSON found in agent output")
            except Exception as e:
                logger.warning(f"Schema validation failed: {e}")
                return None

        # 否则返回纯文本
        return result.content if result.content else None

    async def _parallel_wrapper(self, thunks: list[Callable]) -> list[Any]:
        """并行执行多个任务（barrier）

        Args:
            thunks: 任务列表，每个是 lambda: agent(...)

        Returns:
            结果列表，失败的任务为 None
        """
        # 并发执行所有任务
        results = await asyncio.gather(
            *[thunk() for thunk in thunks],
            return_exceptions=True
        )

        # 将异常转换为 None
        return [
            None if isinstance(r, Exception) else r
            for r in results
        ]

    async def _pipeline_wrapper(self, items: list[Any], *stages: Callable) -> list[Any]:
        """流水线执行（无 barrier）

        每个 item 独立流过所有 stage，无需等待其他 item。

        Args:
            items: 要处理的项目列表
            stages: 处理阶段函数列表

        Returns:
            最终结果列表
        """
        async def process_item(item, index):
            """处理单个 item 通过所有 stage"""
            current = item
            for stage_idx, stage in enumerate(stages):
                try:
                    # Stage 函数签名: stage(prev_result, original_item, index)
                    result = stage(current, item, index)
                    if asyncio.iscoroutine(result):
                        current = await result
                    else:
                        current = result
                except Exception as e:
                    logger.error(f"Pipeline stage {stage_idx} failed for item {index}: {e}")
                    return None
            return current

        # 并发处理所有 item（每个 item 独立流过所有 stage）
        tasks = [process_item(item, i) for i, item in enumerate(items)]
        return await asyncio.gather(*tasks, return_exceptions=False)

    def _phase_wrapper(self, title: str):
        """开始新的 phase

        Args:
            title: Phase 标题
        """
        self.ctx.current_phase = title
        self._phase_stack.append(title)

        # 发送 phase 事件
        if self.ctx.emit_event:
            asyncio.create_task(
                self.ctx.emit_event(
                    "workflow.phase",
                    {"title": title}
                )
            )

        logger.info(f"Workflow phase: {title}")

    def _log_wrapper(self, message: str):
        """输出进度消息

        Args:
            message: 消息内容
        """
        # 发送 log 事件
        if self.ctx.emit_event:
            asyncio.create_task(
                self.ctx.emit_event(
                    "workflow.log",
                    {"message": message, "phase": self.ctx.current_phase}
                )
            )

        logger.info(f"Workflow: {message}")

    async def _workflow_wrapper(self, name: str, args: Optional[dict] = None) -> Any:
        """调用嵌套 Workflow

        Args:
            name: Workflow 名称
            args: 传递给 Workflow 的参数

        Returns:
            Workflow 的返回值
        """
        # TODO: 实现 Workflow 注册表和加载
        raise NotImplementedError("Nested workflows not yet implemented")

    # ─────────────────────────────────────────────────────────────
    # 安全沙箱
    # ─────────────────────────────────────────────────────────────

    def _safe_builtins(self) -> dict:
        """返回安全的内置函数集合"""
        safe = {
            # 基础类型
            'int': int,
            'float': float,
            'str': str,
            'bool': bool,
            'list': list,
            'dict': dict,
            'tuple': tuple,
            'set': set,

            # 基础函数
            'len': len,
            'range': range,
            'enumerate': enumerate,
            'zip': zip,
            'map': map,
            'filter': filter,
            'sorted': sorted,
            'sum': sum,
            'min': min,
            'max': max,
            'abs': abs,
            'round': round,

            # 类型检查
            'isinstance': isinstance,
            'type': type,

            # 其他
            'print': logger.info,  # 重定向到日志
        }
        return safe
