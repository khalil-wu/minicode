# Desktop voice capture and transfer consumer

本批补齐 `VO-CAPTURE1` 与 `VO-TRANSFER1` 的 MiniCode 本地客户端调用链。行为依据为 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 与 Windows Codex Desktop `26.1016.6573.0` 的只读摘录，原始定位见 `2026-10-21-voice-session-tools-source-gates.md`。没有操作 GUI、computeruse、CDP 或 Playwright，没有新增普通 UI 控件。

## 原始所有者与工具发现

桌面主进程现在持有唯一 voice claim，绑定实际主窗口记录、host、thread 与随机 claim ID。renderer 只有在后端 start command 接收成功且收到真实 realtime started 后，才能把该 claim 标为 active。原 claim 的 release 不得清除替代 call。

`desktopVoice` 是 MiniCode 的私有 start envelope，由 WS handler 在验证公开 Source Start 参数前分离；它不进入 Source Start schema、模型提示词、线程 durable context 或 ToolsetPolicy 的公开 mapping。后端服务保留原窗口绑定和私有 native client。模型工具与结束工具共享真实线程 runtime、store、main agent 和 captured service identity 门控，普通文本、嵌套工具和子 agent 不获得这些工具。没有实际 desktop client 能力时，capture/transfer 不进入发现目录；目录 cache key 纳入这些实际能力。

后端请求复用原有 token 认证桌面 HTTP bridge 和原窗口 binding。`/v1/voice-control` 在调用 native manager 前仍核对原窗口、owner、binding token 和远端 host token。模型的真实 tool call ID 随原请求贯通至 renderer；错误的窗口、claim 或 call ID 不能提交结果，已取消或结束的请求拒绝迟到回包。

## Windows 当前页面上下文

只在原 voice active、没有正在转移且 MiniCode 实际前台时接受 capture。主进程在 renderer await 前后都核对同一真实前台窗口、原 active claim 与 transfer 状态。转移一开始即撤销已在等待的 capture，目标 preflight 期间也不接受新 capture。前台是其他应用时返回真实不可用错误，不截图浏览器页面或其他应用作为替代。原通话停止、窗口退出、reload、renderer gone、WS 断开以及原 HTTP 请求取消均撤销对应请求。

renderer 返回当前窗口的 Source summary：`page`、该页的 `thread.id/title`（nullable title）、`rightSidebarVisible` 与已挂载的 `rightPanelTabs.type/focused`。页面和线程来自实际 store，侧栏来自已渲染 DOM；紧凑右侧栏同样按真实 `data-open` 与 `aria-hidden` 处理，普通 tab 和 portal browser tab 的 focused 都取实际 `aria-selected`。单纯闭合侧栏不删除其仍挂载的 tab。相关纯投影实现和 11 个用例由主 agent 完成。

## Transfer 与 return

模型仍使用 Source 的目标 `threadId`、可选 `hostId/context`，或者 `return: true`。context 上限为 4000 字符；rtc call ID 不作为转移参数。未指定 host 时沿用原 host，连续显式转移保留最初的 previous locator，return 使用并清除该 locator。

实际顺序为：

1. native coordinator 解析目标 locator；跨 host 使用已有 RemoteDesktop 真实连接/runtime 和实际窗口。
2. 目标 renderer 使用其本身已连接的 WS session，读取其本身 backend 的 thread metadata voice API；核对目标存在、未归档、实际 realtime 配置与媒体支持。检查失败保留原通话。
3. 原 renderer 关闭实际 RealtimeAudio，随后发送原 thread 的 realtime stop；只有真实 stop command 回执完成，才释放原 native claim 并回复 `stopped/mediaReleased`。音频 close 失败保留原句柄和 claim，成功重试前不报已释放。
4. 目标 renderer 通过既有 `requestConversationSwitch` 等待真实 `conversation.switched`。不提前改写本地 active conversation，因此现有 snapshot/restore 路径保留原窗口或隐藏工作区的草稿、附件与状态。
5. 目标以 transfer claim 启动真实 realtime；context 按参考桌面端语义作为 initial developer item，使用 MiniCode 产品名。普通 start 仍保持原有 command acceptance 语义；transfer 额外等待 command acceptance、started 与 native active claim。
6. native coordinator 复核真实目标窗口、host、thread、active claim 及原请求 signal，随后才返回 `transferred: true`。

目标检查、媒体释放、stop、会话切换或目标 start 的失败都保留真实失败，不返回转移成功，不替换后端线程 owner，也不自动重新启动原通话。原窗口退出会取消正在等待目标 ready 的转移；原 HTTP 请求断开会取消尚未完成的 renderer 操作。

## 本次集中验证

最终有效收据保存在 `output/full-parity-20261021/`：

| 范围 | 结果 | 收据 |
| --- | --- | --- |
| Native manager、实际 preload/IPC、认证 HTTP、现有安全/多窗口回归 | 63 / 63，通过；0 skip | `voice-native.xml`、`voice-native.log` |
| 实际 controller/consumer、canonical switch、语音投影与页面 summary | 27 / 27，通过；0 skip | `voice-frontend.json`、`voice-frontend.log` |
| Source realtime、真实线程/模型工具、loopback native HTTP、目标 preflight API | 30 / 30，通过；0 skip | `voice-backend.xml`、`voice-backend.log` |
| TypeScript build typecheck、Node syntax 与 git diff whitespace | 通过 | `voice-typescript.log` 与机器回执 |

测试覆盖真实生产代码的消息、HTTP、IPC、所有者和生命周期路径；Node 测试使用可控主窗口对象，前端媒体设备使用可控音频实现。因此这些通过不等于物理麦克风、真实 provider 的双向语音、实际 OS 前台切换或实际远端机器验收。本批没有运行这些设备/外部集成验收，也没有把未运行项算作通过。官方账号接入不在本批范围内。
