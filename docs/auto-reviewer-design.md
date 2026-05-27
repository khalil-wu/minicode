# Auto-Reviewer Agent 设计草案

## 概述

参考 Codex `approvals_reviewer=auto_review` 模式，为 MiniCode 设计一个 reviewer agent，
在 PermissionChecker 判定需要用户确认（CONFIRM / DIFF_REVIEW）时，先由 reviewer 评估是否可以自动放行。

核心约束：**reviewer 不能突破 sandbox 边界**。它只能在 sandbox 已允许的范围内减少用户打断。

## 定位

```
Tool Call
  → PermissionChecker (规则引擎，确定 permission level)
  → SandboxPolicy (声明式边界)
  → AutoReviewer (可选，评估是否自动放行)
  → User Approval UI (仅当 reviewer 不放行时)
```

- AUTO 级别：reviewer 不介入（已自动放行）
- ALWAYS_DENY 级别：reviewer 不介入（硬拒绝，不可覆盖）
- CONFIRM / DIFF_REVIEW 级别：reviewer 可以 approve → 跳过用户确认

## 输入

```python
@dataclass
class ReviewRequest:
    tool_name: str
    tool_input: dict[str, Any]
    permission_level: PermissionLevel      # CONFIRM or DIFF_REVIEW
    sandbox_policy: SandboxPolicy          # 当前 sandbox 声明
    diff: str | None                       # unified diff (file writes only)
    recent_tool_history: list[ToolCallRecord]  # 最近 N 次工具调用
    workspace_root: Path
    risk_signals: RiskSignals              # 预计算的风险信号
```

### RiskSignals（预计算，不依赖 LLM）

```python
@dataclass
class RiskSignals:
    targets_sensitive_path: bool     # .env, .git/, settings.json, credentials
    targets_system_path: bool        # /etc, /usr, C:\Windows
    has_network_access: bool         # command 含 curl/wget/npm publish 等
    diff_size: int                   # 变更行数
    diff_deletes_more_than_adds: bool
    command_has_side_effects: bool   # rm, mv, kill, drop, truncate
    outside_writable_roots: bool    # 目标路径不在 sandbox writable_roots 内
    installs_packages: bool          # npm install, pip install, apt install
    modifies_ci_config: bool         # .github/, .gitlab-ci.yml, Jenkinsfile
```

## 输出

```python
@dataclass
class ReviewDecision:
    action: Literal["approve", "deny", "ask_user"]
    reason: str                      # 人类可读的决策理由
    risk_level: Literal["low", "medium", "high"]
    confidence: float                # 0.0 - 1.0
```

语义：
- `approve`：自动放行，不打断用户
- `deny`：直接拒绝，返回错误给 agent（仅当 sandbox 边界被违反时）
- `ask_user`：保持原有行为，展示审批 UI

## 决策规则（Phase 1：纯规则，无 LLM）

Phase 1 不调用 LLM，只用确定性规则。这保证了：
- 零延迟（不等 API 调用）
- 可审计（规则可枚举）
- 不增加 token 成本

### 硬拒绝（→ deny）

| 条件 | 理由 |
|------|------|
| `outside_writable_roots == True` | sandbox 边界违反 |
| `targets_system_path == True` | 系统路径不可写 |
| `permission_level == ALWAYS_DENY` | 不可覆盖 |

### 自动放行（→ approve）

| 条件 | 理由 |
|------|------|
| CONFIRM + 命令在 user allowlist 中 | 用户已预授权 |
| DIFF_REVIEW + diff_size ≤ 20 行 + 目标在 workspace 内 + 无敏感路径 | 低风险小改动 |
| CONFIRM + read-only 命令（git status, ls, cat） | 实际无副作用 |
| 重复操作（最近 3 次相同 tool+类似 args 已被用户 approve） | 模式学习 |

### 需要用户确认（→ ask_user）

所有不满足上述条件的情况，保持原有审批流程。

## Phase 2 扩展（未来，需 LLM）

当规则无法覆盖时，可选调用轻量 LLM（Haiku 级别）：

```
System: You are a security reviewer for a code agent.
Given the tool call below, decide: approve / deny / ask_user.
You CANNOT override sandbox boundaries.
You CANNOT approve writes to sensitive files.
Respond with JSON: {"action": "...", "reason": "...", "risk_level": "..."}

Tool: {tool_name}
Args: {tool_input_json}
Sandbox: writable={writable_roots}, network={allow_network}
Diff: {diff_preview}
Recent history: {last_3_tools}
```

Phase 2 约束：
- reviewer LLM 调用有 5s 超时，超时 → ask_user
- reviewer 不能看到用户消息内容（隐私隔离）
- reviewer 的 approve 仍受 sandbox 硬边界约束
- reviewer 调用计入 cost tracker

## 与现有模块的集成

### PermissionChecker

```python
class PermissionChecker:
    def check(self, tool_name, args, context) -> PermissionDecision:
        level = self._determine_level(tool_name, args)

        if level in (PermissionLevel.AUTO, PermissionLevel.ALWAYS_DENY):
            return PermissionDecision(level=level)

        # 新增：auto-reviewer 评估
        if self._reviewer:
            decision = self._reviewer.evaluate(ReviewRequest(...))
            if decision.action == "approve":
                return PermissionDecision(level=PermissionLevel.AUTO, auto_reviewed=True)
            if decision.action == "deny":
                return PermissionDecision(level=PermissionLevel.ALWAYS_DENY, reason=decision.reason)

        return PermissionDecision(level=level)
```

### ApprovalManager

不改动。reviewer approve 后，ApprovalManager 不会收到该请求。

### SandboxPolicy

reviewer 读取 policy 但不能修改。`outside_writable_roots` 检查在 reviewer 内部完成，
作为硬拒绝条件。即使 reviewer 代码有 bug 返回 approve，SandboxRunner 执行时仍会拦截越界写入。

### Hooks

reviewer 决策可以被 `pre_tool_use` hook 覆盖（hook exit 1 → block）。
Hook 优先级高于 reviewer，因为 hook 是用户显式配置的确定性规则。

## 配置

```json
{
  "auto_reviewer": {
    "enabled": false,
    "mode": "rules_only",
    "auto_approve_repeated": true,
    "max_auto_approve_diff_lines": 20,
    "sensitive_paths": [".env", ".git/", "settings.json", ".claude/"]
  }
}
```

默认关闭。用户显式开启后，从 rules_only 模式开始。

## 审计日志

每次 reviewer 决策记录到 `data/reviewer_audit.jsonl`：

```json
{
  "ts": "2026-05-27T10:30:00Z",
  "tool_name": "write_file",
  "action": "approve",
  "reason": "small diff (8 lines) within workspace, no sensitive paths",
  "risk_level": "low",
  "confidence": 1.0,
  "mode": "rules_only"
}
```

## 不做的事

- 不替代 sandbox（reviewer 是建议层，sandbox 是强制层）
- 不看用户消息内容（隐私）
- 不自动 approve ALWAYS_DENY 级别
- 不在 Phase 1 调用 LLM
- 不持久化 approve 历史跨 session（避免权限膨胀）
