# Source 增强 REPL 证据：后续真实调用链边界

状态：本文件是当前源码只读审计，不表示该能力已经实现或运行通过。当前三项生产批（原生命令策略、物理回合投影、执行生命周期）验收前不据此增加控件或工具。

Source：`.tmp/codex-src`，`822e58cc3d666166c7446c5b1ea2e52f5d09594c`。

## 实际入口

`codex-rs/protocol/src/mcp.rs:42–52` 精确识别已注册 raw server `node_repl` / `cua_repl`，或 raw server `codex_apps` 且注册 connector ID 为 `connector_openai_browser`。不按 tool arguments、provider metadata、工具名中的关键词推测身份。

`core/src/tools/handlers/mcp.rs:318–439::on_tool_result_accepted` 只在实际 CodeMode 嵌套调用、已接受且成功的真实 MCP 结果上捕获。普通 MiniCode 本地 `functions.exec` / CodeVM 不是该 producer；不能将任意 JavaScript、任意 MCP 或伪造 TrustedTool marker 变成 Node/CUA 身份。

当前 MiniCode 已有实际 `MCPToolProxy` 的 raw identity、冻结 catalog、CodeMode call source 和完整 native result；但尚无对应 thread-owned REPL store / accepted-result capture / Guardian delivery cursor。`guardian_enhanced_node_repl_transcripts` 与 `guardian_node_repl_transcript_images` 当前均默认 false。已有同名 flag 和 policy 资源不能算该链完成。

## 必须保留的 Source 行为

- `core/src/context/node_repl_review_evidence.rs:28–39`：真实 admitted model 的 computer-use review 要求可选择 multimodal；否则按两个 feature 选择 multimodal / text-only / disabled，不从任意模型 extra 伪造 host-only 要求。
- 捕获成功 native content，跳过 `_meta['codex/encryptedContent']`。仅在缺少文本且整份内容没有 encrypted block 时用 structuredContent 的真实 JSON 补文本；不是执行或解密该内容。
- 图片保留实际 detail，按真实图片解码验证；捕获预算 8 MiB。失败结果、直接普通 MCP 调用和不匹配的 raw server 不进入这份增强证据。
- thread extension data 拥有 live retained store；既有记录序号递增、provenance 按128 UTF-8 bytes约束，替换实际控制分隔字符，文本按 Source Guardian truncation预算处理。存储/淘汰优先保留文本的规则与普通 FIFO 不等价。
- `guardian-context/src/node_repl.rs` 将已捕获 snapshot 作为不可信用户证据渲染。32,000 UTF-8 bytes预算、资源省略标记、顺序与图片去重都来自 Source；不得把 transcript 当 developer/system 指令。
- `core/src/guardian/review_session.rs` 的 `pending_node_repl_evidence_admission` / `last_admitted_node_repl_response_sequence` 在真实 reviewer 用户 item 准入后才推进。不能在准备请求、网络首达或首 token 时提前抹掉未交付证据。
- 开启截图捕获与 synchronous transcript feature 是独立事实；与 retained Conversation / async Decisions 的现有原 owner 组合，不能创建第二套 reviewer/task owner。

这些是增强证据链，不是内置一个假 Node REPL 执行器，也不是把普通 MCP 动作改成 TrustedTool。账号相关 hosted connector 仍按用户范围排除；已注册本地 node/cua MCP 的真实结果可以走该 Source 入口，需在同一条生产 accepted-result → owner → reviewer 请求链上验证。

后续实现应统一补完整 producer、live owner、同步交付 cursor、实际模型/feature gate、retained/async消费及必要链测试，再做一次实际 stdio MCP / CodeMode / reviewer HTTP 验证。本次没有把只读审计或已存在的 CodeMode 媒体测试计作新增通过。
