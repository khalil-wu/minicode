# 协议同步门禁扩面：两端镜像的值集合

日期：2026-09-20
范围：`scripts/check-protocol-sync.py`（+21 项检查）、`backend/ws/events.py`（9 处过期 Literal）、
`backend/llm/model_selection.py`、`frontend/src.v2/protocol/server-event-validation.ts`（内联数组提升为具名集合）、
`frontend/src.v2/protocol/streaming-types.ts`、`frontend/src.v2/stores/types.ts`、`frontend/src.v2/chat/runtimeEvents.ts`
前置：`docs/harness-audit-2026-09-19.md` §8（同一门禁上一次扩面：非重放游标集合）

---

## 1. 基线

门禁原本只核 10 项：事件名、命令名、三个 `AGENT_PROGRESS_*`、TypedDict **字段名**、注册命令、
字面事件、非重放游标集合、会话投影不变量。字段**值**（status/source/phase 这类枚举）一项都不在。
09-19 的 P0 正是这种"两端各改一边、没门禁看见"的漂移，所以本轮把能解析的镜像集合全部纳入。

## 2. 扫描方法与结论

先派一个只读 agent 扫全仓（19 个候选，每条给两端 file:line），我逐条追进函数体核实。
按"两端是否都有可解析的单一声明"分三类：

**A. 两端都是具名声明，直接进门禁（21 项）**

| 对 | 后端 | 前端 | 扫描时状态 |
|---|---|---|---|
| `AGENT_PROGRESS_PROVIDER_STATES` | `message.py` frozenset | `streaming-types.ts` as const | 一致，未门禁 |
| `_AGENT_MESSAGE_COMPLETION_STATUSES` / `_THINKING_LIFECYCLES` / `_AGENT_ITEM_STATUSES` / `_AGENT_ITEM_VISIBILITIES` / `_DONE_STATUSES` | `message.py` frozenset | `server-event-validation.ts` 内联数组（本轮提升为具名 `new Set`） | done 前端多 `interrupted`（后端从不发，`agent_runner.py:5547` 映射成 cancelled） |
| `_RUNTIME_SPAN_STATUSES` / `_TOOL_RUNTIME_SPAN_EVENTS` | `runtime_spans.py` | `server-event-validation.ts` 具名 Set | 一致，未门禁 |
| `PERMISSION_MODES` | `permissions/checker.py` + 4 份后端副本 | `stores/types.ts` `PermissionMode` | 一致；后端 5 份副本互核也进门禁 |
| `REASONING_LEVEL_ORDER` ⊇ `_SUPPORTED_AGENT_EFFORT_LEVELS` | `model_selection.py` / `agents/loader.py` | `stores/types.ts` `EffortLevel` | **前端 `none` vs 后端 `off`；`ultra` 后端序列缺失**（`model_catalog.py:21` 声明了 ultra，`clamp_model_thinking_level` 遇到不在序列里的值退到 `available[0]` 而不是就近） |
| `events.py` 9 个 TypedDict Literal ↔ `message.py`/`runtime_spans.py`/`runtime_records.py` 权威集合 | 后端内部 | — | **全部过期**：`AgentMessageItemData.status` 缺 cancelled/failed、`source` 缺 pending；`AgentProgressData.stage` 缺 image_generation/cache、`status` 缺 partial；`RuntimeSpanData.status` 缺 4 个；`AgentItemData.status` 缺 3 个、`kind` 缺 hook_response/async_hook（`hooks/manager.py:149,1745` 在发）；`AgentRunData.phase` 多 `verify`（无发射者，`runtime_records.py:37` 没有）；`SubagentDoneData.status` 缺 interrupted |
| `InspectorUpdateData.target_kind` | `events.py` | `stores/types.ts` `InspectorTargetKind` | 后端缺 `cache`（`cache_metrics.py:80` 在发） |
| `Control*RequestData.subtype` | `events.py` | `common-types.ts` `Control*Request` + `ControlRequestPayload` | 后端缺 `conversation_resources_cleanup`（`handlers/conversation.py:1447` 在发） |

**B. 真 bug（1 条，已修）**

`subagent.done` 的 `status="interrupted"` 前端渲染成"done"。链路：进程重启后 `runtime.py:356`
把未完成的子 agent 记为 interrupted → `subagent_service.py:78` 任何非 pending/running/blocked 状态都走
`subagent_done(status=status)`（我用 `build_subagent_status_event` 直接复现，事件 status 就是 `interrupted`）
→ 前端 `runtimeEvents.ts:1187` 只认 partial/cancelled/failed/error，其余一律 `"done"`。
用户重启桌面端后看到被打断的子任务显示为已完成。修：interrupted 与 cancelled 同路；
`runtimeEvents.test.ts` 新增一条。TS 联合与后端 TypedDict 同步补 interrupted。

**C. 两端都是散落的 if/else，不纳入门禁，记录结论**

- tool_call `status`/`transition`：后端 `state.py:22` Literal 是持久化口径（含 legacy `error`），
  前端 `tool-call-reducer.ts:5` 是 UI 口径（含 pending/running）；`stream_state.py:44` 入站归一化
  `error→failed` 等。设计上不对称，不该强行镜像。
- `command.result.level`：后端只发 success/info/warning/error；前端多判一个 `failed`（从不发，无害）。
- `error.error_type`/`error_code`：后端 ~40 处发射无集合；前端分支的 6 个值全部核过有发射者；
  `autocompact_circuit_open`/`prompt_too_long` 走通用分支。
- WebSocket close code：后端发 1000/1008/1011/1012，**从不发 4001/4003**；前端 `PERMANENT_CLOSE_CODES`
  里的 4001/4003 是死分支，1011/1012 走重连（正确）。留着不动，改动无收益。
- `rate_limit.error_type`：后端只发 rate_limit/busy；前端多 quota_exceeded/concurrency_limit（无发射者）。
- `stream_resume.phase`：后端散落，前端存而不判。

## 3. 门禁新增的解析器

`parse_typescript_set`（`new Set([...])`）、`parse_typescript_union`（`export type X = "a" | "b"`，
忽略开放 `string` 成员）、`parse_python_literal_in`（任意模块的 `NAME = Literal[...]`）、
`parse_python_typeddict_field_literal`（AST 取 `class C: field: Literal[...]`）、
`parse_python_typeddict_subtypes`、`parse_python_assignment_strings`（普通与带注解赋值都收）。
全部 AST 或锚定正则，注释里的引号不会被误读。

## 4. 验证

- `python scripts/check-protocol-sync.py`：31 项全 OK（原 10 + 新 21）。改前会报 3 处 DRIFT
  （EffortLevel none/off/ultra、InspectorTargetKind cache、ControlRequest subtypes）加 9 处 events.py。
- 前端 tsc 0 错；`src.v2/protocol`、`runtimeEvents`、`chatStreamEvents`、`FooterRow`、`hooks` 共 17 文件 316 用例通过。
- 后端两半 + root 见提交信息。

## 5. 顺带

`EffortLevel` 的 `"none"` 在前端从未被赋值（composer 缺省 medium，FooterRow 只列 low…ultra），
改成后端的 `"off"` 无行为变化。`REASONING_LEVEL_ORDER` 补 `ultra` 后，`clamp_model_thinking_level("ultra")`
在模型不支持时就近降到 max 而不是掉到最低档；`model_thinking_levels` 对带 `thinking_level_map` 的模型
把 ultra 与 xhigh/max 同样处理（未显式映射就不列出），否则 agent 编辑器目录会凭空多出 ultra 档
（`test_agent_editor_service.py` 抓到）。
