# 原生命令投影与内部证据链：最终交付

2026-10-18 最新后续入口：[本机 Changes 与 harness 收尾交付](2026-10-17-local-changes-and-harness-delivery.md)。最新实现 `37f05e5f` 的 [CI 39047123759](https://github.com/khalil-wu/minicode/actions/runs/39047123759) 已 **8/8 成功**。已补五类真实 Git 比较、完整内容/预览/导入过滤、Source 导航和展开合同、原生 CUA owner/审批/关闭链；build 6 真实 Electron 与 r20 新安装分别留证。下方“Computer Use 未实现”、旧 wheel、构建与当时待办均为历史，不表示本批状态。当前范围按用户限定为本机与 codexsrc，外机/普通 WSL 不冒称通过。

本轮后续收尾入口：[Node、桌面投影与安装候选](2026-10-16-node-desktop-followup-delivery.md)。r13 新包独立 41/41、startup ERROR=[]，实际生产 Electron 的两轮 Node、标题、图片及完成折叠/草稿已验；0c CI 的多窗口测试提前退出另已修 fixture，完整新 CI 按独立运行核对。下方“r13 尚待”及 r12 记录保留为各阶段事实，不代表最新候选。

最终实现 `8dfff051` 的 [CI 38920283863](https://github.com/khalil-wu/minicode/actions/runs/38920283863)现已八项成功：backend 6946 passed / 108 skipped、frontend 4350 tests、desktop smoke 6/6。Ubuntu 实际清理标记和 `child.close {0,null}` 断言均通过，旧失败留证。r13 后端仍冻结于 0c；0c→8d 的五条 desktop-test/docs 改动与 1271 个冻结输入无交集，最终匹配凭据为 `output/source-executor-remote-final-wheel-r13-20261016/final-project-head-match.json`，未借新提交冒称重跑新包。后续仅文档收尾不改变这些实现与验收身份。

## 2026-10-16 当前后续批：Node、工作树、设置与 Linux 路由

最近完整检查点已更新到 `25e98f7a` 的 [CI 38910536542](https://github.com/khalil-wu/minicode/actions/runs/38910536542)：**8/8 jobs success**。backend 6935 passed / 108 skipped，root 1768 passed / 51 skipped，Windows 362 passed / 15 skipped，frontend 363 files / 4350 tests，desktop 161 passed / 9 skipped + 6 smoke，Electron 1 passed，两项 native build/验证成功。原 logs/XML 和计数见 `output/ci-25e98f7a-20261016/CI-REPORT.md`。它不是下述安装问题消失的证据：r12 的 40 功能断言通过，但实际日志有三次 Node 前置启动错误，已标 stale/hold。后续 `e7a9fc86` 修复原 host 捕获、SDK fork 和动态配置/catalog 准入；内核另已移除 Worker-local Inspector、修复直接 CPU dispatcher 死锁和异常退出结算。最终本机消费者 43 passed / 1 native skip，新八项在 Node 22/Electron 各通过，三运行时各 100 双 reset + 20 direct CPU 通过；详见[分批 Node 报告](2026-10-16-node-companion.md)。新源码仍需自己的完整 CI 与新候选安装冷验。

本节优先于下文旧构建、旧 wheel 和已失效的待办。`ee80a807` 的[完整 CI 38905092822](https://github.com/khalil-wu/minicode/actions/runs/38905092822)为 **6 jobs success / 2 failure**：backend 6917 passed / 106 skipped / 12 failed；Windows 81 passed / 1 failed 后停止。root 1768 passed / 51 skipped、frontend 4335 tests、desktop 161 passed / 9 skipped 及 6 项 smoke、Electron 1 项、两项 native build 均通过。后续修复不能借这轮结果记成八项全绿。

- **Linux OS managed 路由已接通**：pinned Source helper 进入正式 Linux native binary，包含 `--verify-fd-mount` 前置参数分派；原 namespace、SCM_RIGHTS、attribution、proxy、seccomp 与生命周期均沿 Source。该次 CI 的路由 16/16 通过，含七项真实 namespace/proxy/cancel。`output/linux-managed-routing-20261016/` 与该 CI 的原日志保留原始证据。
- **Windows 工作树消费链已修复**：分开实际 cwd 与原策略 cwd，Git metadata 和 worktree pool 使用动作实际所需范围；模型 snapshot/list/restore 与 GUI 共用原执行器、Git refs 和事务，不再写控制端 TEMP。集中 76 passed / 1 symlink 权限 skip，最终三项受影响复验通过。原 Unicode 归因已排除；私有 OWNER RIGHTS Temp ACL 与 unelevated deny-read 保留为原生边界。见[后续报告](2026-10-16-windows-worktree-followup.md)。
- **真实 Node MCP companion 已实现**：跨回合内核、正常 npm module realm、图片与 Guardian、captured 环境/权限/网络、同名用户 MCP 覆盖、原 borrower 退役和真实进程关闭均已接通。公开 Source 没有 Node 执行器实现，不宣称未公开内部等价；Computer Use 仍未实现。该 CI 另外暴露 Node 22 活跃重置原生 abort，正在修复，见[Node 报告](2026-10-16-node-companion.md)。
- **设置与面板修复已推送**：删除额外页面副标题、呈现无 history 的真实有效 provider、统一设备/诊断/Problems 的选择菜单；预览改用 `--token=<value>`，解决随机令牌以 `-` 开头时实际启动失败。必要前端最新唯一 170 passed、TSC/build/两项预算通过；真实 Electron 核对手机尺寸、字体及重载草稿保留。该 CI 的固定 dash-token 用例通过。见[设置与预览报告](2026-10-16-settings-preview-followup.md)。
- **拒绝启动的关闭终态已本地修复**：`83cf0d43` 只有在原 job/runner/network 清理全部 ACK、没有错误及 watcher 时发布 `closed`；保留真实启动拒绝、`accepted=false` 和未知 exit code。聚焦 46 passed / 1 native skip，未配置账号或绕过沙箱。该提交尚待后续完整 CI。

`83cf0d43` 的 r11 Windows wheel 冷验实际 **37/37**，包括原 28 项和九项 Electron Node 检查，wheel SHA256 `b9140e18223957c3a9c73670c0e18c4a5a43e23d0e101fcd7bf33d168cabd8dc`。它只证明该冻结候选；后续 Node/SDK 修复会使其过期，不能作为新源码安装验收。具体候选身份和边界见 `output/source-executor-remote-final-wheel-r11-20261016/REPORT.md`。

后续修复现已提交：`c0f3f684` 修 Node 22 中断时的实际 ALS 后端并对齐 JS 过程标题，`f5d1ed65` 补三色编辑，`418c130d` 修 SDK 长会话 owner、公开审批回调和 MCP 启动取消。Node 本机 22/24/Electron 24 的 reset/timeout/cancel/晚输出已通过；SDK/MCP/notes/observer 89 个不同 case 全通过，保留原四项精确 owner 集合，真实 SDK 同 PID 两 prompt 经两次审批后执行。原中间失败和准确范围见[Node 报告](2026-10-16-node-companion.md)。

三色编辑最新唯一 **195/195**，JS 标题及相关四文件 **80/80**，两范围分别记录而不累计为全产品数。最终 TSC/build/两预算通过，入口 `assets/index-CCh13oC-.js`，index SHA256 `17dff27b79573909e5cedaf6d1a27dff9e5465c65fed84c31f23dbb6a69ba129`；主 CSS 与此前一致。Root 使用该实际 dist 的独立生产 Electron 完成十项观测、六张截图，核对 picker 键盘/模式/重载/草稿/原字体，pageerrors 为空；组件 QA 另核原 Monaco dirty buffer。见[外观配色报告](2026-10-16-appearance-colors.md)及 `output/playwright/latest-ui-followup-20261016/validation.json`。

上面三项修复的完整八项 CI 已在本节顶部单列；后续安装审查发现的问题也独立保留。r12 不能交付，新 host/内核修复完成后将重新冻结候选并验收，不能借旧包断言。最新剩余范围统一见[当前差距](2026-10-16-current-desktop-gaps.md)，不按测试数量推算产品完成率。

## 2026-10-16 最新补齐：桌面过程、MCP App 与 executor 配套

本次后续批新增完整答案后的主/子过程折叠修正、交互 MCP App 的原会话寿命与常驻展示、目标 LSP/环境安装/Git delivery、工作树实际新会话切换及 Windows PTY resize。逐项证据、原失败与 Source 边界见 [桌面 harness 集成](2026-10-16-desktop-harness-integration.md) 和 [当前差距](2026-10-16-current-desktop-gaps.md)。下文旧 entry、r5 wheel 及“尚无 LSP/resize”等描述继续作为其当时构建的历史事实，不代表最新源码。

当前集中结果为前端全量 **4333 passed**、目标环境/LSP/文件/生命周期 **112 passed**、MCP/remote Skill/PTY/host **209 passed + 1 skipped**、桌面单元 **170 passed**。MCP 的最新 7 项窄验删除无 Source 依据的 App 上下文上限，替换原相同节点而不重复相加。工作树另批明确保留 **10 passed / 2 failed / 1 skipped**；受限 Windows 的 Git metadata 和 split writable root 操作仍未闭合。r9 独立候选 wheel 冷验 **28/28**，但因此仍标候选，不称为全产品完成。测试范围各自记录，不与本文件历史数累计。

## 2026-10-16：executor、远程桌面与网络链后续批

本节是后续实现的交付范围；下文 2026-10-15 的前端、wheel 和冻结清单均保留为历史身份，不能代表本节源码。此前 GitHub 检查点为 `ae278f910148f9c731a0dfaa51453431d66179c7`；本节代码推送后由独立的八项 CI 检查，不能借旧 CI 记通过。[本分支 CI 运行页](https://github.com/khalil-wu/minicode/actions/workflows/ci.yml?query=branch%3Acodex%2Frelease-readiness-20260927)保留每次真实提交及完整结果。

此前列为待做的本地 executor 注册与连接、thread owner、环境选择、目标文件/命令/权限、MCP/Skill 和 SSH/WSL desktop host 现已实现。各入口复用实际 receiving-thread owner 与捕获的原环境 lease；不能把传输重连、注册表同 ID 的替换或控制端目录当作原执行环境。命令、文件、checkpoint、compaction、编辑器请求和 Guardian 均沿同一份归属传递。

| 已接通的范围 | 可核对的报告 |
| --- | --- |
| 目标命令准入、冻结 argv、权限与 Guardian | [命令链](2026-10-16-executor-command-guardian-admission.md) |
| 目标文件、搜索、Git、checkpoint 和恢复 | [文件链](2026-10-16-executor-file-checkpoint-consumers.md) |
| 编辑器/API、附件、预览、前端 workspace owner | [编辑器与 API](2026-10-16-executor-editor-api-consumers.md) |
| MCP 独立 authority、Skill gate、原生工具输出和退休寿命 | [MCP 与 Skill](2026-10-16-remote-mcp-skill-consumption.md) |
| Source 原生网络策略、审批 consumer | [网络审批](2026-10-16-network-approval-consumer.md) |
| composer 替换、原 prompt 决策与实际 WS 回传 | [网络审批投影](2026-10-16-network-approval-projection.md) |
| 原请求 RPC 错误、原 handle 清理与 audit | [独立网络审阅](2026-10-16-network-runner-independent-review.md) |
| Windows 明确初始化入口与远端窗口的归属 | [显式初始化](2026-10-16-windows-explicit-network-setup-consumer.md) |
| 持久 SSH/WSL host、流量背压、重连和 window secret | [远程桌面后端](2026-10-16-desktop-remote-host-backend.md) |
| 最终前端、真实 Electron、内部 Git 元数据与构建身份 | [前端与桌面统一验收](2026-10-16-frontend-desktop-final.md) |
| SDK 超时、真实 child 晚交接、原租约/能力与会话占用 | [SDK 清理持有链](2026-10-16-sdk-mcp-cleanup-ownership.md) |
| 原生 wheel 平台、正式包身份与独立安装冷验 | [安装包验证](2026-10-16-native-wheel-packaging.md) |
| 全部后端节点、分段执行/修复记录及真实跳过 | [后端最终合并](2026-10-16-backend-final-validation.md) |
| 首次 Linux CI 的入库字节及平台测试入口修复 | [CI 修复证据](2026-10-16-ci-native-bytes-and-platform-probes.md) |

网络域名/Unix socket 匹配、managed ceiling、permission-profile URI 与规则写入使用该 Source pin 的原生库。后台命令继续由原 BackgroundCommandManager 管理；未确认清理保留原 process/session ID 和网络决策，冷恢复不会用本机 PID 冒充远端终止。Windows 网络初始化只有明确的设置按钮会启动系统配置；status 和任务执行路径不做隐式提权、账号或防火墙初始化。

真实边界仍保留：Windows WFP 流量矩阵需要显式 provision，本轮未执行该系统操作，相关用例是跳过；没有实际外机 SSH 或 WSL 发行版验收。独立 Source executor 没有 LSP server/transport 时返回 503；Windows 原生 PTY 尚无 resize channel，返回 409。它们不能计作功能通过，也不因官方账号排除而被隐藏。

统一前端最新去重 4297/4297、桌面单元 170/170、桌面 Electron 6/6、前端 Electron 默认入口 1/1，0 个最终失败/跳过；原完整运行与受影响修复复验分别保留，不能把它们说成在最终源码重跑过所有未受影响文件。最终前端 entry 为 `index-DdoZ091a.js`，2026-10-16 14:53:16 构建，详细身份见前端报告。最新桌面截图来自该批重新构建的真实 Electron；连接行三列布局、单列 SSH 表单和自定义身份验证菜单已逐张核对，保留 MiniCode 字体设置。

根目录 Python 测试初始 1807 项，1737 passed / 62 skipped / 8 failed；初败已按具体契约修复后补验，并新增八个实际平台打包合同，得到唯一 1815 个节点、1753 passed / 62 skipped。随后加入四个 Source 缓存初始化回归；实际重新收集 1819 个唯一节点，与旧结果加新四项的集合精确一致，最新合并为 1757 passed / 62 skipped、0 missing/failed/error，不称为 1819 项全套重跑。跳过继续保留其平台/条件原因。backend 首批在 MCP 测试替身误启动裸 Python、连续等待 initialize 时停止，3470 个实际完成节点为 3383 passed / 74 failed / 13 skipped，3481 项未完成；未执行不能记跳过。MCP 替身已改为真实 owned JSON-RPC peer，7/7；后端余段带原阈值和逐项 failure report 统一完成，3535 项为 3500 passed / 9 failed / 26 skipped，九项均另行修复/补验，初败没有删除。最终 current collection 为 6962 个节点，按其加载代码快照的开始时间取最新，6923 passed / 39 skipped、0 missing/failed/error；全节点合并报告单独记录每个节点的证据与真实跳过原因，不把重叠批次相加，也不称为单次未经中断的全绿运行。

最后的独立 SDK 审查另外证实 registry drain 之后的晚交接缺口：外层 wrapper 已结束，而实际 invoke child 仍在运行时，原环境曾提前关闭。bindings 现在只沿本次原 call ID 的原 `cleanup_tasks_by_call` 字典追踪 child；Prepared 退役捕获同一原字典，不能扫描包含自己的共享 lifecycle 集合，也不能转去下一回合的新字典。真实 SDK 的 bound/unbound × early/late 四场景及两阶段能力关闭/占用验证集中 16/16；对应 R3 最新 36 passed / 1 个安装版 Source stub skipped。这里保留原失败和修复证明，不将此前十三项、未越过该交接的验证泛化为完整生命周期已通过。

正式 r5 Windows wheel 为 `output/source-executor-remote-final-wheel-r5-20261016/minicode-0.2.0-py3-none-win_amd64.whl`，15,994,583 bytes，SHA256 `2843f8df9dc1699bb67dafa3e5eeb3166dfa697c8fcdf08797de0a308fea722c`。1247 个冻结输入、1228 个 backend 包成员在当前工作区/冻结树/wheel/全新安装目录逐字节核对，`changed_inputs=[]`、`candidate_only=false`。bundled Python 3.11.9、`-I`、全新独立 Temp cwd/state 与限定 PATH 的完整冷验 26/26，0 failed/skipped；包含实际原生网络策略/规则 writer 与 minicode-host 入口。r4 相对 r3 更新两处 CI 测试夹具，r5 相对 r4 仅更新 Source 缓存初始化 builder，不复用旧包二十六项结果；Windows 二进制继续保留实际旧 producer 来源，未虚改 manifest 或重编原生资源。r2 保留为停止交付的历史候选。Windows WFP 和真实外机边界仍按上文保留。

此前 CI 检查点（2026-10-16）：新会话与侧栏按钮修复后的 `ae278f910148f9c731a0dfaa51453431d66179c7`，GitHub run `38775603301` 七项全部成功；此前两次异步测试失败和修复证据见 `2026-10-16-ci-async-ownership-boundaries.md`。该次运行不包括本节后续 executor 与 remote 实现；安装版 executor 不支持的 API 也不计入这次 CI 通过。

后续状态（2026-10-15）：用户随后报告的 CI、新会话和项目动作问题已收尾。冻结基线 `18e8bd8d` 的 GitHub run `38749202449` 七项全部通过，见 `2026-10-15-ci-lifecycle-and-linux-mount-repairs.md`。现在继续下列已确认的 heartbeat、Memory Phase 1 WS 与 remote 剩余范围，尚不把只读审计或施工中的代码计为完成。本文件下方冻结输入、前端和 wheel 身份是此前交付批的历史记录，不代表后续 CI 修复及新功能后的当前源码或安装包。

后续实现检查点：heartbeat H1–H3（重复版本、按接受顺序恢复、完整 transcript 引用及运行时 Skill 证据）相关节点最新去重 391/391；H4（真实 busy gate、start-or-steer、durable run 与等待寿命）最新去重 130/130；Memory Phase 1 独立 WS 及原运输回归最终一次五文件 83/83；前端 renderer observer 123/123，均无跳过。范围相互有重叠，不能相加；初败及修复后窄复验均保留。详见 `2026-10-15-source-heartbeat-retained-context.md`、`2026-10-15-source-heartbeat-host-admission.md` 和 `2026-10-15-memory-phase1-websocket-transport.md`。完整修正提交 **2c3cae04** 的 GitHub run **38764385600 七项全部成功**，Backend **6579 passed、27 skipped、0 failed/error**，详见 `2026-10-15-heartbeat-memory-ci-checkpoint.md`；remote 仍另行实施。

本批生产修改、必要验证、前端构建与独立安装冷验已完成。它闭合下列已定位的问题，不代表整款 MiniCode 已与 Codex 完全等价。官方账号登录、授权及依赖该账号的 hosted 服务按用户要求排除；排除项不记通过。

行为依据为 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。视觉和活动投影另核实际安装版 desktop ASAR 与隔离 app-server 消息，不能把 Rust 源码版本当作安装版版本。

机器可核对的最终索引：`output/source-internal-evidence-delivery-final-20261015.json`。最后一次核对的 1144 个冻结输入没有变化，前端身份和安装包身份与验收记录相同。本轮收尾没有重跑已闭合的测试或修改生产代码。

## 实际修复

- 原生命令区分 provider 空参数 announcement、真实冻结 planned item、底部审批 request、实际开始和终态。Source 在审批前确实会发布 planned item；MiniCode 保留这份真实记录，不把等待审批当作进程已经执行，也不添加准备命令、等待批准或 shield 等自创状态。
- 命令真实开始时间由原执行入口产生，经 canonical、原 journal、持久恢复和前端消费。实际 direct callback 曾绕过 QueryEngine 的 stream journal；现在先等待同一个 journal owner 写入再调用宿主。SDK 无宿主也使用同一入口，nested/CodeMode 保留原路径；旧 turn/iteration 回调不能覆盖新回合复用 call ID 的时钟。
- 只有真实 native `abort` 表示用户取消。Guardian denied/timed_out、宿主超时或 digest 拒绝不会被错误升级成用户中止。未执行的取消保留 command、原始原因和终态，继续没有 execution start、exit 或 duration。
- 按实际 Source 输出组件修正详情：真实 exit 0 显示成功；没有 exit 且非真实 interrupted 时显示“退出码 未知”。未执行的模型取消回执留在原始记录，不再冒充 Shell stdout；真实 stdout/stderr 与实际执行异常继续保留，不新增“无输出”。
- 成功且实际 accepted 的 raw `node_repl`/`cua_repl` MCP CodeMode 结果，进入原 conversation 的 Guardian 证据存储。冻结私有执行身份、实际历史准入游标、顺序、预算和异步图片选择均沿 Source；普通 MCP、名称相似工具、wire metadata 不能伪造该身份。本批没有创建新的 Node/CUA 执行器。
- 修复 Responses 图片编码把显式 `original` 覆盖成 `auto` 的真实 wire 丢失；Responses、Chat、Anthropic 消费同一份图片，不重复包装或计数。正式声明并安装 Pillow native decoder。
- heartbeat 输入来源经真实调度 claim、durable run、逐条输入 origin、QueryEngine/Guardian 和冷 snapshot 保留；同回合真人 steer 仍来自 User。Memory Phase 1 请求归属真实启动会话 A，不借用被分析的 B/C 身份。Guardian reviewer 路由按 Source 实际 caller gate 修正，当前 custom/API-key/override 不发送不适用的 reviewer/parent response 投影，不新增官方认证子系统。

## 验证范围

各行是各自范围的最新结果，存在重叠，不能相加作全产品通过数。初始失败、错误期望与必要窄复验均保留。

| 范围 | 最新结果 | 证据与边界 |
| --- | --- | --- |
| 原生命令前端与 ExecCell | 21/21 | `native-command-activity-frontend-batch04.log`；最终输出投影字节 |
| canonical/journal consumer 与 producer | 71/71 | `native-command-activity-persistence-20261015-latest-results.json`；该批本身未证明 direct callback 入口，后续补下一行 |
| 实际 QueryEngine 启动准入及必要回归 | 67/67，新增 7 项 | `native-query-start-admission-delivery-20261015.json`；真实 PowerShell 阻塞期间从新 journal 读取，direct/nested/CodeMode × host/无 host，以及两 turn 旧闭包回放 |
| Node/CUA accepted evidence 与原 Guardian 消费者 | 349/349，新增 33 项 | `source-node-repl-evidence-delivery-20261015.json`；真实 stdio MCP、CodeMode、本地 HTTP 与三 API 图片请求；实际 stdio/HTTP 另在最终 QueryEngine 字节下复验 |
| heartbeat 输入来源 | 176/176 | `source-heartbeat-input-origin-20261015-latest-results.json`；不等于完整 heartbeat 合并/注入已实现 |
| Guardian 路由及不可伪造来源 | 23/23 | `source-guardian-response-route-host-boundary-final-20261015.xml`；早期 47 项中两个错误 Source 肯定断言已撤回，不与本行相加 |
| Memory Phase 1 启动归因 | 42/42 | 两份 `source-memory-phase1-provenance-*-20261015.xml`；实际独立 Responses HTTP，不声称 Phase 1 WS 验收 |

上述必要范围均无跳过。开发测试使用 Python 3.12；安装冷验与 decoder smoke 使用项目 bundled Python 3.11.9，不把两种运行环境混算。

详细调用链分别记录在 `2026-10-15-native-command-activity-persistence.md`、`2026-10-15-native-query-start-admission.md`、`2026-10-15-source-node-cua-evidence.md`、`2026-10-15-source-heartbeat-input-origin.md`、`2026-10-15-guardian-response-routing-correction.md` 和 `2026-10-15-memory-phase1-request-provenance.md`。

## 最终前端与真实桌面

最终 entry 为 `assets/index-2l4V0mq0.js`，index SHA256 为 `b8195e87598a308abe3073b63ac9ac948df585f06ed6b00fb81f688454fad142`，entry SHA256 为 `1f11fd9cc96c514cbef80d95d609d1bdd447cf9a634bfc1d73b96c3a4b4decf3`。TSC、Vite、UI debt、bundle budget 与 protocol sync 已通过，记录在最终索引。

CSS 为 `assets/index-CpG3V_Lq.css`，SHA256 `1c87eb577857c9aa7a7bbd66a71660172858449eaa7b12a6714529ec7011d621`，与此前 BT3 逐字节一致，保持既有字体、字号和字重。

真实 Electron/WS/native policy 验收按构建分开保留，目录均在 `output/playwright/codex-control-audit-20261013/`：

| 实机阶段 | 构建 | 可计入的结果 |
| --- | --- | --- |
| `production-native-command-activity-companion01` | Cz0Kkr_F | allow 完整链通过；整个 run 因 QA 把实际 cancelled 误预期为 interrupted 而为 false，不宣称整场通过 |
| `companion02` | BqCYZOvL | 0 个功能场景；QA 使用本地 store switch 而非实际 WS 请求，失败原证据保留 |
| `companion03` | BqCYZOvL | 剩余实际 abort、真正 cold restart 两项通过，errors=[]；其中截图暴露取消回执冒充 Shell，随后独立修复，不把该输出显示计为通过 |
| `companion04` | 2l4V0mq0 | 最终受影响显示只读验收 1/1，errors=[]；0 次新 provider 请求、0 条新命令，不重复旧功能场景 |

合计仅为 **3 个唯一真实功能范围**，加 **1 个最终只读显示范围**，不称为在最终前端重跑了前三项。最终 backend 在 companion03/04 前后 16 个相关生产文件哈希相同。

Root 独立逐张查看 companion01 的 01/02/04/05、companion03 的最终五图及 companion04 的 09 图；中间展开动画帧不作为 settled 验收。最终图：

`output/playwright/codex-control-audit-20261013/production-native-command-activity-companion04/09-current-build-native-preexecution-raw-receipt-and-empty-shell.png`

真实批准命令在冷启动后保留原 start `1792053612540`、duration `3253ms`、exit `0` 和实际输出；原 abort 没有伪造执行时间/exit/duration。原隔离 profile 的 8 条 transcript、`NATIVE_POLICY_VISUAL_DRAFT_KEEP` 与原规则保留。没有操作用户普通窗口，所有自有 QA app/provider 已退出。

## 最终 wheel 与独立安装冷验

交付 wheel：`output/source-internal-evidence-final-wheel-delivery-20261015/minicode-0.2.0-py3-none-win_amd64.whl`，大小 **7,543,989 bytes**，SHA256 **`5fa1d189f3ddaa48d503f41f97854c6a972f12806bc0bc3b7c5204aa17b30ace`**。这是 Windows x64 backend wheel；不据此宣称重新构建了完整桌面安装器。

冻结清单 `source-internal-evidence-delivery-final-inputs-20261015.json` 含 1144 个输入，SHA256 `36739741def78226e04f37861ae1b1b5a6c8fb6fa6effc756d94aae9b1db32ab`。最终核对未变；wheel 的 1129 个 backend 文件与冻结源码、全新安装目录逐字节一致。三个新增模块 `guardian/node_repl.py`、`guardian/images.py`、`backend/ids.py` 均实际打包。

全新安装目录为 `C:/Users/ago/AppData/Local/Temp/minicode-source-evidence-final-a6e94a11d8634318863a455ab08fb92a`。使用 bundled Python `-I`、Temp cwd、仅 System32 PATH，实际安装包 native consumer 冷验 **14/14**，覆盖 native identity、资源、冻结 argv/审批、实际 PowerShell、prefix 落盘及冷 owner 复用；详见 `source-internal-evidence-final-installed-cold-20261015.json`。

`pyproject.toml` 正式声明 `Pillow>=11,<13`，sidecar lock 固定 `Pillow==12.2.0`，wheel metadata 为 `Pillow<13,>=11`。同一全新安装目录的 backend 经 bundled `_imaging.cp311-win_amd64.pyd` 实际解码 PNG、capture/store/render，并保留 `original`，见 `source-internal-evidence-final-installed-node-decoder-20261015.json`。这只是安装后的 native decoder 边界，不另算一个 MCP/server 场景。没有重建/清空整个 runtime，也没有伪造 runtime build stamp。

早期候选 wheel `94e8d3d1…` 在独立审阅发现 direct callback 漏写后停止交付，仅保留原 build/install 历史；未给它记冷验通过。当前 `5fa1d189…` 是修复后的最终包。

## 仍需后续对齐的范围

此前交付批没有未通过的必要项，但不代表全产品等价。heartbeat 重复触发合并、完整 transcript 引用、busy 自动注入及相关 gate 已在上方后续检查点接通，分别保留其新验证；不追写成此前批次已实现。此前 remote executor 的 registry/connection、thread selection、远程 filesystem/tools 与生命周期待办，已由文件顶部的 2026-10-16 后续批接通；其实际外机、LSP、PTY 与 Windows WFP 边界以上述报告为准。远程能力仍以实际桌面入口为依据，不能因 Source 有 API 就自创按钮。

Memory Phase 1 的归因及 HTTP 链由此前批完成。随后确认的可选 WS 差异现已另批修复：Source `memories/write/src/runtime.rs::stream_stage_one_prompt` 调用 `ModelClientSession::stream`，后者在 `core/src/client.rs:1030、2237` 按 provider WebSocket 能力及 session fallback 选择运输，没有官方账号门槛。MiniCode 现为实际 side context 建立独立 pool/request，保留主回合连接和 fallback，具体真实 HTTP/WS 与取消、partial usage、计费隔离证据见上方新报告，不借主回合或 Phase 2 测试替代。

Node/CUA 本批是已注册工具的证据消费链，不包含新执行器。官方账号相关 hosted connectors 不做。旧“高级 review”笼统待办不能直接沿用：实际桌面 Markdown/code-comments 工作流与 Rust 可选 ReviewTask 应分别核对。更多桌面区域也不能借本批测试数被宣布完全一致；已证实范围与后续只读范围分别保留在 active-work 和 `2026-10-15-local-remote-environment-scope-audit.md`。
