# 2026-10-18 真实 UI 与 agent 链路验收

本批从独立 Electron 窗口的实际模型任务中修正了三个执行问题，并保留供应商原始错误诊断。没有改变字体、字号、字重或新增前端控件。

## 基线与范围

- Codex 参考源码：`822e58cc3d666166c7446c5b1ea2e52f5d09594c`；实际安装 Appx：`26.1013.4894.0`。本轮读取了当前原生窗口，并从当前 `app.asar` 提取对应 UI 资源。参考截图和资源在 `output/codex-parallel-ui-agent-20261018/reference/`。
- MiniCode：共享分支基线 `620fc020`；最终实际 UI 使用主会话构建 `assets/index-BFib2CFp.js`。本任务未重建或修改 `frontend/dist`。
- 模型主链固定为 `gpt-6.1-sol`、`high`、Responses。只从授权临时文件读取凭据到独立 QA 进程环境；不改正式模型设置，不记录密钥。
- 避开主会话持有的 Connections、远端 host、目录 picker 文件；未修改、删除、暂存原有 `data/`，未 stage/commit/push。

## 根因与修改

1. **Windows Node MCP 无法启动。** 嵌入服务器源码经多层命令行引用后超出 Windows CreateProcess 限制，真实桌面日志反复出现 `WinError 206`。现在压缩传输同一模块，由同一执行器进程解压加载，保留 MCP stdin、沙箱和线程生命周期；模块使用固定 sourceURL，错误栈不展开整个 data URI。
2. **真实 MCP 工具发现后 HTTP 400。** provider checkpoint 保留了实际搜索结果，但 Responses serializer 漏发 `status="completed"`、`execution="client"`。实际错误为 `No tool output found for tool search call ...`。按照 Codex `protocol/src/models.rs::ToolSearchOutput` 和 `core/tests/suite/direct_tool_metadata.rs` 补齐字段。没有合成结果、禁用发现、换模型或自动降级。
3. **Node 空闲后丢失上下文。** 真实压缩后的跟进出现 `Cannot find context with specified id`。Worker 初始化局部变量没有为其整个生命周期持有 `vm.Context`，Inspector ID 也不保持强引用。现在由 Worker 保持 VM；原有变量、模块和内核继续存在。强制 GC 回归在修复前稳定失败，在修复后通过；没有自动重置或重试策略。
4. **错误证据丢失。** `provider_error_details` 现在把 provider 边界已经处理过的 `provider_error_message` 保留到现有 runtime/Inspector 证据中，避免只剩泛化错误。

## 真实任务证据

| 场景 | 实际结果 | 原始证据 |
| --- | --- | --- |
| 两个并行只读子 agent、独立事实读取 | 实际启动、读文件和完成；两者均继承指定模型与 high。父邮箱发送未发生，message_list 为空，不计为通过 | 根目录 `wire-events.json`、`real-multiagent-summary.json` |
| CodeMode、真实 shell、故意 exit 7、父代理写文件并校验 | 成功输出与真实退出码 7 均保留；获得批准后写入并读回 | 根目录截图 `01`–`04`、`approval-1`、`final-state.json` |
| 最终过程折叠和子记录回放 | 完整最终答案后默认折叠；展开可查看；子记录没有主用户气泡和输入框 | 根目录 `02-real-final`、`03-process-expanded`、`04-child-transcript` |
| Skill、外部 MCP、Node 持久变量、reset、文件校验 | 实际 nonce、Node 文件事实、数字 42、reset 后 undefined 均得到并读回 | `final-real/` 和 `features-wire-fixed/` 的事件、provider 请求、截图与实际 nonce 服务调用日志路径（见 `real-run.json`） |
| 手动压缩与草稿 | 实际压缩完成，原对话仍可查看，压缩期间新草稿保留 | `features-wire-fixed/07`–`09`、`13`；`final-real/21`–`22` |
| 原生工具审批 | 审批替换输入区；实际待批时可见 textarea 数量为 0；处理后恢复 | `features-wire-fixed/15`–`18`；`final-real/approval-*` |
| 当前菜单与设置 | 实测统一模型/强度入口、模型列表、常规/外观设置与返回后草稿 | `features-wire-fixed/05`–`06`、`10`–`13` |

最终同一 Node 内核跨真实压缩后，执行的原始代码仅为 `nodeRepl.write(qaRetainedAfterCompact)`，返回原值 `birch-otter-582637`。中间没有重置、重新声明或读文件恢复。`final-real/19`–`24`、`verdicts.json` 与 provider 请求保存了完整证据。

追加文件审批也已完成：审批时可见 textarea 为 0，提交时按钮实际 disabled，批准后草稿、附件 ID 和 artifact ID 完全一致；图在 `final-real/25`–`28`，前后状态为 `draft-before-approval.json` / `draft-after-approval.json`。`29`–`30` 记录 hover/键盘 focus；`31`–`32` 和 `disclosure-motion.json` 记录真实 300ms 展开动画；`33`–`39` 记录侧栏拖动、右侧菜单和实际 turn diff 独立展开收起。

## 验证与边界

- 最终后端相关批次：88 项中 **87 passed、1 skipped、0 failed/error**。日志/XML：`backend-final.*`。
- 前端过程、子记录、审批、输入 admission：5 文件 **77 passed**。日志：`frontend-invariants.log`。
- 跳过项是真实 Windows unelevated restricted-token 无法直接强制 denied-read 策略；不是已通过，也没有放宽策略。
- 原 restricted-token 写文件测试使用 Python `TemporaryDirectory` 的 Windows 私有 0700 DACL，正常权限检查因此拒绝。相同真实内核在独立、继承正常项目权限的目录可写，工作区外仍被拒绝；仅修正测试目录准备。原拒绝与正常目录结果均保存在 `restricted-node-*.log`。
- 历史失败保留：`features/` 是漏配 MCP transport 的 QA 准备失败；`features-audit/` 是真实 provider wire 失败；`features-wire-fixed/` 的压缩后内核错误属于修复前真实失败，模型自行请求 reset 后的恢复不冒充原内核成功。
- 当前原生 Codex 仅做只读观察；未操作普通用户窗口。本轮截图覆盖所列实际状态，不宣称覆盖所有菜单、hover、focus、动画或未公开产品能力。
- 官方账号服务、未公开 Node executor 实现与平台未支持策略没有被测试数替代为等价结论。本批没有启动完整 CI、打包或发布；集成构建由主会话统一进行。

## 续验结果与当前集成门槛

本节覆盖早期 BFib2CFp 构建后的续验；完整七区域清单见 `output/codex-parallel-ui-agent-20261018/REGION-CHECKLIST.md`。最新已检查集成构建为 `assets/index-CxYfWP2g.js`（index SHA256 `b43befb614262988ebe1cf988619c5dc52c9547bc731767be44347a3521ad931`）。

追加实际根因修复：桌面显式预览启动采用 Codex app-server `process/spawn` 的用户进程权限，agent 预览工具继续使用捕获的 agent 策略；浏览器同 URL 打开复用实际原生页面，重启完成后刷新原页面；新后台子 agent 默认保留消息协调能力，既有持久化显式拒绝和父级限制仍保留。后端预览相关批次 36 passed，后台消息相关批次 133 passed。

邮箱结论已纠正：第一次双子 agent 任务没有发送邮箱消息，该子要求仍为未通过。后续独立的真实消息任务中，新后台子 agent 确实执行 send_message，父 agent 的 message_list 确实收到 `REGION_MAILBOX_PROOF_94713`，证据为 `regions-agent-messaging-fixed/mailbox-verdict.json`。不能将第一次任务的子 agent 返回事实当作邮箱证明。

新增实际忙时输入验证：普通发送进入排队，Ctrl+Enter 引导当前任务；首个实际最终回答含 `REGION_BUSY_BASE_DONE REGION_STEER_INPUT_SEEN`，后续排队回答为 `REGION_QUEUE_FOLLOWUP_DONE`。浏览器批注添加到了原草稿；diff 评论、完整文件打开、真实 TS 补全/outline/find、终端双会话及 24→27 行调整均有实际证据。QA 编辑器未保存修改在模式切换中保留，随后有意恢复原文且未保存。

设置完成 13 个非 Connections 分类及高级折叠区清点，31 个下拉菜单打开/hover/focus/Escape；最新集成构建另记录 343 次按钮状态观察（含共享按钮重复，8 次 disabled），没有把 hover/focus 当作全部功能通过。安装、清理、安全配置、未接入硬件/服务未执行。Git/个性化仍有来源可见差异，未宣称全量等价；官方账号服务不计为阻塞。

用户自行处理原生本地网络提示后，实际页面加载成功，预览服务重启更换 PID 并保持同一原生 target；但实际页面没有刷新。原始失败在 `regions-integrated/restart-before-fix.json`，手动刷新成功对照在 `manual-reload-control.json`。根因是 BrowserPanel 将 Windows 原始工作区路径与规范化路径直接比较，丢弃重启刷新请求；现改用已有 workspaceRootsEqual 并添加直接 Windows 路径回归。最新四文件前端批次 **132 passed**，日志 `preview-browser-final-windows-path.log`。

该最终路径修复晚于 CxYfWP2g 构建，仍需主会话重新集成后核验真实 PID 更换、单一原生 target 和页面 load/timeOrigin 更新。当前源码稳定，未再增加 production 修改计划；未重建 dist、未暂存/提交/推送、未运行完整 CI。未自动批准原生权限、未加入 localhost 全局放行，也未改变正式凭据和原有 data/。

## 最终构建实际门槛通过

主会话新构建 `assets/index-rC2EnfFC.js` 的 index SHA256 实际核对为 `caf6a30f75c8099b66f3f5fa15c137447b695a998ed160c56f5f191380981290`。仅重载隔离 QA 渲染器后，实际 UI 重启将 PID `6288` 更换为 `9060`，旧进程树 4 项全部退出；原生页面仍为 `browser_mvdeh83c_xcq22v`，仅 1 个浏览器标签。实际记录了 1 次 Document 请求、1 次 Page.loadEventFired，timeOrigin 从 `1792303296958.1` 更新为 `1792305128237.4`，HTTP 检测返回 200；原生全窗口视觉复核也显示实际预览内容和原草稿。

重载及重启后的草稿文字、两个附件的 ID/artifact ID、选中引用完全一致，当前无未保存编辑器 buffer。随后仅通过自有预览 Stop UI 和现有 QA finish.flag 正常关闭；新预览进程树 4 项及 QA 应用/运行器 16 项全部退出，QA profile/草稿保留。证据在 `output/codex-parallel-ui-agent-20261018/regions-release/` 的 verdict、wire-events、进程树/cleanup、截图及 ARIA 文件。

因此本任务唯一待验的最终集成门槛现已完成；七区域清单中的其他明确未验证/排除项仍如实保留。共享私密配置留给主会话在两个会话均结束后清理，路径清单为 `PRIVATE-CONFIG-CLEANUP.json`；本任务未提交、推送、重建 dist 或运行完整 CI，relay/Proactor 和完整 CI 仍由主会话负责。
