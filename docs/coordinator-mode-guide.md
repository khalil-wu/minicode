# Coordinator 模式使用指南

## 概述

Coordinator 模式是一种**角色分离架构**，将复杂任务的编排和执行分离：

- **Coordinator（协调者）**: 只能使用 3-4 个工具，负责分解任务、协调 Worker、合成结果
- **Worker（执行者）**: 拥有完整工具集，负责执行具体的有界任务

灵感来自 Claude Code 的 Coordinator 模式。

---

## 核心理念

### Coordinator 的职责
1. **分解任务** - 将用户请求拆解为清晰、有界的工作项
2. **生成 Worker** - 为每个工作项生成独立的 Worker
3. **协调结果** - 收集 Worker 输出并合成最终答案

### Worker 的职责
1. **执行任务** - 完整准确地完成分配的任务
2. **独立工作** - 不需要进一步协调
3. **返回结构化结果** - 便于 Coordinator 合成

---

## 启用 Coordinator 模式

### 方法 1: 环境变量
```bash
export MINICODE_COORDINATOR_MODE=1
```

### 方法 2: Python 代码
```python
from backend.agent.coordinator import CoordinatorMode

# 启用
CoordinatorMode.enable()

# 禁用
CoordinatorMode.disable()

# 检查状态
if CoordinatorMode.is_enabled():
    print("Coordinator mode is active")
```

---

## 工具限制

### Coordinator 可用工具（白名单）
- `task` - 生成 Worker Agent
- `ask_user` - 询问用户
- `task_create` / `task_update` / `task_list` / `task_get` - 任务管理

### Worker 禁用工具（黑名单）
- `task` - 禁止递归委派（Worker 不能生成更多 Worker）

### Worker 可用工具
除 `task` 外的所有工具：
- 文件操作：`read_file`, `write_file`, `edit_file`
- 代码搜索：`grep_files`, `glob_files`
- 命令执行：`run_command`
- MCP 工具：所有 MCP 服务器工具
- 等等...

---

## 使用示例

### 示例 1: 多文件代码审查

**用户请求**:
```
用 Coordinator 模式审查所有 Python 文件的安全问题
```

**Coordinator 的思考过程**:
```python
# 1. 列出文件
files = ["auth.py", "api.py", "db.py"]

# 2. 为每个文件生成 Worker
workers = []
for file in files:
    worker = task(
        description=f"Review {file} for security issues",
        prompt=f"""Review {file} for:
        - SQL injection vulnerabilities
        - XSS risks
        - Authentication bypasses
        - Input validation issues
        
        Return structured findings with:
        - File and line number
        - Issue description
        - Severity (critical/high/medium/low)
        - Recommendation
        """,
        agent_type="general-purpose"
    )
    workers.append(worker)

# 3. 合成结果
all_issues = []
for worker_result in workers:
    # 解析 Worker 的结构化输出
    issues = parse_issues(worker_result)
    all_issues.extend(issues)

# 4. 按严重性排序并返回
all_issues.sort(key=lambda x: severity_order[x['severity']])

return f"""Security Review Complete

Found {len(all_issues)} issues across {len(files)} files:

Critical: {count_by_severity(all_issues, 'critical')}
High: {count_by_severity(all_issues, 'high')}
Medium: {count_by_severity(all_issues, 'medium')}

Detailed findings:
{format_issues(all_issues)}
"""
```

---

### 示例 2: 大型重构任务

**用户请求**:
```
用 Coordinator 模式重构整个认证模块，迁移到 JWT
```

**Coordinator 的分解**:
```python
# Phase 1: 分析现有实现
analysis_worker = task(
    description="Analyze current auth implementation",
    prompt="""Analyze the current authentication system:
    1. List all auth-related files
    2. Document current session management approach
    3. Identify all auth entry points
    4. Map dependencies
    
    Return structured analysis.
    """
)

# Phase 2: 设计新架构（基于 Phase 1 的结果）
design_worker = task(
    description="Design JWT-based auth architecture",
    prompt=f"""Based on this analysis:
    {analysis_worker}
    
    Design a JWT-based authentication system:
    1. Token structure and claims
    2. Refresh token strategy
    3. Migration path from old to new
    4. Backward compatibility approach
    
    Return detailed design doc.
    """
)

# Phase 3: 并行迁移各模块
migration_workers = []
modules = ["login", "logout", "middleware", "token_refresh"]
for module in modules:
    worker = task(
        description=f"Migrate {module} to JWT",
        prompt=f"""Implement JWT-based {module} according to design:
        {design_worker}
        
        1. Update code
        2. Add tests
        3. Document changes
        
        Return: changed files + test results
        """
    )
    migration_workers.append(worker)

# Phase 4: 合成最终报告
return f"""Authentication Migration Complete

Architecture: {design_worker['token_structure']}

Migrated modules:
{format_migration_results(migration_workers)}

Next steps:
1. Review PR at <link>
2. Run integration tests
3. Deploy to staging
"""
```

---

## 禁止的模式（Anti-Patterns）

### ❌ 桶店式委派（Bucket-shop delegation）
```python
# 错误示例
worker = task(
    description="Fix the code",
    prompt="Figure out what's wrong and fix it"
)
```

**问题**: 要求 Worker 既分析又执行，边界不清

**正确做法**:
```python
# 先分析
analysis = task(
    description="Analyze issues",
    prompt="List all bugs in auth.py with line numbers"
)

# 再执行
for bug in analysis['bugs']:
    fix = task(
        description=f"Fix bug at {bug['file']}:{bug['line']}",
        prompt=f"Fix this specific issue: {bug['description']}"
    )
```

---

### ❌ 递归委派（Recursive delegation）
```python
# 错误示例（Worker 内部）
sub_worker = task(  # Worker 不能调用 task！
    description="Sub-task",
    prompt="..."
)
```

**问题**: Worker 试图生成更多 Worker，违反角色规则

**正确做法**: Coordinator 负责所有分解，Worker 只执行

---

### ❌ 模糊需求（Vague requirements）
```python
# 错误示例
worker = task(
    description="Review code",
    prompt="Check the code for any issues"
)
```

**问题**: 没有明确的验收标准

**正确做法**:
```python
worker = task(
    description="Check SQL injection in auth.py:45-67",
    prompt="""Review auth.py lines 45-67 for SQL injection:
    - Check if user input is sanitized
    - Verify parameterized queries are used
    - Test with common injection payloads
    
    Return: True/False + explanation + code snippet if vulnerable
    """
)
```

---

## 集成到 Agent Loop

Coordinator 模式通过以下方式集成到 Agent Loop：

```python
# backend/agent/loop.py

from backend.agent.coordinator import apply_coordinator_mode_to_loop

async def run_agent_loop(...):
    # ... 构建 tool_schemas 和 system_prompt ...
    
    # 应用 Coordinator 模式过滤
    tool_schemas, system_prompt_parts, permission_context = \
        apply_coordinator_mode_to_loop(
            tool_schemas=tool_schemas,
            system_prompt_parts=system_prompt_parts,
            permission_context=permission_context,
        )
    
    # ... 继续正常流程 ...
```

### 自动角色检测

1. **顶级 Agent** + Coordinator 模式启用 → 自动成为 **Coordinator**
2. **Coordinator 生成的 Worker** → 自动成为 **Worker**
3. **普通模式** → 无角色限制

---

## 最佳实践

### 1. 清晰的任务边界
✅ **好**:
```python
task(
    description="Check auth.py:52 for SQL injection",
    prompt="Line 52 uses string concatenation. Check if vulnerable."
)
```

❌ **坏**:
```python
task(
    description="Fix security issues",
    prompt="Look for and fix any security problems"
)
```

---

### 2. 并行 vs 串行

**并行** (默认首选):
```python
# 独立任务 → 并行执行
workers = [
    task("Check file1"),
    task("Check file2"),
    task("Check file3"),
]
```

**串行** (有依赖时):
```python
# Phase 1: 分析
analysis = task("Analyze codebase")

# Phase 2: 基于分析结果设计（依赖 Phase 1）
design = task(f"Design based on: {analysis}")

# Phase 3: 基于设计实现（依赖 Phase 2）
impl = task(f"Implement: {design}")
```

---

### 3. 结构化输出

鼓励 Worker 返回结构化结果：

```python
task(
    prompt="""Find bugs and return JSON:
    {
        "bugs": [
            {"file": "...", "line": 52, "severity": "high", "desc": "..."},
            ...
        ]
    }
    """
)
```

---

### 4. 最终合成是 Coordinator 的责任

❌ **坏** (甩锅给 Worker):
```
"Worker will handle the final answer"
```

✅ **好** (Coordinator 合成):
```python
results = [worker1, worker2, worker3]
summary = synthesize(results)
return f"Complete analysis:\n{summary}"
```

---

## 性能考虑

### 并发限制
- Coordinator 可以并行生成多个 Worker
- 实际并发受系统资源限制（默认 ~10-16 并发）
- 过多 Worker 会排队执行

### Token 使用
- Coordinator 本身的 token 消耗少（工具少）
- Worker 总 token = Worker 数量 × 单个 Worker token
- 适合 token 预算充足的场景

---

## 调试技巧

### 查看角色
```python
from backend.agent.coordinator import CoordinatorMode

role = CoordinatorMode.get_role_from_context(permission_context)
print(f"Current role: {role}")  # 'coordinator' or 'worker'
```

### 查看可用工具
```python
if role == 'coordinator':
    allowed = CoordinatorMode.get_allowed_tools('coordinator')
    print(f"Coordinator tools: {allowed}")
```

### 禁用 Coordinator 模式（调试时）
```python
CoordinatorMode.disable()
# 现在所有 Agent 都有完整工具访问
```

---

## 总结

**何时使用 Coordinator 模式**:
- ✅ 大型复杂任务需要分解
- ✅ 多个独立子任务可并行
- ✅ 需要清晰的职责分离

**何时不用**:
- ❌ 简单单一任务
- ❌ 高度交互式任务（需要频繁用户输入）
- ❌ Token 预算有限

**关键原则**:
1. 清晰的任务边界
2. 禁止递归委派
3. Coordinator 负责最终合成
4. 结构化的 Worker 输出
