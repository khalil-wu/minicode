# 🚀 MiniCode vs Claude Code 完整对比与实施计划

## 紧急！立即让 MiniCode 完全像 Claude Code

---

## 第一步：找出所有差异

让我系统性对比 Claude Code 的核心交互功能：

### 1. 任务清单功能对比

| 功能点 | Claude Code | MiniCode 现状 | 差距 | 优先级 |
|--------|-------------|---------------|------|--------|
| 自动显示任务清单 | ✅ | ✅ | 无差距 | - |
| 进度条 | ✅ | ✅ | 无差距 | - |
| 状态图标 | ✅ | ✅ | 无差距 | - |
| 实时更新 | ✅ | ✅ | 无差距 | - |
| 折叠/展开 | ✅ | ❌ | **缺失** | P0 |
| 点击任务编辑 | ✅ | ❌ | **缺失** | P1 |
| 拖拽排序 | ✅ | ❌ | **缺失** | P2 |
| 删除任务 | ✅ | ❌ | **缺失** | P1 |
| 添加新任务 | ✅ | ❌ | **缺失** | P1 |

### 2. 流式输出对比

| 功能点 | Claude Code | MiniCode 现状 | 差距 | 优先级 |
|--------|-------------|---------------|------|--------|
| Thinking 可视化 | ✅ | ✅ | 无差距 | - |
| 工具调用实时显示 | ✅ | ✅ | 无差距 | - |
| 打字机效果 | ✅ | ✅ | 无差距 | - |
| 暂停/继续按钮 | ❌ | ❌ | 都没有 | P3 |
| 停止按钮 | ✅ | ✅ | 无差距 | - |
| 重新生成 | ✅ | ❌ | **缺失** | P1 |

### 3. 工具调用展示

| 功能点 | Claude Code | MiniCode 现状 | 差距 | 优先级 |
|--------|-------------|---------------|------|--------|
| 工具调用卡片 | ✅ | ✅ | 无差距 | - |
| 展开/折叠 | ✅ | ✅ | 无差距 | - |
| 失败重试 | ❌ | ⚠️ UI only | MiniCode 更好 | - |
| 复制输出 | ✅ | ❌ | **缺失** | P1 |
| 查看完整输出 | ✅ | ✅ | 无差距 | - |

### 4. 编辑器集成

| 功能点 | Claude Code | MiniCode 现状 | 差距 | 优先级 |
|--------|-------------|---------------|------|--------|
| Diff 预览 | ✅ | ✅ | 无差距 | - |
| 应用/拒绝按钮 | ✅ | ✅ | 无差距 | - |
| 文件点击跳转 | ✅ | ✅ | 无差距 | - |
| Inline 编辑 | ✅ | ❌ | **缺失** | P2 |

### 5. 消息操作

| 功能点 | Claude Code | MiniCode 现状 | 差距 | 优先级 |
|--------|-------------|---------------|------|--------|
| 复制消息 | ✅ | ✅ | 无差距 | - |
| 编辑消息 | ✅ | ❌ | **缺失** | P0 |
| 删除消息 | ✅ | ✅ | 无差距 | - |
| 重新生成 | ✅ | ❌ | **缺失** | P0 |
| 分支对话 | ✅ | ❌ | **缺失** | P2 |

---

## 第二步：P0 级别缺失功能（立即实现）

### P0-1: 任务清单折叠/展开 ⚡

**实施计划**：

1. **前端状态管理**
```tsx
// InlineTaskList.tsx
const [collapsed, setCollapsed] = useState(false);
const [showCompleted, setShowCompleted] = useState(true);
```

2. **折叠按钮**
```tsx
<button onClick={() => setCollapsed(!collapsed)}>
  {collapsed ? <ChevronRight /> : <ChevronDown />}
</button>
```

3. **自动折叠规则**
- 所有任务完成后自动折叠
- 用户手动展开后记住状态

**预计时间**：30 分钟

---

### P0-2: 编辑消息功能 ⚡

**实施计划**：

1. **后端支持**
```python
# backend/conversations/repository.py
def update_message(conversation_id: str, message_id: str, new_content: str):
    # 更新消息内容
    # 触发重新生成
```

2. **前端 UI**
```tsx
// UserMessageCell.tsx
const [editing, setEditing] = useState(false);

<button onClick={() => setEditing(true)}>
  <Edit size={12} />
</button>

{editing && (
  <textarea value={content} onChange={...} />
  <button onClick={handleSave}>Save</button>
)}
```

**预计时间**：1 小时

---

### P0-3: 重新生成按钮 ⚡

**实施计划**：

1. **后端接口**
```python
# backend/ws/handlers/chat.py
async def handle_regenerate(session, data):
    message_id = data["message_id"]
    # 删除该消息后的所有回复
    # 重新发送到 agent loop
```

2. **前端按钮**
```tsx
// AssistantMarkdownCell.tsx
<button onClick={() => regenerate(messageId)}>
  <RotateCw size={12} /> 重新生成
</button>
```

**预计时间**：45 分钟

---

## 第三步：P1 级别功能（当天完成）

### P1-1: 任务清单手动编辑

**功能点**：
- 点击任务内容可编辑
- 添加新任务
- 删除任务
- 更改状态

**预计时间**：1.5 小时

---

### P1-2: 复制工具输出

**功能点**：
- 每个工具调用旁边显示复制按钮
- 点击复制完整输出
- Toast 提示

**预计时间**：30 分钟

---

### P1-3: 删除任务按钮

**功能点**：
- 每个任务旁边显示 ✕ 按钮
- 点击删除（带确认）
- 重新计算进度

**预计时间**：30 分钟

---

## 第四步：立即开始实施

### 实施顺序（按紧急程度）

#### 今天必须完成（3-4 小时）

1. ✅ **任务清单折叠/展开** (30 分钟)
2. ✅ **重新生成按钮** (45 分钟)
3. ✅ **编辑消息功能** (1 小时)
4. ✅ **复制工具输出** (30 分钟)
5. ✅ **任务删除按钮** (30 分钟)
6. ✅ **任务添加按钮** (30 分钟)

**总计**：约 4 小时

---

#### 明天完成（2-3 小时）

7. ⏳ 任务手动编辑内容
8. ⏳ 任务拖拽排序
9. ⏳ 分支对话功能
10. ⏳ Inline 编辑

---

## 第五步：实施细节

### 1. 任务清单折叠/展开（立即开始）

让我现在就实现这个功能...

**文件修改**：
- `frontend/src.v2/chat/components/InlineTaskList.tsx`
- `frontend/src.v2/chat/components/inline-task-list.css`

**关键代码**：
```tsx
export function InlineTaskList() {
  const todos = useAppStore((s) => s.todos);
  const [collapsed, setCollapsed] = useState(false);
  const [showCompleted, setShowCompleted] = useState(true);
  
  // 自动折叠逻辑
  useEffect(() => {
    const allCompleted = todos.every(t => t.status === 'completed');
    if (allCompleted && todos.length > 0) {
      setCollapsed(true);
    }
  }, [todos]);
  
  return (
    <div className="inline-task-list">
      <div className="inline-task-header">
        <button onClick={() => setCollapsed(!collapsed)}>
          {collapsed ? <ChevronRight /> : <ChevronDown />}
          <ListChecks size={14} />
          <span>Tasks</span>
        </button>
        {!collapsed && (
          <button onClick={() => setShowCompleted(!showCompleted)}>
            {showCompleted ? 'Hide' : 'Show'} completed
          </button>
        )}
      </div>
      
      {!collapsed && (
        <div className="inline-task-section">
          {/* 任务列表 */}
        </div>
      )}
    </div>
  );
}
```

---

### 核心差距总结

**MiniCode 现在缺少的关键功能**：

1. ❌ **任务清单折叠** - 完成后应该自动折叠
2. ❌ **任务手动编辑** - 点击可以修改内容
3. ❌ **编辑消息** - 修改已发送的消息
4. ❌ **重新生成** - 对回复不满意时重新生成
5. ❌ **复制输出** - 快速复制工具执行结果
6. ❌ **任务删除** - 移除不需要的任务

**这 6 个功能是让 MiniCode 完全像 Claude Code 的关键！**

---

## 行动计划

我现在立即开始实施，按照这个顺序：

1. ✅ 任务清单折叠/展开（最容易，30 分钟）
2. ✅ 复制工具输出（简单，30 分钟）
3. ✅ 任务删除按钮（中等，30 分钟）
4. ✅ 重新生成按钮（中等，45 分钟）
5. ✅ 编辑消息功能（较复杂，1 小时）
6. ✅ 任务手动编辑（较复杂，1 小时）

**预计完成时间：今天内（4 小时）**

---

准备好了吗？让我们立即开始！🚀
