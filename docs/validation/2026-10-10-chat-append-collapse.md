# 追加消息与完成折叠 · 2026-10-10

## 原因与修复

- MessageList 将最新轮次放在独立容器，追加后将它移动到历史列表。React 会卸载旧回复并重建，破坏正文 DOM、文本选择与局部状态。近期八轮现在处于同一个按轮次 ID 标识的列表；较早记录继续虚拟化。新消息只追加，最新轮次仍保留回复视口和发送后的定位。
- `verbose` 模式同时控制信息详细程度与完成后的展开，阻止最终答案触发收起。本地设置记录存在 `verbose`，两条真实会话的完成投影均具有 `model_final/completed/isStreaming=false`。现在三个显示模式都以完整最终答案为自动折叠边界，保留用户主动展开；失败、中断、部分完成仍显示证据。
- 对照本地 Codex `app-server/src/request_processors/turn_processor.rs` 的 `turn_steer_inner` 与 `bespoke_event_handling.rs` 的 item 完成通知。此次没有修改后端消息存储或轮次协议。

## 验证

- 7 个相关测试文件共 **162 项通过**，0 最终失败/跳过。覆盖 1、9、12、40 轮下追加后的 DOM 身份、历史虚拟化边界、展开选择、滚动定位、三个模式的 item 完成与 done、失败/中断恢复、会话事件和增量投影。
- 首批 161 通过、1 失败：旧滚动 fixture 给原尾部容器设置几何位置，依赖该容器被复用。改为给新轮次设置几何位置后，MessageList 29 项全部通过；其他 6 个文件的 133 项不重复计数。
- 独立 Playwright 浏览器加载当前源码，使用实际会话持久化内容回放 `item.completed` / `done`。确认 detailed 模式自动折叠、追加后原轮次/正文节点未更换、正文内容与草稿不变。结果为 `output/playwright/chat-append-result.json`，截图为 `chat-working.png`、`chat-completed.png`、`chat-appended.png`。这是真实内容的事件回放，未重新请求模型。
- 浏览器首个正文断言错误地将 Markdown 标记与渲染文本比较；修正为追加前后渲染文本及存储原文分别比较后通过。旧 DevToolsActivePort 探测失败，实际运行实例没有开放 CDP；未重启或操作该窗口。
- `tsc -b`、desktop-release 构建及 `git diff --check` 通过。已将前端构建同步至当前 unpacked 程序资源目录；保留旧资源以服务仍打开的窗口，不强制刷新，重新打开程序加载新版本。

用户的 `design.md` 既有修改、会话、草稿和编辑器数据未改动。
