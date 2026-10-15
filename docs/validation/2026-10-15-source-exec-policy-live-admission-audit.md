# Source exec policy 与运行中模型切换准入审计

> 后续 caller 核实与实现更正：普通 Codex 桌面模型/effort 按钮写 thread 下一回合默认，不能直接套 core live gate。原审计记录下面保留为当时只读发现；它不再代表当前 model.set 行为。后续已拆分 next-turn selection 与显式 step activation，并完成相应真实验证，见 [模型选择与运行步骤准入](2026-10-15-source-model-selection-and-live-activation.md)。原生 `.rules`/PrefixRule/持久 amendment 仍未实现；ignore_rules 目前只在 live admission 比较分类，未接命令 allow-prefix 过滤。

2026-10-15，只读核查。Source checkout 为 `.tmp/codex-src`，commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。本次只新增这份审计记录，未修改产品、未实现下述差距、未运行新测试，也不从既有测试通过数推导本链路已经通过。官方账号登录、授权和接入按用户要求排除。

## 结论

MiniCode 目前没有真正的 Source `.rules` / `PolicyParser` / `PrefixRule` 生产链。`auto_review.ignore_rules` 能校验、保留并按层合并，但没有运行时模型消费。已有内容规则、命令意图列表和危险命令 argv 判断分别是其原有能力，不能算 Source 原生前缀策略。

现有普通 AUTO 提前执行没有因为 `required_on_models` 而统一进入 Guardian，这一点与 Source 的 safe `Skip` 分支一致，不应为补 `ignore_rules` 把所有安全命令强制复审。真正缺的是 Source prefix-policy producer、按 admitted model 过滤 Allow PrefixRule、持久 amendment 以及运行中模型切换的授权准入边界。

MiniCode 已有不可变模型快照、下一 provider step 激活、小上下文窗口切换时压缩及进行中请求保留原模型。它们不等于 Source `step_activation` 的全部授权限制。当前有两个真实模型发布入口，都缺相应的统一准入检查。

## Source 实际调用链和语义

下列路径均相对 `.tmp/codex-src/codex-rs/`。

| 源码位置 | 实际行为 |
|---|---|
| `core/src/exec_policy.rs:662`，`load_exec_policy` | 按可信 config stack 从低到高读取各 config folder 下 `rules/*.rules`；disabled project layer 已体现 trust；单独的 CLI ignore-user/project 配置跳过指定来源。使用真正 `PolicyParser`，最后合并 managed requirements exec policy。 |
| `execpolicy/src/parser.rs:57`，`PolicyParser::parse`；`:349`，`prefix_rule` builtin | `.rules` 是 Starlark，支持真实 pattern token/候选项、allow/prompt/forbidden 决策、match/not_match 示例校验与 justification；同 parser 也管理 network rule、host executable 数据。不是 Python eval、JSON、命令字符串 startswith 或 `Tool(content)` glob。 |
| `execpolicy/src/rule.rs:111`，`PrefixRule` | 具体规则类型包括 tokenized PrefixPattern、Decision、justification；按具体类型过滤具有实际含义。 |
| `core/src/session/turn_context.rs:647`，`allow_prefix_rules` | 从 admitted turn 的 frozen config/model 读取 `model_specialty == cyber` 或 `auto_review.ignore_rules` 中的模型 slug，得到 Honor / IgnoreForCyberModel。与 `required_on_models` 是不同维度。 |
| `core/src/exec_policy/model_policy.rs:27`，`current_for_prefix_rules` | Ignore 时只删除当前 policy 中 `PrefixRule && decision == Allow`；不删除 prompt、forbidden、其他规则种类、network rules 和 host executable 数据。 |
| 同文件 `:15`，`current_for_environment` | 先执行上述过滤，再合并实际 environment exec-policy overlay。不能把环境 overlay 的 allow 也顺手删掉，或把 ignore 理解为忽略全部规则。 |
| `core/src/tools/handlers/unified_exec.rs:29`，`ExecCommandArgs`；`core/src/unified_exec/process_manager.rs:1480` | 真实命令请求含 `prefix_rule: Option<Vec<String>>`；构造 ExecApprovalRequest 时携带 captured step 的 approval policy、环境 permission profile/exec policy、sandbox intent，以及 admitted turn 的 allow_prefix_rules。 |
| `core/src/exec_policy.rs:327`、`:337`、`:876` | shell/PowerShell 解析得到实际子命令 argv，再做 `check_multiple_with_options` 和 Source unmatched-command heuristics；产出 Forbidden / NeedsApproval / Skip。只有所有子命令都被真实 execpolicy allow 时，Skip 的 bypass_sandbox 才为 true。普通 heuristics Allow 也可以是 Skip，但不因此绕开实际 sandbox。 |
| `core/src/exec_policy.rs:358` | `auto_amendment_allowed` 仅在 Honor。Ignore 模型不能由当前命令自动提出可重复使用的 prefix amendment。 |
| `core/src/tools/orchestrator.rs:168` | `Skip` 仅在 `strict_auto_review` 时进入 request_approval；普通 Skip 记 Config Approved 并执行。`required_on_models` 不把所有 safe Skip 变成强制 Guardian 请求。Forbidden 保持拒绝，NeedsApproval 才进入真实审批。 |
| `core/src/session/handlers.rs:174`；`core/src/session/mod.rs:2630`；`core/src/exec_policy.rs:464` | 用户真实 ApprovedExecpolicyAmendment 回执触发持久化；将 allow prefix 写入 `rules/default.rules`，更新 in-memory Policy，再完成原审批等待。不是只在前端显示一条记忆规则。 |
| `core/src/session/world_state.rs:174` | 生成 permissions instructions 时也使用当前 admitted turn 的 `current_for_prefix_rules`，模型所见策略不能仍宣告已被 Ignore 过滤的可重用 prefix。 |

注意两种 ignore 的区别：CLI `--ignore-rules` 对应 config stack 的忽略用户/项目来源；`requirements.auto_review.ignore_rules` 是模型 slug 列表，控制上述 Allow PrefixRule 过滤。二者不能混为一个开关。

## MiniCode 当前接入和缺口

1. **配置层已接，运行消费未接。** `backend/config_requirements.py:105` 实际实现的是 `auto_review_required_for_model`；`:456` 同时校验 `required_on_models` 和 `ignore_rules` 字符串数组；`:912` 合并 required models，`:915` 保留高层指定的 ignore_rules 或继承低层值。生产搜索中 ignore_rules 的非文件搜索用途只到这些位置，没有命令判定、模型快照或提示词 consumer。因此不能把字段出现在配置投影中算作生效。

2. **没有 Source `.rules` parser/manager/producer。** 当前 backend 生产代码没有 `PrefixRule` / `prefix_rule` 的实现、规则文件读取或可持久 prefix amendment。`backend/tools/command_tool.py:263` 的真实 schema 是 MiniCode run_command 参数；不存在 Source `prefix_rule` 参数。`backend/permissions/argv_rules.py:1` 判断危险 argv，不解析 Source policy。`backend/permissions/rules.py:14` 是文件访问匹配，不是 Source execpolicy。项目 Markdown/MDC 指令规则也不能当 Starlark exec policy。

3. **内容规则是另一套实际规则。** `backend/permissions/content_rules.py:78` 解析 `Tool(content)`，`:151` 明确使用 CC 风格 `prefix:*` 或 fnmatch。当前合法命令名字是 `run_command(...)`，`:65` 明确拒绝旧 `Bash` 名字；这里提到 Bash(content glob) 是区分语义来源，不代表当前支持 Bash 旧入口。`backend/permissions/checker.py:1063`、`:1246` 消费这些内容规则，不能把它们贴成 Source PrefixRule，也不能为 ignore_rules 直接清空全部 content allow。

4. **command_prompt allow 不是 Source prefix。** `backend/ws/permission_runtime.py:167` 只把已批准 prompt 意图合并进 PermissionContext，`:195` 之后按 session_prompt_rules 投影；`backend/permissions/context.py:75`、`checker.py:1090` 和 `agent/tool_batch_execution.py:2094` 等负责保存/传递。没有找到它在当前本地 checker 决策中等同 Source token PrefixRule 的消费，更没有 `.rules` 持久化和 Policy 更新。

5. **safe AUTO 提前执行目前不是缺口。** `backend/agent/tool_batch_execution.py:905` 将 AUTO 调用直接放入执行队列；`:1826` 的 `review_tool_request` 位于真实串行审批路径。`backend/agent/guardian/tool_approval.py:20`、`guardian/service.py:73` 的 required-model authority 在该审批路径使用。`backend/permissions/checker.py:1316`、`:1425` 与 command_tool.py:239 让真实 strict grant 的普通命令进入 CONFIRM；MCP 有自己的严格路径。不要由 required_on_models 反向改变普通安全 AUTO。这份审计不声明所有工具和 strict 子类已验证，只确认不能新增“所有 safe Skip 强审”语义。

6. **模型前缀指令未接。** MiniCode 没有与 `world_state.rs:174` 相应的 Source execpolicy 视图，所以未来只补执行判定仍不完整；提示词中可复用命令权限必须来自同一份实际 policy。

## Source 运行中模型切换的边界

真实入口为 `app-server/src/request_processors/turn_processor.rs:212` 的 turn/settings/update，提交指定 thread/turn 的 Op::TurnSettings；`core/src/session/handlers.rs:601` 调用 `step_activation.rs:229` 的 apply_turn_settings。

- `step_activation.rs:240`：涉及模型/effort/summary/service tier 的更新需要 StepModelSwitching feature。纯 reviewer/environment 有相应独立分支，不能把 feature 限制扩大成所有设置都不能改。
- `:249` 捕获**原来指定的**活动 task、TurnContext、done identity 和 current settings；异步解析之后 `:305` 再核相同 task.done/context 指针且未取消。完成、替换或同 turn ID 复用时返回 TargetUnavailable，不改后来出现的任务。
- `:383` 以当前 requirements、Guardian feature、trusted reviewer 和实际环境权限解析目标模型设置；`:330` 之后在最终发布前重新校验最新 managed authorization 和环境 owner/cwd。`next_step_settings.store` 到 `:374` 才真正发布，更新从请求到完成需要串行。
- `:30` 的 check_legacy_turn_safety 要求 destination/current/admitted 的 approval policy、required-model authority 和 ignores-prefix 分类保持一致。admitted prefix 来源仍是原 TurnContext，不能只看新模型字段。
- `:88` 的 check_legacy_model_safety 还限制 fallback model metadata、Cyber Guardian rejection class、computer-use/node REPL review requirement、Guardian coverage、node REPL availability 和显式 reviewer model override。
- `:145` 开始按 feature 和覆盖条件比较 Guardian V2 分类配置，规范化默认空 transcript；若无显式 reviewer override，`:181` 起比较 parent-fallback node REPL policy、resolved Guardian policy 和 policy template。这些是当前 Source 对仍读取 admitted turn 的消费者设置的限制，不能凭“新模型支持 tools”代替。

## MiniCode 两个真实 live 发布入口

**UI/WS 选择模型：** `backend/ws/handlers/misc.py:305` → `command_handlers.py:340` set_selected_model → `:175` _set_selected_provider_model。它能定位原 conversation、读取其 workspace config、解析可用 model/effort、准备 adapter。`:317` 先保存 conversation model_selection，`:325` 由 `:14` publish_live_model_execution → `backend/ws/run_manager.py:883` 将 snapshot 写入当前 conversation 的 RunContext。

这条链目前没有上述 Source authority/ignore-prefix/Guardian metadata 分类比较，也没有在异步 auth/catalog 解析后校验最初 task/done identity。`publish_model_execution` 只重新取当前 conversation 的 task/context；如果原任务在等待期间结束并被同 conversation 的新任务替换，当前代码没有 Source 的“不得把更新转投替代 task”约束。这是静态调用链差距，未用新测试声称复现。

**Extension 选择模型/effort：** `backend/ws/agent_runner.py:2885` 的 _set_model 在解析和 credential 刷新后调用 `:3018` _publish_model_selection；该函数直接给 `selection_owner.model_execution` 赋值，根 owner 另持久化 model_selection。`:3055` 的 _set_thinking_level 也调用它。它绕过前述 RunManager 发布入口，因此以后只在 UI handler 加检查仍会漏这一条。

**已接通的部分：** `backend/agent/model_execution.py:18` 的 ModelExecutionSnapshot 冻结实际 config/model；`backend/agent/turn_iteration_runtime.py:122` 在下一 provider iteration 读取新 snapshot、必要时压缩较小 budget，再由 `:85` 激活 adapter、budget、工具 schema 和上下文；在途 provider request 与已经捕获的工具仍持有原 snapshot。不能把“下一 step 确实切到模型”当作 Source live admission 已完整。

## 未来一次完整可实施的接入链

以下是待实施设计，不是已有功能或新验证结果。应整批补齐后统一验证，不能只加 ignore_rules 字段或单独前端控件。

1. 从 `load_config` 已有 ConfigLayerStack.get_layers 读取实际可信层路径，按 Source 顺序加载 `rules/*.rules`，保留 path/line 与解析错误，合并 managed exec-policy。完整语法可以直接复用固定版本 Source `codex-execpolicy` Rust crate 作为随产品打包的本地 helper，核心 command parsing/heuristics 与 model_policy 仍按 Source 接入；当前并没有该 helper。不要依赖临时 `.tmp` 路径出货，也不要用 regex、Python eval 或 CC glob 冒充 Starlark parser。

2. 在真实 turn admission 捕获 model/config/exec policy。实际 run_command 请求生产 token prefix proposal，绑定原 canonical FinalToolRequest digest；由 Source shell/PowerShell 子命令语义解析后，先按 admitted Cyber/ignore_rules 分类过滤**当前 Allow PrefixRule**，再合 environment overlay，生成实际 Forbidden / NeedsApproval / Skip、bypass_sandbox 和允许提出的 amendment。

3. 把真实 producer 接在 `PermissionChecker` 冻结判定与 `tool_batch_execution` 分流之前；保留已有明确 deny 与其他来源的真实状态，不把 content allow 重新解释为 native prefix。Skip 继续原 AUTO 队列；只有 strict_auto_review 使 Skip 进入审批。NeedsApproval 继续真实 user/Guardian authority 路径，Forbidden 拒绝。required_on_models 控制已有审批的 reviewer authority，不创造全部 safe Skip 复审。

4. 审批 response 的真实 native prefix-amendment 选择绑定原 owner/request，持久写入实际规则文件并更新 policy，再恢复原等待；Ignore 模型不生成可重用 amendment。将同一过滤后的 policy 供权限提示词使用，闭合“模型所见 → 命令判定 → 审批 → 持久化 → 下一次调用”全过程。

5. 将 UI/WS 与 Extension 两个 live 发布入口都接到同一实际 activation 边界：捕获原 task/RunContext 和 admitted snapshot；异步解析目标；以最新 requirements 复核 Source 列出的 approval/prefix/Guardian 分类与元数据来源；比较原 task identity、取消状态和实际权限；通过后才原子发布 next-step snapshot 与需持久的选择。失败或 TargetUnavailable 保持原任务、模型选择和草稿，并投影真实回执。不能只在一个按钮 handler 加比较，也不能补一个从未参与决策的字段。

上述 `.rules` producer、ignore_rules runtime、amendment 和 live admission 目前均不能记为完成。后续需要整批真实 argv/allow-prompt-deny、ignore 前后保留 deny/network/host/环境 overlay、safe Skip 与 strict、amendment 冷恢复、模型跨 authority 拒绝和任务替换身份场景验收；本审计未运行这些验证。
