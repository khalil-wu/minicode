# 端到端会话门禁：真后端抓包 → 渲染端原样回放

日期：2026-09-20
范围：`scripts/capture_session_gate.py`（新）、`frontend/src.v2/hooks/useWebSocket.session-gate.test.tsx`（新）、
`frontend/src.v2/hooks/__fixtures__session_gate.json`（新）、`frontend/src.v2/chat/transcriptHydration.ts`（修）
前置：`docs/harness-audit-2026-09-19.md` §8（同类方法首次用于定位「点新建任务没反应」P0）

---

## 1. 为什么要这道门禁

09-19 的两个 P0 都是单测全绿、真机死：后端与渲染端各自的单测都用手写事件，
两端对同一条 wire 事件的假设一旦漂移，没有任何测试能看见。§8 的修法是把真后端
抓到的 21 条事件原样喂给 `useWebSocketConnection`，这一次把它固化成覆盖完整会话
生命周期的门禁，并接进 CI 的前端 job（`npm run test` 即 vitest 全量，测试文件在
`src.v2/` 下自动纳入）。

## 2. 抓包脚本

`scripts/capture_session_gate.py` 用 `websockets` 直连一个已启动的后端，在**同一个
session id** 上走两条连接：

| 连接 | 客户端动作 | 目的 |
|---|---|---|
| c0 | `conversation.list`/`commands.list`/`skills.list`（首次启动无 restore） | 与渲染端首连行为一致 |
| c0 | `conversation.create` A（绑定工作区，bypass） | 建会话 |
| c0 | `user_message` #1「跑 pytest、找 bug、修、复跑」 | 带工具回合 |
| c0 | `user_message` #2（长任务）→ 运行中 `conversation.create` B（会激活 B）→ `conversation.switch` A → `interrupt`(turn_id+message_id) | 运行中 create/switch、带围栏中断 |
| （断连） | 关闭 socket | 模拟网络断 |
| c1 | `session.restore(last_seq, last_conversation_id, last_workspace_root)` → `commands.list`/`skills.list` → `conversation.list` | 重连恢复 |
| c1 | `user_message` #3「Reply OK」 | 恢复后会话仍可用 |

脚本镜像了渲染端的耐久游标规则（`NON_REPLAYABLE_CURSOR_EVENT_TYPES` + `session.*` +
瞬态 provider reasoning）来算 `last_seq`；回放测试会断言渲染端自己算出来的
`last_seq` 与抓包时发出的相等，所以镜像若过时，门禁会直接报出来。

每条命令记录 `sent_after_events`（发出时已收到的事件数），回放时在同一位置驱动
渲染端，保证「运行中 create」确实发生在流式期间。

fixture 已把 `api_key` 字段替换为 `***`；实测 `.env` 里只有 `SUPERTOKEN_BASE_URL`
出现在 fixture 中（provider base_url，本来就在设置页可见）。

### 复现步骤

```bash
# 1. 隔离 state root 启动真后端（模板 .tmp/e2e-gate/start_backend.sh）
MINICODE_STATE_ROOT=<isolated> MINICODE_BACKEND_PORT=8123 LLM_PROVIDER=custom \
CUSTOM_BASE_URL=... CUSTOM_MODEL=glm-5.3-flash CUSTOM_WIRE_API=chat CUSTOM_API_KEY=... \
python -m backend
# 工作区需先写入 <isolated>/data/trusted_workspaces.json，并放一个 add() 写成 a - b 的 calc.py + test_calc.py
# 2. 抓包
python scripts/capture_session_gate.py --port 8123 --workspace <ws> \
  --out frontend/src.v2/hooks/__fixtures__session_gate.json
# 3. 回放
cd frontend && npx vitest run src.v2/hooks/useWebSocket.session-gate.test.tsx
```

## 3. 回放测试

`useWebSocket.session-gate.test.tsx` 用 `MockWebSocket` 替换全局 WebSocket，挂载真实的
`useWebSocketConnection`，命令**由渲染端自己的入口发出**：

- `user_message` → `sendChatMessage`（传入抓包用的 assistant/user message id）
- `conversation.create` → `sendClientCommandAwaitResult`
- `conversation.switch` → store 的 `requestConversationSwitch`
- `interrupt` → `buildInterruptCommand(store)`，即 Composer/ChatTurn 停止按钮走的同一函数
- `session.restore`/`conversation.list`/`commands.list`/`skills.list` 由 hook 自己发

抓到的事件里引用的 `client_command_id` 是脚本生成的，回放前按「命令类型 + 同连接内序号」
改写成渲染端实际发出的 id（`rekeyEvent`），改写不到的记入 `unmapped` 并断言为空。
命令合并发送走 microtask，所以每一步 `await act(async)`。

断言（全部对着抓包数据，不手写期望）：

- 渲染端发出的 `user_message` 去掉 `client_command_id` 后与抓包命令**逐字段相等**
  （content、workspace_root、permission_mode、agent_mode、ids）
- `interrupt` 携带的 `turn_id`/`message_id` 等于抓包时的
- 断连时游标 = 抓包 c0 游标；重连 `session.restore.last_seq` = 抓包 c1 的
- 终态：`connectionPhase=connected`、活动会话 = A、会话列表含 A 与 B、无任何 streaming 标志、
  socket 未被 resync 关闭、`console.error` 为空
- 回合 1：`terminalStatus=completed`，tool_call 块 id 集合 = 抓包 tool_result 的 id 集合
  （8 个：list_files、read_file×2、run_command×4、edit_file），无 running/pending
- 回合 2：`terminalStatus=interrupted`
- 回合 3：`completed` 且答案含 OK；A 的 user 消息 id 顺序 = 三条抓包 id

## 4. 门禁挡住的问题（已修）

**中断且未产出任何内容的助手记录在重载后消失。** 真机链路：`interrupt` 命中回合 2
时模型还没产出文本或工具调用，后端落盘的记录是
`{"id":"a_gate_2","content":"","terminal_status":"cancelled","termination_reason":"user_interrupted"}`；
重连 `session.restored`/`conversation.switched` 携带的 transcript 也含这条。渲染端
`transcriptHydration.ts` 的投影循环把「无 content、无 blocks、无 artifacts、非 failed」
的消息全部丢弃，于是回放后 A 里只剩 `u_gate_2` 单独一条 user 消息，用户看到的是
「这轮没被回答」，而断连前的活视图显示的是「已停止」。

三家对照：cc 把中断写成一条 user 消息 `[Request interrupted by user]`
（`cc/src/utils/messages.ts:207,545`），重载后仍可见；codex 的历史投影把
`TurnAborted` 投成 `TurnStatus::Interrupted` 保留在 changed_turns
（`app-server-protocol/src/protocol/thread_history_projection.rs:54-68`，
测试 `projects_identified_turn_aborts`）。两家都不会因为回合无产出而抹掉「被中断」这一事实。

修法：`transcriptHydration.ts` 的丢弃条件补 `terminalStatus !== "interrupted"`，
中断记录进入投影后由 `chatSurfaceState` → `project-turn.ts:mapStatus` 渲染成「已停止」。
单测 `transcriptHydration.test.ts` 新增一条；门禁改前在回合 2 断言处失败、改后通过。

## 4b. 顺带发现：main 在 c9e802f8 之后 root 测试有 6 个红

按铁律 4 结束前跑全量时发现 `tests/`（root 半）6 个失败，在 c9e802f8 的父提交 aea9007f 上
用 worktree 复跑全绿，即 09-19 那批提交漏跑了 root 半。逐个追进去：

| 测试 | 原因 | 处理 |
|---|---|---|
| `test_phase0_security_guards::test_reasoning_effort_config_is_not_persisted_for_deepseek_chat` | `_model_execution_owner` 新读 `session.ws_manager`，测试替身没这个属性 | 替身补 `ws_manager = None`（与 `test_llm_config_events.py:25` 一致） |
| `test_runtime_architecture` 两条 `fake_foreground` | `_execute_foreground` 新增 `max_chars` 关键字 | 替身补参数 |
| `test_smoke_websocket` 两条 `control_request` | `user_message` 后现在多发一条带 `conversation_id` 的 `llm.model.updated`（会话级 model 投影，预期行为），测试按位置取事件 | 改用文件里已有的 `_receive_control_request(ws, id)` / `_receive_next_type` 按类型取 |
| `test_regressions_agent_loop::test_run_agent_loop_does_not_replay_timeout_after_partial_text` | 09-19 把"已出可见文本则不重试"改成"文本是投机的，重试前用 `item.completed(status=cancelled)` 撤回"（`provider_stream_transport.py` safe_to_replay 只看已提交工具效果）。旧测试断言 `calls == 1` 是旧契约 | 重写为断言新契约：4 次调用、前 3 个 agent_message item 以 cancelled 空文本关闭、最后一个 partial、done=partial |

最后一条核过上游：cc `services/api/claude.ts:2332` 空闲看门狗中止后走非流式重试，不因已收到
文本而放弃；codex `session/turn.rs:2367` 把流中途关闭映射为 `CodexErr::Stream` 走
`responses_retry.rs` 的重试阶梯，同样不看已发的 delta。渲染端对 `status=cancelled` 的
item.completed 已有撤回测试（`chatStreamEvents.test.ts:778`）。

这些都是测试替身/断言随生产代码漂移，不是生产 bug；但 main 红了一天没人知道，
说明"后端分两半跑"的第二半在 09-19 被跳过了。

## 5. 顺带实测的数字（glm-5.3-flash，chat 线，supertoken）

| 项 | 值 |
|---|---|
| 抓包事件 | c0 896 条、c1 34 条，31 种类型，全部在 `SERVER_EVENT_TYPES` 内 |
| 回合 1 provider 调用 | 8 次，首字节 10.8–13.5s（模型侧推理），chunk 间最大间隔 31–610ms |
| prompt cache 命中率 | 93.9–99.3%；一次 `[PromptCacheBreak] prompt unchanged`（供应商侧驱逐），一次 `turn-aborted marker changed`（中断后预期） |
| 回放耗时 | 约 0.3s（930 条事件） |

## 6. 排除项（核过不是缺口）

- 运行中 `conversation.create` 激活 B 时 A 的流继续进 `conversationStreaming[A]`，
  切回 A 后 `stream_resume` 正确接回；`done(cancelled)` 落到 A。
- 重连后 `session.restored` 的 `last_seq/current_seq/cursor_reset=false/replayed_events=0`
  与渲染端游标完全一致，`conversation_switched_follows=true` 的两段式恢复正常。
- `interrupt` 围栏：渲染端从 `agent.run.started` 绑定的 `turnId` 与后端 run id 一致。

## 7. 维护

- 后端事件形状或渲染端命令形状一旦变化，先重跑 §2 抓包再提交 fixture，不要手改 JSON。
- fixture 1.26MB（紧凑 JSON）。若再增长，优先在脚本里裁掉 `agent_message.delta`
  之外的高频瞬态事件，而不是删断言。
- 已知未覆盖：审批阻塞（`control_request`）、压缩触发、多 agent；可按同一脚本追加连接段。
