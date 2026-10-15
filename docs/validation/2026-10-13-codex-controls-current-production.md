# 本轮控件、harness 和当前生产证据

本轮前端使用此前实际 Codex 桌面原图和安装版 `26.1007.2314` 的只读 ASAR，对照 MiniCode 当前构建；后端参考 `.tmp/codex-src` `822e58cc`。没有修改字体、字号或文字风格。这里的通过范围仅为明确列出的调用和状态，不代表全部 1063 个静态控件节点、所有设置页或公网服务已经验证。

本次继续工作的最新统一构建为 `frontend-build-run14`，TypeScript/Vite均成功。新增的模型/权限菜单、Markdown审阅和shell/login/tty已接通真实前后端；Source没有的分支统计/预算调度通知不铺到UI，真实事实保留记录。现有bundle与动态import警告保留，未称零构建警告。正常用户旧UI/Python进程没有被强制重载，生成资产与正常已运行进程的版本不能混为一谈。

| 追加批次 | 完成范围与真实证据 |
| --- | --- |
| 模型、强度、权限 | 后端55、前端152项相关范围通过；Source支持档位保留，否则模型自身默认，reset原子恢复项目配置model+effort+selection mode，owner/workspace与冷恢复贯通；configured/extension真实catalog消费已修。9个桌面场景/24图的证据和失败轮见`2026-10-13-codex-model-permission-controls.md`，不代表Source推荐preset/Guardian/custom profile已经实现。 |
| shell/login/tty与实际取消 | Python145项去重范围及4项Node打包契约通过，14个新增真实shell/PTY用例全部通过。真实中文stdin、原字节输出、owner、timeout/cancel、父子进程退出事实贯通；修了taskkill成功但pwsh仍活着的真实回收问题。Source ABI/离线依赖/fixture失败均保留，见`2026-10-13-codex-tool-approval-source-audit.md`。 |
| Native生产资产 | 正式`desktop/windows-sandbox-runtime`无MiniCode进程占用后，默认prepare增量4.57秒成功；新input SHA `ab28aabb…`、3枚exe hashes/许可证/版本contract均通过，已在正式生产加载路径。没有仅靠QA env冒充激活，也没有强制关闭正常用户进程。 |
| shell投影与patch | 实际shell选型驱动读取/搜索/原命令投影，一个文件66项通过；cmd不被误判为PowerShell读取。patch不强制typed先读，实际审批diff snapshot不覆盖审批期间用户改动；原69项范围+15项冻结授权通过。两份对应报告明确重叠和边界。 |
| Markdown inline review | 135个前端与29个后端相关范围最终通过；3个完整桌面场景、4PNG、真实Git/merge-base/WS/CodeMode文件读取/模型批注/冷恢复通过。仅模型HTTP受控，用户原草稿、附件、quote、skill保留。指令与真实请求同一metadata贯通，Root持久Plan不被review改写。见`2026-10-13-codex-markdown-review-inline.md`。 |
| 最后视觉与预算投影 | Source批注`font-sans`只作用新卡片；build13一个已有场景字体补拍/2PNG通过，UI字体与body一致、代码字体原样。新增实机发现的非Source预算调度toast已移除，真实warning/用量/Inspector/实际压缩保持，两文件94项范围通过，有旧重复不累加。 |

追加实机失败如实保留：模型run01为Windows大小写路径fixture假设；run02发现真实configured目录被前端缩成current，修生产consumer而非改成fake live。Review run01为真实slash菜单重新打开并遮挡子菜单，修菜单生命周期而非z-index/force click；run02前两场景过，cold测试错误要求自动开栏，改为“打开右侧栏→添加面板→变更”的真实用户操作，run03全部过。模型/权限和完整review最终pageErrors/renderer warnings均空，native窗口均不可见；不把此前control-run18的资源404抹去。

最终模型补拍 `production-model-permission-run04/model-defaults` 在build14的6场景全部通过，19新PNG没有自写预算调度toast，完整wire仍有4个真实budget.warning；仅增加真实UI不存在该toast的断言，没有把事件或fixture改成成功。Ultra3场景沿已过run02原证据，最终仍9个业务场景，不把重跑叠成15个。Review完整3场景沿run03，字体补证沿visual-run01，运行范围分别记录。对照站点更新这些新图、实际Source参考和报告，HTTP已核对；原图及失败轮保留。

## 已补齐的真实差异

- 内置提问使用官方 `request_user_input` 多题 schema 与原答案映射，root/模式/readonly/直接模型控制工具按 Source 注册。Default/Plan 与 permissionPlan 分离，原模式指令放在 developer，问题不再套 MCP 工具审批或把答案 JSON 包成命令输出 XML。
- 新输入面板逐题显示选项说明、自由文本、Skip 和取消；选择后的 180ms 推进、300ms 题目过渡及减少动效来自实际桌面调用。Skip 提交空答案；Esc 沿真实 WS/SessionRunManager 中断当前 turn。原输入、引用和附件保留。
- 计划实施从底部请求面板发送一次 `PLEASE IMPLEMENT THIS PLAN:\n{plan}`，不额外引用或重复计划。反馈仅发自由文本；Skip 恢复输入；Esc 等待 mode RPC 的实际 ACK。回复中的计划组件只负责呈现。
- MCP URL 从官方 SDK 到 producer/pending/replay/WS/UI 保留 URL、elicitationId、meta 与完整 owner。先真正打开链接，再手动 Continue；decline/cancel 不变成 accept、不带 content，server complete 不能自动批准。
- MCP 工具白名单、审批或并行策略更新只改变 filtered catalog 和版本，保持原 transport 与在途调用；原始库存支持重新启用。连接参数变化和整 server 停用继续关闭连接。
- 不同 turn 允许复用相同 model call ID。前端新 canonical input 草稿和迟到 ACK 现按 conversation/turn/message/task 匹配；已有 legacy 审批草稿 key 不变，不迁移或清空旧草稿。
- 命令展开按真正 `sites-end-resource::aC → user-message-attachments::un/vn/yn` 对照：分别复制原命令和原输出、长命令两行后手动展开、9rem 尾部滚动，实际 Success/Stopped/Exit code 在详情底部。折叠行不加失败徽章或红色，live 空输出不造额外运行状态。
- 头像从实际 `app-initial` 导出 `KOc as gg` 找到原 28 组深浅色 SVG 和 `qOc` UTF-16 rolling31 算法；替换六组自绘/FNV。导出名与另一个模块 import 局部名 `x2` 区分，未采用错误匹配。
- `response.output_item.done` 的完整 assistant item 才确定邮件边界，真实 tool receipt 重新开启同一 turn；直接 kernel 构造也把唯一输入队列交给 RunContext/mailbox 消费者。
- 实际生产样式诊断发现全局 `:focus-visible` 覆盖组件并把请求面板圆角改成 2px。修正 reset 为低优先级 `:where(:focus-visible)`，移除全局强制圆角；真实计算样式已恢复 24px 和 outline none，字体和字号原样保留。
- 真正 `sites-end-resource::Ax` 的工作对话回复行只保留通用 Copy/Fork 和有真实内容的扩展操作。工作模式移除额外 quote/regenerate 按钮，独立旧聊天模式保留原行为。复制成功显示实际 Check；Fork 在真实语义结果之前禁用并显示图标，transport ACK 不冒充完成。
- Fork 之前仅发布 context_forked 或 context_error，没有完整 command.result；现补真实 success/error 回执、父 owner 与已创建分支/激活回滚事实。生产进一步捕获后端已激活而前端没有 canonical conversation.switched 的 owner 分裂，已补正式切换 payload、hydration 和前台导航 receipt；延迟 fork 不抢更晚选择，原分支创建事实保留。
- 分支的统计通知不属于 Source 聊天呈现。真实 `context_forked` 仍进入 Inspector 原始记录，主消息区不再插入分支 id、模型历史数量和估算 tokens。正式导航继续只由 canonical switched 驱动。
- 安装版真正 Open in 位于 conversation overflow，不新增顶层 HeaderBar 控件。安装发现、真实图标、Work mode 的类型过滤、workspace global/perPath 首选、file 一次性 target 贯穿 native 服务；原 MiniCode 内部默认保留。未安装或本版没有 Windows entry 的品牌不虚构支持。
- Skill MCP auto/Never 安装策略读取实际 turn 权限和已编译沙箱能力，安装前后使用现有 managed policy。删除 build/plan 重复的自写 user 模式段，保留官方 Default/Plan developer 原文；不以 prompt 替代 Source ReviewTask。

## 证据与明确边界

| 批次 | 真实范围 / 最终结果 | 记录 |
| --- | --- | --- |
| root harness | 原75项范围最终通过；direct queue owner16项通过，有重叠不相加 | `codex-control-audit-20261013/root-harness-batch-run*`、`root-kernel-owner-run01` |
| 主聊天错误/启动事实 | 3文件59项范围最终通过；缺真实startedAt的fixture已修，失败原记录保留 | `root-chat-batch-run01/02` |
| 命令字节、独立复制、原命令和中性退出详情 | 4文件37项通过 | `root-command-batch-run01.xml/log` |
| 头像、主行/列表真实identity稳定 | 4文件90项范围最终通过；原fixture把双主题图片当两个头像，改验证单一头像容器后只重验该文件 | `root-avatar-batch-run01/02` |
| canonical input/MCP URL/UI owner/协议/普通审批/Composer | 7文件218项范围最终通过；唯一旧quote空格fixture修后定向重验1项，过滤的33项不算新增通过 | `root-control-batch-run01/02` |
| Plan 底部发送与真实语义准入 | 独立10项通过，真实 sendChatMessage/ws-outbox/state，原输出由执行agent返回本会话；3个纯计划呈现用例通过 | `frontend/src.v2/chat/PlanImplementationPrompt.test.tsx`、`root-plan-answer-run01` |
| 后端规范输入和Skill问题 | 已保存 XML 去重后159项通过、0失败/跳过；不把旧被参数化替代的案例或重跑相加 | `codex-rebuild-20261012/backend-control-audit/request-user-input-run02–08` |
| MCP URL 与审批现有边界 | 29项通过，官方 SDK 类型/实际 bootstrap/WS owner 和 reply；没有公网 OAuth | `mcp-url-owner-chain-final2` |
| MCP policy/catalog/transport | 40项通过，其中3个新回归；与URL批次有重叠不相加 | `mcp-policy-catalog-chain`；原 before=`mcp-policy-reload-before.json` |
| Skill安装权限与模式角色 | 追加小批58项范围最终通过；真实临时配置写入/备份、managed等待后变更、真实loop先编译权限后激活Skill | `backend-control-audit/skill-dependency-policy-run01/02` |
| Fork语义准入与持久恢复 | 追加小批40项范围最终通过，不与前述重跑相加；创建失败、部分成功、回滚/registry/list警告保留真实事实 | `backend-control-audit/context-fork-semantic-run01–03` |
| 回复行与等待/拒绝重试 | 4文件52项通过；legacy聊天原引用/重试链保留，Source工作行仅Copy/Fork | `root-reply-actions-run01` |
| Fork前端导航 | 2文件13项通过，有4项回复行重复；正式switched激活、父草稿保留、晚到fork不抢更晚选择、后台fork仅list | `root-fork-navigation-run01` |
| Native Open producer/consumer | 前端/桌面受影响批次通过，重叠不累加；真实无窗口本机发现五目标与16px图标，外部OS启动未执行 | `2026-10-13-codex-open-targets.md`、`open-targets-native-discovery.json` |
| 最新 production 控制与上下文全链 | 1个真实桌面端到端测试、11个场景通过；提问、Esc、Plan、Copy、Open in、Fork、sidechat、context tools、手动压缩和Header归档Home；模型回复受控，系统剪贴板/外部应用仅拦截目的地 | `production-control-run18/verdict.json`、13张PNG及原 wire/provider 请求 |
| 最新 production 主/子过程 | 1个端到端测试、5个场景通过；真实 VM、文件、命令、exit7、编辑diff、TaskTool、child、重启和journal，无虚构消息/tool事件 | `production-projection-run02/code-projection-evidence.json`、12张PNG |

此前控制全链 production build 为 `frontend-build-run10`，Vite 成功，包含移除分支统计通知的实际生产修改；上一批 `frontend-build-run09` 的 TypeScript/Vite 均成功，包括真实 Fork、更多菜单、侧聊、富文本复制、Source glyph、ContextCompaction 与归档 Home。run05 的本机 npm wrapper 未启动构建；run06 使用已安装 CLI 后捕获 Markdown 的 context-ref union 与新增压缩 item 穷举错误，按真实 Source/引用 kind 修复。run07–10 后续构建仅因真实生产发现再修改空摘要/导航或分支事件消费者而执行，没有重装依赖或清空 dist 旧资源。前期 run01–04 原记录保留；现有 bundle 分块警告未冒称修好，也未为此重复构建。

生产控制 run01 的 offscreen fixture 曾用 subclass，导致 Electron 按原生构造类型枚举窗口时找不到 QA 窗口；改成保留原生窗口类型的 constructor proxy。run02 错把 provider 的 environment 包络当纯用户正文，run03 错把明确的 `quoted_message:null` 当 omitted；原失败保存，改按真实 sent command 和 provider envelope 分别验证，run04通过。run05是在全局焦点样式修复后验证新构建，并等待有限过渡完成再截图。没有从失败轮推导完整通过。

QA 仅创建独立临时 profile/workspace。原桌面、IPC、sidecar 和 renderer 使用生产代码，测试入口只令该测试进程的窗口 offscreen、拦截可见 show/focus。`verdict.native.visible=[false]`；未操作正常用户窗口，未覆盖未保存文档或用户历史。

追加 production-control run06 的目录打开被真实 native trust 拒绝：fixture位于受保护的`C:/Windows/Temp`，不能把只写信任账本算成获得native权限。改用独立`C:/Temp`受信任QA目录，不放宽生产guard。run07实际Open菜单/IPC/perPath通过，随后捕获真实Fork owner分裂（context_forked与success收到但无switched）；该失败保留，未计为完整通过。新测试profile关闭question/completion通知，外部应用真实launch仅拦截destination，不替换实际menu/preload/IPC/发现和首选存储。

追加批次已落地：

- Source `TBr/OBr/FBr/JBr` 回复纯文本/HTML、原生选区复制、`jGi` 真正写入等待/两秒 Check，以及实际 Copy/Check/Fork paths；12组 Source 独立执行 fixture。原165项范围最终通过，加两项实际异步/计时行为，不与重跑相加；原失败/XML和字节差异说明见 `2026-10-13-codex-reply-copy.md`。
- Header Source Copy/Fork 子菜单、完整分页 Markdown、实际 deep-link producer、新 side fork、空名提交和归档确认；前端7文件71项通过，编译修复后仅8个 Markdown 项定向通过。Source条件、7枚原glyph及未实现的pin/scheduled-task/new-window consumer见 `2026-10-13-codex-thread-overflow.md`。
- 后端 rename/sidechat 56项去重通过：真实完整 snapshot、资源和原 window metadata、独立边界与标准 public 历史；真正 WS→代理循环确认 side-only reply、父运行任务/待答 future 不变。Source `$Os` 从 record→agent_runner→initial developer，完整继承仅作为参考。
- Fork正式激活31项通过；Header归档 Home 17项一次通过。Root的对应当前 projection/session/validator128项范围通过（含旧重复用例）；显式 null/null 从 producer、payload contract 到 frontend 贯通，较晚导航胜出，源草稿与当前 mode 保留。
- 默认关闭的 TokenBudget 核心 `new_context/get_context_remaining`、Source90%/95%窗口/计数、结算后无总结切换、原窗口完整存证、真正 WS/CAS/REST/重开及独立手动操作：316项去重通过。真实 partial projection 已先保存新窗口时，必须由同一 core receipt 和真实 archive 证明该窗口，普通空摘要仍拒绝。最后两项真实 WS 无退出警告；Notes/model-owned启用/全部 hooks等边界见 `2026-10-13-codex-token-budget-context-window.md`。
- 前端只显示 Source 的 ContextCompaction 生命周期；真实内部模型工具原文和JSON仍存证。已有214项范围与独立 CompactTask/notice增量36项通过，有重复不相加。Source glyph、10秒说明、中性中断、原开始/结束时间、冷恢复、foreign owner、无假 assistant/重复 notice均有实际链验证。

生产控制 run08 在前三场景通过后因 QA 未悬停回复动作栏，普通鼠标被隐藏态 pointer-events 拦截；没有使用 force click 或改变生产 hover。run09 正常悬停后 Copy、Source菜单、Open in和Fork通过，随后新fixture错误要求自定义 Chat wire 保留developer role。现有 Chat adapter明确把leading developer合入system prefix；源内容真实只出现一次，core developer角色由真正内部WS loop另证。run10–12 的新会话/side首次启动超过旧5秒断言，按实际provider请求分阶段准入，而非延长UI断言掩盖失败。

run13–14 与仅Window diagnostic-run01 捕获真实前端故障：Source无总结窗口的合法空summary被旧validator拒绝，客户端主动断WS，重连replay再次拒绝。后端后续request/reply已完成，不是模型没回复或nested互等。生产修复后，仅Window `production-context-diagnostic-run02` 1测试/3场景全部通过、3张PNG，renderer warnings为空、pageErrors为空、native visible=[false]。

run15 前10场景通过，最后Header归档Home失败：正式null/null及home list都真实收到，前端却从包含其他聊天的inventory主动发fallback switch。修完整list consumer，Header实际home不用客户端fallback覆盖；其他入口保留原行为。`root-archive-navigation-run03` 2文件79项通过，有旧重复不相加，新增同一次正式home后多聊天list与不发反向switch的回归。后端缺usage另外沿真实usage_reported修正，实际Chat HTTP/SSE+VM+WS小批14项通过，明确reported0仍按0、未报告保留估计。

完整 `production-control-run16` 在 build-run09 的11场景全部通过。人工查看该轮截图发现非Source分支通知，修复后 `root-fork-notice-run01` 两文件96项范围通过（与旧导航/runtime范围重叠，不累加）。run17 的新增全页断言误匹配输入区的隐藏用量说明：截图中实际消息区已无分支统计通知。按真实目标作用域修正断言为 `.message-list-content`，保留原失败及截图；未为测试改变生产UI或隐藏真实状态。

最终 `production-control-run18` 在 build-run10 的完整11场景全部通过，退出0，用时约1.9分钟；13张生产截图已查看，sidechat无分支统计通知，manual压缩是独立真实状态，Header归档面对多聊天inventory仍停留Home且原草稿保留。`pageErrors=[]`、`native.visible=[false]`；renderer保留一条资源404，未称零警告。系统剪贴板和外部应用启动的目的地拦截不代表OS服务已实机验证。原失败轮和截图均保留，不由partial-checks冒称全场通过。协议sync一次通过（118事件、124命令及已登记payload字段/枚举），不是UI节点通过数。

后续 Source 文件链补批：apply_patch 不再强制 typed read；匹配实际当前文件并以同一真实预览快照贯穿审批→私有执行参数，审批期间磁盘改动不会被旧diff覆盖，canonical参数/digest保持原事实。四文件69项范围最终通过，另15项冻结请求授权通过；两个新fixture的callback/wire错误原日志保留，重跑不重复计数。详细依据及边界见 `2026-10-13-codex-patch-current-file-snapshot.md`。这项后端修复发生在上述production截图之后，未据旧截图冒称新的整批桌面通过。

公网模型、真实外部 MCP/OAuth、所有设备/应用发现分支仍未验。已接通的是实际桌面Markdown inline review；条件author-review、detached delivery、独立Rust ReviewTask与旧review/explore角色配置的完整迁移仍分开保留边界，不互相冒称完成。Source Guardian retained-context/reviewer、Notes及模型自动启用、stdin的launch-policy审批/hook配对、feature-gated request_permissions grant、remote/container/受限ConPTY、全部设置/非聊天控件和无完整consumer的pin/scheduled-task/new-window仍有明确缺口。当前证据不代表全部1063个静态节点或UI/服务100%相同；富文本全部插件块、系统clipboard/native MIME服务和所有跨根选区也未冒称完成。
