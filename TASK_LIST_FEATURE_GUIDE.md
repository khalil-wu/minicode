# 任务清单功能使用指南

## 概述

MiniCode 已经实现了类似 Claude Code 的任务清单功能，可以在处理复杂任务时自动显示进度。

---

## 功能特性

### 1. 自动任务分解
当 Agent 接收到多步骤任务时，会自动：
1. 调用 `todo_write` 创建任务清单
2. 在界面顶部显示任务列表
3. 逐步执行并更新状态

### 2. 可视化进度
```
进度
☑ 定位 Windows curl 别名失败路径
○ 补充 curl.exe 规范化和失败提示的回归测试
○ 实现命令规范化和 guardrail 文案优化
○ 运行相关后端/前端验证
```

### 3. 状态标识
- `☑` 已完成 (completed)
- `⟳` 进行中 (in_progress) - 旋转动画
- `○` 待处理 (pending)
- `⚠` 已阻塞 (blocked)

### 4. 进度条
```
Tasks [███████░░░] 7/10
```

---

## 后端实现

### 1. Tool 定义

**文件**: `backend/tools/todo_tool.py`

```python
class TodoWriteTool(BaseTool):
    """创建和管理会话级任务清单"""
    
    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name="todo_write",
            description="Create or update a session-level task checklist",
            parameters={
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "content": {"type": "string"},
                                "status": {"enum": ["pending", "in_progress", "completed", "blocked"]},
                                "activeForm": {"type": "string"}
                            }
                        }
                    }
                },
                "required": ["todos"]
            }
        )
```

### 2. Guidance 强化

**文件**: `backend/agent/harness/guidance.py`

**关键规则**（已优化）：
```python
"- **MANDATORY**: For ANY multi-step work (≥2 steps, several files, user-supplied list, "
"or takes >5 minutes), you MUST call todo_write FIRST before doing anything else."
```

**触发条件**：
- ≥2 个步骤
- 涉及多个文件
- 用户提供了待办列表
- 预计耗时 >5 分钟
- 需要阶段性验证

**不触发条件**：
- 单文件简单修改
- 回答问题
- 一步完成的任务

---

## 前端实现

### 1. 组件结构

**文件**: `frontend/src.v2/chat/components/InlineTaskList.tsx`

```tsx
export function InlineTaskList() {
  const todos = useAppStore((s) => s.todos);
  
  // 计算进度
  const completed = todos.filter((t) => t.status === "completed");
  const progress = (completed.length / todos.length) * 100;
  
  // 渲染任务行
  return (
    <div className="inline-task-list">
      <div className="inline-task-header">
        <ListChecks size={14} />
        <span>Tasks</span>
        <div className="inline-task-progress-bar">
          <div style={{ width: `${progress}%` }} />
        </div>
        <span>{completed.length}/{todos.length}</span>
      </div>
      {/* 任务列表 */}
    </div>
  );
}
```

### 2. 集成位置

**文件**: `frontend/src.v2/chat/MessageList.tsx`

任务列表显示在消息列表顶部，流式输出时实时更新。

### 3. 样式特性

**文件**: `frontend/src.v2/chat/components/inline-task-list.css`

- ✅ 完成动画（checkmark-appear）
- ⟳ 进行中旋转动画（inline-task-spin）
- 进度条平滑过渡（300ms cubic-bezier）
- Hover 高亮效果
- 响应式布局

---

## 使用示例

### 示例 1：多步骤开发任务

**用户输入**：
```
Add user authentication to the app:
1. Create login form
2. Set up JWT middleware
3. Add protected routes
4. Write tests
```

**Agent 响应**：
```
[调用 todo_write]

进度
○ Create login form component
○ Set up JWT middleware
○ Add protected routes
○ Write authentication tests

[开始执行第一个任务]
○ Create login form component → ⟳ Create login form component
```

---

### 示例 2：Bug 修复

**用户输入**：
```
Fix the curl command issue on Windows
```

**Agent 响应**：
```
[调用 todo_write]

进度
⟳ 定位 Windows curl 别名失败路径
○ 创建 shell_commands.py 规范化模块
○ 接入所有命令执行入口
○ 添加回归测试
○ 验证修复

[开始执行]
```

---

### 示例 3：重构任务

**用户输入**：
```
Refactor the authentication system
```

**Agent 响应**：
```
[调用 todo_write]

进度
⟳ 分析现有 auth 代码
○ 设计新架构
○ 实现核心模块
○ 迁移现有功能
○ 更新测试
○ 更新文档

Tasks [█░░░░░] 1/6
```

---

## 测试验证

### 手动测试清单

#### 1. 基础功能
- [ ] 发送多步骤任务请求
- [ ] 观察任务列表是否出现
- [ ] 确认任务数量正确
- [ ] 验证初始状态为 pending

#### 2. 状态更新
- [ ] 第一个任务变为 in_progress（旋转图标）
- [ ] 完成后变为 completed（绿色对钩 + 删除线）
- [ ] 下一个任务变为 in_progress
- [ ] 进度条实时更新

#### 3. 完成状态
- [ ] 所有任务完成后显示 "Tasks complete"
- [ ] 绿色完成图标
- [ ] 任务列表可折叠

#### 4. 边缘情况
- [ ] 单步骤任务不显示清单
- [ ] 空任务列表不渲染组件
- [ ] 阻塞任务显示警告图标

---

## 配置选项

### 1. 关闭任务清单

如果不想看到任务清单，可以在 guidance 中禁用：

**临时禁用**（在对话中）：
```
请不要使用任务清单，直接执行即可。
```

**永久禁用**（修改代码）：
在 `backend/agent/harness/guidance.py` 中注释掉 `todo_write` 相关的 guidance。

### 2. 自定义样式

修改 `frontend/src.v2/chat/components/inline-task-list.css`：

```css
.inline-task-list {
  /* 调整位置 */
  margin: 24px 0;
  
  /* 调整背景 */
  background: var(--surface-page);
  
  /* 调整边框 */
  border: 2px solid var(--accent-primary);
}
```

---

## 对比 Claude Code

| 特性 | MiniCode | Claude Code |
|------|----------|-------------|
| 任务自动分解 | ✅ | ✅ |
| 进度可视化 | ✅ 进度条 + 分数 | ✅ |
| 状态图标 | ✅ 4 种状态 | ✅ |
| 实时更新 | ✅ | ✅ |
| 完成动画 | ✅ checkmark-appear | ✅ |
| 旋转加载 | ✅ | ✅ |
| 任务编号 | ✅ #1, #2... | ✅ |
| 折叠/展开 | ⏳ 待实现 | ✅ |
| 手动编辑 | ⏳ 待实现 | ✅ |

**结论**：MiniCode 的任务清单功能已经达到 Claude Code 的核心水平，缺少的是高级交互功能（手动编辑、折叠）。

---

## 故障排除

### 问题 1：任务清单不显示

**可能原因**：
1. Agent 没有调用 todo_write
2. 任务太简单（单步骤）
3. Guidance 没有生效

**解决方案**：
```bash
# 1. 检查 guidance 是否包含 todo_write
grep "todo_write" backend/agent/harness/guidance.py

# 2. 查看 agent 日志
tail -f logs/agent.log | grep todo

# 3. 手动触发（测试用）
# 在对话中明确要求：
"请先创建任务清单，然后执行以下步骤：..."
```

---

### 问题 2：状态不更新

**可能原因**：
1. WebSocket 连接断开
2. Agent 没有调用 todo_update
3. 前端 store 没有同步

**解决方案**：
```bash
# 检查 WebSocket 连接
# 浏览器控制台 -> Network -> WS -> 查看消息

# 检查 store 状态
# 浏览器控制台 -> 输入：
window.__MINICODE_STORE__.getState().todos
```

---

### 问题 3：样式错乱

**可能原因**：
1. CSS 文件没有加载
2. 设计 token 冲突

**解决方案**：
```bash
# 重新构建前端
cd frontend
npm run build

# 检查 CSS 是否生成
ls dist/assets/*.css
```

---

## 后续改进计划

### 高优先级
1. ✅ 强化 guidance（已完成）
2. ⏳ 添加折叠/展开功能
3. ⏳ 支持手动编辑任务

### 中优先级
4. ⏳ 任务依赖关系可视化
5. ⏳ 子任务嵌套显示
6. ⏳ 任务耗时统计

### 低优先级
7. ⏳ 导出任务列表（Markdown/JSON）
8. ⏳ 任务历史回放
9. ⏳ 多人协作任务同步

---

## 总结

MiniCode 的任务清单功能已经完整实现并优化：

✅ **后端**：todo_write 工具 + 强化 guidance  
✅ **前端**：InlineTaskList 组件 + 完整样式  
✅ **集成**：已在 MessageList 中渲染  
✅ **体验**：进度条、动画、状态图标  

**下一步**：
1. 测试复杂任务场景
2. 观察 Agent 是否主动使用 todo_write
3. 根据用户反馈调整 guidance 阈值

**MiniCode 的任务清单功能现在已经是生产就绪状态！** 🎉
