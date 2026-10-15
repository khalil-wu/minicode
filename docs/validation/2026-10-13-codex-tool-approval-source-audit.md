# Codex 命令、文件操作与审批调用链审计（2026-10-13）

参考 checkout：`.tmp/codex-src`，commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。前五节记录修改前的源码基线；没有用工具名称差异推导能力缺失，也没有将静态阅读算作运行通过。审计阶段未运行测试/构建、未操作用户窗口、未改生产代码。随后按授权单独实现 `run_command` shell/login/tty，实现与验证见追加章节。权限请求和审批 hook 保持只读交接；文件 patch 策略由 root 独立修改，本报告前五节不冒充修复后的状态。

## 结论与优先级

| 项目 | 当前实际情况 | 对齐范围 |
| --- | --- | --- |
| 冻结工具请求、hook 改参后重新授权 | MiniCode 已存在，不能再登记为“审批后随意改参”缺口 | 保留现有链，补权限能力时复用，不再搭第二套审批注册表 |
| 命令等待、原始输出、退出码、stdin、精确取消 | MiniCode 已有 conversation/task owner 和持久输出；`monitor` 覆盖 Source 的等待/写入用途 | API 名字不必改；PTY、shell/login 和保留进程的权限语义另有真实差异 |
| shell/login/tty | 当前 model 与 execution schema 均未暴露这三个参数，命令 manager 实际用 pipe | 可独立补全，但必须贯穿 SandboxRunner，不能用 host PTY 绕过受限执行 |
| stdin 的 launch authority 与当前策略比较 | Source 默认启用；MiniCode 保存 sandbox_policy，却只按当前普通工具请求做审批 | 属于实际缺口。不能把“monitor 一律弹审批”说成已实现 Source 策略漂移检查 |
| 指定路径/网络的权限请求与 turn/session grant | Source feature 默认关闭；MiniCode 有内部 profile 合并，尚无 model producer/审批子集/owner grant consumer | 保留为带 feature gate 的能力缺口，不能默认给模型开全权限，也不能把 tool/session allow 当路径 grant |
| 文件读取与 patch | 基本读写/范围/hash/diff/部分提交已存在；MiniCode 的强制 read-before-patch、保护路径与 Source 策略不等价 | 单独修文件策略/提交链；不能仅删除检查或隐藏报错来宣称一致 |
| `fs/*` | Source 是客户端 app-server 文件 API，不是模型的 `read_file` 工具 | 客户端文件操作和模型权限分别审计，禁止混为一层 |

## 1. Source exec 与 MiniCode command 的实际链

Source 模型入口是 `core/src/tools/handlers/shell_spec.rs:24` 的 `create_exec_command_tool_with_environment_id`：`cmd`、`workdir`、`tty`、`yield_time_ms`、`max_output_tokens`，按配置附带 shell/login/environment_id 与审批参数；strict=false，additionalProperties=false。`tty=false` 默认是 plain pipes，并非工具 description 中出现 PTY 就必然创建 PTY。

实际调用链：

1. `core/src/tools/registry.rs:609` 运行 PreToolUse；`:635` 用 `with_updated_hook_input` 重建 invocation，`:695` 才进入实际 handler。
2. `handlers/unified_exec/exec_command.rs:186` 先解析 environment/workdir，`:232` 才以对应 cwd 解析完整权限字段。`:248` 解析 sandbox intent；`:288` 经 `unified_exec.rs:100::get_command` 选 shell/login 并产生 argv。
3. `exec_command.rs:317` 到 `:364` 区分请求的 additional permissions 与已有环境/turn grant，判 feature 与 approval policy，得到规范化的有效权限。
4. `exec_command.rs:372` 先识别命令中的真实 apply_patch，导回 patch handler 的同一权限/执行链。普通命令在 `:413` 构造 `ExecCommandRequest`，包含实际 argv/cwd/sandbox cwd、environment、tty、权限、justification/prefix_rule。
5. `core/src/unified_exec/process_manager.rs:1478` 创建 shell exec approval requirement，`:1524` 交给 `tools/runtimes/unified_exec.rs` 与 `tools/orchestrator.rs:125`。Orchestrator 的 `:164` 判 Skip/Forbidden/NeedsApproval，`:202` 构建该 req 的 approval action，`:226` 起选择实际 sandbox，`:380` 执行同一 req。失败升级由真实 sandbox denial 与 policy 决定，`:436` 明确 Never/OnRequest 不能泛化重跑成 unsandboxed。
6. `exec_command.rs:433` 区分 interactive 与 one-shot：one-shot 移除 tty/yield/session_id（`:478`），有真实 completion timeout；interactive 产生可续用 session。终端 sandbox denial 保留真实 exit/output 且不返回虚构 live process（`:442`）。

MiniCode 链：`tools/command_tool.py:108::model_schema` → `:314::get_execution_schema`（同一 model schema，不能拿 host-only 字段当模型能力）→ `agent/tool_batch_execution.py` 最终授权 → `tools/command_tool.py:328::execute` → `:394::_workspace_sandbox_policy` → `:465::_execute_background` / `_execute_foreground` → `terminal/manager.py:224::run_background` → SandboxRunner 实际 spawn。模型 command/raw public text 与 effective shell command 分离（`command_tool.py:512`），持久输出/byte cursor 与真实退出码由 process owner 消费，不能将 yield 当退出。

已具备而非缺口：普通 task 的 yield/live handle；后台命令；精确 stdin bytes 和 close；cancel owned process tree；完整输出持久化，head/tail 预览、cursor 读剩余；conversation/task scope；SandboxPolicy 和 denied-read 保留的升级（`sandbox/policy.py:470`）。

实际参数/行为差异：

- model/执行 schema 只有 `command/cwd/env/yield_time_ms/max_chars/run_in_background/timeout/description/with_escalated_permissions/justification`。未出现 shell/login/tty，当前 `_host_shell_command` 固定 host 默认 shell，background manager 使用 pipe。Source PTY 的交互程序能力不由现有普通 stdin pipe 提供。
- Source `get_command` 使用 `args.login.unwrap_or(allow_login_shell)`，allow=false 时显式 login=true 返回模型错误（`unified_exec.rs:106`）；最终平台参数由 `core/src/shell.rs:24::derive_exec_args` 产生，不能笼统把所有 shell 当 `-l`。
- Source shell 参数是 shell binary/path；这是执行环境的能力，不是输入摘要 label。MiniCode 内部前台 shell 适配不代表模型能选择 shell。
- Source exec 输出 token budget / write_stdin 默认 draining recent output，与 MiniCode max_chars + 可重读 durable byte cursor 是不同输出契约。已有输出不丢，不能将字段名称差异算作丢输出。严格 token budget / 默认只返回新增输出仍须另核。
- Source 本地/remote environment 的 cwd/URI/permissions、zsh-fork、one-shot tool profile 与 MiniCode 单本地 workspace/后台 API 不同；这一批不能冒充实现全部 remote executor。
- 未在 MiniCode `run_command` 中找到 Source `intercept_apply_patch` 等价调用。description 建议使用专用工具不是实际 interception；shell 内 patch 的准入/变更记录要单独处理。

feature 的真实默认值：`features/src/lib.rs:1041` UnifiedExecTty=true，`:1117` StableEnvironmentTools=false，`:1285` ExecPermissionApprovals=false，`:1291` WriteStdinApproval=true，`:1303` RequestPermissionsTool=false。`tools/spec_plan.rs:1171` 决定 login schema 可见性。不能将未开启的新权限请求工具说成所有 Codex 默认运行都有，也不能因此忽略默认已有的 stdin approval。

## 2. Source stdin 权限快照与 MiniCode monitor

Source `handlers/unified_exec/write_stdin.rs:84` 解析 session/input，`:102` 交原 session 的 process manager。`write_stdin` 可空写 poll；它不创建新的普通 Bash PreToolUse（`:144`），终端完成的 poll 则补原命令配对的 Bash PostToolUse（`:151`），使用结果里的原 tool_use_id/input。

Source 权限不是只看 stdin 请求本身：

- `core/src/unified_exec/stdin_approval.rs:32::TerminalPermissions` 保留启动时 policy、native/executor source、launch SandboxPermissions、filesystem escalation、agent additional grant 与 runtime internal grant。`:88::for_launch` 捕获成功启动的实际 authority；内部路径不得序列化进 approval。
- `:119::review_requirement` 将旧进程快照与当前环境 policy 比较。当前出现 denied-read 而旧进程 bypass/快照不一致时，审批不能把沙箱补装到正在运行的进程，要求新建 terminal（`:125`）。保留升级、policy/network 漂移需重新审批；只有 runtime internal grant 不单独要求审批（`:136`）。
- `:190::stdin_approval`：空 poll、非 tty 的纯 Ctrl-C、feature 关闭均跳过；普通 baseline 未漂移且无 strict review 可直接写，其他情况产生包括原 exec id/新 approval id/environment/process/input/launch cwd/tty/权限的 `ApprovalAction::WriteStdin`（`:240`）。
- `process_manager.rs:900` 一进程 interaction lock；排队后再读 strict_auto_review。`:959` 等待审批，`:978` 复核同一进程实例，批准后不能把复用 process id 的另一进程当原终端。`:983` plain-pipe session 除 Ctrl-C 不能继续 stdin；PTY 才写真实输入。

MiniCode `tools/monitor_tool.py:23` 声明 status read-only，write/cancel external。`execute:126` 与 `terminal/manager.py:865::write_stdin` 使用 context.command_scope_id 核对 owner，`:886` 锁 stdin；只检查 running/实际 stdin 通道，并写 UTF-8 bytes。进程状态保存 `BackgroundCommand.sandbox_policy`（`manager.py:84`），但 write_stdin 函数没有当前 policy/context 参数，也未比较旧 sandbox/network authority。

普通 checker 可能为 external monitor 要求审批，bypass 可跳普通审批（`permissions/checker.py:1372`），但两者都不实现 Source 的“当前不需要额外权限可直接写；旧进程扩大权限/policy 漂移需要审；无法执行当前 denied-read 时拒绝旧进程”。这是 producer → owner process authority → approval → locked actual write 的缺口，不能只在前端加一张卡。

后续可修范围：让进程 owner 保存明确 launch authority 与策略快照，monitor 的实际 write action 在 owner 锁内生成当前 policy 比较与冻结审批请求，批准后同实例校验再写；poll/cancel 分开。复用 MiniCode 现有 owner/digest/waiter/取消管线。Source 是否限制普通 pipe 的 stdin，须随 tty 传输一起处理，避免把已有能力悄悄删掉；原命令完成 hook 配对需同一次独立改造。

## 3. 最终请求与审批：已有链，以及不能冒充的部分

MiniCode 已有以下明确调用链：

1. `agent/tool_execution.py:1354::_apply_pre_tool_hook_owned` 运行真实 PreToolUse，`:1380` 接 updated_input；extension before_tool_call 同处于最终授权之前。
2. `tool_batch_execution.py:768` 调 `_authorize_final_tool_request`，`tool_execution.py:195` 复核 schema/required args/toolset/subagent scope；`:274` 冻结 canonical request，复制参数给 permission evaluator。`final_tool_request.py:87` 递归冻结映射/数组并计算 policy identity + args 的 SHA-256（不是 provider id）。`tool_batch_execution.py:803` 绑定脱离调用者对象的最终参数。
3. `tool_batch_execution.py:1535` PermissionRequest hook 可以改参，但 `:1595` 到 `:1640` 会对新请求完整重新授权并更新 digest、permission、diff，不沿用旧批准。
4. `:1810` 发 approval request 的参数/diff/digest/policy_identity；`ws/approval_runtime.py:259` 与 `:320` 核对 pending request digest 和实际 owner。`:1833` 等审批返回，`:1839` 重新算当前 args digest，期间改参则恢复原 proposal 并拒绝。普通 approval response 的 updated_input 不被视为旧批准（`:1929`），partial 文件批准中任何拒绝会原子拒绝整次工具调用（`:1976`）。
5. 仅完全批准的最终 request 写 `_approved_request_digests`（`:2109`），catastrophic shell 执行检查精确 digest（`command_tool.py:362`）。`tool_execution.py:1148` 给 adapter 复制执行参数，内部 hash/cursor 不污染 approved/journal args；registry execute 核对 expected_policy_identity（`:1168`）。

因此本次没有把已经修复的“hook 修改参数仍用旧批准”重复登记为 bug，也不需要再加额外 digest/兜底系统。静态阅读只能确认存在完整检查位置，不能替代实际 hook/rejection/WS 运行验证。

Source 与 MiniCode 的 approval policy 仍不同：Source `ToolOrchestrator` 持不可变的 typed req，同一 req 建 approval action 并执行；结合 exec-policy prefix rule、按环境的 permission profile、Guardian/strict auto review 与 managed network approval。MiniCode 的 exact request、tool allow/session cache、content rules/command prompt rules并非这些 Source 权限类型的全替代。后续要在原 policy consumer 中补完整语义，禁止仅把 enum 名称改成 Codex 来宣称对齐。

## 4. RequestPermissions 的缺口必须带 feature/scope/子集

Source `shell_spec.rs:161` 真实 model contract 为 `{ reason?, environment_id?, permissions: { network?, file_system? } }`，read/write paths 相对被选环境 cwd 解析。`handlers/request_permissions.rs:70` 先选择环境，`:79` 解析路径，`:88` 规范化权限，`:97` 交 session。

`session/mod.rs:2900::request_permissions_for_environment` 中 Never/granular禁止提问时返回空权限而非虚构 granted（`:2925`）；有权限批准时产生 immutable `ApprovalAction::RequestPermissions`（`:2951`），取消不执行未批准 grant。协议 `protocol/src/request_permissions.rs:12` scope 为 Turn/Session，`:65` 返回实际权限子集 + scope + strict_auto_review。

回复消费者 `session/mod.rs:3155` 找 pending 原环境与 turn；`:3193::normalize_request_permissions_response` 取 request∩response，不能由客户端扩大请求；strict_auto_review 的 session grant拒绝；`:3222` 将批准权限按 environment_id 分入 TurnContext 或 SessionServices。exec 的 `handlers/mod.rs:291::apply_granted_turn_permissions` 与 patch 的 `apply_patch.rs:496` 消费实际 grant，不等于批准所有后续 command。scope=session 存活到实际 session owner；scope=turn 到该 turn，不写全局配置。

MiniCode `sandbox/policy.py:430::with_additional_permissions` 已有内部网络 grant-only、filesystem entries 合并，Git/worktree 使用真实 runtime 内部 grant。未找到注册的 request_permissions model tool、相应控制请求/permission subset response、按 turn/session/environment 的 grant state 或 command/patch consumer。`PermissionContext.session_overrides` 是工具级 allow；approval cache 是同 args 操作批准；二者都不提供 Source 的指定路径 grant。该缺口可后续整链实现，但目前 Source feature 默认 false，不能在这轮默认开启或把 partial diff review 当 permission subset。

## 5. 文件读取、patch 与客户端 fs 的真实边界

此 Source revision 的核心 model handlers 没有 typed `read_file/list_dir`。文本读取/搜索/列表通常是 exec 的实际 shell，由 `registry.rs:670` 和 shell-command parse 为 Read/Search/ListFiles 的活动。`view_image` 则调用 selected environment 的 ExecutorFileSystem 并传 sandbox（`handlers/view_image.rs:175`）。MCP 的 filesystem/read_file 是外部 MCP contract，不能当本地 core read_file 的标准 schema。

MiniCode `tools/read_file.py:189` 提供 typed file/range 读取，`:201` 核路径和 declared read roots；`:251` 从同 snapshot 取内容/hash，`:304` 到 `:378` 保留真实行号、范围、hash、截断及可读 artifact。`agent/tool_execution.py:347` 为真实 write/edit 注入 runtime-owned hash。存在大量输出契约差异，但 typed API 能读同一文件不应仅因名字不同被判缺失。

Source patch 是 freeform 原 envelope：`handlers/apply_patch.rs:302` parse → selected environment → `verify_apply_patch_args`（`:341`，真实 filesystem + sandbox）→ `:477::execute_verified_patch`。写路径先排除已可写目标，再只给需要的父目录 derive additional permission（`:226`）；`:496` 用实际 turn grant，`:525` assess/prepare safety，`:546` typed request 冻结 patch/files/changes/permissions。`runtimes/apply_patch.rs:75` 为同 req 构建 approval action，`:179` 在实际 sandbox/fs 再 apply，`:202` 无论成功/部分失败都保留真实 committed delta。

MiniCode `tools/apply_patch.py:115` 也提供 raw envelope/freeform schema，`:186` plan实际修改；`:253` 对 source/destination 排序加锁，`:289` 无论成功/部分失败都记录已提交文件的真实 diff，`:302` 返回 committed_diff。这些已有实现不能反复改成相同功能的新包装。

真实差异是 MiniCode `apply_patch.py:210` 强制已有 source/destination 必须先出现在 read_file hash；`:227` 注释明确是 CC read-before-edit contract。Source verify 自己读取当前文件和匹配 patch，不存在“必须先调用 typed read_file”要求。MiniCode `:471` 即使 bypass 也拒绝 protected repository/agent config，而 Source 是 permission profile/denied reads/protected metadata 的 sandbox 与批准执行规则；两者不能称相同。MiniCode 用 `_resolve_path` 实际 workspace containment（`:346`），没有 Source 用户批准的特定外部路径 grant consumer。

可修范围：先把 Source patch verify/safety/profile/committed delta 与当前 MiniCode plan/锁/diff 整链对照，再调整强制 read contract和授权路径。需要保留用户外部改动/未保存内容；不能只删 hash 检查或对报错装作成功，也不应给模型注入一个本不存在的 Source read_file instruction。

客户端 `app-server/src/request_processors/fs_processor.rs:64` 的 `fs/readFile` 返回完整 bytes base64；`:79` 的 writeFile 解码真实 bytes，再调用本地 ExecutorFileSystem，sandbox=None。createDirectory/metadata/readDirectory/remove/copy/watch 同属客户端操作；protocol routes在 `app-server-protocol/src/protocol/common.rs:983`。这些不是 model tool，也不经过 model 的 exec approval。MiniCode 的 `workspace/api.py:260` read、`:287` write、`:298` compare-and-write 是客户端 editor 链；应以用户主动文件操作/草稿/保存/冲突行为独立验，不能用 agent read_file 的限制解释或复制客户端 fs 的权限。

## 验证边界与交接

- 本报告是精确静态源码/调用链审计，未新增 runtime pass 数。
- 之前 command/output/WS/hook/hash 回归的存在不证明这次新增的 Source PTY、retained stdin policy 或 request_permissions 已通过；不得合并成“后端完全一致”。
- shell/login/tty 将按已授权范围独立补齐并统一必要验证；request_permissions、stdin launch-policy审批、hook 配对、patch 策略和 remote executor保持明确待办。
- 未运行公网模型/外部 MCP/OAuth；未请求或操作用户桌面窗口；未清理任何用户 history、draft、attachment 或已有 dirty changes。

## 本批 shell/login/tty 实现与真实构建

实现只补现有 `run_command` 的参数与传输，不改模型工具名字，也不另建审批通道：

- `command_tool.py` 的 model/host/execution schema 增加 shell/login/tty。`command_support.py::command_shell_argv` 按 Source shell_detect 先由模型路径选择类型，再发现实际可执行程序；PowerShell 的 login=false 为 -NoProfile，true允许 profile，bash/zsh/sh 为 -lc/-c，cmd 为 /c。未知/不可用 shell 使用 Source 明文的 ultimate cmd/sh 选择，不是猜测的兼容层。当前 config layer 与 managed allow_login_shell=false 对显式 true 的拒绝均进入真实执行前边界。
- MiniCode 原 UTF-8/native-exit PowerShell adapter 保留；这是一项已有输出/退出码处理，不能将它声称为 Source 完全逐字相同的原 cmd。源参数选型和 startup profile 行为已对照实际 argv。
- 实际 argv/tty 经 `BackgroundCommandManager` 保存并传入原 SandboxRunner。普通命令仍用 pipe；PTY 未绕过当前 SandboxPolicy，bwrap/seatbelt/container/native 路径继续由同一个 sandbox wrapper产生。container 保留 stdin，tty才申请实际终端；公网/容器/非Windows运行尚未实测。
- Windows full-access PTY由打包的 Source `codex-utils-pty::spawn_pty_process` + `forward_sandbox_session_stdio` 创建/转发，stdio、ConPTY 与 Job Object是实际 Source 实现。受限 Windows wrapper 的新 --tty 直接传 `WindowsSandboxSessionRequest.tty`，其内层 ConPTY在原账号/ACL/WFP/私有 desktop下执行，不是给外壳挂一个假的PTY。没有 pipes fallback。
- 此项目打包的固定 tag为 rust-v0.158.0-alpha.2.1，其 spawn_pty_process 最后参数是空 &[i32]；审计 checkout 新接口为 ChildFds。实际编译确认差异后使用打包 tag的原签名，未修改 `.tmp/codex-src`。
- root另在 `agent/command_actions.py` 让分类跟同一 command_shell_argv 的实际选型：PS走PS parser，bash/zsh/sh走POSIX，cmd保留unknown原命令。该文件及其验证由root负责，不由本agent重复修改。

修改文件：`backend/tools/{command_tool,command_support}.py`、`backend/terminal/{manager,pty_process}.py`、`backend/sandbox/{runner,windows_native}.py`；`desktop/native-windows-sandbox/{Cargo.toml,src/main.rs}`；`desktop/scripts/{patch-windows-sandbox-source.py,prepare-windows-sandbox-runtime.ps1}`；专属测试 `backend/tests/test_codex_shell_pty_chain.py`。真实取消验证另暴露公共Windows tree helper过早判断回收完成，已由root确认文件归属后修改 `backend/subprocesses.py`：在 taskkill之前捕获实际父子身份，taskkill exit0后等待已捕获的进程退出，未退出的沿用原精确PID回收；不能因wrapper已经退出丢掉其子进程。

构建与运行证据目录：`output/playwright/codex-control-audit-20261013/source-shell-pty/`。隔离验证阶段没有替换正常runtime；随后root确认正式目录无占用并执行生产prepare，见下文。没有强制重启用户窗口/进程，没有做账号provisioning。

| 批次 | 实际结果 | 事实与边界 |
| --- | --- | --- |
| baseline-run01 | 110通过、0失败、0跳过 | 普通command/env/escalation/output/yield/background/monitor/native policy/provisioning契约回归；不代表新的TTY能力已验证 |
| parameters-run01 | 10通过、4 deselected | 新schema类型、模型路径选shell类型、真实PS profile flags、managed拒绝、真实cmd exit7/原命令、required sandbox不逃逸和native argv/profile/tty生产者。deselected四项没计通过 |
| native准备首次 | exit1，未进入Rust编译 | 原prepare --offline缺少固定tungstenite Git rev 4fffad30… cache；原脚本默认offline不改，任务构建补取依赖 |
| native-build-run02 | exit1，E0433 | 实际编译确认打包tag没有ChildFds；完整日志保留 |
| native-build-run03 | exit0 | 统一刷新生成文件后按实际tag签名离线构建成功，产物只写隔离目录 |
| chain-run02 | 10通过、4失败 | 新fixture错误等待已被既有manager.detach在yield0后释放的stream_callback，plain pipe同样失败；没改生产detach，改为owned cursor等待真实输出 |
| chain-run03 | 2通过、2失败、10 deselected | pipe/PTY、中文stdin、真实isatty、owner拒绝、原命令/同PID、原字节输出持久化通过；timeout/cancel发现回收证明缺口 |
| chain-run04 | 2失败、12 deselected | 加精确身份后确认：timeout时pwsh.exe PID27624、cancel时PID30744仍被报告is_running，而wrapper已退出且cleanup_pending=false。不能跳过该真实child断言 |
| chain-run05 | 66通过、1失败、0跳过 | 修真实Windows回收链后的统一67项：全部14项shell/PTY、中文原字节、owner拒绝、timeout/cancel真实父子进程退出均通过。唯一失败为既有PendingRunner fixture未提供真实Runner的capability接口 |
| chain-run06 | 5通过、0失败、0跳过 | fixture调用真实SandboxRunner capability；定向修复该1项，加4个独立subprocess/communicate真实树回收与unproven cleanup契约用例。没有添加生产getattr/兼容兜底 |
| runtime-manifest-contract | 4通过、0失败、0跳过 | Node包装契约：源fingerprint、全部3个exe hashes、旧owner/runtime拒绝与替换文件拒绝；是打包契约测试，不是用户当前app重启激活 |

按baseline/parameters/chain-run02到run06的每个testcase最新结果去重，Python共 **145项通过、0项未解决失败、0跳过**，其中完整新增shell/PTY为 **14项通过**。不能将各批次数相加，也不与root其他批验重复相加。先前真实失败如上全部保留，不因最终通过删日志。命令和stdlib输出全部在临时fixture owner下运行，凭实际isatty和捕获子进程身份验证，而非只检查函数调用次数。

新增两个文件的ruff检查通过；整文件首轮ruff有41项诊断，其中新增两个文件的4项已修复并定向通过。其余既有修改文件未据此做不归本批的清理，不宣称全仓lint通过。定向diff --check通过。唯一修复的旧fixture文件为 `backend/tests/test_harness_command_cleanup_regressions.py`。

Windows full-access PTY和普通pipe已真实执行；Windows native受限路径验证了原producer/policy/argv/tty和固定Source的编译，未provision账号、未真实发起受限ConPTY/网络测试。Linux/macOS/container、Source stdin launch-policy审批/strict review、request_permissions grant和公网provider仍不因本批通过宣称等价。

## 原生运行时激活边界

新运行时隔离产物为 `output/playwright/codex-control-audit-20261013/source-shell-pty/native-runtime/`：codex.exe、codex-command-runner.exe、codex-windows-sandbox-setup.exe、原许可证和runtime.json。reported_version仍为 `minicode-windows-sandbox 0.158.0-alpha.2.1 owner-v3`，区分本次TTY能力必须检查源输入fingerprint与文件SHA，不靠版本字符串猜测。

- 新 input_sha256：`ab28aabb6b32388061494d34c3842a5f45bda01f2108106d2606e7c778cfadc4`。
- 新 codex.exe SHA-256：`3fd3a3053530879a715d2d4b6f09cabd1d78d716bd4e1105558e89ba6c4d273c`。
- 隔离验证阶段，旧 `desktop/windows-sandbox-runtime/runtime.json` input_sha256为 `724c56ce494f1a7efe8fa01f493fab7ad51aa52ed11f8acf696a0e8cb4b13c18`，codex.exe SHA为 `9bb6914646f93fbb00d1910d40c795e390f4fd7941fa3f1f82acfd3a025a7412`。这是更新前的历史证据；root随后已完成正式目录更新，不能继续将它写成当前状态。

`prepare-windows-sandbox-runtime.ps1` 新增 -OutputDirectory 供隔离构建；不传参数的生产准备路径仍为 `desktop/windows-sandbox-runtime`，同一输入fingerprint检查触发新源构建。QA进程明确设置 MINICODE_WINDOWS_SANDBOX_EXECUTABLE为隔离codex.exe；这本身不证明正式目录已经更新。Runtime失败不会静默降成pipes。

### 正式目录收尾验证

root通过CIM精确检查确认 `desktop/windows-sandbox-runtime` 无MiniCode进程占用，保留当前Codex自用的不同路径bin。随后不带 -OutputDirectory 执行默认生产prepare：**exit0、增量构建4.57秒**，正常生产目录的3个exe、原许可证和manifest均已更新。原日志：`source-shell-pty/native-production-prepare-run01.log`。

本agent只追加一次正式目录只读契约检查，未重编译、未重跑145项/TTY链、未操作用户窗口：

- 真实文件系统调用 `desktop/scripts/verify-windows-sandbox-runtime.js`，校验正式manifest身份、源input fingerprint、全部3个exe SHA与许可文件，**exit0**。原日志 `source-shell-pty/native-production-contract-run01.log` 中为 `PRODUCTION_RUNTIME_CONTRACT_OK`。
- 正式 `runtime.json` 实际input为新 `ab28aabb6b32388061494d34c3842a5f45bda01f2108106d2606e7c778cfadc4`；其codex.exe实际SHA为上方新 `3fd3a305…`，另外2个exe与新manifest匹配。
- 直接执行正式 `desktop/windows-sandbox-runtime/codex.exe --version` 为 `minicode-windows-sandbox 0.158.0-alpha.2.1 owner-v3`，exit0；不是仅检查QA环境变量指向的文件。

结论：**新的原生资产已经进入正式生产加载路径，runtime待准备不再是待办**。正常已加载旧Python/UI代码的app进程没有被强制restart/reload，不能由资产更新推导该旧进程已消费新backend/UI。该运行进程的reload状态由root单独确认，不把本次只读检查冒充用户当前窗口的TTY操作验证。
