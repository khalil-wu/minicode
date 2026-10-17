# 当前 MiniCode 与 Codex 桌面端的差距

2026-10-17 最新实现为 `53dc068429ce4ba4f1a5b50c4f4a19b9ed08bc10`：[完整 CI 39030260425](https://github.com/khalil-wu/minicode/actions/runs/39030260425) **8/8 成功**，前端 4494 passed、backend 7034 passed / 127 skipped、根目录 1771 passed / 52 skipped。本批按实际桌面 `host.spawn → process/spawn` 纠正 Changes WS 的执行归属，删除误套模型沙箱后增加的权限拼接、备用 cwd/SID/临时位置和双向 numstat；模型 Git 权限保持原样。媒体计量改为固定 Source 原生实现并移除 PyAV 消费及依赖，真实 restricted-token 启动也交给 Source 创建并保活 private desktop。清理后统一受影响回归 **164/164**；新 Windows backend 候选 r18 独立安装 **101/101**、0 failed/error/skipped，5 组进程自然 exit 0，实际消费后的模块和 DLL/PYD 均无 av/av.libs。r17 的原崩溃与未知上游触发仍保留为历史；本批关闭的是应用加载该依赖的路径。cold 41 的受限 PATH 另有 3 条真实 Git enrichment 日志，范围和真实 Git WS 验证分开记录。详见[Source 媒体与 Git 执行器](2026-10-17-source-media-and-git-executor.md)。本批没有新增前端菜单或修改前端构建；下列全界面、四项 Changes 菜单和外机等边界仍保留。

2026-10-17 上一批生产实现 `bbc52231` 已补 HTML 词内高亮、普通会话评论、真实位置与隐藏阅读草稿归属，真实桌面发送/历史/隐藏草稿链已验证，见[Diff 评论与词内投影](2026-10-17-diff-comments-word-projection.md)。首次完整 CI 前端两处旧单文本节点断言失败，已在仅测试提交 `bf14f6f0f2d0881708cb2b63924b1d912bc51178` 修正；[上一批完整 CI 39012950337](https://github.com/khalil-wu/minicode/actions/runs/39012950337) **8/8 成功**：前端 4494 passed，backend 6999 passed / 118 skipped，根目录后端 1768 passed / 51 skipped。最终本地前端为 `assets/index-CfrFa9WJ.js`。r17 新安装的 cold 41 项 exit 0，但 presentation 9 项断言通过后 Python 发生 PyAV DLL 的原生退出崩溃；dump 已证实 C++ 析构/TLS 路径，触发根因未关闭，因此 r17 不记正式 50/50，也未验收交付。下列 `4826a0c1` / r16 均是历史，不能证明本批。Source “变更”菜单完整文件加载、富预览、隐藏空白/导入等仍未全部接入；Windows `node_repl` readiness 前置错误和这次新增观察到的 PyAV 退出崩溃分别保留。

2026-10-17 此前验收的实现为 `4826a0c12f856e17d6806f22807c6668b3e28b14`，[完整 CI 39005641653](https://github.com/khalil-wu/minicode/actions/runs/39005641653) **8/8 成功**：前端 4415 passed，backend 6997 passed / 118 skipped，根目录后端 1768 passed / 51 skipped。Windows 后端候选 r16 独立新安装 **112/112**，0 failed/error/skipped，启动错误为空。前端为 `assets/index-C9rIzayf.js`（index SHA256 `4e2bedf3caaf5f3dbe919c9b52b2a51314134ac52e830c375bc2e1c80ce8f622`）。窗口配色、Diff 各颜色来源、工作区 Git 错误和 agent 邮箱/原工作树完整交接的修改、实机范围及本地曾有的失败/挂起均见[本批交接记录](2026-10-17-window-diff-mailbox.md)。CI 与候选验收不代表全产品等价；下列旧提交、r15/r13 等均保留为历史。

2026-10-17 上一批验收：实现冻结 `19058aef` 的[完整 CI 38932478631](https://github.com/khalil-wu/minicode/actions/runs/38932478631)已 8/8 成功；前端 4393 passed，backend 6956 passed / 111 skipped。当时 Windows backend 候选为 r15，基础 41、媒体 7、目录 6 三组在独立新安装分别通过，Source peer 与 CI 跳过项分开记。当时前端为下文 Wz 构建。详情及实际目录修复、冷重启草稿保留见[最终目录与安装交付](2026-10-17-source-directory-depth.md)；下面 8dfff051 / r13 是更早专项检查点的历史验证。

核对日期：2026-10-16。后端依据为 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`；界面依据为已提取的实际 Codex 桌面资源和已有截图。最新实现检查点 `8dfff051` 的[完整 CI 38920283863](https://github.com/khalil-wu/minicode/actions/runs/38920283863)八项全部成功，backend 6946 passed / 108 skipped，frontend 4350 tests，desktop smoke 6/6。r13 后端候选另独立安装 41/41、startup ERROR=[]，原 host/内核修复及实际桌面投影均有各自证据。旧 `ee80a807`、r12 启动错误及 `0c10889e` Desktop 失败保留为历史，r12 仍暂停交付。CI 证明各自范围的合同与回归结果，不是全产品等价证明。本页不提供推测完成率。

Node 专项收尾见[Node 与桌面后续交付](2026-10-16-node-desktop-followup-delivery.md)。2026-10-17 最新后续为[主题与 MCP 媒体](2026-10-16-code-themes-mcp-media.md)及[Source 即时目录修复](2026-10-17-source-directory-depth.md)：实现冻结 19058aef、前端 Wz、Windows backend r15 候选分别留证；不再拿旧 r13 候选证明新输入。实际生产 Electron 的 Node 两轮、标题、图片、最终自动折叠、草稿及稳定图片布局均有既有证据。`0c10889e` CI 发现的多窗口测试提前退出已修 fixture，并在 `8dfff051` Ubuntu 完整运行中核对全部清理标记及正常退出，不将它误记为 Node 内核回归失败。

## 已确认的能力缺口

| 范围 | 当前实际边界 | 源码依据 |
| --- | --- | --- |
| Computer Use 配套服务 | 系统桌面操作服务仍未实现。真实 Node 持久 MCP companion 已接通工具注册、Code Mode、图片/Guardian、WS/SDK 长会话归属和实际审批；公开 Source 不含 Node 执行器，不能宣称复刻其未公开内部实现。内嵌浏览器 CDP 不代表系统桌面操作服务。 | [Node companion](2026-10-16-node-companion.md)；`backend/tools/browser_control_tool.py` |
| Node 内核与原始准入 | 安装暴露的原 host 捕获/动态配置提前启动已修复。三运行时相关回归、实际 SDK/CodeMode/图片/Guardian、生产 Electron 投影和 r13 41 项新安装验收均有证据，r13 启动 ERROR=[]。它仍是 backend 候选，不证明系统 Computer Use 或全产品等价；完整 CI 按具体提交另列，r12 不交付。 | [后续交付](2026-10-16-node-desktop-followup-delivery.md)；[分批 Node 结果](2026-10-16-node-companion.md) |
| MCP 交互式 App 界面 | 已补注册 descriptor→原调用会话→事件/恢复→交互 App；完成后常驻、原 iframe 跨历史/虚拟行/真实侧栏连续。官方 ext-apps postMessage 与 Codex 私有 sandbox relay 的协议差异明确保留，不能宣称私有 relay 已复刻。 | [本批集成与验证](2026-10-16-desktop-harness-integration.md)；`output/mcp-apps-20261016/implementation-audit.md` |
| Windows 原生权限边界 | 目标 LSP、环境安装、Git delivery、工作树快照/移除/恢复与真实 WS 新会话切换均已有实现。linked Git metadata、实际 cwd/policy cwd 分离及模型/GUI 共用快照消费链已修复；原 Unicode 归因已排除。仍保留 Source restricted token 对私有 OWNER RIGHTS Temp ACL 的拒绝，以及 unelevated deny-read 明确不支持的原生边界。 | [Windows 工作树后续修复](2026-10-16-windows-worktree-followup.md)；[目标语言服务](2026-10-16-executor-editor-lsp.md) |
| Windows backend 退出稳定性 | 应用媒体路径已移除 PyAV，直接消费固定 Source 算法；r18 的 101 项新安装验证、5 组进程自然退出及实际模块/DLL/PYD 无 av 加载均通过。r17 的 PyAV 原生崩溃保留为历史，未宣称修复其未知上游触发。该范围仍是 backend 候选，不代表完整桌面安装器或全产品等价。 | [Source 媒体与 Git 执行器](2026-10-17-source-media-and-git-executor.md)；`output/source-executor-remote-final-wheel-r18-20261017/REPORT.md` |
| Linux 本地 managed 网络路由 | 已接通 pinned Source helper 的 namespace、SCM_RIGHTS、attribution、proxy bridge、seccomp 与生命周期，含 Source 的 FD 前置参数分派。`ee80a807` CI 的 Linux 路由 16/16 通过，其中七项真实 namespace/proxy/cancel；不再列为未实现。Linux 独立安装包冷验仍不由本 Windows wheel 证明。 | `desktop/native-network/linux/entry.rs`；`output/linux-managed-routing-20261016/`；`output/ci-ee80a807-20261016/CI-REPORT.md` |
| Windows 远端 PTY 尺寸调整 | 已补原 ConPTY 请求/ACK 通道，实际尺寸、原 PID、断线重连、重复取消与窗口释放均通过；正式 runtime 已激活并核对字节及输入哈希。普通用户窗口没有被重启，新 Python 消费链需新进程加载。 | [PTY 实测](2026-10-16-windows-remote-pty-resize.md)；`output/windows-remote-pty-resize-20261016/formal-runtime-activation.json` |

永久 MCP 批准、discovery/tool_search、Skill/提示词注入、CodeMode、Guardian、多 agent 委派和消息不再按旧清单笼统列为“未实现”；它们已有生产消费链和各自范围的验证。它们的存在也不证明所有扩展服务已就绪。

## 已实现但尚未完整实机验收

- 外部 SSH 主机及真实 WSL 发行版的部署、连接、路径、权限、终端、预览和重连整链。现有本机真实 daemon/loopback 验证不替代外机验收。
- Windows WFP 的真实流量矩阵。显式 setup/status/readiness 已有，但本轮没有执行账号、ACL、防火墙 provisioning，相关跳过不能计通过。
- 相同真实模型下的大型编码任务、多 agent 长任务和长时断网对照。受控模型的端到端验收与两次真实天气任务不等于该范围已验证。
- 外部应用启动、系统剪贴板全部 MIME 类型、更多设备与平台分支。既有目的地拦截或局部系统验证不能扩大成全范围。

原 daemon / executor 会话已死亡时，当前会明确中断或保留待清理证据，不会接管另一环境来冒充原任务。这个限制与“断开前端 proxy 后原 daemon/命令继续”不同；本页不据此推断 Codex 能接管已经死亡的进程。

## UI 与操作差距

### 本次真实反馈：回答结束后过程不收起

两次实际回复的原记录均有 `item.completed(model_final/completed)`、`agent.run.completed(completed)` 和 `done(completed)`，持久正文也已结束。没有发现这两例是模型漏发最终答案。

当前前端把任意内部 `aria-expanded` 按钮、`summary` 及 WorkGroup 点击升级成父过程的手动展开状态。实际 Codex 的父过程显式选择由顶部开关控制；只有展开原始命令输出或普通 MCP 原始详情等特定阅读动作，才另外标记该对话最后阅读的轮次。收起子详情不会创建新阅读标记，父显式选择优先。

修复范围：删除广泛点击代理，分离父显式选择、子详情自身选择和原始内容阅读保护。普通网页搜索/读取、文件详情或过程组操作不再锁住整个父过程；完整最终答案到达后恢复默认收起。用户要求的“完整答案后收起”优先于 Source 的 final-start 边界。当前用户不确定曾点过哪些控件，不能把已确认的错误代码路径冒称为已经捕获当时的全部交互。

精确 minified 函数、调用片段、文件 SHA 与偏移保存在 `output/fold-completion-20261016/codex-asar-collapse-contract.{md,json}`。两次原回复的脱敏事件及完整限制在同目录的 `manifest.json`；原日志不含文本 delta，没有补造丢失事件。修复与最终验证仍以该目录后续记录为准。

本地修复已完成：16 个受影响文件按各文件最新结果合并为 **282/282**，0 skipped；首批 281 pass / 1 个旧阅读合同断言失败已保留，修正该错误期望后的两个文件 48/48，不称为重新跑过整套产品。原始 durable WS 顺序经过生产 handler、store、memo projection 与主/子记录 DOM 重放 **8/8**；仍保留无文本 delta 的记录限制。

Root 另外通过 Playwright CLI 实际点击隔离浏览器中的真实 ChatTurn 组件，用原天气回复核对三项范围：主对话网页详情打开后完整 final 自动收起、重开父过程恢复原详情选择、子记录组打开后完整 final 自动收起且没有用户气泡。最终截图为 `output/playwright/fold-completion-20261016/03` 至 `07` 开头的 PNG。这是隔离组件与实际记录验证，不冒称普通用户窗口录屏；原始命令/MCP 窄阅读保护由上述回归单独覆盖。最初 QA 页的 favicon 404 和一次 CLI 引号错误保留，不算产品失败，也不抹掉诊断。

该折叠批的入口为 `assets/index-DfQ9IEM8.js`，已通过当时 TSC、构建及两项预算；完整冻结身份见 `output/fold-completion-20261016/root-final-validation.json`，不是后续构建的当前入口。修复已随后续代码推送，`ee80a807` 的 frontend job 为 360 文件、4335 tests 通过；该次 backend 与 Windows job 的独立失败仍保留。普通 MiniCode 窗口没有被调试过程重载或关闭，独立 QA 浏览器及自有 Vite server 已退出。

### 全界面一致性尚未完成验收

过程组合、主/子 agent 工作记录、底部审批替换、侧栏选中、项目动作、统一选择菜单等已有实现和真实场景证据，不能再统称未做。后续已补设置标题、无历史的真实有效 provider、浏览器设备和 Problems 筛选菜单，并实测设备尺寸、字体及重载后的草稿保留，见[设置与预览后续修复](2026-10-16-settings-preview-followup.md)。三色主题编辑也已完成，见[外观配色](2026-10-16-appearance-colors.md)。最新一批进一步加入 Source 中 15 个开源代码主题、23 个明暗变体、分享/导入，以及 Markdown、Diff 和 Monaco 的真实 TextMate 消费链；字体、未保存代码、当前及隐藏草稿保持原样。14 个许可不明的定制主题未收录，因此不是完整 29 主题目录；OS 半透明效果和 Source app Diff 最终背景覆盖链也未借此宣称闭合。详见[主题与 MCP 媒体后续交付](2026-10-16-code-themes-mcp-media.md)。

上一批主题交付入口为 `assets/index-WzIWjdjR.js`，index SHA256 `684f9691c65912832d1bf9e6f21d34f920783284f71683328623c41eaa2f9b76`。该批实际生产 Electron 的主题菜单、导入、代码块与脏编辑器切色、嵌套焦点及重载后的活动/隐藏草稿见 `output/playwright/code-themes-20261017/`，字体仍为该 profile 原 13px/400；QA 错误与隔离环境 Git 失败单列保留。此前 CCh 构建的十项配色观测仍保留在 `output/playwright/latest-ui-followup-20261016/validation.json`，不混记为 Wz 的整场复验。JS 工具的真实 `title` 过程短行也已按参考源码修正，原参数/输出保留。后续 CE/C9 构建进一步修复双窗口配置和普通 Diff 的背景、正文、行/词/gutter 配色；本批 Cfr 已补 HTML 词内片段、普通会话评论、真实行坐标和隐藏阅读草稿，详见本页顶部新记录，不再把这些具体能力列为未实现。Source “变更”菜单的完整文件加载、富预览、隐藏空白/导入仍缺完整行为；所有区域同构建、同尺寸和缩放的普通、hover、focus、disabled、菜单、滚动及过渡状态也仍未全量核完。

因此仍需逐区域核对主聊天过程/最终答案、子 agent、左右栏、输入与审批、设置菜单、Diff/编辑器和浏览器预览。未逐项核对不等于已证明这些区域都存在样式缺陷；本页不编造具体像素差。字体、字号、字重按用户要求保留，不列为待修改项。

系统通知当前只有点击回到聊天；快捷审批/直接回复是否属于当前 Windows Codex 的能力，现有平台 gate 尚未完成核对，暂不记为已确认的 Windows 差项。类似地，不把旧报告里的缺少新窗口、新 worktree、Pin 或永久批准继续机械当作当前待办。

## 范围排除

官方账号登录、授权和依赖其账号的 hosted 服务按用户要求不做，不计为剩余缺口，也不计为已通过。
