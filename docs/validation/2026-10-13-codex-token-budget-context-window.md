# Codex TokenBudget 核心工具与窗口生命周期（2026-10-13）

参考 checkout：`.tmp/codex-src`，commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。下列路径相对 `.tmp/codex-src/codex-rs/`。本轮实现的是明确开启 TokenBudget 时的核心工具、无总结窗口切换及真实生产存证；不能由此推导所有模型、Notes 或 Guardian 已等同。

## Source 调用链

- `features/src/lib.rs::Feature::TokenBudget`，默认 false，UnderDevelopment。`core/src/tools/spec_plan.rs` 仅 feature 开启时注册两工具；`NewContextWindowHandler` 为 DirectModelOnly，`GetContextRemainingHandler` 为通常可调用的函数工具。
- `core/src/tools/handlers/new_context_window_spec.rs` 实际模型名为 **new_context**；不是文件名 new_context_window。参数为无字段 object、additionalProperties=false、strict=false。handler 只请求窗口切换，回 `A new context window will start without summarizing conversation history.`，不调用摘要模型。
- `get_context_remaining_spec.rs` 同一无参 schema，Code Mode 的 output_schema 为 `{tokens_left: integer|null}`。handler 直接函数输出原文 `You have N tokens left in this context window.`，未知时 N 为 unknown；Code Mode 才返回结构对象。
- `core/src/session/context_window.rs` 按当前 active usage、auto-compact scope 和模型 hard cap 取最小剩余量。`protocol/src/openai_models.rs::ModelInfo` 默认 base limit 为模型窗口的 90%，hard usable cap 为默认 95%；BodyAfterPrefix 减去窗口首个输入 baseline。`context_manager/history.rs::get_total_token_usage` 使用最近 completed response 的 input+output，再估算最后模型项之后新增的真实局部项。
- `session/turn.rs` 等当前 sampling 和全部工具完成后才消费 request；有后续工作时切换。`compact_token_budget.rs` 先执行 PreCompact，再发 ContextCompaction 开始、安装新窗口、发完成，然后执行 PostCompact。`session/mod.rs::start_new_context_window` 安装当前 initial/world-state 上下文并持久化 checkpoint，不总结旧历史。
- `state/auto_compact_window.rs` 保存 first/previous/current UUID v7，切换窗口重置 prefill 与一次性 reminder/fallback 状态。`session/token_budget.rs::maybe_record` 使用配置原文，threshold reminder 和 fallback 各每窗口一次，主动 new_context 跳过 fallback。

## MiniCode 实际链

`feature_flags.py` → `services/tool_registry_factory.py` → `tools/context_window_tools.py` → turn-owned `RunContext.context_builder` → `ContextBuilder` window state → `TurnIterationExecutor` sampling/tool settlement → `token_budget_runtime` → `QueryJournalRecorder` / WS canonical commit → live process block / public transcript / REST / child journal replay。

- 默认不开启；不注册 new_context_window 别名，不把原 compact 换名。声明 feature true 时才注册原两工具，managed requirements 的关闭仍优先。配置表 enabled、scope、threshold、guidance、fallback 以 Source 字段读取；没有 provider 声明窗口时返回 null，不拿应用 fallback 的 1M 窗口冒充模型上限。
- UI 的 response reserve 不参与这对工具的剩余量。独立 window state 记录最近模型 input+output、prefill 和 source 默认 cap；追加项按 Source 的 UTF-8 可见字节近似计数。Code Mode 保留原 JSON 结构和 output_schema，直接调用/状态/模型历史保留原英文，不追加 XML 或 wall_time。
- 新窗口从当前权限/协作模式、环境、真实选中 Skill 和 host initial developer 层继续。旧用户输入、图片/引用、模型响应与工具原结果全部留在 `inactive_context_windows` 的完整旧 checkpoint 和原 journal。退出 active context 的内容不会再以摘要、tool tail 或假用户提醒注入。
- 读时 hash 属于 active provider read provenance：旧值留 archive，active 值清掉，防止 write/notebook 用已退出当前窗口的读事实授权。文件字节、真实 background commands、当前 extension values/消费 cursor 及真实待投递队列保留；只对旧历史 undo 索引做 rebase。
- 空 summary 只有真实 `window_number + 1`、first/previous/current ID 关系与旧 archive 一致时才允许生产 CAS commit。普通未产生摘要的 compact 仍报真实错误。Repository 保存 active snapshot 不替换用户可见 transcript；独立 manual 操作追加真实 system/process item，与 checkpoint 同一个 CAS 提交，不伪造 assistant reply。

## 真实前端 item 与证据边界

Source 两 handler 没有发普通工具 UI item；`tools/registry.rs::CoreToolRuntime` 的 builtin-control 默认 false，`tools/lifecycle.rs` 只通知 extension contributors，`event_mapping.rs::parse_turn_item` 的普通 FunctionCall/FunctionCallOutput 返回 None。`app-server-protocol/src/protocol/thread_history.rs` 的 raw response 历史重建也不会把它们造为普通活动。真正显示的是 ContextCompaction 生命周期；原模型工具结果仍留 rollout。

MiniCode 成功 tool_call/result 以 tool-owned `visibility=debug` 保留原始事实；已有真实失败链仍保留详情。切换窗口的事件为：

- `item.started/completed`：item `{id, type: context_compaction, source: auto|manual, status, started_at, finished_at}`，时间为真实整数 epoch 毫秒；auto 有实际 assistant owner，manual 使用独立 operation turn_id，不借当前 assistant。
- `context_compacted`：summary 可以为空，带同一个 `compaction_item_id`、before/after 计数和实际 context_window_state。预算兼容通知和 UI 状态复用该 id，不重复造摘要 notice。
- WS/AgentTurnState 还原唯一 process block `{id, itemKind: context_compaction, content: '', source, status, startedAt, finishedAt, timestamp}`；timestamp 保留开始时间。public projection 保留空 content 和时间；journal 保存原 item 生命周期，child replay 使用同一 reducer。缺少真实完成而 turn 已结束的记录保持 interrupted，不能伪造成 completed。

侧聊边界另外直接消费真实 record.conversation_type/parent_conversation_id，经 agent_runner → populate_prompt_context → initial developer 注入实际桌面 `$Os` 原文；不会把 inherited history 当新 active 任务。该 developer 边界在新窗口继续生效，未新增自写 user mode 或猜测 subagent 禁用表。原文位于 `backend/agent/codex_prompt_resources/side-chat-boundary.md`；创建、独立 source owner 和原 pending request 隔离由 sidebar 区域 agent 的真实 WS 测试另证。

## 尚未冒称等同的范围

- Source Notes/native history extension 与旧 notes.thread_hint MCP bridge 是可选依赖；MiniCode未接其 OpenAI backend/auth 接口，未虚构 Notes 已连接。Source 的不安装 Notes 集成测试也可以启动新窗口，因此核心工具未因外部 Notes 缺失伪装成 compact。
- Source model-owned TokenBudget 自动启用、ChatGPT Plus/Pro experimental-context admission 和模型自有 reminder/fallback 默认配置未接同一 catalog/auth 全链。本轮只用明确 feature flag 及当前实际选中 adapter 的已声明窗口验证。
- Source RetainClientDeveloperMessages 默认 false，且要求 metadata.client_authored provenance。MiniCode现有 SDK/extension 没有该同一 producer，不凭 role=developer 猜 client 身份；现有 host developer、override 和 persistent context 仍由当前 initial 层保留。
- 本地 byte estimate不是外部模型 tokenizer 的精确值；原 Source 也明确使用近似。PDF/native document 项沿现有 adapter 的估算，没有在本轮宣称为 Source 相同媒体项。外部模型、MCP/OAuth/Notes、非默认 model effective-percent 和外部 provider 额外 reasoning 计数仍需各自真实验证。
- 自动窗口任务的真实 Pre/PostCompact stop 已用本地 HookResult 验证。独立 manual 仍使用 MiniCode 现有任务/配置 hook 装配边界，没有宣称已复刻 Source 所有 compact SessionStart 或第三方 hook 行为。

## 运行记录

所有运行保留 log 和 JUnit XML，失败与修复后的定向复验分别保存，不能把失败或未跑算通过。模型使用受控本地 LLMAdapter；文件、编辑、PowerShell 命令、QueryEngine、WS dispatcher/outbox、CAS repository、SQLite/journal、ASGI REST handler 与快照恢复使用真实实现。没有操作或激活用户桌面窗口，没有调用公网模型、清除用户历史或写入用户会话库。

- `output/playwright/codex-control-audit-20261013/token-budget-run01/`：250项，246通过、4失败、0跳过。4项为新增fixture漏必需 api_key 或误用 journal phase 键；不计完整通过。
- `token-budget-run02/`：108项，100通过、8失败、0跳过。新增reducer fixture 参数错误；manual system记录误用 content_blocks 而实际公共协议为 blocks；真实WS模型fixture identity不一致；旧manual断言尚未计入新真实system process。生产协议差异已定位并修复，最终以定向复验记录为准。
- 后续结果继续按当前testcase IDs去重，不把重叠批次相加。前端 Source glyph、10秒说明、完整 offscreen UI 核验由 root 另证，本报告不把后端通过冒充截图/UI全量通过。

最终去重结果：7个已保存批次按 testcase.classname/name 的最近结果取值，**316项通过、0项未解决失败、0跳过**。不是将批次相加，也不是全部 backend/UI 控件的通过数。最新新核心两文件 `token-budget-run06` 为32项通过；最后真实 WS auto/manual `token-budget-run07` 为2项通过、exit 0、无 task-destroyed 警告。

| 后续批次 | 实际结果 | 真实发现与处理 |
| --- | --- | --- |
| token-budget-run03 | 15项，13通过、2失败、0跳过 | 已修实际 blocks 公共协议/新system操作原历史断言；WS model fixture缺真实catalog/load-config声明；manual事务clone在读取旧无window字段snapshot时清掉已初始化feature state，暴露真正初始窗口生命周期差异。 |
| token-budget-run04 | 5项，4通过、1失败、0跳过 | manual完整WS独立操作、真实命令/journal/快照通过；auto真实WS揭示partial UI先保存同一个新window后canonical commit错误拒绝的问题。 |
| token-budget-run05 | 2项通过、0失败/跳过 | 生产auto commit现在必须证明archive真实+1，并仅在core window_state receipt和当前/Repo新window完全一致时接受已投影窗口；普通空summary、缺receipt或错误receipt仍拒绝。真实WS新window后active prompt不含旧task，旧用户、读文件结果、回复在session/REST/reopen全部保留。该轮pytest退出仍有无关memory-startup借用任务警告，原log保留，不能称无警告。 |
| token-budget-run06 | 新核心32项全部通过 | 同一当前源码统一复验核心两文件；仍有fixture event-loop在memory store/prune任务结束前关闭的警告，不以功能通过掩盖此退出证据。 |
| token-budget-run07 | 真实WS auto/manual 2项全部通过、exit 0、无退出警告 | fixture明确memory disabled，同时按真实conversation owner等待已安排的memory-startup prune/store工作结束，再关闭event loop；没有改生产memory代码，也没有虚称外部Memory模型/第三方依赖通过。 |

生产消费细节：`context_compacted` 在 core completion 之前交给消费者持久化；auto WS consumer 完成同 owner CAS 后才接收 item.completed。manual成功 item 与完整新 snapshot、原 transcript 在一个 CAS 事务保存。真实运行中的 partial projection可能先安装相同window，故auto路径使用core精确receipt，而manual事务仍严格要求此次 before→after advance。公共消息协议实际使用 **blocks**；system记录和原assistant工具记录都使用这一真实协议，未新增 content_blocks 兼容别名。

## 生产 Chat 缺少 usage 帧的真实修复（追加）

`production-control-run13/14` 的受控 Chat 服务没有发送 usage。原 Chat producer 正确保留 `StreamEvent.usage=None`，`ProviderAttempt.usage_reported` 也正确为 false；但 settlement 把用于聚合统计的默认 `UsageInfo(0)` 无条件当成 provider 报告，覆盖了先前真实 prompt estimate 并把 prefill 错标为 server observed，导致 get_context_remaining 返回满 90000。

Source `core/src/session/mod.rs:4737::record_token_usage_info` 只在 `Some(token_usage)` 时覆盖计数并升级 server prefill；`None` 不做这两步。`:4785::recompute_token_usage` 是另一个明确历史替换/估算生命周期，不是把未报告用量当 0。本轮仅修 `provider_stream_settlement.py`：复用实际 producer 已设置的 `ProviderAttempt.usage_reported`，未报告传真实 None；明确报告的 UsageInfo(0) 仍接受为真正 0。没有加 total>0 推测、改 billing 数值、伪造 usage 或通用 UI fallback。

实际小批：`output/playwright/codex-control-audit-20261013/token-budget-usage-run01/`，**14项通过、0失败、0跳过、exit 0、无task-destroyed警告**；该范围不与前述316直接相加。真正本地 HTTP Chat/SSE → OpenAIAdapter → code-mode-only VM → new_context → followup，分别验证缺usage、明确0和正常usage：缺usage保留正estimate、prefill仍未标为server；0是真实0；正常值是真正input+output。另真正Chat+VM+nested events+WS case发出ContextCompaction完成、后续agent_message完成和done，Repo保存最终回复。Source输出/估算恢复语义得到验证，不冒称外部模型tokenizer计数精确。

终态另一个生产问题已经与缺usage分离：`production-context-diagnostic-run01/renderer-warnings.json` 明确记录前端旧semantic validator拒绝 `conversation.compaction.updated` / `context_compacted` 的合法空summary并关闭WS、重连后再次拒绝replay。对应desktop log中后端后续iteration已真正收到Chat回复并完成checkpoint。这是Root修复的前端规范化/validator链；本agent没有据此改NestedToolEvents或通过延长等待冒充终态通过。
