# Source Guardian V2：Snapshot 异步分类与审批路由

Source checkout：`.tmp/codex-src`，提交 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。本批按实际 `classname + parameterized node` 的最新结果合并，**297 passed、0 failed、0 error、0 skipped**；不累计重叠回归，也不将此前同步 326 个节点计入 Async 结果。实现与验证跨至 2026-10-15，文件名保留本批起始日期 20261014。逐节点结果见 [latest-results.json](../../output/guardian-async-source-20261014-latest-results.json)。

## Source 调用链

`features/src/lib.rs` 中 `guardianv2`、`guardianv2_decisions_comparison`、`guardian_enhanced_node_repl_transcripts`、`guardian_node_repl_transcript_images`、`guardian_conversation_history_tools`、`guardian_root_handoff_context` 均为 UnderDevelopment、默认关闭。它们没有桌面 display_name，本批仅保留真实 backend effective/config gate，不新增桌面开关。

`ext/guardian-v2/src/async_scorer/{extension,observation,classification,score,authorization,coverage,wrapper_lag}.rs` 定义实际 tool lifecycle 观察、Snapshot 捕获、后台分类、原子发布、授权和 lag 复用。分类模型固定 `gpt-5.6-luna`，首个非空 output_text delta 必须整个等于 `high` 或 `low`，风险分别为 1 或 0；后续输出不修改这个决策。普通 classifier 请求没有 tools、main prompts、final-output schema 或自加输出 token 上限。

`async_scorer/{sampler,parent_compaction,config,startup}.rs` 定义 16 并发、最多两次采样重试、8192 output bytes、完整 action 的 10000 token 默认上限、classifier 自己的模型输入预算、parent checkpoint 与 Luna 非空 comp_hash 的严格兼容。ThreadOwned 的 checkpoint 不兼容即回同步；Independent 不借父 checkpoint；Legacy 可省略不兼容或 unusable checkpoint，但 oversized 必须如实失败。

`config/src/guardian.rs` 与 `protocol/src/openai_models/guardian.rs` 定义 model-owned policy 优先于 legacy gate、unknown mode 保留同步、omitted scope disabled。Source `[auto_review].required_on_models` 来自真实 requirements，不从名字或提示词猜；只匹配 exact slug 与 Source 允许的单层 provider namespace。用户不能选 manual reviewer 的约束也要求 Guardian。protected model 禁止普通 adaptive scorer。

`ext/guardian-reviewer/src/routing.rs:104` 精确区分 requireGuardian 和 requireFresh。strict turn grant 要求 Guardian；它本身不等于强制同步。显式 require_synchronous_review、model-required 且 async flag 未开、cancel、retry、Exec RequireEscalated 才要求新的同步审查。`WithAdditionalPermissions` 仍是受沙箱限制的权限扩大，不能当作 RequireEscalated。

## 实现接点

owned `async_config.py`、`async_progress.py`、`async_sampler.py`、`async_service.py` 实现上述 Snapshot 配置、评分生命周期、HTTP classifier 与缓存。模型和 sampler lifetime 包含真实 Luna catalog/runtime/hash/window；当前父授权、root、review revision、模型、配置、实际权限和 environment 都参与复验，不能拿旧 model runtime 的 comp_hash 继续复用。

shared `RunContext`、`GuardianHostState`、pre-tool hook 与 canonical result store_result 负责真实 producer→observer→finish/cleanup。先应用 PreToolUse/extension 的改参，再捕获实际完整 action；同一个真正 call 只观察一次，实际拒绝/失败也结束 active call。Host 从实际 builder context 与 live owner model/permission 读取当前上下文，停止时取消并 await 分类和 drain。

共享 OpenAI raw SSE transport 不进行 main reply 收集、trim 或拼 token；owned sampler 原样采用首段，后续 drain 记录真实 usage 和 error 到 private side-call/audit。首段后的失败不会冒充成功完成，也不能重判已采用的首段。raw SSE 迭代器由 `async with aclosing(events)` 明确持有并关闭，失败、返回及取消路径均结束 HTTP response。真实 usage 通过 `usage_info_from_openai`、`LLMSideCallContext` 与 `record_non_stream_usage` 进入父 turn side-call 记账。图片只来自当前实际模型 history，沿现有 native token estimator 计费，base64 字节不当作全文 token。

classifier route 使用真实 History/Notes auth gate 与 `ModelRuntime.token_budget_auth_context(model)` 的能力事实：要求 `supports_codex_backend_routes`、`requires_openai_auth`，同时不存在 env key、experimental bearer token、普通 auth 或 AWS auth。满足这些条件才附加 `x-codex-guardian: classifier`、清除 service tier 并带入真实 parent response ID；其他请求沿普通 `/responses` 路由保留实际可用的 tier。tier 从父 `ModelExecutionConfig.model_request_policy`，或实际父 adapter 的 `model_request_policy()` 获取，再经过 `service_tier_for_request`，不读取不存在的 `LLMSettings.service_tier`。headers/agentIdentity 路由已用受控 gate fixture 验证，不能等同于官方账号 billing route 实测。

中央 GuardianReviewService 审批接点覆盖主工具、request_permissions、stdin 与网络等 direct consumers；缓存不能绕过冻结 digest、environment 或 cancel 校验。None 保留原同步 reviewer；同步 checkpoint、cold restore 与一次恢复核心没有替换。

## 实际验证与修复证据

| 实际批次 | passed | failed | error | skipped | exit | 原始记录 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| batch01 | 277 | 6 | 0 | 0 | 1 | [JUnit](../../output/guardian-async-source-20261014-batch01.xml)、[log](../../output/guardian-async-source-20261014-batch01.log) |
| batch02 | 57 | 1 | 0 | 0 | 1 | [JUnit](../../output/guardian-async-source-20261014-batch02.xml)、[log](../../output/guardian-async-source-20261014-batch02.log) |
| batch03 | 53 | 0 | 0 | 0 | 0 | [JUnit](../../output/guardian-async-source-20261014-batch03.xml)、[log](../../output/guardian-async-source-20261014-batch03.log) |
| batch04 | 10 | 0 | 0 | 0 | 0 | [JUnit](../../output/guardian-async-source-20261014-batch04.xml)、[log](../../output/guardian-async-source-20261014-batch04.log) |

batch01 六个失败完整保留：四个来自 sampler 读取不存在的 `LLMSettings.service_tier`，现已接通真实 model request policy producer；另两个来自 shared producer fixture 构造 `LLMSettings` 时漏传必填 `api_key`，fixture 已显式提供空 key，未修改实际模型结构。

batch02 唯一失败是 classifier 已发布 `low` 后发生真实 late error，private `record.error` 被转换成泛化中文错误，导致原始证据丢失。修复后 private `provider_error`、`error` 与 sampler audit 保留实际 provider 事件及异常，用户显示路径独立处理。该批 log 同时记录了 `Task was destroyed but it is pending!` 与 `async_generator_athrow`，不是 JUnit 的第二个 failure；根因是 raw SSE generator 的结束归属未完成，现通过 `aclosing` 修复，保留原 log 作为资源泄漏证据。

batch03 的 53 个 owned 节点验证了上述修复、实际 `httpx.Response.is_closed`、first-delta 决策、late-error/usage 记账及审批 freshness。batch04 的 10 个新增 route-gate 节点使用实际注册的 runtime/auth/capability producer，验证 header、tier 与 parent-response attribution。最终仅取每个节点的最新实际执行，合并为 297 passed；四批总数不得相加。

## 保留的真实缺口

- 官方 hosted Apps account/identity 已按用户最新决定移出本轮范围，不计待办或阻塞。Source `core/src/mcp_tool_call/conversation_history.rs` 只允许父线程真实 hosted Apps binding 的 `user_message.search_messages` 与 `read_messages`；本批未验证这些官方账号流程，不使用同名自定义 MCP 冒充。
- MiniCode 尚无实际 Source NodeREPL runner/身份和 enhanced transcript producer。所有当前 MCP 按 MCP scope；不靠 `node_repl`、`cua_repl` 或 `js` 字符串虚构 CUA，Python Code Mode 不注入 Node 政策、不虚构 JS execution。增强 Node policy/history 工具仍未实现。
- WebSocket connection pool/prewarm 未在本批实现；已在后续批次补齐并通过独立统一验证，记录见 [2026-10-15-source-guardian-websocket-pool.md](2026-10-15-source-guardian-websocket-pool.md)。Conversation retained classifier 与 decisions comparison 随后的独立实现/验证见 [2026-10-15-source-guardian-retained-decisions.md](2026-10-15-source-guardian-retained-decisions.md)。本批 297 passed 仍只表示本批范围，不累计后续重叠回归。
- 官方账号解密、classifier Codex billing route、account-owner generation 监听的原始 not_run 只作为历史验证边界保留；按用户最新决定，不再作为本轮待办或阻塞，也不从 token 字符串推测账号身份。
- managed model 的 `ignore_rules` execution-prefix、live model 切换与早期 Skip consumer 尚未全链路接通，不能宣称所有安全命令或既有 allow rule 均已强制走 model-owned Guardian。

本批完成 Snapshot 异步链路的上述实现与验证，不代表整个 Guardian 与 Source 全等。未实现与 not_run 保持单独列出，不能因本批 JUnit 0 skipped 将不可执行的官方账号或 Node 场景算作已验证。
