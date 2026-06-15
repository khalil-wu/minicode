# MiniCode 全面优化计划
**基于 Claude Code / Codex 桌面端参考架构对比分析**
**日期**: 2026-06-13

---

## 目录
1. [架构对比总览](#1-架构对比总览)
2. [P0: 核心稳定性](#2-p0-核心稳定性)
3. [P1: Agent Loop 优化](#3-p1-agent-loop-优化)
4. [P2: 工具执行引擎](#4-p2-工具执行引擎)
5. [P3: 流式输出与前端渲染](#5-p3-流式输出与前端渲染)
6. [P4: Session / Workspace / Terminal](#6-p4-session--workspace--terminal)
7. [P5: MCP 与网络搜索](#7-p5-mcp-与网络搜索)
8. [P6: 长期架构演进](#8-p6-长期架构演进)

---

## 1. 架构对比总览

### Claude Code 关键设计模式

| 维度 | Claude Code | MiniCode | 差距 |
|------|------------|----------|------|
| **工具并行** | `StreamingToolExecutor` + `partitionToolCalls` 按 concurrency-safe 分批 | `StreamingToolExecutor` 已实现但**未启用**，实际走 `batch_tool_calls` | 需要激活 |
| **工具取消** | `siblingAbortController` — Bash 错误时取消所有并行兄弟 | 无取消机制，工具继续执行到超时 | **高优** |
| **工具编排** | `runTools` 分区: 连续 read-only → 并发，mutating → 串行 | 类似但通过 `flush_queue` 间接实现 | 中等 |
| **会话桥接** | `bridge/` 模块: REPL ↔ Desktop 通过 WebSocket 双向通信 | WebSocket 事件流，但缺少重连和状态同步 | 需要加固 |
| **进度反馈** | `ToolProgress` 类型 + `pendingProgress` 立即 yield | `tool_output_delta` 事件，但仅限 `run_command` | 需要扩展 |
| **MCP 集成** | `MCPClient` + `ToolSearch` 延迟加载 | `MCPServerManager` + `MCPToolProxy` | 接近 |
| **错误恢复** | `streaming_fallback` → `discard()` → 重新流式 | `_degrade_and_finish` 三阶梯 | 接近 |
| **Context 管理** | 内置压缩 + token 计数 | LLM 摘要压缩 + 字符估算 | 需要改进 |

---

## 2. P0: 核心稳定性（本周完成）

### 2.1 工具调用前置阻塞

**问题**: 模型重复调用失败工具浪费迭代（你的日志: `pip install` 超时 → 下一轮又调用）

**Claude Code 做法**: `partitionToolCalls` 在执行前检查工具状态，`StreamingToolExecutor` 在 `addTool` 时就检查 `isConcurrencySafe`

**MiniCode 现状**: `ToolCallGuardrailController` 在工具**执行后**才记录失败

**修复**:

```python
# backend/agent/tool_execution.py - execute_tool_batch 入口

async def execute_tool_batch(tool_calls, ...):
    for tc in tool_calls:
        # ✅ 前置检查：最近 N 次是否有相同签名的失败
        fingerprint = _tool_fingerprint(tc.name, tc.args)
        if guardrail_controller.is_repeated_failure(fingerprint):
            yield AgentEvent.tool_result(
                id=tc.id,
                summary=f"Blocked: {tc.name} with identical args already failed recently. "
                        f"Try a different approach.",
                is_error=True,
                status="blocked",
                projection="warning",
            )
            continue
        # ... 正常执行
```

```python
# backend/agent/harness/guardrails.py - ToolCallGuardrailController

class ToolCallGuardrailController:
    def __init__(self):
        self._failure_fingerprints: dict[str, tuple[float, int]] = {}
        # fingerprint → (last_failure_time, consecutive_count)
    
    def is_repeated_failure(self, fingerprint: str, window: float = 120.0) -> bool:
        """检查是否是最近 window 秒内的重复失败"""
        entry = self._failure_fingerprints.get(fingerprint)
        if entry is None:
            return False
        last_time, count = entry
        return count >= 2 and (time.time() - last_time) < window
    
    def record_failure(self, fingerprint: str):
        entry = self._failure_fingerprints.get(fingerprint)
        if entry:
            self._failure_fingerprints[fingerprint] = (time.time(), entry[1] + 1)
        else:
            self._failure_fingerprints[fingerprint] = (time.time(), 1)
```

### 2.2 MCP websearch 修复（已完成代码修改）

**已完成**: `backend/mcp/servers/websearch.py` 从 `duckduckgo_search` 改为 `ddgs`
**待完成**: 重启 MCP 服务器（需要重启 MiniCode 或手动重启进程）

### 2.3 超时工具保留部分输出

**问题**: 工具超时 → `status="failed"` → 不进入回退池 → 无最终回复

**修复**:

```python
# backend/agent/tool_execution.py - run_tool_with_timeout

async def run_tool_with_timeout(tc, tool_registry, tool_ctx):
    try:
        result = await asyncio.wait_for(tool.execute(...), timeout=timeout)
        return result
    except asyncio.TimeoutError:
        # ✅ 收集已有的增量输出
        partial_output = ""
        if hasattr(tc, 'output_preview'):
            partial_output = tc.output_preview
        
        return ToolResult(
            content=f"Timed out after {int(timeout)}s.",
            is_error=False,
            status="partial",  # ← 新状态
            result_kind="timeout",
            output_preview=partial_output,
        )
```

```python
# backend/agent/loop.py - _successful_tool_result_records

def _successful_tool_result_records(state):
    return [
        tc for tc in state.tool_calls
        if getattr(tc, "status", "") in {"success", "partial"}  # ← 加入 partial
        and _is_user_visible_tool_output(str(getattr(tc, "tool_output", "") or ""))
    ]
```

---

## 3. P1: Agent Loop 优化（2 周内）

### 3.1 工具取消机制（参考 Claude Code `siblingAbortController`）

**Claude Code 做法**: 当 `bash` 工具报错时，`siblingAbortController.abort()` 取消所有并行兄弟工具

**MiniCode 现状**: 并行工具继续执行到超时，浪费资源

**修复**:

```python
# backend/agent/tool_execution.py - batch_tool_calls

async def batch_tool_calls(tool_calls, ...):
    # ✅ 创建兄弟级别的 AbortController
    sibling_abort = asyncio.Event()
    
    async def execute_with_abort(tc):
        if sibling_abort.is_set():
            return _cancelled_result(tc)
        try:
            result = await run_tool_with_timeout(tc, ...)
            # 如果是 bash 且失败，通知兄弟取消
            if tc.name in {"run_command", "bash", "powershell"} and result.is_error:
                sibling_abort.set()
            return result
        except Exception as e:
            sibling_abort.set()
            raise
    
    # 并行执行
    results = await asyncio.gather(
        *[execute_with_abort(tc) for tc in concurrent_tools],
        return_exceptions=True
    )
```

### 3.2 Final Answer 边界明确化

**问题**: 前端无法区分"模型思考文本"和"最终答案"

**Claude Code 做法**: Anthropic API 原生区分 `text` (思考) 和 `tool_use` (工具调用)，没有 `final_answer` 概念 — 文本就是最终答案

**MiniCode 方案**: 延迟 `final_answer_started` 到确认无工具调用时

```python
# backend/agent/loop.py - StreamEventType.DONE 分支

elif event.type == StreamEventType.DONE:
    usage = event.usage
    finish_reason = event.finish_reason
    
    # ✅ 仅在确认无工具调用时发送 final_answer_started
    if not pending_tool_calls and full_text.strip():
        for ev in final_answer.emit_final(full_text):
            yield ev
    elif pending_tool_calls:
        # 工具调用前的文本 → thinking_delta
        if full_text.strip():
            yield AgentEvent.thinking_chunk(
                full_text,
                source="model_reasoning",
                visibility="timeline",
            )
```

### 3.3 空回复处理优化

**问题**: 模型返回空回复时，三次 nudge 后才强制回退，浪费 3 轮迭代

**优化**: 减少为 2 次 nudge，第 2 次失败后立即回退

```python
# backend/agent/loop.py - 空回复处理

if not full_text.strip():
    if state.empty_reply_retries == 0:
        state.empty_reply_retries = 1
        ctx.append_assistant("(empty)")
        ctx.append_user("请根据工具结果回答用户问题。")
        continue
    
    # ✅ 第 2 次直接回退，不再等待第 3 次
    state.transition = "empty_reply_fallback"
    full_text = _tool_result_fallback_reply(state, reason="模型多次返回空回复。")
    if full_text:
        yield AgentEvent.final_answer_delta(full_text)
        yield AgentEvent.final_answer_committed(full_text)
    break
```

---

## 4. P2: 工具执行引擎（2 周内）

### 4.1 激活 StreamingToolExecutor（参考 Claude Code 的 `runTools`）

**Claude Code 做法**:
- `StreamingToolExecutor.addTool()` 在工具流式到达时就开始执行
- `partitionToolCalls` 将连续的 read-only 工具分为一批并发执行
- 结果按原始顺序 yield

**MiniCode 现状**: `StreamingToolExecutor` 已实现（362 行）但**未激活**（loop.py:1672 注释）

**激活步骤**:

```python
# backend/agent/loop.py - StreamEventType.TOOL_CALL 分支

elif event.type == StreamEventType.TOOL_CALL:
    pending_tool_calls = event.tool_calls
    # ✅ 激活流式工具执行
    if settings.enable_streaming_tool_execution:
        for tc in pending_tool_calls:
            streaming_executor.add_tool_call(tc)
```

**需要前置条件**:
- LLM adapter 需要支持 `TOOL_CALL_DELTA` → 完整参数解析（目前 DeepSeek 等模型拆分 id/name 和 args）
- 需要验证工具 schema 的 `isConcurrencySafe` 标记

### 4.2 工具执行并发限制

**Claude Code 做法**: `getMaxToolUseConcurrency()` 默认 10，通过环境变量可配置

**MiniCode 现状**: `batch_tool_calls` 无并发限制，10 个 read-only 工具全部 `asyncio.gather`

**修复**:

```python
# backend/agent/tool_execution.py

MAX_CONCURRENT_TOOLS = int(os.environ.get("MINICODE_MAX_TOOL_CONCURRENCY", "10"))

async def batch_tool_calls(tool_calls, ...):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TOOLS)
    
    async def limited_execute(tc):
        async with semaphore:
            return await run_tool_with_timeout(tc, ...)
    
    results = await asyncio.gather(
        *[limited_execute(tc) for tc in concurrent_tools],
        return_exceptions=True
    )
```

---

## 5. P3: 流式输出与前端渲染（3 周内）

### 5.1 前端事件状态机

**Claude Code 做法**: 消息类型有明确的层次: `AssistantMessage` (包含 `tool_use` blocks) → `UserMessage` (包含 `tool_result` blocks)

**MiniCode 方案**: 引入显式状态机

```typescript
// frontend/src.v2/chat/chatStreamEvents.ts

type AgentUIState = 'idle' | 'thinking' | 'tool_executing' | 'answering' | 'error';

const deriveAgentState = (events: ServerEvent[]): AgentUIState => {
  // 根据最近的事件序列推断状态
  const lastEvent = events[events.length - 1];
  switch (lastEvent?.type) {
    case 'thinking_delta': return 'thinking';
    case 'tool_call': return 'tool_executing';
    case 'final_answer_started': return 'answering';
    case 'error': return 'error';
    case 'done': return 'idle';
    default: return 'idle';
  }
};
```

### 5.2 工具进度条增强

**Claude Code 做法**: `ToolProgress` 类型支持任意进度数据，`pendingProgress` 立即 yield

**MiniCode 扩展**:

```typescript
// frontend/src.v2/chat/components/ToolCallCard.tsx

const ToolCallCard = ({ toolCall }: { toolCall: ToolCallRecord }) => {
  const progress = toolCall.outputPreview;
  const isRunning = toolCall.status === 'running';
  const isPartial = toolCall.status === 'partial';
  
  return (
    <div className="tool-call-card">
      <ToolCallHeader name={toolCall.name} status={toolCall.status} />
      {isRunning && progress && (
        <div className="tool-progress">
          <ProgressBar value={parseProgress(progress)} />
          <pre className="text-xs text-muted">{progress.slice(-200)}</pre>
        </div>
      )}
      {isPartial && (
        <div className="tool-partial-warning">
          ⚠️ 工具超时，部分结果已保留
        </div>
      )}
    </div>
  );
};
```

### 5.3 差异预览（Diff Review）

**Claude Code 做法**: `FileEditTool` 和 `FileWriteTool` 在执行前生成 diff，用户确认后才执行

**MiniCode 现状**: `approval_request` 事件支持 diff 展示，但 `write_file` 不生成 diff

**修复**:

```python
# backend/tools/agent_tools.py - write_file

async def write_file(file_path, content, tool_ctx, ...):
    resolved_path = _resolve_path(file_path, tool_ctx.workspace_root)
    
    # ✅ 生成 diff
    if resolved_path.exists():
        old_content = resolved_path.read_text(encoding="utf-8")
        diff = generate_unified_diff(old_content, content, file_path)
        # 如果需要审批，附带 diff
        if tool_ctx.permission.requires_approval("write_file"):
            yield AgentEvent.approval_request(
                tool_call_id=tc.id,
                tool_name="write_file",
                args={"file_path": file_path, "content": content[:1000]},
                diff=diff,
            )
    
    # 执行写入
    ...
```

---

## 6. P4: Session / Workspace / Terminal（3 周内）

### 6.1 Session 切换时 Workspace 同步

**问题**: 切换会话后 workspace 路径不更新

**修复**:

```typescript
// frontend/src.v2/chat/chatStreamEvents.ts - stream_resume

case "stream_resume": {
  const ev = e as unknown as { conversation_id?: string; ... };
  const resumeConversationId = ev.conversation_id || conversationId || "";
  
  // ✅ 同步 workspace 绑定
  const conversation = s.conversations.find(c => c.id === resumeConversationId);
  if (conversation?.workspaceRoot && conversation.workspaceRoot !== s.workingDirectory) {
    // 静默切换 workspace（不打断当前对话）
    sendClientCommand({
      type: "workspace.open",
      path: conversation.workspaceRoot,
      silent: true,
    });
  }
  
  s.resumeStreaming(resumeConversationId, ev.tool_calls_pending);
  ...
}
```

### 6.2 Terminal CWD 跟踪

**问题**: `TerminalSession` 的 `_initial_cwd` 在创建后不更新，`cd` 命令后 CWD 不同步

**修复**:

```python
# backend/terminal/session.py - _extract_command_result

async def _extract_command_result(self, command: str, ...):
    # ✅ 在命令执行后检测 CWD 变化
    if command.strip().startswith("cd ") or "cd " in command:
        # 注入 pwd 命令获取新 CWD
        cwd_marker = f"__CWD_{uuid.uuid4().hex[:8]}__"
        await self._send_input(f" && echo {cwd_marker}$(pwd){cwd_marker}")
        # ... 解析输出获取新 CWD
```

### 6.3 Workspace 验证

**问题**: `workspace_root` 未验证目录是否存在

**修复**:

```python
# backend/agent/loop.py - workspace 验证

if workspace_root is not None:
    if not workspace_root.exists() or not workspace_root.is_dir():
        logger.warning("Workspace root does not exist: %s", workspace_root)
        workspace_root = None
        state.workspace_context = None
        # 不 disable 工具，而是提示用户
        yield AgentEvent.error(
            message=f"工作区路径不存在: {workspace_root}。请重新打开文件夹。",
            recoverable=True,
            error_type="workspace_missing",
        )
```

---

## 7. P5: MCP 与网络搜索（1 周内）

### 7.1 MCP 服务器按需启动

**问题**: `autoStart: false` 的服务器在工具被调用时不会自动启动

**修复**:

```python
# backend/mcp/manager.py - MCPToolProxy.call

async def call(self, args, tool_ctx):
    server_name = self._server_name
    manager = self._manager
    
    # ✅ 按需启动
    if server_name not in manager.active_servers:
        config = manager.get_config(server_name)
        if config and not config.auto_start:
            try:
                await manager.start_server(config)
            except Exception as e:
                return ToolResult(
                    content=f"MCP server '{server_name}' failed to start: {e}",
                    is_error=True,
                )
    
    return await self._call_impl(args, tool_ctx)
```

### 7.2 MCP 服务器健康检查增强

**Claude Code 做法**: `MCPClient` 有 `health_check` 方法，断开后自动重连

**MiniCode 现状**: 有健康检查但断开后工具立即不可用

**修复**: 增加 graceful degradation — 服务器断开时返回缓存的工具 schema + 提示信息

---

## 8. P6: 长期架构演进（1-3 个月）

### 8.1 显式状态机（参考 Claude Code 的消息类型层次）

**Claude Code**: 消息类型有明确层次: `AssistantMessage` → `ToolUseBlock` → `ToolResultBlockParam`

**MiniCode**: 事件类型扁平化（80+ 事件类型），难以追踪状态

**建议**: 引入 `AgentSession` 类型，包含状态机和历史:

```python
@dataclass
class AgentSession:
    state: AgentState  # idle → thinking → tool_executing → answering → done
    history: list[AgentEvent]
    tool_executor: StreamingToolExecutor
    context_builder: ContextBuilder
    
    async def process_user_message(self, message: str) -> AsyncIterator[AgentEvent]:
        """单个用户消息的完整处理流程"""
        self.transition_to("thinking")
        # ... 统一的状态转换逻辑
```

### 8.2 可观测性（Observability）

**Claude Code 做法**: `logEvent` 记录每个工具调用的延迟、成功率、token 消耗

**MiniCode 建议**:

```python
# backend/agent/metrics.py

@dataclass
class AgentMetrics:
    tool_latency: dict[str, list[float]]  # tool_name → [latency_ms, ...]
    tool_errors: dict[str, int]  # tool_name → error_count
    llm_tokens: dict[str, int]  # input/output/cache
    iteration_count: int
    compaction_count: int
    
    def record_tool_call(self, name: str, latency_ms: float, is_error: bool):
        self.tool_latency.setdefault(name, []).append(latency_ms)
        if is_error:
            self.tool_errors[name] = self.tool_errors.get(name, 0) + 1
    
    def to_summary(self) -> str:
        return (
            f"Tools: {sum(len(v) for v in self.tool_latency.values())} calls, "
            f"{sum(self.tool_errors.values())} errors | "
            f"Tokens: {self.llm_tokens.get('input', 0)} in / {self.llm_tokens.get('output', 0)} out | "
            f"Iterations: {self.iteration_count}"
        )
```

### 8.3 Context 智能选择

**Claude Code 做法**: 使用实际 token 计数（Anthropic API 返回），不依赖字符估算

**MiniCode 现状**: 使用 `_estimate_content_tokens` 字符估算，精度约 ±30%

**改进**:

```python
# backend/agent/context.py

class ContextBuilder:
    def _estimate_content_tokens(self, content: str) -> int:
        """优先使用 API 返回的实际 token 数，fallback 到估算"""
        if self._last_usage and self._last_usage.input_tokens > 0:
            # 使用实际 token 数校准估算系数
            estimated = self._character_based_estimate(content)
            calibration = self._last_usage.input_tokens / max(self._total_estimated_chars, 1)
            return int(estimated * calibration)
        return self._character_based_estimate(content)
```

---

## 优先级排序

| 优先级 | 任务 | 工作量 | 影响 |
|--------|------|--------|------|
| **P0** | 工具前置阻塞 | 2h | 解决重复调用浪费迭代 |
| **P0** | 超时工具 partial 状态 | 1h | 解决超时后无回复 |
| **P0** | MCP websearch 重启 | 0.5h | 恢复搜索功能 |
| **P1** | 工具取消机制 | 4h | 防止并行工具浪费资源 |
| **P1** | Final Answer 边界 | 3h | 改善前端显示准确性 |
| **P1** | 空回复优化 | 1h | 减少浪费的迭代轮次 |
| **P2** | StreamingToolExecutor 激活 | 8h | 提升工具执行效率 |
| **P2** | 并发限制 | 1h | 防止资源耗尽 |
| **P3** | 前端状态机 | 4h | 改善 UI 状态准确性 |
| **P3** | 工具进度条 | 3h | 改善用户体验 |
| **P3** | Diff 预览 | 4h | 改善文件操作体验 |
| **P4** | Session Workspace 同步 | 2h | 解决切换会话丢失上下文 |
| **P4** | Terminal CWD 跟踪 | 3h | 改善终端体验 |
| **P4** | Workspace 验证 | 1h | 防止无效路径错误 |
| **P5** | MCP 按需启动 | 2h | 改善 MCP 可用性 |
| **P6** | 显式状态机 | 16h | 长期可维护性 |
| **P6** | 可观测性 | 8h | 调试和监控 |
| **P6** | Context 智能选择 | 4h | 提升 context 精度 |

---

## 建议执行顺序

```
Week 1: P0 全部 (3.5h) + P1 空回复优化 (1h) + P5 MCP (2h) = 6.5h
Week 2: P1 工具取消 + Final Answer 边界 = 7h
Week 3: P2 StreamingToolExecutor + 并发限制 = 9h
Week 4: P3 前端状态机 + 进度条 + Diff = 11h
Week 5: P4 Session/Terminal/Workspace = 6h
Week 6+: P6 长期架构 = 28h
```

---

## 测试验证清单

- [ ] P0: 工具重复调用在第 2 次迭代前被阻塞
- [ ] P0: 工具超时后生成包含部分输出的回退答案
- [ ] P0: MCP websearch 重启后搜索正常
- [ ] P1: Bash 工具失败时并行兄弟被取消
- [ ] P1: 最终答案仅在确认无工具调用后发送
- [ ] P1: 空回复 2 次后直接回退
- [ ] P2: read-only 工具并发执行
- [ ] P2: 最多 10 个工具并发
- [ ] P3: 前端状态指示器正确显示 thinking/tool/answering
- [ ] P3: run_command 显示实时输出进度
- [ ] P3: write_file 显示 diff 预览
- [ ] P4: 切换会话自动同步 workspace
- [ ] P4: cd 命令后终端 CWD 更新
- [ ] P4: 不存在的 workspace 路径给出友好提示
- [ ] P5: autoStart=false 的 MCP 服务器按需启动
