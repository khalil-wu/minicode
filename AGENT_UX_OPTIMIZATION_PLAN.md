# MiniCode Agent UX 优化方案

## 📋 调研发现

### 当前状态分析

#### ✅ 我们已有的功能
1. **任务管理系统**
   - 后端：`TodoWriteTool` + `TodoReadTool`
   - 前端：`InlineTaskList` 组件（内联在对话中）
   - 侧栏：`TaskManagerPanel` 组件

2. **任务显示位置**
   - ✅ **对话内联显示**：`InlineTaskList` 在消息流中显示任务进度
   - ✅ **侧栏面板**：`TaskManagerPanel` 显示完整任务列表
   - ✅ **状态图标**：○（pending）、●（in_progress）、✓（completed）

3. **后台任务**
   - ✅ `BackgroundTaskEntry` 类型支持
   - ✅ 后台任务在侧栏单独显示

#### ❓ 可能的问题

**任务没有显示的可能原因：**

1. **Agent 没有调用 `todo_write` 工具**
   - Agent 可能认为任务太简单，不需要创建任务列表
   - Agent 可能不知道有这个工具

2. **侧栏没有打开或选择正确的面板**
   - 侧栏有多个 tab：文件、任务、设置等
   - 用户可能在其他 tab

3. **任务完成后自动清空**
   - 代码中有逻辑：所有任务完成后清空列表
   - 完成的任务只显示 30 秒（Claude Code 的策略）

4. **UI 状态同步问题**
   - WebSocket 消息可能没有正确更新前端状态
   - Store 更新逻辑可能有问题

---

## 🎯 优化方案

### Phase 1: 诊断与修复（P0）

#### 1.1 确认任务创建

**检查点：**
- [ ] Agent 是否在使用 `todo_write` 工具？
- [ ] 后端是否正确处理 `todo_write` 调用？
- [ ] WebSocket 是否正确推送任务更新？

**验证方法：**
```python
# 在 backend/tools/todo_tool.py 的 execute 方法中添加日志
logger.info(f"[TODO] Creating {len(validated)} tasks for session {session_id}")
logger.info(f"[TODO] Tasks: {validated}")
```

#### 1.2 检查前端状态同步

**检查点：**
- [ ] `useAppStore` 中的 `todos` 是否更新？
- [ ] WebSocket 消息处理是否正确？
- [ ] React 组件是否正确订阅状态？

**验证方法：**
```tsx
// 在 InlineTaskList.tsx 中添加调试日志
useEffect(() => {
  console.log('[TaskList] Todos updated:', todos);
}, [todos]);
```

#### 1.3 检查侧栏显示

**检查点：**
- [ ] 侧栏是否打开？
- [ ] 是否选择了"Tasks"标签？
- [ ] `TaskManagerPanel` 是否正确渲染？

---

### Phase 2: 参考 Claude Code 优化任务 UX（P1）

#### 2.1 任务列表增强（参考 TaskListV2.tsx）

**Claude Code 的最佳实践：**

1. **智能优先级排序**
   ```typescript
   // 优先级：最近完成 > 进行中 > 待处理 > 旧的已完成
   const prioritized = [
     ...recentCompleted,    // 30秒内完成的
     ...inProgress,
     ...pending,
     ...olderCompleted
   ];
   ```

2. **完成任务的渐隐**
   ```typescript
   // 完成的任务显示 30 秒后自动隐藏
   const RECENT_COMPLETED_TTL_MS = 30_000;
   ```

3. **任务图标系统**
   ```typescript
   // Claude Code 使用 figures 包的图标
   completed:  figures.tick           // ✓
   in_progress: figures.squareSmallFilled  // ■
   pending:    figures.squareSmall     // □
   ```

4. **任务计数摘要**
   ```
   5 tasks (2 done, 1 in progress, 2 open)
   ```

5. **阻塞关系显示**
   ```typescript
   // 显示被哪些任务阻塞
   blocked by: #1, #3
   ```

#### 2.2 任务在对话中的显示优化

**当前实现：**
```tsx
// InlineTaskList.tsx - 简单的列表
○ Task 1
● Task 2 (正在执行)
✓ Task 3
```

**Claude Code 的实现：**
```
┌─ Tasks ────────────────────
│ ✓ #1 Setup environment
│ ■ #2 Install dependencies
│ □ #3 Run tests (blocked by: #2)
│ □ #4 Deploy
└────────────────────────────
```

**建议优化：**
- 添加任务 ID（#1, #2, #3...）
- 添加边框/分隔符，让任务列表更突出
- 显示阻塞关系
- 添加进度条（如 3/5 completed）

#### 2.3 侧栏任务面板增强

**当前实现（TaskManagerPanel.tsx）：**
- ✅ 基础的任务列表
- ✅ 圆形状态指示器
- ✅ 完成任务的删除线

**Claude Code 的额外功能：**
1. **任务详情展开**
   - 点击任务查看完整描述
   - 显示子任务（如果有）
   - 显示创建时间、完成时间

2. **任务操作**
   - 手动标记完成/未完成
   - 删除任务
   - 重新排序任务

3. **团队协作（如果支持多 agent）**
   - 显示任务所有者（哪个 agent 负责）
   - 显示 agent 的实时活动

4. **任务统计**
   - 总任务数
   - 完成率（百分比）
   - 平均完成时间

---

### Phase 3: Agent 行为优化（P1）

#### 3.1 确保 Agent 创建任务

**问题：** Agent 可能不知道何时应该创建任务

**解决方案：**

1. **在系统提示中强调任务管理**
   ```python
   # backend/agent/system_prompt.py
   
   对于复杂任务（≥3 步），你应该：
   1. 首先使用 todo_write 创建任务列表
   2. 在开始每个子任务前，更新状态为 in_progress
   3. 完成后更新为 completed
   4. 所有任务完成后，清单自动清空
   
   示例：
   用户："帮我重构认证模块"
   你应该：
   1. todo_write: [
        {id: "1", content: "分析当前认证代码", status: "in_progress", ...},
        {id: "2", content: "设计新的认证架构", status: "pending", ...},
        {id: "3", content: "实现新代码", status: "pending", ...},
        {id: "4", content: "编写测试", status: "pending", ...},
        {id: "5", content: "更新文档", status: "pending", ...}
      ]
   2. 开始执行第一个任务
   3. 完成后更新: {id: "1", status: "completed"}，{id: "2", status: "in_progress"}
   4. 依次类推...
   ```

2. **在工具描述中明确使用场景**
   ```python
   # 已经在 TodoWriteTool.description 中，但可以更明确
   description = (
       "创建或更新会话任务清单。"
       "⚠️ 重要：当用户的请求包含多个步骤时，你应该立即创建任务列表。"
       "这不仅帮助用户了解进度，也帮助你自己组织工作。"
       "每完成一个子任务，立即更新状态。"
   )
   ```

#### 3.2 任务粒度指导

**问题：** Agent 可能创建过于粗糙或过于细碎的任务

**最佳实践：**
- ✅ **好的粒度**：每个任务 2-10 分钟完成
- ❌ **太粗**：一个任务 30 分钟+（应该拆分）
- ❌ **太细**：每个任务 30 秒（过度分解）

**示例：**

用户请求："构建一个 TODO 应用"

❌ **太粗：**
```
1. 实现前端
2. 实现后端
3. 部署
```

✅ **合适：**
```
1. 创建项目结构
2. 实现 API 端点
3. 创建数据模型
4. 实现前端组件
5. 连接前后端
6. 编写测试
7. 准备部署配置
```

❌ **太细：**
```
1. 创建 package.json
2. 安装 React
3. 安装 TypeScript
4. 创建 src 目录
5. 创建 components 目录
6. 创建 App.tsx
...（20+ 个任务）
```

---

### Phase 4: 视觉增强（P2）

#### 4.1 InlineTaskList 视觉升级

**当前样式：**
```css
/* 简单列表 */
.task-row {
  display: flex;
  gap: 8px;
}
```

**建议增强：**
```css
.inline-task-list {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--surface-soft);
  padding: 12px 16px;
  margin: 16px 0;
  box-shadow: 0 1px 3px color-mix(in oklch, var(--text-primary) 5%, transparent);
}

.inline-task-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border-subtle);
}

.inline-task-progress {
  font-size: var(--text-xs);
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.task-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 0;
  transition: background-color var(--transition-fast);
}

.task-row:hover {
  background: color-mix(in oklch, var(--accent-primary) 3%, transparent);
  margin: 0 -8px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
}

.task-id {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--text-muted);
  width: 24px;
  text-align: right;
}

.task-icon {
  width: 16px;
  height: 16px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
}

.task-icon-completed {
  color: var(--state-success);
  animation: checkmark-appear 150ms cubic-bezier(0.34, 1.56, 0.64, 1);
}

.task-icon-in-progress {
  color: var(--accent-primary);
  animation: pulse 2s ease-in-out infinite;
}

.task-icon-pending {
  color: var(--text-muted);
  opacity: 0.5;
}
```

#### 4.2 TaskManagerPanel 视觉升级

**建议增强：**

1. **进度条**
   ```tsx
   <div className="task-progress-bar">
     <div 
       className="task-progress-fill" 
       style={{ width: `${(completed / total) * 100}%` }}
     />
   </div>
   ```

2. **任务分组**
   ```tsx
   <section>
     <h3>In Progress (1)</h3>
     {inProgressTasks.map(...)}
   </section>
   
   <section>
     <h3>Pending (3)</h3>
     {pendingTasks.map(...)}
   </section>
   
   <section>
     <h3>Completed (2)</h3>
     {completedTasks.map(...)}
   </section>
   ```

3. **任务时间线**
   ```tsx
   <div className="task-timeline">
     {tasks.map((task, i) => (
       <div className="timeline-item">
         <div className="timeline-dot" />
         <div className="timeline-content">
           <span className="task-title">{task.content}</span>
           <span className="task-time">
             {task.completedAt ? formatDuration(task.completedAt - task.createdAt) : 'ongoing'}
           </span>
         </div>
       </div>
     ))}
   </div>
   ```

4. **完成庆祝动画**
   ```tsx
   {allCompleted && (
     <div className="completion-celebration">
       🎉 All tasks completed!
     </div>
   )}
   ```

---

### Phase 5: 高级功能（P3 - 可选）

#### 5.1 任务依赖关系

**功能：** 某些任务需要等待其他任务完成

**实现：**
```typescript
interface TodoItem {
  id: string;
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
  priority: 'high' | 'medium' | 'low';
  blockedBy?: string[];  // 被哪些任务阻塞
  blocks?: string[];     // 阻塞哪些任务
}
```

**UI 显示：**
```
□ #3 Run tests
  ⚠️ Blocked by: #1, #2
```

#### 5.2 子任务支持

**功能：** 任务可以有子任务

**实现：**
```typescript
interface TodoItem {
  id: string;
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
  children?: TodoItem[];  // 子任务
}
```

**UI 显示：**
```
■ #2 Implement authentication
  ✓ #2.1 Setup JWT
  ■ #2.2 Create middleware
  □ #2.3 Add tests
```

#### 5.3 任务估时

**功能：** Agent 估算每个任务需要多长时间

**实现：**
```typescript
interface TodoItem {
  id: string;
  content: string;
  estimatedMinutes?: number;  // 估算时间
  startedAt?: number;         // 开始时间
  completedAt?: number;       // 完成时间
}
```

**UI 显示：**
```
■ #2 Install dependencies (est. 2 min)
  Started 30s ago
```

#### 5.4 多 Agent 协作

**功能：** 不同 agent 负责不同任务

**实现：**
```typescript
interface TodoItem {
  id: string;
  content: string;
  owner?: string;        // agent 名称
  ownerColor?: string;   // agent 颜色
}
```

**UI 显示：**
```
■ #2 Write tests (@tester)
□ #3 Review code (@reviewer)
```

---

## 🔧 立即行动计划

### Step 1: 诊断问题（今天）

1. **添加调试日志**
   ```python
   # backend/tools/todo_tool.py
   logger.setLevel(logging.DEBUG)
   logger.info(f"[TODO] Session {session_id}: {len(validated)} tasks")
   ```

2. **前端添加调试**
   ```tsx
   // frontend/src.v2/chat/components/InlineTaskList.tsx
   console.log('[TaskList] Rendering with todos:', todos);
   ```

3. **测试场景**
   - 打开 MiniCode
   - 发送："帮我创建一个包含 3 个文件的项目"
   - 观察控制台输出
   - 检查侧栏是否显示任务

### Step 2: 快速修复（1-2 小时）

如果任务没有显示，根据诊断结果修复：

**场景 A：Agent 没有调用 todo_write**
- 修改系统提示，强调任务管理
- 在 tool description 中添加更多示例

**场景 B：前端状态没有更新**
- 检查 WebSocket 消息处理
- 确保 store 正确更新
- 验证组件订阅

**场景 C：UI 位置不对**
- 确保侧栏默认打开
- 考虑在对话中更突出地显示任务

### Step 3: 增强 UX（2-4 小时）

基于 Claude Code 的最佳实践：

1. **优化 InlineTaskList**
   - 添加边框和背景
   - 添加任务 ID
   - 添加进度条
   - 完成动画

2. **增强 TaskManagerPanel**
   - 任务分组（进行中/待处理/已完成）
   - 总览统计
   - 更好的视觉层次

3. **任务图标升级**
   - 使用更清晰的图标
   - 添加动画效果（完成时的勾、进行中的脉冲）

### Step 4: 长期优化（可选）

- 任务依赖关系
- 子任务支持
- 任务估时
- 多 Agent 协作（如果需要）

---

## 📊 对比分析

### 当前 vs Claude Code vs 理想状态

| 功能 | MiniCode（当前）| Claude Code | 理想状态 |
|------|----------------|-------------|----------|
| **任务创建** | ✅ TodoWriteTool | ✅ | ✅ |
| **对话内显示** | ✅ 简单列表 | ✅ 带边框框架 | ✅ 增强视觉 |
| **侧栏面板** | ✅ 基础列表 | ✅ 分组+统计 | ✅ 分组+时间线 |
| **状态图标** | ✅ ○●✓ | ✅ □■✓ | ✅ 带动画 |
| **完成动画** | ❌ | ❌ | ✅ 勾动画 |
| **进度条** | ❌ | ❌ | ✅ |
| **任务 ID** | ❌ | ✅ #1, #2 | ✅ |
| **阻塞关系** | ❌ | ✅ | ✅ |
| **子任务** | ❌ | ❌ | ⚠️ 可选 |
| **估时** | ❌ | ❌ | ⚠️ 可选 |
| **多 Agent** | ❌ | ✅ | ⚠️ 可选 |
| **任务操作** | ❌ | ⚠️ 部分 | ✅ 完整 |

**图例：**
- ✅ 已实现
- ⚠️ 部分实现
- ❌ 未实现

---

## 🎯 优先级建议

### P0 - 必须修复（立即）
- ✅ 诊断任务为什么没有显示
- ✅ 确保基础任务功能正常工作

### P1 - 重要优化（本周）
- 🎨 InlineTaskList 视觉增强（边框、进度、动画）
- 🎨 TaskManagerPanel 分组显示
- 📝 Agent 系统提示优化（确保创建任务）
- 🔢 添加任务 ID

### P2 - 有价值的增强（下周）
- ⏱️ 任务时间跟踪
- 📊 任务统计面板
- 🔗 任务依赖关系

### P3 - 高级功能（未来）
- 👥 多 Agent 协作
- 📁 子任务支持
- ⏲️ 任务估时

---

## 💡 参考资料

### Claude Code 相关文件
- `cc/src/components/TaskListV2.tsx` - 任务列表主组件
- `cc/src/components/tasks/*` - 各种任务类型
- `cc/src/utils/tasks.ts` - 任务工具函数

### 设计参考
- **图标**：`figures` npm 包（Terminal 友好的 Unicode 图标）
- **动画**：参考 Phase 5 的 checkmark-appear、pulse
- **颜色**：使用设计令牌（--state-success, --accent-primary 等）

---

## ✅ 验收标准

完成后，用户应该能够：

1. **看到任务进度**
   - [ ] 在对话中看到清晰的任务列表
   - [ ] 任务有编号（#1, #2, #3）
   - [ ] 进度一目了然（2/5 completed）

2. **理解任务状态**
   - [ ] 清晰的图标（□ 待处理、■ 进行中、✓ 已完成）
   - [ ] 完成任务有庆祝动画
   - [ ] 进行中的任务有脉冲效果

3. **在侧栏管理任务**
   - [ ] 任务按状态分组
   - [ ] 显示总览统计
   - [ ] 可以展开查看详情

4. **感受到 Agent 的组织性**
   - [ ] 复杂任务自动创建清单
   - [ ] 任务粒度合适（2-10分钟/任务）
   - [ ] 及时更新进度

---

**准备好了吗？** 让我们开始优化！🚀

建议从 **Step 1: 诊断问题** 开始，确定任务是否正在创建和显示。
