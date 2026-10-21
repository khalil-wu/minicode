# MiniCode / Codex 全产品功能族去重清单

本清单依据当前 MiniCode 消费代码、公开 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 和已取证 Codex Desktop `26.1013.4894.0`。Bedrock 设置、主题目录、语音客户端门控和远端安装consumer另只读核对当前安装 `26.1016.6573.0` 的 ASAR；没有把这些范围扩张为全部新版桌面已验收。本批没有操作本机 GUI；旧截图不能代替当前构建所有状态的验收。

以独立行为组为单位。测试数、工具数及静态节点不作产品覆盖分母，不给没有全量验收依据的百分比。功能实现、集成验证、上游/设备/外机与视觉验收分别记录；官方账号排除仅适用于真正依赖账号的官方服务。

当前已确认的待实现行为为 **14 组**：浏览器 BR-G1–G10、独立 CUA 分发 DIST-CUA1、语音客户端页面上下文 VO-CAPTURE1、语音转移 VO-TRANSFER1、应用级网络要求 APP-NET1。队列 Undo/Redo 与 Bedrock 本地链路已实现；主题 DATA-THEME1 已由当前代码与完整数据验证撤回。真实上游、小时任务、CI、设备和视觉的待验收项不重复算成缺功能；这不是全产品覆盖率分母。

## 主要功能族

| 功能族 | 当前消费者和证据 | 剩余组 |
|---|---|---|
| 读取、搜索、编辑、补丁、命令、PTY与Code Mode | 真实权限/沙箱准入、异步命令句柄与cell、deferred工具、原owner取消/退休及结果投影均有生产消费者。 证据：`output/full-parity-20261021/runtime-audit.md`、`output/final-agent-chain-20261020/REPORT.md` | RT11、NR-A1、NR-A2 |
| LLM、模型目录、能力、推理强度、请求快照与协议 | Chat/Responses/Anthropic、按真实模型捕获的参数/能力/预算与原adapter生命周期、模型与思考强度共用入口已实现；新增 Bedrock 四种设置、SigV4、原凭据恢复和 Source 模型元数据。Realtime另列。 证据：`output/full-parity-20261021/runtime-audit.md`、`docs/validation/2026-10-21-bedrock-provider-chain.md`、`docs/validation/2026-10-21-bedrock-setup-ui.md` | APP-NET1、PROV-BEDROCK-A1 |
| Memory、compaction、上下文窗口、History/Notes | Memory抽取/整合/来源、手动与自动压缩、窗口切换和预算、History/Notes与checkpoint消费者已存在；不将任一旧子批未覆盖声明视为当前缺失。 证据：`output/full-parity-20261021/runtime-audit.md`、`docs/validation/2026-10-15-memory-phase1-request-provenance.md`、`docs/validation/2026-10-14-codex-compact-hooks-source-chain.md` | RT11 |
| Goal、heartbeat、定时任务与Guardian | 真实目标状态/预算续跑、本地调度claim、忙回合输入来源、自动准入/Guardian与真实权限回执链存在。 证据：`output/full-parity-20261021/runtime-audit.md`、`docs/validation/2026-10-15-source-heartbeat-input-origin.md`、`docs/validation/2026-10-15-source-heartbeat-host-admission.md` | RT11 |
| agent、mailbox、任务树、工作记录与message board | 六个V2协调工具按一组计；配置/namespace/准入与提示词组已补。RT10为九个board工具一组，local/:memory:已验证，公开remote HTTP/SSE已实现并进行独立集中验证。 证据：`output/full-parity-20261021/runtime-audit.md`、`output/full-parity-20261021/message-board-local.md` | RT11 |
| MCP、MCP Apps、Skills、plugins、hooks | 真实连接/目录、权限与原lease、资源/媒体/上下文、Skill指令与调用、插件清单/详情/安装消费、hooks存在；CSP握手和MCP原生窄权限已补。私有relay差异本身不是缺功能。公开skills/extraRoots/set、user scope runtime roots、真实watchdog与catalog广播已补；没有新增无桌面consumer的目录按钮。 证据：`output/full-parity-20261021/native-remote-audit.md`、`docs/validation/2026-10-16-remote-mcp-skill-consumption.md`、`docs/validation/2026-10-21-desktop-agent-alignment.md` | OS-A1、NR-A1 |
| Git、Changes、Diff、review与编辑器 | Git元数据/提交/交付、独立review、完整文件/富预览/空白与导入过滤/评论/行词高亮、Monaco脏buffer及local/独立executor LSP已有实际消费者。29主题44变体已有完整数据；编辑器按用户约定继续参考Cursor。 证据：`docs/validation/2026-10-18-git-review-settings-native-delivery.md`、`docs/validation/2026-10-16-executor-editor-lsp.md`、`docs/validation/2026-10-21-code-themes-current-catalog.md` | UI-A1、NR-A1 |
| projects、worktree、会话/草稿、storage与import | 稳定项目/会话身份、无源码项目受管目录、worktree真实创建分支与冷清理、归档/永久删除区别、导入/存储、活动与隐藏草稿/附件/未保存buffer的owner链存在。本轮已补Source自定义sections、PR/worktree持久资源、原队列编辑排序和Undo/Redo。 证据：`docs/validation/2026-10-21-thread-sections-resource-references.md`、`docs/validation/2026-10-21-runtime-catalog-and-queue.md` | UI-A1、NR-A1 |
| 本地/远端附件与媒体 | G3远端附件仅一组：条件菜单→原picker多选→原host/window/session导入→同一上传/owner/adopt/retry/草稿/缩略图消费。Windows默认文件与目录混选已撤回为误报。 证据：`output/full-parity-20261021/general-audit.md`、`output/full-parity-20261021/remote-file-picker.md`、`output/full-parity-20261021/native-remote-audit.md` | UI-A1、NR-A1 |
| 内嵌browser、下载、内容设置、密码与多级菜单 | 已有导航/标签/设备/历史/基础权限与清理/管理/原表单autofill/live下载；F1–F5已补原请求归属、单次下载准入和unknown-blob，真实 Electron POST/HTTP/blob smoke 待当前CI。G1–G10仍为不同引擎/原生边界。 证据：`output/full-parity-20261021/browser-audit.md`、`output/full-parity-20261021/browser-download-admission.md` | BR-G1、BR-G2、BR-G3、BR-G4、BR-G5、BR-G6、BR-G7、BR-G8、BR-G9、BR-G10、BR-A1、BR-A2、BR-A3、UI-A1 |
| 听写与双向Realtime | 听写原链存在；Source公开六Realtime命令/11通知、V1/V2/V3、WebRTC/existing-call、原provider/owner、真实query/steer、已提交final回传、durable speech和不推理的私有结束tail已经闭环；模型结束工具及异步tail/真实工具结果成对已修，最新57例集中通过。 证据：`docs/validation/2026-10-21-realtime-source-chain.md`、`docs/validation/2026-10-21-voice-session-tools-source-gates.md` | VO-CAPTURE1、VO-TRANSFER1、APP-NET1、VO-A1、VO-RT-API1 |
| SSH、WSL、独立executor与native sandbox | persistent host、真实alias/login shell、原窗口binding、PTY/流控、远端fs/git/LSP/worktree已有实现；Windows restricted readiness旧缺口已闭。当前26.1016已移除旧显式remote安装RPC/方法，连接行仍过滤安装action；该历史内部API差异不计当前缺功能。 证据：`output/full-parity-20261021/native-remote-audit.md`、`docs/validation/2026-10-18-real-ssh-source-harness-delivery.md` | NR-A1、NR-A2 |
| Computer Use | 显式安装Source runtime的本机Windows消费与SDK/AgentSession/ThreadRuntime关闭链已实现；独立随Mini分发及remote/non-Windows receiving尚不在已交付覆盖。 证据：`output/full-parity-20261021/native-remote-audit.md`、`docs/validation/2026-10-17-installed-cua-source-consumer.md` | DIST-CUA1、CUA-A1 |
| 桌面shell、通知、设置、语言、外观与菜单布局 | 侧栏/工作记录/审批替换/完整final收起、native菜单与通知返回、设置/自定义指令/本地音频/窗口外观等已有消费者；已去掉Source不存在的常规控件；保留用户特色字体。 证据：`output/full-parity-20261021/general-audit.md`、`docs/validation/2026-10-21-desktop-agent-alignment.md`、`docs/validation/2026-10-14-notifications-source.md` | UI-A1、OS-A1、INTEG-A1 |

## 本轮已实现组

| 组 | 当前状态 | 证据 |
|---|---|---|
| RT-CORE | implemented_verified_locally | `output/full-parity-20261021/runtime-audit.md`、`output/full-parity-20261021/runtime-final-delta.xml` |
| RT-PROMPT | implemented | `output/full-parity-20261021/runtime-audit.md` |
| RT10 | implemented_verified_locally | `output/full-parity-20261021/message-board-local.md`、`output/full-parity-20261021/runtime-remote-reconnect-repair.xml` |
| G3-REMOTE-ATTACH | implemented | `output/full-parity-20261021/general-audit.md`、`output/full-parity-20261021/remote-file-picker.md`、`output/full-parity-20261021/native-remote-audit.md` |
| MCP-CSP | implemented | `output/full-parity-20261021/native-remote-audit.md` |
| G1 | implemented_removed_extra_controls | `output/full-parity-20261021/general-audit.md` |
| BR-F1 | implemented | `output/full-parity-20261021/browser-audit.md` |
| BR-F2 | implemented | `output/full-parity-20261021/browser-audit.md` |
| BR-F3 | implemented | `output/full-parity-20261021/browser-audit.md` |
| BR-F4 | implemented | `output/full-parity-20261021/browser-audit.md` |
| BR-F5 | implemented_verified_locally_pending_native_ci | `output/full-parity-20261021/browser-download-admission.md` |
| UI-SECTIONS | implemented_verified_locally | `docs/validation/2026-10-21-thread-sections-resource-references.md` |
| THREAD-RESOURCES | implemented_verified_locally | `docs/validation/2026-10-21-thread-sections-resource-references.md` |
| SKILL-EXTRA-ROOTS | implemented_verified_locally | `output/full-parity-20261021/catalog-queue-audit.md` |
| VO-RT1 | implemented_verified_locally | `docs/validation/2026-10-21-realtime-source-chain.md`、`output/full-parity-20261021/realtime-final-freeze.xml` |
| QUEUE-EDIT-ORDER | implemented_verified_locally | `docs/validation/2026-10-21-runtime-catalog-and-queue.md` |
| PROV-BEDROCK1 | implemented_verified_locally_pending_external_acceptance | `docs/validation/2026-10-21-bedrock-provider-chain.md`、`docs/validation/2026-10-21-bedrock-setup-ui.md` |
| DATA-THEME1 | already_implemented_current_data_verified | `docs/validation/2026-10-21-code-themes-current-catalog.md` |

RT01–RT06/RT09 同属六个 V2 协调工具的一组，RT07/08 是提示词一组；RT10 的九个 board 工具是一组。远端菜单、picker、host import、原路径到模型和 owner/retry 是同一远端附件链。sections 与持久 PR/worktree 资源关联各一组，后者不等于上传附件。

Realtime 当前冻结为一次 **57/57** 集中运行：13 个协议/原 owner/真实 query/冷读/关闭、11 个模型结束工具、27 个生命周期/checkpoint、6 个历史性能用例；不再累加旧40例。它没有使用真实麦克风或外部 Realtime 服务。关闭尾部按 Source `transcript_tail::record` 仅持久上下文，不启动推理；异步tail插入也不能让真实工具结果被误丢为 orphan。详见 `docs/validation/2026-10-21-realtime-source-chain.md`。

## 精确剩余组

| ID | 状态 | 具体行为与证据 |
|---|---|---|
| BR-G1 | partial_missing | Chromium intrusive-ad分类及内容设置消费缺失 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-G2 | partial_missing | 完整origin/request-initiator限流、原permission callback队列及例外缺失；与BR-V1 agent下载准入不同 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-G3 | partial_missing | 按origin的Background Sync内容设置缺失 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-G4 | partial_missing | 第三方默认/例外/分区/embedded access策略未等价；已有cookie保存与清理不能代替 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-G5 | partial_missing | FedCM及嵌入身份设置消费缺失 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-G6 | partial_missing | 用户手势/trusted-trigger分类、origin policy及blocked-popup展示缺失；原POST/opener事务已补 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-G7 | partial_missing | navigator.registerProtocolHandler按origin permission/registration管理缺失 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-G8 | partial_missing | collapse-all/recommended/expand-all及native推荐分类器缺失；原pending生命周期已补 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-G9 | partial_missing | Source私有save bubble、成功登录分类、suggestion/generation UI未复现；现有加密管理/原表单填充并非缺失 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-G10 | partial_missing | 任意iframe/script navigation仍缺相同trusted modifier事实；主frame HTML/SVG已补 证据：`output/full-parity-20261021/browser-audit.md` |
| DIST-CUA1 | private_dependency | 独立随MiniCode分发的可再分发native runtime/完整替代，以及remote/non-Windows receiving覆盖尚未交付；已安装Source原runtime本机消费不是缺失。 证据：`output/full-parity-20261021/native-remote-audit.md`、`docs/validation/2026-10-17-installed-cua-source-consumer.md` |
| VO-CAPTURE1 | partial_missing | 缺原active voice/host/thread门控→实际主窗口前台→page/thread/right-sidebar/tabs summary的客户端consumer；browser screenshot不等价。证据：`docs/validation/2026-10-21-voice-session-tools-source-gates.md` |
| VO-TRANSFER1 | partial_missing | 缺original locator→目标解析/返回→原媒体stop→目标start/context/前端状态迁移；换后端service owner不能代替。证据：`docs/validation/2026-10-21-voice-session-tools-source-gates.md` |
| APP-NET1 | partial_missing | 缺application/network requirements→应用HTTP/WS/AWS SDK准入、策略更新撤销及unsupported-SDK命令gate；既有工具进程NetworkExecutionPolicy/WFP及GovCloud advisory不能代替。证据：`docs/validation/2026-10-21-application-network-source-boundary.md` |
| RT11 | pending_acceptance | 需真实>=1h业务/不可变oracle/child、持续原PID/handle、>=30s断线、周期B实际provider/read_file、冷读与关闭；343.828s历史证明不替代。 证据：`output/full-parity-20261021/runtime-audit.md`、`output/final-agent-chain-20261020/REPORT.md` |
| BR-A1 | pending_physical_acceptance | pause/resume/completion与updater restart blocker真实实例验收。 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-A2 | pending_physical_acceptance | 真实device/native permission及OS secure storage，不由fixture推断。 证据：`output/full-parity-20261021/browser-audit.md` |
| BR-A3 | pending_isolated_ci_acceptance | 新增原事务与动态blob smoke需隔离Electron CI实际执行，本机禁GUI未执行。 证据：`output/full-parity-20261021/browser-audit.md` |
| UI-A1 | pending_physical_acceptance | 当前构建、同尺寸/缩放逐区域普通/hover/focus/disabled/多层菜单/拖动/滚动/动效；保留特色字体；未核状态不是已证实像素缺陷。 证据：`output/full-parity-20261021/general-audit.md`、`output/full-parity-20261021/browser-audit.md`、`docs/validation/2026-10-21-desktop-agent-alignment.md` |
| VO-A1 | pending_physical_acceptance | mic、键盘capture/SendInput、focus/clipboard原目标、录音/转录/重试/设置导航真实验收。 证据：`output/full-parity-20261021/general-audit.md`、`docs/validation/2026-10-21-desktop-agent-alignment.md` |
| NR-A1 | pending_external_environment_acceptance | 实际host上的fs/PTY/Git/LSP/worktree/断线/窗口与voice本地服务归属；既有外机证据按其旧冻结身份引用。 证据：`output/full-parity-20261021/native-remote-audit.md`、`docs/validation/2026-10-18-real-ssh-source-harness-delivery.md` |
| NR-A2 | pending_physical_acceptance | selected restricted/elevated/managed networking、ACL/private desktop/进程清理、IPv4/IPv6/proxy等真实策略矩阵；不改用户机器政策来伪造通过。 证据：`output/full-parity-20261021/native-remote-audit.md`、`docs/windows-native-sandbox.md` |
| CUA-A1 | pending_physical_acceptance | 实际截图/输入/app approval与原pipe/helper关闭；历史仅bootstrap/list_apps不能替代。 证据：`output/full-parity-20261021/native-remote-audit.md` |
| OS-A1 | pending_physical_acceptance | 原生clipboard/default app/focus/Mica及MCP encrypted-media/手势clipboard；macOS路径仅相应平台验收。 证据：`output/full-parity-20261021/native-remote-audit.md`、`output/full-parity-20261021/general-audit.md` |
| INTEG-A1 | pending_integration_acceptance | 本轮所有生产编辑冻结后构建/包/冷装/CI；baseline九个green jobs只证明baseline。 证据：`output/full-parity-20261021/general-audit.md`、`output/full-parity-20261021/runtime-audit.md` |
| PROV-BEDROCK-A1 | pending_external_provider_acceptance | 本批独立签名、真实loopback HTTP/SSE和前端验证证明本地实现；没有真实AWS/GovCloud凭据或上游推理回执。证据：`docs/validation/2026-10-21-bedrock-provider-chain.md`、`docs/validation/2026-10-21-bedrock-setup-ui.md` |
| VO-RT-API1 | pending_external_provider_and_physical_acceptance | 本轮loopback真实HTTP/WS与ScriptModel证明本地协议和harness链；未使用真实麦克风/扬声器和外部Realtime服务，不把配置可用等同上游兼容或设备已验收。 证据：`output/full-parity-20261021/realtime-source-expanded-repair.xml` |

浏览器 G1–G10 是十个真实引擎/原生行为组，现有 stock Electron 没有对应的完整 producer；不是十个可用开关。BR-F5 的 agent 单次下载准入已实现，与 G2 的网页 origin 多次下载 limiter 分开；真实 POST/HTTP/blob smoke 仍由隔离 Electron CI 验证。

## 已撤回或被后续覆盖的旧说法

- MCP私有relay不同即缺功能：真实public ext-apps消费已在；私有协议identity差异不自动成为功能差异。
- MCP camera/microphone/geolocation授权缺口：当前Source native MCP session也拒绝这些设备；不虚构grant。
- Windows默认必须混选文件和目录：Source默认openDirectory仅macOS；Windows openFile+multiSelections。
- SSH必须自动安装peer：Source启动也PATH-probe失败并提示手动安装；26.1013虽有独立显式安装内部API，但26.1016已无该RPC/方法，当前设置行也明确过滤安装action。NR-INSTALL1按新版consumer撤回，未声称Mini已实现受管远端安装；见 `2026-10-21-remote-install-current-boundary.md`。
- 缺独立executor LSP：后续executor_editor真实process/stdin/stdout/LSP与原lease已实现，旧503报告被后续交付覆盖。
- 没有CUA或restricted-token readiness仍依赖elevated marker：已安装Source runtime消费及discover_restricted_runtime当前已在；原早期报告过时。
- 自动跨应用重启重建下载是必需Source parity：已核Source仅liveDownloads参与恢复/重启阻断，未确立自动跨重启重建合同。
- 仍缺14个定制主题21变体可执行目录：`aba4a45c` 已进入当前HEAD，本批验证29ID/44变体及完整规则；实际来源与notice状态另存，不再把历史资产问题计为技术缺功能。

## 验收解释

- 读取/编辑/命令/Code Mode、模型快照、Memory/compact/goal/heartbeat、agent/mailbox、MCP/Skill/plugin/hook、Git/review/editor、projects/worktree/storage/import 等已有完整生产消费者；本表没有把这些大模块的每一状态冒称为全部复核。
- UI-A1 是当前构建在相同尺寸/缩放下的普通、hover、focus、disabled、多级菜单、拖动、滚动和动效验收。未核状态不能直接称为已证实的像素缺陷。用户特色字体、字号及字重不列改动。
- 成功收据只证明其冻结输入和范围；失败、中断、skip、未执行分别保留。baseline 的九个 CI green jobs、旧外机记录或短任务不能替代本轮最终构建和一小时验收。
- 真实上游账号/API Key、麦克风/扬声器、WFP/OS secure storage、外机 SSH/WSL、CUA 输入及系统窗口状态的验收单列，不以 fixture 推断实机通过。
- `thread/inject_items` 的接口名不同暂不计作独立产品缺失：需与 MiniCode 已有受控 history、side-chat 和跨 host 恢复的实际 consumer 对照；公开 Source 的 prediction 方法自身未实现也不算 MiniCode 缺功能。

机器可读版本：`output/full-parity-20261021/full-parity-ledger.json`。新生产改动完全冻结后，root 负责最终构建、小时任务、发布候选和最新 HEAD 的 CI。
