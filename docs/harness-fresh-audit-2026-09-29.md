# MiniCode harness 独立审计与修复（2026-09-29）

基线：MiniCode `3c55424155bb`；本地公开 Codexsrc 快照 `588b781ab492`。本轮重新启动后端、Vite 和 Electron，沿生产入口阅读控制面与执行面代码，并重新运行任务、抓取事件、执行外部验收。旧审计报告及旧跑分没有作为本页结论的证据。审查覆盖相关生产调用链；没有逐行人工复核仓库中所有 1,307 个后端、前端和桌面文件。

## 架构边界

| 职责 | MiniCode 当前路径 | Codexsrc 对照 | 结论 |
| --- | --- | --- | --- |
| 模型与回合 | `QueryEngine` → `TurnKernel` → `TurnIterationRuntime` → provider adapter | `core/src/session/turn.rs`、`session/step_context.rs` | 控制面明确；每步工具目录与执行策略由同一策略生成。 |
| 工具、审批与多 Agent | `ToolRegistry`、`PermissionChecker`、`ToolBatchRunner`、`TaskTool`、`AgentRuntime` | `core/src/tools/router.rs`、`orchestrator.rs`、`parallel.rs` | 权限、工具路由和子任务属于控制面；子任务写作用域有准入检查。 |
| 执行 | `SandboxRunner` 运行命令；文件工具在后端进程内用工作区路径规则执行；网页工具在后端进程内经网络目标检查访问 | Codexsrc 的环境/执行器与 Windows WFP 实现见 `windows-sandbox-rs/src/wfp.rs` | 命令有独立 OS 执行边界；文件和网络工具仍主要是应用层边界，尚未形成统一的隔离执行面。 |
| 持久化与投影 | `ConversationRepository`、`ExecutionJournal`、检查点、WebSocket outbox → 前端事件归约 | Codexsrc rollout 与 app-server 事件投影 | 会话历史和模型上下文分别持久化，再投影到 UI。运行 `completed` 不代表用户验收通过。 |

## 一、可用

- `ConversationRepository.create_conversation`、`commit_turn_projection`、`update_goal` 保存会话、目标、转录和上下文；`ContextBuilder.start_turn` 注入项目规则并管理长历史；`TurnKernel` 保存停止检查点。独立代码任务中，Agent 明确读取了工作区 `AGENTS.md`，随后搜索、读文件、修改、运行测试并检查 diff。
- 本地 `gpt-6-luna` 通过真实前端 WebSocket 完成“请只回复 OK”并实时投影最终文本和用量。独立的多 Agent 代码任务产生两个真实 `subagent.start/done`，主 Agent 修改 `endpoint.py`，校正后的工作区外 oracle 通过；基线 oracle 失败。两个子 Agent 在这次轨迹中**顺序**执行，实际峰值并行为 1。
- 无桌面终端管理器的 headless 任务曾向模型展示 `read_terminal`，模型调用后只能收到“无 terminal manager”。现已在当步工具策略中隐藏它；同一策略同时约束模型目录和执行。带终端管理器的桌面会话仍可使用。

## 二、可靠

- 首轮新会话在模型未输出文本或工具就失败时，原前端没有采用服务端生成的会话 ID。后端已持久化 `error`、`agent.run.completed` 和 `done`，页面却一直显示“正在处理”，停止命令也没有可用的会话围栏。现在 `agent.run.started` 先把同 ID 的乐观助手消息绑定到服务端会话，再进入旧轮事件栅栏。修改后用本地 402 provider 重跑：失败约 1 秒内实时显示，无需刷新，停止按钮退出。
- 新抓包得到 81 条实时 wire 事件、29 条重放事件；长文本的单条 `item.completed` 在重放窗口内被截短，但恢复快照包含完整 26,421 字符。用这次抓包驱动前端真实 WebSocket hook，在线和重连两条路径均通过。
- 多 Agent 评测器曾把 `task` 的代码单元调用 ID 当作子 Agent ID，误报峰值并行为 2。现在优先用公开子 Agent 生命周期事件，其次运行时记录，最后才用工具调用近似值，并在摘要中标明来源。对本轮原始轨迹重新计数为两个子 Agent、峰值并行 1。
- 调度器历史样例的 oracle 与现行“只有 Agent 创建的循环计划才自动过期”契约冲突，导致参考代码也失败。已修正样例和任务文本；历史缺陷基线仍有 6 个失败，当前参考 4/4 通过。
- **Windows 网络边界仍不完整。** 当前主机的低完整性沙箱报告文件隔离为真、网络隔离为假；本轮实际在受限命令里用原始 socket 连通本机服务。修复后，权限检查器对这类命令强制逐项审批，已有 AUTO 覆盖和全局允许规则不能绕过；审批卡明确警告直接网络连接，并隐藏这类命令的批量/永久允许入口。用户批准后命令仍可联网，这不是 OS 网络隔离。要实现严格无网络执行，需要容器或 Windows WFP/AppContainer 类执行后端。

## 三、产品化

- 检查了空态、成功答复、首轮失败、模型菜单、输入区、窄屏与宽屏。390px 和 1280px 视口没有文档横向溢出。`completed` 只表示回合结束，因此桌面通知改为“回复已就绪”，不再宣称“任务已完成”。
- 真实 Electron 多 Agent 测试通过，覆盖子任务转录、取消、延迟结果及重启恢复。另有审批卡、聚合 diff 和移动端输入区 3 项 Playwright 流程通过。浏览器预览模式不能验证原生目录选择器；原生桌面能力由 Electron 用例覆盖。

## 真实任务结果与验证

| 任务 | 运行终态 | 独立验收 | 判断 |
| --- | --- | --- | --- |
| 文件读取快照修复，`gpt-6-luna` | `completed`，约 575 秒，85 轮、182 次工具调用 | 2/5，失败 | 模型补丁仍有换行、超大 PDF 和 Unicode 分隔符错误；不能记作 harness 成功。轨迹里还暴露了已修复的 `read_terminal` 目录错误。 |
| 隔离的 endpoint 修复，要求两个只读子 Agent | `completed`，两个子 Agent 启停 | 校正外部 oracle 通过，缺陷基线失败 | 完整代码闭环成立；本次并未实现请求中的并行启动。初次 oracle 脚本因工作区外导入路径错误误报失败，修正脚本后对同一产物重新验收，没有重跑模型。 |
| 调度器历史缺陷样例 | 模型前验收 | 当前参考 4/4；历史缺陷基线 6 个失败 | 样例再次可用于后续公平评测，本轮没有据此跑新模型。 |

验证：关联后端与沙箱/权限/恢复用例通过；前端关联 Vitest 通过；TypeScript、Python compileall、生产 Vite 构建、协议同步、Agent 内核边界和 `git diff --check` 通过；Electron 多 Agent 1/1、产品交互 Playwright 3/3、最新抓包回放 2/2 通过。未重新运行全仓数千项 Python 测试，也未把局部验证表述为全量通过。

本轮临时数据和轨迹保存在 `.tmp/harness-audit-*20260929/`。本页不记录 provider 密钥。
