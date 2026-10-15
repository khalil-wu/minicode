# 2026-10-14 桌面侧栏与任务菜单收尾

依据为保存的真实 Codex 截图 `output/playwright/alignment-audit-20261011/site/images/codex-live-main.png`、过程投影四张用户原图，以及保存的 ASAR 提取 `output/playwright/codex-rebuild-20261012/desktop-regions/source/`。本轮没有读取当前安装包，也没有把旧截图当作当前 MiniCode 构建。修改前独立 offscreen Electron 直接加载最新 `frontend/dist/index.html`，真实后端与 WS 创建 QA 任务。

## 已落实

- **项目与最近只选中一处**：Source `app-shared::jti` 的 `sidebar-keep-projects-in-recent-v1` 默认 true；`Tti/Dti` 读取 `sidebarThreadLocationId`，将选择归属到实际 occurrence。真实 Codex 图也保留两份条目但仅项目条目高亮。MiniCode 原来两处 `active` 都按相同 conversation ID；现在保留两份真实记录，默认选择可见的项目条目，点击最近则将唯一选择移到最近。外部 owner 切换清除原 occurrence 归属，普通会话在最近选中。工作区标题仍不同时高亮。
- **侧栏菜单按实际 Source 分类**：`app-initial::jJo(surface=sidebar)` 与 `_0/$Vo` 是菜单、图标和 Copy 子项顺序的依据。移除常规侧栏自创的克隆、父会话合并、导出会话树及清理工作区入口；其后端 API 和历史没有删除。保留真实重命名、复制、原生打开、已知 Git 工作树移动及归档。复制按深度链接、Markdown、工作目录排序，并使用已经提取的 Source 图标。菜单共用已有 ContextMenu，保留 MiniCode 现有字号。
- **完整复制原 owner**：后台行调用与 Header 相同的 `loadConversationMarkdown`，捕获实际 conversation ID、执行目录、消息、分页、session ID 后读取。不会切换主对话，不拿当前前台消息冒充被点击任务的记录。深链使用已注册的 `minicode://conversation?conversation_id=...`。
- **归档确认与后端错误**：正在工作的后台 owner 先确认停止并归档；取消不发送归档，不改变前台草稿。重命名提交原始输入，空白由已经对齐 Source 的后端返回真实拒绝，不在前端静默当作取消。
- **菜单焦点**：侧栏菜单传递 Source 的显式 `returnFocusTo` 语义，Tab/Escape 回到真实触发按钮；二级菜单沿现有左右键、上下键、Home/End 和焦点保持。模式菜单上键方向表达式修正，保留两项菜单原有循环行为。

## 已完成验证

- 第一批侧栏与已有过程/子 agent 披露组件 **104 passed / 0 failed / 0 skipped**：`desktop-reference-finish-unit.json`。这一批包括组合读/运行、真实 pending/running/completed 过渡、过程收起以及子 agent 的阅读选择；不是新的真实模型任务验收。
- 第二批菜单第一次 **98/100**，失败记录 `desktop-reference-menu-unit01.json` 保留。一个新增测试主动替换历史 fixture 后错误断言旧数组引用；另一个暴露共享菜单缺少显式返回触发按钮的真实焦点问题。修复后受影响三文件 **42/42**；按文件与测试名合并最新结果为 **100 passed / 0 failed / 0 skipped**，见 `desktop-reference-menu-latest-results.json`。这些范围重叠，不相加。
- 首次整体验证时 Root 的新 MCP 文案缺 i18n 类型键，TypeScript 失败；Root 补键后本轮最新 TypeScript 和 Vite 均通过，构建日志 `desktop-reference-menu-build.log`。
- 初始 `desktop-reference-before01` 真实复现两处 active；它随后因 QA 错把原生“设置与帮助”菜单入口当成“设置”按钮而失败。修正定位后的 `desktop-reference-after01` 通过，三张 settled 图逐张查看，真实一处选择与切换位置一致。
- 新菜单 production `desktop-reference-menu-after01` 已完成真实模型回复与前后台 owner 创建，但 QA 直接点尚未 hover 出现的省略号，命中检查失败；失败原样保留。后续采用真实行 hover，再进行鼠标点击，没有 force click。
- 最终 `desktop-reference-menu-after02/verdict.json` **passed=true / errors=[]**。四项检查涵盖唯一选择、点击项目/最近、实际后台任务 Markdown 与深链复制、General 原生菜单导航；五张 settled PNG。真实后台回复 `DESKTOP_MENU_QA_FINAL` 来自生产 harness→WS→journal，复制包含原用户输入与最终回复，前台仍为第二 owner，草稿 `UNSENT_SECOND_OWNER_DRAFT` 保留。Copy 二级菜单三项顺序实际断言通过，系统剪贴板未动。当前菜单与草稿保留两张截图已逐张查看。

## 实机边界与剩余差距

所有运行使用独立临时 QA profile/workspace，窗口始终 offscreen；没有操控、保存或重启普通用户窗口及其草稿、附件、编辑器。新菜单 QA 的 model HTTP 回复受控，Electron、WS、后端、会话记录和 Markdown 读取使用生产实现；剪贴板写入接收端由 QA 替换为内存数组，不改系统剪贴板。

本轮没有将所有按钮宣称与 Codex 完全等同。Pin/取消 Pin、已读未读、移动到项目/自定义分组、Source 新窗口与附属计划任务等仍需要各自真实 producer/consumer；本轮没有摆仅外观可点的空按钮。原生打开目前侧栏提供真实 Explorer，Header 的已安装目标发现链保持原实现；两处目标集合仍不是同一完整菜单。

已有组合高光、300 ms 披露过渡及子 agent 默认折叠实现与本轮保存的 Source/原图核对后保持；没有为本轮虚构新的“所有过程实机通过”结果。当前完整工具/子 agent 生产任务验收应使用它们各自的最新链路记录。

后续三个显式控件已继续闭合，见 [固定、已读/未读和项目归属](2026-10-14-sidebar-pin-read-project.md)：真实持久化、全窗口 inventory、两份草稿与上传 owner、原执行 cwd，以及冷启动固定顺序均已验证。上文 Pin/已读未读/移动项目缺口描述的是本批最初菜单收尾状态；宿主自动未读、自定义分组及其余列出的范围仍不能据三控件验证算完成。

最新生产构建的真实主/子过程、组合扫描、收起、原始证据及冷启动已继续验收，见 [最新主对话与子 agent 过程投影](2026-10-14-production-process-final.md)。该记录同时保留真实 StreamEvent 回归和 QA 旧字段失败，最终 8 项结果、14 张逐图核对来自修正后的独立 run05，不把前批组件检查当成实机任务。
