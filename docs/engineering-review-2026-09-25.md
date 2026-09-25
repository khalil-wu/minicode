# MiniCode 工程化设计审查

日期：2026-09-25。本次按生产入口和真实调用链审查；接手时工作树已有未提交修改，未清理或覆盖。参考源码是本地 `.tmp/codex-src` 与 `cc`；`cc/AGENTS.md` 明确说明它是重建源码，因此只比较职责和可验证机制。

## 调用链和状态归属

| 环节 | MiniCode 生产调用链 | 归属判断 |
| --- | --- | --- |
| 配置与模型 | `config_helpers.load_config_layer_stack` → `config.load_config` → `ModelRuntime` / `model_selection` → `llm_adapter_factory` → 当前 turn 的 `ModelExecutionSnapshot` | 配置解析、模型能力与当步选择分开；显式覆盖仍优先于缺省窗口 |
| 指令、提示词、技能与记忆 | `instruction_discovery` → `ContextBuilder.start_turn` / `_build_prompt_parts` → `SkillManager` / `SkillExecutor`；记忆由显式工具读取 | 项目指令及选中技能必须进入当步模型上下文；读取失败不能伪装为“没有内容” |
| 预算与压缩 | `TurnIterationAdmission` → `prepare_turn_context` → `manage_context_budget` → `ContextBuilder.compact`，之后再发模型请求 | 预算快照应描述真实工具 schema 与摘要续接文字；压缩提交和预算刷新是两个边界 |
| 模型流与工具 | `QueryEngine.submit` → `TurnKernel` → `run_agent_loop` → `provider_stream_runtime` / provider adapter → `StreamingToolExecution` → `ToolBatchRunner` | 终止帧决定响应是否完整；完整工具项先记录再执行；传输故障与内部处理错误分别传播 |
| 发现、MCP、权限和沙箱 | `tool_registry_factory` / `tool_search` → `MCPToolRegistry` → `PermissionChecker` / `ToolExecutionContext` → tool registry / `SandboxRunner` | 广告、授权和执行须使用同一当步工具及权限视图；副作用与进程清理归执行者 |
| 多 agent 与取消 | `TaskTool` / `AgentRuntime` / mailbox → 子任务；执行任务、取消事件与清理收据回到父运行 | 邮件虽不写工作区，仍有顺序和非幂等副作用 |
| 任务调度 | `TaskScheduler` → `scheduled_task_runner` → `run_owned_rest_chat` → `QueryEngine` | 调度器持有任务/运行记录；会话仓库持有模型上下文和转录；两者以会话 ID 连接 |
| 会话、检查点与终态 | `ExecutionJournal` 记录增量事实；`TurnKernel` 保存停止检查点；`prepare_query_recovery` 恢复上下文；`QueryTerminalTransaction` 提交运行终态；`ConversationRepository.commit_turn_projection` 发布会话投影 | 运行终态、模型上下文与 UI 转录各有明确权威来源；投影不能先于权威提交宣告成功 |
| 后端事件与前端 | `EventOutbox` / replay log → `session.restore/sync` → 当前 `stream_resume` → `useWebSocket` 游标校验 → `chatStreamEvents` / turn projection | 旧轮事件只能重放其事实；当前轮的流状态必须由当前 stream slot 决定 |

这条链中，`completed` 是一次运行的终态，不能单独证明用户问题已解决。`terminal_validation` 只排除空最终答复等假完成；任务正确性仍需用户要求的验收条件、测试和外部 oracle。已有真实任务证据见 `docs/harness-audit-2026-09-24.md` 与 `docs/harness-audit-2026-09-25.md`：一个明确契约的结算任务达到外部 10/10，另有大仓库文件任务在 600 秒内 0/5。两种结果都保留，不能从单次成功推出总体能力。

## 本轮确认并修复的根因

| 位置 | 原行为及用户影响 | 修复 |
| --- | --- | --- |
| `instruction_discovery`、`ContextBuilder` | 从子目录执行时，根目录 `AGENTS.md` 的项目内相对导入被当作越界而丢弃；记忆文件损坏和指令读取异常可变成缺失上下文 | 以指令所属项目边界解析导入，读取失败明确上报；保留项目外导入限制 |
| `context_budget`、`SkillExecutor` | 工具 schema 获取失败可能低估输入；技能目录固定说明未计入配额，`active_skills=0` 仍有注入；压缩续接后缀未计入摘要预算 | 让错误向边界传播，并统一按实际注入文本计预算 |
| `mcp_tools` | 服务器目录请求失败被报告成空目录；空字符串资源被误报不存在 | 失败/部分结果显式返回，空资源作为有效内容说明 |
| tool registry、`SendMessageTool`、`SandboxRunner` | 取消路径可能重复等待同一清理；代理消息可进入并行只读批次而重排；交互进程启动失败后资源归属不完整 | 一次清理收据、消息顺序执行、启动失败及取消时清理进程/准备状态 |
| `session.restore/sync`、`chatStreamEvents` | 同一会话旧轮的 `done` 会压掉新轮 `stream_resume`，断线期间的新文本又不在持久 delta 日志内 | 仅由当前流的 `terminal_fenced` 决定重发；空内容的新轮也恢复其活动占位 |
| 手动压缩 WebSocket 路径 | 预算异常被吞；若压缩已提交，后续预算/内存刷新或事件发送失败仍可能被误报为“压缩失败” | 提交前预算失败阻止压缩；提交后保留已提交事实、单独报告刷新错误，事件发送异常原样传播；共享上下文在下一轮查询前从仓库重载，前端清掉旧预算 |
| `TaskScheduler`、`scheduled_task_runner` | 一次性任务触发后不能重试；独立任务重试另建会话；新会话 ID 要等整个运行结束才写入运行记录；助手终态消息与上下文分别提交 | 一次性任务可显式重试，沿用前次会话；模型执行前持久化会话绑定；用 admission revision 原子提交助手消息与上下文 |

修复集中在产生错误的边界，没有为内部值层层加校验、吞异常兜底或新增缓存/重试框架。

## 与参考源码的职责对照

- Codex 的 `core/src/session/step_context.rs` 将当步模型配置、MCP binding 和工具路由固定在一个执行视图；`core/src/session/context_window.rs` 单独管理窗口与压缩阈值。MiniCode 已有配置层、ModelRuntime 和当步快照，但 `ContextBuilder` 仍约 3700 行，同时负责提示词、媒体、预算、压缩、文件恢复和快照。它是目前最明显的内聚性负担；应在有具体变更需求时按状态所有权拆分，不以行数为由整块重写。
- Codex 的 ToolRouter/parallel gate 以工具能力决定并行，rollout recorder 在语义边界持久化；MiniCode 的工具注册、准入、执行日志已分层，本轮修正了消息顺序与取消收据。`TaskTool` 仍聚合子代理准入、模型配置、执行、恢复和投影，是另一个耦合点。`cc` 的重建版 `partitionToolCalls` 和 task framework 只作为职责参照。
- Codex 的 rollout reconstruction 从有效替换历史按序重放；MiniCode 已在原有工作树改动中保留完整当前模型上下文的检查点，并以 run ID、conversation ID、revision 校验恢复。此次增加的调度会话绑定和原子投影补上独立任务的持久化接缝。
- Codex app-server 的事件协议将转录与 UI 通知分开；`cc` 重建版桥接层使用 ID 去重与确认后重放。MiniCode 的 outbox、恢复编排、前端 cursor 分层整体合理；本轮的跨轮错误来自会话级旧终态越权判断当前流。

## 验证和边界

- `compileall backend`、前端 TypeScript 检查、`git diff --check`、协议同步、内核边界与工具名重复检查通过。
- 第一批 13 个相关后端测试文件通过，1 项跳过；第二批 12 个跨模块后端测试文件通过。覆盖调度重启、真实会话仓库往返、checkpoint 恢复、Responses EOF、MCP、沙箱与取消。
- 前端定向回放 47/47、全量 **180 文件 / 2090 项**通过；生产构建通过。桌面端 **56 项单测 + 1 项真实 Electron 启动测试**通过。
- 提交后异常归属的最后修改另经压缩边界 Python 6/6、压缩恢复 5/5、前端投影 45/45 定向验证；此前全量前端结果发生在这次局部修改之前，不冒充修改后重新全量通过。
- 本轮没有重新发起付费真实模型任务，也没有重跑全部后端约 4600 项；已有真实模型成功与失败轨迹见上述两份 harness 报告。现有“所有模型默认 1M”是既定策略，不等于所有 provider 的真实 1M 输入已经核验。
- 硬进程终止发生在工具副作用之后、终态上下文发布之前时，调度器会把遗留 `running` 运行标为失败并保留同一会话供显式重试；不能把它宣称为自动精确续跑或副作用幂等。Windows 低完整性沙箱也不提供真正的网络隔离。MCP 并发回调在归属不明确时仍按现有策略拒绝。
