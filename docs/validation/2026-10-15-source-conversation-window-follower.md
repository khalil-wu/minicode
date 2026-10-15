# 同一运行线程的多窗口公共投影与原执行归属

本批修复已有聊天通过新窗口 `session.restore` 后仅有历史、`isStreaming=false`、没有原任务过程的问题。第二个窗口观察同一真实 turn，后续输入、停止和审批回到实际执行 owner；没有创建第二个 agent、复制私有运行时或转移浏览器、终端、模型的窗口身份。

产品文件在 backend batch03 运行前冻结。当前后端范围 **297 / 297 passed，0 failed / error / skipped**；前端四文件 **218 / 218 passed，0 failed / pending**。这不是整个 Codex 等价性结论，也不能把下述离线调度验证冒充真实模型/桌面通过。三窗口真实 HTTP / 工具 / UI 补验由桌面 agent 另行执行。

## Source 依据

参考 checkout 固定为 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。保存的 ASAR 只读查阅，没有执行其中的 JS；其文件 SHA256、字符偏移与原始片段保存于 `output/conversation-window-follower-20261015-source.json`。

- `codex-rs/app-server/src/thread_state.rs` 的 `ThreadListenerCommand::SendThreadResumeResponse` 明确用于在原 thread listener 的串行顺序内发送运行中历史并原子订阅新通知。`ThreadStateManager` 记录 thread 的实际 connection subscribers，`subscribed_connection_ids` 不建立第二个 thread。
- `request_processors/thread_processor.rs` 的 running-thread resume 分支读取 `existing_thread`、原 `config_snapshot` 和 `listener_command_tx`，将 resume 发给现有 listener，不冷启另一会话或应用新窗口的配置来替换正在运行的配置。
- `request_processors/thread_lifecycle.rs::handle_pending_thread_resume_request` 合并当前 active turn 与持久历史，核对 unload / live connection，`try_add_connection_to_thread` 后发送 response，并重放该 thread 的待处理请求。普通 thread 通知使用 `ThreadScopedOutgoingMessageSender` 和真实 subscribers。
- Source `thread_resume.rs` 中 `thread_resume_keeps_in_flight_turn_streaming`、`thread_resume_rejoins_running_paginated_thread_with_initial_page`、`thread_resume_replays_pending_command_execution_request_approval` 和 file-change approval 用例分别覆盖运行中恢复、历史分页与真实待审批请求重放。
- 保存的 `app-shared-737655e1fb23.js`：`y3t` stream state 持有 `followedConversationIds`、`followerClientIdsByConversationId`、revision 和 snapshot / patch publication；`sendThreadFollowerRequest` 仅在真实 follower role 下将请求交给 `ownerClientId`。
- 同文件的 follower 请求分发支持 `thread-follower-start-turn`、`thread-follower-steer-turn`、`thread-follower-interrupt-turn(expectedTurnId)`，以及原 owner 的 command/file/permissions approval、user input 与 MCP elicitation 响应。它没有让新窗口拿自己的权限缓存替代原请求的 waiter。

## 修复的完整调用链

### 公共观察与原子恢复

`WebSocketManager` 分开记录真实 execution owner、各窗口观察过的 conversation 与逐 conversation 的公共流顺序锁。owner 由真实 run 注册产生；观察者只是订阅，不持有另一份 `run_tasks`、`_conversation_streams`、approval future、permission cache 或 model activation。

`send_event` 先完成原有 canonical owner / payload 校验，再在同一公共流锁内更新原 owner 的 reducer，给原窗口及 observers 入队。恢复在该锁内订阅，并重新读取当前公共持久历史、冻结原 owner 的 stream / pending request，再接后续增量。恢复读取与订阅之间恰好到达的最终答案由最新持久历史覆盖，不依赖“以后应该还有一个 delta”。大 snapshot 沿用既有分页协议，不在 page 0 和最后一页之间插入新 delta。

新窗口的公共事件直接进入它自己的 outbox，不再执行一次 `send_event`。因此不会再运行一次 notification hook、注册新的 waiter 或回写私有 stream reducer。原始 `source_event_seq` 保留；接收窗口生成自己的 `seq` / `event_id` 和 durable replay chain。既有 live-only delta 策略保持，断线靠累计 `stream_resume` 恢复；它们不被伪记成 durable replay。

公共分发只接受现有 thread timeline / typed controls。SDK-only provider frames、原始 provider reasoning、Inspector 私有 payload、provider auth prompt、session capability snapshot 和浏览器 / 原生终端控制流不复制。工具失败的真实 status / output 仍存在于公开过程及展开详情，没有删除错误来伪装成功。

### 每个窗口保留自己的 reader 和草稿

`task.update` 不把原 owner 的整份 session 发给另一个窗口。每个接收窗口重新生成自己的 runtime，只合入所观察线程的真实 running IDs、当前 reader 对应的 task ID、公开 pending input 和 approval 摘要。`active_task_id` 只代表当前 reader 的真实运行 turn，避免 A 的旧 task 在完成后经兼容分支把 B 或 Home 重新标成 busy。

观察 A 后切 B / Home，A 的公开事件继续携带 A 的 conversation ID 更新后台记录；不会改变当前 active ID、工作区、权限对象或草稿。原 execution owner 正在阅读 B 时，另一个窗口的 A 输入也不会通过 activate / prepare 路径把它切回 A。

另一窗口的用户输入真正提交后，当前正在阅读 A 的窗口复用现有 `session.synced` 和 `stream_resume` 更新真实持久 transcript；不创建新的伪用户消息事件或控件。若读取期间用户已导航到 B，则不发布 A 的 active snapshot。

### delegated 调度、原输入身份与真实审批

现有 `owner_conversation_id` / `ownerConversationId` 在 dispatch 和 lifecycle 锁之前统一为 canonical conversation ID。锁、转发和结果封套使用同一个目标，不能把持久 A 的命令重新解释成后来选中的 B。

转发仍保留提交窗口的 durable command、ACK、client ID 与 host 产生的 `_client_queue_key`。`start_agent_run` 只在 origin 缺失时生成本地 origin，不能覆盖已经准入的原窗口身份。scheduled runner 与 cleanup 继承回执归属，迟后的实际 admission 再按原 origin 定位。已知原提交窗当前不可达时明确不把其私有回执发给 execution owner；原 admission / consumption journal 留给原 origin 恢复后真实 reconcile。相同 client ID 在两个窗口的本地 durable command 不相互完成或改绑。

转发的执行入口有自己的 `try/finally`，负责清除该 execution dispatcher 创建的 regeneration transition。拒绝、清理尚未结束、异常、取消和成功退出都不遗留永久阻止后续 turn 的 transition。

delegated `CommandScope` 来自 server 已确认的 execution owner，通过 host-only ContextVar 绑定 A，不修改 session 的 reader B。普通命令的 stale owner 检查继续存在；不能从 wire flags 声称这个执行身份。子 agent 的 mailbox / cancel / plan review 使用实际 parent ownership；停止子 agent 的显式 follow-up resume 从 A 获取 permission context 和 registry，保持原 source session / cwd。

原始提交 dispatcher 继续拥有 pre-run admission。Stop 在模型尚未注册、slash / review 准备仍 await 时，定位真正的 admission owner，先持久化原输入 Stop fence，再取消该任务。没有复制跨窗口 admission 对象，也不等进入模型循环才允许停止。迟到的旧 Stop 不取消新 replacement 输入。

审批、问题和 MCP elicitation 使用原 owner 的冻结 request / digest 与真实 waiter。新窗口只接公共 projection，响应由原 owner 校验并结算；不会用新窗口的 bypass 或 session cache 决定原工具是否获准。重复 / 迟到响应仍被原 settlement 拒绝。approval diff 也从原 owner 的 cache 读取。

### 上传与退场

发送窗口先规范化附件引用，并通过 `AttachmentStore.get_payload` 验证实际 conversation + workspace 复合 scope，再让原 owner 消费。没有把 base64、另一工作区 grant、供应商私有数据或新窗口的 browser binding 当作附件所有权。曾错误使用返回空字典的 `get_metadata` 判断不存在，首批负例抓到后改为真正返回 `None` 的 payload 权限边界。

公共 producer 身份与“仍可接新命令的 owner”分开。现有 disconnect retirement 在从 manager 移出旧 session 后仍会进行实际取消 / 收尾；已注册旧 producer 可以发布最后真实 terminal。另一真实 execution 注册后立即替换 producer 身份，旧 owner 迟到的 done 不再投影到新的 follower busy 状态。没有延长旧 session 的生命周期，也没有迁移原生资源。

## 当前文件范围

- `backend/ws/conversation_stream.py`：公共事件 / snapshot 与 host-only receipt origin。
- `manager.py`、`handler.py`、`event_outbox.py`：真实 owner / subscribers、原子快照与独立 transport、窗口自身 runtime。
- `command_scope.py`、`command_dispatcher.py`、`handlers/misc.py`：canonical target、delegated execution / finally、原始 admission Stop、typed controls / 子 agent。
- `approval_runtime.py`、`agent_runner.py`、`handlers/conversation.py`：恢复订阅、原 owner 冻结状态和已提交用户输入的公开投影。
- `frontend/src.v2/chat/runtimeEvents.ts` 复用 `sessionEvents.ts` 中既有 queue / pending-input reducers 处理自己的 runtime snapshot。字体、布局、图标、按钮和动画未调整。
- `handler.send_conversation_list` 同时按另一个 agent 的明确接口协作增加一行 `get_sidebar_order(summaries)`，排序服务属于独立 scope；它不算本报告验证了所有 Sidebar 排序行为。

## 实际验证与失败保留

采用隔离测试目录与受控 asyncio / extension / task producer。真实 `WebSocketSession`、outbox、dispatcher、durable queue、approval waiter、AgentRuntime mailbox / journal 和 scheduler 被执行；本报告中的受控 runner 不调用外部模型。原始用户工作区、上传和未保存 editor buffers 未用于这些单元验证。

新 `backend/tests/test_conversation_window_follower.py` 的 **30 个当前节点** 覆盖：累计 / 后续流、分片 snapshot 顺序、final 在恢复窗口内的竞态、断线恢复、B / Home 后台观察、私有 frames、三类原 waiter 响应和迟到拒绝、实际 Stop turn fence、带真实存储附件的 busy steer / queue、跨 scope 附件拒绝、队列取消 / steer、真实错误、idle sticky-owner 的实际 scheduler 与离线 origin journal reconcile、pre-run slash Stop / replacement、四类 regeneration finally、两种 owner alias、两种子 agent mailbox delivery、resume 的 A 权限 / registry、退场及新 owner 替换。

原有必要回归包含 WS replay / projection、turn input queue、control receipt / elicitation、Stop、retirement、conversation activation、long stream、provider control 和 subagent ownership / resume，共 19 个文件。当前 collector 是 `output/conversation-window-follower-20261015-latest-results.json`。

| 实际执行 | 节点 | Passed | Failed | Error / Skipped | Exit |
| --- | ---: | ---: | ---: | --- | ---: |
| backend batch01 | 203 | 185 | 18 | 0 / 0 | 1 |
| backend batch02 | 249 | 241 | 8 | 0 / 0 | 1 |
| backend batch03 | 297 | 295 | 2 | 0 / 0 | 1 |
| backend batch04，仅两个受影响文件 | 30 | 29 | 1 | 0 / 0 | 1 |
| backend batch05，仅剩余 timing fixture 节点 | 1 | 1 | 0 | 0 / 0 | 0 |

原 XML / log 均保留，文件名前缀为 `output/conversation-window-follower-20261015-batch`。最新统计从完整 batch03 的当前 297 节点出发，只以 batch04 的实际文件结果、batch05 的实际单节点结果替换；不累加重跑。最初无效 fixture 参数 `[thread-b]` 已被真实分配的 ID 参数替代，原失败保留在 `retired_fixture_nodes`，不混入当前 297 项，也不伪记为通过。

失败中除上述真实附件边界和 delegated command context 问题外，还修正了测试本身的假设：cursor 0 与 live-only delta 不是可重放窗口；question answers 必须使用 Source 的 answers 数组结构；原 waiter 的 owner / digest / Guardian 接受顺序不应被错误断言成原始 wire 字典；最小 fixture 显式提供实际 lifecycle / ws_manager / PermissionContext，并由其真正 owner 关闭通知订阅和队列。既有手动 compact fixture 也更新为当前 `manual=True`、真实 context-compaction item ID / 生命周期契约，未为测试添加生产兼容字段。

最后的 selection timing fixture 在前一个 switch await 期间必须保持“输入尚未准入，interrupt 可通过，durable accepted 原输入未绑定新 owner”；accepted 输入此时可以仍在 pending，也可以已经 claimed / inflight。断言检查真实两种阶段的同一输入，再释放 switch 并核实只准入 B，不要求恰好一种调度时机。

前端实际四文件结果保存为 `output/conversation-window-follower-ui-20261015-batch01.json/.log`：218 passed。新增 runtime case 核实 A 的 queued / steered 输入投影到 A 后台缓存时，B reader、cwd、草稿及非流式主区保持。最终合并构建与真实三窗口 QA 由 Root / 桌面验证记录提供；本报告不把准备好的脚本当作已执行通过。

## 范围边界

官方账号登录、授权与依赖官方账号的 hosted 服务依用户要求排除，不计待办或阻塞。本批没有改 last-subscriber 生命周期、原生窗口关闭策略或将原资源迁给 follower，也没有声明远程 host / archived / side-chat 新窗口已支持。真实外部模型语义、原生浏览器 / PTY 操作和完整 UI 一致性须按对应实际桌面场景分别审计。

### 已确认的执行 owner 关闭差距，尚未补齐

Source `thread_processor.rs::connection_closed`（约 3588）移除 connection，只有 core thread 本身已不存在才 finalize；`thread_state.rs::remove_connection`（约 622）仅移除 subscriber；`thread_processor_tests.rs::removing_auto_attached_connection_preserves_listener_for_other_connections` 明确要求另一 subscriber 仍在时保留 listener。

MiniCode 当前 `WebSocketManager.disconnect` 在 30 秒 grace 后仍会无条件 `session_lifecycle.shutdown`，显式 `shutdown_session` 同样把运输窗口退场与执行上下文销毁耦合。因此，**关闭执行 owner 后超过 grace，即使还有 follower，任务仍会取消**。本批修正了该真实取消的 terminal 可见性，没有修成 Source 的继续执行语义。当前 297 项及正在进行的三窗口 QA 均不得算作这个场景通过。

后续须把 conversation execution context 与 WebSocket / renderer 连接生命周期分开：其他真实 subscriber 保留同一个执行 context、权限和 waiter；只释放已关闭窗口的运输关系。不得复制 private runtime 来冒充新 owner，也不能把 browser / native terminal 的窗口绑定换给 follower。仅延长 30 秒超时不是这条差距的修复。Root 已将其纳入后续 follower 范围；为保持当前固定 QA 批次，本报告更新时尚未修改该生产生命周期。
