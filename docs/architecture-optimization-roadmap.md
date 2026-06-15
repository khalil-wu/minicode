# MiniCode 架构优化建议清单（对标 Codex/Claude Code）

## 背景
基于以下三个角度的深度分析：
1. MiniCode 当前前后端代码（已全面审计）
2. ClaudeCode-ref 还原源码（1,987 个 TS 文件，发现 7 大隐藏特性）
3. Codex 桌面端交互模式（业界标杆）

---

## 一、前端 UI/UX 优化

### 1.1 当前已完成（本轮 P1/P2）
✅ **斜杠命令瞬时化**：检视类命令（/usage /status /help 等）不再污染 transcript，改为 toast + activity trace  
✅ **大段粘贴转附件**：超 20 行/2000 字符自动转 `pasted-N.txt`，输入框保持干净

### 1.2 待优化：按钮/控件体系（P3 进行中）
**问题**：97 个 onClick 分散在 shell/composer/overlays，部分可能死连或逻辑不完整  
**正在做**：静态审计（Explore agent 后台运行）+ 动态 Playwright 探测 + 修复

**建议增补**（对标 Codex）：
- **Command Palette 增强**：当前 `/` 触发斜杠菜单，Codex 有 Cmd+K 全局指令面板（搜索所有命令/文件/skill），建议补齐
- **右侧面板标签自动激活**：当前打开 diff/plan/inspector 需要手动点标签，Codex/Claude Code 会在产生内容时自动切过去（已有部分实现如 plan_updated → setRightStackTab，需统一）
- **快捷键体系**：当前只有基础快捷键，建议参考 Claude Code CLI 的快捷键表（Ctrl+C 打断、Ctrl+L 清屏、Ctrl+D 退出等），桌面端可映射到 Electron accelerators

### 1.3 待优化：聊天流布局
**现状**：消息投影到 turns（userCell / finalAnswerCell / committedCells），已修复"命令结果跑到上一轮"bug  
**Codex 做法**：
- **工具调用折叠**：默认只显示工具名 + 状态，点击展开完整参数/输出；MiniCode 当前全展开，长输出会撑爆视口
- **Artifacts 内联预览**：图片/代码/diff 渲染在消息流内（已有 LiveArtifacts，可能未全接通）
- **引用溯源**：点击 AI 回复里的文件名 → 跳转到对应代码位置（MiniCode 有 `@file:path#L123` 语法，但渲染侧未必全可点）

**建议**：
- 工具调用默认折叠（inspector.update / tool_call 事件已有，前端渲染时加折叠态）
- 确认 LiveArtifacts 对所有 artifact 类型生效（image/code/diff/mermaid）
- file:line 引用全部可点（当前 chatSurfaceState.ts 有部分解析，需统一）

---

## 二、后端 Agent 架构优化

### 2.1 当前架构总结
**核心设计**（`backend/agent/loop.py`）：
- Single-loop + Recovery-ladder：while-true 循环，model 决定 tool_calls → execute → loop / no tool_calls → done
- 5 阶段：Context Pipeline → Streaming Execution → Recovery Paths → Termination → State Threading
- 策略可插拔：ReflectionPolicy / StreamRetryPolicy / AnswerGate / ErrorWithholding
- 核心优势：**简洁、可审计**，代码清晰度高于 Claude Code 还原版（后者有大量 feature gate 和内部工具残留）

### 2.2 Claude Code 发现的高阶特性（参考价值）
从 ClaudeCode-ref 还原源码中发现的 7 大隐藏特性，部分适合 MiniCode 借鉴：

#### 2.2.1 **KAIROS（永不关机 Agent）** — **高价值**
**功能**：跨会话持久运行、主动模式（Proactive）、自动做梦（Dream）、后台任务  
**MiniCode 现状**：Agent loop 随会话结束而终止，无跨会话状态  
**可借鉴点**：
- **后台任务持久化**：当前 background_manager 存在但无持久化，session 关闭任务丢失；可参考 KAIROS 的任务持久化到 `.claude/tasks/` + PID 锁机制
- **主动模式框架**：添加 `<tick>` 周期性触发，让 agent 自检"有无可做的事"（如定时检查 CI、定期整理记忆）—— 但这是**重量级特性**，需慎重评估用户需求
- **Dream（记忆整合）**：24 小时无活动 + 5+ 新会话时，后台启动子 agent 做记忆去重/聚合 —— 可作为 memory-rag 的高级模式

**建议**：
- **近期**：先做后台任务持久化（`.claude/tasks/<session>/<task-id>.json` 存状态，重启恢复）
- **中期**：添加可选的 proactive 模式（用户需明确开启）
- **远期**：Dream 作为 memory 高级功能（自动触发成本高，可做成手动 `/consolidate` 命令）

#### 2.2.2 **Ultraplan（云端深度规划）** — **不适用**
**功能**：把难题甩给云端 Opus 独立研究 30 分钟，CCR 会话轮询  
**MiniCode 现状**：纯本地/自建后端，无 CCR（Claude Code Remote）基础设施  
**结论**：跳过，MiniCode 是独立部署版，云端 orchestration 不适配

#### 2.2.3 **Coordinator（多 Agent 编排）** — **已有类似**
**功能**：Workflow 编排，多 agent 并行  
**MiniCode 现状**：
- `backend/tasks/coordinator.py` 已实现 pipeline / parallel，与 ClaudeCode Coordinator 同构
- 前端 Workflow tool 已接通（P3a 刚完成 plan_updated 事件管线）

**建议**：
- 当前 coordinator 功能完备，无需大改
- 可补充：**子 agent 断点续跑** —— 子 agent 中途失败时，保存 checkpoint 允许 resume（当前无，每次失败需全量重跑）
- 可补充：**子 agent token 预算分配** —— workflow 总预算如何分给各子 agent（当前无显式分配）

#### 2.2.4 **BUDDY（AI 宠物）** — **娱乐性特性**
电子宠物系统，18 种物种 + 稀有度 + 动画，编译开关 `feature('BUDDY')`  
**结论**：娱乐向，MiniCode 作为生产力工具不适配，跳过

#### 2.2.5 **其他隐藏特性**（Slack 集成、小红书模式、Jupyter 支持）
均为特定场景集成，MiniCode 暂无对应需求，跳过

### 2.3 当前 Agent Loop 可优化的细节点

#### 2.3.1 **断点续跑（Resume from Checkpoint）** — **高优先级**
**问题**：长任务中途失败（超时/网络断/手动打断），需从头重跑  
**方案**：
- 每个工具调用后写 checkpoint（`state.messages` + `state.tool_results` + iteration）到 `.claude/checkpoints/<session>/<task>.json`
- 添加 `/resume` 命令或自动检测未完成 checkpoint，恢复 state 继续
- ClaudeCode-ref 没有显式 checkpoint 系统（它的 KAIROS 持久化是整个会话级），这是 MiniCode 可超越的点

**实现要点**：
- checkpoint 触发时机：每轮迭代后、每次 tool_call 后、遇到 ask_user 时
- 恢复时重建 ToolExecutionContext（workspace_root / terminal_manager / permission）
- 幂等性：部分工具调用可能已执行（如文件已写），恢复时需检测或允许重复

#### 2.3.2 **Reflection Policy 增强** — **中优先级**
**现状**：已有 DefaultReflectionPolicy 和 MultiPerspectiveReflectionPolicy  
**Claude Code 模式**：reflection 触发后，会 append 一个 `<reflection>` 标记到 context，模型自审  
**MiniCode 现状**：reflection 是静默的（append 反思结果到 messages），前端无可见标记

**建议**：
- 前端渲染 reflection turn：显示"🤔 Reflecting…"折叠区，点开看反思内容（类似 thinking）
- 添加 reflection 开关暴露给前端（当前写死在 settings，可做成 `/reflect on|off`）

#### 2.3.3 **Tool Concurrency 优化** — **低优先级**
**现状**：`streaming_executor.py` 实现并发工具调用，有 `is_tool_concurrency_safe` 判断  
**问题**：当前 safe list 较保守（只允许 read-only 工具并发），实际上 `bash` 命令互不干扰也可并发

**建议**：
- 细化并发安全规则：按 `workspace_root` 分组，不同 workspace 的文件操作可并发
- 添加工具级并发声明：tool schema 加 `concurrent_safe: true` 标记

#### 2.3.4 **Error Withholding 策略补充** — **中优先级**
**现状**：`error_withholding.py` 实现错误隐藏机制（连续失败后才暴露给模型）  
**缺失**：ClaudeCode 有 "error budget" —— 每轮最多容忍 N 次错误，超过则强制终止

**建议**：
- 添加 `max_errors_per_turn` 配置（如 5），超过后返回 "Too many errors, stopping" 而非继续循环
- 前端显示错误计数（当前 agentProgress 无）

#### 2.3.5 **Streaming Text Draft 优化** — **低优先级**
**现状**：text draft 每 32 字符触发一次 `text_chunk` 事件（`_TEXT_DRAFT_STREAM_THRESHOLD_CHARS`）  
**问题**：高频事件可能导致前端频繁 re-render

**建议**：
- 改为时间窗口聚合（如 200ms batch）而非固定字符数
- 或前端 debounce（zustand store 的 text append 做节流）

---

## 三、基础设施层

### 3.1 协议同步（Protocol Sync）
**现状**：`scripts/check-protocol-sync.py` 校验前后端类型一致性，只有一个既存 drift（`terminal.resized`）  
**建议**：保持现有机制，继续维护零 drift

### 3.2 测试覆盖
**现状**：
- 后端：agent/plan/verify 共 59 测试，覆盖核心 loop
- 前端：383 测试（+5 本轮新增），覆盖 chat/composer/protocol

**建议**：
- 补充 **端到端集成测试**（Playwright 驱动完整交互流：用户输入 → agent 回复 → 工具调用 → 结果渲染）
- 补充 **长对话压力测试**（模拟 100 轮对话，测 context compaction / memory 正确性）

### 3.3 性能监控
**缺失**：无运行时性能指标（token 用量/耗时/工具调用频次）埋点  
**Claude Code 模式**：GrowthBook + Sentry，远程开关 + 错误上报

**建议**（轻量级）：
- 添加 local metrics 日志：每轮对话的 token breakdown / tool call 耗时 / iteration 数，写到 `.claude/metrics/<date>.jsonl`
- 前端 `/metrics` 命令展示当前会话统计

---

## 四、优先级排序（建议实施顺序）

### P0（立即可做，投入产出比高）
1. **断点续跑（Resume）** — 长任务容错能力核心
2. **工具调用折叠渲染** — 前端视口清爽度
3. **后台任务持久化** — 防止 session 关闭丢任务

### P1（中期规划）
1. **Reflection 可见化** — 前端显示反思过程
2. **Error budget 机制** — 防止错误死循环
3. **Command Palette 增强** — 全局搜索/快捷键

### P2（长期探索）
1. **Proactive 模式（可选）** — 主动 agent，需用户明确场景
2. **Dream（记忆整合）** — memory-rag 高级功能
3. **子 agent token 预算分配** — workflow 精细控制

### P3（暂不推荐）
- Ultraplan（需云端基础设施）
- BUDDY（娱乐向）
- 特定集成（Slack/小红书/Jupyter，按需）

---

## 五、当前会话遗留工作

### 已完成
✅ P1: 斜杠命令瞬时化（toast，不进 transcript）  
✅ P2: 大段粘贴转附件（pasted-N.txt）  
✅ 测试：383/383 全绿，tsc 通过

### 进行中
🔄 P3a: 按钮静态审计（Explore agent 后台运行，等待完成通知）  
🔄 P3b: Playwright 动态探测（脚本已准备，等 P3a 报告）

### 待做
⏳ P3c: 修复发现的坏按钮  
⏳ P4: 全面验证 + 清理

---

## 总结

**MiniCode 当前架构**：核心 agent loop 简洁、可审计，优于 ClaudeCode-ref 的复杂度（后者有大量内部工具残留和 feature gate）。前端组件化清晰，协议同步机制完善。

**关键优化方向**：
1. **容错能力**：断点续跑（P0）、error budget（P1）
2. **UI 清爽度**：工具调用折叠（P0）、command palette（P1）
3. **可观测性**：reflection 可见化（P1）、metrics 埋点（P1）

**不建议照搬的**：Ultraplan（需云端）、BUDDY（娱乐向）、Proactive（重且需求不明确）

---

*本文档基于 MiniCode 当前代码（2026-06-11）+ ClaudeCode-ref 还原源码分析撰写。*
