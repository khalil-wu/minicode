# MiniCode Bug 分析报告 & 与 Claude Code 桌面端差距评估

> 基于对 MiniCode src.v2 代码库的全面审计，以及对 Claude Code 官方桌面端的深度对标分析。
> 修订版：反映已修复的 bug 和当前代码状态。

---

## 0. 现状总览

MiniCode 已实现的核心功能比初版分析更丰富：

| 功能                  | 状态 | 备注                                |
| --------------------- | ---- | ----------------------------------- |
| 会话 CRUD + 归档      | ✅   | 创建、重命名、删除、归档/取消归档   |
| Git Worktree 隔离     | ✅   | 后端 WorktreeManager + 前端隔离会话 |
| Side Chat (旁路对话)  | ✅   | SideChatPanel，继承上下文           |
| Permission 5 模式     | ✅   | Ask/Auto/Accept/Plan/Bypass         |
| View Mode 切换        | ✅   | normal/verbose/summary，Ctrl+O      |
| Preview + Auto-verify | ✅   | 有 verify 但非全自动化              |
| Desktop 通知          | ✅   | Electron Notification               |
| Diff Review Modal     | ⚠️  | 有但缺行级评论                      |
| Deep Link             | ⚠️  | handler 存在但未注册协议            |
| Context Usage         | ⚠️  | 基础 used/limit，无环形图           |
| MCP Connectors        | ⚠️  | 后端有，前端无 UI 设置流            |

**实际差距约 55-60%**。核心差距在安全、审查流、远程/SSH、插件系统、企业功能。

---

## 1. 已修复的 Bug ✅

以下 bug 在本轮审计中已确认修复：

### 前端

| ID | 原始问题 | 修复方式 |
|----|----------|----------|
| F-1 | `AskUserPrompt` useEffect 缺少依赖数组，stale closure 导致发送空答案 | `respond` 改为 `useCallback`，useEffect 依赖 `[answer, pendingAskUser, respond]`，增加 `e.isComposing` 守卫 |
| F-2 | `appendTextChunk` 用 `findIndex` 找第一个 streaming 消息而非最后一个 | 改为 `findLastStreamingIndex()` 辅助函数 |
| F-3 | error 事件全局清除所有 approval/ask-user 状态，导致 agent 永久挂起 | 改为按 `requestId` 精确清除，只清除与当前错误关联的 approval |
| F-4 | `approveAll` 循环中逐个 `clearApproval` 导致队列错位 | 改为批量 `clearApprovals(all.map(item => item.requestId))` |
| F-6 | `submitPartialApproval` 的 setTimeout 竞态清除新 diff review | setTimeout 回调中增加 `requestId` 匹配检查 |

### 后端

| ID | 原始问题 | 修复方式 |
|----|----------|----------|
| B-3 | `SandboxValidator` 和 `PermissionRuleMatcher` 被注释掉，路径遍历保护无效 | 已取消注释，`__init__` 中正确初始化 `self._sandbox` 和 `self._rule_matcher` |
| B-3b | `validate_file_operation` / `validate_command` 被注释掉 | 已恢复实现，增加路径规范化（`expanduser` + 相对路径解析） |

---

## 2. 仍存在的 Bug（待修复）

### 前端 — 高优先级

| ID | 文件 | 问题 | 影响 |
|----|------|------|------|
| F-5 | `useWebSocket.ts:113-117` | WS 断连期间排队的 approval/rejection 命令在重连后被重放，可能批准错误的 tool call | 重连后自动批准不相关的工具调用 |
| F-7 | `useWebSocket.ts:1160-1183` | `diff.git_working_tree` / `diff.git_staged` handler 中 `const s` 变量遮蔽外层，且 `setGitChangesLoading(false)` 未被调用 | Git changes panel 永久显示 loading spinner |
| F-8 | `stores/index.ts:680` | `sendMessage` 中 user/assistant 消息 ID 基于 `Date.now().toString(36)`，同毫秒内双发会产生重复 ID | React key 冲突，消息可能丢失 |

### 前端 — 中优先级

| ID | 文件 | 问题 | 影响 |
|----|------|------|------|
| F-9 | `useWebSocket.ts:140-146` | Stream buffer 模块级单例无 destroy 调用，HMR/StrictMode 下可能泄漏 RAF | 开发模式下文本追加到错误消息 |
| F-10 | 全局 | Escape 键始终触发 `interrupt()`，即使 modal/输入框正在操作 | 用户关闭 modal 时意外中断 AI |
| F-11 | 全局 | 删除当前活动会话后状态为空白，无引导 | 用户看到空白面板 |
| F-12 | 全局 | WS 重连后不清除残留的 `isStreaming` 和 stream buffer 状态 | UI 卡死在"生成中" |

### 后端 — 高优先级

| ID | 文件 | 问题 | 影响 |
|----|------|------|------|
| B-1 | `ws/command_handlers.py:97` | `terminal.exec` 命令通过 `create_subprocess_shell` 执行，无权限检查 | 绕过 `run_command` tool 的审批流程 |
| B-2 | `agent/loop.py:693-709` | `_flush_auto_tool_batch` 并发执行时，`asyncio.gather` 中任一工具抛异常会导致整个批次失败且无 error handling | 并发工具批次中一个失败导致全部结果丢失 |
| B-4 | `agent/loop.py:399` | Self-heal 时 `_tool_call_hashes.clear()` 重置计数器，最坏情况 agent 重复 9 次相同调用 | 浪费 token 和时间 |
| B-5 | `tools/command_tool.py:257` | `proc.stderr.read()` 无 timeout 保护，大量 stderr 时可能长时间阻塞 | 命令执行卡住 |

### 后端 — 中优先级

| ID | 文件 | 问题 | 影响 |
|----|------|------|------|
| B-6 | `ws/agent_runner.py:92-105` | `_persistent_notes` 无边界检查，compaction_summary 变化时无限增长 | 长 session 消耗 context window |
| B-7 | `ws/handler.py:298-305` | `_on_file_changed` 每次文件变更都调用 `running_preview_processes()`，且只取 `[0]` | 多 preview server 只通知第一个 |
| B-8 | `ws/handler.py` | `_pending_approvals` dict 无锁，approval_handler 和 resolve 可能同时操作 | 极端情况下 approval 丢失 |
| B-9 | 全局 | `ConversationRepository` 无并发控制，两个 WS 会话同时写同一对话会互相覆盖 | 对话数据损坏 |

### Desktop (Electron) — 安全

| ID | 文件 | 问题 | 影响 |
|----|------|------|------|
| D-1 | `desktop/main.js` | FS IPC handler 无路径校验，renderer 可读写系统任意文件 | 安全漏洞 |
| D-2 | `desktop/main.js` | `openDevTools()` 生产环境无条件开启 | 信息泄露 |
| D-3 | `desktop/main.js` | `sandbox: false` | preload 有完整 Node.js 访问权 |
| D-4 | `desktop/main.js` | PTY session 窗口关闭时未 kill | 孤儿进程泄漏 |
| D-5 | `desktop/main.js` | `openExternal` 只校验非空字符串，未限制协议 | `file:///` / `javascript:` 可执行 |

---

## 3. 与 Claude Code 桌面端差距评估

### 已较好实现 ✅

| 功能 | 评价 |
|------|------|
| Agent Loop (四级进化循环) | 完整：tool call + compaction + stagnation + self-heal |
| 权限系统 (5 模式) | 完整：AUTO/CONFIRM/DIFF_REVIEW/ALWAYS_DENY + bypass/plan/accept_edits |
| 工具系统 | 核心工具齐全：文件读写/命令执行/搜索/git |
| MCP Server 集成 | 有 circuit breaker 和健康检查 |
| Skills 系统 | 有 marketplace 和自动加载 |
| RAG Pipeline + Vector Memory | 有实现 |
| WebSocket 实时流式 | 完整事件协议和断线重连 |
| Conversation 持久化 | 有 |
| 后台命令执行 | 有 |
| File Watcher | 有 |
| Checkpoint/Undo | 有 |
| Orchestrator (复杂任务分解) | 有 |
| Git Worktree 隔离 | 有 |
| Side Chat | 有 |
| Permission Modes | 5 种模式 |

### 关键差距 ❌

| Claude Code 功能 | MiniCode 状态 | 差距程度 |
|-----------------|--------------|---------|
| Prompt Caching (5min TTL, cache_control) | ❌ 未实现 | 大 — 直接影响成本和延迟 |
| Sub-agents (并行独立 agent) | ❌ 有 orchestrator 但非真正并行隔离 | 大 |
| IDE 集成 (VS Code/JetBrains) | ❌ 只有 Electron 独立 app | 大 |
| Diff 行级评论 + Review Code | ❌ 有 diff panel 但无行级评论 | 中 |
| Remote/SSH Sessions | ❌ 无 | 大 |
| Computer Use (屏幕控制) | ❌ 无 | 大 |
| Plugins 系统 | ❌ 有 skills 但无插件市场 | 中 |
| Scheduled Tasks | ❌ 无 | 中 |
| Multi-turn Approval (session allow) | ❌ 每次单独 approve | 中 |
| Conversation Branching (fork) | ❌ 无 | 中 |
| OAuth / 认证 MCP | ❌ 无 | 中 |
| Context Window 环形图 | ⚠️ 有数据无可视化 | 小 |
| Extended Thinking (budget_tokens) | ⚠️ 有 thinking_chunk 流式 | 小 |
| Hooks 系统 (user-configurable) | ⚠️ 有 hook_manager | 小 |
| CLAUDE.md 层级 | ⚠️ 有但层级不完整 | 小 |
| Cost Tracking UI | ⚠️ 有 CostTracker 无前端展示 | 小 |
| Notification System | ✅ Electron Notification | — |
| Model Switching | ✅ /model 命令 | — |
| Streaming Terminal Output | ✅ stream_callback | — |

---

## 4. 修复计划

### Phase 0：安全止血（P0，1-2 周）

#### 0.1 Desktop Electron 安全加固

| # | 问题 | 修复 |
|---|------|------|
| S-D1 | FS IPC 无路径校验 | 所有 `minicode:fs:*` handler 增加 workspace 白名单校验 |
| S-D2 | `openDevTools()` 生产环境开启 | 加 `if (!app.isPackaged)` 守卫 |
| S-D3 | `sandbox: false` | 设为 `true`，确认 preload 用 `contextBridge` |
| S-D4 | `openExternal` 无协议限制 | 校验必须以 `http://` 或 `https://` 开头 |
| S-D5 | PTY 窗口关闭不清理 | `before-quit` 中遍历 `ptySessions` 逐个 `.kill()` |

#### 0.2 Backend 安全加固

| # | 问题 | 修复 |
|---|------|------|
| S-B1 | `terminal.exec` 无权限校验 | 增加 `PermissionChecker.validate_command()` 调用 |
| S-B2 | API Key 写入 `os.environ` | 改为进程内内存存储，仅在 LLM 调用时使用 |
| S-B3 | 会话 ID 无校验 | 增加正则校验 `[A-Za-z0-9_-]{4,64}` |
| S-B4 | 终端 CWD 未校验 | 限制在工作空间目录内 |
| S-B5 | MCP config 无内容校验 | 增加 JSON schema 验证 |

#### 0.3 前端关键竞态修复

| # | 问题 | 修复 |
|---|------|------|
| S-F1 | WS 重连重放过期命令 | 队列中的命令增加 timestamp，重连时丢弃超过 30s 的命令 |
| S-F2 | Escape 始终 interrupt | 检查 `document.activeElement` 是否在 modal 内 |
| S-F3 | 删除当前会话后无引导 | 自动创建新会话或显示欢迎页 |
| S-F4 | 重连不清除 streaming 状态 | `session.restore` 响应处理前重置 `isStreaming` |
| S-F5 | Git changes loading 永不清除 | `diff.git_working_tree` handler 中调用 `setGitChangesLoading(false)` |

---

### Phase 1：核心体验对齐（P1，3-5 周）

#### 1.1 Diff 行级评论 + Review Code

1. DiffReviewModal 增加行号点击 → 弹出评论输入框
2. 评论收集到 store 的 `diffComments: Map<string, LineComment[]>`
3. "Submit Review" → 发送 `diff.review.submit` WS 命令
4. 后端将评论注入 agent 上下文
5. "Review Code" 按钮 → 调用 agent 对当前 diff 做代码审查

#### 1.2 Context Usage 环形图

1. 后端在 `done` 事件中携带 `context_usage: { used, limit }` 字段
2. 前端 `UsageRing` 组件接收实际数据
3. Hover 显示详细 token 用量

#### 1.3 并发工具批次 Error Handling

1. `_flush_auto_tool_batch` 中 `asyncio.gather` 加 `return_exceptions=True`
2. 对异常结果生成 `ToolResult(is_error=True)`
3. 保证批次中单个失败不影响其他工具

#### 1.4 编辑器保存完整性

1. 后端新增 `workspace.compareAndWrite` — 接收 `{path, expectedHash, content}`
2. 校验 hash 后写入，防止 TOCTOU

---

### Phase 2：功能对齐（P2，5-8 周）

- Connectors UI（MCP 连接器图形设置）
- Session Persistence 增强
- 环境变量管理（加密本地存储）
- PR 监控（CI 状态栏）
- Scheduled Tasks（定时任务）

### Phase 3：高级功能（P3，8-12 周）

- Remote Sessions
- SSH Sessions
- Computer Use
- Plugins 系统
- Diff View 增强（文件列表 + 双列分割）

### Phase 4：企业级 & 打磨（P4，12-16 周）

- SSO / 认证
- MDM / Group Policy
- 性能优化
- 长尾打磨

---

## 5. Quick Wins（可立即执行）

以下修复不依赖 Phase 顺序，改动量小：

1. `openDevTools()` 条件化 — 1 行
2. PTY 窗口关闭清理 — 15 行
3. Escape 键 interrupt 守卫 — 5 行
4. 删除会话后自动创建新会话 — 10 行
5. `openExternal` 协议校验 — 5 行
6. 会话 ID 正则校验 — 3 行
7. `terminal.exec` 权限检查 — 10 行
8. Git changes loading 状态清除 — 2 行
9. `_flush_auto_tool_batch` 加 `return_exceptions=True` — 5 行
10. `_persistent_notes` 加上限（最多 10 条） — 3 行

---

## 6. 关键路径依赖图

```
Phase 0 (安全) ──→ Phase 1 (核心体验)
                       │
                       ├─→ Phase 2 (功能对齐)
                       │
                       └─→ Phase 3 (高级功能)
                              │
                              └─→ Phase 4 (企业级)
```

---

## 7. 里程碑

| 里程碑 | 内容 | 预计时间 |
|--------|------|----------|
| M0 | Phase 0 安全止血完成 | 第 1-2 周 |
| M1 | Diff 行级评论 + Context Ring + Error Handling | 第 3-5 周 |
| M2 | 会话增强 + Preview Auto-verify | 第 5-7 周 |
| M3 | Connectors UI + 环境变量管理 | 第 7-10 周 |
| M4 | Scheduled Tasks + Diff View 增强 | 第 10-13 周 |
| M5 | Remote/SSH Sessions | 第 13-16 周 |
| M6 | Computer Use + Plugins | 第 16-20 周 |
| M7 | SSO + MDM + 性能优化 | 第 20-24 周 |
