# Codex / MiniCode Review 与 Explore 源码审计

审计日期：2026-10-13。工作区：`C:\Desktop\MiniCode`。

本报告记录开始实现前的只读源码状态。随后完成的 inline Markdown review、补齐的 renderer/projector 证据和验证失败/修复记录见 `2026-10-13-codex-markdown-review-inline.md`；不以实现后的状态覆盖本报告原始审计结论。

## 结论

**已安装 Codex 桌面端的 Code review 入口与 Rust 的 `review/start → ReviewTask` 是两条不同调用链。** 桌面端把完整 review guidelines 和目标说明放入普通用户输入，要求普通 Markdown 和 `::code-comment` 内联批注；Rust 原生 ReviewTask 则使用独立 review 上下文、专用 base instructions、Never 审批策略和结构化 JSON 结果。不能将后一条链的 JSON、禁止多 agent、Never 等规则套到桌面端普通 review 入口。

MiniCode 当前 `/review` 与桌面端只共有“普通回合”这一外层入口：它仅展开一句 uncommitted 提示词，缺少目标选择、完整 review guidelines、真实 merge-base、review delivery 和模型批注消费。主对话 `review` / `explore` 持久化模式也只是 MiniCode 自定义提示词，既不启动 ReviewTask，也不应用 Codex 子角色配置。

Explore 同样需要区分。MiniCode 子 agent `explore` 有真实的工具执行层只读限制，使用 CC 来源的角色提示词；Codex `explorer` 是 spawn 时应用的子角色配置。本次参考版本内置 `explorer.toml` 为 **0 字节**，其角色描述用于 spawn 工具指导，不能推导出同样的只读提示词或通用只读 OS 沙箱。

## 证据版本与范围

- Rust 参考 checkout：`.tmp/codex-src`，审计时 `git rev-parse HEAD` 为 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`，官方来源 `https://github.com/openai/codex`。
- 已安装桌面包：`C:/Program Files/WindowsApps/OpenAI.Codex_26.1007.2314.0_x64__2p2nqsd0c76g0/app/resources/app.asar`。
- ASAR 使用现有 `desktop/node_modules/@electron/asar` 直接只读提取到进程内；没有修改安装包。已有提取目录为 `output/playwright/codex-control-audit-20261013/codex-open-source/`。
- 三个已提取主模块与当前 ASAR 中的文件逐字节相同：`app-primary-19fe57fd8c00.js`、`app-initial-b8e1c8014bb4.js`、`app-shared-737655e1fb23.js`。
- MiniCode 依据当前工作区源码，包含现有未提交修改。没有用旧 MiniCode 截图判断当前实现。
- 本次只读审计没有启动任务、调用 provider、运行测试/构建、打开或激活用户窗口。源码结论不等于本机 UI 或 provider 端到端通过。唯一新增文件是本报告。

ASAR 文件是压缩单行源码，以下定位用函数名和 `String.indexOf` 的 **UTF-16 字符偏移**，不是字节偏移。

| ASAR asset | SHA-256 | 主要定位 |
| --- | --- | --- |
| `code-review-aae677399d79.js` | `bee20a5cfea1d5fa4f628cfeb6fcad9b74d50ae47604d4130a591a832ea80119` | `P` 3489；`F` 3583；`I` 4602；长度 6893 |
| `code-review-5897a031c509.js` | `dad9125ef6361a0b9cc9b909825aa6409dd97242fec38ce582f39276cbc07128` | 167 字符的导出桥接：`prepareCodeReviewRequest` / `useStartCodeReview` |
| `review-slash-command-submenu-registration-c60fc0d982c9.js` | `2d5f39cea8f5063b3219a03767fe0b2a55d98d9638c1b66d08996a671244dbbd` | `F`；uncommitted 8082；base-branch 8449；长度 10507 |
| `app-primary-19fe57fd8c00.js` | `15ded0579fb738e12f0e39076d78e8d137848d3ffa78aac1019b0290ae78262e` | `brt` 1032730；`xrt` 1035728；`Nrt` 1039261；composer 挂载 1154554 |
| `app-initial-b8e1c8014bb4.js` | `799331dde54905c4df5e360f23a9621141759e2c0acb230aeab909f9904b646f` | `HGi` 4910587；`oqi` 4946856；`Goa` 5381621；`Yro` 6452178；`joc` 9590736 |
| `app-shared-737655e1fb23.js` | `a1daa891e89a395b8db399ff6eaa365f12a86880c3d8cf891caf4ccbcb9eebf5` | review 标题/请求分隔符导出 |
| `composer-utility-bar-beaa03ca39ad.js` | `927e45a2de3f0e4ae693fc8593d3cb4d5d82c3a24a9fff1f2e5481a7299ba8a2` | 已检查 review/explore 入口相关字符串；长度 138816 |

## 1. 已安装桌面端：目标选择型 review

### 1.1 Composer 的真实入口和提交前上下文

`app-primary` 的 composer 挂载 `Nrt`，传入真实 `conversationId`、`cwd`、host 和 `allowSideChat`。`allowSideChat` 只在 local/main placement 为真（偏移 1154554），因此不能把某个快捷分支描述为所有位置都适用。

`Nrt` 读取当前 conversation 的附加 worktree，使用其 `workspaceRoot` / `root` 或当前 cwd / Git root。`beforeReview` 回调重新读取附加 worktree；若 root 或 workspaceRoot 相比选择时变化，返回 false 并要求重新选择目标；读取失败则保留实际错误。它用于确保分支和权限是当前实际工作区的，而不是某个全局激活目录。

当快捷分支条件不成立、review slash submenu 打开且真实 Git/worktree 条件满足时，动态加载 `ReviewSlashCommandSubmenuRegistration`（`app-primary` 偏移 1044558）。加载占位、读取失败和 retry 都来自真实读取状态。

### 1.2 目标、权限和 Git 解析

`review-slash-command-submenu-registration` 的 `F`：

- 从当前 conversation/cwd/host 读取权限选择、approval reviewer、permission profile；这里的变量名 **`agentMode` 是桌面权限选择，不是 MiniCode 的 build/plan/review/explore `agent_mode`**。
- 读取当前、默认和近期分支，过滤当前分支、去重；默认目标缺失时该源码使用 `main`。
- submenu 显示 uncommitted 和 base branch 两类目标。当前桌面入口没有暴露原生协议中的 commit/custom 目标。
- 权限加载/pending、附件刷新和提交中状态控制可用性；`reviewSubmissionLock.acquire/release` 覆盖真实提交。
- 点击先调用 `beforeReview()`，再提交；命令声明 `requiresEmptyComposer: true`。
- loading、分支读取失败/retry、Git 的 Xcode license 错误都有对应源分支。
- delivery 读取设置，缺省 `inline`；成功后切换真实 diffFilter 和 baseBranch。detached 导航到返回的 conversation；inline 打开 split diff，sidechat tab 情形不强制打开该面板。

`code-review-aae677399d79.js` 的 `F`：

- uncommitted 返回 `diffFilter: 'unstaged'`、`baseBranch: null`，提示词的实际范围仍为 **staged、unstaged 和 untracked**。不要把这个显示过滤名误当作只审 unstaged 的任务范围。
- base-branch 调用真实 `git-merge-base` IPC，携带 gitRoot、baseBranch 和 hostId。没有 mergeBaseSha 就失败，不捏造 SHA；成功后把确切 SHA 放进 `git diff {mergeBaseSha}` 指令。

### 1.3 提示词与结果格式

`N` 中的 `M` 是完整的 `# Review Guidelines`。`P` 精确按下面顺序拼接用户输入：

```text
## Code review guidelines:
<完整 M.trim()>
<目标 reviewInstructions.trim()>
## My request:
<可本地化的用户请求>
```

`M` 明确要求 normal Markdown，**禁止 JSON、XML、findings object 和 structured review schema**。需要行内反馈时输出 `::code-comment{...}`；必填 `title`、`body`、`file`，可选 `start`、`end`、`priority`，使用最短有用范围；无问题时不输出 directive，直接简短说明。

同一资源还规定：只报告变更引入且作者会修的离散问题；依据适用的根/作用域项目指令文件；按真实项目规则贡献判断 rule-supported finding；按位置与缺陷/修复去重并保留规则支持；规则引用必须核验具体文件与最小行范围；collaboration 可用时，每条适用规则至多一个聚焦调查者。单句“优先给 findings”不具备这些行为。

目标说明原文分别为：

```text
Review the current code changes (staged, unstaged, and untracked files) and provide concise, actionable feedback in a normal Markdown response.
```

```text
Review the code changes against the base branch '{baseBranch}'. The merge base commit for this comparison is {mergeBaseSha}. Run `git diff {mergeBaseSha}` to inspect the changes relative to {baseBranch}. Provide concise, actionable feedback in a normal Markdown response.
```

### 1.4 Harness 调用和真实完成身份

`I`（导出 `useStartCodeReview`）在相同 host 的已有 conversation、inline delivery 下调用 `aOt → oqi → client.startTurn`：

```text
turnTrigger: 'code_review'
threadId / cwd
input: [{ type: 'text', text: prepared.prompt, text_elements: [] }]
当前真实 permissions / approvalsReviewer / 活跃 profile 的 runtimeWorkspaceRoots
collaborationMode: null
context: { inheritThreadSettings: false }
```

detached 或不满足 inline 条件时调用 `nkt → HGi → client.startConversation`，传 `threadSource: 'code_review'`、同一 turnTrigger、实际 cwd、`workspaceRoots: [gitRoot]`、权限配置和 `collaborationMode: null`。只有实际 `status: 'created'` 才视为创建成功；首回合拒绝/未启动/结果未确定有独立状态，不抹去已经创建的 conversation 身份。

这些入口**没有调用 `review/start`**，也没有在入口强制 Never、review_model、禁 collaboration 或 JSON schema。不能因为名字叫 review 就替换其权限或 lifecycle。

## 2. 已安装桌面端：有条件启用的 author-review 快捷分支

`Nrt` 在 `allowSideChat`、有效 conversation/Git/worktree 和编译后的 `Wq` 条件均满足时，挂载 `xrt`；其 `/review` onSelect 在 `beforeReview()` 成功后调用 `brt`。`Wq` 还依赖源码 gate 与 host 状态；本次没有验证该 gate 在用户实际 UI 中是否启用。

`brt`（`app-primary` 偏移 1032730）读取真实 base branch、current branch 和 reviewDelivery，调用同一个 `prepareCodeReviewRequest`，然后包入以下执行说明：

```text
Delegate this review to one subagent working in <actual cwd>. Pass it the complete review instructions below and ask it to include staged, unstaged, and untracked files without modifying files. Reuse an active review of these changes.
<Yro>
Keep this conversation available and return the reviewer's findings here with file locations. Do not fix issues unless the user asks. If subagent tools are unavailable, perform the same read-only review here.
<完整 prepared.prompt>
```

`Yro`（`app-initial` 偏移 6452178）的原文规定：新 review **只调用一次** collaboration spawn，`fork_turns: none`；新 child 仅获得 review 上下文和执行说明，不继承当前聊天及附件；告诉它不再 spawn、不改代码、不向外部服务发送/发布内容；启动后只等待同一个 child，不替换、不并行 spawn、不自行重审；child 失败就报告真实失败。入口同时有“复用活跃 review”和“subagent 工具不可用则原地只读审阅”的明确指令，不能实现成失败后再伪造一个已启动 reviewer。

- inline：`xpe → joc → Goa → client.sendFollowUpMessage`，正常 follow-up/admission 路径，turnTrigger 为 `code_review`。
- detached：`CE` 的导出别名也是 `nkt → HGi → client.startConversation`；权限来自源 conversation currentPermissions 与 latestThreadSettings，创建 settled 为真实 `created` 后才导航；创建失败/首回合失败/结果未确定保留其真实证据。

此分支使用同一 Markdown guidelines，依然不是 Rust 原生 ReviewTask。它的单 child 规则也不能机械应用到目标选择型 review 的每个分支。

## 3. Codex Rust：原生 `review/start → ReviewTask`

以下路径相对 `.tmp/codex-src/codex-rs/`。

| 环节 | 源码位置 | 实际契约 |
| --- | --- | --- |
| API target/delivery | `app-server-protocol/src/protocol/v2/review.rs:17`、`:46` | thread_id、target、inline/detached；target 为 uncommitted/baseBranch/commit/custom；response 有 turn 和 reviewThreadId |
| deprecated detached | `app-server/src/request_processors/turn_processor.rs:339` | detached 发 deprecationNotice，推荐 thread/start + inline review，或 thread/fork + turn/start 自定 review 指令 |
| admission/任务提交 | `turn_processor.rs:1437`、`:1555` | 真实加载 thread、target 检查、direct input admission、provider 检查；inline 提交 `Op::Review`；legacy detached 有独立 review-agent skill 路径 |
| 目标解析 | `prompts/src/review_request.rs:9`、`:43`、`:62` | 专用 REVIEW_PROMPT 资源；真实 merge-base 解析；commit/custom 保留各自语义；空 custom 报错 |
| review 上下文 | `core/src/session/review.rs:5`、`:140` | review_model/effort/tier 解析，web search Disabled、Goals disabled；独立上下文；替换活动任务、清 connector selection；EnteredReviewMode |
| 上下文隔离 | `core/src/session/turn_context.rs:403` | developer_instructions None；multi_agent_version Disabled；新 extension/turn state 和 turn_grants；共享 session_grants，保留实际 environments/network |
| reviewer one-shot | `core/src/tasks/review.rs:99` | 专用 base instructions；审批 `Never`；WebSearch Disabled；Collab/MultiAgentV2 disabled；SubAgentSource::Review；initial_history None |
| 事件转接 | `core/src/tasks/review.rs:145` | 不直接透出最后 normal assistant item/delta；TurnComplete 时从 last_agent_message 解析 ReviewOutputEvent；中断输出 None |
| 退出与持久化 | `core/src/tasks/review.rs:212` | ExitedReviewMode 带真实 review_output；记录 review result user wrapper 与可读 assistant 结果；中断有真实可读结果 |

`prompts/templates/review/rubric.md` 要求 JSON findings 和 overall verdict，禁止额外 Markdown/散文和 PR fix。`protocol/src/protocol.rs:3478` 的 ReviewOutputEvent 与 ReviewFinding/ReviewCodeLocation/ReviewLineRange 定义了 title/body/confidence/priority、绝对文件路径及 start/end。

`core/src/tasks/review.rs:193` 的原生 parser 先读严格 JSON，再尝试对象子串，最后把原文放入 `overall_explanation`。这是这一具体原生契约已有的实现，**不是为 MiniCode 普通 review 增加通用兜底/吞错层的依据**。

两点边界：

1. `Never` 是审批策略；`for_review` 保留实际 environments/network，并没有统一把文件系统改成 read-only。不能将“不询问审批”错误解释为“任何权限都可以”，也不能将“review 不生成 fix”错误解释为统一 OS 沙箱设置。
2. `session/review.rs:29` 和 `tasks/review.rs:108` 注释称 view_image 被禁，但已检查实现只显式禁 web search/Goals/Collab/MultiAgentV2。`tools/spec_plan.rs:1358` 仍按真实环境与 Feature::ViewImage 注册工具；在 inspected core production source 中没有找到 review 专用 `disable(Feature::ViewImage)`。本报告不把注释当作已证实的硬限制。

## 4. MiniCode 当前主对话调用链

| 环节 | 当前源码位置 | 实际行为 |
| --- | --- | --- |
| 正常 UI 模式入口 | `frontend/src.v2/composer/ComposerAddMenu.tsx:42`；`FooterRow.tsx:339`、`:447` | 加号菜单只提供 Plan toggle，Plan chip 可关闭；当前没有正常四模式选择菜单 |
| 公共/持久化模式类型 | `frontend/src.v2/stores/types.ts:1520`；`backend/conversations/models.py:46` | build/plan/review/explore 仍是接受的真实状态值 |
| 模式 mutation | `frontend/src.v2/stores/composer-slice.ts:179` | 按 owner 乐观更新；真实 `conversation.agent_mode.set` command.result；pending commandId 与 revision；失败回到真实持久状态 |
| 后端模式保存 | `backend/ws/handlers/conversation.py:3376` | 仅更新 repository、列表与 command result，未启动 review task，也未更改 permission profile |
| 输入/队列归属 | `frontend/src.v2/chat/sendChatMessage.ts:383`、`:409`；`backend/ws/command_dispatcher.py:1214`、`:1307`、`:1326`、`:1648` | 用户输入携带目标 owner 的 agent_mode；队列/admission 使用模式 revision |
| 运行时来源 | `backend/ws/agent_runner.py:3966`、`:4079`、`:4878` | 从实际 conversation record 填 mode；stream 记录 mode；已接受回合提交模式 revision |
| 恢复/同步 | `frontend/src.v2/stores/ui-slice.ts:588`、`:615`；`frontend/src.v2/chat/sessionEvents.ts:285`、`:906`、`:1497` | 持久化模式恢复到 owner workbench 与 UI state |
| collaboration mode | `backend/agent/loop_session.py:204` | 只有 plan 选择 Plan；review/explore 选择 Default；权限 mode 独立 |
| 模式提示词 | `backend/agent/context.py:1879`、`:2119` | build/plan 不再叠加额外 agent-mode 文本；review/explore 加 MiniCode 自定义 `<agent_mode>` user runtime prefix |
| 工具面 | `backend/agent/tool_schema_derivation.py:96`；`loop_components.py:146`；`backend/tools/toolset_runtime.py:30` | 根据真实权限/registry/禁用工具/workspace/tool mode 等选工具；没有主 review/explore 的 ReviewTask 或子角色 enforcement |

`context.py:2119` 的 review 文本只是 “Review first: prioritize bugs, regressions, risks, and missing verification…”，explore 文本只是 “Map the problem space, gather evidence, and explain options before committing to changes…”。因此 review/explore 持久状态可经旧记录、store/API 或显式 user_message 生效，但不是 Codex 原生审阅任务/子角色；也不能用 UI 没有选择项推导为后端不可达。

### 4.1 `/review` 真实链与缺口

`backend/commands/catalog.py:32` 的 `_REVIEW_PROMPT`：

```text
Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings.
```

这与本次 Rust `prompts/src/review_request.rs:18` 的 uncommitted target 句子相同，**不包含 Rust REVIEW_PROMPT，也不包含已安装桌面端 Markdown guidelines**。catalog `:256` 把它声明为 template。

前端 `frontend/src.v2/lib/runtime-commands.ts:283`、`:336` 走普通 template/tokenize/sendChatMessage；后端 `backend/commands/slash_commands.py:750` 展开模板，发 prepared-prompt command result，然后 `return False, prompt` 继续普通用户回合。prepared-prompt 回执不代表实际 model turn 已接受。

当前没有该命令的真实 base-branch 目标选择、merge-base 准备、delivery 生命周期、author-review 单 child 执行说明或 Code review 批注消费。已有 `DiffPanel.tsx` 的 `diffReview` / `lineComments` 服务于工具修改审阅和人工行评论，不能仅凭同名 `review` 视为已支持模型 `::code-comment`。

对 `backend` 和 `frontend/src.v2` production 源码搜索 `::code-comment|review_output|ReviewStart|EnteredReview|ExitedReview|review_model|reviewThreadId` 未找到对应消费者；结合上述实际入口可确认目前没有这两条 review 契约的显式对接。此为所检索范围的源码证据，不声称已运行每个 provider 或第三方扩展。

## 5. Codex 子角色与 MiniCode 子 Explore

### 5.1 Codex 角色配置

`models-manager/src/collaboration_mode_presets.rs:6` 只有 Plan/Default，其开发者消息来自 bundled model messages。`explorer` / `worker` 不在该模式表中。

`core/src/agent/child_config.rs:51` 从当前父 step/context 构造子配置，应用实际 model/effort/runtime 权限；`:73` 再按 fork/version 规则应用 role。V1 full-history fork 不接受 role override；V2 支持显式角色层。它不是主 conversation 切换一个 explore mode。

`core/src/agent/role.rs:51` 解析自定义角色优先于 builtin；`:69` 读取角色层并对 developer instructions、model、effort、summary、verbosity、personality、service tier 和源码允许的 features/skills 收窄应用 override；`:187` 将 developer instructions 放入真实 config。角色文件由 `agent-roles/src/agent_role_config.rs:39` 按 TOML 解析，metadata 与可执行 config 分开。

`role.rs:350` 起的 builtin 为 default/explorer/worker。explorer description 要求聚焦代码问题、复用已有 explorer、不同问题可并行；这是工具角色指导。`core/assets/agent/builtins/explorer.toml` 经文件长度验证为 0，不能虚构额外 developer prompt、模型或通用 read-only 合同。

### 5.2 MiniCode 子 Explore：确有执行限制，但语义不同

- `backend/tools/subagent_catalog.py:5` builtin 是 general-purpose/explore/plan；与 Codex 名称和角色表不同。
- `backend/tools/subagent_context.py:734` 的 explore 提示词明确注释来自 `cc exploreAgent.ts`，包含不创建/修改/删除文件、专用搜索工具、shell 不可用等 CC 角色文本。它不是空 `explorer.toml` 的拷贝。
- `backend/tools/agent_tools.py:765` 强制 explore/plan 的 `read_only: True`；运行记录/context metadata 传递在 `:2034`、`:2521` 等位置；`:2581` 还把 child 角色送入 prompt context。
- `backend/tools/subagent_context.py:593` 计算 forced_read_only，但实际 permission mode/approval/network/sandbox 从父真实快照保留。`:628` 的 sandbox 改动来自 plan、更窄 permission 或父 read-only；只读任务标志本身不统一替换 OS 沙箱。team 的只读合同另禁 ExitPlanMode，防止解除合同。
- 真正工具拦截位于 `backend/agent/tool_execution.py:662` / `:673`：read_only child 对 `tool.is_read_only(arguments)` 判定为非只读的调用直接 blocked。普通 batch 在 `tool_batch_execution.py:720` 拦截；最终请求授权 `tool_execution.py:261` 仍检查 scope，涵盖 hook/编辑后的实际调用。
- CodeMode 内层也保留 `cell.parent.metadata` 并调用同一 `ToolBatchRunner`（`backend/agent/code_execution.py:175`、`:183`；`tool_batch_runner.py:56`），因此本次检查的 CodeMode 入口没有丢失 read_only 限制。工具 metadata 的 `registry.py:832` 只是视图信息，不能当作 sole enforcement。
- `backend/agents/loader.py:1`、`:66`、`:145` 使用 MiniCode Markdown/YAML frontmatter agent 定义，提供 prompt/model/tools/disallowed_tools/effort/permission_mode/background 等现有行为；不是 Codex TOML role adapter。
- `backend/hooks/runners.py:566` 也使用 explore prompt_context 做内部 verifier。不能只删掉 root UI 类型就假定其余消费者不受影响。

## 6. 已证实差异、根因与完整修复范围

| 差异 | 根因 | 应修的调用链 |
| --- | --- | --- |
| `/review` 没有桌面完整指南/结果格式 | 将 Rust target 单句当成完整 review 产品功能 | catalog/command metadata → review request preparation → ordinary turn input → Markdown directive renderer/diff owner |
| 不支持真实 base branch | review 仅 template，没有目标/仓库准备阶段 | composer slash submenu → owner cwd/worktree → actual branch list/merge-base → prepared target → turn admission |
| 没有桌面 review delivery/真实创建身份 | 普通 template 没有 review action 语义 | inline owner admission；detached creation + first-turn receipts；真实 identity/navigation/diff selection |
| 快捷 review 无单 child/no-history 指令 | 未接入 installed author-review 分支 | 适用入口条件 → 当前上下文刷新 → exact execution prompt →真实 spawn/final/failure record |
| 模型批注不能进入 diff | 现有 lineComments 不是 code-comment consumer | 流式/最终 Markdown extraction → owner conversation/turn/file/line → diff presentation/cold replay；保留原始回答 |
| 主 review/explore 与原生 task/child role 混淆 | 用一个持久 agent_mode 标签表达三个维度 | conversation collaboration mode、review action、spawn role 分开；审计历史/API/hook 消费者后处理遗留模式 |
| 子 explore 被误当 Codex explorer | builtin 名称、CC prompt 和 read_only 合同沿用 | role declaration/config adapter → spawn config/prompt priority →实际工具面/权限；现有用户自定义 agents 保留身份 |

### 6.1 优先对齐已安装桌面入口

1. 在真实 slash review action 建立 uncommitted/base branch 请求，不新增四模式 selector。明确目标 owner/cwd/worktree，使用现有真实 admission/command.result 机制；不能把 `agent_mode='review'` 永久写回作为审阅任务的实现。
2. 将已核验 ASAR 的完整指南与目标模板保存为带版本/来源的资源，并按实际用户输入顺序使用。base-branch 先解析真实 merge-base；失败保留实际失败。不要猜提示词、以 HEAD 伪装 merge-base，或在前端写一个空 review 占位。
3. 实现当前所需的 inline/detached 行为，沿 owner 归属、创建/首回合 receipt、diffFilter/baseBranch 和导航链完成；已创建而首回合失败的身份必须可查。不能把 prepared-template ACK 或 create request 发送视为开始工作。
4. 实现 normal Markdown `::code-comment` 的解析、owner 定位和 diff 呈现。利用现有 diff 工具可用部分，但与人工 line comments/审批区分；原始模型文本和无问题/中断/失败结果仍可查。具体 malformed directive 行为需读 installed renderer 后再定，不在本次审计中猜兜底。
5. 若启用对应主 composer 的 author-review 快捷分支，完整接入单 child、fork none、无附件继承、禁止 child 再派遣/修改/外发、复用真实活跃 review 和失败呈现。按真实触发条件实现，不把功能 gate 分支扩大为全局默认。
6. 明确旧 review/explore record/API 的产品定位，先处理全部消费者与回放归属，再决定是否移除/迁移。不能清空用户草稿/附件、覆盖历史、删掉有实际用处的 hook/child 配置来隐藏差异。

### 6.2 原生 ReviewTask 仅作为独立目标实现

若产品明确要支持 Rust 原生 review API，另做 ReviewTarget/ReviewDelivery、ReviewStartResponse、专用 review context/model/审批策略、真实 entered/exited lifecycle、结构化结果和中断/持久化完整链。按原生协议测试；不将其 JSON/Never/禁多 agent 注入桌面 Markdown review 路径，也不把 legacy detached 当新接口首选。

### 6.3 子角色对齐

按实际 Codex role 配置建立角色声明与子配置应用链：description 属于 spawn 指导；developer instructions/model/effort/features/skills 属于真实子配置；父当前权限及 fork version 规则单独处理。内置 explorer 以空资源的实际语义为准。现有 MiniCode explore 的只读合同和用户 agent 文件属于已有产品状态，迁移需明确，不通过简单别名替换意外扩大权限，也不额外增加没有源码依据的通用安全/兼容层。

## 7. 后续一次性验证范围（本次均未运行）

功能调用链补齐后统一增加必要测试并运行，避免每补一项就重复全套：

- prompt/target：核验完整资源和拼接顺序、staged/unstaged/untracked 范围、真实 merge-base、无共同祖先/branch 读取失败；明确桌面 Markdown 与原生 JSON 两条契约不能混用。
- admission/owner：原工作区/附加 worktree 变更、并发选择、inline 切会话、pending permissions、detached 已创建但首回合失败、unknown outcome 与迟到 receipt；草稿和附件仅在真实接受后处理。
- review result：有/无批注、文件范围定位、多文件、流式结束和冷恢复、中断/失败原始证据；不得把失败或跳过计为通过。
- author-review：真实一次 spawn、fork none、只等同一 child、复用 active review、未启动/失败不伪造头像短行；外部通信和修改行为遵循真实指令与权限。
- role：default/explorer/worker 声明、空 explorer 配置、自定义 override、fork version、model/effort 选择和当前父权限；现有只读子合同通过 direct、CodeMode、hook 修改调用的一致执行入口。
- UI：使用当前构建与实际 installed Codex 截图/操作对照 review submenu、loading/retry、inline split diff、detached 导航、批注和过程状态；本次未进行此检查。

## 8. 验证状态与已知限制

| 项目 | 状态 | 证据/限制 |
| --- | --- | --- |
| Rust revision | 已核验 | rev-parse 输出上述 SHA |
| installed ASAR 入口与 alias | 已核验 | 直接读 package；主模块与已有提取字节一致；函数/偏移/哈希如上 |
| MiniCode mode/slash/child 执行调用链 | 已核验源码 | 当前 source reads/search；未调用 provider |
| 主 Explore 桌面入口 | 在所检查入口中未找到 | primary composer/review 模块、utility bar 没有对应 root Explore action；不能等价为整个应用/所有 gate 永不存在 |
| 原生 view_image hard disable | 未证实 | 注释与已检查注册条件不同；没有将注释写成执行承诺 |
| 当前 UI/provider 运行验证 | 未运行 | 本次明确只读；不打开用户窗口 |
| 测试/构建 | 未运行 | 本次明确不运行；此前其他批次结果不作为本报告通过证据 |

检索中的不存在路径/引号错误已按实际 `rg --files` 路径和有效表达式纠正后重新读取；没有把这些失败检索算作“未实现”的证据。后续实现已读完 installed Markdown directive renderer 与用户消息 projector：`Bii/zii/Aii/a1 → Vii/Hii → Mmc`、`Gol/y9` 和 `owr/zp` 的精确定位、批注与请求投影、真实验证失败及修复均记录在 `2026-10-13-codex-markdown-review-inline.md`。
