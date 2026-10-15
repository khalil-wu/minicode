# Source Guardian：父会话检查点、恢复和审查上下文

日期：2026-10-14。Source checkout：`.tmp/codex-src`，提交 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。

## 源码依据

- `codex-rs/core/src/context/guardian_context_mode.rs`、`context_manager/history.rs`：ThreadOwned、Independent、Legacy；冷恢复对真实 reviewer comp_hash 重新选择；live compaction 不因 hash 不同降级 ThreadOwned；Legacy 遇到兼容检查点才能提升并使原审查失效。
- `codex-rs/history/src/compaction_checkpoint.rs`：最新检查点包含不完整检查点；id 为字符串、encrypted_content 非空才可用；不退回更旧的有效项。
- `codex-rs/core/src/guardian/review_session_context.rs`、`review_session_setup.rs`：同步审查允许 ThreadOwned 和 LegacyWithCheckpointReuse 把原始检查点交给后端验证；捕获实际行动时的父上下文；繁忙 reviewer 使用一次性 fork；没有 cursor 的 fork 从 captured checkpoint 启动；恢复最多一次，使用同一份 capture 和原始 deadline。
- `codex-rs/core/src/guardian/input_budget.rs`、`request_budget.rs`：新检查点审查跳过启动压缩；旧 reviewer 需要压缩时重新启动；完整行动和最小授权证据仍无法装入新审查器时明确失败，不能反复恢复或替换为摘要。
- `codex-rs/core/src/guardian/prompt.rs`、`guardian-context/src/{transcript,profile}.rs`：loaded checkpoint 使用当前父模型窗口；保留可信用户限制与明确的 omission；加密 AgentMessage 原样保留或在相同 cursor 位置记录明确 omission；普通 message 只取文本，image/audio、tool_search、compaction、reasoning 不成为同步审查 transcript。
- `codex-rs/ext/guardian-v2/src/sync_reviewer/reviewer_config.rs`：reviewer 显式禁用 TokenBudget、ContextManagement 并重置 startup/default 配置，不能继承父会话启用状态或被模型默认配置反向启用。
- `codex-rs/features/src/lib.rs`：`guardian_reuse_parent_compaction` 是 Stable、默认开启的后端策略，桌面端未自行增加开关。

## 实现与衔接

`backend/agent/guardian/context.py` 保存深复制的 opaque item、实际 endpoint origin 与 issuing model comp_hash。父模型的 native compaction producer 记录实际请求发出时的 hash，模型切换不能覆盖它；旧窗口缺少 hash 时继续保留 None，未知 reviewer override 不借用父模型 hash。

`retained.py` 在完整快照中保存 context mode、原始检查点、真实 transcript cut 和 backup 存在标记。重复的 pinned opaque prefix 不把 cut 移到后续消息；cold restore 不重演 compaction、不推进 cut/history、不伪造 producer。旧快照从现存模型窗口重建原始检查点；没有 transcript backup 的 Legacy/Independent 从 surviving window 建立真实 bounded backup，原有授权遗漏仍如实保留。

没有 backup 时，`restore_requires_parent_context` 按 Source 检查 verified answer 与原始 user/assistant 的 id、turn、role、bounded text 是否确实在原窗口中。不把 `user_messages_incomplete` 单个标记当成足够依据；已有真实 backup 时也不因遗漏标记强行选择 ThreadOwned。

`host.py` 捕获当前模型窗口的真实 user、assistant、手动批准 developer、function/custom call、result、local shell 与 web search 证据。加密 AgentMessage 使用原生 provider item；超出 Source 单项字节预算时只留下明确 omission。`GuardianPrompt` 保持这些原生 item 和文本的顺序；`GuardianContextBuilder` 在原位置拆成实际模型消息，共享 OpenAI serializer 原样发送，不把密文序列化进普通文字、公开事件或界面。

普通 worker 的新输入由实际 QueryEngine admission 记录明确 `input_origin`，随 typed model history 与 cold snapshot 保存；Guardian 把真实 agent 来源记成 assistant context。来源不从消息内容猜测。用户输入仍为 user；子 agent 任务不因主模型使用 user wire role 而变成新的人工授权。真实 parent/worker 注册在独立临时 AgentRuntime 中验证，不复用默认运行时的已封存 run id。

`service.py` 与 `pool.py` 按实际配置、模型、父 history/reset 和对应 root 授权/审查版本复用 reviewer。模型或配置变化、root revocation、reset 会替换原 trunk；busy fork 不提升为主 reviewer。审查器自己的真实压缩使旧 delta cursor 无效；没有父检查点的非 ThreadOwned 模式仍保留 Source 允许的旧 parent_history_version。

预算使用现有 typed LLM estimator，包含实际 history、policy、reviewer 工具 schema、最终输出 schema 和 followup reminder；用户明确 model_context_window 覆盖被保留。对复用 reviewer 的一次恢复继续使用原 captured checkpoint 与原 deadline。新输入仍过大时保留明确失败证据，不发虚构的 summary/checkpoint。

## 验证记录

最新去重结果：**326 passed、0 failed、0 error、0 skipped**。证据为 `output/guardian-checkpoint-source-20261014-latest-results.json`，按各批最新真实 JUnit 节点合并，保留原失败文件。

- `output/guardian-checkpoint-source-20261014-batch01.xml`：真实 exit 1，315 passed、2 failed、0 error、0 skipped，317 项，79.45 秒。两项失败分别是新增 fixture 把 ModelDefinition 的 extra property 当成 dataclass field、旧 native mode fixture 使用已不符合 Source 的 `agent_mode` 与自创 `mode: plan` user wrapper 断言。
- `output/guardian-checkpoint-source-20261014-batch02.xml`：真实 exit 1，211 passed、1 failed、0 error、0 skipped，212 项，78.14 秒。统一重验全部 Guardian、native compaction、producer、manual lifetime、token activation、granular approvals、model request policy；包含七个旧 schema 恢复节点。首批两项失败均通过；新的 worker 入 history fixture 因复用默认全局运行时的固定 run id 与虚构 parent 所有权而失败。
- `output/guardian-checkpoint-source-20261014-batch03-admission.xml`：真实 exit 0，2 passed、0 failed、0 error、0 skipped，7.56 秒。修正 fixture 为独立临时 AgentRuntime 与真实注册 parent/worker，仅重跑两个实际 admission/cold/审查角色节点。原 worker 非 completed 证据保留，未降低断言，也未把中止计入通过。

去重范围：原 Guardian 28、authority chain 9、新 checkpoint chain 56、native compaction 19、producer 6、model runtime 88、adapter factory 26、manual compaction lifetime 9、token activation 26、granular approval 42、model request policy 17。model runtime 与 adapter factory 在首批实际通过且此后没有对应生产变动，未重复运行；受后续 origin/restore 变动影响的范围全部在第二批重验。

两个真实本地 HTTP 协议 fixture（`responses/compact` → snapshot/cold restore → GuardianRuntime/QueryEngine → 实际 `responses` 请求）验证开启复用和明确关闭复用两个分支，均通过。覆盖原始 opaque item、实际 endpoint/producer hash、cross-hash LegacyWithCheckpointReuse、原生 AgentMessage 顺序、读文件 call/result、可信限制、公开事件不泄露 ciphertext。该服务返回显式 fixture 值，并不执行官方解密。

旧 native mode fixture 现在按实际 `build()` 返回的完整请求检查 Source `collaboration_mode`：Plan 与 Default 使用 developer 指令；重复构建和模式切换均不改写已冻结的原始 native 历史，不增加自创用户 mode wrapper。

## 实际服务边界

官方账号下的 opaque 解密、跨 advertised comp_hash 接受/拒绝和服务端真实授权响应：**not_run**，当前没有可使用的官方账号。fixture 验证了实际 HTTP 与 harness 调用链、原始 item/provenance、冷恢复、隐私和失败行为，不能算作官方服务通过，也不能据此声称整个 Codex 桌面端已一致。

服务返回的原始 native item 按其真实类型与来源处理；如果历史服务只返回普通 user item 且没有 agent 来源信息，不按相同文本猜测其身份。完整官方 AgentMessage/opaque 服务身份协议仍需要上述真实账号验证。

后续 Phase 4 的 Async Guardian V2 与增强审查工具仍未闭合，不在本批通过范围内。本报告验证的是同步 Guardian 的上述检查点和事件调用链，并不宣称整个 Guardian 与 Source 全等。
