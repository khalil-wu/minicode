# MiniCode 全面测试与优化方案

## 🎯 测试目标

确保 MiniCode 的智能性、UI 美观性和性能与 Claude Code 桌面端相当或更好。

---

## 📋 测试维度

### 1. Agent 智能性测试

#### 1.1 基础对话测试
- [ ] 简单问答（"你是谁"）
- [ ] 代码解释（解释一段代码）
- [ ] 技术咨询（"如何实现 xxx"）

#### 1.2 任务管理测试
- [ ] 单步任务（修改一个文件）
- [ ] 多步任务（创建 5 个文件）
- [ ] 复杂任务（重构一个模块）
- [ ] 任务中断和恢复

#### 1.3 工具使用测试
- [ ] 文件读取（Read）
- [ ] 文件写入（Write）
- [ ] 文件编辑（Edit）
- [ ] 命令执行（Bash/PowerShell）
- [ ] 搜索（Grep, Glob）
- [ ] MCP 工具调用

### 2. UI/UX 测试

#### 2.1 视觉设计
- [ ] 色彩对比度
- [ ] 字体层级
- [ ] 间距和对齐
- [ ] 图标一致性
- [ ] 动画流畅度

#### 2.2 交互体验
- [ ] 响应速度（< 100ms）
- [ ] 加载状态
- [ ] 错误提示
- [ ] 成功反馈
- [ ] 键盘快捷键

#### 2.3 响应式设计
- [ ] 桌面（> 1024px）
- [ ] 平板（768-1024px）
- [ ] 手机（< 768px）
- [ ] 窗口调整

### 3. 性能测试

#### 3.1 前端性能
- [ ] 首屏加载时间（< 2s）
- [ ] 交互响应时间（< 100ms）
- [ ] 滚动帧率（60 FPS）
- [ ] 内存占用（< 200MB）
- [ ] 动画性能

#### 3.2 后端性能
- [ ] API 响应时间（< 500ms）
- [ ] WebSocket 延迟（< 50ms）
- [ ] 并发处理能力
- [ ] 内存泄漏检测

### 4. 功能完整性测试

#### 4.1 核心功能
- [ ] 对话历史
- [ ] 文件操作
- [ ] 代码高亮
- [ ] Diff 显示
- [ ] 搜索和替换

#### 4.2 高级功能
- [ ] MCP 集成
- [ ] Skills 调用
- [ ] RAG 检索
- [ ] 多 Agent 协作
- [ ] 任务管理

---

## 🧪 自动化测试脚本

### 测试 1: Agent 任务管理能力

**测试目标：** 验证 Agent 能否正确创建和管理任务

**测试用例：**
```python
def test_agent_task_management():
    """测试 Agent 任务管理"""
    
    # 发送多步骤任务
    message = """
    帮我创建一个简单的 TODO 应用，包括：
    1. 创建 index.html 文件
    2. 创建 styles.css 文件
    3. 创建 app.js 文件
    4. 测试应用功能
    """
    
    # 预期行为
    expected = {
        "should_create_task_list": True,
        "task_count": 4,
        "should_show_progress": True,
        "should_update_status": True,
        "should_show_completion": True,
    }
    
    # 检查点
    # 1. 是否调用了 todo_write
    # 2. 是否发送了 task.update 事件
    # 3. 前端是否显示任务列表
    # 4. 进度条是否更新
    # 5. 完成时是否显示庆祝
```

### 测试 2: UI 响应性能

**测试目标：** 验证 UI 交互响应时间 < 100ms

**测试用例：**
```python
def test_ui_responsiveness():
    """测试 UI 响应性能"""
    
    interactions = [
        "click_send_button",
        "type_in_input",
        "scroll_messages",
        "open_modal",
        "close_modal",
    ]
    
    for interaction in interactions:
        start = time.time()
        # 执行交互
        perform_interaction(interaction)
        end = time.time()
        
        response_time = (end - start) * 1000  # ms
        assert response_time < 100, f"{interaction} took {response_time}ms"
```

### 测试 3: WebSocket 实时性

**测试目标：** 验证 WebSocket 事件延迟 < 50ms

**测试用例：**
```python
def test_websocket_latency():
    """测试 WebSocket 延迟"""
    
    # 后端发送事件
    event = {
        "type": "task.update",
        "data": {"todo_id": "1", "status": "in_progress"}
    }
    
    start = time.time()
    send_websocket_event(event)
    
    # 等待前端接收
    wait_for_frontend_update()
    end = time.time()
    
    latency = (end - start) * 1000  # ms
    assert latency < 50, f"WebSocket latency: {latency}ms"
```

### 测试 4: 动画性能

**测试目标：** 验证动画保持 60 FPS

**测试用例：**
```python
def test_animation_fps():
    """测试动画帧率"""
    
    animations = [
        "task_completion_checkmark",
        "progress_bar_fill",
        "modal_fade_in",
        "toast_slide_in",
    ]
    
    for animation in animations:
        fps = measure_animation_fps(animation)
        assert fps >= 55, f"{animation} FPS: {fps} (expected ≥ 55)"
```

---

## 🔧 发现的问题和优化建议

### 问题 1: Agent 系统提示可能不够明确

**现状：**
```python
# guidance.py
"- For multi-step work (≥3 steps, several files, several subtasks..."
```

**优化建议：**
- 添加更多具体示例
- 明确任务粒度（每个任务 2-10 分钟）
- 强调只有一个任务为 in_progress

**优化后：**
```python
"""
Planning contract:
- For multi-step work (≥3 steps), call todo_write FIRST to create a task checklist.
- Example 1: "Create a React app" → tasks: ["Setup project", "Create components", "Add styling", "Test app"]
- Example 2: "Fix authentication bug" → tasks: ["Reproduce bug", "Identify root cause", "Implement fix", "Test fix"]
- Each task should be 2-10 minutes of focused work.
- ONLY ONE task should be in_progress at a time. Mark completed before starting next.
- Update status immediately after each task: pending → in_progress → completed.
"""
```

### 问题 2: 前端可能缺少加载状态

**现状：**
- 发送消息后没有即时反馈
- Agent 思考时没有加载指示器

**优化建议：**
```tsx
// ChatInput.tsx
function ChatInput() {
  const isStreaming = useAppStore(s => s.isStreaming);
  
  return (
    <>
      <textarea disabled={isStreaming} />
      {isStreaming && (
        <div className="thinking-indicator">
          <div className="pulse-dot" />
          <span>Agent is thinking...</span>
        </div>
      )}
    </>
  );
}
```

### 问题 3: 任务列表可能在某些情况下不显示

**可能原因：**
- WebSocket 事件未正确发送
- 前端状态更新失败
- 事件监听器未注册

**调试步骤：**
1. 检查后端日志：`[TODO] Emitted ... events`
2. 检查浏览器 Console：`[RuntimeEvents] Received task.update`
3. 检查 Zustand store：`todos` 数组

### 问题 4: 滚动性能可能不佳

**优化建议：**
```css
/* 使用 will-change 优化滚动 */
.message-list {
  will-change: scroll-position;
  contain: layout style paint;
}

/* 使用 transform 替代 top/left */
.scroll-to-bottom-button {
  transform: translateY(0);
  transition: transform 200ms ease;
}

.scroll-to-bottom-button.hidden {
  transform: translateY(100px);
}
```

---

## 📊 性能基准

### Claude Code 桌面端基准

| 指标 | Claude Code | MiniCode 目标 |
|------|-------------|---------------|
| 首屏加载 | < 1s | < 2s |
| 交互响应 | < 50ms | < 100ms |
| WebSocket 延迟 | < 30ms | < 50ms |
| 滚动 FPS | 60 | 60 |
| 内存占用 | < 150MB | < 200MB |
| 动画 FPS | 60 | 60 |

---

## 🚀 优化优先级

### P0（必须修复）

1. **Agent 任务创建可靠性**
   - 确保多步骤任务总是创建任务列表
   - 修复 WebSocket 事件发送

2. **UI 响应性**
   - 交互响应时间 < 100ms
   - 添加加载状态

3. **核心功能稳定性**
   - 对话不丢失
   - 文件操作成功率 > 99%

### P1（应该优化）

1. **性能优化**
   - 首屏加载 < 2s
   - 滚动 60 FPS

2. **视觉精致度**
   - 动画流畅
   - 间距一致
   - 色彩和谐

3. **错误处理**
   - 友好的错误提示
   - 自动重试机制

### P2（可以增强）

1. **高级功能**
   - 任务依赖
   - 子任务
   - 时间跟踪

2. **可访问性**
   - 键盘导航完善
   - 屏幕阅读器支持

---

## 🧪 实际测试计划

### Phase 1: 手动测试（今天）

1. **启动服务**
   ```bash
   # 后端
   cd C:/Desktop/MiniCode
   python -m backend.main
   
   # 前端（另一个终端）
   cd frontend
   npm run dev
   ```

2. **测试 Agent 任务管理**
   - 发送："帮我创建一个包含 3 个文件的项目"
   - 观察是否出现任务列表
   - 检查进度条是否更新
   - 验证完成动画

3. **测试 UI 响应**
   - 点击按钮，计时响应
   - 滚动消息列表，观察帧率
   - 打开/关闭模态框，检查动画

4. **测试性能**
   - Chrome DevTools Performance 录制
   - 检查 FPS、内存、网络

### Phase 2: 自动化测试（明天）

1. **安装 Playwright 浏览器**
   ```bash
   python -m playwright install
   ```

2. **编写测试用例**
   - `tests/e2e/test_agent_tasks.py`
   - `tests/e2e/test_ui_performance.py`
   - `tests/e2e/test_websocket.py`

3. **运行测试**
   ```bash
   pytest tests/e2e/ -v
   ```

### Phase 3: 性能优化（本周）

1. **前端优化**
   - 代码分割
   - 懒加载
   - 虚拟滚动

2. **后端优化**
   - 缓存策略
   - 并发处理
   - 内存优化

---

## 📝 测试报告模板

```markdown
# MiniCode 测试报告

## 测试环境
- OS: Windows 11
- Browser: Chrome 131
- Python: 3.13.9
- Node: 20.x

## 测试结果

### Agent 智能性
- [ ] ✅/❌ 任务创建
- [ ] ✅/❌ 任务管理
- [ ] ✅/❌ 工具使用

### UI/UX
- [ ] ✅/❌ 视觉设计
- [ ] ✅/❌ 交互体验
- [ ] ✅/❌ 响应式

### 性能
- [ ] ✅/❌ 加载速度
- [ ] ✅/❌ 响应时间
- [ ] ✅/❌ 动画流畅度

## 发现的问题
1. ...
2. ...

## 优化建议
1. ...
2. ...
```

---

准备好了吗？让我开始实际测试！🚀
