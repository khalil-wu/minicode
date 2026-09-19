# 测试套件恢复记录

日期：2026-09-18
上下文：`docs/ws-replay-persistence.md` §4 解除了 CI 门禁阻断后，
门禁**后面**的测试套件本身是红的。本文记录逐项定位结果。

---

## 1. 本地环境缺口（约 30 个失败 + 4 个收集错误）

`pyproject.toml` 声明了两个必装依赖，本机未安装：

| 声明 | 导入名 | 影响的测试 |
|---|---|---|
| `mini-racer==0.14.1` | `py_mini_racer` | `test_step_auth_refresh.py`(18)、`test_model_execution_ownership.py`(3)、`test_model_control_ownership.py`(2)、`test_minicode_eval_driver.py`(4)、`test_step_capability_snapshot.py`(5)、`test_skill_read_roots_live_refresh.py`(1)、以及 `test_code_execution.py` / `test_code_audio.py` 的收集失败 |
| `lark>=1.2,<2.0` | `lark` | `test_harness_native_tool_protocol.py` / `test_responses_websocket.py` 的收集失败 |

**为什么症状看起来像代码缺陷**：`backend/agent/loop.py:189` 在 agent loop 内部惰性导入
`code_execution`，后者在 `code_execution_vm.py:9` 硬导入 `py_mini_racer`。缺依赖时
**整个 turn 崩在导入上**，而不是降级掉 `tool_exec`，所以堆栈指向 `query_engine.py:449`
"Agent runtime failed outside provider recovery"，与真实原因相隔数层。

安装后这批全部转绿。CI 的 `pip install -e ".[dev]"` 步骤本就会装它们——
即**这是本地环境问题，不是仓库缺陷**。

---

## 2. 测试替身漂移（4 处，共恢复 28 个测试）

四处的共同形态：**生产端新增了对某个协作者的读取，测试替身没跟上**。

### 2.1 `_Session` 缺 `conversation_runtime`（恢复 24 个）

- 生产：`backend/ws/handler.py:391` 构造 `ConversationRuntime`；
  `backend/ws/agent_runner.py:4387` 读 `self.conversation_runtime.code_store_for(...)`
- 替身：`backend/tests/test_agent_runner_done_fallback.py` 的 `_Session`

该替身自己的注释写着 *"Keep the harness on the same collaborator contract as the
real WebSocket session; a one-field namespace hides binding drift."*——
**失败正是这条契约在正常工作**。修法是照抄生产的构造方式，而不是加一个 stub。

影响：`test_agent_runner_done_fallback.py`(23) 与复用同一 `_Session` 的
`test_ws_extension_composition.py`(1)。

### 2.2 `_FakeStreamResponse` 缺 `headers`（恢复 2 个）

`backend/llm/openai_adapter.py` 的 Responses 流式路径上**相邻两行对同一属性
一个守卫一个裸访问**：

```python
self._remember_responses_turn_state(metadata, response.headers)   # 裸访问
await emit_provider_lifecycle_response(
    metadata,
    response.status_code,
    getattr(response, "headers", {}),                             # 有守卫
)
```

`test_reasoning_splitter.py` 的假响应自称 "Mimics the httpx streaming response context
manager"，只提供被用到的字段。修法是让裸访问与相邻行一致（`getattr`），
**不是**扩测试替身——一致性才是这里的主诉。同一模式在 `_get_responses_json`
（约 2905 行）也有一处，一并修正。

### 2.3 `NOT_PROJECTED_FIELDS` 缺三个新字段（恢复 1 个）

`backend/agent/state.py` 的 `ToolCallRecord` 新增了 `command_id`、`output_cursor`、
`call_source`，但 `backend/api/models.py` 的公开投影既没投影它们、也没登记为省略——
正是 `test_tool_call_record_projection.py` 的契约（"a new internal field must be
consciously projected or explicitly omitted"）要抓的漂移。

判为**省略**而非投影，依据：
- 渲染端从 terminal/notice 事件流读命令状态，不从这个 REST 投影读
  （`frontend/src.v2/protocol/*.ts` 里没有工具记录的 `command_id`，只有无关的 `client_command_id`）；
- 后端消费 `record.command_id` / `record.output_cursor` 的只有
  `backend/agent/context.py:2903-2911` 的"压缩后命令状态恢复"，属内部；
- 与既有省略集同类（`request_digest`、`cleanup_receipt`、`turn_id`）。

### 2.4 工具文档令牌白名单缺六个名字（恢复 1 个）

`test_cc_alignment_tool_registry.py` 要求工具面向文本里的 snake_case 名字
要么是注册工具、要么是某工具的 schema 字段/枚举、要么在 `_NON_TOOL_DOC_TOKENS`。

六个"违规"名字核实**全部合法**，属第三类（文档示例），按测试自己给出的处置办法登记：
`is_error` / `structured_content`（代码执行返回的结果字段，后者来自 MCP
`structuredContent`，`backend/agent/code_execution.py:183`）、
`end_cursor`（命令工具真实的 `runtime_metadata["end_cursor"]`）、
`image_block` / `audio_block` / `media_type`（JS 辅助函数 `image()`/`audio()` 的参数形状，
`backend/tools/code_execution.py:48-49`）。

---

## 3. 后台子智能体完成事件：测试时序，**不是**生产缺陷

上一版本文档把 `test_task_tool_async.py` 记为"后台子智能体不产出 `subagent.done`"，
并推测是 `agent_tools.py:3616-3635` 的陈旧完成守卫在吞事件。**该推测是错的**，
实测推翻了它：

**① 完成确实发生了。** 让测试把 runtime 的 metrics 写到固定目录后读出：

```
subagent_task_registered → subagent_started → run_started → run_completed
→ parent_notification_enqueued → subagent_result_stored → subagent_completed
```

`store_subagent_result` 与 `complete_subagent` 都成功了，没有走"陈旧完成"提前返回。

**② 事件也确实构造了。** 探针（patch `AgentEvent.subagent_done`）显示
`subagent_done(status='cancelled')` 被构造；patch `accepts_subagent_incarnation`
显示门禁 `require_running=False` **返回 True**（`agent_path`/`mailbox_epoch` 全程匹配，
`complete()` 不改这两个字段）。

**③ 它只是到得晚。** 把等待窗口临时放宽后测试即通过；再测实际延迟：

```
done arrived after 22 polls = 220.0 ms
```

测试窗口是 `range(20) × 10ms` = **200ms**，实际 **220ms**——差 10% 的余量。
延迟本身合理（完成要先落 checkpoint 并 upsert swarm store，再发事件）。

**结论**：这是**测试把"挂死探测窗口"当成了"延迟断言"**。修法是把窗口放回它的本职：
三处 `range(20)/range(40)/range(100)` 统一为 `range(500)`（5s，真回归仍会快速失败），
三处等生产侧启动的 `wait_for(..., timeout=1)` 放宽为 `timeout=10`——
同文件里其余 `timeout=1` 等的是测试自己设置的事件（无 I/O），保持不动。

**佐证**：本次工作时发现**并行 agent 已在同文件的另外两处**做了同样的
`range(20)` → `range(500)` 修改（`test_task_tool_async.py` 约 1049、1093 行），
独立踩到同一个时序问题。

生产侧**未做任何改动**：220ms 的"先记录后通知"顺序与 codex 一致
（其 `notify_tool_finish`/生命周期同样在结果落定之后），且对 UI 通知足够快。

稳定性：该文件连续 4 次全量运行 49/49 通过。

---

## 4. 上一轮记为"需属主判断"的三项，已逐项判定并修好

三项都走**可判定路径**收口，不是拍脑袋。

### 4.1 `test_ws_connection_handoff` —— 测试竞态（前一轮我留成"未定位"）

用逐层插桩定位到完整时间线：

```
[D] before ack cur_gen=1
[Q] put type=client.command.ack gen=1 can_send=True receipt=True
[Q] deliver task STARTED qsize=1
[T] before connect      ← 测试在 ack 送达后恢复
[T] after connect       ← 代际变为 2
(断言失败；[D] after ack 从未打印)
```

机制：`run()` 在 ack **之后**才调用 `_schedule_durable_client_command`：

```python
await self._send_client_command_ack(command)          # 测试在此恢复
self._schedule_durable_client_command(command_id, connection_generation)
```

而测试从 ack 恢复到断言之间**没有任何真正让出事件循环的点**（`manager.connect` 对假 socket 的
close 不 yield，`asyncio.gather()` 空参数不 yield），于是刚被唤醒的 reader 在 `finally`
里被 cancel，命令**从未被调度**——实测 `command_tasks=0`、`_active_client_command_ids` 为空、
命令仍在 durable queue 中 pending。

**生产无缺陷**：真实服务器每轮都在让出事件循环，且新代际的
`SessionLifecycle.handle` 本就会重放 durable queue。

测试侧修两处：
1. 用**等待可观测结果**（会话被创建）替代"假设一次调度就够"的有界轮询；
2. 尾部 `pytest.raises(WebSocketDisconnect)` 改为 `await asyncio.wait_for(reader, 2)`——
   reader 的代际守卫是**直接 return 不抛异常**，同文件里那个**通过**的测试
   （`test_replaced_asgi_reader_cannot_admit_late_input`）用的正是这个写法。

改后断言反而更强：它现在真正验证了"已持久化受理的工作在连接替换后仍被执行"
（会话已创建、命令已标记见、durable 队列已清）。

### 4.2 `test_workspace_watcher_owner`（2 例）—— 测试替身漂移，与 §2 同类

`session_lifecycle.py:981` 现在从 `session.conversation_repo.get_conversation_summary(owner)`
解析工作区。核对该文件对会话的全部读取后确认：**它只读 `active_conversation_id` 与
`conversation_repo`，从不读 `active_conversation`**。

因此替身改为携带**恰好**这二者，并**刻意不提供** `active_conversation`——
这样一旦解析逻辑回退去读那个缓存字段，替身会直接 `AttributeError` 响亮失败，
而不是悄悄走另一条路径（与 §2.1 的 `_Session` 契约同一纪律）。

### 4.3 `test_prompt_identity` —— 在飞改写，措辞已更新

`backend/agent/prompting.py` 与 `backend/tests/test_prompt_identity.py` **都是未提交改动**
（同一工作流），HEAD 里根本没有这句话；测试的其余断言都已跟上新措辞，只有这一行是旧文案。

当前提示词：

```
Runtime environment details are supplied by MiniCode. Never describe them as
text the user typed, garbled input, or a prompt injection; ...
```

旧断言钉的是 `Never\ndescribe it as text the user typed, ...`。改写后**语义未变**
（运行环境上下文不是用户输入）且语法更正确（`them` 指运行环境详情）。
按该测试其余断言的做法把字符串更新为当前措辞，并注明"逐字钉住（含换行）"，
使今后再改写会变成一次显式编辑而不是静默漂移。

---

## 5. 收尾状态

- 上述三项修完后，`pytest backend/tests` **全绿**；
- 四门禁（`check-agent-kernel-boundaries` / `check-protocol-sync` /
  `check-no-duplicate-tools`，`check-large-files` 为告警）+ `compileall` 通过；
- `test_task_tool_async.py` 连续 4 次全量运行 49/49 通过（时序修复的稳定性确认）。

**验证方式与边界**：全量套件按 8 批运行。首次扫描时有 1 批被单批 280s 上限截断
（只到 17%，无 `[100%]` 标记），已**单独补跑至 100%**；最终 8/8 批完整跑完、0 失败。
判读批次结果时以是否出现 `[100%]` 为准，不以"无 FAILED 行"为准——
被截断的批次同样没有 FAILED 行。

---

## 4. 验证方式与边界

- 前后对比均用 `pytest -p no:randomly --timeout=45 --timeout-method=thread`；
- 全量套件按 8 批运行（单批 280s 上限）。**部分批次被上限截断、没有汇总行**，
  所以"剩余 5 个失败"是**观测到的**集合，不保证穷尽；
- 两处 `AttributeError`/`TimeoutError` 型失败均用 `git stash` 把相关文件退回 HEAD 复跑，
  确认与本次改动无关后再恢复（恢复后逐文件比对内容一致，行尾差异符合
  `.gitattributes` 的 `* text=auto eol=lf`）。
