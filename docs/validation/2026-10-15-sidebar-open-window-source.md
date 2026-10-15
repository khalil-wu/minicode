# Sidebar / Header 的 Open in 与已有聊天新窗口

本批只处理本地桌面已有 main thread 的窗口与应用打开链。依据为保存的 Codex ASAR `26.1007.2314.0`，未执行参考应用代码，未操作正常用户窗口或账号。原图 `output/playwright/ui-reference-20261004-v2/desktop/01-codex-chat-sidepane.png` 已查看；它用于区域和入口对照，不能冒充新窗口操作记录。字体、字号、主题、独立文件编辑器入口与 Pin 拖动 CSS 均未改。

## 精确 Source

原函数和原生函数保存在 `output/playwright/codex-control-audit-20261013/source/sidebar-open-window-source-functions.json`。`app-initial-b8e1c8014bb4.js` SHA256 为 `799331dde54905c4df5e360f23a9621141759e2c0acb230aeab909f9904b646f`。

- `jJo`（7600114）是 header/sidebar 共用菜单 producer。窗口组在两个 surface 的专有分支之后，统一以 `cwd != null && MJo({conversationId, hostId})` 添加 `OJo`，不是仅 fork 分支的条件。
- `MJo`（7606235）要求 `rdr(thread)` 为真且不是 remote-control connection；`rdr`（3031394）读取真实 host/dirs origins，并要求对应 origin 的 `root != null`。普通任务持久化 `git_branch` 为空不代表不是 Git 仓库。
- `OJo`（7597388）查询目标；涉及真实 worktree 时刷新其 latest workspace root。选择目标时调用 native mutation，携带实际 `path/cwd`、explicit target、`openMode=workspace`、`persistPreferredTargetPath=cwd`。
- `kJo`（7598694）仅保留 `!hidden && available === true`，Work 模式进一步去掉 editor/terminal；空集合隐藏 Open in；读取中和失败使用已有 Source 禁用行。
- `jJo` 的 `open-in-new-window` 要求非 orbit 且该能力可用；发 `/local/<threadId>`，remote 另带 host。`WXo`（7651829）从实际 sidebar row 读取 thread；`app-shared::sAi`（6592835）在 Mac 使用 Meta，其余使用 Ctrl。
- native `main::QXe`（3374481）验证 route 后调用 `createFreshWindow(path, originatingWindow)`；真正实现（3758628）是 `async(path='/', opener) => fe({show:true,initialRoute:path,opener})`。因此用户显式“新建窗口”的默认是 Home，不能把复用槽位的旧 thread 当作 fresh 路由。
- 新窗口图标直接来自 `app-initial::Cqo`（7564816）的原 SVG，保存为 `frontend/public/apps/thread-new-window.svg`；中文“在新窗口中打开”取自同版 `threadHeader.openInNewWindow`。

## 当前生产调用链

侧栏 `SessionRow` 捕获真实 row 的 ID 和执行 cwd，`ConversationMenu` 与 Header `ChatActionsButton` 共同消费 `useWorkspaceOpenMenu`。此 hook 对被点击的真实执行目录调用已有 `fetchWorkspaceGitStatus` → `/api/workspace/git/status` → `workspace_git_status_payload/run_ui_git_metadata`，严格要求 `is_git_repo === true` 后再读取 native 应用集合。该查询带 runtime 认证和受信工作区，使用已有固定 UI Git metadata 服务，不启动模型工具、不切换主会话、不从 branch 字符串或目录名字推断仓库。

应用选择继续走 `openPreferredWorkspace(capturedRoot,targetId)` → runtime/preload → 受信 `minicode:openPreferredPath` → 实际 native opener。global/perPath 首选项由 native producer 持久化并广播。`fileOpenTargets()` 本身只产出检测到真实 executable 的 Windows 条目；未安装 catalog 条目不进入返回集合，内部 minicode/system 条目均 hidden。该生产契约没有可选 available 字段，不把未知 availability 兜底成 true，也不借 General 的历史选择规则显示不可用应用。

新窗口菜单和 Source 平台 modifier 调用 `openThreadWindow(clickedId)` → runtime/preload → 受信 `minicode:window:openConversation` → `windowManager.createNewWindow({conversationId})`。它创建不同的 `windowOwnerId/windowInstanceId/windowBindingToken`，只附带一次性导航目标；不调用 `conversation.create`、fork 或发送 user_message。新 renderer 通过真实 `session.restore` 读取原 thread，由 canonical 后端事件确认 owner。

初始化明确区分三种意图：应用初次启动的 `createMainWindow` 选择 restore-last（initialNavigation=null）；用户显式新空窗选择 Home（initialNavigation={conversationId:null}）；已有 thread 操作传实际 ID 的 navigation object。显式 nullable object 避免 undefined 经过 IPC 时与 Home 混淆。Electron 会在中间 document 和真正 frontend preload 各读取一次 runtime，因此 getter 保留待处理 route；只有真正 renderer 的 `main.tsx` 在连接前完成 `initializeWindowNavigation`、写入当前窗口活动 key 后，才经受信同步 consume ACK 清除 native record。中间 preload 不消费。WS 重连不再次读取 renderer 留存的 initial runtime 字段。Primary 保留既有活动 key，secondary 使用 `minicode.conversation.active-id:<windowOwnerId>`，因此后一个窗口不会回读 Primary 全局导航。

Home 只改变当前窗口的活动路由，不删除原有 conversation/workspace 草稿、真实上传附件或未保存 editor buffers；这些继续使用既有 window-owner + conversation/workspace 的存储。`conversation.list` 是 inventory；当前没有活动 owner 且当前窗口持久 key 明确为空时，不再自动挑第一条记录。真实旧 owner 被移除时的既有 canonical fallback 请求保留。`SessionRow` 的 Pin `dragActivatorRef/dragAttributes` 保留。独立文件编辑器/预览打开逻辑保持原实现。

## 验证记录与真实失败

必要前端检查首次 8 文件 70/70；真实仓库 gate 和平台 modifier 修正后的受影响 3 文件 22/22；最终导航/consume/Home inventory 两文件 7/7。按最后结果替换整文件后合并为 **9 文件 76/76、0 failed、0 skipped**，见 `sidebar-open-window-unit-latest.json`，不把旧测试重跑或已替换的名称相加。原生窗口/IPC 最终 **12/12**，日志 `sidebar-open-window-native-final.log`。Enter、compact 和原 settings 312 项未重跑。

每批完整修正后统一 TypeScript/Vite。最后 consume ACK/Home inventory 根因批次 `sidebar-open-window-build04.log` 退出 0，实际 bundle 为 `index-ClcLUw5p.js`；CSS 仍为 `index-CCxOW9kY.css`。每个实机 evidence 保存其实际 index/native hashes，不把旧构建当作最终构建。

- `production-sidebar-open-window-run01`：真实模型运行、附件上传和文件读取已发生，QA 仍定位旧 textarea.inputarea，但当前 Monaco 使用 native EditContext，超时发生在任何窗口/应用场景之前。已查看失败图，仅改 QA 为实际 view-line 鼠标聚焦和原生输入，并用已有 split 布局动作同时显示聊天。未改产品编辑器。
- `production-sidebar-open-window-run02`：真实 Git 工作区却没有 Open in；持久 meta 的 `git_branch` 为空。确认了把 Source origin gate 映射成 conversation branch 的产品根因，随后改为上述真实目录 Git 查询。未启动外部 app，失败原样保留。
- `production-sidebar-open-window-run03`：QA 新增的只读 native executable 核对使用了 Electron evaluate 中不可用的 require，在实际应用选择之前失败；改为 Node 自带 module.createRequire，只读确认目标必须是本批受控 executable 后才允许点击。产品未为 QA 改目标发现。
- `production-sidebar-open-window-run04` 的第一项真实 native 选择、子进程参数、global/perPath 首选项及原稿保持通过，原菜单图已查看。第二项虽然打开了 A，实际却没有 session.restore：第一次中间 preload 提前消费 route，随后 inventory fallback 自动发送 conversation.switch(A)，不能据“界面选到了同一任务”冒称正确。`sidebar-open-window-native-debug01` 又在真实 IPC 中确认目标参数正确传到 native、每个 webContents 有两次 runtime:get；没有运行模型任务，不计功能场景。随后修复上述 explicit navigation object、真正入口 consume ACK 与 Home inventory 条件，未加入猜目标兜底。
- `production-sidebar-open-window-run05` 前三项通过；第四项 QA 错用浏览器不允许主动发送的 close code 1011。`run06` 换成了产品明确视为 permanent-auth close 的 4001，同样不能验证自动重连。最终使用产品已有的 `CLIENT_RESYNC_CLOSE_CODE=4002`，不是修改产品关闭语义来适配 QA。
- `run07` 前三项通过，第四项真正 Home/resync 已成功；随后 QA 把 active composer 的陈旧 workbench shadow 与后续正确更新的缓存做全对象比较而失败。改为持续比较真实 active draft/附件/editor，以及真正隐藏 owner 的原稿；不把当前 active owner 的旧 shadow 当作权威草稿，也不修改产品或原证据。
- 一次受影响 unit 调用误从仓库根运行，未加载 frontend Vitest 配置，得到 ResizeObserver 缺失；原报告保存为 `sidebar-open-window-unit-wrong-cwd.json`。正确 frontend cwd 的 22 项随后通过，不把错误环境记录算成功。

最终 `production-sidebar-open-window-run08/evidence.json` **7/7、errors=[]、退出 0**。真实 localhost HTTP held/release 建立原任务运行，真实受控 native executable 把打开参数写入文件，WS restore/收据、上传、原生 Monaco 输入及 process cold 均走生产实现，没有注入 lifecycle/running 或预造 conversation。七项互不重复的范围是：完整本地应用选择和首选项；同一 running A 的新窗只 restore、不 create/第二次前台采样；两窗独立内容；新建 C 后真实归档 Home、4002 resync 不复活最初 A；空窗与真实 Ctrl 点击原 row、关闭后不停止原 task；复用已切 B 的 secondary 槽位 fresh 必须 Home、再选 B 恢复内容；真实进程冷启动恢复 Primary A，fresh Secondary 先 Home、用户再点 B 恢复该窗原内容。前台模型任务请求始终只有 **1 次**，另 1 次是 memory auxiliary，不算第二个用户任务。

run08 的 9 张原 PNG 均已实际查看。06/07 的 fresh 和 09 的 cold B 原图保留了初始 inventory/hydration 画面，不把它们冒称稳定完成照片。`production-sidebar-open-window-cold-photo01` 复用同一个原 profile，仅重绑该 QA 的 localhost model endpoint，真正 cold 后等待可见 composer、实际 inventory 和 Monaco 文本，再拍 3 张稳定照片，**1/1、errors=[]、HTTP requests=[]、退出 0**。三图均已查看：`01-cold-primary-visible-draft-attachment-and-unsaved-editor.png`、`02-reused-fresh-home-with-real-inventory-settled.png`、`03-cold-secondary-visible-original-draft-attachment-and-unsaved-editor.png`。没有重跑七个功能，没有发送、编辑或上传；原 artifact IDs 和磁盘 `note.ts` 字节保持。此 companion 重叠于已通过范围，不加到七项计数。

run08 起止 index SHA256 同为 `546dfa4bfc409016e8d347b85d7e184e180cf1d3627f89eb312338f535074cd9`；被测 native/入口文件起止 hash 一致，见 evidence 的 initialHashes/finalHashes。所有本批 QA Electron、localhost HTTP 和执行进程已退出；原临时 profile 保留真实恢复数据。没有操控、保存、清空或重启正常用户窗口、草稿、附件及文件。

## 范围边界

官方账号和 hosted 服务依用户范围排除。当前本地后端 restore 仅支持未归档 main thread，因此未展示 archived/side-chat/remote/ssh 的无 driver 新窗口操作；MiniCode 没有伪造 orbit 或照抄实验 gate ID。独立 app 冷恢复和用户显式 fresh window 记录分开。外部真实 VS Code/Antigravity 窗口不应借受控 executable 结果宣称启动通过；远程 host、archived renderer、customFileHandlers 与整个 Codex/MiniCode 等价性不在本批通过结论内。

**仍必须补全的同功能缺口：跨窗口 public running/过程/完成观察。** run08 的新窗虽然真正 restore A 并且不重启模型，`isStreaming` 仍为 false；其真实 session.restore 没有原执行窗口的 active task/stream，不能交付为已经对齐的运行界面。原响应与完整 conversation/task_runtime 已单独保存 `production-sidebar-open-window-run08/running-window-restore-public-payload.json`，02 图也保留该事实。原执行模型、浏览器与终端 owner 不应移交；后续需由真正后端 observer/fanout 提供 public 运行、过程、完成及必要控件路由，再独立实机验收。未通过的 live 范围不被这七个导航/内容保持结果替代。实际 feature-flags 的 Guardian v2 为 false，不据本批宣称启用分支通过。
