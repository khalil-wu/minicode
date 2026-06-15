# 任务显示诊断报告

## 🔍 诊断结果

### ✅ 发现的事实

1. **后端工具已注册**
   - ✅ `TodoWriteTool` 已在 `backend/tools/todo_tool.py` 实现
   - ✅ 工具已在 `backend/agent/loop.py` 注册
   - ✅ 工具权限为 `AUTO`（无需用户批准）

2. **前端状态管理完整**
   - ✅ `useAppStore` 有 `todos` 状态
   - ✅ 有 `setTodos` 和 `updateTodo` 方法
   - ✅ `TodoItem` 类型定义完整

3. **WebSocket 事件已定义**
   - ✅ `TodoTaskUpdateEvent` 已在 `streaming-types.ts` 定义
   - ✅ 事件类型为 `"task.update"`
   - ✅ 包含 `todo_id`, `status`, `content`, `activeForm` 字段

4. **前端事件处理已实现**
   - ✅ `runtimeEvents.ts` 中的 `handleRuntimeEvent` 处理 `"task.update"` 事件
   - ✅ 更新逻辑正确：如果任务存在则更新，否则创建新任务
   - ✅ 更新会触发 React 重渲染

5. **UI 组件已实现**
   - ✅ `InlineTaskList.tsx` - 对话中内联显示
   - ✅ `TaskManagerPanel.tsx` - 侧栏面板显示

---

## 🐛 可能的问题

基于代码分析，任务没有显示可能是以下原因之一：

### 问题 A：Agent 没有调用 `todo_write` 工具

**可能性：高 🔴**

**原因：**
- Agent 可能认为任务太简单，不需要任务列表
- Agent 可能没有收到足够的指导何时使用 `todo_write`
- 系统提示可能不够明确

**症状：**
- 对话中没有任何任务显示
- 侧栏任务面板显示"No tasks"
- 后端日志中没有 `[TODO]` 相关日志

**验证方法：**
```bash
# 查看后端日志，搜索 todo_write 调用
tail -f backend.log | grep -i "todo\|task"
```

**解决方案：** 见下文 "修复方案 A"

---

### 问题 B：后端没有正确发送 WebSocket 事件

**可能性：中 🟡**

**原因：**
- `todo_write` 工具执行后没有发送 `task.update` 事件
- WebSocket 连接断开或消息丢失

**症状：**
- Agent 在对话中提到"任务清单已创建/更新"
- 但前端没有显示任务
- 浏览器 DevTools Network 中看不到 `task.update` 消息

**验证方法：**
```python
# 在 backend/tools/todo_tool.py 的 execute 方法中添加
logger.info(f"[TODO] Session {session_id}: {len(validated)} tasks created")

# 在 WebSocket 发送逻辑中添加
logger.info(f"[WS] Sending task.update event: {event}")
```

**解决方案：** 见下文 "修复方案 B"

---

### 问题 C：任务完成后自动清空

**可能性：中 🟡**

**原因：**
- 代码逻辑：所有任务完成后自动清空列表
  ```python
  # backend/tools/todo_tool.py:195
  all_done = all(t["status"] == "completed" for t in validated) if validated else False
  new_todos = [] if all_done else validated
  ```
- Agent 可能在创建任务后立即全部标记为完成

**症状：**
- 任务短暂显示后消失
- Agent 说"所有任务已完成！任务清单已清空"

**验证方法：**
在浏览器 DevTools Console 中添加：
```javascript
// 监控 todos 变化
useAppStore.subscribe(
  state => state.todos,
  todos => console.log('[TaskList] Todos changed:', todos)
)
```

**解决方案：** 见下文 "修复方案 C"

---

### 问题 D：UI 组件没有正确渲染

**可能性：低 🟢**

**原因：**
- React 组件条件渲染逻辑问题
- CSS 隐藏了任务列表
- 侧栏没有打开或选择了错误的 tab

**症状：**
- 前端 store 中有 `todos` 数据（DevTools 可见）
- 但界面上看不到任务

**验证方法：**
```javascript
// 在浏览器 Console 中检查
console.log('Current todos:', useAppStore.getState().todos);
```

**解决方案：** 见下文 "修复方案 D"

---

## 🔧 修复方案

### 修复方案 A：确保 Agent 创建任务

#### A1. 增强系统提示（推荐 ⭐）

在 agent 系统提示中添加明确的任务管理指导：

**文件：** `backend/agent/harness/guidance.py`（或系统提示文件）

**添加内容：**
```python
# 在工具使用指导部分添加

## 任务管理（todo_write）

对于**复杂的多步骤任务**（≥3 步），你应该：

1. **首先**使用 `todo_write` 创建任务列表，让用户看到整体计划
2. 在开始每个子任务前，更新该任务状态为 `in_progress`
3. 完成后更新为 `completed`
4. 所有任务完成后，清单自动清空

### 何时使用 todo_write？

✅ **应该使用：**
- 用户请求包含多个步骤（如"重构模块"、"构建功能"、"修复多个问题"）
- 任务需要多个文件修改
- 任务需要验证/测试等多个阶段
- 预计耗时 >2 分钟的工作

❌ **不应该使用：**
- 单个简单操作（如修改一个文件、回答问题）
- 纯对话/信息性回复
- 已经在执行中的单步任务

### 示例

**用户：** "帮我重构认证模块，添加 JWT 支持"

**你应该：**
```python
# 1. 首先创建任务列表
todo_write({
    "todos": [
        {
            "id": "1",
            "content": "分析当前认证代码结构",
            "activeForm": "正在分析当前认证代码",
            "status": "in_progress",  # 立即开始第一个任务
            "priority": "high"
        },
        {
            "id": "2",
            "content": "设计 JWT 认证架构",
            "status": "pending",
            "priority": "high"
        },
        {
            "id": "3",
            "content": "实现 JWT 生成和验证",
            "status": "pending",
            "priority": "high"
        },
        {
            "id": "4",
            "content": "更新 API 端点使用 JWT",
            "status": "pending",
            "priority": "medium"
        },
        {
            "id": "5",
            "content": "编写测试",
            "status": "pending",
            "priority": "medium"
        },
        {
            "id": "6",
            "content": "更新文档",
            "status": "pending",
            "priority": "low"
        }
    ]
})

# 2. 开始执行第一个任务（分析代码）
# ... 执行分析 ...

# 3. 完成后更新任务，开始下一个
todo_write({
    "todos": [
        {"id": "1", "content": "分析当前认证代码结构", "status": "completed", "priority": "high"},
        {"id": "2", "content": "设计 JWT 认证架构", "activeForm": "正在设计 JWT 架构", "status": "in_progress", "priority": "high"},
        {"id": "3", "content": "实现 JWT 生成和验证", "status": "pending", "priority": "high"},
        {"id": "4", "content": "更新 API 端点使用 JWT", "status": "pending", "priority": "medium"},
        {"id": "5", "content": "编写测试", "status": "pending", "priority": "medium"},
        {"id": "6", "content": "更新文档", "status": "pending", "priority": "low"}
    ]
})

# 4. 依次执行，每完成一个任务就更新...
```

**重要提示：**
- 同一时刻只有**一个**任务为 `in_progress`
- 每个任务应该是 2-10 分钟可完成的粒度
- 使用清晰的祈使形式描述（"实现功能"而不是"功能实现"）
- `activeForm` 使用进行时（"正在实现功能"）
```

#### A2. 修改工具描述（补充）

**文件：** `backend/tools/todo_tool.py`

**修改 `TodoWriteTool.description`：**
```python
description = (
    "创建或更新会话任务清单，用于模型自驱动地跟踪复杂工作进度。\n"
    "\n"
    "⚠️ 重要使用指南：\n"
    "- 当用户请求包含 3 个以上步骤时，你应该**立即**创建任务列表\n"
    "- 这不仅让用户看到进度，也帮助你组织工作流程\n"
    "- 每完成一个子任务，立即更新状态为 completed，并将下一个任务标记为 in_progress\n"
    "- 同一时刻只有一个任务为 in_progress\n"
    "\n"
    "每个 todo 包含：\n"
    "- id: 唯一标识符（如 '1', '2', '3'）\n"
    "- content: 任务描述（祈使形式，如 '运行测试'、'修复认证 bug'）\n"
    "- activeForm: 进行时形式（如 '正在运行测试'），仅在 in_progress 时显示\n"
    "- status: pending（待处理）| in_progress（进行中）| completed（已完成）\n"
    "- priority: high | medium | low\n"
    "\n"
    "适用场景：\n"
    "✅ 复杂多步骤任务（≥3 步）\n"
    "✅ 用户提供了任务清单\n"
    "✅ 需要跟踪进度的非平凡任务\n"
    "✅ 多个文件/模块的修改\n"
    "✅ 需要多个验证阶段的工作\n"
    "\n"
    "不适用场景：\n"
    "❌ 单个简单任务（1-2 步）\n"
    "❌ 纯对话/信息性问答\n"
    "❌ 已经在执行的单步操作\n"
    "\n"
    "注意：所有任务完成后，清单会自动清空，以便开始新的工作。"
)
```

---

### 修复方案 B：确保后端发送 WebSocket 事件

#### B1. 添加事件发送逻辑

**问题：** `todo_write` 工具可能没有触发 WebSocket 事件

**检查：** `backend/tools/todo_tool.py` 的 `execute` 方法

**当前代码：**
```python
# backend/tools/todo_tool.py:198-203
self._todos[session_id] = new_todos
self._save_to_disk(session_id, new_todos)

# 构建摘要
summary = self._build_summary(old_todos, validated, all_done)
return self._success_result(summary)
```

**问题：** 工具返回后，Agent 会将结果添加到对话，但**没有主动发送 `task.update` 事件**

**解决方案：** 需要在工具执行后发送事件

**修改方式有两种：**

**方式 1：在工具中直接发送事件（如果有 WebSocket 引用）**
```python
# backend/tools/todo_tool.py - 在 execute 方法中添加

async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
    # ... 现有逻辑 ...
    
    self._todos[session_id] = new_todos
    self._save_to_disk(session_id, new_todos)
    
    # 🆕 发送 WebSocket 事件
    if context and hasattr(context, 'emit_event'):
        for todo in new_todos:
            await context.emit_event({
                "type": "task.update",
                "todo_id": todo["id"],
                "status": todo["status"],
                "content": todo["content"],
                "activeForm": todo.get("activeForm", ""),
            })
    
    summary = self._build_summary(old_todos, validated, all_done)
    return self._success_result(summary)
```

**方式 2：在 Agent Loop 中监听工具结果并发送事件**
```python
# backend/agent/loop.py - 在工具执行后添加

# 检测 todo_write 调用后发送事件
if tool_name == "todo_write" and tool_result.success:
    session_id = context.session_id or "default"
    todos = todo_tool.get_session_todos(session_id)
    for todo in todos:
        await emit_ws_event({
            "type": "task.update",
            "todo_id": todo["id"],
            "status": todo["status"],
            "content": todo["content"],
            "activeForm": todo.get("activeForm", ""),
        })
```

**推荐：** 使用方式 2，在 agent loop 中统一处理

---

### 修复方案 C：防止任务过早清空

#### C1. 延迟清空策略

**当前问题：** 所有任务完成后立即清空

**修改：** 保留已完成的任务一段时间（30秒），让用户看到成果

**文件：** `backend/tools/todo_tool.py`

**修改：**
```python
# 从
all_done = all(t["status"] == "completed" for t in validated) if validated else False
new_todos = [] if all_done else validated

# 改为
# 保留已完成的任务，不立即清空
# 前端会在 30 秒后自动淡出（参考 Claude Code 的 RECENT_COMPLETED_TTL_MS）
new_todos = validated
```

**同时在前端添加自动清理逻辑：**

**文件：** `frontend/src.v2/chat/components/InlineTaskList.tsx`

**添加：**
```tsx
const COMPLETED_DISPLAY_TTL = 30_000; // 30 秒

export function InlineTaskList() {
  const todos = useAppStore((s) => s.todos);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const [visibleTodos, setVisibleTodos] = useState(todos);

  // 过滤掉完成超过 30 秒的任务
  useEffect(() => {
    const now = Date.now();
    const filtered = todos.filter(t => {
      if (t.status !== 'completed') return true;
      if (!t.completedAt) return true;
      return (now - t.completedAt) < COMPLETED_DISPLAY_TTL;
    });
    setVisibleTodos(filtered);
    
    // 设置定时器，30 秒后重新过滤
    const timer = setTimeout(() => {
      setVisibleTodos(prev => prev.filter(t => 
        t.status !== 'completed' || !t.completedAt || (Date.now() - t.completedAt) < COMPLETED_DISPLAY_TTL
      ));
    }, COMPLETED_DISPLAY_TTL);
    
    return () => clearTimeout(timer);
  }, [todos]);

  if (visibleTodos.length === 0) return null;
  
  // ... rest of component ...
}
```

**需要在 TodoItem 类型中添加 `completedAt`：**
```typescript
// frontend/src.v2/stores/types.ts
export interface TodoItem {
  id: string;
  content: string;
  status: 'pending' | 'in_progress' | 'completed' | 'blocked';
  priority?: 'high' | 'medium' | 'low';
  activeForm?: string;
  completedAt?: number;  // 🆕 完成时间戳
}
```

---

### 修复方案 D：确保 UI 正确渲染

#### D1. 添加调试日志

**文件：** `frontend/src.v2/chat/components/InlineTaskList.tsx`

**添加：**
```tsx
export function InlineTaskList() {
  const todos = useAppStore((s) => s.todos);
  const isStreaming = useAppStore((s) => s.isStreaming);

  // 🆕 调试日志
  useEffect(() => {
    console.log('[InlineTaskList] Todos updated:', todos);
    console.log('[InlineTaskList] Active:', todos.filter(t => t.status !== 'completed'));
    console.log('[InlineTaskList] Completed:', todos.filter(t => t.status === 'completed'));
  }, [todos]);

  if (todos.length === 0) {
    console.log('[InlineTaskList] No todos, not rendering');
    return null;
  }

  // ... rest of component ...
}
```

#### D2. 确保侧栏正确显示

**检查：** 侧栏是否默认打开？任务 tab 是否可见？

**文件：** 可能是 `frontend/src.v2/panels/SidebarRight.tsx` 或类似文件

**确保：**
- 侧栏有"Tasks"标签页
- 点击后显示 `TaskManagerPanel` 组件
- 组件正确订阅 `useAppStore` 中的 `todos`

---

## 🚀 立即行动清单

### 第 1 步：添加调试日志（5 分钟）

1. **后端日志**
   ```python
   # backend/tools/todo_tool.py
   async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
       # ... 现有代码 ...
       logger.info(f"[TODO] Session {session_id}: Creating {len(validated)} tasks")
       for t in validated:
           logger.info(f"[TODO]   - {t['id']}: {t['content']} ({t['status']})")
       # ... 继续现有逻辑 ...
   ```

2. **前端日志**
   ```tsx
   // frontend/src.v2/chat/components/InlineTaskList.tsx
   useEffect(() => {
     console.log('[TaskList] Current todos:', todos);
   }, [todos]);
   ```

### 第 2 步：测试场景（10 分钟）

1. 打开 MiniCode 桌面应用
2. 打开浏览器 DevTools（F12）
3. 打开后端日志终端
4. 发送测试消息：
   ```
   "帮我创建一个包含 3 个文件的 React 项目：
   1. package.json
   2. src/App.tsx
   3. src/index.tsx"
   ```

5. 观察：
   - 后端日志中是否有 `[TODO]` 输出？
   - 浏览器 Console 中是否有 `[TaskList]` 输出？
   - 对话中是否显示任务列表？
   - 侧栏是否显示任务？

### 第 3 步：根据诊断结果修复（30-60 分钟）

**场景 A：** 后端没有 `[TODO]` 日志
→ Agent 没有调用 `todo_write`
→ 执行**修复方案 A**（增强系统提示）

**场景 B：** 后端有日志，但前端没有
→ WebSocket 事件没有发送
→ 执行**修复方案 B**（添加事件发送）

**场景 C：** 前端有日志，但任务立即消失
→ 任务过早清空
→ 执行**修复方案 C**（延迟清空）

**场景 D：** 前端有日志和数据，但界面不显示
→ UI 渲染问题
→ 执行**修复方案 D**（检查组件条件）

---

## 📊 预期结果

完成修复后，用户应该能够：

✅ **在对话中看到任务列表**
- 边框清晰的任务框
- 任务编号（#1, #2, #3）
- 状态图标（○ □ ● ■ ✓）
- 进度提示（2/5 completed）

✅ **在侧栏查看任务**
- 任务按状态分组
- 显示总览统计
- 可以展开查看详情

✅ **实时看到进度更新**
- Agent 开始任务时，图标变为 ●
- Agent 完成任务时，图标变为 ✓
- 完成任务有勾动画

---

## 🎯 下一步优化

修复显示问题后，可以继续：

1. **视觉增强**（Phase 4）
   - 添加任务框边框和背景
   - 添加完成动画
   - 添加进度条

2. **功能增强**（Phase 5）
   - 任务依赖关系
   - 子任务支持
   - 任务估时

---

**准备好了！** 让我们开始第 1 步：添加调试日志 🚀
