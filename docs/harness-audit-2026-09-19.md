# 2026-09-19 Harness 审计修复：流式截断、熔断阶梯、多 agent 邮箱、记忆引用、时区

日期：2026-09-19
上游对照：codex `.tmp/codex-src/codex-rs`、cc `cc/src`
前置：`docs/command-policy-argv.md`（同日，危险命令 argv 底料）

每项均先实测复现再改，复现脚本在 `.tmp/audit_repro_*.py`。

---

## 1. 流式答案被 sanitizer 截断（P0）

`backend/agent/stream_sanitizer.py` 的 `ThinkingStreamSanitizer` 在看到 `<|`、`<think`、
`<internal` 这类前缀时无限期扣住后续文本等待标签闭合，`finish()` 再把扣住的全部丢弃。
实测（`project_provider_text_chunk` 真路径）：

| 提供方发送 | 实时增量 | 落库答案 |
|---|---|---|
| `In F# use \`f <| x\` ...`（94 字符） | 13 字符 | 13 字符 |
| `I <think the loop is fine...`（95 字符） | 2 字符 | 2 字符 |

cc（`services/api/claude.ts:2134`）和 codex（`core/src/session/turn.rs:2677`）都原样转发
text delta，没有流式标签清洗。修法：悬挂前缀超过一个真实标签的最大长度即放行；`finish()`
把未成形的前缀作为可见文本吐出。真实 `<think>...</think>` 块仍被剥离。
测试：`test_reasoning_splitter.py` 新增两条。

## 2. 熔断/重试阶梯四处

### 2.1 连接阶段重试轨在生产路径不可达

`docs/provider-connect-retry.md` 描述的轨道以 `is_connection_not_established(cause)` 为闸，
需要异常**类型**。但三个适配器和 `base.py:307` 的共享包装都把异常吞成 ERROR StreamEvent，
`provider_stream_error_event.py` 只拿到字符串，类型名判定恒 False。实测：WiFi 断开时
消耗 10 次请求预算后回合失败，而不是等网络。原有测试全部手工构造
`ProviderStreamFailure(httpx.ConnectError)`，生产从不走这条路。

codex `codex-api/src/api_bridge.rs:183` 把 `TransportError::Connection` 保成类型化错误直到
`responses_retry.rs:58` 的重试决策。修法：`llm_error_raw` 在构造 ERROR 事件时写入
`raw["connect_phase"]=True`；ERROR 阶梯据此走 `plan_connection_retry`（5s→60s，不动请求序号）。
实测：4 轮等待 5/10/20/40s，`stream_attempt` 保持 0；去掉标记后 3 次预算用尽即 finish。

### 2.2 `server_error` 判不可重试

`errors.py` 结构化信号与关键词表都没有 `server_error`。codex `sse/responses.rs:453` 把未识别的
`response.failed` 一律映射为 `ApiError::Retryable`。已加入 `server_error`/`internal_error`
→ network 可重试。实测两种线协议都从"首次即终止"变为"重试 attempt 1"。

### 2.3 忽略消息正文里的 "try again in Ns"

OpenAI TPM/RPM 429 只在消息里给重置时间，无 `Retry-After` 头。codex
`sse/responses.rs:681` `try_parse_retry_after` 正则解析。已加 `retry_after_from_message`，
两处入口（HTTP 异常链、Responses `response.failed`）都写入 `retry_after_seconds`。
实测：等待从 0.5s 变为服务器要求的 11.05s。

### 2.4 Anthropic "input length and max_tokens exceed context limit" 未识别

cc `withRetry.ts:561` 专门解析此 400。已把 `exceed context limit` 加入 prompt_too_long 标记
（分类器 + `error_withholding`），现在走上下文溢出恢复而不是通用失败。

测试：`backend/tests/test_provider_failure_ladder.py`（4 条，全部用真适配器 + MockTransport）。

## 3. 多 agent 邮箱五处

| 缺陷 | 上游 | 修法 |
|---|---|---|
| teammate→leader 消息不唤醒空闲 leader（`run_manager.py:866` 只看 parent outbox） | cc `useInboxPoller.ts:843` 空闲即提交新 turn | `send_swarm_message` 对 parent 收件人同时写一条 `kind=mailbox_wake` 的 outbox 标记；leader 回合里 `inject_parent_notifications` 遇到该 kind 只 ack 不渲染，正文仍由邮箱 claim 投递 |
| 原始协议 JSON 当指令喂给 leader，idle 通知不去重 | cc `attachments.ts:3590,3645` | `mailbox_delivery` 过滤 `_STRUCTURED_PROTOCOL_TYPES`（直接 ack），idle 通知按 teammate 只留最新并渲染成人话 |
| `shutdown_request` 的 `from` 只认 run id，而所有信封写 `team-lead` | cc `SendMessageTool.ts:275` | 消费端接受 `{parent_run_id, "team-lead"}` |
| `team_delete` 删掉仍在运行的 teammate | cc `TeamDeleteTool.ts:79` 拒绝 | 新增 `runtime.running_team_members`；有运行成员则报错并提示先发 shutdown_request |
| `task_update` 指派不通知被指派者 | cc `TaskUpdateTool.ts:276` | 指派变更时向被指派者邮箱发一条带 `task_id` 的人话消息 |

实测（`.tmp/audit_repro_swarm.py`）：注入条数 5→1（只剩最新 idle 通知，人话渲染）；
指派后 worker 可 claim 1 条；`team_delete` 拒绝；outbox 出现 6 条 pending 唤醒标记。
测试：`test_swarm_tools.py` 新增 3 条。

## 4. 记忆引用是死管道

解析器（`memory/citations.py`）、流式捕获（`stream_sanitizer.py`）、使用记账
（`job_store.record_stage1_output_usage`）都在，但 `memory/templates/read_path.md`
从不要求模型输出 `<minicode-memory-citation>`。于是 `last_usage` 永远为空，Phase 2 按
`PHASE2_UNUSED_RETENTION_DAYS=30` 把所有 30 天前的 stage-1 记忆全部清掉，不管是否天天在用。
codex `ext/memories/templates/memories/read_path.md:75-113` 明确要求引用块。
已在模板追加同结构要求；测试保证模板里的示例能被解析器解析。

## 5. 提示词时区乱码

`ContextBuilder.__init__` 用 `datetime.tzname()`，Windows 中文区域下是 ANSI 代码页解码错的
`�й���׼ʱ��`，每轮都进 `<timezone>`。cc 用 `Intl.DateTimeFormat().resolvedOptions().timeZone`。
改为 `local_timezone_name()`：`TZ` 环境变量 → tzlocal 的 IANA 名 → `UTC±HH:MM`。
`tzlocal` 进主依赖。测试断言结果是 ASCII 的 IANA 名或偏移。

## 6. 前置 commentary 每个 delta 重发整段快照

Responses `message_phase=commentary` 在首个工具提交前的叙述走 `maybe_stream_process_text`，
每个 provider chunk 都发一条携带**全部累计内容**的 `agent.item`（`stream_attempt.py:244`）。
`agent.item` 可重放，于是每条快照都进 ws-event-log、`stream_state` 和强制部分持久化。
实测 400 chunk / 4800 字符：400 条 `agent.item`，1,111,200 字节，231x。
codex 用 `AgentMessageContentDeltaEvent{delta}`（`protocol.rs:1955`）。

修法：新增 live-only 事件 `agent.item.delta {item_id, delta}`。首个 chunk 仍发完整 `agent.item`
（running），之后只发增量；终态 flush 仍发 completed 快照。`stream_resume` 的 content_blocks
已携带 running process 块，与 `agent_message.delta` 同一套重连保障，故加入
`LIVE_ONLY_EVENT_TYPES`。前端 `appendProcessItemDelta` 只扩展已宣告的块，未宣告的忽略。
两端注册点：`ws/events.py`、`handler.py`、`agent_runner.py`、`event_envelope.py`、
`payload_contracts.py`、`stream_state.py`；`protocol/events.ts`、`streaming-types.ts`、
`server-event-validation.ts`、`useWebSocket.ts`、`chatStreamEvents.ts`、`stores/types.ts`、`chat-slice.ts`。
`check-protocol-sync.py` 通过。改后同样输入：1 条 `agent.item` + 399 条 delta，27,117 字节，5.6x。

## 未做 / 已排除

- `send_message` 到 `*` 被工具拒绝但 store 支持广播；`summary` 参数不持久化。P3。
- PowerShell 脚本仍未走 AST（见 command-policy-argv.md）。

## 7. 真机验证（glm-5.3-flash via supertoken，chat 线）

任务：在 2 文件工作区里"跑测试、找 bug、修、复跑"。`backend.evals.minicode_driver` 驱动，
完整走权限、沙箱回退、工具流。

- 结果：模型 6 次工具调用（read×2、pytest、edit、pytest），把 `a - b` 改成 `a + b`，最终答案
  引用了失败/通过计数，并说明沙箱镜像不可用时回退主机执行。`done.status=completed`。
- 每次 provider 调用 5 条 SSE 摘要：首字节 5.6–9.3s（模型侧推理），chunk 间最大间隔 0.2–2.1s，
  prompt cache 命中率 92–98%（跨迭代前缀稳定的实证）。
- item 生命周期：3 段 commentary 各自 `item.started(pending)` → `item.completed(commentary)`，
  答案 `item.completed(model_final)`；tool_call 先 pending 后 running 各一条，无重复无乱序。
- 输出含 U+2014 破折号，字节正确（终端 cp936 显示为 �� 是显示问题，非数据问题）。

未覆盖：Electron 渲染帧率、中断/重连的真机交互。这些只能开桌面端看。

## 8. 桌面端"点新建任务没反应"（真机复现，P0）

现象：桌面端一个回合运行中，点"新建任务"十次毫无反应，回合结束后仍不可用。

证据链（`%APPDATA%\minicode-desktop\desktop.log` + `data/ws-event-log/<session>.jsonl` +
`client-command-log`）：10 条 `conversation.create` 都被后端处理并回了 `command.result success`，
渲染端一条没应用。desktop.log 在启动 1 秒后有一条 renderer 报错
`[ws] Failed to apply server event; leaving it replayable`。

根因（用真后端 + 桌面真实数据目录抓包，再把 21 条 wire 事件原样喂给 `useWebSocketConnection`
复现，见 `frontend/src.v2/hooks/useWebSocket.launch-replay.test.tsx`）：

- 09-18 的 `docs/ws-replay-persistence.md` 把 `agent_message.delta`、`tool_output_delta`、
  `commands.list` 加入后端 `LIVE_ONLY_EVENT_TYPES`：不进重放日志，因此**不带**
  `previous_replay_seq`，但仍带线上 `seq`。
- 前端 `useWebSocket.ts` 的 `NON_REPLAYABLE_CURSOR_EVENT_TYPES` 没同步。渲染端收到
  `commands.list seq=3176` 后把耐久游标推到 3176；下一条耐久事件
  `conversation.hydration.updated seq=3181 previous_replay_seq=3175` 与游标不连续，
  `assertInboundReplayCursorContinuity` 抛错 → `markInboundEventFailed` 记一个 replay hole
  → `shouldProcessInboundEvent` 之后把**所有** seq > 3181 的事件静默丢弃。此后所有
  `conversation.switched`/`conversation.list`/`command.result` 都不再应用，UI 看起来"卡死"。
- 这是"上游有成熟机制而自制更差"的一例：后端有 `check-protocol-sync.py` 门禁维护事件集合
  两端同步，但这两张互为镜像的集合没有纳入门禁，于是一端改动另一端静默漂移。

修法：
1. 前端集合补齐 `agent_message.delta`、`agent.item.delta`、`tool_output_delta`、`commands.list`。
2. `scripts/check-protocol-sync.py` 新增 `NON_REPLAYABLE_CURSOR_EVENT_TYPES` 与后端
   `NON_REPLAYABLE_EVENT_TYPES ∪ LIVE_ONLY_EVENT_TYPES` 的漂移检查（AST 解析，注释里的
   反引号不再误读为条目）。
3. 修正 `useWebSocket.test.ts` 一条断言了错误行为的旧测试。

验证：回放测试改前在 seq 3181 报错、游标停在 3176、后续 create 全丢；改后 21 条全应用，
active 切到新会话。门禁 `[OK] Non-replayable cursor set: 15 entries match`。

排除项：`conversation.create` 后端处理、`session.restore` 游标重置握手、命令调度器锁、
`conversation.list` 修订号门都逐一核过，均正常。
