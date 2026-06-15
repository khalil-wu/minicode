# Frontend UI Bug Scan Report

Date: 2026-06-14
Scope: `frontend/src.v2/`

## 1. ActivityCell 状态同步问题

### Bug 1.1: 竞态条件导致 activeCell 判断不准确
**文件**: `frontend/src.v2/chat/chatSurfaceState.ts:1080-1143`

**问题**:
- `activeCell` 基于 `assistantMsg.isStreaming` 和 `activityCells.filter(c => c.status === "running")` 判断
- 存在竞态：WebSocket 收到 `tool_result` 事件更新 `status="done"` 时，React 状态更新未同步完成，导致：
  1. `activeCell` 还在显示刚完成的工具
  2. `ChatTurn` 组件传入 `isActive={true}` 给已完成的 `ActivityCell`
  3. ActivityCell 显示"运行中"UI（蓝色点、计时器），但实际已完成

**触发场景**:
- 快速完成的工具调用（< 1s）
- 多个工具串行执行时
- `tool_result` 事件与 `done` 事件之间的窗口期

**根本原因**:
- `chatSurfaceState.ts:1108-1116` 依赖 `c.status === "running"` 判断，但状态更新是异步的
- `ActivityCell.tsx:66` 的 `isRunning = isActive || cell.status === "running"` 导致双重判断，增加不一致风险

**建议修复**:
```typescript
// chatSurfaceState.ts
if (runningActivityCells.length > 0) {
  // 只选择真正运行中的 cell（有 startedAt 但无 completedAt）
  const trulyRunning = runningActivityCells.filter(c => 
    c.startedAt && !c.completedAt
  );
  if (trulyRunning.length > 0) {
    activeCell = trulyRunning[trulyRunning.length - 1];
  }
  committedCells.push(...doneActivityCells);
}
```

### Bug 1.2: ActivityCell elapsed 计时器泄漏
**文件**: `frontend/src.v2/chat/cells/ActivityCell.tsx:12-26`

**问题**:
- `useElapsedTime` hook 在 `isRunning=false` 时清理 interval
- 但当 `isActive` prop 从 `true` → `false` 时（见 Bug 1.1），`isRunning` 变为 `false`
- `clearInterval` 在 `return` cleanup 和 `useEffect` 内部都调用，可能存在重复清理
- 更严重：如果父组件 unmount 快于 effect cleanup，可能泄漏

**触发场景**:
- 快速滚动导致 ActivityCell unmount（`contentVisibility: auto`）
- 多个工具快速完成

**建议修复**:
```typescript
useEffect(() => {
  if (!startedAt || !isRunning) {
    // 统一在这里清理
    return;
  }
  const tick = () => {
    const ms = Date.now() - startedAt;
    setElapsed(formatDuration(ms));
  };
  tick();
  const intervalRef = setInterval(tick, 1000);
  return () => clearInterval(intervalRef);
}, [startedAt, isRunning]);
```

---

## 2. WebSocket 断线恢复问题

### Bug 2.1: 断线期间的 tool_result 事件丢失
**文件**: `frontend/src.v2/hooks/useWebSocket.ts:252-268`

**问题**:
- WebSocket `close` 事件处理中清空所有 pending 状态（approval/diffReview/askUser）
- 但不清理运行中的 `ActivityCell` 状态
- 重连后 `session.restore` 发送，后端返回 `stream_resume`，但：
  1. `stream_resume` 只携带 `accumulated_text` 和 `tool_calls_pending`
  2. 已完成但未 ACK 的工具调用状态丢失
  3. 前端 `ActivityCell` 永久停留在 `status="running"`

**触发场景**:
- 网络闪断（< 5s，在 RECONNECT_STABLE_MS 窗口内）
- 工具刚完成，`tool_result` 还在传输时断线

**影响范围**:
- `ActivityCell` 蓝点永久闪烁
- elapsed 计时器永不停止
- 用户需手动刷新页面

**建议修复**:
```typescript
// useWebSocket.ts:252
ws.addEventListener("close", () => {
  useAppStore.getState().setConnected(false);
  // 清理运行中的工具状态
  useAppStore.setState((state) => ({
    messages: state.messages.map(msg => 
      msg.role === "assistant" && msg.isStreaming
        ? {
            ...msg,
            content_blocks: msg.content_blocks?.map(block => 
              block.type === "tool_call" && block.status === "running"
                ? { ...block, status: "interrupted" }
                : block
            )
          }
        : msg
    ),
    pendingApproval: null,
    // ...
  }));
  // ...
});
```

### Bug 2.2: stream_resume 不触发 ActivityCell 重新渲染
**文件**: `frontend/src.v2/chat/chatStreamEvents.ts:411-426`

**问题**:
- `stream_resume` 事件调用 `s.resumeStreaming()` 和 `s.replaceStreamingText()`
- 但不更新 `content_blocks` 中的 `tool_call` 状态
- `ActivityCell` 依赖 `cell.status` 判断，而 status 来自 `content_blocks`
- 导致：重连后 UI 不更新，用户看到过期的"运行中"状态

**建议修复**:
```typescript
// chatStreamEvents.ts:411
case "stream_resume": {
  const ev = e as unknown as { 
    conversation_id?: string; 
    accumulated_text?: string; 
    tool_calls_pending?: PendingToolCallResume[];
    tool_calls_completed?: Array<{ id: string; status: "success" | "failed" }>;
  };
  s.resumeStreaming(resumeConversationId, ev.tool_calls_pending);
  // 同步已完成的工具状态
  if (ev.tool_calls_completed) {
    ev.tool_calls_completed.forEach(tc => {
      s.updateToolCall(tc.id, { status: tc.status }, resumeConversationId);
    });
  }
  // ...
}
```

---

## 3. Monaco Editor 崩溃场景

### Bug 3.1: Monaco unmount 时未清理 editor 实例
**文件**: `frontend/src.v2/panels/EditorPanel.tsx:636-641`

**问题**:
- `LazyMonacoEditor` 的 `onMount` 回调保存 `editorRef.current = editor`
- 但没有对应的 cleanup（`onUnmount` 或 `useEffect` cleanup）
- 当 tab 关闭或 EditorPanel unmount 时，monaco 实例未 dispose

**触发场景**:
- 快速切换 tab（Ctrl+Tab）
- 关闭多个 tab
- EditorPanel 从 `panelSlots` 移除

**影响**:
- 内存泄漏（每个 editor 实例 ~10-50 MB）
- 多次打开大文件后浏览器卡顿
- 极端情况：浏览器 OOM

**建议修复**:
```typescript
// EditorPanel.tsx
const editorRef = useRef<MonacoEditorInstance | null>(null);

useEffect(() => {
  return () => {
    // 清理 Monaco 实例
    const editor = editorRef.current;
    if (editor && typeof editor.dispose === 'function') {
      editor.dispose();
    }
    editorRef.current = null;
  };
}, []);

// 在 onMount 中
onMount={(editor) => {
  editorRef.current = editor as MonacoEditorInstance;
  editor.onDidChangeCursorPosition((event) => {
    setCursor({ line: event.position.lineNumber, column: event.position.column });
  });
}}
```

### Bug 3.2: LazyMonacoEditor Suspense 边界未捕获 chunk load error
**文件**: `frontend/src.v2/panels/EditorPanel.tsx:16,629-676`

**问题**:
- `lazy(() => import("@monaco-editor/react"))` 在网络不稳定时可能抛出 `ChunkLoadError`
- `Suspense fallback={<EditorLoading />}` 只处理 pending 状态，不处理 error
- 外层的 `ChunkErrorBoundary` 可以捕获，但用户体验差（整个 EditorPanel 崩溃）

**触发场景**:
- 弱网环境首次打开 Editor
- CDN 故障
- Vite dev server 重启时用户正在打开文件

**建议修复**:
```typescript
// EditorPanel.tsx
const [monacoError, setMonacoError] = useState<Error | null>(null);

useEffect(() => {
  // 预加载 Monaco，捕获错误
  loadEditorPanel().catch(err => {
    console.error("Failed to load Monaco:", err);
    setMonacoError(err);
  });
}, []);

// 在渲染中
{monacoError ? (
  <div className="h-full flex flex-col items-center justify-center gap-3">
    <FileWarning size={28} style={{ color: "var(--state-danger)" }} />
    <div>Failed to load editor</div>
    <button onClick={() => window.location.reload()}>Reload</button>
  </div>
) : (
  <Suspense fallback={<EditorLoading />}>
    <LazyMonacoEditor ... />
  </Suspense>
)}
```

### Bug 3.3: 大文件加载后 Monaco 未释放旧内容
**文件**: `frontend/src.v2/panels/EditorPanel.tsx:330-371`

**问题**:
- `loadFileContent()` 检查文件大小，超过限制时设置 `largeFile: true`
- 但如果用户先打开小文件（加载到 Monaco），再切换到大文件，Monaco 仍然渲染，只是显示 `LargeFileNotice` overlay
- Monaco model 未清理，内存中保留两份内容

**建议修复**:
```typescript
// EditorPanel.tsx
useEffect(() => {
  if (activeTab?.largeFile && editorRef.current) {
    // 大文件时清空 Monaco 内容
    if (typeof editorRef.current.setValue === 'function') {
      editorRef.current.setValue("");
    }
  }
}, [activeTab?.largeFile]);
```

---

## 4. 面板布局异常

### Bug 4.1: MainSlots 缺少 ResizeObserver，子面板高度计算错误
**文件**: `frontend/src.v2/shell/MainSlots.tsx:15-40`

**问题**:
- `MainSlots` 使用 `flex: 1` 布局，但 `ChatPane` 和 `EditorPanel` 都使用 `min-h-0` + `flex-1`
- 在某些浏览器（Safari, 旧版 Chrome）上，`min-h-0` 失效，导致：
  1. `MessageList` 滚动容器高度计算错误
  2. EditorPanel Monaco 高度为 0
  3. 用户需手动 resize 窗口触发 reflow

**触发场景**:
- 启动后首次切换到 Code mode
- 从 maximized 状态恢复
- DevTools 打开/关闭

**建议修复**:
```typescript
// MainSlots.tsx
const containerRef = useRef<HTMLDivElement>(null);

useEffect(() => {
  const el = containerRef.current;
  if (!el) return;
  
  const ro = new ResizeObserver(() => {
    // 强制 Monaco 重新计算布局
    window.dispatchEvent(new Event('resize'));
  });
  ro.observe(el);
  return () => ro.disconnect();
}, []);

return (
  <div ref={containerRef} style={{ flex: 1, minWidth: 0, minHeight: 0, ... }}>
    {/* ... */}
  </div>
);
```

### Bug 4.2: WorkbenchShell activeMaximized 状态未同步到 EditorPanel
**文件**: `frontend/src.v2/shell/WorkbenchShell.tsx:98-114`

**问题**:
- `activeMaximized` 变量在 `CodeModeShell` 中计算
- 但 EditorPanel 不知道当前是否 maximized，仍然渲染 sidebar 占位符
- 导致：maximized 后编辑器宽度未完全展开

**触发场景**:
- 双击 EditorPanel header 最大化

**建议修复**:
```typescript
// stores/types.ts
export interface PanelSlot {
  id: string;
  kind: string;
  maximized?: boolean; // 添加到 slot 本身
}

// WorkbenchShell.tsx
const activeMaximized = Boolean(activeSlot?.maximized);
// 移除 activeSlot.kind !== "chat" 条件
```

---

## 5. 其他次要问题

### Issue 5.1: MessageList contentVisibility 导致 ActivityCell 生命周期异常
**文件**: `frontend/src.v2/chat/MessageList.tsx:190-193`

**问题**:
- `contentVisibility: "auto"` 会让浏览器懒加载不在视口的 turn
- 但 ActivityCell 的 `useElapsedTime` hook 依赖持续执行
- 导致：用户向上滚动后，计时器停止；滚回来后时间不同步

**影响**: 低（计时器不是关键数据）

**建议**: 将 elapsed 计算移到 store，不依赖组件生命周期

### Issue 5.2: ChatTurn groupActivityCells 的 running 判断过于严格
**文件**: `frontend/src.v2/chat/components/ChatTurn.tsx:71-75`

**问题**:
- `buffer.some(cell => cell.status === "running")` 阻止聚合
- 但如果最后一个 cell 刚完成（`status="done"`），前面的 running cell 已经变成 committed，不应该阻止聚合

**影响**: 低（UI 展示略显冗余）

---

## 优先级评估

| Bug ID | 严重程度 | 用户影响 | 修复成本 | 优先级 |
|--------|---------|---------|---------|-------|
| 1.1 | P0 | 高（状态错乱） | 中 | **立即修复** |
| 2.1 | P0 | 高（功能卡死） | 高 | **立即修复** |
| 3.1 | P1 | 中（内存泄漏） | 低 | 下个迭代 |
| 1.2 | P2 | 低（计时器小问题） | 低 | 下个迭代 |
| 2.2 | P1 | 中（重连体验差） | 中 | 下个迭代 |
| 3.2 | P2 | 低（弱网场景） | 中 | 可选 |
| 3.3 | P2 | 低（罕见） | 低 | 可选 |
| 4.1 | P1 | 中（偶现布局错乱） | 中 | 下个迭代 |
| 4.2 | P3 | 低（体验优化） | 低 | 可选 |

## 测试建议

1. **ActivityCell 状态同步**: 添加集成测试，模拟快速 tool_result → done 序列
2. **WebSocket 重连**: 单元测试 + E2E，模拟网络闪断
3. **Monaco 内存泄漏**: 手动测试（打开 Chrome DevTools Memory Profiler，重复打开/关闭 tab 50 次）
4. **布局异常**: 视觉回归测试（Percy/Chromatic）

## 扫描文件清单

- `frontend/src.v2/chat/cells/ActivityCell.tsx`
- `frontend/src.v2/chat/chatSurfaceState.ts`
- `frontend/src.v2/chat/chatStreamEvents.ts`
- `frontend/src.v2/chat/components/ChatTurn.tsx`
- `frontend/src.v2/chat/MessageList.tsx`
- `frontend/src.v2/hooks/useWebSocket.ts`
- `frontend/src.v2/panels/EditorPanel.tsx`
- `frontend/src.v2/components/MonacoDiffView.tsx`
- `frontend/src.v2/shell/MainSlots.tsx`
- `frontend/src.v2/shell/WorkbenchShell.tsx`
