# 2026-10-24 对齐实施账本

本文件记录当前共享工作树中“本轮改了什么、没有改什么、哪些原判断已纠正”。它不是全量对齐声明，也不把代码接线、静态审阅、受控 fixture、当前 GUI 和真实 provider 验收混为一个状态。

## 依据与状态口径

- 固定审计基线：MiniCode `838feed479d7cdfa356b788471f17a5b89c0a056`；Source checkout `822e58cc3d666166c7446c5b1ea2e52f5d09594c`；Windows 包 `26.1016.8622.0`、包内应用 `26.1016.62342`。checkout 与安装包不声称同一构建。
- 基线组身份来自 [issue-groups.json](C:/Desktop/MiniCode/output/full-parity-audit-20261023/issue-groups.json)、[findings.json](C:/Desktop/MiniCode/output/full-parity-audit-20261023/findings.json) 和 [REPORT.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/REPORT.md)。原“45 个静态组、18 个 UI 组”是实施前快照，不能作为当前剩余数，也不能用总数减本轮改动推算完成度。
- 后续明确新增的 Composer 三组、Scheduled 一组另列；不重算旧审计 JSON，不将扩展叶子、两条恢复风险或未核实项重复计组。
- **本轮有实现**：当前源码/diff 和交接能定位到相应调用链；该行同时写明实施范围及仍待 GUI、provider 或真实任务验证的边界。这个标签不表示整组所有分支已闭合。
- **本轮未改**：本轮没有修复该组所指的具体差异。相关文件可能因性能、设置 namespace 或别的功能被修改，不能因此抵扣此项；原有 partial 能力也没有被抹成“全部缺失”。
- **审计已撤销**：撤回具体误判；不伪装成一次功能修复。当前只有下文写明的子项纠正，没有据此整组撤销仍成立的差异。

观察范围和未看状态以 [checkpoint](C:/Desktop/MiniCode/docs/validation/2026-10-24-parity-checkpoint.md) 及原始 live-ui 记录为准。用户已要求停止继续 GUI，后续整理和实现没有继续 GUI 或调用真实模型。各分工的 authored/READY 记录与 Root 的统一验证分别记账，最终批次结果见末尾及 checkpoint，不用旧首轮状态代替当前结果。

## 静态协议与功能组

### Runtime、工具和角色

本节原始证据见 [runtime.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/runtime.md)；各行保留其 feature/model/role gate 与静态证据边界。

| issue_group / 原记录 | 本轮状态 | 具体范围与仍保留的差异 |
|---|---|---|
| `runtime.R08` / R08 | 本轮未改 | 未接入宿主 `thread/inject_items` 式的 trusted ResponseItems 输入、client-authored provenance 和无新 turn 持久化回路。已有 steer、内部 context 和导入新会话不替代该契约；没有实测故障结论。 |
| `runtime.R09` / R09 | 本轮有实现（继续批次） | 本地 compaction 取消对每个已接收工具参数/结果的额外 500-token 裁剪，保留现有历史 admission、用户约束、输出限制及 native/encrypted 分支；自动/手动真实 ContextBuilder 请求与冷恢复用例已补。真实压缩质量仍未测。见 compaction-goals-cleanup 交接。 |
| `runtime.T05` / T05 | 本轮有实现（继续批次） | 移除 child 身份对 update_plan 的硬 deny，前后台 child 按实际配置、父 deny 及 Plan mode 决定 schema/执行；保留其余 child 限制。受控实际工具事件用例已补，真实 provider child 任务仍未运行。 |
| `runtime.async-user-tools-gating` / T07、T08 | 本轮有实现（继续批次） | request 与 send 的模型/catalog/feature 曝光独立，保留 root-only 和 DirectModelOnly；schema、目录与执行 policy 共用同一选择。T07/T08 仍一组；直接 async UI/journal 的最终回归结果在下节单列，不将旧嵌套调用算等价。 |
| `runtime.T12` / T12 | 本轮有实现（继续批次） | CurrentTimeReminder 或模型 clock 均可曝光 curr_time，factory 与每 step 使用同一工具门控。此处不包含 RQ02 的外部时钟注入或整个自动时间提醒生命周期。 |
| `runtime.T13` / T13 | 本轮有实现（继续批次） | sleep 消费 Source sleep_tool、model_driven/always_on 和 reminder.sleep_tool；reminder 开启时不再用 clock 覆回明确禁用。最终 policy/执行 guard 拒绝被禁工具，显式 active tool selection 不能重新打开；保留原 sleep 的实际等待/取消行为。 |
| `runtime.T19` / T19 | 本轮未改 | 未补每 thread dynamic tool schema → host `item/tool/call` → 多模态回应的 callback/owner 链。进程内插件 callable 注册不是同一宿主协议。 |
| `runtime.M05` / M05 | 本轮有实现（继续批次） | 角色 memories=false 和 Source 明确支持的旧名 memory_tool=false 归一为 canonical 禁用，经过 project/inherit/cold resume 继续限制记忆工具；父配置与已存记忆保留。Memory V2 不在此范围。 |

### 集成、MCP、Hooks、环境和 Memory V2

原始范围见 [integrations.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/integrations.md)。这里列的是具体接口/可选分支，不是把现有 MCP、Hooks、环境执行或 V1 memory 整体判缺。

| issue_group / 原记录 | 本轮状态 | 具体范围与仍保留的差异 |
|---|---|---|
| `integrations.skills_dependency_originator` | 本轮有实现（继续批次） | 真实桌面 producer 明确携带 minicode-desktop；Skill MCP 依赖安装接纳 Mini 自己的 first-party 来源，SDK 来源保持自身语义。实际 approval、managed admission 和安装服务仍是原链；受控 run_agent_loop 夹具验证先安装再请求模型，未实际安装用户依赖。 |
| `integrations.hooks_interrupt` | 本轮有实现（继续批次） | 根线程的用户中断在 durable terminal/journal 提交后、公开 completion/DONE 前执行 Interrupt；默认 1 秒、上限 3 秒，失败不阻止中断。严格 systemMessage 与真实运行证据进入原表面，不能回灌上下文或 asyncRewake。既有 Mini 全局 hook 配置仍用 snake_case，整套 Source 配置名迁移未做。 |
| `integrations.hooks_mcp_executor` | 本轮有实现（继续批次） | mcp_tool 经过配置、递归 typed 模板、当前步已连接且启用的 MCP 工具执行；保留 threadId、权限 scope 和实际错误，禁止 SessionEnd MCP，不启动新 server。同步/取消/超时夹具已补，真实 MCP/远程位置仍未验。 |
| `integrations.mcp_modern_protocol` | 本轮未改 | 可选 MCP `2026-07-28` 生命周期模式缺口未补；SDK 安装/声明不作为项目已执行该模式的证据。 |
| `integrations.mcp_extended_forms` | 本轮未改 | `openai/form` 与扩展 form 的处理契约未补；未跑真实 server/form fixture。 |
| `integrations.environments_public_api` | 本轮未改 | 内部环境执行链仍存在，Source 同级外部注册/管理 API 的差异未改；本轮环境样式调整不抵扣此组。 |
| `integrations.environment_deferred_wait` | 本轮未改 | 模型主动等待延迟环境工具的分支未补，未启动延迟环境作动态验收。 |
| `integrations.memory_v2` | 本轮未改 | 可选 V2、dual-write/readiness 迁移链未补。主机记忆开关、生成默认值和 use policy 的实现属于另一组，不意味着 V2 已支持。 |
| `integrations.server_diagnostics` | 本轮未改 | Source 本地 `server/diagnostics` 指标接口差异未改；已有 Doctor、health 或通用诊断导出不等价。 |

### Desktop 与文件工作区基础契约

原始范围见 [desktop.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/desktop.md)、[workbench.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/workbench.md) 和 [runtime-followup.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/runtime-followup.md)。

| issue_group / 原记录 | 本轮状态 | 具体范围与仍保留的差异 |
|---|---|---|
| `desktop.D-SEARCH` / D-SEARCH | 本轮有实现（继续批次） | 已有全局/归档入口接 session-owned public-body 搜索，保留即时标题/项目匹配；真实游标、排序/过滤、50 条客户端批次、每 HTTP 最多 200 个聊天扫描和 cold zstd 单向首命中读取已实现。结果为明确 11 字段轻量摘要及 message/snippet，复用原定位路径；大型全库与 GUI 时延仍未验。 |
| `desktop.D-COMMANDS` / D-COMMANDS | 本轮未改 | Source 同级可配置命令/快捷键表面的其余已列差异未系统补齐。新增可见 View→Browser 层级另记，不能据此把整个命令表判闭合；注册数量差不当缺失数。 |
| `desktop.D-DEBUG` / D-DEBUG | 本轮有实现（继续批次） | 按已读 Source build-flavor 的内部 debug gate 为完整诊断使用 Vite DEV 门控；生产仅保留已聚焦且有 owner 的真实记录详情，不挂聚合诊断/控制平面。当前普通 launcher 本已是浏览器新标签，未伪称删除不存在的菜单；不新增 Developer 按钮。DEV=false 及真实失败记录夹具已补。 |
| `workbench.W-SEARCH-CANCEL` / W-SEARCH-CANCEL | 本轮未改 | 文件引用搜索仍缺后端取消与持续搜索会话契约；失效前端旧结果或新增树筛选不等于终止后台搜索。没有负载实测或卡顿量化结论。 |
| `workbench.W-FS-WATCH` / W-FS-WATCH | 本轮未改 | 现有 workspace watcher 保留，独立 watchId 订阅/停止的粒度与 app-server 契约差异未补；未做文件变动验收。 |
| `workbench.W-PROJECT-IDEMPOTENCY` / W-PROJECT-IDEMPOTENCY | 本轮未改 | 项目创建的逻辑幂等请求身份未补；目录归属冲突不等于同一 request 返回先前结果。未把静态差异写成当前桌面必然重复创建。 |
| `runtime-followup.RQ02` / RQ02 | 本轮未改 | 外部宿主可注入 clock 与后端系统时钟的条件性差异未改；未操作系统时钟或运行模拟 clock。 |

### Canonical flags 与 shell snapshot

原始范围见 [features-followup.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/features-followup.md)。本轮确实把配置层中的 `feature_flags` 旧表别名在逐层合并前归一到 `features`，并让受编辑项按 canonical namespace 写回；这是配置优先级修正，不是下面具体 feature 名称和消费 gate 已对齐的证明。

| issue_group / 原记录 | 本轮状态 | 具体范围与仍保留的差异 |
|---|---|---|
| `FF-SNAPSHOT` / FF-shell_snapshot、FF-shell_snapshot_v2 | 本轮未改 | 本地 shell snapshot capture/replay 的显式 unsupported 分支未改；远端 exec-server 与全部 V2 语义没有被执行验收。两条记录仍只计一组。 |
| `FF-CODE-MODE` / FF-code_mode、FF-code_mode_only | 本轮未改 | Canonical code-mode 开关对应的 fallback/控制差异未修；并未声称代码执行整体缺失或所有显式模型声明都会走该 fallback。 |
| `FF-MULTI-AGENT` / FF-multi_agent | 本轮有实现（继续批次） | canonical multi_agent 控制 Source fallback；显式模型版本、V2 与 agents.enabled 按 Source 优先级选择。静态 Task spec 不再固化构造时隐藏，实际执行仍读当前 step 捕获配置；同 registry off/on 和启动前拒绝夹具已补，保存的 child 工作记录保留。 |
| `FF-GOALS` / FF-goals | 本轮有实现（继续批次） | goals off 贯通 factory/step/schema/执行、目标 owner 的 WS/slash、自动 queue/admission、普通回合提示与计费；能力刷新从当前 scope 统一生成 flags/目录，不回写已运行快照。前端隐藏入口/bar/panel并保留目标与草稿，既存 goalMode 在 off 期间按普通输入消费；切 Plan 不再清 disabled goal。真实长任务/GUI仍未执行。 |

### Marketplace、审阅目标、导入及模型/权限接入

原始范围见 [client-followup.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/client-followup.md) 和 [account-followup.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/account-followup.md)。官方账号服务排除不扩张到自定义模型网关、MCP OAuth、命名权限或 AWS 本地接入。

| issue_group / 原记录 | 本轮状态 | 具体范围与仍保留的差异 |
|---|---|---|
| `client-followup.CF-MARKETPLACE-UPGRADE` | 本轮未改 | 市场升级未更新已装插件及其运行时的调用链未补。`backend/skills/marketplace.py` 本轮改的是安全卸载身份，不是 plugin marketplace upgrade。 |
| `client-followup.CF-MARKETPLACE-REMOVE-CACHE` | 本轮未改 | 移除市场来源后的物化缓存清理未补；已有 pre-uninstall 和本轮单 Skill 卸载不抵扣此项。未实际移除用户市场。 |
| `client-followup.CF-REVIEW-TARGETS` | 本轮未改 | 发起模型审阅时指定 commit/custom target 的入口与服务契约未补。Changes 为复制 patch 新增的 `commit_sha`/source 读取不等于模型 review target 已接线；未运行模型审阅。 |
| `client-followup.CF-IMPORT-HISTORY-CONTRACT` | 本轮未改 | 导入历史固定截断与独立结果记账差异未改；设置入口位置和普通文件工作区变化不覆盖导入 producer。未读取用户真实历史或假定已达到截断阈值。 |
| `account-followup.AF-PROVIDER-GATEWAY` | 本轮未改 | 自定义网关独立 OAuth 配置进入标准 provider 链的差异未改；将已有 Provider 页面分离不等于补认证配置。未登录、读凭据或发线上请求。 |
| `account-followup.AF-NAMED-PERMISSIONS` | 本轮未改 | 命名 permission profile 的解析、目录与选择链未补。已有内建权限模式继续 enforcement，不能推断已越权。 |
| `account-followup.AF-AWS-RECOVERY` | 本轮未改 | 已有后端 AWS 认证恢复保留；开始/完成通知的同等 producer 未补。未令凭据过期、刷新或声称看到了当前 Source 对应提示。 |

### Finalpass 与并发摘要

原始范围见 [runtime-finalpass.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/runtime-finalpass.md)、[features-finalpass.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/features-finalpass.md)、[integrations-finalpass.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/integrations-finalpass.md) 和 [remaining-runtime-resolution.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/remaining-runtime-resolution.md)。

| issue_group / 原记录 | 本轮状态 | 具体范围与仍保留的差异 |
|---|---|---|
| `runtime-finalpass.FP-F01` / FP-F01 | 本轮未改 | 实验 timeline 的 canonical rollout position、turn 边界和页首实时会话状态差异未修。既有 realtime item 已持久到 transcript，本轮性能/namespace 修改不能扩写成 timeline 完整支持或语音完全无持久化。 |
| `runtime-finalpass.FP-F02` / FP-F02 | 本轮未改 | Responses 模型元数据形成同等内建客户端投影的条件性差异未修；并发摘要 wire 字段属于下行 RR-F01。 |
| `features-finalpass.FP-CODE-MODE-LIFECYCLE` | 本轮未改 | cell 预热与中断控制分离未补；能力快照性能优化不改变 lifecycle 契约。 |
| `features-finalpass.FP-IMAGE-PREPARATION` | 本轮未改 | 模型条件图片预算/尺寸通知未接入；独立文件查看器和普通图像上传不等价。 |
| `features-finalpass.FP-PATCH-STREAMING` | 本轮未改 | patch 参数流的增量预览分支未补；最终 patch 比较、复制 git apply 不等价。 |
| `IF-MCP-NAMESPACE` / IF-non_prefixed_mcp_tool_names | 本轮未改 | 可选无前缀 MCP namespace producer 未补。 |
| `IF-PLUGIN-INSTALL-TOOLS` / IF-tool_suggest | 本轮未改 | 模型可调用的本地插件发现/安装请求工具未补；现有 UI 安装或 Skill 卸载不等价。 |
| `IF-WORKSPACE-DEPENDENCIES` / IF-workspace_dependencies | 本轮未改 | model-facing discovery 未接线，配置页所需 bundle enable/diagnose/reinstall/version producer 也未找到。构建时 prepare、环境布尔值、普通 Python 版本及通用 diagnostics 不作为替代；没有新增空控件。 |
| `IF-FEEDBACK-FLOW` / IF-feedback-upload | 本轮未改 | 用户反馈入口及提交链未补；没有把通用 health/诊断导出算成反馈上传。 |
| `remaining-runtime-resolution.RR-F01` / RR-F01（实现交接 RR02） | 本轮有实现 | [loop_bootstrap](C:/Desktop/MiniCode/backend/agent/loop_bootstrap.py) → provider metadata → [OpenAI adapter](C:/Desktop/MiniCode/backend/llm/openai_adapter.py) 接入 opt-in `reasoning_summary_delivery: sequential_cutoff`；抑制中间摘要事件，投射当前 item 的最终 text，保留传输活动和已有持久化链。专项受控验证已有记录。真实服务接受度、跨 item 交错边界和当前 GUI 时序仍未验；默认关闭/custom provider 的原 delta 路径保留。 |

RR-F01 的实施与验证证据见 [runtime-implementation-verification.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/runtime-implementation-verification.md) 及同名 JSON；不能继续把它记成“本轮未改”，也不能凭受控 transport 测试宣称真实 provider 已接受。

## 当前 UI 与控件组

以下保留原 18 个 group 的身份。第一批和第二批代码已写入共享工作树；“仍待 GUI”指相应新实现/全部状态尚未完成当前验收，不否认 checkpoint 中写明的实施前实看或其他独立性能观察。

| issue_group / 原记录 | 本轮状态 | 具体实施、证据与仍待项 |
|---|---|---|
| `ui-controls-review.UC-GENERAL-EXTRA-CONTROLS` | 本轮有实现；Reset 子项审计已撤销 | [GeneralTab](C:/Desktop/MiniCode/frontend/src.v2/overlays/GeneralTab.tsx) 移除普通常规页的默认终端位置、高级新任务默认权限、桌面更新三类控件；底层现有偏好不被清空。目录 Reset 经 Source 条件复核后保留，不是移除了四类。仍待新常规页 GUI 核对。 |
| `ui-controls-review.UC-GENERAL-MISSING-CONTROLS` | 本轮有实现 | 常规页、原生 [preferences](C:/Desktop/MiniCode/desktop/preferences.js)/IPC 已接默认 full-view、Popout 快捷键/独立聊天、confetti。新任务初始化和首次 Files/Browser 选择消费 full-view；[popout-window](C:/Desktop/MiniCode/desktop/popout-window.js) 管理注册/切换/还原；同一 PR 身份真实 OPEN→MERGED 才触发动画，开 Toys 不触发。菜单 workingDirectory 发布订阅和原生打包文件已补。仍待真实全局快捷键、恢复/多窗口及动画 GUI。Dot 类别仍未核实，不伪造线上能力。 |
| `ui-controls-review.UC-CONNECTIONS-NAV` | 本轮有实现 | [SettingsCenter](C:/Desktop/MiniCode/frontend/src.v2/overlays/SettingsCenter.tsx) 去掉无条件隐藏 connections，复用真实 ConnectionsTab/desktop API。未用注册组件冒充已连接；新导航 GUI 和所有连接状态仍待验。 |
| `ui-controls-review.UC-APP-SNAPSHOTS-NAV`（含 SD-APPSHOT-CONTROLS 扩展） | 本轮未改 | 应用快照设置/路由/目标 capture 生产消费链未补；composer 的 Attach Electron 只是应用名对应的同一 Appshot 叶子。没有新建空页或按钮来假闭合，具体服务 gate/全部控件仍保留原边界。 |
| `UI-INDEXED-LABEL` / UC-INDEXED-LABEL | 本轮有实现 | [ConfigTab](C:/Desktop/MiniCode/frontend/src.v2/overlays/ConfigTab.tsx) 中文改为实看标签“索引”，英文及 wire `indexed` 保留；原有 TOML/run snapshot/Responses 链未被当作缺失重写。仍未新增真实 provider acceptance 验收。 |
| `UI-CONFIG-WARNING-SURFACE` / UC-CONFIG-WARNINGS | 本轮有实现 | ConfigLayerStack 的真实 startup warnings → [agent-config GET/PUT](C:/Desktop/MiniCode/backend/services/agent_config_settings_service.py) → 配置页 alert；保留实际文字和路径，不暴露配置值或编造警告。仍待当前 GUI、不同 scope 的完整视觉状态。此组不与聊天 notice/FP15 重复。 |
| `UI-CONFIG-SURFACE` / UC-CONFIG-SURFACE | 本轮有实现（部分范围） | 普通 Config 不自动展示/激活 Provider 和 internal feature groups；已有 Footer、听写/搜索进入独立“模型与 API”页，自定义接口和草稿保留，没有新增 Provider 主导航。按真实模型能力显示 Max/Ultra 可见性，原生 `enabledReasoningEfforts` 传到 composer，隐藏当前档位不自动改值。workspace bundle/Beta 等无 producer 范围仍未补；选中 Max 再隐藏的 Source GUI 未实看，不能声称交互完全等价。详见 [配置交接](C:/Desktop/MiniCode/output/full-parity-audit-20261023/config-implementation-handoff.md)。 |
| `UI-HOST-MEMORY-PREFERENCES` / SD-MEMORY-HOST-PREFERENCES | 本轮有实现 | [PersonalizationTab](C:/Desktop/MiniCode/frontend/src.v2/overlays/PersonalizationTab.tsx)、窄主进程转发和 [memory settings service](C:/Desktop/MiniCode/backend/services/memory_settings_service.py) 实现已连接 host 选择、主机 memory feature/生成/使用及外部上下文 policy，指令草稿按 host 保留；新 run 捕获策略，生成默认值用于新 thread，旧任务模式/已有记忆不被偏好写入覆盖。配置别名逐层归一化已落地。仍待跨 host GUI 和真实新 run 行为；role `memories=false` 与 V2 另见 M05/memory_v2，未闭合。详见 [host memory 交接](C:/Desktop/MiniCode/docs/validation/2026-10-24-memory-host-preferences.md)。 |
| `UI-PET-CUSTOMIZATION` / SD-PET-CUSTOMIZATION | 本轮有实现 | [SourcePetsTab](C:/Desktop/MiniCode/frontend/src.v2/overlays/SourcePetsTab.tsx) 改为选中宠物大预览、响应式卡片和 Customize/size/Reset 对话框；复用真实持久 API。没有把 Source 创建时失败以后的流程编造成已观察状态；新布局、缩放和创建分支仍待对应 GUI/服务验收。 |
| `UI-PET-BAR-ONLY` / SD-PET-BAR-ONLY | 本轮有实现 | `selectedPetId:null` 可独立于可见性保存，pet manager/window 支持 None 横条模式；隐藏宠物不被当作 None。仍待真实原生窗、重启、聚焦/大小的完整 GUI 状态。 |
| `UI-SKILL-UNINSTALL-ENTRY` / SD-SKILL-UNINSTALL-ENTRY | 本轮有实现 | 两个 Skill 管理入口/详情接到既有 DELETE，后端提供真实 `uninstall_name`，卸载仅向 Mini 管理的可卸载身份开放并刷新目录/可见行；插件 Skill 仍由所属插件管理。未实际卸载用户 Skill；正常依赖安装、市场升级和来源缓存不是本项范围。 |
| `UI-COMPUTER-CONTROL-ROWS` / SD-COMPUTER-CONTROL-ROWS | 本轮未改 | Chrome/Edge/Excel 控制行的 settings producer/renderer contract、管理及引用链未补；已有 browser navigation、电脑操控状态或 generic diagnostic 不能替代。浏览器 tab intake 另有独立 group，见下表。 |
| `UI-SSH-COLOR` / SD-SSH-COLOR | 本轮有实现 | [ConnectionsTab](C:/Desktop/MiniCode/frontend/src.v2/overlays/ConnectionsTab.tsx) 与 native remote-host/desktop 保存、列表和详情接入实际 color 字段；不虚构连接成功。仍待真实 SSH/WSL 各状态、跨窗口和重启视觉验收。 |
| `ui-workbench-deep-review.UW-FILE-WORKSPACE` | 本轮有实现（原链范围） | [file-workspace owner](C:/Desktop/MiniCode/frontend/src.v2/protocol/file-workspace.ts)、[FileViewerPanel](C:/Desktop/MiniCode/frontend/src.v2/panels/FileViewerPanel.tsx)、后端文件读取/比较串起 Home、Files 树、根目录/筛选/All changes、独立文件标签、查看器/比较/打开选项及置顶/左右移动。保留执行根、浏览根、scope/host/conversation 身份，编辑器仍为独立路径。所有媒体、大文件/二进制、远程/多根、失败/重载及完整拖动组合仍待各自 GUI/真实任务验证。 |
| `ui-workbench-deep-review.UW-COPY-GIT-APPLY` | 本轮有实现 | Changes toolbar → owner/source/base/commit 捕获 → `diff.git_review_patch` → [review_patch](C:/Desktop/MiniCode/backend/diff/review_patch.py) → [复制命令](C:/Desktop/MiniCode/frontend/src.v2/chat/diff/copy-git-apply-command.ts)；包含 binary patch 和 repo-relative 路径，生成真实 `git apply --3way`。仍待当前 GUI/剪贴板完整组合；不据此声称模型 commit/custom review target 已实现。 |
| `ui-workbench-deep-review.UW-SUMMARY-SURFACE` | 本轮有实现 | [ChatContextCard](C:/Desktop/MiniCode/frontend/src.v2/chat/ChatContextCard.tsx)、[thread-summary-layout](C:/Desktop/MiniCode/frontend/src.v2/chat/thread-summary-layout.ts) 和主槽接入独立 pin/popover/shift/gutter，右栏打开不再移除摘要；阅读/布局状态沿现有链保留。当前所有宽度、同时打开面板和 focus/动画仍需对应 GUI 核对。 |
| `UI-GLOBAL-THEME-DERIVED-SURFACES` / SC-GLOBAL-THEME-SURFACES | 本轮有实现 | [appearance-colors](C:/Desktop/MiniCode/frontend/src.v2/lib/appearance-colors.ts) 修正公共派生 soft surface，Browser Tools、Git pill 等共用叶子受益，仍只是一组；用户所选字体/缩放不重置。所有主题/高对比/disabled 状态未因此自动通过。 |
| `UI-BROWSER-NEW-TAB-TOOLS` / SC-BROWSER-NEW-TAB-TOOLS | 本轮有实现 | [BrowserPanel](C:/Desktop/MiniCode/frontend/src.v2/panels/BrowserPanel.tsx) 接入顶部 action cards、图标和快捷键内容，沿真实 browser actions 执行；公共底色归上组，不重复计。新页全部窗口尺寸、键盘/focus 和真实导航/失败状态仍待验证。 |

本节原始 UI 证据分别见 [控件审阅](C:/Desktop/MiniCode/output/full-parity-audit-20261023/ui-controls-review.md)、[配置审阅](C:/Desktop/MiniCode/output/full-parity-audit-20261023/ui-config-review.md)、[设置深入审阅](C:/Desktop/MiniCode/output/full-parity-audit-20261023/ui-settings-deep-review.md)、[工作区深入审阅](C:/Desktop/MiniCode/output/full-parity-audit-20261023/ui-workbench-deep-review.md)、[样式因果链](C:/Desktop/MiniCode/output/full-parity-audit-20261023/ui-style-chain-review.md)。原 finding 表里的旧“四类”标题需按下一节纠正，不能沿用为当前判断。

## 后续独立 group 与已纠正子项

这些组由后续专项明确记录，尚未重新写入旧 issue-groups/findings 聚合。分别链接原报告，既不漏列，也不把它们包装成旧“63 组还剩多少”。

| group | 本轮状态 | 范围与边界 |
|---|---|---|
| `UI-COMPOSER-TAB-CONTEXT` | 本轮未改 | Source 真实 Tabs 候选搜索与 dedicated browser-tab identity/snapshot node 的 composer intake 未补；普通 URL/导航/标注不能替代。见 [composer review](C:/Desktop/MiniCode/output/full-parity-audit-20261023/ui-composer-menu-review.md)。 |
| `UI-COMPOSER-SKETCH` | 本轮未改 | Sketch menu → canvas/Host → 保存/重开 `Sketch.png` attachment producer 未补；普通图片上传已存在，未被判缺。见同一 composer review。 |
| `UI-VIEW-BROWSER-SUBMENU` | 本轮有实现 | [desktop/main.js](C:/Desktop/MiniCode/desktop/main.js) 增加可见 View→Browser 子菜单，复用已有 owner/capability/renderer/native page 分发；reload 保留实际 page availability。其余 D-COMMANDS 与完整 focus/history/失败恢复仍未因此闭合，当前 GUI 未穷尽。 |
| `UI-SCHEDULER-ENTRY-SURFACE` | 本轮有实现 | 独立 [ScheduledTasksPage](C:/Desktop/MiniCode/frontend/src.v2/overlays/ScheduledTasksPage.tsx)/侧栏/新建弹窗接既有本地 cron/heartbeat；复用频率和高级编辑，保留关闭草稿/隐藏工作区；单次 typed global list 聚合真实任务，捕获 task ID/storage root；新 heartbeat 通过 `activate:false` 创建专用聊天并复用失败重试身份。没有执行 prompt；真实计时触发、重启恢复、结果/通知及各 kind 的 Source 对照仍待验。官方 cloud/Site/线上模型目录单独排除。见 [专项审阅](C:/Desktop/MiniCode/output/full-parity-audit-20261023/ui-scheduler-review.md) 和 [实施交接](C:/Desktop/MiniCode/output/full-parity-audit-20261023/scheduler-implementation-handoff.md)。 |
| `UI-EXTRA-TASK-DIRECTORY-RESET`（UC-GENERAL-EXTRA-CONTROLS 子项） | 审计已撤销 | Source `general-settings-27118198cef3.js:245–246` 实际按 override 非 null 显示 Reset，写 null 恢复默认；Mini 同样按 `taskDirectoryOverride` 门控。保留原有按钮。仅撤该子项，另外三类差异仍按上表实施；未执行真实用户目录变更。 |

其他已排除误判继续保留：模型菜单数量来自 profile，不是缺模型；Attach Electron 中 Electron 是 Appshot 目标名；Windows 的 files/folders 标签不能证明缺隐式 folder picker；Git 选中样式/公共 CSS 已存在，底色归全局主题一组；不同字体/zoom/DPI 不能直接当像素缺陷。这些不是新实现，也不生成新的“已关闭组”。

常规页导入入口位置等非独立 finding 叶子仍以原控件审阅为准；本轮没有因配置或独立 Provider 页面调整就把历史导入契约、Appshots 或全部菜单验收算通过。

## 两条恢复风险：由受控复现转为已有修复，不新增 Source finding

原 [runtime-risk-coverage.json](C:/Desktop/MiniCode/output/full-parity-audit-20261023/runtime-risk-coverage.json) 把 UW-R1/UW-R2 记为静态待验风险，明确 `finding:false`。本轮随后补受控 fixture，先复现再修复，专项验证见 [运行时实施记录](C:/Desktop/MiniCode/output/full-parity-audit-20261023/runtime-implementation-verification.md)；旧 JSON 的 unverified 是历史证据层，不代表当前没有实施，也不能倒写成早先已有真实用户故障。

| 风险 | 本轮状态 | 具体实现与仍待项 |
|---|---|---|
| `UW-R1` / UW18 | 本轮有实现，已有专项验证 | 当前 restore/sync 被拒绝时通过既有清流链把本地未确认 streaming 结束为可恢复 partial；保留原消息、cursor、草稿/附件/队列，后续权威结果可替换。不虚构后端已完成或已失败。真实断网/后台存活、多窗口及 provider 长任务仍未因此全部验收。 |
| `UW-R2` / UW19 | 本轮有实现，已有专项验证 | 父任务仍 live 时独立水合 durable child；保留父计划/过程、同 incarnation 的名称/记录/序号，按既有 fence/epoch 接收新 incarnation 并拒绝旧状态回退。真实 child 在 replay 窗口外完成、长任务并发和 GUI 时序仍需另验。 |

runtime 后续 steer 回归修正的是测试直接修改 `messages` 却未同步 owner cache 的 fixture；使用 `hydrateConversationMessages` 后按真实双投影契约验证。没有为通过测试改写生产恢复语义，记录见 [runtime-steer-replay-regression.md](C:/Desktop/MiniCode/output/full-parity-audit-20261023/runtime-steer-replay-regression.md)。

## 未核实、历史验收与后续补记

- `desktop.D-TEMPORARY`、`desktop.D-VISUAL` 在旧 group 表中为 `in_scope:false / unverified`，不是上表已确认差异。临时聊天 eligibility/gate 和全版本多级视觉没有整体验收；局部后续 GUI 观察不能升级它们为全量 matched。
- 基线 coverage 的 active unverified、scoped partial/on-off/平台/失败/取消等分支没有因本轮代码而全改状态；没有重算整份审计 JSON。精确未看范围仍见 checkpoint 和相应专项。
- [interaction-performance.md](C:/Desktop/MiniCode/docs/validation/2026-10-24-interaction-performance.md) 独立记录性能改动与其实际观察范围。窄范围性能结果、静态按钮清点和旧 dist 不能抵扣本表全部新增 GUI、provider 或端到端验收；该文保留的峰值、脚本失败和未覆盖状态不被抹掉。
- 本轮统一验证结束后，由 Root 在此补最终命令/日志、受影响回归结果、构建与必要真实任务边界。失败、跳过和未执行分别记录；不把不同批次或重跑数量相加为覆盖率。

### Root 最终验证补记

前两批所列首轮失败已完成定位和定向回归：前端相关 14 文件 438 passed，后端 phase1 WebSocket 10 passed，原生清理 2 文件 19 passed；追加的能力消费者/路径/job store 12 文件 121 passed、23 skipped。后者跳过为 Windows symlink 创建权限分支，不计为通过。首轮前端 466/3、后端 471/1、Node 107/3 的日志及原失败复现均保留，重叠用例没有累加为完成度。

严格 TypeScript 和相对路径桌面生产构建通过；首次 UI debt 检查的两处 Scheduled 硬编码动效已改用现有 token，增量打包后 bundle/UI debt 均通过，git diff --check 通过。入口 JS 914147 bytes；main 的本地 require 发布文件表静态核对无遗漏。检查没有启动或重启用户窗口，也没有通过用户实际配置或真实模型请求做变更验收。

精确日志、失败原因及夹具修正见 [checkpoint 的第二批回归与最终构建](C:/Desktop/MiniCode/docs/validation/2026-10-24-parity-checkpoint.md)。所有“本轮未改”和各行未验证分支继续保留；未运行全仓测试、修改后完整 GUI、真实 provider 长任务或 GitHub CI。

### 继续批次：工具门控、Hooks、压缩和正文搜索

13 个原有组的本次实施范围已逐行更新，不将“本轮有实现”自动标为所有状态闭合。交接分见 [Runtime gates](C:/Desktop/MiniCode/docs/validation/2026-10-24-runtime-gates.md)、[Hooks/Skill](C:/Desktop/MiniCode/docs/validation/2026-10-24-hooks-skills-parity.md)、[搜索/调试](C:/Desktop/MiniCode/docs/validation/2026-10-24-desktop-search-debug.md) 和 [压缩/Goal/清理](C:/Desktop/MiniCode/docs/validation/2026-10-24-compaction-goals-cleanup.md)。

整批首轮为 core 441 passed/38 failed、integrations 201 passed/1 failed、前端 382 passed/1 failed；原日志未覆盖。修正构造和显式 gate fixture 后，Runtime 四文件为 112 passed/1 failed；最后 direct async 失败进一步复现出真实 journal 缺口，沿同一桥修复并保留 UI/持久化断言，相关 QueryEngine/async/protocol 七文件最终 112 passed。搜索轻量返回契约六项、表式 feature override 十三项分别通过。

前端保留原“暂不可用”状态，同时按实际 feature 控制 Goal 行是否显示；对应七文件重跑为 207 passed/1 failed，后者是重新启用后编辑器自动聚焦关闭菜单的 fixture 顺序，重新打开菜单保留全部断言后十四项通过。最后搜索本地错误文案复用已有翻译键，hook 五项通过。重叠数量不累加为覆盖率。

最终 `npm run build` 含 TypeScript、相对路径产物生成均通过；bundle/UI debt、git diff --check 通过，入口 JS 914824 bytes。精确日志和最终验证边界已追加 checkpoint。清理请求被自动审批两次拒绝，所选旧构建/缓存未删除；这是未完成项，不写成已释放空间。未继续 GUI、重启用户窗口、运行真实模型或 GitHub CI。
