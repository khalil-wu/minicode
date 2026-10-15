# Source 执行 context 与连接退场：接入与验证

本子项已完成生产接入和必要验收。官方账号不在范围内；这里不宣称整个 MiniCode 已与 Codex 桌面端完全一致。**旧失败批全部保留**。最终接受 6 个不同的实际 GUI 场景：CC81C9Cg 的原窗口/原任务生命周期 4 项，以及 BT3XeGQn 的独立 memory HTTP/应用退出 2 项。最后模型、子 agent 与资源收尾统一批 **86/86**；按用例去重后的历史必要后端最后状态 **190/190**，不是把运行数相加。

## 当前实际接入

- manager 强持原实际 execution host/context，与运输 `_sessions` 分离；公共健康/runtime 会话枚举仍只使用原运输语义。只有 scoped 资源收尾与内部执行路由通过独立 `resource_sessions` 看见 detached executor。
- durable user input 在 scheduler 建立其实际 Task 时、任何协程第一步之前取得原 host admission；Home 无真实 cid 也保留原提交 task 和 durable origin。真实 cid 确定后绑定已有实际 executor，不创造临时 conversation，不复制 Session。
- `_wait_and_cleanup` 从运输 command tasks 移至实际 execution cleanup 集合；正常原 task、RunContext、permission、waiter、已发 adapter、窗口 binding token、后台命令仍由原 host 持有。排队 dispatch 同样绑定实际 execution admission。
- 普通断开仍在 **30 秒**退运输；显式 `shutdown_session` 同样退运输。连接 UI/RPC 命令、原 UI terminal、session preview、文件 watcher 和投递尾部正常收尾。原核心、普通 server approval/user-question/elicitation waiter、后台命令、extension/LLM borrower 不随 socket 被停掉。
- reader hydration 按真实 borrower 分配：无 cid 的已接受 Home admission、以及当前 reader cid 上实际 core/cleanup/notification/child task 仍需要它时保留；owner 阅读 B 而 A 运行，B 的 reader 不因此成为 A 的执行状态。原代码 store 不在运输退场中清理。
- 每 conversation 的 idle watch 使用 Source 默认 **60 秒**、`max(no_subscribers_since, inactive_since)`、布尔状态实际变化时间，并在到期重查实际执行。实际 manager watch/unload 协程不加入自己的 pending/drain 集合。
- 实际异步子 agent、后台 continuation 和 cleanup owner 通过既有 `get_agent_owner_run` 的真实 owner 边映射到原 conversation；用真实 `_subagent_tasks` 当前 Task 占用活动门，不从 UI running 状态推导任务。
- archive/delete/clear 提交前停止该 conversation 的真实 admission Task（含其他 submitting host），再停止原 run/children 与 scoped 资源；停止 A 不关闭同 host 的 C。真正卸载使用 exact context/request token，迟到旧 cleanup 不能摘除 replacement。普通 idle unload 不清 durable follow-up 队列。
- 原物理窗口合法重连复用**同一个原 Session、EventOutbox、durable queue owner 和 writer**。native binding 验证后仍核原 window instance+owner；新物理窗口不继承旧身份。清理失败的 closing host 保留 registry，但拒绝用半关闭对象接新连接。
- extension 明确 shutdown intent 使用独立 `shutdown_execution_host`；app shutdown 遍历 attached 与 detached 原 host、排空真实对象、按 exact owner 释放。真实失败聚合传播并保留 owner，不丢弃 gather 结果。已断 ASGI 连接按真实 client/application 状态处理，避免二次 close 掩盖实际 drain。
- 独立 memory/model borrower 不进入前台 run/侧栏 spinner。普通运输或 idle core 退场不取消它，原 host 的资源等待原实际 lease Task 完成；这期间合法原窗口重连仍复用原 channel，不等待 memory 结束。真正 execution/app shutdown 先停止命令与主 run，再由 runtime authority 停子 agent、写入实际 shutdown 原因，随后排空剩余模型借用 Task，最后才退 ModelRuntime/扩展和 close adapter。未排空即保留原资源并传播失败；不再 force-close retired adapter / clear 实际 lease graph。

## 接入后发现并修复的根因

1. 实际新建 A 的 reader，可能先于异窗首次 core 启动。原 create producer 没有登记订阅，运行窗口关闭后该 reader 不能看到原活动 runtime。已在 create 成功 activate 后、`conversation.switched` 发布前调用既有 `observe_conversation_stream`，即使暂时没有 core 也保存真实订阅。未根据 `active_conversation_id` 在执行 registry 猜订阅。Source 对应 `thread_processor.rs:1490-1493`：创建客户端在 `start_thread` 之前显式 `try_add_connection_to_thread`。
2. full shutdown 后读取 pending Task 原先经 `running_task_for` 间接读取已关闭 durable queue。已直接借用 RunManager 的实际 `run_tasks`，保持“注册直到 cleanup”语义，也避免用 reader/queue 查询冒充生命周期证据。
3. 实际 Electron app 退出时原窗口可能已经关闭 ASGI；full host drain 不再重复发送 websocket.close。失败 host 的原 writer 仍归 registry，不另起新 writer。
4. 同物理窗口的普通重连不重新创建一个仍在运行的 file watcher；真正运输退场后的合法重连才重新启动已停止 watcher。

## 必要验证（保留失败，按具体用例追踪）

- 初批 `output/source-execution-lifetime-backend-20261015.xml`：**102 通过 / 8 失败**。不覆盖原结果。
- 受影响批 `output/source-execution-lifetime-affected-20261015.xml`：**50 通过 / 10 失败**。10 个生命周期失败均暴露上述 closed durable queue 根因；不计通过。
- 修正实际 Task drain 后 `output/source-execution-lifetime-registry-drain-20261015.xml`：**14/14**。
- 实际 reader producer、ASGI close、冷连、激活与前台导航统一批 `output/source-execution-lifetime-live-reader-20261015.xml`：**45/45**。其中包含实际 Home durable scheduler、原对象保活、原审批 waiter 由 reader 解决、idle 双门、不同 cid、实际 child continuation、失败 owner 保留和迟到 unload token。
- failed-host reconnect 单例 `output/source-execution-lifetime-failed-host-reconnect-20261015.xml`：**1/1**；随后扩展为实际有 core / 无 core 两参数用例，旧单例由这两项取代。
- 最终强持 writer drain 批 `output/source-execution-lifetime-final-drain-20261015.xml`：**65/65**，覆盖最终生命周期 caller 与运输退场、显式关闭、冷连。无 core 的真实 writer 收尾也持有原 host，失败不能使另一个 writer 接管同 sid；没有因此创建 conversation。
- 在上述前六批中，按具体 testcase 去重并排除被两参数用例取代的旧单例，当时为 **143 个不同用例最后状态通过**。加入后续 auxiliary 两批后为 **190 个不同用例最后状态通过**。明细 `output/source-execution-lifetime-latest-cases-20261015.json` 保存每例来源和原始失败批，不把运行次数相加，也不由单元通过推导 GUI 通过；实际 GUI 单独记录如下。
- 随后真实 GUI 抓到独立 memory 仍借用已关闭 adapter 的错误，修复后第一批 `output/source-auxiliary-model-lifetime-20261015.xml` **60/60**，最后统一真实子 agent authority / lease / lifecycle callback 收尾批 `output/source-auxiliary-model-subagent-lifetime-20261015.xml` **86/86**。新增用例实际调用 `schedule_memory_startup` 注册真实 Task；验证取消清理尚未结束时原 ModelRuntime/adapter 保持活、超时原 host 保留、未绑定 Home command 阻止提前关资源，以及普通运输退出的 memory 不成为前台任务。它们控制 provider/清理时间，不冒充下面的实际 HTTP GUI。

## 最终实际 GUI 验收

明细 `output/playwright/codex-control-audit-20261013/execution-lifetime-latest-results.json`：**6 个不同实际场景通过**。没有重跑此前已验收的五个 follower 场景；后续只对新发现的辅助模型关闭问题做两项窄验。六张被接受场景的 PNG 均已打开目视。

### 原窗口与原任务（CC81C9Cg）

构建 index SHA `bb69f2b5eb17923690ac17fbe789485a2ca20b02f0c37971ac82727de33b4e94`。

- `production-execution-lifetime-run02/evidence.json` 的**同物理窗口 reload**一场真实通过：SID / instance / owner 不变，实际原 HTTP/task 没有重复。该次整体结果因后续 QA 原生 URL 准备被阻塞而保留失败；只接受已经完成的这一场。
- `production-execution-lifetime-run04/evidence.json` **3/3**。复用同临时 `uCBjHo` profile 与 `zgRc59` workspace；没有重新 seed 会话、清理旧消息/隐藏草稿、编辑器或剪贴板。按同构建和 caller hash 引用 run02 已过的 reload，避免重复。
- 原执行窗口在阅读 B 时真正关闭，等待 **32.510 秒**，原 HTTP 继续。原 native Window、PTY process 与被实际创建的 browser WebContents 都真实消失。
- 新物理窗口复用 `secondary-1` owner slot，但以**新的 instance 和 SID**观察原 A；保留 B 的隐藏未发送草稿，没有借用原窗口资源。
- 原真实 executor 继续 `read_file` 成功与 PowerShell `run_command`，后者真实 **exit_code 0**、stdout 为 `EXECUTION_LIFETIME_REAL_COMMAND`。两次 provider HTTP 均正常完成；第二次请求包含真实两条 tool result。
- 同一 `task_00001_5d0db495feb7` / `run_1553899039b8` / `m-05b2209927a8-a` 到最终 `completed`，primary 和新窗口都收到 `EXECUTION_LIFETIME_ORIGINAL_FINAL`，过程自动折叠。`changedRuntimeInputs=[]`。
- run04 正常退出后额外发现真实 memory-consolidation `Chat HTTP client is not initialized` 日志；这条记录没有被当作“全局退出无错误”。按下面的根因修复和新窄验闭合。live 56→final 55 的计时 producer 问题交由 Root 按同一个真实 run clock 另批修复；本文不以旧截图宣称该计时差已通过。

### 独立 memory 模型与应用退出（BT3XeGQn）

`production-auxiliary-model-shutdown-run01/evidence.json`：**2/2**，index SHA `c625d46c2ddaead0ad95fa6b5a2c857dc8cf81462b57089686a5ed9ba6cada28`。这是新的独立临时 profile `kN4Dpb` 和 workspace `tqomXW`；没有 lifecycle/memory store fixture，没有缩短任何 grace。

1. 一次真实前台 HTTP 完成、仅一条完成答案；随后实际 memory-consolidation HTTP 已进入流并保持。界面仍 `streaming=false`、`active_task=null`，没有重开前台工作或新增助手答案。
2. 真正 `app.close` 后，memory HTTP 在退出完成之前被取消 (`aborted=true`) 并排空。原错误 `Chat HTTP client is not initialized` 消失，desktop.log 无 ERROR，明确 `Application shutdown complete`、`Finished server process [37628]`、exit 0。实际退出用时 **1.584 秒**，`changedInputs=[]`。自身 Node/Electron/provider/sidecar 与 PID 37628 的子进程核对无残留。

### 失败记录没有覆盖

- run01：create reader 未订阅导致活动状态 UI 断言失败；已修真实 create producer。关闭时实际 ASGI double close 也已修，必要回归通过。
- run02：同窗 reload 通过，但准备一个未登记的 localhost browser URL 时触发产品真实 private-network 确认。没有审批/打开该 URL；只销毁本 QA 的 owner 窗口取消请求，然后真实 app.close。自身进程核对全部退出，旧日志/evidence 保留。后续资源仅用 `about:blank`，不把真实 URL 访问算通过。
- run03：Playwright 的 Electron main execution context 在已有 reload 场景中销毁，finalizer 再 inspect 也失败。原 log 与 `automation-failure.json` 保留，没有计 GUI 通过，也没有在无生产根因证据时改产品。核对自身进程均退出；run04 只补未验收场景。

上述 190 个后端不同用例最后状态和 6 个 GUI 不同场景分别记录，不能合成一个“全部 Codex 行为已对齐”的百分比。

## 实际离屏 QA run01（失败保留）

`output/playwright/codex-control-audit-20261013/production-execution-lifetime-run01/evidence.json`；独立临时 profile `minicode-execution-profile-Pb5Ogw`。没有操作普通窗口、草稿、编辑器或剪贴板，没有写 dist，也没有缩短运输 grace。

- **通过**：同一物理原窗口 reload，实际 SID / window instance / window owner 不变，原 HTTP 请求只有一条，恢复同一个实际 task。
- **观测但未通过整场**：原执行窗口在阅读 B 时真正关闭，实际等待 **32.505 秒** 后原 HTTP 仍未 abort；现有 reader 的活动 runtime 未接上，UI 断言超时。因此该场记失败，不能把 HTTP 仍在运行冒称完整界面验收通过。
- **未执行**：新物理窗口同 owner slot 的新 SID 观察、实际 `read_file/run_command` 与最终答案。run01 在到达工具之前失败，未用它宣称 native consumer 正常。
- 退出清理另暴露真实 ASGI double close，已修并有必要回归。独立 Electron、sidecar 与 provider 全部退出。

下面保留接入前审计和当时方案。其“尚未接入 / 等待冻结释放”描述是历史阶段，不代表当前生产状态。

## Source 的实际边界

- `app-server/src/message_processor.rs:868` 的 `connection_closed` 关闭该连接的 RPC gate、MCP event streams、请求回调，以及 connection-owned command/process 执行，再调用 thread processor；它没有把连接关闭当作 core thread Shutdown。
- `app-server/src/request_processors/thread_processor.rs:3588` 只经 `ThreadStateManager.remove_connection` 移除 subscriber。只有 core manager 已无该 thread 时才清理陈旧 bookkeeping。
- `thread_state.rs:622` 只从 live connections 和 thread subscribers 集合移除目标连接。`request_processors/thread_processor_tests.rs:1225` 用两条连接和真实 oneshot cancellation receiver 明确证明：移除 A 后 B listener 保留，cancel receiver 未触发。
- `thread_processor.rs:1020` 的显式 `thread/unsubscribe` 同样只退订。`prepare_thread_for_removal:1055` 才从 core manager 移除真实 thread、等待 Shutdown 并清理 pending requests/listeners；全局 shutdown 也是独立入口。
- `command_exec.rs:388` 只终止其 `process_id.connection_id` 属于关闭连接的裸命令；`request_processors/process_exec_processor.rs:427` 对其 connection-owned process handles 做同样处理。不能据“core 继续”推导所有窗口终端和裸进程都必须保活。
- `request_processors/thread_lifecycle.rs:20` 的 `UnloadingState` 同时观察 subscriber 与 core active；两者都为 false 才以 `max(no_subscribers_since, inactive_since) + delay` 为卸载目标。`core/config/mod.rs:3927` 的 `thread_unload_delay_secs` 默认 **60 秒**。不能永久强持所有 loaded idle thread，也不能让运输的 30 秒清理杀掉 active thread。
- Source 仅在上述布尔状态实际变化时更新对应时间；到期后还检查真实 `AgentStatus::Running`，若 watch 仍误报 inactive，`note_thread_activity_observed` 重置该 inactive 区间。`thread_lifecycle.rs:427` 只有真实 idle unload 时才取消该 thread 的 pending server callbacks；Shutdown 提交失败或超时保留原 loaded runtime，并清除本次 pending unload。
- `OutgoingMessageSender.connection_closed:275` 清的是 connection RPC `request_contexts`。普通 server approval callback 存在独立的 `request_id_to_callback`（`:330` 起，`:375` 注册），不会随普通运输关闭被清掉；官方账号 user verification 是独立例外，按本任务范围排除。

## MiniCode 的完整杀伤链

1. `backend/main.py:545` 在连接 handler 退出后调用 manager.disconnect，带原 connection generation。
2. `manager.py:569` 标记 disconnected，30 秒后从 `_sessions` 移除，再调用 `session_lifecycle.shutdown(disconnect_timeout)`。
3. 显式 `manager.shutdown_session:606` 也从 `_sessions` 移除并进入同一 full shutdown；extension requested shutdown 的真实 caller 为 `agent_runner.py:1696`。全局 manager.shutdown 又复用该入口，当前把运输退场和执行退出混在一起。
4. `session_lifecycle.shutdown:1375` 设置整个 lifecycle shutting down，取消全部 agent runs，清空全部 TurnWaitState waiters/approval diff/Guardian hosts，停止 notification wakes、扩展和子 agent，随后取消 command tasks 和 TaskManager 中全部任务。
5. 还存在独立取消传播路径：`handler.py:1318` 把 `_wait_and_cleanup` 放入 `command_dispatcher.command_tasks`，该 cleanup 在 `:1191` 直接 await managed run。取消 transport 的 command task 集合，即使跳过 `cancel_agent_runs`，仍可经 cleanup waiter 把真实 core task 取消。
6. full shutdown 随后销毁所有 UI terminal、session preview、BackgroundCommandManager，关闭 LLM adapters、ArtifactStore 和 durable queues。这些对象已有活跃 core borrower 时不能提前退场。
7. `_conversation_execution_owners` 目前为弱引用；`execution_session_for_conversation` 和 `running_session_for_conversation` 又要求 owner 仍在 `_sessions`。因此即便单独保住 asyncio task，retire 后 subscriber 的 Stop/steer/approval 和后续输入仍会失去真实 executor。

资源实际调用链也已读：`RunCommandTool` 使用 `BackgroundCommandManager` 的 conversation/task scope；UI terminal 是另外的 `TerminalSessionManager`。`loop_bootstrap.py:508` 把实际 task/background manager、terminal manager、permission、artifact store、conversation ID 和 run_context.window_binding_token 固定到 ToolExecutionContext。`BrowserControlTool:545` 使用原 window instance/binding token 发起操作；不能给旧 run 换成 follower 的 token。

## 具体方案

### 1. 单一执行 owner 独立于 socket registry

让 manager 强持真实 loaded conversation 的 execution context；context 持有原 executor 和其实际 run/cleanup/admission 所有权，不复制 Runtime、权限、waiter、adapter、browser 或 terminal。`_sessions` 只负责当前运输绑定及窗口身份；执行路由从 loaded context 查找，而不是把“仍连接”当作执行权证明。

首次真实接受的 startup/admission 也必须取得执行生命周期归属，不能等 `_register_agent_run` 之后才保留 context。已经收到 durable ACK 的输入不因为发出窗口关闭而消失或变成 Stop。已经运行的原 task、cancel event、RunContext、ToolExecutionContext、模型调用和 child tasks 保持原对象。

### 2. 两种清理 authority

- 运输退场：退订这个 connection，关闭旧 socket/RPC gate，取消和排空它真正拥有的 UI/RPC 工作，停止其裸进程、窗口 terminal/browser 资源。保持现有超时，不能延长 30 秒、跳过整个清理、把资源改归其它窗口。
- core 退出：真实 Stop、archive/delete/unload、明确的 harness shutdown intent 或最终 application shutdown，按相应 conversation/host 取消、排空并释放执行对象。Stop 结束 turn，不能顺便关闭同 host 其它 loaded thread。

extension caller 当前使用 `shutdown_session`；实现时必须把“明确结束执行 host”与“关闭 session 的运输连接”改为不同明确入口。不能仅凭任意字符串 reason 猜 authority，也不能把 application shutdown 降成普通退订。

### 3. 清理任务与借用关系一起迁移

把 execution cleanup waiter、durable input admission、排队 follow-up、turn notification wakes 和 core 子任务从 transport 的 command/task cancellation 集合移出，纳入真实 execution context 的生命周期。关闭连接时仍排空其 RPC/投递尾部；不取消原执行、原审批 Future 或共享给活跃 turn 的 MCP/extension/LLM borrower。

一个原 session 可能执行 A、C 而正在阅读 B。关闭 B 所在窗口只退它的 reader/transport 和窗口资源；A/C 的 BackgroundCommandManager 记录、LLM leases、原权限及各自 waiter 保持。只停止 A 时使用已有 conversation-scoped cancel/destroy，不调用会关闭 C 的全 session 清理。

### 4. 重连必须维持唯一原始 channel writer

原 owner 合法重连仍先验证 server-issued window binding 和原 window owner 身份；不能让其它窗口用同 session ID 认领 executor。保留的逻辑 origin/channel 复用原 EventOutbox 与 durable command owner，再绑定新 socket/generation，避免两个 outbox 同时写同一 `{session_id}.jsonl`、各自从旧 `_event_seq` 分配重复序号。

这保留的是 loaded execution 和逻辑 channel 的唯一 owner，旧 socket/RPC/UI 资源仍正常退休，不能用增加 timeout 替代。现有 `ConversationRuntime.shutdown` 仅排空 reader hydration，可用于运输退场而不清理 core。旧 disconnect timer、旧 delivery、旧 run cleanup 均按原对象和 generation 释放，不能按相同 ID 清除新绑定或 replacement turn。

### 5. 没有 subscriber、真实 idle 卸载与最终 shutdown

按照 Source，subscriber 数为零不等于 thread Stop。真实 active 执行仍产生原 journal/最终记录，后来订阅可恢复真实快照与同一 turn，不复制任务。只有 **无 subscriber 且 inactive** 的区间满足 Source 默认 60 秒双条件 deadline，才启动真正 thread unload；不是强持全部 idle context 永不卸载。普通审批 callback 不因 socket 关闭被自动拒绝或复制，真实 core 卸载时才按 thread 清理。原原生窗口能力已经关闭时保持真实 unavailable 结果，不能借用其它 subscriber 的 browser/terminal。

显式删除/归档通过 loaded execution registry 找到离线 executor；最终 app shutdown 同时遍历 attached 和 detached execution hosts，按原对象去重后完整 drain。不能只遍历活跃 `_sessions` 而泄漏离线执行。

## 拟修改文件边界

| 文件 | 必要修改 |
|---|---|
| 新 `backend/ws/execution_context.py` | 明确 loaded context、原 host 和实际执行任务/cleanup/admission 的唯一生命周期所有权，不做状态副本或兜底 |
| `backend/ws/manager.py` | transport 与 loaded execution registry；两个退出入口；原 owner/generation 重连；离线 executor 路由；全局 drain |
| `backend/ws/session_lifecycle.py` | 拆运输清理与 full execution shutdown；按真实 owner/borrower 分配 resource 清理 |
| `backend/ws/handler.py` | 注册实际 execution context；cleanup waiter 归 execution；保留原 issued context/window token；唯一 outbox 重连 |
| `backend/ws/command_dispatcher.py` | durable admission/执行 mutation 与运输 RPC 的任务归属；沿用已经修好的 server-owned origin 和 delegated scope |
| `backend/ws/run_manager.py` | scoped run/context 释放与原 generation 核对；多个 thread 共享 host 时的收尾通知 |
| `backend/ws/agent_runner.py` | extension 明确 shutdown intent 使用 execution 入口；不修改原 task 权限/adapter/browser identity |
| `backend/ws/handlers/conversation.py` | Stop/archive/delete 查 loaded executor，作用于正确 conversation；不能清同 host 其它 thread |
| 必要的原生 transport/resource 入口 | 只在上述 owner 分类证明需要时改，不迁移原 terminal/browser owner |

不改前端控件、字体或 dist；不改 SourceNotes 独占的 execpolicy helper。本方案没有提出新的常规前端状态或控件。

## 已新增但尚未接入的基础模块

当前仅写入 `backend/ws/execution_context.py`。没有任何现有模块 import 它；所以 30 秒退场、执行路由、审批和 shutdown 的现有行为尚未改变，不能将此模块记为功能完成。

| 接口 | 当前准确职责 |
|---|---|
| `ExecutionOrigin`、`ExecutionHost` | 强持原实际 session，冻结原 session/window instance/window owner 标识；不复制 Session、权限、waiter、adapter 或窗口 token |
| `ConversationExecutionRegistry.retain_host/load` | 同 sid 只认同一个真实 host 对象；同 conversation 只认原 executor；正在 unload 的 context 不接受新 admission |
| `host_for_reconnect` | 在 caller 已验证真实 native binding token 后，仍严格匹配原 window instance 与 owner；仅返回原 host，不绑定 socket，不更改任何旧任务的 browser/terminal scope；原 EventOutbox/durable origin 由 caller 复用 |
| `ThreadUnloadingState.create/sync/unloading_target/note_thread_activity_observed` | 精确表达 Source 双 false、monotonic 时间、状态实际变化与默认 60 秒；模块内部不创建计时任务、不改变运输 30 秒 deadline |
| `track_admission_task/track_lifecycle_task/pending_tasks` | 强持真实 Task 对象，将来承接 accepted startup 与 core cleanup；原 SessionRunManager 仍是 run/cancel/borrower 的唯一实际 registry，无第二份 task ID/权限状态 |
| `try_begin_idle_unload` | 消费实际 subscriber/active 状态；到期再看原 executor 的真实 run/accepted admission，仍有执行时按 Source activity observed 推迟；没有自行 Stop/关闭资源 |
| `begin_unload → ExecutionUnload → finish_unload` | 每次真正卸载拿原 context 的具体请求令牌；只有原 context 和原请求都仍匹配、真实任务已排空才摘除，不让迟到 callback 删除 replacement |
| `unload_failed` | 仅清本次仍匹配的 pending unload、保留原 runtime；不吞异常、不自动重试。新 listener 必须按 Source 重新建立自己的 UnloadingState watch |
| `contexts/retained_hosts/contexts_for_host/release_host` | 全局收尾可遍历 detached hosts；只释放原 host 对象且其 conversation 已释放。实际资源 drain、attached/retired 判定与重连串行化仍由 manager 接入承担 |

后续待接：manager 的强 registry 与真实 subscriber watch；注册 admission/实际 run；将 cleanup waiter 从 transport task 集合移出；按 Source 60 秒建立并重查 idle listener；运输退场、显式 execution shutdown、scope stop/archive/delete、最终 app shutdown；合法原窗口重连的唯一 outbox writer及迟到 generation 处理。不能只接一个 registry 而保留旧 full shutdown 取消传播。

本次仅做 AST 语法解析，结果正常；未 import/执行该模块，未运行未接链测试，未写 dist。完整批次接入后再统一补必要测试和实机，仍待 Root 明确释放共享文件。

## 一次补齐后统一验证

必要场景包括：30 秒真实退场和显式关闭两入口；owner 阅读 B 同时执行 A/C；仍在 HTTP/tool 中及 startup admission 的原 task 持续；原 approval/用户提问 waiter 可由 follower 解决；原 BackgroundCommand 继续而 connection-owned UI 资源正常退场；无 subscriber 后恢复同一 turn；合法 owner 重连保持唯一事件和 durable origin；旧 timer/cleanup 不清 replacement；归档/删除 A 不影响 C；最终 app shutdown 释放全部 detached contexts 和真实进程。

先实现完整批次再统一补必要测试、运行校验和实机。当前只读阶段未把任何一项记为通过，待 Root 明确释放共享 WS 文件后才实施。
