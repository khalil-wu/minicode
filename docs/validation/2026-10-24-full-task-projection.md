# 2026-10-24 完整真实任务投影验收

本次使用用户已配置的真实 provider，运行独立 Python 修复任务，并通过真实 Electron 页面核对 harness → durable events → frontend。原始完整 E2E **未全通过**：任务本身完成，发现测试终态字段错误，以及真实 child → root 消息在展开记录中被投影为空的问题。后者已修复，保留 profile 的无付费功能恢复和最终可见窗口恢复均通过，原生 PNG 已人工核对。原始失败与两轮隐藏截图专项失败均保留，不覆盖为成功。

## 实例与来源

- Provider 为 `custom`、`gpt-6.1-sol`、Responses；读取现有配置，凭据仅进入进程环境，不写入验收文档。端点位于 `127.0.0.1`，现有监听程序为 `cliproxyapi.exe`（PID 20360），测试没有启动模拟 provider。
- 两次真实预检分别耗时 8.328 秒、7.016 秒，均成功。04 复用第二次预检 receipt，没有再次预检请求。
- 04 证据目录：[full-task-projection-20261024-04](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04)。工作区、配置、Electron `userData` 与 `sessionData` 均位于该目录，未使用用户原工作区或原窗口。
- 04 的前端在启动时从 `frontend/dist` 冻结复制，`hmr=false`。记录的基线为 `aa96bb9a`，源 `index.html` 构建时间为 `2026-10-24T08:19:55.208Z`；[frontend-artifact.json](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/frontend-artifact.json) 与 [durable-audit.json](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/durable-audit.json) 中的 SHA-256 标识实际受测文件，不表示包含后续修改。
- 原实例 zoom=1、devicePixelRatio=1.25、CSS viewport=1442×922。原生启动焦点为 false；`showInactive()` 仍曾遮住 Source 参考窗口，原实例已在 teardown 关闭。前三轮恢复实例禁用 `show`、`showInactive`、`focus`；Root 完成 Source 现场核对后，最终恢复按已有用户授权使用自身 `showInactive()` 窗口获取真实 PNG，随后关闭。

启动命令：

```powershell
python scripts/run_full_task_projection.py `
  --configured-state C:/Users/ago/AppData/Roaming/minicode-desktop `
  --out output/playwright/full-task-projection-20261024-04 `
  --preflight-receipt output/playwright/full-task-projection-20261024-02/provider-preflight.json `
  --timeout-seconds 900
```

## 任务与原始结果

任务要求读取需求和错误代码，真实启动两个并行 child，分别读取父级未知随机标记的笔记并审阅两个模块；每个 child 必须先通过实际 `send_message` 发回标记和发现，再返回审阅。父级不得直接读取两份笔记，须修复 `ledger.py`、`reader.py`，实际生成 `result.json` 并运行不允许改动的 `verify.py`。未加入 sleep、dummy work 或伪造启动状态。

独立 oracle 包含 6 项标准库测试，覆盖 Decimal 精度、全局首次 ID 去重、零数与负数、空输入、BOM/引号逗号/字段去空白、输出工件、输入所有权，以及 25 轮各 500 行随机数据。

| 检查 | 04 原始结果与证据 |
|---|---|
| 真实读、改、运行 | canonical 主记录共 16 个：read_file 8、tool_exec 2、spawn_agent 2、write_file 2、run_command 2；两个命令均 exit 0。 |
| 两个真实 child | `ledger-review` / `subagent-f8e3585d`，`reader-review` / `subagent-9e15c88c`；均 done，实际重叠 59.182 秒。两份 durable child journal 分别有 104、99 个事件，均保留真实 start、read、send_message、terminal。 |
| 真实通信 | 两条 encrypted mailbox 消息已入 SQLite，recipient 为 `parent`、public path 为 `/root`，均有 `acked` receipt，且发送发生在相应 child terminal 之前。未尝试解密消息。 |
| 笔记与 verifier | `verify.py` 与初始 fixture 完全相同，两份 child 笔记逐字不变；主记录没有直接读取 child 笔记。child 的工具记录只有读取、编排读取和发消息，没有写文件。 |
| 文件结果 | `ledger.py` 与 `reader.py` 均实际改变；`result.json` 为 `{"cash, reserve":"0.30","debt":"-2.00"}`。 |
| 任务内 oracle | 6 tests、0 failure/error、0.140 秒、exit 0；命令 stdout 与 exitCode 留在主 transcript。 |
| 外部独立 oracle | 6 tests、0 failure/error、0.086 秒、exit 0；见 [oracle.log](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/oracle.log)。 |
| 运行中切换 | 真实工具开始后切到 B：B 的随机草稿完整，B 非 streaming，A 的 streaming map 为 true；再切回 A。见 `01-running`、`02-side-while-main-runs`。 |
| 运行中 reload | child 已启动且 A 仍 streaming 时 reload；连接与 owner conversation A 恢复，无 pending switch。见 `03-child-running-before-reload`、`04-restored`。 |
| 完整最终答案 | 两个准确 CHILD 标记与 `FULL_TASK_OK_98e6c7af0f46` 均到达；最后 assistant 的 `terminalStatus=completed`、`isStreaming=false`。任务实际持续 249.563 秒。 |
| 最终过程自动收起 | 用户尚未主动展开时，主过程 1 组、`data-collapsed=true`；见 `05-final-collapsed`。随后测试主动展开，`last-observed` 的 false 是用户操作结果。 |
| Child 记录 | 当前已由修复后的最终可见恢复验证通过。04 原始记录中工作记录和 CHILD 结果可打开、无 child textarea，但 sent_message 行在展开后为空，原断言失败；该历史失败仍保留，见 `last-observed`、Playwright failure screenshot/context。 |
| 完成后 B 草稿 | 当前已由最终可见恢复验证通过：state、rich composer、原生 PNG 三者一致。04 原 spec 在 child 消息断言处中止，未执行这一最终断言，不把它追记为原轮通过。 |
| Renderer errors | 原轮 `pageErrors=[]`。 |

主任务 17:33:45 左右开始，17:37:55 已有 `item.completed`、`agent.run.completed`、`done(status=completed)` 与 `session.state_changed(idle)`。测试错误地等待不存在的 `last.completionStatus`，直到 900 秒 deadline 才进入最终断言；总运行器观察时长 907.202 秒，不能当作模型任务耗时。

原 [result.json](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/result.json) 保持 `passed=false`，[runner-exit.json](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/runner-exit.json) 保持 exit 1。teardown 已关闭该测试拥有的 Electron 实例。

WebSocket 共记录 1385 个原始双向帧，1220 个 incoming；它们包含心跳、分块和恢复过程，不能作为工具调用数量。CDP 在 reload 区间只捕获了 15 个逻辑 call ID；第 2 个 spawn 由 canonical transcript、真实 delegation receipt 与其独立 child journal 交叉确认，不虚构缺失的原始帧。

## 根因与修复

Source 依据为 `.tmp/codex-src/codex-rs/core/src/tools/handlers/multi_agents_v2/message_tool.rs`：消息先经 `resolve_agent_target` 解析到真实接收 thread，发送成功后再以真实接收身份发布 `SubAgentActivityItem`。`agent/agent_resolver.rs` 与 `agent/control/target.rs` 统一处理 canonical/relative agent path。

MiniCode 的 `SendMessageTool` 已把真实 `/root` 解析到 canonical runtime mailbox `parent`。但 `tool_execution.store_result` 刻意排除了 `recipient=parent`，导致 event、API、journal、冷恢复记录都丢掉这一实际解析结果。前端 `chatSurfaceState` 改用原参数 `/root`，`CollaborationCell` 又仅识别 `parent/team-lead`，因而返回空容器。该行位于 collaboration 边界，不是未展开的 read/run work group。

本批修复完整发布链：

- `backend/agent/tool_execution.py` 保留 canonical `parent` recipient receipt，与其他单收件人使用同一已有字段、事件和持久化路径。
- `frontend/src.v2/chat/chatSurfaceState.ts` 优先消费实际 receipt，并在同一投影边界把 Source canonical `/root` 与 runtime `parent` 统一为一个视图身份；保存的原生调用也经过这个入口。`/root/review` 等实际 agent 路径不被误当 root。
- `CollaborationCell` 只消费统一后的 `parent` 语义，不再自行维护地址别名。
- 完整任务 fixture 改用生产 `terminalStatus`，通过真实可见 avatar/name 入口逐一打开两份 child 记录，并保留不可修改笔记、终态、草稿等断言。未再次运行付费完整任务。

## 统一必要回归

以下一次批次均通过，不与其他批次的重叠测试累计成项目总数：

- 后端 30 项：`test_collaboration_message_receipts.py`、`test_source_encrypted_agent_messages.py`、`test_subagent_service.py`。新用例实际调用无 LLM 的 Source mailbox runtime，覆盖不同 root run、嵌套 parent 路径和 sibling 接收人，核对 store → event → API → journal → incremental/cold child transcript。
- 前端 89 项 / 4 文件：`collaborationRecipientProjection.chain.test.tsx`、`chatSurfaceState.test.ts`、`CollaborationCell.test.tsx`、`ChatTurn.referenceProcess.test.tsx`。真实 04 记录保存在 `frontend/test/fixtures/real-full-task-agent-messages.json`，仅省略 encrypted message 参数，保留原 call ID、状态、时间和 child final text；回归通过 child 的真实过程展开入口检查父级消息，另验证非 root agent 仍显示真实名字并可导航。

## 保留 profile 的无付费恢复检查

Root 最新生产构建后执行 `electron-full-task-recovery.spec.ts`，只恢复 04 已完成 profile，不发送用户消息、followup 或新模型任务。第一次恢复 **1 passed (27.0s)、exit 0**：

- 原生窗口 `visible=false`、`focused=false`，`userData/sessionData` 均为原 04 独立 state。
- 最终答案完整恢复，`terminalStatus=completed`，两个 child 均 done。
- 通过主过程中的真实 avatar/name 入口分别打开 `ledger-review`、`reader-review`，主动展开相应 child 过程后，真实成功的 `已向父代理发送消息` 行通过 DOM 文本与 Playwright layout visibility 检查；各自准确 CHILD 结果保留，无 child 编辑框。
- B 的 `ISOLATED_SIDE_DRAFT_a2b8b0a5e6c364c3` 在 store 与实际 rich composer 中完全一致；没有重新填入 fixture 草稿。
- `pageErrors=[]`。出站命令只有 restore/list/transcript/switch/renderer-heartbeat，没有新用户消息或模型任务。
- [recovery/result.json](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/recovery/result.json) 为 `passed=true`；[recovery-runner-exit.json](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/recovery-runner-exit.json) 为 0；[closed.json](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/recovery/closed.json) 记录 18:06:03 关闭自身实例。功能观察 17.773 秒，关闭后合计 23.639 秒。

该恢复的 `capturePage()` PNG 经人工复核含前一帧或过渡帧，**不计为修复后的原生视觉通过证据**。为改进截图而做的两个后续无付费专项独立保留：

- `recovery-02`：隐藏 Electron 下 `page.screenshot()` 在首张图等待 native frame 超时，exit 1，已关闭。
- `recovery-03`：改为 Electron `beginFrameSubscription` + `invalidate` 后拿到绘制帧；第 1 个 child 的 state/DOM 检查通过，第 2 个 child 的祖先 CSS opacity 在隐藏 compositor 中停于 0，新加的“等待 opacity=1”截图断言失败，exit 1，已关闭。它不是原始 parent 收件人投影错误的复发；DOM 中仍有成功消息文本。

这些是隐藏截图专项失败，不与第一次恢复的功能检查混为一项，也不作为动画/最终原生视觉已经验证的证据。没有重发付费任务，没有通过修改 CSS 或强制设置 opacity 制作成功截图。

## 最终可见窗口恢复与截图

Source 现场核对完成后，Root 明确安排复用 04 已完成 profile，以 `showInactive()` 打开独立 QA 窗口，不再使用隐藏 compositor。最终 `recovery-visible` 为 **1 passed (25.2s)、exit 0**，全部 8 个功能 gate 再次通过，`visible=true/focused=false`。等待实际 child 过程与面板过渡结束后，由 Playwright 获取原生页面 PNG，未修改 CSS、opacity 或动画速度。

- [ledger-review 工作记录](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/recovery-visible/01-child-1-record.png)：可见真实 REQUIREMENTS/child-one/ledger 读取行、`已向父代理发送消息`、准确 CHILD_1 与审阅结果。
- [reader-review 工作记录](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/recovery-visible/01-child-2-record.png)：可见真实 REQUIREMENTS/child-two/reader 读取行、`已向父代理发送消息`、准确 CHILD_2 与审阅结果。
- [B 草稿](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/recovery-visible/02-side-draft.png)：会话选择、空会话页面及完整随机 rich-composer 草稿已同时呈现，已等待 conversation switch 完成，没有重新填入文本。

上述三张 PNG 已逐张人工查看，均与对应 DOM JSON 一致；没有 child 用户气泡或 child 输入框。该项验证的是本轮读取/通信/草稿场景，不替代整个产品的视觉或动效对齐审计。

最终 [result.json](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/recovery-visible/result.json) 为 `passed=true`；[runner-exit](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/recovery-visible-runner-exit.json) 为 0；[closed.json](C:/Desktop/MiniCode/output/playwright/full-task-projection-20261024-04/recovery-visible/closed.json) 记录 18:17:02.650 关闭。功能观察 20.364 秒，关闭后合计 22.036 秒。出站命令只包含 restore/list/transcript/switch/renderer-heartbeat，未提交新任务或重发模型请求。

最终恢复的冻结产物 hash 与源路径见 `recovery-visible/frontend-artifact.json`，完成后工作区文件 hash 与原 04 durable audit 再核对，未改变任务文件或 oracle 结果。恢复 spec 现保留最终可见窗口方式；完整任务 spec 的历史 revision 不再硬编码，后续只有调用方提供 `MINICODE_FULL_TASK_BASE_REVISION` 时才记 reported revision，并始终记录实际冻结 index hash。

两个新 E2E 均按项目现有 real-provider spec 的 `test.describe/test.skip` 模式声明手动输入：完整任务缺 out/key/base/model/wire 时跳过，恢复缺 retained out 时跳过。默认 discovery 不读取缺失路径、不启动 Electron 或真实模型；手动 launcher 提供的输入仍会执行。该边界仅属于可选验收配置，不是生产行为的兜底。

## 先前设置尝试

- 01：真实 provider 预检成功，但 launcher 的 GBK stdout 无法编码 Vite 箭头，任务未发送。已改为 UTF-8；保留 `launcher-failure.json`。
- 02：预检成功，选择 `appMode=code` 导致编辑器布局，旧 textarea 选择器等待失败，任务未发送。
- 03：cowork 布局正确，但 rich composer 的 `data-placeholder` 不支持旧 `getByPlaceholder`；任务未发送。已改为真实 role textbox `消息输入`。
- 04：冻结生产前端后的第一次完整真实任务。上述设置失败均保留在各自输出目录，不计为完整场景通过。

未清理测试输出、durable journals 或 fixture 工作区；未修改用户原窗口、未保存内容、原工作区草稿，也未做 Git 操作。
