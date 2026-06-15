"""
Coordinator 模式 - 角色分离架构

Coordinator: 只能使用 Agent、SendMessage、Shutdown
Worker: 拥有完整工具集

灵感来自 Claude Code 的 Coordinator 模式。
"""

from __future__ import annotations

import os
from typing import Any
from backend.permissions.context import PermissionContext


class CoordinatorMode:
    """Coordinator 模式管理器"""

    # Coordinator 可用的工具（极少数）
    COORDINATOR_TOOLS = frozenset({
        'task',  # 或 'agent' - 生成 Worker
        'send_message',  # Agent 间通信
        'ask_user',  # 询问用户
        'task_create',  # 任务管理
        'task_update',
        'task_list',
        'task_get',
    })

    # Worker 禁止使用的工具（避免递归委派）
    WORKER_FORBIDDEN_TOOLS = frozenset({
        'task',  # Worker 不能再生成 Worker
    })

    @classmethod
    def is_enabled(cls) -> bool:
        """检查 Coordinator 模式是否启用"""
        return os.getenv('MINICODE_COORDINATOR_MODE', '').lower() in ('1', 'true', 'yes')

    @classmethod
    def enable(cls):
        """启用 Coordinator 模式"""
        os.environ['MINICODE_COORDINATOR_MODE'] = '1'

    @classmethod
    def disable(cls):
        """禁用 Coordinator 模式"""
        os.environ.pop('MINICODE_COORDINATOR_MODE', None)

    @classmethod
    def get_allowed_tools(cls, role: str) -> frozenset[str] | None:
        """获取角色允许使用的工具

        Args:
            role: 'coordinator' 或 'worker'

        Returns:
            - coordinator: 返回白名单
            - worker: 返回 None（表示所有工具减去禁用列表）
        """
        if role == 'coordinator':
            return cls.COORDINATOR_TOOLS
        elif role == 'worker':
            return None  # 所有工具，但会被 WORKER_FORBIDDEN_TOOLS 过滤
        else:
            return None

    @classmethod
    def get_forbidden_tools(cls, role: str) -> frozenset[str]:
        """获取角色禁止使用的工具

        Args:
            role: 'coordinator' 或 'worker'

        Returns:
            黑名单工具集
        """
        if role == 'worker':
            return cls.WORKER_FORBIDDEN_TOOLS
        return frozenset()

    @classmethod
    def build_coordinator_system_prompt(cls) -> str:
        """构建 Coordinator 系统提示"""
        return """You are a Coordinator agent. Your job is to orchestrate complex tasks by:

1. **Breaking down** the user's request into clear, bounded work items
2. **Spawning Workers** to execute each work item independently
3. **Coordinating results** and synthesizing the final answer

# CRITICAL RULES

## Task Decomposition
- Each Worker task must have:
  - Clear, unambiguous requirements
  - Explicit acceptance criteria
  - All necessary context to work independently
- Never delegate unclear requirements — clarify with the user first
- Tasks should be parallel when possible, sequential only when dependencies exist

## Worker Management
- Workers have access to the full toolset (except spawning more Workers)
- Workers cannot see each other's work unless you explicitly share it
- You are responsible for coordinating between Workers if needed

## Prohibited Patterns
- ❌ **Bucket-shop delegation**: "Figure out what to do, then do it"
- ❌ **Recursive delegation**: Workers cannot spawn more Workers
- ❌ **Vague requirements**: "Fix any issues you find"
- ✅ **Good delegation**: "Check auth.py:45-67 for SQL injection, return True/False + explanation"

## Your Responsibilities
- You must synthesize Worker outputs into the final answer
- You cannot claim a Worker "will handle it" — you must present the complete result
- If a Worker fails, you must retry with clearer instructions or a different approach

# Available Tools
- `task`: Spawn a Worker agent with a specific task
- `send_message`: Send a message to a Worker (not implemented yet)
- `ask_user`: Ask the user for clarification
- `task_*`: Manage the shared task list

# Example Flow

User: "Review all Python files for security issues"

Coordinator:
1. List Python files → ['auth.py', 'api.py', 'db.py']
2. Spawn 3 Workers in parallel:
   - Worker 1: "Review auth.py for SQL injection, XSS, auth bypass"
   - Worker 2: "Review api.py for input validation, rate limiting, CORS"
   - Worker 3: "Review db.py for connection security, query parameterization"
3. Collect results from all Workers
4. Synthesize: "Found 5 issues: [detailed list with file:line references]"

# Remember
- You are the orchestrator, not a delegator
- Clear task boundaries prevent wasted work
- Final synthesis is your responsibility
"""

    @classmethod
    def build_worker_system_prompt(cls) -> str:
        """构建 Worker 系统提示"""
        return """You are a Worker agent under a Coordinator. Your job is to:

1. **Execute the assigned task** completely and accurately
2. **Return structured results** that the Coordinator can synthesize
3. **Work independently** without needing further coordination

# CRITICAL RULES

## Task Execution
- Complete only the task assigned to you
- Do not speculate about the broader context
- If you encounter blockers, report them clearly rather than guessing

## Output Format
- Be concise but complete
- Include file:line references for all findings
- Use structured format when possible (lists, tables)
- Distinguish between facts, analysis, and recommendations

## Limitations
- You cannot spawn more Workers (no recursive delegation)
- You cannot see other Workers' outputs
- You have access to all tools except `task`

## Example

Coordinator: "Review auth.py lines 45-67 for SQL injection vulnerabilities"

Worker:
✅ Good response:
"Reviewed auth.py:45-67. Found 1 SQL injection risk:
- Line 52: User input `username` concatenated directly into query
  ```python
  query = f"SELECT * FROM users WHERE name='{username}'"
  ```
  Recommendation: Use parameterized query with placeholders"

❌ Bad response:
"I found some issues. The Coordinator should look into them."
"""

    @classmethod
    def filter_tool_schemas(
        cls,
        schemas: list[dict],
        role: str,
    ) -> list[dict]:
        """过滤工具 schema

        Args:
            schemas: 原始工具 schema 列表
            role: 'coordinator' 或 'worker'

        Returns:
            过滤后的 schema 列表
        """
        if role == 'coordinator':
            # Coordinator: 只保留白名单工具
            allowed = cls.get_allowed_tools('coordinator')
            return [
                schema for schema in schemas
                if schema.get('name') in allowed
            ]
        elif role == 'worker':
            # Worker: 移除黑名单工具
            forbidden = cls.get_forbidden_tools('worker')
            return [
                schema for schema in schemas
                if schema.get('name') not in forbidden
            ]
        else:
            return schemas

    @classmethod
    def build_permission_context(
        cls,
        role: str,
        parent_context: PermissionContext | None = None,
    ) -> PermissionContext:
        """构建角色专用的权限上下文

        Args:
            role: 'coordinator' 或 'worker'
            parent_context: 父级权限上下文

        Returns:
            新的权限上下文
        """
        if parent_context is None:
            parent_context = PermissionContext()

        # 构建工具拒绝规则
        deny_rules = list(parent_context.tool_deny_rules)

        if role == 'worker':
            # Worker 禁止 task 工具
            if 'task' not in deny_rules:
                deny_rules.append('task')

        return PermissionContext(
            mode=parent_context.mode,
            session_overrides=dict(parent_context.session_overrides),
            tool_deny_rules=deny_rules,
            filesystem_constraints=dict(parent_context.filesystem_constraints),
            source=f"coordinator:{role}",
        )

    @classmethod
    def get_role_from_context(cls, context: Any) -> str | None:
        """从上下文中提取角色

        Args:
            context: PermissionContext 或类似对象

        Returns:
            'coordinator', 'worker', 或 None
        """
        if not hasattr(context, 'source'):
            return None

        source = getattr(context, 'source', '')
        if isinstance(source, str) and source.startswith('coordinator:'):
            return source.split(':', 1)[1]

        return None


# ──────────────────────────────────────────────────────────────────
# 集成到 Agent Loop
# ──────────────────────────────────────────────────────────────────

def apply_coordinator_mode_to_loop(
    *,
    tool_schemas: list[dict],
    system_prompt_parts: list[str],
    permission_context: PermissionContext,
) -> tuple[list[dict], list[str], PermissionContext]:
    """将 Coordinator 模式应用到 Agent Loop

    Args:
        tool_schemas: 工具 schema 列表
        system_prompt_parts: 系统提示部分
        permission_context: 权限上下文

    Returns:
        (过滤后的 schemas, 更新后的 prompts, 更新后的 context)
    """
    if not CoordinatorMode.is_enabled():
        return tool_schemas, system_prompt_parts, permission_context

    # 从上下文中提取角色
    role = CoordinatorMode.get_role_from_context(permission_context)

    # 如果是顶级 Agent 且没有指定角色，默认为 Coordinator
    if role is None and permission_context.source == "main":
        role = 'coordinator'
        permission_context = CoordinatorMode.build_permission_context(
            'coordinator',
            permission_context
        )

    if role is None:
        return tool_schemas, system_prompt_parts, permission_context

    # 过滤工具
    filtered_schemas = CoordinatorMode.filter_tool_schemas(tool_schemas, role)

    # 添加角色专用系统提示
    role_prompt = (
        CoordinatorMode.build_coordinator_system_prompt()
        if role == 'coordinator'
        else CoordinatorMode.build_worker_system_prompt()
    )

    updated_prompts = system_prompt_parts + [role_prompt]

    return filtered_schemas, updated_prompts, permission_context
