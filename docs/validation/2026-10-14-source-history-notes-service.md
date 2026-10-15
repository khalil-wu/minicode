# Codex Source History/Notes 服务

日期：2026-10-14。参考 checkout：`.tmp/codex-src`，提交 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。

## 源码依据

- `codex-rs/ext/history-notes/src/extension.rs`：TokenBudget 的 `use_history_notes_extension`、OpenAI provider 与真实 Codex backend auth gate、每窗口 `thread_hint`、UTF-8 Bytes(4000) 限额。
- `codex-rs/ext/history-notes/src/backend.rs`：真实 HTTP POST、35 秒请求超时、trusted session/agent context、输出预算与加密参数 headers。
- `codex-rs/ext/history-notes/src/tools.rs`：history/notes 两个 namespace、九个 DirectModelOnly 工具、并发规则、完整参数和描述、加密输出与图片附件、剥图片后的 hook JSON。
- `codex-rs/core/src/tools/handlers/extension_tools.rs`：扩展调用直接交给 handler，不增加参数属性的本地 schema 验证，保留 Source 的前后 hook 契约。
- `codex-rs/core/src/session/mod.rs:4281`、`codex-rs/codex-mcp/src/{runtime,connection_manager}.rs`、`codex-rs/rmcp-client/src/rmcp_client.rs`：仅原生 Notes 配置关闭时调用实际注册的 `notes/thread_hint` legacy bridge，等待已有 startup，以 `_meta.threadId` 标记真实 thread，拼接非空 text。
- `codex-rs/app-server/tests/suite/v2/history_notes_extension.rs`：namespace、加密字段、每窗口提示、失败不注入替代提示。
- `codex-rs/protocol/src/auth.rs`：真实 API auth mode 的 `uses_codex_backend` 枚举；`protocol.rs`：`{"mode":"bytes|tokens","limit":...}` 序列化。
- `codex-rs/models-manager/src/model_info.rs`：无 catalog 声明时的 Bytes(10000) 输出政策。
- `codex-rs/http-client/src/client_builder.rs`：默认允许重定向；原生 HTTP transport 使用十次重定向上限。

## 实现

`backend/agent/history_notes.py` 负责服务请求、配置/认证 gate、工具注册和窗口提示。`backend/tools/history_notes.py` 实现九个工具；`history_notes_schemas.json` 保存上述 Source 的原文参数与 namespace 描述，已包含在 `backend.tools` 的 `*.json` 打包规则中。

ModelRuntime 从实际 provider 名称与登录 producer 保存的 API auth mode 判断服务身份；API key、模型名称、看起来像 backend 的 URL 都不会激活扩展。每次服务请求重新通过现有 OAuth auth owner 解析凭据、base_url 和 headers。模型传入的 `context` 被 trusted session/agent identity 覆盖，输入原对象保持不变。

四个操作（history/notes 搜索、notes 追加和替换）使用 `x-openai-encrypted-tool-arguments: true`；每次请求携带 Source 格式的 `x-openai-tool-output-truncation-policy`。35 秒完整 HTTP 请求预算从实际请求执行开始，认证解析与 transport 构造属于前置阶段。服务请求沿用 MiniCode 已有 provider proxy 配置。

所有工具声明 `private_model_only=True` 和 `direct_model_only`。虚拟 notes 路径没有文件权限含义，私有状态操作对环境声明 `read_only=True`。Source 的 `supports_parallel_tool_calls` 仍独立约束 notes 写入按顺序执行，不能把“不并行”误判为项目文件修改。notes 写入不进入普通外部写入审批，也不使用幂等结果缓存；工具不覆盖 `check_permission`，用户明确配置的 ask/deny 继续由真实 PermissionChecker 决定。模型看到完整 Source 参数；执行端只要求 JSON object，其余参数语义交给远端服务。

原生 `history_notes_output` 保留服务已执行输出预算后的文本、`encrypted_content` 和独立 `input_image`；不把 ciphertext 再截断，不转公开 artifact，不进入 Code Mode。图片在序列化日志/hook JSON 前移除，Source 支持的 original/low/detail 缺省均保留。共享 namespace、public projection、窗口状态、快照与 journal 的衔接由主任务实现，完整任务链验证由 `test_codex_history_notes_chain.py` 独立负责。

`contribute_legacy_notes_thread_context` 使用已存在的 MCP manager、真实注册的 `notes/thread_hint` 和原始 SDK RPC。它等待已准入的连接尝试，不新建 server；request owner 由 session/conversation/manager 提供，`_meta.threadId` 来自真实会话。无 arguments 的请求按 Source 省略该 wire 字段。legacy text 按 Source 保留非空原文并拼接换行，不套用原生 Notes 的 Bytes(4000) 限额。原生配置开启但 auth 不符或请求失败时不会调用 legacy。实际 RPC/transport 错误按 Client 已归一化的 typed result 不注入；远端 CallToolResult 自带 `isError` 的 text 则保持 Source 本来的处理方式。内部程序错误继续上抛，不能吞掉后伪装为空提示。

## 验证结果

最新 producer 范围：`backend/tests/test_history_notes_service.py`，**61 个去重用例通过，0 failed，0 error，0 skipped**。

- JUnit：`output/history-notes-service-20261014-batch04.xml`（41 passed）、`output/history-notes-service-20261014-batch07-legacy.xml`（7 passed，41 deselected）、`output/history-notes-service-20261014-batch09-plan-after.xml`（13 passed，48 deselected）。deselected 是已验证且本轮未重跑的节点，不计为 skipped。
- 去重明细：`output/history-notes-service-20261014-latest-results.json`。
- 测试使用临时目录、实际 ModelRuntime 注册与认证解析、明确标记的测试登录凭据和本机受控真实 HTTP server。它不读取、修改或登录正常用户账号。
- 覆盖全部九个 endpoint、解析后的 auth/base_url/headers、trusted 身份、Source 精确预算 header、真实请求超时、默认同源 307 重定向、登出与非 object 不发请求、HTTP/无效 JSON 错误、4000 UTF-8 字节边界、模型/Code Mode/虚拟路径边界、注册保持与切换 owner、原生加密/图片/无图片 hook JSON。
- 新增 legacy 范围用实际 Python stdio MCP 进程验证 text/remote isError/empty/RPC error、长于 4000 字节的文本、请求 owner 与 thread metadata、无 arguments 的 Source wire、等待真实 startup、无 server 与缺工具不调用、内部程序错误不吞。
- 新增 Plan 范围使用真实 PermissionChecker 检查 write/append 的 default/ask/deny 六个节点；真实 SDK Plan query 在 read-only agent scope 中执行两种写入；同一 provider batch 的两次真实服务写入观测最大在途数量为 1。四个真实 QueryEngine 审批节点分别批准/拒绝 write 与 append，确认存在恰好一个公开 control request、审批 digest 等于最终 request digest、真实 callback 收到该 call ID、仅批准后才产生一次真实 HTTP、日常私有 call/result 仍不公开。

历史失败独立保留，不能算通过：batch01 的 fixture 缺少 runtime 要求的 OAuth login callback（32 failed / 8 passed）；batch02 的计时 fixture 把认证/transport 初始化错误计入 HTTP timeout（1 failed / 39 passed）；batch03 修复后 40 passed。batch04 增加 Source 默认重定向覆盖后为 41 个去重用例；batch05 的 6 个 legacy 节点通过，batch06 的 4 个 metadata 节点按 Source omission 更新后通过；batch07 统一 legacy 与新增内部错误节点共 7 个通过。历史重复节点不累加。

Plan 的 before 证据保存在 `output/history-notes-service-20261014-batch08-plan-before.xml`：5 failed / 4 passed。其中两个显式 ask 被工具的 unconditional AUTO 覆盖；write/append 的两个实际 Plan query 和顺序场景因 `read_only=False` 的 agent scope 误分类而没有发出实际 HTTP。修复私有虚拟状态的环境声明并移除权限 override 后，batch09 新增审批交互覆盖共 13 个节点全部通过。Source 依据为 history-notes 的 parallel flag、extension adapter 直接 dispatch 且没有普通 filesystem/外部写入 guard；不引用当前 checkout 不存在的旧 `is_mutating` 接口。

完整 Query/Responses、public event/inventory 隐私、private journal 与冷恢复链的结果需查看对应独立链测试记录；此 producer 报告不提前将其记为通过。

## 外部实测限制

本轮没有使用可调用官方 alpha/history/v2 与 alpha/notes/v2 的真实 Codex 服务账号。上述结果证明真实 transport 与本地 harness 的契约，不证明官方服务当前对某账号可用；官方服务权限、返回数据与真实加密载荷的端到端实测仍受外部账号条件限制。未创造本地笔记系统代替该服务，也未把这个限制记为测试跳过或测试通过。
