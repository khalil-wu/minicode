# Codex conversation overflow：第二批

以安装版 `26.1007.2314` 的 ASAR 和原图 `output/playwright/ui-reference-20261004-v2/desktop/01-codex-chat-sidepane.png` 为依据；没有操作正常用户窗口。Source 提取位于 `output/playwright/codex-control-audit-20261013/codex-open-source/`。字体、字号、Root clipboard 和 reply cell 保持其当前实现。

## 实际 Producer 和字段

`thread-overflow-menu-52f4df713089.js::hn` 负责 Header。它调用 `app-initial-b8e1c8014bb4.js::jJo(surface=header)`，并通过 `_0` 补菜单标签、图标和快捷键。`tHo` 将 rename/archive 统一为 `v0.rename/v0.archive`，不是直接显示 `oJ.renameThread` 的长标签。`jJo` 按 organization / continuation / copy / window / notifications / lifecycle 分组；本批补齐对应组间分隔线。

| 控件 | Source 条件、字段与副作用 | MiniCode 本批 |
|---|---|---|
| 聊天操作 | hn 的 moreActions 本版中文“聊天操作”；pending worktree 与普通 thread 分支不同 | 保持原入口名；仅对已选 canonical owner 操作，pending switch 不误用标题 |
| 重命名 | 普通 thread `requireNonEmpty=false`、`trimOnSave=false`、原值到 setThreadTitle。官方 `thread_processor.rs::thread_set_name_response_inner` → `normalize_thread_name`，trim 后空串明确 error | Prompt 新增局部 allowEmpty，Header 不先 trim 或把空串当 Cancel；后端由 sidebar_source_width 修真实 normalize/error/revision |
| 打开侧边聊天 | hn 的 Tn 条件包括真实 thread、非 archived；中文虽为“打开侧边聊天”，英文为 New side chat。xks → ZOs → Hoa(ephemeral=true, sideConversation=true, sourceConversationId, addForkedSyntheticItem=false) | 每次 Header 操作生成新的 side id/owner，保留已有 draft；固定 id、source_conversation_id、原 execution cwd、captured permission、activate=false 到实际 create/fork |
| 分叉 / 继续 | jJo → Xda；Rua 只排 cloud/durable。local 与 worktree target，cowork 的 Continue 文案不同；hn 的 tr/rr 成功后导航目标 | 用真实最后消息 id 调 context.fork(create_branch=false/true, activate=true)，由 canonical switched 接管，不本地伪造选择。新 worktree 仅对已知 repo；已有 worktree 使用 Source 同一工作树标签 |
| 复制 | `$Vo` 固定子项顺序：deeplink、Copy as Markdown、working directory。来源至少两项时生成 Copy submenu | 已收为“复制”子菜单，保留原 owner；没有 Source 的 Header Export tree 项已移除 |
| 复制深度链接 | Jda → Kda，Codex 为 codex://threads/id | MiniCode 用现有注册协议 minicode://conversation?conversation_id=encodedId；desktop/main.js::normalizeDeepLinkTarget 已按 query 解码并经真实 deep-link receipt 切会话 |
| 复制为 Markdown | qda 先 ensureConversationHistoryLoaded；nmi → conversation-markdown::fe/he：标题、引用用户内容、披露过程、普通最终 Markdown，reasoning 不导出 | 独立读取原 owner 缺失的较早消息和工具页；不写阅读缓存、不改滚动位置；真实错误从 errorInfo 保留，未知操作使用真实名称。空会话不制造正文或 role 标题 |
| 复制工作目录 | `$Vo` 用 Copy icon；enabled=Boolean(cwd)，Gda 的 captured thread/cwd | 使用原 execution cwd，禁用空目录；不误复制 projectless 浏览根 |
| 归档 | hn 在实际 active turn 或 attached heartbeat 时确认；AGo running 提示“停止并归档此聊天？”/“停止并归档”；W 默认 archiveNavigation=home | running/waiting owner 明确确认，取消不发 archive/interrupt。Header 独有 archive_navigation=home 已贯穿 helper/wire；后端真实 drain + canonical home/receipt 与 Root projection 由协调 agent 接入 |

## 侧边聊天完整消费链

新增 `chat/openSideChat.ts` 仅创建 side 客户端 owner 和打开实际 panel。`SideChatPanel` 带固定 child id 和 source parent 的 `conversation.create`，后端真 fork 上下文/完整主历史/资源，主前台和主 turn 不移动。移除了旧 `ensureSideChat` 最近 6 条 × 280 字摘要，以及首次提交把该摘要加在用户提问前的做法。

parentConversationId 随已有 side draft 的原始附件、选区、上下文引用持久化；旧没有 lineage 的真实 draft 仍保留原独立记录。新 child 初始化的 side history 为 []。同固定 id 复用时，后端返回已切好边界的 `side_chat_transcript`，Frontend 标准 hydrate 并保留其现有 side-owned live 消息和 draft；不会把 Main 前缀展开成“新侧边消息”，也不会根据 index=0 等值猜边界。

已对定 ACK：data.conversation_id=固定 child id，source_conversation_id / parent_conversation_id=真实 parent，parent_message_index（0 合法），side_chat_transcript，conversation_type=side_chat，created=true，activated=false，revision。后台 Source/WS/实际 agent loop 的证据由 `/root/sidebar_source_width` 记录；真实最终答案是 item.completed / item.type=agent_message / source=model_final，没有构造 agent_message.completed。

## 验证与边界

首批前端统一运行 6 文件 68 项：65 passed / 3 failed。两处是旧测试契约（sendChatMessage 的 displayContent 与 Source 已移除 command-level Stop），一处查到 Markdown 新 serializer 错读 legacy error 字段，已改为实际 errorInfo。失败输出未当通过。

修复后的统一批次 7 文件 71 passed，0 skipped：ChatActionsButton / MainSlots / conversationMarkdown / SideChatPanel / DialogService prompt / composer-drafts / side-chat-selection。涉及原 owner、历史与工具分页、读取失败、空输入提交、真实创建请求、冷恢复、多个 side draft、Stop/Archive 取消、canonical Fork 和工作树条件。没有重跑已完成的 Open in 专项，也没有单独 build。

本批尚未生成新的正常窗口截图；统一 production build 与 offscreen control-run08 由 Root 执行。不是所有 Source 条件都已实现：Pin 缺实际 sidebar consumer；scheduled task 与新窗口/new-chat-in-worktree 缺对应完整 producer/consumer；未摆空按钮。附属 heartbeat 的永久 remove 与当前 stop_for_conversation 不是同一行为，未写虚假“移除计划任务”确认。cloud/durable、真实 active-turn Fork、远程实例、任意 Source render type 的字节级 Markdown一致性仍未声明运行通过。Source 标准快捷键注册亦未据菜单存在宣称通过。

## actual_source_header：归档 Home 后端链

实际 Source 为安装版 `thread-overflow-menu-52f4df713089.js::hn`：`archiveNavigation` 未提供时取 `home`，`On` 将 `onArchiveStart` 设为导航 `/`（replace，focusComposerNonce，真实 project）；running/heartbeat 确认仍由原前端链处理。本批没有给其他归档入口改默认导航。

Header 请求为 `conversation.archive` + `archive_navigation: home`，`conversation_id` 保持捕获的原 owner。后端继续真实 drain/资源 ACK/metadata archive；该导航 receipt 仍当前时清当前执行 runtime，再发布正式 `conversation.switched`：`conversation_id: null`、`conversation: null`、实际 `session.active_conversation_id: null`。该 switched 和同命令的 initiator `conversation.list` 都带 `archive_navigation: home` 与原 command id/type。最终 receipt 仍归属原被归档 owner，明确 `archived`、`home_activated`、`superseded`，没有用前端置空伪造后端选择。

更晚的 switch/create 会替代旧 Header home receipt，原目标仍归档，旧 home 不再发布，也不自动选 fallback 覆盖新导航。未带 flag 的 sidebar 请求保留现有 fallback 逻辑；其他窗口沿用自己的原处理链。源 transcript/snapshot、附件与工具资源、未保存文件保留。

`header-archive-home-run01` 一次 **17 passed、0 failed、0 errors、0 skipped**。真实 WS 验证空 owner/switched → 带 home 的 list → source-owned semantic receipt 顺序；另有较晚 switch/create、旧 sidebar fallback、cleanup 拒绝不发 home、原 terminal/cleanup ACK、已有最后会话归档回归。证据：`output/playwright/codex-rebuild-20261012/backend-control-audit/header-archive-home-run01.log` 与同名 `.xml`；这个独立批次不与先前计数相加。没有单独 build 或操作正常用户窗口。
