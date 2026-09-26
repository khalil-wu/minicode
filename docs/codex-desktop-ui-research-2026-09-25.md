# Codex 桌面端调研与 MiniCode 优化建议

> **已撤回，待重新验证。** 本报告把官方功能文档、未连接后端的 MiniCode 预览和代码观察混合推导成设计优先级，缺少对 Codex 真实交互的充分交叉验证。下文保留作为原始记录，不应作为实施依据。尤其是侧边栏、面板组织、侧聊保留等建议，尚未证明优于现有设计。

调研日期：2026-09-25。对象：当前官方桌面端文档、官方 UI 示例、本机 Codex 更新状态，以及 MiniCode 当前工作区代码和前端预览。

**建议优先完成三件事：任务导航在不同模式下保持一致；跨任务的待处理与未读状态清楚可见；任务结束后直接进入成果和变更审阅。** MiniCode 已经实现不少核心能力，下一阶段的主要收益来自把它们组织成连贯的工作流程。

## 依据与边界

- 本机 Codex：`26.917.71314`，build `10954`，`prod` 渠道；官方更新工具返回 `up_to_date`。这表示当前安装渠道没有可用更新，不代表所有渠道的全球最高版本。
- 官网 `/codex/...` 文档目前跳转至 `learn.chatgpt.com/docs/...`。文档以 ChatGPT desktop app 为桌面外壳，同时保留 Codex 的开发者视图、代码审阅和 Git 功能，不能把 ChatGPT Web、CLI、iOS 的更新全部当作桌面功能。
- 已获取并阅读官方功能、项目、通知、审阅、自动任务、目标模式、工作树、浏览器、文件预览、插件、Appshots 和更新记录页面；实际查看了官方功能页的 UI 示例。
- MiniCode 检查覆盖导航、会话元数据、输入框、右侧面板、Git 审阅、自动任务、搜索、侧边聊天、浏览器批注等代码，并在 1440px 宽度的浏览器预览中查看代码模式、协作模式、右侧面板入口及自动任务。
- 本次前端预览没有连接桌面后端。截图中的连接失败由这个调研环境造成，**不据此判断生产桌面端存在连接故障**。涉及模型执行、原生浏览器、系统通知、重启恢复的判断属于代码核对，未重新进行端到端验收；不提供未经测量的性能结论。
- 本次只新增调研文档及本地截图，没有修改产品实现。工作区已有其他未提交改动，结论基于调研时的当前代码。

## 最新变化中，哪些值得参考

| 官方资料中的能力或变化 | 对 MiniCode 的实际启发 | 来源 |
|---|---|---|
| 项目和任务是持续工作的组织单位；支持置顶、搜索、归档和多文件夹项目 | 任务导航应稳定存在，文件树作为当前任务的工作工具 | [Projects and chats](https://learn.chatgpt.com/docs/projects) |
| Activity 在可用时集中显示未读、运行中、等待回复；通知区分完成、权限、问题 | 把“需要我做什么”做成可处理的队列，同时保留系统通知 | [Notifications](https://learn.chatgpt.com/docs/notifications) |
| Review 支持未暂存、已暂存、某次提交、分支、最近一轮，以及行评论、文件与 hunk 操作 | 审阅需要明确比较范围，并连接后续修改和交付 | [Code review](https://learn.chatgpt.com/docs/code-review) |
| 浏览器和文件预览支持针对具体内容给反馈 | 让成果、批注、修订进入同一任务流程 | [Browser](https://learn.chatgpt.com/docs/browser)、[Work with files](https://learn.chatgpt.com/docs/artifacts-viewer) |
| 自动任务支持独立运行及回到原聊天；Scheduled 有状态和未读结果 | 已有调度能力之外，增加面向用户的结果收件箱 | [Scheduled tasks](https://learn.chatgpt.com/docs/automations) |
| `/goal` 的进度条支持暂停、继续、编辑、清除目标 | 长任务需要可见的目标、当前阶段与控制入口 | [Long-running work](https://learn.chatgpt.com/docs/long-running-work) |
| 2026-09-11 公共更新条目：浮动 Quick Chat、Windows Appshots，并改进来源文件打开和批注草稿保留 | 全局快捷输入和上下文捕获值得后续做；来源打开、草稿保留属于更高频的基础体验 | [Changelog](https://learn.chatgpt.com/docs/changelog)、[Appshots](https://learn.chatgpt.com/docs/appshots) |
| 2026-08-11：Linux preview，以及导入其他智能体的设置和近期工作 | 降低迁移成本有价值，需按我们的目标用户安排优先级 | [Changelog](https://learn.chatgpt.com/docs/changelog) |
| 2026-09-22：Sol、Luna 新模型按计划及工作区逐步开放 | 模型选择应展示实际提供商能力；不把某个官方账号能用的模型写死为所有用户默认 | [Changelog](https://learn.chatgpt.com/docs/changelog) |

特别注意：2026-08-25 发布的 Gmail、Slack、GitHub 事件触发任务，当前自动任务文档明确标注为 Web 和移动端能力，**不适用于桌面端、CLI、IDE**。不应据此列出“桌面竞品已有，我们必须补”的需求。

## 我们已经具备的能力

以下“已有”指找到明确实现，不能等同于本次完成了运行验收。

| 能力 | 当前实现依据 | 判断 |
|---|---|---|
| 项目分组、会话重命名、归档、分叉/副本 | `ConversationsTab.tsx`、`SessionRow.tsx`、会话命令处理 | 已有，重点改善入口和组织 |
| 隔离工作区、Local/Worktree 交接与清理 | 命令面板的“新建隔离会话”、会话菜单和 workspace 后端 | 已有，不需要另写工作树引擎 |
| 长任务目标、暂停和继续 | `Composer.tsx` 的 `GoalBar` 与 `conversation.goal.set` | 已有，缺少更完整的编辑和进度呈现 |
| 运行中引导、消息排队 | `GeneralTab.tsx` 的 queue/steer 选项及输入处理 | 已有，值得强化当前消息将如何处理的反馈 |
| 自动任务和继续当前对话 | `SchedulerTab.tsx` 的 standalone/heartbeat、时区和隔离选项 | 已有，重点改结果管理与设置可读性 |
| 系统完成通知 | `chatStreamEvents.ts` 的后台完成通知、Electron Notification | 已有，不能说“没有通知” |
| Diff、逐文件决策和行评论 | `DiffPanel.tsx`、diff review 状态及协议 | 已有 |
| Git 文件级暂存、取消暂存和还原 | `DiffPanel.tsx`、`backend/diff/git_integration.py` | 已有，仍需补完整审阅范围及交付入口 |
| 浏览器、元素/区域选择与批注 | `BrowserPanel.tsx`、Electron embedded browser | 已有，不能重复立项“增加浏览器批注” |
| 文件成果和预览 | `ArtifactsTab.tsx`、`PreviewPanel.tsx`、artifact 后端 | 已有，重点提高完成后的可发现性 |
| 插件、Skills、MCP 与设置搜索 | Marketplace、SettingsCenter、plugins/skills/mcp 后端 | 已有 |
| 侧边聊天和上下文引用 | `SideChatPanel.tsx` 的 inheritedContext、selectedContext | 已有，当前产品明确将其定义为临时聊天 |

## 优先优化清单

P0 表示建议第一批交付，P1 表示随后完善，P2 表示需要结合用户需求再排期；不是故障严重等级。

### P0-1：让任务导航贯穿协作与代码模式

**现状。** `SidebarLeft.tsx` 在协作模式渲染 `ConversationsTab`，代码模式改为 `FileTree`。`HeaderBar.tsx` 主要展示项目名，缺少常驻的当前任务、分支及执行位置表达。任务能通过命令面板切换，但可见任务导航随模式变化。

**影响。** 用户在代码模式处理多个任务时，需要额外寻找任务；打开文件和管理任务争用同一块导航区域。复杂工作更容易失去“现在在哪个任务、哪个工作目录”的定位。

**建议。** 两种模式共用“置顶 / 项目 / 任务”导航；文件树放入当前任务工作区的可切换子面板。顶部显示任务名及项目/分支/本地或隔离工作区。新任务入口直接提供执行位置选择，让已有 worktree 能力容易被发现。

**验收目标。** 无需切换工作模式即可切换任务；切换任务后草稿、打开的文件和审阅位置仍属于各自任务；用户能直接辨认当前修改落在哪个目录和分支。

依据：[SidebarLeft.tsx](C:/Desktop/MiniCode/frontend/src.v2/shell/SidebarLeft.tsx:154)、[HeaderBar.tsx](C:/Desktop/MiniCode/frontend/src.v2/shell/HeaderBar.tsx:65)。

### P0-2：补齐跨任务的待处理、未读、完成与失败状态

**现状。** `ConversationMeta.sessionStatus` 仅声明 running/waiting/idle；任务行主要显示转圈、圆点、时钟。等待原因存在，但正文标签通过 `sr-only` 隐藏。当前会话元数据与列表未见置顶、已读游标或未读成果的完整路径。后台完成通知已实现。

**影响。** 多个任务并行时，用户难以一眼区分“已经完成但没看”“执行失败”“等我回复”。操作系统通知消失后，也缺少持续可追踪的统一入口。

**建议。** 增加“待处理”入口和数量，汇总问题、批准、审阅、失败及未读结果；任务行展示短状态文本。支持置顶、按待处理优先排序、标记已读、跳到下一个待处理任务。

**实现重点。** 使用现有运行事件、终态和问题队列；持久化置顶与阅读位置，并从真实状态派生展示。不能只在 UI 增加颜色或红点，刷新后又丢失语义，也不需要复制一套运行状态引擎。

**验收目标。** 运行、等待、失败、完成未读可辨认；重启后未读和置顶保留；点一次能到需要处理的具体问题或成果；回放旧完成事件不重复提醒。

依据：[types.ts](C:/Desktop/MiniCode/frontend/src.v2/stores/types.ts:1163)、[SessionRow.tsx](C:/Desktop/MiniCode/frontend/src.v2/shell/SessionRow.tsx)、[完成通知](C:/Desktop/MiniCode/frontend/src.v2/chat/chatStreamEvents.ts:1369)。

### P0-3：把成果与代码审阅组织成任务的完成流程

**现状。** 右侧已有上下文、审阅、预览、浏览器、侧边聊天、产物、子智能体、运行详情、运行状态等入口，通过添加面板按需打开，并非全部常驻。默认是上下文。产物面板内部标题又叫“文件”，另有主工作区文件标签、底部 Git 和设置中的 Git 与工作树。

Diff 面板已有“待审阅 / 上一轮 / 未提交”三类来源，支持 Git 文件级操作。这里有一个优先修正的问题：标为“上一轮”的 history 来源，实际遍历当前已加载的所有 `messages` 收集工具 patch 并反转，并没有按最后一个 turn 过滤；它也会读取工具参数或摘要中的 diff。与此同时，消息流已经处理了按 turn 归属的权威变更，两处的数据语义需要统一。

当前面板没有把 Branch / Commit 组织成统一范围选择，也没有在该面板内形成完整的 commit/push/PR 交付入口。项目已有 PR monitor 相关实现，后续应接入复用。

**影响。** 用户完成一个任务后，需要理解多个相近入口，才能确认改了什么、产生了哪些文件、测试怎样、下一步怎么交付。

**建议。** 右侧保留容易发现的“成果 / 变更”入口，浏览器、上下文、运行详情等继续按需打开。完成消息附近提供成果文件、变更统计和真实验证结果，点击进入同一工作面板。统一命名，减少“文件 / 产物 / 预览”的意义重叠。

先让“上一轮”使用现有权威 turn diff，再逐步补齐未暂存、已暂存、分支、指定提交；把已有行评论、暂存、PR 监控与后续操作串起来。代码审阅范围必须来自 Git 或已有权威 turn diff，不能从聊天中的 patch 文本推断为当前文件状态。历史工具记录可以保留为明确的执行历史。跨任务切换时面板继续遵守现有的归属关系。

**验收目标。** 任务结束后一步打开变更或成果；比较范围明确；评论可进入下一轮修改；已有用户改动与智能体本轮修改不混为一谈；未执行验证时明确显示未验证。

依据：[SidebarRight.tsx](C:/Desktop/MiniCode/frontend/src.v2/shell/SidebarRight.tsx:131)、[ArtifactsTab.tsx](C:/Desktop/MiniCode/frontend/src.v2/shell/tabs/ArtifactsTab.tsx:39)、[DiffPanel.tsx](C:/Desktop/MiniCode/frontend/src.v2/panels/DiffPanel.tsx:134)、[MessageList.tsx](C:/Desktop/MiniCode/frontend/src.v2/chat/MessageList.tsx:501)。

### P1-1：把任务搜索升级为能找回工作内容的搜索

**现状。** `CommandPalette.tsx` 目前以会话标题和 `goal.text` 做字符串匹配，并排除已归档会话。后端 transcript index 用于记录定位和分页，本次没有看到接通到任务搜索的全文检索路径。

**建议。** 保留命令面板，同时补任务内容、项目、分支搜索；搜索结果展示命中片段，点击定位到消息；提供包含归档的选项。先基于已有会话存储做必要检索，再按数据量决定是否加入全文索引。

**验收目标。** 只记得错误文本、函数名或分支名也能找回任务，不必记得自动生成的标题；不为搜索一次性向前端加载所有长会话。

依据：[CommandPalette.tsx](C:/Desktop/MiniCode/frontend/src.v2/overlays/CommandPalette.tsx:139)、[transcript_index.py](C:/Desktop/MiniCode/backend/conversations/transcript_index.py)。

### P1-2：让“已安排”成为结果管理页面

**现状。** 实际点击左侧“已安排”，通过 `openAutomations()` 进入设置中的 scheduler 页面。代码中另有 `AutomationsCenter` 模态组件，但不能把它当作当前主入口。已有频率预设、时区、隔离执行、立即运行、暂停、重试和原对话继续。任务列表直接展示 Cron，最近运行固定取前 8 条；当前展示中未见状态筛选、未读结果管理和任务编辑入口。

**建议。** 将“已安排”从配置型设置页提升为主工作区中的任务与结果管理页，统一现存入口。默认展示“工作日 09:00，上海时间”这样的描述，Cron 保留在高级设置；展示下一次运行、最近结果、所属项目、运行位置；允许筛选、编辑和查看完整运行历史。复用同一待处理机制提醒有意义的结果。

**实现重点。** 列表展示、编辑结果、下次运行时间与持久化调度配置使用同一份数据。继续复用现有 scheduler、运行记录和 heartbeat 能力。

**验收目标。** 用户能修改已有计划、找到第 9 次以后的运行、看懂下一次何时执行，并从结果返回对应任务。

依据：[automations-navigation.ts](C:/Desktop/MiniCode/frontend/src.v2/lib/automations-navigation.ts:3)、[SchedulerTab.tsx](C:/Desktop/MiniCode/frontend/src.v2/overlays/SchedulerTab.tsx:170)。

### P1-3：提高输入上下文与运行控制的可发现性

**现状。** 模型、推理强度、权限、附件、目标模式、消息引导和排队已存在；浏览器批注也已支持元素和区域选择。新建隔离会话的明显入口在命令面板。目标条有暂停、继续、清除，没有直接编辑目标的控件。

**建议。** 输入区域清楚显示本次使用的项目、执行位置、选中文件/网页和模型；集中提供 Goal、Plan、Skills 等常用能力入口。运行中发送消息时，直接说明它将“立即调整当前任务”还是“等待当前任务结束”。目标条补编辑目标和可信的阶段信息，不伪造百分比。

页面批注默认突出“选元素 / 框选区域 + 描述修改”，把 CSS selector 输入移到高级选项，使用现有选择能力降低学习成本。

依据：[FooterRow.tsx](C:/Desktop/MiniCode/frontend/src.v2/composer/FooterRow.tsx)、[GoalBar](C:/Desktop/MiniCode/frontend/src.v2/composer/Composer.tsx:739)、[BrowserPanel.tsx](C:/Desktop/MiniCode/frontend/src.v2/panels/BrowserPanel.tsx:1043)。

### P1-4：让侧边聊天的有用结论能留下来

**现状。** 已支持继承上下文及选择内容提问，但侧边聊天明确是临时模式；组件卸载会请求删除服务端会话，并清理本地状态。这是现有产品选择，不能误报为意外数据丢失 bug。

**建议。** 保留临时提问的轻量性，同时增加“将结论带回主任务”“保留为独立任务”操作；附上原问题和所选上下文的来源。不要直接把整段侧聊强塞回主任务。

**验收目标。** 用户得到有价值的解释后可以选择保留，主任务仍在原上下文中继续运行。

依据：[SideChatPanel.tsx](C:/Desktop/MiniCode/frontend/src.v2/panels/SideChatPanel.tsx:46)。

### P1-5：修正首页和模式表达

**观察。** 预览中的协作与代码空态共用“今天想构建什么？”，中部欢迎语与底部输入框相距较远；协作模式的用户也被引导到编程语义。没有体现打开项目、继续工作、使用现有技能的不同起点。

**建议。** 首次使用时把输入框与欢迎区放在一起，提供少量明确动作，如打开项目、分析文件、使用技能；已有任务时优先显示继续工作和待处理事项。进入对话后再将输入框固定在底部。保持现有字体、色彩和圆角体系，先解决内容层级及动作距离。

这属于基于当前空态的设计判断，不是通过用户测试验证过的转化结论。

依据：[MessageList.tsx](C:/Desktop/MiniCode/frontend/src.v2/chat/MessageList.tsx:487)、本地预览截图。

### P2：后续扩展

| 方向 | 价值与安排理由 |
|---|---|
| 全局快捷输入、Appshots 类当前窗口上下文 | 减少复制截图和切换应用；等任务入口与附件体验稳定后做 |
| 语音输入或语音协作 | 取决于实际用户使用频率和模型供应商支持，暂不阻塞主要流程 |
| 从其他智能体导入项目、指令和近期工作 | 有利于迁移；先明确需要支持的工具和数据格式 |
| 跨机器 / SSH / 云端连续工作 | 应单独评估运行环境和同步语义，不能只补一个远程按钮 |
| 宠物、浮动装饰和大规模动画改版 | 对上述核心工作问题收益较低，当前不建议优先投入 |

## 推荐的界面结构

这是针对 MiniCode 的建议结构，不是声称已实现的 Codex 精确复刻。

```text
┌─────────────────────────────────────────────────────────────────────────┐
│ 当前任务                 项目 · 分支 · 本地/隔离工作区        运行状态 │
├─────────────────┬────────────────────────────┬──────────────────────────┤
│ 新建任务        │ 对话 / 编辑器              │ 成果 / 变更              │
│ 待处理 3        │                            │                          │
│ 已安排          │ 目标与主要进度             │ 文件、变更、验证结果     │
│ 插件            │ 回答和可展开的执行过程     │                          │
│                 │                            │ 按需打开：浏览器、上下文 │
│ 置顶            │                            │ 子任务、运行详情         │
│ 项目 > 任务     │ 项目/附件/引用             │                          │
│                 │ 输入框                     │                          │
│ 设置            │ 模型 · 推理 · 执行方式     │                          │
└─────────────────┴────────────────────────────┴──────────────────────────┘
```

文件树服务于中部编辑器；切换协作/代码只改变工作区内容和技术信息密度，不移除任务导航。右侧已有面板可复用，重点是入口、命名和任务归属。

## 实施顺序与整体验收

1. **第一批：任务导航 + 注意事项状态 + 成果入口。** 先确定持久化任务字段和已有运行状态的映射，再改导航、任务行、完成展示；把一条完整的多任务使用流程做通。
2. **第二批：审阅范围 + 搜索 + 自动任务页面。** 接通已有会话、Git、scheduler 和 PR 能力；增加真正缺失的接口，不重造运行系统。
3. **第三批：输入与首页细节 + 侧聊保留。** 统一入口及文案，改善跨页面衔接。

每批先完成关联改动，再统一补充必要测试与运行验收，避免一个小控件一次全量构建。建议以以下场景验收，而非仅检查按钮是否存在：

- 三个任务分别运行、等待回复、完成未读；从代码模式直接切换和处理，草稿与成果不会串到其他任务。
- 刷新或重启后恢复置顶、未读、任务位置；旧事件回放不会变成新的提醒。
- 一次任务修改文件、给出验证结果；用户查看本轮变更、附上行评论、继续修改，再进入明确的 Git 交付流程。
- 修改自动任务，查看较早的失败运行，重试后返回它对应的任务和成果。
- 搜索正文中的错误文本，包含归档记录并跳到命中消息。

以上是未来改动的验收目标，本次没有运行产品功能测试。

## 调研截图

- [官方 Features 页面与桌面 UI 示例](C:/Desktop/MiniCode/output/playwright/codex-official-features-20260925.png)
- [MiniCode 代码模式空态](C:/Desktop/MiniCode/output/playwright/minicode-research-code.png)
- [MiniCode 协作模式空态](C:/Desktop/MiniCode/output/playwright/minicode-research-cowork.png)
- [MiniCode 右侧面板入口](C:/Desktop/MiniCode/output/playwright/minicode-research-panels.png)
- [MiniCode 自动任务界面](C:/Desktop/MiniCode/output/playwright/minicode-research-scheduled.png)

截图存于被 Git 忽略的 `output/playwright/`；是本机调研附件，不是产品发布截图。MiniCode 的红色连接提示属于未连接桌面后端的预览环境。

## 官方来源

全部于 2026-09-25 实际打开或获取正文。文档是持续更新页面；平台和账号可用性以各页说明为准。

- [Features](https://learn.chatgpt.com/docs/features)
- [Use ChatGPT：Chat / Work / Codex 的区别](https://developers.openai.com/codex/use-chatgpt)
- [Projects and chats](https://learn.chatgpt.com/docs/projects)
- [Notifications](https://learn.chatgpt.com/docs/notifications)
- [Code review](https://learn.chatgpt.com/docs/code-review)
- [Scheduled tasks](https://learn.chatgpt.com/docs/automations)
- [Long-running work](https://learn.chatgpt.com/docs/long-running-work)
- [Git worktrees](https://developers.openai.com/codex/environments/git-worktrees)
- [Browser](https://learn.chatgpt.com/docs/browser)
- [Work with files](https://learn.chatgpt.com/docs/artifacts-viewer)
- [Plugins](https://learn.chatgpt.com/docs/plugins)
- [Appshots](https://learn.chatgpt.com/docs/appshots)
- [Commands and keyboard shortcuts](https://developers.openai.com/codex/reference/commands)
- [ChatGPT & Codex changelog](https://learn.chatgpt.com/docs/changelog)
