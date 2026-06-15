# MiniCode 项目完整审查报告
**日期**: 2026-06-13  
**审查范围**: UI、Agent Loop、后端流程、前端事件处理、Session/Workspace 管理、Write 文件流程

---

## 执行摘要

基于你提供的日志和代码审查，项目存在以下核心问题：

1. **工具调用重复阻塞机制不够智能** — 模型重复调用失败的工具导致迭代浪费
2. **超时后无最终回复** — 工具超时后模型没有生成最终回答，用户看到空响应
3. **流式输出缺少明确的 final_answer 边界** — 前端难以区分"思考文本"和"最终答案"
4. **Session/Workspace 切换时状态同步不完整** — 切换会话后 workspace 绑定丢失
5. **文件写入流程缺少增量 diff 预览** — 大文件写入时用户无法看到实时进度

---

## 1. Agent Loop 核心流程分析

### 1.1 当前架构（`backend/agent/loop.py`）

```
用户消息 → Context Pipeline → LLM 流式调用 → 工具执行 → 循环/终止
              ↓                    ↓                ↓
          压缩/预算检查      Stream Events    工具结果累积
```

**关键组件**:
- `run_agent_loop`: 主循环（while True）
- `FinalAnswerController`: 管理最终答案的流式输出和撤回
- `ToolCallGuardrailController`: 工具调用护栏（阻止重复调用）
- `ErrorWithholdingController`: 错误扣留和恢复策略

### 1.2 现存问题

#### 问题 1: 工具调用阻塞逻辑触发过晚

**现象**（来自你的日志）:
```
Iteration 1/60 → pip install torch ... [timeout 120s]
Iteration 2/60 → pip install torch ... [blocked: repeated failed tool call]
```

**根因**: `ToolCallGuardrailController` 在工具**执行后**才记录失败，模型在**下一次迭代**才收到阻塞反馈。

**位置**: `backend/agent/tool_execution.py:1658-1670`
```python
async for ev in _execute_tool_batch(...):
    yield ev
# guardrail_controller.halt_decision 在工具执行后才检查
if guardrail_controller.halt_decision is not None:
    # 此时已经浪费了一轮 LLM 调用
```

**修复方向**:
- **前置阻塞**: 在 `_execute_tool_batch` **之前**检查每个工具调用是否与最近失败的调用重复
- **参数级指纹**: 使用 `(tool_name, args_hash)` 作为去重 key，而不仅仅是 `tool_name`

---

#### 问题 2: 超时后模型未生成最终回复

**现象**:
```
run_command [failed] Tool 'run_command' timed out after 120s.
工具调用失败，而且模型没有生成最终回复。这轮不能当作成功完成，失败点如下：
```

**根因**: `_degrade_and_finish` 的降级阶梯（Tier 2）在超时场景下没有触发。

**位置**: `backend/agent/loop.py:1269-1298`
```python
except asyncio.TimeoutError:
    async for ev in _degrade_and_finish(
        ...
        profile=_RecoveryProfile(
            allow_last_resort=True,  # ✅ 允许回退
            ...
        ),
    ):
        yield ev
```

**但是**，`_degrade_and_finish` 的 Tier 2 要求 `_successful_tool_result_records(state)` 非空：

```python
# Tier 2: if prior tool calls succeeded, synthesize from saved results
if profile.allow_last_resort and _successful_tool_result_records(state):
    # 调用 llm.simple_chat 生成回退答案
```

**问题**: 如果工具超时时 `tool_output` 仍然为空（没有 stdout），`status` 会被标记为 `"failed"`，不会进入 `_successful_tool_result_records`。

**修复方向**:
- **工具超时时保留部分输出**: 即使超时，如果工具已经产生了 `tool_output_delta`（stdout），应该将累积的 `outputPreview` 作为候选证据
- **放宽 "successful" 定义**: 引入 `status="partial"` 状态，允许超时但有部分输出的工具进入回退池

---

#### 问题 3: 流式输出中 `final_answer_started` 事件发送不确定

**现象**: 前端难以区分以下情况：
- 模型在"推理"（thinking），工具调用即将到来 → 不应显示为最终答案
- 模型在"回答"（final answer），不会再有工具调用 → 应显示为最终答案

**根因**: `FinalAnswerController` 的 `stream_delta` 方法在文本累积到 `_TEXT_DRAFT_STREAM_THRESHOLD_CHARS`（32 字符）时才发送 `final_answer_started`，但此时**无法保证**后续不会有工具调用。

**位置**: `backend/agent/loop.py:149-165`
```python
def stream_delta(self, content: str) -> list[AgentEvent]:
    if not content:
        return []
    events: list[AgentEvent] = []
    if not self.final_started:
        self.final_started = True
        events.append(AgentEvent.final_answer_started())  # ← 过早发送
    self.streamed_text += content
    events.append(AgentEvent.final_answer_delta(content))
    return events
```

**修复方向**:
- **延迟 `final_answer_started`**: 仅在 LLM 流结束（`StreamEventType.DONE`）且 `pending_tool_calls` 为空时才发送
- **引入 `thinking_delta` 事件**: 在工具调用前的文本全部走 `thinking_delta`，前端显示为"正在思考"

---

## 2. 前端事件处理分析

### 2.1 流事件处理（`frontend/src.v2/chat/chatStreamEvents.ts`）

**核心逻辑**:
```typescript
switch (e.type) {
  case "final_answer_started":
    finalAnswerConversations.add(finalAnswerKey(conversationId));
    s.setFinalAnswerStreaming(conversationId, true);
    break;
  case "final_answer_delta":
    s.appendTextChunk(ev.content, conversationId);
    break;
  case "final_answer_retracted":
    s.replaceStreamingText(conversationId, "");
    break;
  case "final_answer_committed":
    finalAnswerConversations.delete(finalAnswerKey(conversationId));
    s.setFinalAnswerStreaming(conversationId, false);
    break;
}
```

**问题**: 前端依赖后端准确发送 `final_answer_started` 来决定是否显示"最终答案"UI，但后端当前**无法保证**发送时机的正确性（见上述问题 3）。

**修复方向**:
- **前端增加状态机**: 引入 `answering` / `thinking` / `tool_executing` 三个状态，根据事件序列自动推断
- **后端明确区分事件**: 
  - `thinking_delta` → 思考中
  - `tool_call` → 工具执行中
  - `final_answer_started` → 确认进入最终答案阶段（仅在 `DONE` 后发送）

---

## 3. Session/Workspace 管理

### 3.1 Conversation Repository（`backend/conversations/repository.py`）

**存储结构**:
```
data/conversations/
  ├── conv_abc123.meta.json          # 元数据（title, workspace_root, git_branch, etc.）
  ├── conv_abc123.transcript.jsonl   # 消息列表（每行一个 JSON）
  └── conv_abc123.snapshot.json      # 上下文快照（context_builder 的 history）
```

**问题**: 
1. **Workspace 绑定在切换 session 时丢失** — `workspace_root` 存储在 meta 中，但前端切换会话后没有主动拉取最新的 `workspace_root` 并更新 UI
2. **Transcript 和 Snapshot 不一致** — 如果 transcript 写入失败但 snapshot 成功，下次加载会话时会触发自动恢复（`_load_record:382-410`），但恢复逻辑依赖 `snapshot.history`，可能丢失 `tool_calls` 等字段

**修复方向**:
- **会话切换时主动同步 workspace**: 前端在 `handleChatStreamEvent` 收到 `stream_resume` 时，检查 `conversations` 中对应会话的 `workspace_root`，如果与当前 `workingDirectory` 不一致则更新
- **事务性写入**: 使用临时文件 + 原子重命名保证 transcript 和 snapshot 的一致性

---

### 3.2 前端 Workspace 状态（`frontend/src.v2/chat/chatStreamEvents.ts:51-66`）

```typescript
const clearMissingWorkspaceBinding = (conversationId?: string) => {
  useAppStore.setState((state) => {
    const targetId = conversationId || state.conversationId;
    return {
      appMode: "cowork",
      workingDirectory: "",  // ← 直接清空，没有检查 meta.json
      workspaceGit: null,
      fileTreeVersion: state.fileTreeVersion + 1,
      conversations: state.conversations.map((conversation) =>
        !targetId || conversation.id === targetId
          ? { ...conversation, workspaceRoot: "", worktreePath: "", gitIsolated: false }
          : conversation,
      ),
    };
  });
};
```

**问题**: 此函数在收到 `error_code === "workspace_missing"` 时被调用，**强制清空** workspace 绑定，但没有给用户恢复的机会（例如 workspace 路径仍然存在，只是后端临时找不到）。

**修复方向**:
- **区分"临时丢失"和"永久丢失"**: 
  - 临时丢失（路径存在但 git 状态异常）→ 显示警告，允许用户重新绑定
  - 永久丢失（路径不存在）→ 清空绑定并提示用户

---

## 4. Write 文件流程

### 4.1 当前流程（`backend/tools/agent_tools.py` - `write_file` tool）

```python
async def write_file(file_path: str, content: str, ...) -> ToolResult:
    # 1. 权限检查
    # 2. 一次性写入整个 content
    # 3. 返回 summary
    return ToolResult(content=summary, ...)
```

**问题**:
- **大文件无进度反馈**: 写入 10MB 文件时用户看不到进度
- **无增量 diff 预览**: 编辑大文件时，diff 在写入**完成后**才生成，用户无法提前审查

### 4.2 理想流程（参考 Claude Code）

```
1. 模型调用 write_file → 前端收到 tool_call 事件
2. 后端开始写入 → 每写入 1MB 发送 tool_output_delta (progress: 20%)
3. 写入完成 → 生成 diff → 发送 tool_result (status: success)
4. 前端显示完整的 diff 审查界面
```

**需要修改的地方**:
- **后端**: `write_file` 工具支持流式写入，使用 `yield AgentEvent.tool_output_delta(tc.id, f"Written {written_bytes} / {total_bytes} bytes")`
- **前端**: `chatStreamEvents.ts` 的 `tool_output_delta` 处理器更新 `outputPreview` 字段，UI 实时显示进度

---

## 5. 具体修复建议

### 5.1 P0: 工具调用重复阻塞前置（解决日志中的核心问题）

**文件**: `backend/agent/tool_execution.py`

**修改点 1**: 在 `_execute_tool_batch` **入口处**增加前置检查：

```python
async def _execute_tool_batch(
    tool_calls: list[ToolCallEvent],
    *,
    guardrail_controller: ToolCallGuardrailController | None = None,
    ...
) -> AsyncIterator[AgentEvent]:
    # ✅ 前置阻塞检查
    if guardrail_controller is not None:
        for tc in tool_calls:
            verdict = guardrail_controller.inspect_before_execution(tc)
            if verdict.block:
                yield AgentEvent.tool_result(
                    id=tc.id,
                    summary=verdict.reason,
                    is_error=True,
                    status="blocked",
                    projection="warning",
                )
                continue
    
    # 原有的执行逻辑
    ...
```

**修改点 2**: `ToolCallGuardrailController` 增加 `inspect_before_execution` 方法：

```python
class ToolCallGuardrailController:
    def __init__(self):
        self._recent_failures: deque[tuple[str, str, float]] = deque(maxlen=10)
        # (tool_name, args_hash, timestamp)
    
    def inspect_before_execution(self, tc: ToolCallEvent) -> GuardrailVerdict:
        args_hash = hashlib.sha256(json.dumps(tc.args, sort_keys=True).encode()).hexdigest()[:8]
        signature = f"{tc.name}:{args_hash}"
        
        # 检查最近 3 次失败中是否有相同的签名
        recent_count = sum(1 for (name_hash, _, ts) in self._recent_failures
                          if name_hash == signature and time.time() - ts < 60)
        if recent_count >= 2:
            return GuardrailVerdict(
                block=True,
                reason=f"Blocked repeated failed call to {tc.name} with identical arguments. "
                       f"Try a different approach or ask the user for clarification.",
            )
        return GuardrailVerdict(block=False)
```

---

### 5.2 P0: 超时工具支持部分输出回退

**文件**: `backend/agent/loop.py`

**修改点**: `_successful_tool_result_records` 放宽定义：

```python
def _successful_tool_result_records(state: AgentState) -> list[Any]:
    successful = [
        tc for tc in state.tool_calls
        if getattr(tc, "status", "") in {"success", "partial"}  # ← 新增 "partial"
        and (
            _is_user_visible_tool_output(str(getattr(tc, "tool_output", "") or ""))
            or bool(str(getattr(tc, "outputPreview", "") or "").strip())  # ← 新增 outputPreview 检查
        )
    ]
    return successful
```

**文件**: `backend/agent/tool_execution.py` - `run_tool_with_timeout`

```python
async def run_tool_with_timeout(tc: ToolCallEvent, ...) -> ToolResult:
    try:
        result = await asyncio.wait_for(tool.execute(...), timeout=timeout)
        return result
    except asyncio.TimeoutError:
        # ✅ 保留已经累积的 outputPreview
        output_preview = getattr(tc, "outputPreview", "") or ""
        return ToolResult(
            content=f"Tool timed out after {timeout}s. Partial output:\n{output_preview[-500:]}",
            is_error=False,  # ← 不标记为 error，允许进入回退池
            status="partial",
            result_kind="timeout",
        )
```

---

### 5.3 P1: 明确 final_answer 边界

**文件**: `backend/agent/loop.py`

**修改点**: 将 `final_answer_started` 延迟到 LLM 流结束且确认无工具调用时发送：

```python
# 在 StreamEventType.DONE 分支：
elif event.type == StreamEventType.DONE:
    usage = event.usage
    finish_reason = event.finish_reason
    
    # ✅ 此时确认是否有工具调用
    if not pending_tool_calls and full_text.strip():
        # 确认进入最终答案阶段
        for ev in final_answer.emit_final(full_text):
            yield ev
```

**同时**，在 `StreamEventType.TEXT_CHUNK` 分支，**不自动发送** `final_answer_started`，而是累积到 `text_buffer`。

---

### 5.4 P1: Session 切换时同步 workspace

**文件**: `frontend/src.v2/chat/chatStreamEvents.ts`

**修改点**: 在 `stream_resume` 分支增加 workspace 同步：

```typescript
case "stream_resume": {
  const ev = e as unknown as { conversation_id?: string; ... };
  const resumeConversationId = ev.conversation_id || conversationId || "";
  
  // ✅ 同步 workspace 绑定
  const conversation = s.conversations.find(c => c.id === resumeConversationId);
  if (conversation && conversation.workspaceRoot) {
    const currentWorkspace = s.workingDirectory;
    if (currentWorkspace !== conversation.workspaceRoot) {
      // 后台静默切换 workspace
      sendClientCommand({
        type: "workspace.open",
        path: conversation.workspaceRoot,
        silent: true,
      });
    }
  }
  
  s.resumeStreaming(resumeConversationId, ev.tool_calls_pending);
  ...
}
```

---

### 5.5 P2: Write 文件增量进度

**文件**: `backend/tools/agent_tools.py` - `write_file`

**修改点**: 使用 `emit_event` 发送增量进度：

```python
async def write_file(
    file_path: str,
    content: str,
    tool_ctx: ToolExecutionContext,
    ...
) -> ToolResult:
    total_bytes = len(content.encode("utf-8"))
    chunk_size = 1024 * 1024  # 1MB
    written_bytes = 0
    
    with open(resolved_path, "w", encoding="utf-8") as f:
        for i in range(0, len(content), chunk_size):
            chunk = content[i:i + chunk_size]
            f.write(chunk)
            written_bytes += len(chunk.encode("utf-8"))
            
            # ✅ 发送进度事件
            if tool_ctx.emit_event:
                progress = int(written_bytes / total_bytes * 100)
                tool_ctx.emit_event({
                    "type": "tool_output_delta",
                    "id": getattr(tool_ctx, "current_tool_call_id", ""),
                    "output": f"Writing... {progress}% ({written_bytes}/{total_bytes} bytes)",
                })
            
            await asyncio.sleep(0)  # 让出控制权
    
    return ToolResult(content=f"File written: {file_path}", ...)
```

---

## 6. 测试验证清单

### 6.1 工具重复调用阻塞
- [ ] 运行你日志中的场景：`pip install torch` 超时 → 模型重试 → **应在第 2 次迭代前阻塞**
- [ ] 检查日志：应该看到 `Blocked repeated failed call to run_command`

### 6.2 超时后生成回退答案
- [ ] 模拟工具超时（设置 `TOOL_TIMEOUTS["run_command"] = 5.0`）
- [ ] 执行一个需要 10 秒的命令
- [ ] 验证：超时后应该看到 `"Tool timed out. Here is what was retrieved:"` 形式的回退答案

### 6.3 Final Answer 边界
- [ ] 发送一个需要工具调用的请求（如"读取 README.md"）
- [ ] 前端应该**不显示**模型在工具调用前的思考文本为"最终答案"
- [ ] 仅在工具执行完成、模型回复后才显示"最终答案"

### 6.4 Session 切换 workspace 同步
- [ ] 创建两个会话，分别绑定不同的 workspace
- [ ] 切换到会话 A → 检查 UI 左上角 workspace 路径 → 切换到会话 B → 验证 workspace 路径自动切换

### 6.5 Write 文件进度
- [ ] 写入一个 5MB 的文件
- [ ] 前端工具调用卡片应该显示 `"Writing... 20%"` → `"Writing... 40%"` → ... → `"File written"`

---

## 7. 架构改进建议（长期）

### 7.1 引入显式状态机（Agent State Machine）

当前 `run_agent_loop` 是一个 `while True` 循环 + 隐式状态转换，难以追踪和调试。建议引入显式状态机：

```python
class AgentState(Enum):
    IDLE = "idle"
    THINKING = "thinking"
    TOOL_EXECUTING = "tool_executing"
    ANSWERING = "answering"
    VERIFYING = "verifying"
    DONE = "done"
    ERROR = "error"

class AgentStateMachine:
    def transition(self, from_state: AgentState, event: str) -> AgentState:
        # 定义所有合法转换
        transitions = {
            (AgentState.THINKING, "tool_call"): AgentState.TOOL_EXECUTING,
            (AgentState.TOOL_EXECUTING, "tool_done"): AgentState.THINKING,
            (AgentState.THINKING, "no_tool_call"): AgentState.ANSWERING,
            ...
        }
        return transitions.get((from_state, event), AgentState.ERROR)
```

### 7.2 工具执行并行化（已有架构，待完全激活）

代码中已经有 `StreamingToolExecutor` 和 `is_tool_concurrency_safe` 的框架（`loop.py:1676-1677` 注释），但尚未完全启用。建议：

- **Phase 1**: 启用读工具并行（`read_file`, `list_files`, `grep_files`）
- **Phase 2**: 启用 MCP 工具并行（`mcp__*__recall`, `mcp__*__search`）
- **Phase 3**: 启用写工具串行队列（避免冲突，但允许读写并行）

### 7.3 前端事件重放（Debugging）

建议在前端增加"事件日志"面板（开发模式），记录所有 WebSocket 事件：

```typescript
if (import.meta.env.DEV) {
  window.__minicode_event_log = [];
  const originalHandler = handleChatStreamEvent;
  handleChatStreamEvent = (e, conversationId, handlers) => {
    window.__minicode_event_log.push({ timestamp: Date.now(), event: e, conversationId });
    return originalHandler(e, conversationId, handlers);
  };
}
```

用户遇到问题时可以导出 `__minicode_event_log`，重放事件流进行调试。

---

## 8. 总结

### 关键问题优先级

| 问题 | 严重性 | 修复难度 | 优先级 |
|------|--------|---------|--------|
| 工具调用重复阻塞不及时 | 高 | 中 | **P0** |
| 超时后无最终回复 | 高 | 中 | **P0** |
| Final Answer 边界不清晰 | 中 | 低 | **P1** |
| Session 切换 workspace 丢失 | 中 | 低 | **P1** |
| Write 文件无进度反馈 | 低 | 中 | **P2** |

### 下一步行动

1. **立即修复（本周）**:
   - 实现 P0-1: 工具调用前置阻塞
   - 实现 P0-2: 超时工具部分输出回退

2. **短期改进（2 周内）**:
   - 实现 P1-3: Final Answer 明确边界
   - 实现 P1-4: Session 切换同步

3. **中期优化（1 个月内）**:
   - 实现 P2-5: Write 文件增量进度
   - 引入显式状态机（重构）

4. **长期规划**:
   - 工具执行并行化
   - 前端事件重放调试工具
   - 完整的端到端集成测试覆盖

---

**报告完成**。建议先从 P0 项开始修复，验证日志中的问题是否解决，再逐步推进 P1/P2。
