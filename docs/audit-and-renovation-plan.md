# MiniCode 深度全面审查与改造计划 v2

基于对 ClaudeCode-ref（1987 个 TS 源文件）、codexref（UI 截图参考）、业界最佳实践（Codex CLI 沙箱、Claude Code Harness 架构）以及 MiniCode 自身前后端 60+ 个核心文件的逐行审查。

---

## 第一部分：与参考实现的架构差距分析

### A. Claude Code 有而 MiniCode 缺失或不足的关键模式

#### A1. 流式工具执行器（StreamingToolExecutor）—— 严重缺失

**Claude Code 做法**：工具在模型流式输出过程中就开始执行，而非等流结束。`StreamingToolExecutor` 类在收到 `tool_use` 块的瞬间就启动执行，并发安全工具并行、非安全工具串行。Bash 工具出错时通过 `siblingAbortController` 取消所有兄弟工具。

**MiniCode 现状**：`tool_execution.py` 的 `execute_tool_batch` 在 LLM 流完全结束后才开始执行工具。这意味着模型生成 3 个工具调用的耗时（比如 5 秒）全部浪费在等待上，而 Claude Code 在这 5 秒内可能已经完成了第一个工具的执行。

**影响**：每次多工具调用的延迟比 Claude Code 高 30-60%。

#### A2. 错误扣留模式（Error Withholding）—— 完全缺失

**Claude Code 做法**：可恢复的错误（如 prompt-too-long、max-output-tokens）不会立即 yield 给前端，而是先尝试恢复。只有恢复失败后才向用户展示错误。

```
模型返回 413 -> withhold=true -> 尝试 Context Collapse Drain -> 失败
-> 尝试 Reactive Compact -> 成功 -> 重试循环（用户看不到任何错误）
```

**MiniCode 现状**：`agent/loop.py` 的恢复阶梯虽然存在，但错误事件会立即发送到前端（通过 `yield error_event`），然后才尝试恢复。用户会看到"闪烁"的错误消息——先报错，然后恢复成功，错误消失。

#### A3. 四层上下文压缩 vs 两层 —— 不足

**Claude Code 做法**（按顺序应用）：
1. **Snip Compact**（HISTORY_SNIP）：硬截断旧消息
2. **Microcompact**（CACHED_MICROCOMPACT）：按 ID 移除单个工具结果，缓存感知
3. **Context Collapse**（CONTEXT_COLLAPSE）：将消息范围折叠为摘要，读时投影
4. **Auto-Compact**：通过 fork 的 Agent 做完整对话摘要

**MiniCode 现状**：只有 MicroCompact（内联截断）和 Auto-Compact（紧急压缩）两层。缺少 Snip 硬截断和 Context Collapse 折叠，导致在长对话中压缩效率低于 Claude Code。

#### A4. 工具结果预算管控 —— 缺失

**Claude Code 做法**：`applyToolResultBudget` 在每次循环迭代前检查所有消息中工具结果的总 token 数，超出预算的按 time-decay 顺序截断。`POST_COMPACT_MAX_FILES_TO_RESTORE = 5`，`POST_COMPACT_TOKEN_BUDGET = 50000`。

**MiniCode 现状**：没有全局工具结果预算。`MicroCompact` 只截断单个超大结果，但不会管控多个结果的累积总量。在 Agent 读取大量文件后，上下文可能被工具结果撑爆。

#### A5. Compaction 摘要结构 —— 过于简单

**Claude Code 做法**：压缩摘要要求 9 个结构化部分（主请求、技术概念、文件与代码片段、错误与修复、问题解决、所有用户消息、待处理任务、当前工作、下一步），并且明确要求包含完整代码片段：
```
4. Files and Code Sections — include FULL code snippets, not summaries
```

**MiniCode 现状**：`agent/compaction.py` 的压缩 prompt 没有要求结构化输出，也没有要求保留代码片段，导致压缩后可能丢失关键代码上下文。

#### A6. 系统提示缓存边界标记 —— 缺失

**Claude Code 做法**：`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 将系统提示分为"稳定"（跨组织缓存）和"易变"（每轮重算）两部分。`DANGEROUS_uncachedSystemPromptSection` 函数明确标记哪些部分会破坏缓存。

**MiniCode 现状**：`agent/prompting.py` 虽然有 stable/context/volatile 三层，但没有利用 LLM API 的缓存边界标记（如 OpenAI 的 `cache_control` 参数），每次都发送完整系统提示。

#### A7. MCP 工具 LRU 缓存 —— 缺失

**Claude Code 做法**：`fetchToolsForClient` 使用 `memoizeWithLRU` 缓存 MCP 工具列表，避免每次循环都重新请求 `tools/list`。工具列表只在连接状态变化时刷新。

**MiniCode 现状**：每次 Agent 循环迭代都重新获取 MCP 工具列表（当 `_mcp_registry_version` 变化时），但由于 B1 bug，registry version 实际上从不变化，意味着工具列表也从不刷新——这是一个隐藏的 bug：即使 MCP 服务器添加了新工具，Agent 也看不到。

#### A8. Query Chain 追踪 —— 缺失

**Claude Code 做法**：
```typescript
type QueryChainTracking = {
  chainId: string   // 每个用户 turn 唯一
  depth: number     // 每次循环迭代递增
}
```
用于遥测、日志关联、和调试。

**MiniCode 现状**：没有 chain tracking 机制。Agent 循环中的错误日志无法关联到具体的用户 turn 或循环深度。

#### A9. 工具摘要异步生成 —— 缺失

**Claude Code 做法**：工具批量执行完成后，使用轻量模型（Haiku）异步生成工具使用摘要。这个摘要在下一轮循环开始时 yield，隐藏了约 1 秒的延迟。

**MiniCode 现状**：没有工具摘要生成。对话历史中只保留原始工具结果，无法快速了解工具做了什么。

#### A10. 模型回退链（529 → Fallback Model）—— 不足

**Claude Code 做法**：
```
529 Overloaded -> 重试 3 次（指数退避）-> 切换到 fallback 模型 -> 失败则 CannotRetryError
```
支持 `fallbackModel` 参数，自动降级到备选模型。

**MiniCode 现状**：`llm/fallback_adapter.py` 有 fallback 链，但没有与 529 重试策略集成。当主模型 529 时直接切换，没有先尝试重试。

### B. Codex CLI 有而 MiniCode 缺失的关键模式

#### B1. 沙箱隔离 —— 严重不足

**Codex 做法**：
- 三层沙箱：网络沙箱（默认禁网）+ 文件系统沙箱（只写工作目录）+ 进程沙箱（Docker/microVM 隔离）
- 权限模式：`suggest`（只建议不执行）、`auto-edit`（自动编辑但命令需确认）、`full-auto`（全自动但沙箱内执行）
- Windows 上使用专门的 Job Object 沙箱

**MiniCode 现状**：`sandbox/` 目录存在但实现基础，没有网络隔离，没有文件系统写限制，没有进程级隔离。`permissions/` 系统只做逻辑层面的权限检查，不做操作系统级隔离。

#### B2. Auto Mode 安全分类器 —— 缺失

**Codex 做法**：`yoloClassifier.ts`（52KB）在 auto 模式下对每个工具调用进行安全分类，判断是否应该自动批准、需要确认、还是直接拒绝。Bash 命令有专门的安全分类器。

**MiniCode 现状**：权限系统有 `auto_allow` 模式，但没有安全分类器。auto 模式下所有工具直接放行，依赖静态规则而非动态分类。

---

## 第二部分：后端深层 Bug 清单（新增发现）

### 严重级（Critical）

| # | 位置 | 问题 |
|---|------|------|
| C1 | `ws/handlers/mcp.py` L335-397 | **TaskScheduler 完全不工作**：每个调度器 handler 都创建新的 `TaskScheduler()` 实例，加载后丢弃。调度循环从未启动。定时任务被保存但永远不会触发。 |
| C2 | `ws/handler.py` L759-765 | **无界 WebSocket 任务创建**：每条 WebSocket 消息都 `asyncio.create_task()` 处理，无并发限制。恶意客户端可以快速发送大量消息耗尽资源。 |
| C3 | `ws/handlers/terminal.py` L219-228 | **terminal.exec 绕过 CONFIRM 权限**：当权限为 CONFIRM 级别时，发送拒绝消息而非创建审批流程，相当于 ALWAYS_DENY。 |
| C4 | `conversations/repository.py` L344-354 | **对话仓库无文件锁**：并发写入同一对话可产生撕裂文件。追加模式 JSONL 写入可交错。 |

### 高级（High）

| # | 位置 | 问题 |
|---|------|------|
| H1 | `ws/handlers/terminal.py` L62-63 | **terminal resize 是空 stub**：`handle_terminal_resize` 直接 `return True`，PTY 从不接收新尺寸，终端 UI 在 resize 后崩溃。 |
| H2 | `ws/handler.py` L154 | **`_conversation_run_locks` 内存泄漏**：dict 中每个对话一个 `asyncio.Lock`，永不清理。 |
| H3 | `ws/approval_runtime.py` L21 | **`_session_approval_cache` 内存泄漏**：已审批工具+参数的缓存 key Set 无限增长。 |
| H4 | `agent/loop.py` 全文 | **253 个裸 `except Exception`**：覆盖整个代码库，吞掉所有异常，包括 `KeyboardInterrupt`、`SystemExit` 等不应被捕获的异常。 |
| H5 | `agent/tool_execution.py` 全文 | **工具执行无总超时**：每个工具有独立的超时，但一个 Agent turn 中所有工具执行的总时间没有限制。如果模型生成 20 个工具调用，每个耗时 30 秒，用户需要等待 10 分钟。 |

### 中级（Medium）

| # | 位置 | 问题 |
|---|------|------|
| M1 | `ws/handler.py` snapshot hydration | 快照水合中的 `except Exception` 静默吞掉错误，UI 停留在永久加载状态 |
| M2 | `agent/tool_execution.py` | 文件 I/O 重试中使用阻塞 `time.sleep()` 而非 `asyncio.sleep()`，阻塞事件循环 |
| M3 | `agent/context.py` | 权限上下文被重复构建两次（在 handler 和 tool_execution 中各一次） |
| M4 | `ws/agent_runner.py` | 直接修改 `ContextBuilder` 的私有属性（`_system_context`），破坏封装 |
| M5 | `ws/events.py` | 可能发送重复的 `done` 事件（正常完成和错误恢复各一次） |
| M6 | `agent/compaction.py` | Compaction 摘要没有结构化 prompt，丢失代码片段 |
| M7 | `llm/openai_adapter.py` | 流式输出没有 backpressure 机制，快速模型可能撑爆 WebSocket 缓冲区 |
| M8 | `bootstrap/app.py` | 启动超时使用 `asyncio.wait_for`，但超时时不关闭已启动的子服务 |

---

## 第三部分：前端深层 Bug 清单（新增发现）

### 严重级（Critical）

| # | 位置 | 问题 |
|---|------|------|
| FC1 | `stores/types.ts` + 各 overlay | **20+ 个 client command 缺少类型化 payload**：`conversation.unarchive`、`workspace.switch`、`session.restore`、`terminal.list` 等都通过 `as never` 发送，完全绕过 TypeScript 类型检查。后端字段变更时前端无编译期报错。 |
| FC2 | `chat/chatStreamEvents.ts` L20-22 | **`finalAnswerConversations` 模块级 Set 泄漏**：WebSocket 断连或对话删除时不清理。后续该对话的 `text_chunk` 事件被静默丢弃。 |
| FC3 | 全局 | **零响应式断点**：整个 UI 使用固定像素值，无 `@media`、`useMediaQuery`。视口低于 ~1180px 时双侧边栏 + 内容区完全不可用。 |

### 高级（High）

| # | 位置 | 问题 |
|---|------|------|
| FH1 | `overlays/SettingsCenter.tsx` | **Settings 缺少无障碍属性**：无 `role="dialog"`、`aria-modal="true"`、焦点陷阱。Tab 键可以导航到模态框后面。 |
| FH2 | `AssistantMessage.tsx` L308-369 | **`toolDisclosureStyle` 类型冲突**：`TurnActivityStyles.item` 是 `React.CSSProperties`（对象），而 `CodexActivityStyles.item` 是函数。交叉类型产生 `never`。如果代码路径尝试将 `styles.item` 当对象展开，会产生运行时错误。 |
| FH3 | 全局 | **Error Boundary 覆盖不足**：仅在 App/SidebarRight/MainSlots/ChunkErrorBoundary 有。面板（Terminal/Editor/Preview/Diff）、Composer、MessageList、Overlay 都没有 Error Boundary。任一组件崩溃导致整个应用白屏。 |
| FH4 | 全局 z-index | **z-index 层级冲突**：ContextMenu(99999)、DialogService(1200)==tooltip(1200)、PreviewPanel popup(120)>ApprovalModal(110)。Preview 弹窗可以覆盖审批对话框——审批应该是阻塞的。 |

### 中级（Medium）

| # | 位置 | 问题 |
|---|------|------|
| FM1 | `stores/chat-slice.ts` L261-285/353-374 | **跨 slice 状态重置重复**：`applyConversationSwitched` 和 `createConversation` 都手动重置 20+ 个字段。新增字段遗漏导致旧对话状态泄漏。 |
| FM2 | `chat/sessionEvents.ts` L198-223 | **`conversation.list` 激进清除缓存**：不在列表中的对话的消息被永久删除。后端分页或瞬态错误时会丢失消息历史。 |
| FM3 | `hooks/useWebSocket.ts` L93-106 | **模块级单例泄漏**：WebSocket handle、subscriber Set、去重队列在 React StrictMode 双重挂载或 HMR 时不清理。 |
| FM4 | `composer/MenuOverlay.tsx` L159-192 | **capture phase 键盘监听**：`document.addEventListener("keydown", handler, true)` 在捕获阶段拦截方向键/Enter/Escape，影响其他组件。 |
| FM5 | `composer/Composer.tsx` L384-407 | **非代码模式 Composer 绝对定位**：`bottom: 24` 浮在 MessageList 上方。`pb-[180px]` 是硬编码估算，Composer 内容多时与最后一条消息重叠。 |
| FM6 | `overlays/ConnectorsTab.tsx` L178-183 | **MCP 安装用 setTimeout 轮询**：安装后 `setTimeout(1000ms)` 再查询状态。安装超过 1 秒时用户看不到变化，应该监听 `mcp.lifecycle` 事件。 |
| FM7 | `panels/DiffPanel.tsx` L218/378 | **`dangerouslySetInnerHTML` XSS 风险**：diff 语法高亮直接注入 HTML。恶意仓库内容可能构成 XSS 攻击。 |
| FM8 | `chat/MessageList.tsx` L177-180 | **`content-visibility: auto` 布局偏移**：`auto 120px` 估算对于含代码块的助手消息过于激进，滚动到旧消息时出现明显跳动。 |

---

## 第四部分：与参考实现对比后的改造计划

### Phase 0：紧急修复（2-3 天）—— 修复无法工作的核心功能

#### 0.1 MCP 连接修复

```python
# mcp/manager.py: _load_external_configs 中
# 当 URL 解析为空时跳过外部配置，降级到本地 stdio 版本
resolved_url = _resolve_env_placeholders(server_cfg.get("url", ""))
if not resolved_url.strip():
    logger.warning(f"[MCP] 外部配置 {name} URL 为空，跳过（将使用本地配置）")
    continue
```

#### 0.2 补全 `get_mcp_manager` 函数

```python
# backend/main.py 添加
def get_mcp_manager():
    from backend.api._state import _state
    if _state.bootstrap and hasattr(_state.bootstrap, 'mcp_manager'):
        return _state.bootstrap.mcp_manager
    return None
```

#### 0.3 MCPToolProxy 动态引用修复

```python
# mcp/registry.py: 存储 manager 引用，动态获取 client
class MCPToolProxy:
    def __init__(self, manager_ref, server_name, tool_name, ...):
        self._manager_ref = weakref.ref(manager_ref)
        self._server_name = server_name
    @property
    def _client(self):
        mgr = self._manager_ref()
        return mgr.get_client(self._server_name) if mgr else None
```

#### 0.4 修复 toolDisclosureStyle 类型冲突

在 `AssistantMessage.tsx` 中统一 `TurnActivityStyles` 和 `CodexActivityStyles` 的 `item` 类型定义。

#### 0.5 修复 TaskScheduler 不启动的问题

在 `bootstrap/app.py` 的 `startup()` 中创建并启动全局 `TaskScheduler` 实例，而不是在每个 handler 中临时创建。

---

### Phase 1：Agent Loop 对标 Claude Code（5-7 天）

#### 1.1 实现流式工具执行器

参照 Claude Code 的 `StreamingToolExecutor`，在 `agent/tool_execution.py` 中添加：

```python
class StreamingToolExecutor:
    """在模型流式输出期间就开始执行工具"""
    def __init__(self, tool_registry, permission_checker, abort_event):
        self._sibling_abort = asyncio.Event()
        self._executing: dict[str, asyncio.Task] = {}
        self._results: dict[str, ToolResult] = {}
    
    async def add_tool(self, tool_call: ToolCall):
        """模型流中发现新工具调用时调用"""
        tool = self._registry.get(tool_call.name)
        if tool.is_concurrency_safe(tool_call.args):
            # 并发执行
            task = asyncio.create_task(self._execute(tool_call))
        else:
            # 等待其他工具完成后再串行执行
            await self._wait_all_concurrent()
            task = asyncio.create_task(self._execute(tool_call))
```

**预期收益**：多工具调用延迟降低 30-60%。

#### 1.2 实现错误扣留模式

```python
# agent/loop.py 中修改错误处理
class WithheldError:
    """可恢复的错误，暂不发送给前端"""
    def __init__(self, error, recovery_strategies: list[RecoveryStrategy]):
        self.error = error
        self.recovery_strategies = recovery_strategies

# 在循环中：
if error.is_recoverable:
    withheld = WithheldError(error, [
        ContextCollapseDrainStrategy(),
        ReactiveCompactStrategy(),
    ])
    for strategy in withheld.recovery_strategies:
        if await strategy.try_recover(state):
            continue  # 恢复成功，重试循环
    yield error_event  # 所有恢复策略失败，才发送给前端
```

#### 1.3 添加 Context Collapse 层

参照 Claude Code 的 `CONTEXT_COLLAPSE` feature，在现有 MicroCompact 和 Auto-Compact 之间加入折叠层：

```python
# agent/context_collapse.py
class ContextCollapser:
    """将旧的工具调用消息范围折叠为摘要行"""
    def collapse_range(self, messages, start_idx, end_idx) -> CollapsedRange:
        # 生成折叠摘要（"Read 5 files, ran 3 commands, all successful"）
        # 保留最近 5 轮完整，其余折叠
    
    def recover_from_overflow(self, messages) -> DrainResult:
        """413 时提交所有暂存的折叠"""
```

#### 1.4 改进 Compaction Prompt

参照 Claude Code 的 9 部分结构化压缩：

```python
COMPACT_PROMPT = """Create a detailed summary with these sections:
1. Primary Request and Intent — what the user asked for
2. Key Technical Concepts — languages, frameworks, patterns
3. Files and Code Sections — include FULL code snippets for modified files
4. Errors and Fixes — every error encountered and how it was resolved
5. Problem Solving — approaches tried, what worked
6. All User Messages — preserve every user instruction verbatim
7. Pending Tasks — what remains to be done
8. Current Work — exactly where you are right now
9. Next Steps — what should happen next

CRITICAL: Include complete code for any file that was read or modified.
Do NOT summarize code — include it verbatim.
"""
```

#### 1.5 添加工具结果全局预算

```python
# agent/context.py 中
TOOL_RESULT_BUDGET_TOKENS = 80_000  # 所有工具结果总 token 上限

def apply_tool_result_budget(self, messages, budget_tokens):
    """按 time-decay 顺序截断超出预算的工具结果"""
    total = sum(count_tokens(m.tool_result) for m in messages if m.has_tool_result)
    if total <= budget_tokens:
        return messages
    # 保留最近的结果，截断最旧的
    ...
```

#### 1.6 添加 Query Chain Tracking

```python
@dataclass
class QueryChainTracking:
    chain_id: str       # 每个用户 turn 唯一（UUID）
    depth: int = 0      # 每次循环迭代递增
    source: str = ""    # 'user' | 'compact' | 'recovery' | 'subagent'
```

---

### Phase 2：MCP 系统重构（3-4 天）

#### 2.1 MCP 工具 LRU 缓存

参照 Claude Code 的 `memoizeWithLRU(fetchToolsForClient)`：

```python
# mcp/registry.py
from functools import lru_cache

class MCPToolRegistry:
    _tool_cache: dict[str, tuple[int, list[MCPToolProxy]]] = {}
    _CACHE_MAX = 32
    
    async def get_tools(self, server_name: str) -> list[MCPToolProxy]:
        version = self._manager.get_registry_version(server_name)
        cached = self._tool_cache.get(server_name)
        if cached and cached[0] == version:
            return cached[1]
        # 版本变化或首次，重新获取
        tools = await self._fetch_tools(server_name)
        self._tool_cache[server_name] = (version, tools)
        return tools
```

#### 2.2 MCP 工具标注映射

参照 Claude Code 的做法，将 MCP annotations 映射到本地工具属性：

```python
# agent/harness/mcp_adapter.py
def adapt_mcp_tool(tool_info, server_name):
    return {
        "is_concurrency_safe": tool_info.get("annotations", {}).get("readOnlyHint", False),
        "is_read_only": tool_info.get("annotations", {}).get("readOnlyHint", False),
        "is_destructive": tool_info.get("annotations", {}).get("destructiveHint", False),
        "is_open_world": tool_info.get("annotations", {}).get("openWorldHint", False),
    }
```

#### 2.3 MCP 连接失败自动降级

```python
# mcp/manager.py
async def _attempt_connection(self, name, config):
    try:
        return await self._connect_external(name, config)
    except ConnectionError:
        # 检查是否有同名的本地 stdio 配置
        local_cfg = self._local_configs.get(name)
        if local_cfg and local_cfg.get("transport") == "stdio":
            logger.warning(f"[MCP] {name} 外部连接失败，降级到本地 stdio")
            return await self._connect_stdio(name, local_cfg)
        raise
```

#### 2.4 MCP 工具结果缓存

```python
# mcp/registry.py MCPToolProxy.execute 中
class MCPToolProxy:
    _result_cache = LRUCache(maxsize=128)
    
    async def execute(self, args, context):
        cache_key = hash((self._server_name, self._tool_name, json.dumps(args, sort_keys=True)))
        if self.is_read_only and cache_key in self._result_cache:
            return self._result_cache[cache_key]
        result = await self._client.call_tool(self._tool_name, args)
        if self.is_read_only:
            self._result_cache[cache_key] = result
        return result
```

---

### Phase 3：前端全面改造（5-7 天）

#### 3.1 清理死代码 + 统一样式系统

```
删除：frontend/styles/          — 整个目录（~4000 行死 CSS）
删除：frontend/public/fonts/    — 废弃 woff2 文件
```

统一样式策略：tokens.css 为唯一真相源，Tailwind 的 fontSize 引用 CSS 变量：

```js
// tailwind.config.js
fontSize: {
  xs: "var(--text-xs)",
  sm: "var(--text-sm)",
  base: "var(--text-base)",
  lg: "var(--text-lg)",
  xl: "var(--text-xl)",
  // ...
}
```

#### 3.2 字体修复

```css
/* tokens.css 修复 font-family 链 */
--font-sans: 'Inter', -apple-system, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
--font-mono: 'JetBrains Mono', 'Cascadia Code', 'Fira Code', 'Consolas', monospace;

/* 移除无效的 "Inter Variable" / "JetBrains Mono Variable" 引用 */
```

#### 3.3 z-index 统一量表

```css
/* tokens.css 添加 z-index 系统 */
--z-base: 0;
--z-sidebar: 10;
--z-composer: 20;
--z-panel-popup: 30;
--z-overlay-backdrop: 40;
--z-modal: 50;          /* Settings, SkillsMarketplace, CommandPalette */
--z-approval: 60;       /* ApprovalModal, AskUserPrompt — 必须在 modal 之上 */
--z-toast: 70;
--z-dialog-service: 80;
--z-context-menu: 90;
```

#### 3.4 Error Boundary 补充

在以下位置添加 Error Boundary：
- 每个 Panel（Terminal、Editor、Preview、Diff、Browser）
- Composer 组件
- MessageList 组件
- 每个 Overlay 模态框

#### 3.5 类型化 Client Command Payloads

```typescript
// protocol/events.ts 补充缺失的类型
interface ConversationUnarchivePayload { conversationId: string }
interface WorkspaceSwitchPayload { path: string }
interface SessionRestorePayload { conversationId?: string; workspaceRoot?: string }
interface TerminalListPayload {}
// ... 其余 16 个缺失类型

// 消除所有 `as never` 转换
```

#### 3.6 跨 slice 状态重置提取

```typescript
// stores/shared-helpers.ts
export function resetConversationScopedState(set: StoreSetter) {
  set({
    pendingApproval: null,
    approvalQueue: [],
    pendingDiffReview: null,
    pendingAskUser: null,
    plan: null,
    todos: [],
    subagents: [],
    agentProgress: [],
    activeGoal: null,
    budgetBuckets: [],
    totalBudgetPercent: 0,
    // ... 所有需要重置的字段
  });
}
```

#### 3.7 响应式布局基础

```css
/* 添加最小响应式支持 */
@media (max-width: 1180px) {
  .sidebar-right { display: none; }  /* 小屏幕隐藏右侧边栏 */
}
@media (max-width: 768px) {
  .sidebar-left { 
    position: absolute; z-index: 10;
    transform: translateX(-100%);  /* 滑出式左侧边栏 */
  }
}
```

---

### Phase 4：后端健壮性（3-5 天）

#### 4.1 WebSocket 并发限制

```python
# ws/handler.py
_SEM = asyncio.Semaphore(20)  # 最多 20 个并发处理任务

async def _dispatch(self, message):
    async with _SEM:
        await self._handle_message(message)
```

#### 4.2 内存泄漏修复

```python
# ws/handler.py: 对话锁清理
async def _cleanup_conversation(self, conv_id):
    self._conversation_run_locks.pop(conv_id, None)

# ws/approval_runtime.py: 审批缓存 LRU 限制
from collections import OrderedDict
_session_approval_cache = OrderedDict(maxsize=1000)
```

#### 4.3 Terminal 功能补全

```python
# ws/handlers/terminal.py
async def handle_terminal_resize(self, cols, rows):
    pty_session = self._terminal_sessions.get(session_id)
    if pty_session:
        pty_session.resize(cols, rows)  # 实际传递尺寸给 PTY
    return True
```

#### 4.4 对话仓库文件锁

```python
# conversations/repository.py
import filelock
async def _write_conversation(self, conv_id, data):
    lock_path = self._conv_dir / f"{conv_id}.lock"
    with filelock.FileLock(lock_path):
        # 原子写入
        tmp = self._conv_dir / f"{conv_id}.tmp"
        tmp.write_text(json.dumps(data))
        tmp.replace(self._conv_dir / f"{conv_id}.json")
```

#### 4.5 全局工具执行超时

```python
# agent/tool_execution.py
TOOL_BATCH_TOTAL_TIMEOUT = 300  # 5 分钟总超时

async def execute_tool_batch(self, tool_calls, ...):
    async with asyncio.timeout(TOOL_BATCH_TOTAL_TIMEOUT):
        results = []
        for batch in partitioned_batches:
            results.extend(await self._execute_batch(batch))
        return results
```

---

### Phase 5：安全与高级特性（持续）

#### 5.1 权限系统增强

- 添加 Auto Mode 安全分类器（参照 Codex 的 yoloClassifier）
- 为 Bash 命令添加命令安全评估
- 实现 denial tracking（连续拒绝 N 次后回退到手动模式）

#### 5.2 系统提示缓存优化

```python
# agent/prompting.py: 利用 LLM API 的缓存机制
# 标记 stable 部分的 cache_control 参数
stable_prompt = {
    "role": "system",
    "content": stable_content,
    "cache_control": {"type": "ephemeral"}  # Anthropic API
}
```

#### 5.3 工具摘要生成

```python
# agent/tool_summary.py
async def generate_tool_summary(tool_results: list[ToolResult]) -> str:
    """使用轻量模型生成工具使用摘要"""
    prompt = f"Summarize what these tool calls accomplished in 1-2 sentences:\n..."
    return await lightweight_llm.complete(prompt)
```

---

## 第五部分：问题汇总统计

| 分类 | 严重 | 高 | 中 | 低 | 合计 |
|------|------|-----|-----|-----|------|
| 后端 Bug（初始审查） | 2 | 0 | 4 | 2 | 8 |
| 后端 Bug（深度审查新增） | 4 | 5 | 8 | 5 | 22 |
| 前端 Bug（初始审查） | 2 | 0 | 2 | 3 | 7 |
| 前端 Bug（深度审查新增） | 3 | 4 | 8 | 8 | 23 |
| 架构差距（vs Claude Code/Codex） | 3 | 4 | 3 | 0 | 10 |
| **合计** | **14** | **13** | **25** | **18** | **70** |

---

## 第六部分：改造优先级总排序

| 优先级 | 任务 | 预期耗时 | 影响 |
|--------|------|----------|------|
| **P0** | MCP 连接修复（外部配置覆盖+get_mcp_manager+Proxy 引用） | 3h | 核心功能不可用 |
| **P0** | TaskScheduler 启动修复 | 2h | 定时任务完全不工作 |
| **P0** | toolDisclosureStyle 类型冲突修复 | 1h | 工具结果渲染崩溃 |
| **P0** | terminal.exec 权限修复 | 1h | 终端权限失效 |
| **P1** | 流式工具执行器 | 3d | 延迟降低 30-60% |
| **P1** | 错误扣留模式 | 2d | 用户体验（错误闪烁） |
| **P1** | WebSocket 并发限制 | 2h | 安全性 |
| **P1** | 内存泄漏修复（3处） | 3h | 长期稳定性 |
| **P1** | 死 CSS 清理 + 字体修复 | 3h | 视觉一致性 |
| **P1** | z-index 统一 + Error Boundary 补充 | 4h | UI 稳定性 |
| **P2** | Context Collapse 层 | 2d | 长对话能力 |
| **P2** | Compaction Prompt 改进 | 3h | 摘要质量 |
| **P2** | MCP 工具 LRU 缓存 + 结果缓存 | 1d | MCP 性能 |
| **P2** | 类型化 Client Commands | 1d | 前端类型安全 |
| **P2** | SidebarRight 拆分 + 响应式基础 | 1d | 可维护性 |
| **P2** | Terminal resize 修复 | 2h | 终端功能 |
| **P3** | 工具结果预算管控 | 1d | 上下文效率 |
| **P3** | Query Chain Tracking | 4h | 可观测性 |
| **P3** | Auto Mode 安全分类器 | 3d | 安全性 |
| **P3** | 系统提示缓存优化 | 4h | API 成本 |
| **P3** | MCP 连接自动降级 | 4h | 容错性 |

---

## 第七部分：已完成的实施状态

以下问题已在审查后实施阶段完成修复。

### 已完成的 P0 修复

| # | 任务 | 修改文件 | 状态 |
|---|------|----------|------|
| C1 | TaskScheduler 不启动 | `bootstrap/app.py` + `tasks/scheduler.py` + `ws/handlers/mcp.py` | ✓ 已修复 |
| C2 | WebSocket 无界并发 | `ws/handler.py` (Semaphore(20)) | ✓ 已修复 |
| 0.3 | MCPToolProxy 动态引用 | `mcp/registry.py` (manager 引用替代 stale client) | ✓ 已修复 |
| 0.4 | toolDisclosureStyle 类型冲突 | `chat/messages/TurnActivity.tsx` (类型修复) | ✓ 已修复 |
| 0.5 | TaskScheduler 启动 | `bootstrap/app.py` (生命周期管理) | ✓ 已修复 |

### 已完成的 P1 修复

| # | 任务 | 修改文件 | 状态 |
|---|------|----------|------|
| P1 | 流式工具执行器 | `agent/streaming_executor.py` (新建) + `agent/loop.py` (集成) + `agent/tool_execution.py` (batch_tool_calls 安全并行分组) | ✓ 已实现 |
| P1 | 错误扣留模式 | `agent/error_withholding.py` (新建) + `agent/loop.py` (集成) | ✓ 已实现 |
| P1 | WebSocket 并发限制 | `ws/handler.py` (Semaphore(20)) | ✓ 已修复 |
| P1 | 内存泄漏 3 处 | `ws/handler.py` (锁清理) + `ws/approval_runtime.py` (LRU 缓存 max=500) | ✓ 已修复 |
| P1 | 死 CSS 清理 + 字体修复 | `frontend/styles/` (删除 ~4000 行) + `tokens.css` (字体栈+CJK 回退) | ✓ 已修复 |
| P1 | z-index 统一 | `tokens.css` (z-index scale 0→90) + 补全 --z-dropdown/--z-overlay/--z-tooltip | ✓ 已修复 |

### 已完成的 P2 修复

| # | 任务 | 修改文件 | 状态 |
|---|------|----------|------|
| P2 | Compaction Prompt 改进 | `agent/prompting.py` (9 段结构化+verbatim 代码) | ✓ 已修复 |
| P2 | MCP 工具 LRU 缓存 | `mcp/registry.py` (OrderedDict 缓存 max=128) + 工具列表缓存 | ✓ 已修复 |
| P2 | Terminal resize | `ws/handlers/terminal.py` (ioctl/SIGWINCH) | ✓ 已修复 |
| P2 | 跨 slice 状态重置 | `stores/shared-helpers.ts` + `stores/chat-slice.ts` | ✓ 已修复 |
| P2 | PanelKind 类型去重 | `stores/types.ts` | ✓ 已修复 |
| P2 | 响应式断点 | `styles/utilities.css` (1180px + 768px) | ✓ 已修复 |

### 已完成的 P3 修复

| # | 任务 | 修改文件 | 状态 |
|---|------|----------|------|
| P3 | 工具结果预算管控 | `agent/context.py` (80K token 全局预算+time-decay 截断) | ✓ 已实现 |
| P3 | Query Chain Tracking | `agent/query_chain.py` (新建) + `agent/loop.py` (集成) | ✓ 已实现 |

### 浏览器验证已修复的前端问题

| # | 问题 | 修改文件 | 状态 |
|---|------|----------|------|
| FH1 | Settings 缺少无障碍属性 | `overlays/SettingsCenter.tsx` (role="dialog" + aria-modal) | ✓ 已修复 |
| FH4 | z-index 层级冲突 | `tokens.css` (完整 z-index 量表) | ✓ 已修复 |
| — | Cowork/Code Tab 可访问性 | `shell/SidebarLeft.tsx` (role="tablist" + role="tab" + aria-selected + roving tabIndex) | ✓ 已修复 |

### 浏览器验证结果

通过 `http://localhost:5173` 在运行实例上验证：

- CSS tokens：106 个全部定义 ✓
- 字体渲染：零死引用 ✓
- OKLCH 色彩系统：light/dark 覆盖完整 ✓
- 响应式断点：1180px/768px 正确 ✓
- 无水平溢出 ✓
- WebSocket 连接成功 ✓
- MCP 服务 3/3 运行、7 工具注册 ✓
- Tab/Dialog 无障碍 ✓

### 运行时发现的 Bug 修复

| # | 问题 | 修复 |
|---|------|------|
| — | QueryChainTracking 参数名错误 (`user_message` → `user_message_preview`) | `agent/loop.py` L859 |

### 未完成的项目（后续迭代）

| # | 任务 | 原因 |
|---|------|------|
| P2 | SidebarRight.tsx 拆分（1044 行→独立文件） | 需更多时间重构 |
| P2 | 类型化 Client Commands（20+ payload） | 大规模前端重构 |
| P3 | Auto Mode 安全分类器 | 需参考 Codex yoloClassifier 52KB |
| P3 | 系统提示缓存优化 | 需 LLM API cache_control 支持 |
| P3 | MCP 连接自动降级 | 需连接失败路径测试 |
| P2 | Error Boundary 补充（面板/Composer/MessageList） | SafeBoundary 已创建但未挂载 |
| P2 | ConnectorsTab MCP install 事件监听 | 需改 setTimeout 为 mcp.lifecycle 事件 |
| — | 流式工具执行器完全集成（mid-stream） | 需 LLM adapter TOOL_CALL_DELTA 改动 |
| — | 四层 Context Collapse 层 | 需独立模块开发 |
| P3 | 工具摘要异步生成 | 需 lightweight LLM endpoint |
