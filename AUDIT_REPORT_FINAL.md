# MiniCode 全栈审计报告（最终修正版）

> 审计日期: 2026-07-16  
> 对照参考: Claude Code (`cc/`)  
> 审计范围: 后端 Agent Loop / 上下文管理 / 工具执行 / LLM 适配器 / WebSocket / 权限 / 沙箱 / MCP / 检查点 / 工作流引擎 / Desktop Electron / 前端 Store / 中断处理 / 内存系统 / Skills 执行 / 附件处理

## 2026-07-16 发布前复核结论

本节以实际生产调用链和回归测试为准，覆盖并修正下文原始审计中的误报。

| 原编号 | 复核结论 | 发布前处理 |
|---|---|---|
| P0-1 | **确认**。每轮会创建新的 `AgentState`，原计数无法跨回合生效 | 已将失败计数同步到会话复用的 `ContextBuilder`，成功压缩时才清零，并增加跨新 `AgentState` 回归测试 |
| P0-2 | **排除**。`checkpoint.rewind` 是写工具文件级回退，不承诺整个 Git 工作区回退；直接 `stash apply` 既不能精确还原后续修改，也可能覆盖用户现有工作 | 不采用不安全的 `stash apply` 建议，继续保留路径受限的文件快照恢复 |
| P1-1 | **误报**。`FallbackLLMAdapter` 已实现，`create_llm_adapter` 会读取 `agent.fallback_providers` 并包装主/备 provider；当前配置也已包含 Anthropic fallback | 无需修改 |
| P1-2 | **设计差异**。命令失败取消 sibling，普通读取失败继续收集其余结果，避免低价值读取失败取消已完成写入 | 保持现状 |
| P1-3/P1-4/P3-9 | **确认是未使用的危险死代码** | 已删除基于 Python `exec` 的旧 `WorkflowEngine`，生产继续使用结构化 `WorkflowTool` |
| P2-1 | **误报**。MCP 通过 `create_subprocess_exec(cmd, *args)` / `Popen([cmd, *args])` 启动，不经过 shell；参数中的路径遍历不构成命令注入。`npx`/`npm` 是用户显式配置的受信 MCP 启动器 | 无需修改；保留命令白名单和 shell 元字符拒绝 |
| P2-2 | **已知平台限制**，不是代码行为偏差 | 保持文档化限制，不在发布前引入高风险沙箱替换 |
| P2-3 | **确认**。大段终端粘贴此前被拒绝 | 已改为 8192 字符分片写入，总输入仍限制为 1 MiB，并增加无截断测试 |
| P2-4 | **误报**。`preload.js` 没有向渲染器暴露通用 `ipcRenderer.on/removeListener`，只暴露固定通道的封装函数 | 无需修改 |
| P2-5 | **确认**。用户中断时未完成工具被误标失败 | 已在中断状态下标记为 `partial`，其他异常终止仍标记 `failed` |
| P2-6 | **架构观察**。全局 `CostTracker` 明确定义为运行时累计指标；每轮 usage 仍由独立 done payload 记录，不会用全局累计值充当单轮用量 | 无需修改 |
| P3-6 | **确认**。Windows 系统目录曾硬编码 C 盘 | 已改用 `SystemRoot`、`WINDIR`、`ProgramFiles`、`ProgramData` 等环境变量动态生成保护路径 |
| P3-7 | **误报**。应用注册的 OS 深链接入口只接收 `minicode://`；HTTP 主窗口跳转和 `window.open` 已由窗口策略拒绝。HTTP 通知目标最终仍经过显式外部打开能力 | 无需修改 |

发布前验证：后端安全/架构/上下文定向测试 125 项通过；前端全量 1231 项通过并完成生产构建；Desktop 全量 13 项通过。后端全量套件运行超过 10 分钟未结束，因此不将其标记为“全量通过”。

---

## 目录

- [P0 — 正确性 / 数据丢失](#p0--正确性--数据丢失)
- [P1 — 多 Agent / 工具执行](#p1--多-agent--工具执行)
- [P2 — 误导 / 安全 / 健壮性](#p2--误导--安全--健壮性)
- [P3 — 架构差异 / 硬化建议](#p3--架构差异--硬化建议)
- [已确认修复的问题](#已确认修复的问题)
- [已排除的误报](#已排除的误报)

---

## P0 — 正确性 / 数据丢失

### P0-1. `consecutive_compaction_failures` 跨回合熔断不触发

**文件**: `backend/agent/loop.py:2117`, `backend/agent/state.py:139`

**问题**: `consecutive_compaction_failures` 计数器在每个用户回合开始时被清零 (`loop.py:2117`)。这意味着如果压缩在多个连续回合中持续失败（例如上下文已满但压缩逻辑无法有效缩减），熔断器永远不会触发。

**cc 对比**: cc 的 `autoCompact.ts` 中，熔断器状态在 turn 间持久化，当连续失败次数达到阈值时会真正阻止后续压缩尝试并切换到降级路径（如 summary-only mode）。

**影响**: 在极端场景下（上下文持续膨胀但压缩无效），agent 会陷入无限重试压缩的死循环，消耗 token 而不前进。

**修复建议**: 将 `consecutive_compaction_failures` 从 per-turn 清零改为仅在压缩成功时清零，或将其迁移到 session 级状态。

**状态**: 未修复（低优先级，因为实际触发概率较低）

---

### P0-2. Checkpoint `rewind` 不使用 `git_stash_ref` 恢复完整工作区

**文件**: `backend/checkpoint/manager.py:64-80`, `backend/checkpoint/store.py:33`

**问题**: `CheckpointRecord` 在创建时记录了 `git_head` 和 `git_stash_ref`（`manager.py:58-59`），但 `rewind()` 方法（`manager.py:64-80`）完全不使用这些 git 引用。它仅恢复写工具涉及的个别文件快照，而非整个工作区状态。

如果用户在 checkpoint 创建后进行了其他文件修改（如通过外部编辑器或 git 操作），rewind 不会回滚这些变更，导致工作区状态不一致。

**cc 对比**: cc 使用 `git stash` 作为完整快照，rewind 时通过 `git stash apply` 恢复整个工作区状态。

**影响**: 部分回滚后工作区状态不一致，后续操作可能依赖错误状态。

**修复建议**: `rewind` 时优先尝试 `git stash apply <git_stash_ref>` 恢复完整工作区，仅在 git 不可用或 stash ref 失效时回退到文件级恢复。

**状态**: 未修复

---

## P1 — 多 Agent / 工具执行

### P1-1. 无模型降级 / Fallback 机制

**文件**: `backend/llm/` (全局)

**问题**: MiniCode 没有实现模型降级策略。当主模型持续返回 429/529（过载）时，cc 会触发 `FallbackTriggeredError` 并切换到备用模型（如 Opus → Sonnet）。MiniCode 的 `classify_llm_error` 将 429/529 分类为 `retryable`，但没有后续的模型切换逻辑。

**cc 对比**: `cc/src/services/api/withRetry.ts:160-168` 定义了 `FallbackTriggeredError`，`withRetry` 函数在连续 529 错误达到 `MAX_529_RETRIES` 时抛出该错误，触发上层循环切换到 `fallbackModel`。

**影响**: 当主模型过载时，agent 只能无限重试同一模型，无法降级到可用模型，导致任务卡死。

**修复建议**: 在 `LLMSettings` 中添加 `fallback_model` 字段，在 `stream_retry_policy` 中跟踪连续 529 次数并触发模型切换。

**状态**: 未修复（架构差异）

---

### P1-2. 工具并发 sibling abort 仅覆盖命令类工具

**文件**: `backend/agent/tool_execution.py:2446-2467`

**问题**: MiniCode **已实现** sibling abort 机制：当 `run_command`/`bash`/`powershell` 类工具失败时，同批次其他未完成的工具会被取消（`should_cancel_siblings = True`）。但触发条件仅限于 `COMMAND_OUTPUT_STREAM_TOOL_NAMES` 集合中的工具。

**cc 对比**: cc 的 `runToolsConcurrently` 在**任何**工具抛出异常时都会取消其余任务。

**影响**: 如果 `write_file` 或 `edit_file` 因严重错误（如权限拒绝、磁盘满）失败，同批次的 `read_file` 等只读工具仍会继续执行，可能基于不一致的状态产生结果。

**修复建议**: 考虑将 sibling abort 触发范围扩展到所有 `is_error=True` 的工具结果，或至少包括写工具（`write_file`、`edit_file`、`apply_patch`）。

**状态**: 部分实现（设计差异——MiniCode 当前选择仅在命令工具失败时取消，避免只读工具失败影响写操作结果收集）

---

### P1-3. Workflow 引擎 `parallel` 吞掉异常返回 None（死代码）

**文件**: `backend/workflow/engine.py:221-230`

**问题**: `_parallel_wrapper` 使用 `asyncio.gather(*thunks, return_exceptions=True)`，然后将所有异常转换为 `None`。调用者无法区分"任务返回 None"和"任务抛出异常"。

**注意**: `WorkflowEngine`（`engine.py`）是**遗留死代码**——生产环境使用的 `WorkflowTool`（`tools/workflow_tool.py`）是完全独立的实现，不调用 `WorkflowEngine.run_script()`。`WorkflowEngine` 仅从 `backend/workflow/__init__.py` 导出，但无任何生产代码导入。

**修复建议**: 如果计划复活 `WorkflowEngine`，需修复异常处理（返回结构化结果而非 None）。否则建议删除死代码以避免混淆。

**状态**: 未修复（低优先级——死代码）

---

### P1-4. Workflow 引擎 schema 验证过于简陋（死代码）

**文件**: `backend/workflow/engine.py:183-206`

**问题**: `_agent_wrapper` 中的 schema 验证只检查 `required` 字段是否存在，不验证类型或格式。JSON 提取使用简单的 `content.index('{')` 到 `content.rindex('}')`，如果 agent 输出中有嵌套 JSON 或代码块包含大括号，会提取错误内容。

**注意**: 同 P1-3，此代码属于遗留死代码，不影响生产环境。

**修复建议**: 使用正则匹配 JSON 代码块，schema 验证应使用 `jsonschema` 库。或直接删除。

**状态**: 未修复（低优先级——死代码）

---

## P2 — 误导 / 安全 / 健壮性

### P2-1. MCP 命令注入防护可被绕过

**文件**: `backend/mcp/manager.py:28-39`

**问题**: `_is_safe_mcp_command` 检查命令 basename 是否在白名单中。但：
1. `npx` 和 `npm` 在白名单中，攻击者可以通过 `args` 传入恶意包名
2. `_has_shell_injection` 只检查 args 中的 shell 元字符，但不检查 args 中的路径遍历（如 `../../../malicious.js`）
3. Windows 上 `.cmd` 后缀的处理可能导致 `python.exe` 和 `python` 被视为不同命令

**影响**: 恶意 MCP 配置可能通过精心构造的 args 绕过命令白名单。

**修复建议**: 对 args 也进行路径遍历检查，考虑使用 `subprocess.run` 的列表参数形式而非 shell 字符串。

**状态**: 未修复

---

### P2-2. 沙箱在 Windows 上无实际隔离

**文件**: `backend/sandbox/runner.py:225-227`

**问题**: Windows 平台上，`_wrap_command` 直接返回原始命令，注释说明"无实际 OS 级隔离"。仅依赖 app 层的 `validate_command` 和工作区边界检查。`CREATE_NEW_PROCESS_GROUP` 和 `CREATE_BREAKAWAY_FROM_JOB` 只提供进程组隔离，不阻止文件系统写入或网络访问。

**影响**: 在 Windows 上，沙箱策略（如 `allow_network=False`）不生效，恶意命令可以访问网络和写入工作区外文件。

**修复建议**: 在 Windows 上考虑使用 WSL2 沙箱或 Windows AppContainer，或在文档中明确标注 Windows 沙箱限制。

**状态**: 已知限制（设计差异，非 bug）

---

### P2-3. PTY `writeToSession` 大小限制可能阻塞交互

**文件**: `desktop/pty-manager.js:226`

**问题**: `writeToSession` 限制单次写入 8192 字节。当用户粘贴大段文本（如配置文件内容）到终端时，超过 8192 字节的部分会被静默丢弃，不会分片发送。

**修复建议**: 将大输入分片为 ≤8192 字节的块依次写入，而非直接拒绝。

**状态**: 未修复

---

### P2-4. Electron `preload.js` 暴露过宽 IPC 监听接口

**文件**: `desktop/preload.js`

**问题**: 虽然使用了 `contextBridge.exposeInMainWorld`，但暴露的 API 中 `ipcRenderer.on` / `ipcRenderer.removeListener` 允许渲染进程监听任意 IPC 通道。如果渲染进程被 XSS 攻击，攻击者可以监听所有 IPC 事件（包括 pty data、approval 请求等）。

**修复建议**: 限制 `on` 方法只允许预定义的通道列表，或使用更严格的 `ipcRenderer.invoke` 模式。

**状态**: 未修复（安全硬化建议）

---

### P2-5. 前端 `finishStreaming` 对未完成工具调用标记为 `failed`

**文件**: `frontend/src.v2/stores/chat-slice.ts:1258-1270`

**问题**: `finishStreaming` 中，所有 `status === "running"` 或 `status === "pending"` 的工具调用都被无条件标记为 `failed`，不区分 `terminalStatus`。即使 `terminalStatus === "interrupted"`（用户主动中断），工具仍被标记为 `failed` 而非 `interrupted`。

对比之下，同函数内的 `progress` 和 `process` 块**会**根据 `terminalStatus` 区分 `failed`/`completed`/`interrupted`。

**影响**: 用户中断时，仍在运行的工具被标记为"失败"（红色），可能误导用户认为发生了错误。但从数据完整性角度看，未完成的工具确实没有可靠结果，标记为非成功是合理的保守策略。

**修复建议**: 在 `terminalStatus === "interrupted"` 时将工具标记为 `interrupted` 而非 `failed`，保持与 progress 块一致的语义。

**状态**: 部分修复——progress/process 块已区分 terminalStatus，但 tool_call 块未区分

---

### P2-6. `CostTracker` 使用全局单例，多会话计费混淆

**文件**: `backend/llm/cost_tracker.py:56-66`

**问题**: `CostTracker` 使用类级 `_instance` 单例模式。在多会话并发场景下（多个 WebSocket session 同时活跃），所有会话的 token 消耗被汇总到同一个 `CostTrackerState` 中，无法区分每个会话的实际成本。

**修复建议**: 将 `CostTracker` 改为 per-session 实例，或使用 session_id 作为 key 的字典结构。

**状态**: 未修复

---

### P2-7. `CostTracker.record_usage` 中 Anthropic 与 OpenAI 的 prompt_cache_total 计算不一致

**文件**: `backend/llm/cost_tracker.py:82-88`

**问题**: 
- Anthropic: `prompt_cache_total = input_tokens + cache_read_input_tokens + cache_creation_input_tokens`
- OpenAI: `prompt_cache_total = max(input_tokens, cache_read_input_tokens) + cache_creation_input_tokens`

对于 OpenAI，`input_tokens` 通常已包含 `cache_read_input_tokens`（即缓存命中不额外计入 input），所以 `max()` 是合理的。但对于某些兼容 API（如 vLLM），这个假设可能不成立，导致 `prompt_cache_total` 虚高。

**修复建议**: 根据 provider 的实际 usage 语义决定计算方式，或添加配置项允许用户覆盖。

**状态**: 未修复

---

## P3 — 架构差异 / 硬化建议

### P3-1. 无 Fast Mode / 模型速度切换

**cc 对比**: cc 实现了 Fast Mode（使用 Haiku 处理简单请求），在 429/529 时自动降级到标准速度。MiniCode 没有此功能。

**建议**: 低优先级，可作为未来优化项。

---

### P3-2. 无 OAuth Token 刷新机制

**文件**: `backend/llm/openai_adapter.py`, `backend/llm/anthropic_adapter.py`

**cc 对比**: cc 的 `withRetry` 在收到 401 时会自动刷新 OAuth token 并重试。MiniCode 仅将 401 分类为 `fatal`，不尝试刷新。

**建议**: 如果 MiniCode 未来支持 OAuth 认证（如 Claude Pro/Max），需要实现 token 刷新逻辑。

---

### P3-3. WebSocket 事件回放无增量同步

**文件**: `backend/ws/session_restore.py:121-135`

**问题**: `sync_session` 的增量同步仅基于消息数量作为版本号。如果消息被修改（如编辑历史消息），版本号不会变化，客户端可能丢失更新。

**cc 对比**: cc 使用更细粒度的事件序列号和内容哈希来检测变更。

**建议**: 使用事件序列号（已在 `_ws_event_seq` 中实现）配合客户端确认机制，替代简单的消息计数。

---

### P3-4. `StreamingToolExecutor` 无独立超时监控

**文件**: `backend/agent/tool_execution.py:853-935`

**问题**: `StreamingToolExecutor` 的预取工具执行没有独立的超时机制。如果一个预取的工具执行（如 `web_fetch`）卡住，它会阻塞整个流式处理管道，直到模型流式结束后的 `execute_tool_batch` 才会应用全局超时。

**cc 对比**: cc 的 `StreamingToolExecutor` 对每个预取任务设置独立超时。

**建议**: 为 `PrefetchedToolExecution` 添加超时参数，在 `add_tool` 时设置。

---

### P3-5. MCP `classify_mcp_phase` 基于错误文本推断认证状态

**文件**: `backend/mcp/manager.py:67-107`

**问题**: MCP 服务器没有真正的认证协议，`classify_mcp_phase` 通过检查错误消息中是否包含 "401"/"403"/"unauthorized" 等关键词来推断认证状态。这种基于文本的推断不可靠——不同 MCP 服务器的错误消息格式不一致，且某些网络错误可能包含这些关键词（如代理返回的 403）。

**建议**: 长期应推动 MCP 协议标准化错误类型，短期可增加更严格的匹配规则（如要求同时匹配 HTTP 状态码和关键词）。

---

### P3-6. Desktop `security.js` 的 `isSafeWorkspacePath` 硬编码 Windows 系统路径

**文件**: `desktop/security.js:101-113`

**问题**: `winSystemPrefixes` 硬编码了 `c:/windows`、`c:/program files` 等路径。如果 Windows 安装在非 C 盘，这些检查会失效。

**修复建议**: 使用 `process.env.SystemRoot` 或 `process.env.PROGRAMFILES` 动态获取系统路径。

---

### P3-7. Electron 深链接未验证目标 URL 域名

**文件**: `desktop/main.js:318-350`

**问题**: `normalizeDeepLinkTarget` 允许 `http:` 和 `https:` 协议的深链接，但不验证目标域名。恶意深链接可以引导用户访问钓鱼网站。

**修复建议**: 对 `http:`/`https:` 深链接添加域名白名单检查，或仅允许 `minicode:` 协议。

---

### P3-8. 前端 `composer-slice` 中 `setPermissionMode("auto")` 自动批准范围过宽

**文件**: `frontend/src.v2/stores/composer-slice.ts:189-196`

**问题**: `isAutoAllowedToolName` 的正则允许 `write_` 和 `edit_` 前缀的工具在 auto 模式下自动批准。这意味着 `write_file` 和 `edit_file` 会在用户无感知的情况下自动执行，可能覆盖重要文件。

**建议**: 在 auto 模式下，写操作仍应要求确认，或至少要求用户明确启用"自动编辑"选项。

---

### P3-9. Workflow 引擎 `exec()` 死代码清理

**文件**: `backend/workflow/engine.py:91`

**问题**: `WorkflowEngine.run_script` 使用 `exec(script, namespace)` 执行 Python 脚本。虽然 `_safe_builtins()` 限制了内置函数，但注入的 `asyncio`/`json`/`time` 模块携带 `__builtins__`，理论上可通过属性链访问 `__import__`。

**重要说明**: 此代码是**死代码**——生产环境使用的 `WorkflowTool`（`tools/workflow_tool.py`）是完全独立的结构化实现，不调用 `WorkflowEngine`。`WorkflowEngine` 仅从 `backend/workflow/__init__.py` 导出，无任何生产代码导入。

**建议**: 删除 `WorkflowEngine` 死代码，避免未来误用。如果计划复活，需使用 AST 解析替代 `exec`。

---

### P3-10. `AttachmentStore.find_payload` 全目录扫描性能问题

**文件**: `backend/attachments/store.py:71-100`

**问题**: `find_payload` 在按 `artifact_id` 直接查找失败时，会遍历整个附件目录（`glob("*.json")`）逐个加载 JSON 检查 `doc_id`/`file_name` 匹配。这是 O(n) 操作，当附件数量增长时性能下降。

**修复建议**: 维护一个 `doc_id → artifact_id` 和 `file_name → artifact_id` 的索引文件，或使用数据库。

---

### P3-11. 会话 transcript 每次追加消息时全量重写

**文件**: `backend/conversations/repository.py:155-174`, `backend/conversations/repository.py:554-569`

**问题**: `ConversationRepository.append_transcript_message`（公开方法）在追加单条消息时调用 `_write_transcript()` 对整个 transcript 文件做全量重写（先序列化所有消息为 JSONL，再原子写入）。对于长会话（100+ 条消息），每条新消息的写入成本为 O(n)。

私有方法 `_append_transcript_message`（`repository.py:554`）实现了真正的 append 模式（`open("a")`），但**从未被任何代码调用**——是死代码。

**影响**: 在高频交互的长会话场景下，I/O 开销随消息数线性增长。对于典型使用场景（几十条消息），影响可忽略。

**修复建议**: 将 `append_transcript_message` 改为使用 `_append_transcript_message` 的 append 模式，但需确保 append 操作在文件锁保护下进行以避免并发写入冲突。

**状态**: 未修复（性能优化，低优先级）

---

### P3-12. 后端终端会话在 Windows 上无真实 PTY

**文件**: `backend/terminal/session.py:145-168`

**问题**: 后端 `TerminalSession` 在 Windows 上使用 `asyncio.create_subprocess_exec` 启动 PowerShell，通过 stdin/stdout/stderr 管道通信，而非真实 PTY。这意味着依赖终端控制序列的交互式 TUI 程序（如 `vim`、`less`、`top`）无法正常工作。

**缓解**: 在 Electron 桌面模式下，PTY 由 `desktop/pty-manager.js` 通过 `node-pty` 管理，支持真实终端。此限制仅影响 Web/浏览器模式。

**状态**: 已知限制（设计差异——桌面模式不受影响）

---

## 已确认修复的问题

以下问题在之前的审计中识别，现已确认修复：

| # | 问题 | 修复说明 |
|---|------|----------|
| 1 | 流式文本按内容子串过滤吞字 | `isRawProviderErrorText` 改为精确匹配整句错误信息 |
| 2 | idle 兜底伪完成 | `finishStreaming` 不再强制 `'completed'`，引入 `injected_last_resort_message` 标志 |
| 3 | 恢复后 finally 擦除回复 | 引入 `committed_last_resort_reply` 标志 |
| 4 | DAG 级联取消 | `task_status` 不再将 partial/failed 塌缩为 cancelled |
| 5 | Persona no-op | live prompt 已接入 persona 参数 |
| 6 | 非流式 LLM 计费 | 引入 `record_non_stream_usage` |
| 7 | `expected_hash` 守卫失效 | 引入 read-time hashes |
| 8 | 孤儿 tool_result | 合成最小 `tool_call` 块 |
| 9 | `write_scope` 互斥 | 引入 `test_write_scope_exclusivity.py` |
| 10 | `apply_patch` `rstrip` 模糊匹配 | 删除 `rstrip` fallback |
| 11 | 顶插 hunk | 仅允许 EOF 追加无 context hunk |
| 12 | `run_command` write guard | 已修复 |
| 13 | 大部分 UI 对比度/a11y 问题 | 已修复 |

---

## 已排除的误报

### 误报 1: `reactive_compaction_attempted` 跨轮不重置

**结论**: 该标志在每轮对话开始时正确重置（`loop.py:2118`），且单次压缩策略是设计意图——避免在同一轮内多次尝试反应式压缩导致上下文不稳定。

### 误报 2: 流式重试前未清理 `StreamingToolExecutor`

**结论**: 流式重试路径（`_plan_stream_retry`）有严格的前置守卫条件（`loop.py:3539`），确保在没有工具调用或工具调用未完成时才进入重试。此时 `StreamingToolExecutor` 实例可能尚未创建或已完成，无需显式清理。

### 误报 3: 工具执行无 sibling error cancellation

**结论**: MiniCode **已实现** sibling abort（`tool_execution.py:2446-2467`）。当 `run_command`/`bash`/`powershell` 类工具失败时，同批次其他未完成的工具会被取消。触发范围比 cc 窄（仅命令类工具），但机制存在且正常工作。已修正为 P1-2（部分实现）。

### 误报 4: Plan Mode 遗漏 `apply_patch`

**结论**: Plan Mode 的权限检查逻辑（`checker.py:662-668`）使用 catch-all `else: ALWAYS_DENY`。`apply_patch` 虽然不匹配 `_PLAN_MODE_DENY` 中的任何显式模式，但会落入 `else` 分支被拒绝。**apply_patch 在 Plan Mode 下是被正确阻止的。**

### 误报 5: `accept_edits` 模式未实现

**结论**: `accept_edits` 模式**已实现**（`checker.py:653-661`）。该模式自动批准写工具（`_WRITE_TOOL_PATTERNS` → `AUTO`），对命令类工具仍要求确认（`_CONFIRM_TOOL_PATTERNS` → `CONFIRM`），其他工具自动批准。

---

## 总结

| 优先级 | 总数 | 未修复 | 已修复 | 备注 |
|--------|------|--------|--------|------|
| P0 | 2 | 2 | 0 | 原报告 3 个，P0-2(exec) 降级为 P3-9 死代码 |
| P1 | 4 | 4 | 0 | P1-2 从"未实现"修正为"部分实现"；P1-3/P1-4 标注为死代码 |
| P2 | 7 | 6 | 1 | P2-5 修正描述 |
| P3 | 12 | 12 | 0 | 新增 P3-9(死代码清理)、P3-10(附件扫描性能)、P3-11(transcript 全量重写)、P3-12(Windows 后端终端无 PTY)；移除原 P3-5/P3-6 误报 |
| **合计** | **25** | **24** | **1** | |

**关键风险领域**（按优先级排序）:
1. **Checkpoint 不完整恢复** (P0-2): `rewind` 不使用 `git_stash_ref`，可能导致工作区状态不一致
2. **压缩熔断器不触发** (P0-1): `consecutive_compaction_failures` 跨回合清零，极端场景下可能死循环
3. **模型降级缺失** (P1-1): 影响系统在模型过载时的可用性
4. **MCP 命令注入** (P2-1): args 路径遍历未检查
5. **Windows 沙箱无效** (P2-2): 在 Windows 上无实际隔离（已知限制）

**与 cc 的核心架构差异**:
- 无模型降级/Fallback 机制
- 无 Fast Mode
- 无 OAuth Token 刷新
- Sibling abort 仅覆盖命令类工具（cc 覆盖所有工具）
- Windows 沙箱无实际隔离

**审计覆盖确认**:
- ✅ Agent Loop 主循环与错误恢复
- ✅ 上下文管理与压缩策略（compaction.py / error_withholding.py / reactive compact）
- ✅ 工具执行与 StreamingToolExecutor
- ✅ LLM 适配器与错误分类（Anthropic / OpenAI / errors.py / cost_tracker）
- ✅ WebSocket 层与会话恢复（handler.py / session_restore / 前端 useWebSocket）
- ✅ 权限系统（含 Plan Mode、accept_edits、auto 模式）
- ✅ 沙箱安全
- ✅ MCP 生命周期
- ✅ 检查点系统
- ✅ 工作流引擎（含死代码识别）
- ✅ Desktop/Electron 层（main.js / pty-manager / security / preload / ipc-handlers）
- ✅ 前端 Store 状态机（chat-slice / agent-slice / composer-slice）
- ✅ 前端流式事件处理（chatStreamEvents / sendChatMessage / useWebSocket）
- ✅ 中断处理（cancel_event 机制完善）
- ✅ 内存系统（文件式，简洁无问题）
- ✅ Skills 执行器（注入式，简洁无问题）
- ✅ 附件处理（功能完整，有性能优化空间）
- ✅ 会话持久化（repository.py — 文件锁 / 原子写入 / 容灾恢复）
- ✅ 文件工具（write_file / edit_file / apply_patch — 路径校验 / 符号链接检查 / 原子写入）
- ✅ 协调器模式（coordinator.py — 工具限制 / 意图检测 / 委托限制）
- ✅ 钩子系统（hooks/manager.py — 完整 cc 生命周期事件 / JSON 输出解析）
- ✅ 工具参数修复引擎（tool_repair.py — 资源路由 / 缺失参数修复）
- ✅ 工作区服务（workspace/service.py — 路径遍历防护 / 文件操作）
- ✅ 配置系统（config.py / bootstrap/app.py — .env / settings.json / 启动超时）
- ✅ 终端会话（terminal/session.py — Windows 限制已标注）
- ✅ 系统提示构建（prompting.py — 分层 / persona / cache prefix）
