# Codex 后端服务与过程契约对齐（2026-10-12）

参考 checkout 为 `.tmp/codex-src`，实际 revision 是
`822e58cc3d666166c7446c5b1ea2e52f5d09594c`（2026-10-06）。桌面前端由本轮其他 agent
读取实际 `26.1007.2314.0` ASAR 与截图核对。MiniCode 保留自己的后端；同一工具执行、持久化与公开投影链需要表达与 Codex 相同的用户可见事实。

## 本轮实际修复

Codex `app-server-protocol/src/protocol/item_builders.rs` 将命令与
`command_actions` 分开，动作包括 Read、Search、ListFiles、Unknown；
`protocol/v2/item.rs` 把真实 status、aggregated_output、exit_code、duration_ms
作为另外的事实。没有由状态拼接出的 “Failed: Read” 动作标题。

MiniCode 原先所有 `run_command` 均归为运行命令，且共享结果生产者自动生成
Failed/Blocked/Timed out/Cancelled 摘要；普通报错又被合成为“工具执行失败。”。
现在动作来自冻结请求的实际参数，失败状态与原错误仍在结果及展开记录中。工具自身的动作 label
保持各自的读取、编辑、搜索、运行等语义，没有用一个泛化工具标题替代。

新增 `backend/agent/command_actions.py` 依据 Codex 的 `shell-command/src/bash.rs`
word-only AST 和 `parse_command.rs` 分类规则生成动作。权限解析器会进入循环、控制流与嵌套命令，
其用途是发现危险操作，不能拿它的字面命令提取结果证明一条展示动作。展示使用 Codex 同样的
AST 边界：变量、替换、重定向、控制流等保持 Unknown。

- PowerShell 遵照该 revision，只把简单 Get-Content/gc/type 单文件读取识别为 Read；复杂参数、多个文件、变量、通配符和 pipeline 保留原命令 Unknown。
- POSIX 对文件读取、目录列表和搜索生成真实 typed 动作，保留原顺序；Read 的路径由实际工作目录解析。
- 有不能归类的操作时，整个脚本作为原命令 Unknown 保留，不能只展示其中的读取或搜索而抹去其余执行。
- `tool_call` 和 `tool_result` 携带相同 `command_actions`；WS 主会话续传、journal 开始/结果、主/子历史公开投影、state/REST 均保留动作。
- 公共历史字段为 `commandActions`。该字段既不改变授权，也不执行额外命令。

Skill 目录现按 `ext/skills/src/fragments.rs` 以 developer 角色和
`<skills_instructions>` 包装进入 Provider 请求，system 与项目 user instructions
不重复包含目录；已选 Skill 的原始正文仍作为 user fragment，快照恢复不改变角色或正文。

Send/Followup 依据 `multi_agents_v2/{send_message,followup_task,message_tool}.rs`
区分 `queue_only` 与 `trigger_turn`。`send_message` 只投递，`followup_task`
才启动闲置/已完成 agent 的下一轮。消息模式写入实际 SQLite message payload，闲置 worker
只 claim 可触发的工作；活动 turn 可读两类邮件。父会话 outbox 保留可投递的 queue marker，
闲置调度器不因该 marker 启动模型。没有改变已有任务与消息：新增字段之前的消息本来就具有
唤醒行为，其模式明确恢复为 `trigger_turn`。

关停消息走已有 leader/epoch/request 的生命周期检查，闲置 worker 能停止；它不是模型新 turn。
Followup 仍受原子任务能力上限、会话与工作区所有权约束；completed resume 使用已有 canonical
checkpoint，保留同一 agent identity、原始权限、历史与实际工作目录。

## 本轮验证

统一运行 7 个相关文件，155 项通过，0 项跳过。第 1 轮因新增解析模块的括号语法错误产生
4 个收集错误，失败日志保留；修复后第 2 轮完整批次通过。后续补充分类与 cwd 对齐的结果另记。

验证包括 Codex 原始 PowerShell 示例、POSIX read/search/list/mixed/unknown、真实失败状态、
原输出以及实际 shell 的 exit 7。真实读取 `source.txt` 和真实失败命令经过规范事件生产者后，
写入 journal、重新打开 journal、恢复主会话 stream snapshot、生成子任务历史、再通过 REST
序列化，核对字段和错误证据不丢失。

Skill 与 Send/Followup 单独统一运行 12 个文件，首轮 277 通过 / 4 个新 fixture 失败。
fixture 分别缺少真实 incarnation 完成 fence（2 个）、没有使用实际 message idempotency key、
调用了不存在的 cancel API；修正后 3 个文件重跑 18 项全通过。最终受影响 **281 项通过，
0 项跳过**，重复运行不累计为新的通过项。实际 persistent teammate 在 Send 后保持闲置且模型
请求数不增加，Followup 才产生第二 turn，排队消息与 Followup 内容各进入模型一次；真实取消
没有产生第三 turn。完成任务、父会话交接与 controller eviction 的 checkpoint 续传，以及
原生 WS 的工作区/会话/owner/关停边界均覆盖。

日志为 `backend-skill-send-followup-tests-run01.log` 与 `run02.log`，同目录保留 XML 与首轮失败。
模型来自测试 LLMAdapter，但任务运行、工具、队列、SQLite、checkpoint、权限和生命周期为实际
MiniCode 实现；不把它描述为公网模型验收。

证据目录：`output/playwright/codex-rebuild-20261012/backend-command-actions/`。
其中 `real-command-actions-wire-history.json` 提供实际 wire、main_blocks、child_messages
与 child_incremental，供前端直接 hydrate/project/render 验证。单元测试与本地 shell 执行不代表
公网 Provider 或外部 MCP/OAuth 已验收。

## 对应调用链审计

| 服务 | Codex 源码依据 | MiniCode 现状与可核对边界 |
| --- | --- | --- |
| 模型基础提示词 | `prompts/src/model_instructions.rs`、`models-manager/{prompt.md,models.json}` | `codex_prompts.py` 使用该 revision 的原始 catalog 模板与 unknown-model 模板；普通 runtime context 另行构造。 |
| 协作模式 | `collaboration-mode-templates/templates/{default,plan}.md` | 原文资源已保留；只把 `request_user_input` 映射为真实 `ask_user` 单问题 schema，不能向模型声称不存在的参数。 |
| Skill 正文 | `ext/skills/src/fragments.rs::SkillInstructions` | 明确选中的正文以 user contextual fragment 注入，完整正文与 invocation 记录在压缩后保留。 |
| Skill 目录 | 同文件 `AvailableSkillsInstructions` | 现以 developer + `<skills_instructions>` 注入；真实 Skill 文件选取、正文、快照恢复与空目录均已验证。 |
| MCP 工具 | `codex-mcp/src/connection_manager/tool_catalog.rs`、`core/src/tools/handlers/mcp.rs` | MiniCode proxy 保留 tool schema/server identity、动态目录版本、host approval、原内容与 typed 图片/资源；本轮没有重新验证全部外部 server。 |
| 子任务真实开始 | `core/src/tools/handlers/multi_agents_v2/spawn.rs`、`SubAgentActivityItem` | MiniCode 资源 accepted 与 execution_started_at 分开，started 行需要真实开始；此前全链已有受控 Electron 证据，本轮没有改该边界。 |
| 子任务消息与续传 | `multi_agents_v2/{send_message,followup_task,message_tool}.rs` | 现提供 QueueOnly Send / TriggerTurn Followup，durable mode 贯通 mailbox、parent wake、idle consumer、native WS 与 checkpoint resume。 |
| 手动压缩 | Codex canonical context compaction items / 原操作等待与终态 | root 已完成并验证 durable admission、真实开始/成功/失败、lease/CAS 与 native completion receipt；本轮保留其已验证链。 |

后端完整服务等同不能从某一张截图或一组单测推导。这里把本轮已实现、已有证据、尚未对齐与尚未重验的服务分别记录，后续修复继续补在实际调用链中。

## 常规设置服务边界

桌面偏好由实际 native preferences 服务持久化。默认权限 profile 在新建任务时传入
`conversation.create.permission_mode`，profile 为 confirm/plan/auto/bypass；默认任务目录
以 `projectless_base` 传入，不能把它填入项目归属 `workspace_root`。`config_layers`
中的 profiles 仍是 TOML 配置分层，不承担 GUI 权限 profile 持久化。

新 projectless 任务由 Repo 在创建幂等去重后、同一 store lock 内分配目录。结构参考实际
26.1007.2314.0 ASAR 的 `yt/xt/Ct/Et/nl/rl`：`base/YYYY-MM-DD/slug`，slug 取首六个
ASCII 单词并最多 80 字符，没有 ASCII 单词时为 `new-chat`；依次尝试 100 个编号名称和
最多五个 UUID 后缀。每个 taskdir 有 `outputs/` 与 `work/`。只将实际新建的 taskdir
追加到原信任 ledger，保留既有 entries；不将整个 base 加入编辑或工具写入授权。

独立持久字段为 `execution_cwd`、`projectless_output_dir`、`projectless_browser_root`。
workspace_root/worktree_path 继续表示真实项目归属。执行 cwd 同时传入 AgentRunner、REST、
工具与权限、上传 reservation、资源读取、终端 owner、快照和恢复。cwd 被固定后不会跟随
全局默认目录变化。worktree 迁移保留 cwd 在项目内部的相对子目录位置。

目录创建后，create/switch 按真实 execution path 进行存在性和信任检查，再挂载 context；
runtime snapshot 发布已经挂载的 execution root。项目不可用时保留历史与 durable binding，
但实际 session root 为空。旧 projectless 没有任何 cwd 来源时，Repo 与 restore/list getter
都保持未绑定；可信 provider snapshot 的 cwd 可以恢复，普通用户消息里的 `<cwd>` 不构成
来源。未知 cwd 只在下一次真实执行 admission、上传 reservation 或终端执行时固定为实际
executor cwd，原 projectless 附件的空 owner grant 通过 same-conversation 精确补 grant 保留。

实际 ASAR `src-BJv5Mk7n.js::Ee` 的 projectless additional developer instructions 已进入
独立 developer 消息：目录名不是语言偏好；优先行内回答；中间文件用 work/，最终交付与链接
仅用固定 outputs 路径；不因 projectless 默认设置直接写用户 home。environment cwd 为 taskdir，
workspace_roots 为固定 browser base；普通线程、REST 和子 agent metadata 保留这组事实。

只读 Files 浏览与代码编辑 owner 分开。`browser-tree`、`browser-file`、`browser-preview`、
`browser-raw` 都以 session_id/conversation_id/execution_cwd/path 核对当前实际 owner，
normal 项目浏览实际 execution root，mint projectless 浏览 server 记录的固定 base，已经执行的
legacy projectless 浏览其真实 cwd。tree entries 的 path 以浏览 root 为相对路径；预览复用既有
文档 parser，raw 保留真实图片/PDF/文件字节。路径和 symlink 边界复用 WorkspaceService。
没有 browser 写入接口，没有切换项目，也没有给 base 增加写授权。外部导入 JSON 不能带入 base
能力；只有与本地 mint 记录的 cwd/base/output 三项完全一致的同源导入才保留它们。

原生图片/PDF 的 browser-raw URL 使用新路由独占的签名：
`browser_raw:v1:session_id:conversation_id:execution_cwd:path:expires_at`，UTF-8 HMAC-SHA256
再 base64url 去 padding，`raw_token=expires_at.signature`。前端有效期为 300 秒，不将 runtime
secret 放入 URL。真实 runtime middleware 支持普通 /api 与 /api/v1 alias，随后仍核对 record owner；
这个 token 不能用于普通预览、generic workspace raw 或写入操作。

projectless 物理挂载和索引不登记最近项目。只有真实 membership 或明确打开项目的调用传入
独立 `project_root`，才记录 recent 与项目导入投影；项目 subcwd 记录原项目 root，不能把 src
注册成新项目。用户明确将已有 taskdir 打开为项目时合法登记，即使同一路径的物理 context 已挂载；
返回 projectless 不删除用户已经明确登记的 recent。

后端 pipe terminal 已消费真实 `shell` 选择，并保持同一 session 的 cd 与变量；restart 保留
`_initial_cwd` 和 `_requested_shell`。Windows cmd 使用 UTF-8 code page 初始化；PowerShell
保留实际原初始化。Desktop PTY 由 Desktop agent 独占并另行验证，不能用 pipe 测试代替 PTY 验收。

桌面 parent lifetime watcher 以 `os.read(parent_pipe.fileno(), 1)` 读取原始 descriptor，避免 daemon
线程在解释器结束时持有 stdin BufferedReader 锁。真实 parent EOF 仍终止 Windows job 的后端和
所拥有的子进程；正常退出不再触发 `_enter_buffered_busy` fatal。

## cwd、浏览与生命周期验证

日志保存在 `output/playwright/codex-rebuild-20261012/`，每轮都有对应 XML。
原始失败和主动停止的批次保留，不记作通过；下列批次包含重复用例，不能直接相加。

| 批次 | 实际结果 | 核对链路 |
| --- | --- | --- |
| backend-execution-cwd-shell-run01 | 收集失败 | 新测试错误引用 TerminalManager，已改为真实 TerminalSessionManager。 |
| backend-execution-cwd-shell-run02/run03 | 每轮 120 通过、8 失败 | 保存 bare Repo 猜 cwd、恢复 getter 伪造 root、pipe restart 错字段，以及一个非法 fixture id 的原始失败。 |
| backend-execution-cwd-shell-run04 | 18 通过、0 跳过 | 修上述根因后重验 8 个失败选择、新增 cwd/真实工具/附件/pipe 和原始 stdin lifetime。 |
| backend-execution-cwd-shell-run05 | 主动停止 | 过宽批次未作为验收；仅精确终止该 XML 对应的 owned pytest，不动用户 App。 |
| backend-execution-cwd-shell-run06 | 19 通过、0 跳过 | 增加真实 dispatcher create→mount→switched→snapshot→scope，覆盖四种不可用项目 restore 与旧图像 sources。 |
| backend-projectless-browser-chain-run01 | 23 通过、0 跳过 | server minted base 的真实 ASGI 列表、读取、解析预览、raw、exact owner 与原写入拒绝。 |
| backend-projectless-browser-chain-run02 | 24 通过、1 失败 | 新 Windows fixture 写入 CRLF，却断言 LF；改 fixture 为明确 LF 字节，生产读取保留真实行尾。 |
| backend-projectless-browser-chain-run03 | 25 通过、0 跳过 | 加 normal 项目 subcwd、PNG 原字节、unknown readonly 不 bind、同源/外部导入能力与原 composite owner 契约。 |
| backend-projectless-browser-auth-run01 | 34 通过、0 跳过 | 完整生产 middleware 的 headerless scoped raw URL、alias、过期/字段修改/跨路由拒绝及原 runtime auth/raw 资源。 |
| backend-projectless-recent-producer-run01 | 34 通过、0 跳过 | 真 create/index 后原 recent ledger 字节不变；明确打开同一路径为项目、返回 projectless 与项目 subcwd 均核对真实 recent，兼顾 foreground/rollback/MCP。 |
| backend-projectless-rest-admission-run01 | 37 通过、2 失败 | 两个既有 canonical-answer fixture 使用 tmp_path.name 作为非法 Repo conversation id；两个新增真实 REST cwd/grant 变体均已通过。 |
| backend-projectless-rest-admission-run02 | 27 通过、0 跳过 | 将两 variant fixture 改为真实允许的 conv_... id，保持原答案/错误断言；未知与可信 snapshot 的 REST cwd、原空 grant 和真实 QueryEngine 均通过。 |

这些证据覆盖真实本地 shell、文件、目录、ledger、Repo、SQLite/查询运行、WebSocket dispatcher
与 HTTP middleware；部分模型来自受控 LLMAdapter。Desktop production 的 Browser/PTY/设置视觉
由并行 native 验收另行记录；这里不把本地受控模型描述为公网 Provider 或外部 OAuth 服务通过。
