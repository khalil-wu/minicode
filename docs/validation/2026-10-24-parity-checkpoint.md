# Codex / MiniCode 对照记录与本轮修复

2026-10-24，按用户要求停止继续操作界面，先保留已看和未看的边界，再开始实现。停止前没有创建定时任务、运行模型、安装插件、修改权限、保存项目或编辑文件。普通界面操作曾打开文件预览、内置编辑器和一个空闲终端；没有输入终端命令。目标输入模式曾临时打开，已恢复为空输入。Codex 最后停在本次新开的空定时任务表单，未提交。

基线：MiniCode `838feed479d7cdfa356b788471f17a5b89c0a056`；`.tmp/codex-src` `822e58cc3d666166c7446c5b1ea2e52f5d09594c`；桌面包 `26.1016.8622.0`，包内应用 `26.1016.62342`。源码和桌面包不声称来自同一构建。

## 已经实际观察

- Codex：19 个设置页逐页进入，包含常规、导入、外观、归档、语音、存储、配置、个性化、Mini 与宠物、快捷键、插件、电脑操控、应用快照、浏览器、钩子、连接、Git、环境和 Worktrees。展开过主题/字体/颜色、配置范围与枚举、指令编辑器、宠物大小、快捷键录入、市场/MCP 表单、Skill 详情更多、Appshot 路由、SSH 手动表单/颜色、环境高级/操作/图标。
- MiniCode：进入对应已有设置页，检查当前真实构建；主题菜单滚至 Xcode、插件/MCP 两种表单、宠物资源最终加载、配置用户范围与搜索枚举、归档筛选、存储空结果、Git/环境/Worktrees 状态。个性化主页有截图观察，部分早期 AX 仍是前一页，不将它算作稳定控件证据。
- Codex 工作区：已有长聊天、摘要浮层、子 agent 列表及工作记录、共用模型/强度入口、添加内容菜单、聊天/项目操作和子菜单、原生 File/Edit/View/Help、浏览器新页/Tools、Files 与独立文件查看器/打开选项、Changes 来源/提交/更多/单文件菜单、完整/分屏/隐藏标签、共享标签右键、应用任务管理器。自然出现过“正在重新连接 1/5”，仅短行观察，未展开详情。
- MiniCode 工作区：切换两条已有会话时的中央消息加载与文件加载；浏览器新页/Tools；Files 树与普通预览、扩大/还原、共享预览右键无菜单；文件树右键及“在 MiniCode 中打开”独立编辑器；内置编辑器行号/高亮/minimap/标签右键/更多（保存全部为 0）；上下文里的输出/终端/用量；右栏溢出；底部空闲终端；模型→强度→模型列表；添加内容、聊天继续/复制、项目编辑→图标→表情、侧栏会话→分组/项目菜单。
- 定时任务：两端入口已进入。Codex 独立侧栏/新建弹窗→高级→重复→自定义的截图已看，未提交。MiniCode 进入设置内嵌表单；其重复/时区/执行位置等下级菜单尚未打开。部分 Codex modal AX 没有刷新，`codex-scheduled-create-advanced.txt` 才有真实 modal 控件；不按文件名声称通过。

原始 AX 与逐帧纠错保存在 `output/full-parity-audit-20261023/live-ui/` 和 `live-ui-index.json/md`。164 份记录只是截至 11:43 的索引快照；以后增加的记录不能用旧数量代替。AX 的聊天正文/Document text 附录不是控件证据。截图只查看工具当次返回，没有另存或重复解码。

## 工作区清单逐项边界

“已看”表示下面写明的页面或控件已观察，不表示业务操作、所有状态或端到端验收通过。

| 项 | 已看范围 | 还没看到或执行 |
|---|---|---|
| R00 全部右栏入口 | 两端 browser-first、Tools、主要共享标签与溢出 | Mini 12 个注册面板并非都已通过可见入口到达 |
| R01 上下文 | 两端摘要，Mini 输出/终端/用量 | 全部有数据分型、错误/后台任务变化 |
| R02 Changes | Codex 来源/提交/更多/单文件；Mini 源码全链 | Mini 当前 Git fixture 的所有菜单与实际复制/审阅 |
| R03 预览 | Mini 文本单页与扩大/还原 | 图片/PDF/视频/音频/Markdown、错误与重载 |
| R04 浏览器 | 新页与 Tools、设置、原生 View 浏览器子菜单 | 本轮未重新执行所有导航/下载/权限/扩展状态 |
| R05 终端 | Mini 底部终端 chrome、空闲提示符 | 右栏迁移、多个会话、退出/恢复；未运行命令 |
| R06 Files | 两端树/文件查看，Mini 文件右键与内置编辑器 | 所有根目录、远程/多根、大文件/二进制/失败 |
| R07 目标 | 源码持久化/事件/续跑链；Mini 临时输入模式 | 保存/暂停/续跑/清除真实 goal 均未执行 |
| R08 侧聊 | 两端入口、源码 | 未新建或运行侧聊 |
| R09 产物 | 摘要/上下文可见文件与 Source 工作记录 | 独立产物面板、各媒体和失败状态 |
| R10 子 agent | Codex 列表/真实记录，Mini 已有多 agent 过程 | Mini 每个 child 详情、通信/中断/恢复运行 |
| R11 运行详情 | 静态生产/消费链 | 当前普通入口可达性与实际详情 |
| R12 运行状态 | 静态生产/消费链 | 当前普通入口可达性与实际详情 |
| R13 MCP App | 静态动态页契约 | 未启动 MCP 应用或打开真实应用页 |
| R14 会话自动化 | 全局任务入口与 Source 新建表单 | 会话动态页/详情、运行结果、通知行为 |
| R15 标签布局 | Codex 完整/分屏/隐藏/右键；Mini 扩大/还原/溢出 | Mini 全部移左右/置顶/拖序/宽度组合 |
| R16 编辑器 | Mini 只读文件、tab/menu/minimap；源码 | 补全/导航/编辑保存/外部变更/草稿恢复，本轮未改文件 |
| R17 底部工具 | 非 code 场景终端 | code 的问题/Git/用量/调试及跨栏状态 |
| C01 聊天骨架 | 两条 Mini 完成会话、Source 长聊天 | 所有消息/媒体/状态分型 |
| C02 模型强度 | 两端共用入口与模型列表 | 未修改配置或跑每种强度；模型数量来自 profile |
| C03 权限 | 当前文案；Source 设置枚举 | 未改变权限，未执行审批 |
| C04 输入草稿 | 原输入为空，返回仍为空 | 未写用户草稿做切换/撤回/历史用例 |
| C05 添加内容 | 两端菜单、Mini 插件空列表条件 | 文件选择/上传/Appshot/Sketch/Tab 引用未执行 |
| C06 slash/mention | 静态目录/上下文链 | 当前菜单全部分支未打开 |
| C07 项目 scope | 项目编辑/来源文件夹/图标/表情 | 多根/远程及新聊天 scope 下级未全看 |
| C08 queue/steer | 静态链 | 未新跑模型，不声称排队/后台发送通过 |
| C09 审批/问题 | 静态替换输入区契约 | 未触发/响应真实审批或问题 fixture |
| C10 过程 | 已有完成会话收起；Source 真实过程与重连短行 | 实时耗时、失败/中断/续传全部状态 |
| C11 搜索 | 原生菜单、静态全文/定位链 | 未输入关键词实测跨页搜索 |
| M01 项目菜单 | 两端编辑/图标/颜色/表情；Mini 空归档禁用 | 未保存、移除、归档、改分组 |
| M02 会话菜单 | Mini 正常聊天及分组/项目二级 | 复制三级之外的所有不同 owner/gate 组合 |
| M03 聊天菜单 | 两端继续/复制/打开方式等已有分支 | 自动化会话页、真实 fork/handoff/新窗口未执行 |

## 比较口径

- 当前 Source 缩放为 1.3，Mini 原生 level 0.5 约 1.095；UI/代码字体也是用户不同偏好。没有改缩放/字体，不拿整窗像素差直接报缺陷。
- 19 个设置页和协议注册库存不等于全按钮、全状态通过。实现前审阅快照含 63 个有范围差异组、32 个协议/feature 未核实项；这不是完成度，也不是缺 63 个按钮。
- 官方账号登录/授权及真正依赖它的线上服务排除；本地模型、MCP OAuth、自定义网关、AWS、本地环境不能整体排除。
- 本轮开始修复的是已确认调用链；尚未验证的恢复条件先通过可控 fixture 复现。未经复现不将静态风险写成实测故障。

## 实现分工与验证

- 文件工作区：Home owner、独立文件标签、树/查看器动作和共享标签菜单。
- 设置：全局派生底色、宠物选择/大小/bar-only、SSH 颜色持久化、已有 Skill 卸载 API 的 UI 接线。
- Runtime：并发摘要 sequential-cutoff 契约；恢复失败/父任务仍运行时 child 水合条件的复现和修复。
- 主线：摘要独立浮层/固定状态、Changes 复制 git apply、当前已见菜单的缺口。

先集中完成代码和必要测试，再统一编译、定向测试及构建。构建和测试结果在完成后另记；本文件不预填通过，不把没执行的 GUI 状态标成已验收。

## 第一批代码验证记录

已实现独立文件标签及完整 owner 读取链、Home 文件入口、树筛选/根目录/All changes、文件查看与比较/打开选项、置顶持久化与左右移动；独立摘要 pin/popover/shift/gutter 与右栏并存；Changes 复制包含二进制及仓库相对路径的 `git apply --3way` 命令；原生 View→Browser 菜单及 Tools 卡片；全局 Scheduled 页面/新建弹窗/真实全局列表与定向资源操作；主题公共填充、Pets 及 None 横条、SSH 颜色、两个 Skill 管理入口的真实卸载；并发摘要请求/投影，以及两条恢复风险的已复现修复。

- 整批 TypeScript 检查通过。
- 首轮前端 41 文件：794 passed、5 failed，另有 2 个由同一缺失 mock 引起的未处理错误。修正后受影响的文件工作区 77 项、设置/App 81 项、恢复生命周期 46 项分别通过，未处理错误为 0。已通过且未受修改影响的用例未重复执行，不把这些回归数量累加成独立覆盖率。
- 原生 Node 一批 67 项通过，包含真实本地 daemon 断开/重连、8 MiB 预览字节与原 PTY 存活记录；这不等于新模型长任务验收。
- 后端新增 patch/Skills/协议注册批通过，2 项跳过；另一次工作区/Scheduled/比较来源批仅一个测试仓库与 runtime-state 目录混用的 fixture 失败，隔离后该 5 项回归通过。14 个需要显式连接 owned Source exec-server 的分支跳过，均不算通过。
- 并发摘要/恢复专项先复现 2 个失败条件，修改后后端 88 项、前端 126 项通过。上述恢复前端文件也在 41 文件批中，不能重复计入不同用例总量。

精确日志位于 `output/full-parity-audit-20261023/`：`frontend-implementation-test.log`、`frontend-file-workspace-retest.log`、`settings-center-regression-test.log`、`runtime-steer-replay-regression.log`、`backend-workspace-scheduler-test.log`、`backend-file-workspace-retest.log` 及各区域 implementation handoff。首轮失败保留，没有改成通过日志。

## 后续已看设置范围与纠错

继续实现常规页实际偏好/弹出窗口、配置警告/可见性/独立 Provider 路由、连接入口、主机记忆设置及已连接 host 选择。这些变动的验证结果尚未并入第一批通过记录。

目录 Reset 曾被审计误列为多余按钮。再次核对当前 Source `general-settings-27118198cef3.js:245–246` 后确认，Source 和 Mini 都仅在存在自定义覆盖值时显示 Reset，点击清为 `null`；已撤销此差异并保留 Mini 原有入口。常规页移除的是其余三类普通控件，不能写成“四类已移除”。

Appshots、Chrome/Edge/Excel 管理与引用、Sketch、浏览器 tab 引用 intake、workspace dependency bundle 的启用/诊断/重装链，以及没有继续操作的界面状态仍有未闭合或未验证范围。没有用空开关、第三方账号连接或通用版本/诊断结果冒充这些能力。

## 第二批已实施范围

- 常规：默认完整视图、弹出窗口快捷键与默认无项目、确认合并后的彩纸偏好，均贯通原生持久化与实际消费者；连接页进入普通导航。移除普通页的默认终端位置、高级新任务权限、桌面更新三类额外控件，底层配置保留。目录 Reset 按上文纠错保留。
- 弹出窗口：独立 window owner、来源主机/目录、隐藏与重唤、原有草稿及重启恢复。复核补齐原生菜单对单独 workingDirectory 变更的订阅；新增主进程模块列入发布包文件表与 Node 单元测试入口。
- 配置：中文 Indexed 显示“索引”，wire 值保持 indexed；真实 startup_warnings 经 GET/PUT 投射到配置页；普通配置页不再挂载 Provider 与内部 feature groups。已有显式模型/API 入口和其未保存草稿保留。Max/Ultra 可见性走原生偏好，过滤仅影响菜单，不改模型能力、当前强度或定时任务。
- 个性化：主机级 memories 配置替代当前会话 memory_mode 开关；仅列本机与真实已连接远端，经主进程向所选 host 读写指令、记忆偏好和 reset，其他 host 的 token 不进入 renderer。切换 host 保留当前会话、目录及各 host 指令草稿。
- 运行消费者：每轮固定记忆策略快照，分别控制生成默认值、注入/工具、后台启动和外部上下文污染。memories feature 默认 false，generate/use 默认 true，disable_on_external_context 默认 false；保存偏好不会重写既有会话模式或删除记忆。
- 配置根因：旧 feature_flags 表仅在每个解析层内归一为 features；同层 canonical 优先，再按既有层顺序合并。修改单个开关只迁移被修改的 key，其他用户值保留。公共协议中叫 feature_flags 的投影字段保留其 wire 契约。

第二批首轮：严格类型检查通过；前端 27 文件为 466 passed、3 failed；后端 31 文件为 471 passed、1 failed；Node 12 文件为 107 passed、3 failed。原始日志分别为 phase2-typescript.log、phase2-frontend.log、phase2-backend.log、phase2-desktop.log，均保留在 output/full-parity-audit-20261023/，不以回归覆盖首轮失败。失败定位及最终回归、构建结果在完成后追加。

上述本轮实现不覆盖整个实施前源码库存。宿主 ResponseItems 注入和 dynamic tools、压缩输入、子 agent 工具门控、Source 规范 role/feature 禁用链、Hook/MCP/环境/Memory V2、搜索/快捷键/文件监控/项目幂等、市场升级/缓存、审阅目标/导入、自定义网关/权限 profile/AWS、timeline/模型元数据/图片预算/补丁流，以及其他明确未改范围继续保留。逐组当前实施范围见同目录 2026-10-24-parity-ledger.md；没有把只改过相邻文件当作该组已闭合。

## 第二批回归与最终构建

首轮失败均保留原日志。后续修正与结果如下，重复覆盖不累加为测试总量或完成度。

| 检查 | 结果 | 日志（output/full-parity-audit-20261023/） |
|---|---|---|
| 前端两份 General、composer adoption/create/switch、实际发送、恢复/长任务投影、宠物创建等 14 文件 | 438 passed，0 failed | phase2-frontend-owner-retest.log |
| 记忆 phase1 WebSocket 完整文件 | 10 passed，0 failed | phase2-memory-websocket-retest.log |
| 追加能力消费者、配置/权限、realtime、memory 路径/job store 12 文件 | 121 passed，23 skipped，0 failed | phase2-capability-consumers.log |
| 原生 remote/window 清理 2 文件 | 19 passed，0 failed | phase2-desktop-cleanup-retest.log |
| npm run build（含严格 TypeScript，MINICODE_VITE_RELATIVE_BASE=1） | exit 0 | final-desktop-build.log |
| 最后两处 CSS 动效改用既有 token 后，vite build 增量打包 | exit 0 | final-desktop-css-build.log |
| 最终 bundle budget / UI debt budget | 均 exit 0 | final-bundle-budget-retest.log / final-ui-debt-retest.log |
| git diff --check | exit 0 | final-diff-check.log |

失败根因及处理：

- General 的保留菜单旧数量及已删除高级 summary 断言过期，改为精确枚举 8 个实际菜单，保留草稿、媒体和原偏好断言。新增 adoption fixture 原本先 resolve result 再手工 switched，违背后端 send_payload/switched → ordered delivery receipt → command.result、前端同步投影的真实顺序；改走 ensureComposerConversation 并按 canonical 先到的顺序断言 full-view、草稿和附件。没有加入假 ACK race 兜底；仅保留本批真正需要的目标布局与 adopted draft 合并。
- 原生 3 个失败来自提取真实 main 清理函数的 VM fixture 未注入新 popoutWindow。补入真实依赖形状，新增首次失败/显式重试的 dispose 调用断言，保留原 owner、传输和后端停止断言。
- phase1 取消 fixture 的旧 entered 只表示服务端发送，不能证明客户端已消费用量；单节点再跑一次复现原失败，日志 phase2-memory-usage-original-cancel-once.log。现在在真实客户端事件迭代器的 usage-bearing created 已交给 collector 后发消费信号，再执行取消/关闭/失败路径；4/2 token、取消抛出、全局/side 计账及连接清理断言保留。产品计账代码未改，没有睡眠或吞取消。
- 首次 UI debt 检查发现 ScheduledTasksPage 两处硬编码 120ms，原失败日志 final-ui-debt.log 保留。两处改为已有 --transition-fast，原 reduced-motion 门控保留；最终硬编码动效值为 0，没有抬预算。

追加批次的 23 个跳过来自 Windows 无 symlink 创建权限的路径分支；junction 及可执行的路径用例仍按原条件验证，跳过不计为通过。第一批另有 owned Source exec-server 分支和环境分支跳过，仍按前述记录保留。

新产物已写入 frontend/dist，入口使用相对 assets，当前入口 JS 914147 bytes；发布文件表含 popout-window.js。静态核对 main 的 29 个本地 require 均在发布文件表内。正常用户窗口和后端保持运行，未重启、保存或清除用户草稿。本轮停止 GUI 后没有再做修改后视觉验收、真实 provider 长任务或 GitHub CI；所列定向回归通过不扩大为全仓或全功能通过。

## 继续批次：清理请求后的源码与桌面对齐

已按原 Source 及保存的桌面证据补齐以下选定调用链：child update_plan、两个 async 用户工具的独立 root/DirectModelOnly 门控、CurrentTimeReminder/clock 与 sleep 配置、role memories 禁用继承、canonical multi_agent/goals、当前项目能力刷新与已运行快照分离、Skill MCP 依赖的 first-party 来源、Interrupt 生命周期和 MCP 工具型 Hook、本地压缩去除额外 500-token 裁剪、全局/归档正文搜索以及生产构建的 debug 表面门控。Goal 前端入口、现存视图和直接模式入口消费同一能力；目标、输入和各 owner 编辑草稿保留。

本批还修复了真实的 direct async 持久化缺口：原 UI callback 不经过 yielded query stream，QueryEngine 桥仅记录 native tool starts，漏记 async item。现在同一桥对 direct `delivery=async` 的 item.started/completed 先记录再交给 host，保留 payload/owner/顺序；nested CodeMode/extension 保留其原记录入口，避免重复。测试同时安装真实 WS 同形 emit callback，继续断言 UI、accepted/questions、call_source、执行日志和不注入模型历史。

本批日志均在 output/full-parity-audit-20261023/，首轮和区域失败没有覆盖为通过：

| 批次 | 原结果或最终回归结果 | 日志 |
|---|---|---|
| 首轮 core 23 文件 | 441 passed、38 failed | phase3-core.log/xml |
| 首轮 integrations 17 文件 | 201 passed、1 failed | phase3-integrations.log/xml |
| 首轮前端 10 文件 | 382 passed、1 failed | phase3-frontend.log |
| Runtime 四文件修构造/gate fixture 后 | 112 passed、1 failed（direct async，随后继续修复） | phase3-runtime-retest.log/xml |
| direct async / QueryEngine / 原生工具 / 准入与生命周期七文件最终回归 | 112 passed、0 failed | phase3-async-journal-retest.log/xml |
| 搜索返回契约回归 | 6 passed | phase3-search-retest.log/xml |
| 表式 feature override 回归 | 13 passed | phase3-feature-overrides-retest.log/xml |
| Goal/菜单/发送/搜索前端七文件区域重跑 | 207 passed、1 failed（fixture 聚焦顺序，随后修正） | phase3-frontend-retest.log |
| Goal 聚焦及菜单恢复最终回归 | 14 passed | phase3-goal-focus-retest.log |
| 搜索 hook 最后文案/取消回归 | 5 passed | phase3-search-locale-retest.log |
| 最终 TypeScript + 相对路径生产构建 | exit 0 | phase3-final-desktop-build.log |
| 最终 bundle/UI debt | 均 exit 0 | phase3-bundle-budget.log / phase3-ui-debt.log |
| git diff --check | exit 0 | phase3-diff-check.log |

首轮 core 的集中错误包括测试未传 TaskTool 必需的 ArtifactStore、AppConfig 缺 LLMSettings，以及旧默认曝光/child hard-deny 假设。修成真实构造和显式源配置，保留父 deny、实际工具事件和 journal 断言；没有给生产构造器加兜底。直接 async 的最后缺口已按上文修完整链，不能统称 fixture 问题。新 Hook 及中断配置、命令 stdin、终态顺序、失败不阻断取消等用例在 integrations 批次通过。

搜索首轮失败确认返回的宽 summary 含 context_snapshot。改为明确的 11 字段搜索摘要，不修改共享 projector、不以防御性 pop 隐藏字段，保留 private/provider checkpoint 不读取断言。正文读取仅使用 public user/assistant 文本；当前批次每次 HTTP 最多扫描 200 个聊天、客户端每批 50 条，真实 cursor 和 cold zstd 单向首命中读取均有受控用例。已开始的线程池扫描可以完成其有界 chunk；大型真实全库延迟未量测。

前端首轮暴露 Goal 行只被 disabled、未按 feature 隐藏。显示门与会话切换暂不可用状态分开，保留原暂不可用行为。重新启用后 GoalPanel 会自动聚焦并按原交互关闭菜单，区域 fixture 重新打开菜单后继续验证原 Goal 行与未保存编辑内容，没有改产品焦点行为。TypeScript 初轮两处动态错误被当作翻译键的失败保留在 phase3-typescript.log；修为展示实际错误，最后本地固定文案复用既有翻译键。最终入口为 ./assets/index-BjZNq0z4.js，914824 bytes。

清理未完成：自动审批拒绝了工作区内批量缓存删除，以及缩小后的三个固定绝对路径删除；返回原因仅 blocked by policy。约 93.6 MB 的旧 baseline-dist、根 pytest/bytecode 缓存仍存在，没有绕过删除拒绝或把枚举大小当作释放空间。细节见同目录 compaction-goals-cleanup 文档。当前构建、依赖、参考源码、审计证据及用户配置/会话/草稿保留。

本批没有继续 GUI、重启用户窗口、安装用户插件、调用真实 provider 或运行 GitHub CI。R08/T19、FF-CODE-MODE、其他协议/集成/桌面/原生未改组及未观察状态继续按逐组账本保留，不报整体完成百分比。
