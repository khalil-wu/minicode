# Codex 逐区域复刻：实现与验收

本轮使用三个 agent 分别负责主聊天与文档/浏览器投影、多 agent 投影、左栏/首页/输入区；主 agent 负责插件/技能/MCP 设置、摘要、集成和统一验收。视觉依据为原图册全部 45 张图片、当前 Codex 多 agent 实机截图及用户追加的项目菜单截图。MiniCode 字体、字号、主题与文字风格保持。

后端保留 MiniCode，实现依据来自 `.tmp/codex-src` 的真实工具、提示词、协作与生命周期调用链。没有接入 Codex app-server 替代 MiniCode，也没有加入无真实来源的账号、会员、排行榜或服务状态。

## 区域和最终行为

| 区域 | 本轮实现 | 验收范围 |
|---|---|---|
| 左栏、首页与项目 | 品牌模式菜单、当前项目标题、就近项目搜索/勾选/不在项目中工作、本地与真实分支范围 | 最新独立资料首页、空项目菜单、模式菜单；导航 receipt 与草稿归属契约 |
| 输入区 | 文件/文件夹子菜单、真实插件引用、目标正常创建/接管会话、按会话保存的 conversational plan；执行计划更新原 owner | 实机保存目标和保留原草稿；插件身份、目标 adoption、计划与权限独立、隐藏 owner、晚回执及队列契约 |
| 主聊天过程 | 灰色真实操作短行、文件/URL/摘要同一界面字体、实际整数秒、保留手动阅读和展开选择 | 组件/事件链批次；真实多 agent 运行画面另见受控生命周期证据 |
| 子 agent | 持久真实身份、执行开始与资源接受分离、真实名称/头像、已开启 0 分组、完成时间不随轮询重置、无聊天框的独立工作记录 | 真实 Electron/后端/WS/工具链受控任务，首轮失败证据原样保留 |
| 摘要、来源与 Diff | 活跃头像优先、终态计数准确、真实来源添加、每个 Diff 文件独立展开、根目录随产物来源贯穿 | 摘要组件/owner 链和受控任务截图；完整真实 PR 检查状态未覆盖 |
| 文档和浏览器 | Markdown 源码/阅读切换；工作区文档打开保留来源根目录；反馈进入正常发送/排队/引导链并保留原草稿附件 | 类型/组件/协议链；网页上浮动反馈卡仍有原生 BrowserView 的布局差距 |
| 设置与扩展目录 | 齿轮设置/帮助菜单；插件、技能、MCP 同一入口；MCP 搜索和真实错误；目录轻量条目、面包屑、真实能力/默认提示词 | 最新实机设置、插件空状态、5 个真实内置技能和技能详情；已安装外部插件与 OAuth 非空状态未全部实测 |

原始 45 图审计表保留在 `2026-10-12-ui-reference-audit.md`，不把旧截图更新日期当新构建证据。最新逐区域图册为 `http://127.0.0.1:43786/index.html`，图片来自 `output/playwright/codex-rebuild-20261012/`。

## 实际发现与根因修复

1. 新目录采用独立工具栏后，内容 grid 被隐式排到错误行，正文为空。明确内容所在 grid 行并重新构建、重新拍摄；没有用旧截图盖过空白。
2. 普通前台子任务续传变成后台 delivery 时，原始执行 profile 的消息能力被错误缩窄。对照 Codex 的 resume-config 路径，保留原本授予的权限和通信能力，仅更新 delivery；原受限 background profile 没有被扩权。
3. 受控任务的第二轮真实通信触发主对话新回合，前后端 UI 快照把会话级子任务列表清空。新主回合、retry 和父任务结束后的子任务回调现在均保留会话历史。新 mailbox epoch 清理旧执行摘要，保留转录；旧 epoch 与错误路径不覆盖当前身份。失败截图与事件保存在 `controlled-multiagent/run-02-failed/`。
4. conversational `agentMode` 原来属于全局，跨会话不能分别记住计划选择。现在进入真实 ConversationRecord、配置命令、Workbench 和草稿，隐藏发送使用其自己的模式；执行计划的晚接收回执不改变另一个会话或用户之后的新选择。
5. 旧 queued turn 开始时会把显式执行模式写回默认配置，覆盖排队期间新选的模式。配置和实际 turn 分开：队列落盘成功/实际 admission 才按接收时的配置修订号更新，执行旧 turn 不重新覆盖默认；真实配置重新选择同一个值也增加其修订号。已验证队列重开、启动拒绝、队列写入失败以及队列落盘与配置写入间的中断。
6. 实机资源预检卡在 Windows Git 子进程。线程和实际进程证据确认，资源工作树与记忆维护的 Git 调用继承了桌面生命周期输入管道。对照 Codex `git-utils/src/git_process.rs` 的 `stdin(Stdio::null())`，两个非交互调用均明确使用 DEVNULL；没有加兜底或缩短超时。持有父输入管道不关闭的真实 Git 回归和完整桌面流程通过。
7. HTTP 400 的状态码/错误码/类型已经存在于后端转录，但前端 ActivityCell 的中间模型没有传递 detail。补齐转录→ActivityCell→现有展开证据链；默认简短错误保留，展开后可见真实 HTTP=400 和 controlled_fixture。原始 Provider 响应正文不当作普通短行内容。
8. 用户追加截图要求去掉子 agent 详情顶部的“运行中”。已去掉这一泛化状态行，空区域收起；过程计时、真实失败和等待关系保留。运行中的真实详情已通过桌面交互检查。

## 验证记录

这些批次存在重叠，不能把表中数字相加作独立用例总数。失败及未完成的场景不算通过。

| 批次 | 结果与证据 |
|---|---|
| 前端受影响批次 | 51 文件首轮 1195 项，1182 通过/13 失败；修复真实问题和旧断言后，12 文件 254 项通过；设置批次 3 文件 82 项通过。修复前日志保留，新增用例计入后受影响范围 1197 项通过。 |
| 后端初始批次 | 13 文件 250 项通过，`backend-batch.log` |
| 后端导航收尾 | 原 29 项与新增 5 项共 34 项通过，`backend-navigation.log`、`backend-navigation-repair.log` |
| 后端续传修复 | 4 文件 248 项首轮 247 通过/1 旧 fixture 失败；fixture 修正资源接受/实际执行边界，保留原断言，单例通过，`backend-resume-repair.log`、`backend-resume-fixture-repair.log` |
| 最终前端状态批次 | 18 文件 240 项断言全部通过，但旧菜单 mock 没有返回真实异步命令回执，造成 2 个未处理测试错误；修正 fixture 后该文件 6 项通过、0 错误。最终受影响 240 项通过，证据 `final-frontend-state-batch.log`、`final-menu-fixture-repair.log` |
| 最终后端状态批次 | 11 文件 248 项首轮 247 通过/1 旧快照预期缺少 startedAt:0；补足不虚构开始时间和二次恢复不改变状态的断言，单例通过。最终受影响 248 项通过、0 跳过，`final-backend-state-batch.xml`、`final-backend-fixture-repair.xml` |
| Git 生命周期输入管道 | 资源启动/记忆维护批次 105 项通过、0 跳过，包含两个真实 Git 持管道回归，`git-stdin-batch.xml` |
| 追加 UI 与失败证据 | 子 agent 详情现有 34 项通过，`user-running-label-check.log`；投影/活动/Provider 状态 4 文件 91 项通过，包含转录到实际详情的回归，`provider-evidence-batch.log` |
| 类型、协议、主目录构建 | 最终通过；类型检查捕获新命令遗漏联合类型声明后已补完整接口/导出/联合。失败日志 `final-build-state-repairs.log` 保留；最新 `final-build-provider-evidence.log`（TypeScript + Vite，6.73 秒）、`final-protocol-complete.log` 均退出 0。两份官方 collaboration 模板原始字节已恢复，分别 9312/1307 字节 |
| 独立资料实机 | 目标保存成功、原草稿保留、计划开启且权限仍为询问；页面异常为空，`visual-live-results.json`、`optimized-ui-errors.json` |
| 多 agent 受控生命周期 | 第 13 轮完整通过，退出 0；5 个实际场景均为 passed，8 张当前 native compositor 截图，Provider fixtureFailures=[]。真实 Electron、后端、WebSocket、QueryEngine、TaskTool、read_file、send_message、持久化与恢复；模型服务为本地确定性 Chat Completions，不能代表外部 Provider。证据 `controlled-run-13.log`、`controlled-multiagent/controlled-evidence.json` |

实际生命周期流程、证据口径和各次运行结果另见 `2026-10-12-controlled-multiagent-e2e.md`。所有已归档失败/诊断目录继续保留，不能算作通过。测试窗口后台操作，仅测试窗禁用原生鼠标输入以避免查看操作影响脚本；生产控件仍由 Playwright CDP 驱动，截图短暂 showInactive 后采集。没有向 store 写入模拟消息/子任务，也没有伪造 WebSocket。

## 集成与用户内容

本轮先保存主目录 481 个已有修改文件和 1 个删除项作为基线，在隔离 worktree 修改，再比较本轮差异。初始集成 117 个文件无冲突，最终状态批次 34 文件和后续小修逐文件对照已集成 checkpoint；没有整目录覆盖用户修改，没有提交或重置 Git，没有清理用户资料、草稿、附件或未保存编辑器缓冲区。

实机和 E2E 使用各自新建的资料与测试目录，不复制原用户 profile。非 Git 场景使用经真实 git rev-parse 确认的 Windows Temp 独立目录；此前错误继承 HOME Git 的测试没有新建工作树，相关 Git 进程均已退出。诊断 helper 仅存在审计目录，最终通过运行已从 PYTHONPATH 去除；产品没有加入调试开关。
