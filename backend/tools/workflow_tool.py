"""Workflow Tool - 多 Agent 编排工具"""

from __future__ import annotations

from typing import Any
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.permissions.context import ToolExecutionContext


class WorkflowTool(BaseTool):
    """执行 Workflow 脚本进行复杂的多 Agent 编排"""

    name = "workflow"
    description = (
        "Execute a workflow script for complex multi-agent orchestration. "
        "Use parallel() for concurrent execution, pipeline() for streaming processing, "
        "and phase() for progress grouping. Supports agent spawning with structured output."
    )
    permission = PermissionLevel.REVIEW  # Workflow 需要审批

    def __init__(
        self,
        *,
        llm_provider: Any | None = None,
        tool_registry_provider: Any | None = None,
        artifact_store: Any = None,
        permission_checker_provider: Any | None = None,
        agent_settings_provider: Any | None = None,
        token_budget_provider: Any | None = None,
    ):
        self._llm_provider = llm_provider
        self._tool_registry_provider = tool_registry_provider
        self._artifact_store = artifact_store
        self._permission_checker_provider = permission_checker_provider
        self._agent_settings_provider = agent_settings_provider
        self._token_budget_provider = token_budget_provider

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "script": {
                        "type": "string",
                        "description": (
                            "Python workflow script. Must start with: "
                            "export const meta = {name: '...', description: '...', phases: [...]}. "
                            "Available API: agent(prompt, schema=, label=), parallel([lambda: ...]), "
                            "pipeline(items, stage1, stage2), phase(title), log(message)."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "Name of a saved workflow to run (alternative to script)",
                    },
                    "args": {
                        "type": "object",
                        "description": "Arguments passed to the workflow as the 'args' global",
                    },
                },
                "anyOf": [
                    {"required": ["script"]},
                    {"required": ["name"]},
                ],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """执行 Workflow 脚本"""
        script = args.get("script")
        workflow_name = args.get("name")
        workflow_args = args.get("args", {})

        if not script and not workflow_name:
            return self._error_result("Either 'script' or 'name' is required")

        # 如果提供了名称，从注册表加载
        if workflow_name:
            script = await self._load_workflow(workflow_name)
            if not script:
                return self._error_result(f"Workflow '{workflow_name}' not found")

        # 解析 meta 信息
        meta = self._parse_meta(script)
        if not meta:
            return self._error_result(
                "Workflow script must start with: "
                "export const meta = {name: '...', description: '...', phases: [...]}"
            )

        # 构建执行上下文
        from backend.workflow.engine import WorkflowEngine, WorkflowContext

        wf_ctx = WorkflowContext(
            llm=self._resolve_llm(),
            tool_registry=self._resolve_tool_registry(),
            artifact_store=self._artifact_store,
            permission_checker=self._resolve_permission_checker(),
            agent_settings=self._resolve_agent_settings(),
            token_budget=self._resolve_token_budget(),
            emit_event=context.emit_event if context else None,
            args=workflow_args,
        )

        engine = WorkflowEngine(wf_ctx)

        # 执行 Workflow
        import time
        start_time = time.perf_counter()

        try:
            result = await engine.run_script(script, meta)
            duration_ms = int((time.perf_counter() - start_time) * 1000)

            # 格式化结果
            import json
            result_text = self._format_result(result, meta, wf_ctx.agent_count, duration_ms)

            return ToolResult(
                content=result_text,
                duration_ms=duration_ms,
                display_summary=f"Workflow: {meta['name']} ({wf_ctx.agent_count} agents)",
                result_kind="workflow",
            )

        except Exception as exc:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            return ToolResult(
                content=f"Workflow execution failed: {type(exc).__name__}: {exc}",
                is_error=True,
                duration_ms=duration_ms,
                display_summary=f"Workflow failed: {meta.get('name', 'unknown')}",
                result_kind="workflow",
            )

    # ──────────────────────────────────────────────────────────────
    # 辅助方法
    # ──────────────────────────────────────────────────────────────

    def _parse_meta(self, script: str) -> dict[str, Any] | None:
        """从脚本中提取 meta 信息"""
        import re
        import json

        # 匹配 export const meta = {...}
        pattern = r"export\s+const\s+meta\s*=\s*(\{[^}]+\})"
        match = re.search(pattern, script, re.MULTILINE | re.DOTALL)

        if not match:
            return None

        try:
            # JavaScript 对象转 Python dict（简单转换）
            meta_str = match.group(1)
            meta_str = meta_str.replace("'", '"')  # 单引号转双引号
            meta_str = re.sub(r'(\w+):', r'"\1":', meta_str)  # 键加引号
            return json.loads(meta_str)
        except Exception:
            return None

    def _format_result(
        self,
        result: Any,
        meta: dict,
        agent_count: int,
        duration_ms: int,
    ) -> str:
        """格式化 Workflow 结果"""
        import json

        lines = [
            f"Workflow: {meta['name']}",
            f"Description: {meta.get('description', 'N/A')}",
            f"Duration: {duration_ms / 1000:.1f}s",
            f"Agents spawned: {agent_count}",
            "",
            "Result:",
        ]

        # 格式化结果
        if isinstance(result, dict):
            lines.append(json.dumps(result, indent=2, ensure_ascii=False))
        elif isinstance(result, list):
            lines.append(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            lines.append(str(result))

        return "\n".join(lines)

    async def _load_workflow(self, name: str) -> str | None:
        """从注册表加载 Workflow 脚本"""
        # TODO: 实现 Workflow 注册表
        # 当前从 .minicode/workflows/ 加载
        from pathlib import Path
        workflows_dir = Path.home() / ".minicode" / "workflows"
        script_path = workflows_dir / f"{name}.py"

        if script_path.exists():
            return script_path.read_text()

        return None

    def _resolve_llm(self):
        if callable(self._llm_provider):
            return self._llm_provider()
        return self._llm_provider

    def _resolve_tool_registry(self):
        if callable(self._tool_registry_provider):
            return self._tool_registry_provider()
        return self._tool_registry_provider

    def _resolve_permission_checker(self):
        if callable(self._permission_checker_provider):
            return self._permission_checker_provider()
        return self._permission_checker_provider

    def _resolve_agent_settings(self):
        from backend.config import AgentSettings

        if callable(self._agent_settings_provider):
            settings = self._agent_settings_provider()
            if isinstance(settings, AgentSettings):
                return settings
        if isinstance(self._agent_settings_provider, AgentSettings):
            return self._agent_settings_provider
        return AgentSettings(max_iterations=8, agent_mode="react")

    def _resolve_token_budget(self):
        from backend.config import TokenBudget

        if callable(self._token_budget_provider):
            budget = self._token_budget_provider()
            if isinstance(budget, TokenBudget):
                return budget
        if isinstance(self._token_budget_provider, TokenBudget):
            return self._token_budget_provider
        return TokenBudget(total=100_000)
