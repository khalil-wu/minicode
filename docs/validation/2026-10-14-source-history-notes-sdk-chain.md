# Source History/Notes SDK 完整调用链收尾

日期：2026-10-14。参考 `.tmp/codex-src` 提交 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。

当前按测试节点取最新结果：**193 passed，0 failed，0 error，0 skipped**，共 **193 个去重节点**。机器记录为 `output/source-notes-sdk-chain-20261014-latest-results.json`。

统一批次 `output/source-notes-sdk-chain-20261014-final.xml` 实测 192 passed / 1 failed。唯一失败是旧 EOF/cancel 夹具未提供实际 ContextBuilder 的 `token_budget_enabled` 字段；补齐明确的 `False` 后，只重跑失败节点，`output/source-notes-sdk-chain-20261014-final-fixture.xml` 为 1 passed，保留了 EOF 必须失败和 consumer close 必须取消的原断言。两个结果按节点合并，不把重跑算成额外覆盖。

## 验证范围

- 新增 `backend/tests/test_codex_history_notes_chain.py`：15 个完整 SDK/HTTP 用例。
- `backend/tests/test_history_notes_service.py`：61 个服务、认证、Source 参数、原生/legacy hint、Plan、并发与审批用例。
- SDK session/admission、context window/chat usage/WebSocket、token budget activation、原生 tool search 实际 Responses 链：69 个节点。
- Conversation payload/工作区失败夹具：5 个节点。
- Provider harness owner、stream settlement、raw stream passthrough、stream text state：43 个诊断与终态回归节点。

新旧批次覆盖重叠，不与之前的 363 passed / 19 skipped 原生工具链报告直接相加。历史未参数化的 SDK fork 节点已被两种真实 conversation owner 场景替代，未计入当前节点数。

## 完整任务链证据

本机 `ThreadingHTTPServer` 提供真实 Responses SSE 和九种 Source Notes endpoint；任务经过公开 `SDKSession.query`、实际 OpenAI adapter、ModelRuntime 认证解析、真实工具执行、private journal、cold replay 和持久化 public conversation projection。凭据是明确的测试 OAuth producer，模型响应受控，不读写正常用户登录资料。

- streamed 和 terminal-only 的 `notes/read_file` 均与同名普通 filesystem `read_file` 共存；重复 arguments-done 不导致重复执行或错误冲突。请求保持原生 namespace/leaf/call ID，journal 与冷恢复保持同样身份。
- 加密输出、plain 文本、图片和 HTTP 503 分别经过实际下一轮 Responses 请求；ciphertext 与原生 `history_notes_output` 不被替代成前端 JSON。hook 消费真实剥图 JSON。
- 真实 `thread_hint` 位于 `<context_window>` 内；实际 `new_context` 创建新窗口后重新获取。cold SDK restore 使用已签发的同一 hint；same/different conversation 的 SDK fork 按真实 owner 决定是否获取新 hint，不把临时 SDK session ID 当作 Source thread。
- Notes 请求 context、Responses `client_metadata.session_id` 和 header 的 thread identity 一致，Source agent 名称为 `/root`；`x-codex-turn-metadata` 与 body 中同名元数据一致。预算 header、actor auth 与 history ingest 来自真实配置/认证 producer。
- auth、config 和显式 adapter gate 关闭后，下一次请求移除 Notes namespace 且不发送替代提示。普通 SDK 即使能访问 auth storage，也不能从 URL、模型名或存储存在性推断 Notes 身份。
- 公开事件、inventory、runtime metadata、budget sources、持久化 conversation 都没有私有工具名称、path、hint/body/ciphertext 或 call ID。Code VM 的实际 `ALL_TOOLS` 不暴露 DirectModelOnly Notes 工具。
- SDK `stream_event`、`inspector.update`、`item.completed` 与 `done` 全部核对私有标识；同时强制普通 `read_file` 在 stream/inspector 的原始诊断中可见，普通错误保留，混合场景最终请求仍计入两个 `function_call_output`，不能通过删除全部记录或改变计数获得通过。

## 本轮修复的真实根因

SDK 丢弃注入的 ModelExecutionSnapshot 会失去实际 ModelRuntime/auth owner；namespace 在 arguments-done 缺失时错误地把同一调用当作冲突；JSON hook 输出被当作字符串截取。以上均在真实公开 SDK 链发现，修复后继续检查原生请求体与 journal。

私有工具标识原先从执行 spans、SDK raw provider traces、public budget source labels 和终态 provider metadata 进入公开记录。修复以实际注册工具与 private receipt 为依据，持久化私有标记，公开投影仅移除对应 named facts。Source 完整 native body、原始 private raw/journal、实际 token/item counts 和普通工具诊断均保留。

Source Notes producer 的独立实现与源码依据详见 `docs/validation/2026-10-14-source-history-notes-service.md`；此前 Code Mode/MCP 原生链详见 `docs/validation/2026-10-14-source-native-harness-fixtures.md`。

## 历史验证记录

| 批次 | 通过 | 失败 | Error | 跳过 |
|---|---:|---:|---:|---:|
| batch01 | 74 | 14 | 0 | 0 |
| batch02 | 3 | 11 | 0 | 0 |
| batch03 | 75 | 8 | 0 | 0 |
| batch04 | 0 | 15 | 0 | 0 |
| batch05 | 76 | 8 | 0 | 0 |
| batch06 | 0 | 8 | 0 | 0 |
| batch07 | 0 | 8 | 0 | 0 |
| batch08 | 0 | 8 | 0 | 0 |
| batch09 | 0 | 8 | 0 | 0 |
| batch10 | 0 | 8 | 0 | 0 |
| batch11 | 8 | 0 | 0 | 0 |
| final | 192 | 1 | 0 | 0 |
| final-fixture | 1 | 0 | 0 | 0 |

历史失败 XML 均保留。01 的新增链失败来自夹具错用 LLMSettings headers 参数；随后批次分别暴露真实 namespace、auth/config ownership、hook、private receipt/source label 和多个 raw/public 终态消费者问题。04 保留生产 `event.tool_call` 字段误用回归；后续修复使用实际 `event.tool_calls` / `tool_call_start`。08—10 没有通过，仅作为明确投影泄漏证据保留；11 的八个失败节点复验全部通过。

## 尚未验证的外部条件

没有使用具备官方 alpha History/Notes 服务权限的真实 Codex 账号。这里证明实际 SDK/HTTP/harness 契约和公开/私有衔接；官方服务可用性与真实加密数据的端到端账号实测尚未完成，未记为 skipped 或 passed。此报告也不代表全部 Codex 桌面端视觉与后端能力已经一比一完成。
