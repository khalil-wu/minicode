# 任务显示调试 - 测试指南

## 🧪 测试准备

### 1. 确保服务运行

**前端服务：**
```bash
# 应该已经在运行（http://localhost:5173）
cd C:/Desktop/MiniCode/frontend
npm run dev
```

**后端服务：**
```bash
cd C:/Desktop/MiniCode
python -m backend.main
```

**桌面端：**
```bash
cd C:/Desktop/MiniCode
cmd /c start-dev.cmd
```

### 2. 打开调试工具

1. **浏览器 DevTools（F12）**
   - 打开 Console 标签
   - 清空现有日志（垃圾桶图标）
   - 勾选 "Preserve log"（保留日志）

2. **后端日志**
   - 打开运行后端的终端
   - 准备查看输出

---

## 🎯 测试场景

### 测试 1：简单的多步骤任务

**发送消息：**
```
帮我创建一个简单的 TODO 应用，包括：
1. 创建 HTML 文件
2. 添加 CSS 样式
3. 编写 JavaScript 逻辑
```

**预期行为：**
- ✅ Agent 应该调用 `todo_write` 创建 3 个任务
- ✅ 后端日志应该显示：
  ```
  [TODO] todo_write called with 3 items
  [TODO] Session <id>: 3 tasks saved
  [TODO]   #1: 创建 HTML 文件 [in_progress]
  [TODO]   #2: 添加 CSS 样式 [pending]
  [TODO]   #3: 编写 JavaScript 逻辑 [pending]
  ```
- ✅ 浏览器 Console 应该显示：
  ```
  [RuntimeEvents] Received task.update event: ...
  [RuntimeEvents] Creating new todo: 1
  [RuntimeEvents] Current todos after update: [...]
  [InlineTaskList] Todos updated: 3 [...]
  ```
- ✅ 对话中应该显示任务列表
- ✅ 侧栏任务面板应该显示 3 个任务

---

### 测试 2：复杂的项目创建

**发送消息：**
```
帮我搭建一个 React + TypeScript 项目，需要：
- 配置 package.json
- 设置 tsconfig.json
- 创建 src 目录结构
- 实现 App 组件
- 添加路由
- 编写测试
```

**预期行为：**
- ✅ Agent 创建 6 个任务
- ✅ 每完成一个任务，状态更新为 `completed`
- ✅ 下一个任务状态更新为 `in_progress`
- ✅ 可以看到任务进度（如 2/6 completed）

---

### 测试 3：单步简单任务（不应该创建任务）

**发送消息：**
```
帮我修改 README.md，添加一行 "Hello World"
```

**预期行为：**
- ❌ Agent **不应该**调用 `todo_write`（因为只有 1 步）
- ✅ 后端日志**没有** `[TODO]` 输出
- ✅ 浏览器 Console **没有** `[RuntimeEvents] task.update` 输出
- ✅ 对话中**不显示**任务列表

---

## 🔍 诊断结果分析

### 场景 A：后端没有 [TODO] 日志

**症状：**
```
# 后端日志中完全没有
[TODO] todo_write called with ...
```

**诊断：** Agent 没有调用 `todo_write` 工具

**原因：**
- Agent 认为任务太简单，不需要任务列表
- 系统提示不够明确

**解决方案：**
→ 执行 `TASK_DISPLAY_DIAGNOSIS.md` 中的 **修复方案 A**
→ 增强系统提示，明确指导何时使用 `todo_write`

---

### 场景 B：后端有日志，前端没有

**症状：**
```
# 后端日志：
[TODO] Session xxx: 3 tasks saved
[TODO]   #1: Task 1 [in_progress]
...

# 浏览器 Console：
（空，没有任何 [RuntimeEvents] 或 [InlineTaskList] 日志）
```

**诊断：** WebSocket 事件没有发送或前端没有接收

**原因：**
- `todo_write` 工具执行后没有触发 WebSocket 事件
- WebSocket 连接断开
- 事件被过滤掉

**解决方案：**
→ 执行 `TASK_DISPLAY_DIAGNOSIS.md` 中的 **修复方案 B**
→ 在工具执行后添加事件发送逻辑

---

### 场景 C：前端接收事件，但任务立即消失

**症状：**
```
# 浏览器 Console：
[RuntimeEvents] Creating new todo: 1
[RuntimeEvents] Current todos after update: [{id: "1", ...}, {id: "2", ...}, {id: "3", ...}]
[InlineTaskList] Todos updated: 3 [...]

# 几秒后：
[InlineTaskList] Todos updated: 0 []
```

**诊断：** 任务创建后立即被清空

**原因：**
- Agent 可能在创建任务后立即全部标记为 `completed`
- 后端逻辑检测到所有任务完成，触发清空

**解决方案：**
→ 执行 `TASK_DISPLAY_DIAGNOSIS.md` 中的 **修复方案 C**
→ 延迟清空策略，保留已完成任务 30 秒

---

### 场景 D：前端有数据，但界面不显示

**症状：**
```
# 浏览器 Console：
[InlineTaskList] Todos updated: 3 [{...}, {...}, {...}]
[InlineTaskList] Active tasks: [...]

# 但对话中没有任何任务框显示
```

**诊断：** UI 渲染问题

**原因：**
- React 条件渲染逻辑错误
- CSS 隐藏了任务列表
- 组件没有正确挂载

**解决方案：**
→ 执行 `TASK_DISPLAY_DIAGNOSIS.md` 中的 **修复方案 D**
→ 检查 `InlineTaskList` 组件的渲染条件
→ 检查 CSS 是否隐藏了元素

---

## 📊 验证清单

完成测试后，勾选以下项目：

### 后端验证
- [ ] 多步骤任务触发 `[TODO]` 日志
- [ ] 日志显示正确的任务数量
- [ ] 日志显示每个任务的 ID、内容和状态
- [ ] 单步任务**不触发** `[TODO]` 日志

### 前端验证
- [ ] 浏览器 Console 显示 `[RuntimeEvents] task.update` 事件
- [ ] Console 显示 `[InlineTaskList]` 日志
- [ ] 对话中显示任务列表框
- [ ] 任务有正确的图标（○ ● ✓）
- [ ] 任务状态实时更新

### 侧栏验证
- [ ] 侧栏可以打开
- [ ] 有 "Tasks" 标签页
- [ ] 点击后显示任务列表
- [ ] 任务与对话中一致

### 交互验证
- [ ] Agent 开始任务时，图标变为 ●
- [ ] Agent 完成任务时，图标变为 ✓
- [ ] 所有任务完成后，列表清空（或保留 30 秒）
- [ ] 可以看到进度（如 2/5 completed）

---

## 🐛 常见问题

### 问题 1：看不到后端日志

**解决：**
```bash
# 确保日志级别正确
export LOG_LEVEL=INFO

# 或在代码中设置
import logging
logging.basicConfig(level=logging.INFO)
```

### 问题 2：浏览器 Console 没有日志

**解决：**
- 确保 DevTools 是打开的
- 清空日志并重新测试
- 检查是否勾选了 "Preserve log"
- 尝试刷新页面

### 问题 3：WebSocket 连接断开

**解决：**
```bash
# 检查 WebSocket 连接状态
# 在浏览器 Console 中
console.log('WS connected:', useAppStore.getState().wsConnected);

# 如果断开，刷新页面或重启服务
```

### 问题 4：Agent 不创建任务

**可能原因：**
- 任务确实太简单（1-2 步）→ 正常行为
- 系统提示不够明确 → 需要修复方案 A
- Agent 模型理解有偏差 → 可能需要调整提示

---

## 📝 报告格式

完成测试后，请提供以下信息：

```
## 测试结果

**测试场景：** [简单多步骤任务 / 复杂项目创建 / 单步任务]

**发送的消息：**
```
[你发送的消息]
```

**后端日志：**
```
[复制后端日志中的 [TODO] 相关输出]
```

**浏览器 Console：**
```
[复制浏览器 Console 中的 [RuntimeEvents] 和 [InlineTaskList] 日志]
```

**观察到的行为：**
- [ ] 对话中显示任务列表
- [ ] 侧栏显示任务
- [ ] 任务状态实时更新
- [ ] 其他...

**诊断结果：**
[场景 A / B / C / D / 正常工作]

**截图：**
[如果可能，提供截图]
```

---

## 🚀 下一步

根据诊断结果：

1. **如果正常工作** → 进入 Phase 4（视觉增强）
2. **如果场景 A** → 修复系统提示
3. **如果场景 B** → 添加 WebSocket 事件发送
4. **如果场景 C** → 实现延迟清空
5. **如果场景 D** → 调试 UI 渲染

---

**准备好了吗？** 开始测试！🧪
