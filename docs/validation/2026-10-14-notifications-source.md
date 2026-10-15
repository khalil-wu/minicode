# Codex Source 通知请求生命周期

参考保存的 `app-initial-b8e1c8014bb4.js::ylc/clc`、`notifications-settings-5f0e05f81547.js::lt`、`app-shared-737655e1fb23.js` 通知 settings 定义。三个偏好的原默认为 `unfocused / true / true`，对应 MiniCode `onlyWhenUnfocused / permissionNotifications:true / questionNotifications:true`。

## Source 依据与实现

`clc` 内部 `h(conversationId,kind)` 在偏好关闭或 **windowFocused && conversationVisible** 时抑制权限/问题系统通知。请求仍在当前界面中等待回答，并不是请求失败后被隐藏。MiniCode 原先只检查偏好 bool；现在 control producer 保留真实 requestId、conversationId 和已有 turn/message/task owner，向 native service 发送实际可见性；native service 使用真实 BrowserWindow.isFocused()，未用 document.hasFocus 冒充窗口焦点。

可见性消费既有会话选择、Settings、pending conversation switch、document visibility 与 `.chat-pane` 实际隐藏祖先。Home 无当前 conversation、旧 A 正在切 B、Settings 替换阅读区以及挂在 Code 编辑器后面的 chat 都不会被当成正在看的请求。界面中的审批、问题、草稿和真实报错不改。

Source `serverRequest/resolved` 按 request ID hide；`ylc` 在查看会话和重新聚焦时 hide 对应 conversation。MiniCode 的实际正常响应在 `backend/ws/command_dispatcher.py::_handle_control_response` 验证 owner、接受 response 后发 `command.result success`；正常 response 不会发 `approval.cancelled`。因此此次接完整链：

- `sendPromptResponseCommand` 等待真实 semantic success 后，按原 request/conversation/turn/message/task owner hide，运输 ACK、error、断线不 hide。
- `controlEvents` 的真实 `approval.cancelled` 按 conversation + requestIds hide，不取当前会话代替原 owner。
- 主进程保存实际 Notification receipt；同一真实请求不重复 show，旧 turn 的迟到 ACK 不能关闭同 ID 的新 turn。原生 close 删除 receipt。
- 原生 window-manager focus/blur 广播既有 window state 的 isFocused；已有 getState API 同步读取真实 native focus。useDesktopEvents 消费这条事件，并以 main-process `onlyIfFocused` 再核对对应 native owner，再 hide 当前实际阅读的会话。Native Browser child view 占 DOM focus 时仍可正确处理。
- preload、runtime、IPC handler 和 IPC_CAPABILITIES manifest 一并更新；没有新增 OS 审批快捷按钮或自动决定权限。

## 验证

Native/IPC/window owner scope 按节点取最新结果：**50 passed，0 failed，0 skipped**，其中新通知链 10 个节点。首批 `output/notifications-source-native-20261014-batch01.log` 为 49 passed / 1 failed；唯一失败为旧 Window fixture 缺少实际 Electron 的 isFocused 方法。补真实 `focused===this` 与原 owner 断言后，仅重跑该节点，`output/notifications-source-native-20261014-fixture.log` 为 1 passed。新增文件当前复验 `output/notifications-source-native-20261014-current.log` 为 10 passed；该 10 项包含在 50 中，不相加。

Native scope 包含真实 MiniCode service 函数、真实 IPC handler/capability/preload、真实 window state publisher；Notification OS 接口被明确的测试 EventEmitter 截获，使用独立临时偏好目录。没有在正常用户 profile 上发系统通知，没有把 VM capture 当作 Windows 通知中心实际投递证明。

前端新增 `chat/desktopNotifications.chain.test.tsx`，并扩 `protocol/ws-outbox.test.ts`、补 `hooks/useDesktopEvents.test.tsx` 的实际 window contract。并行 desktop agent 的统一设置/浏览器/通知前端范围按文件、fullName、同名 case ordinal 取最新结果为 **312 passed / 0 failed / 0 skipped**，明细为 `output/playwright/codex-control-audit-20261013/settings-select-latest-results.json`。首轮 `settings-select-integration-unit01.json` 为 302 passed / 10 failed；三份旧夹具分别为 SettingsCenter 的四项 trigger/option 与 browser subscription 契约、BrowserPanel.nilOwner 四项 subscription 契约、FullAccessVisibility 两项 title/真实 availableModes 契约。保持原行为断言并修正后，仅重跑三个文件的 `settings-select-integration-unit02.json` 为 73 passed，不能相加成 385。此较广范围与别的前端报告重叠，不能累加。

首次 tsc 另发现 `ControlResponseCommand` 漏写了实际已有的 `task_id`。真实 producer 是 `protocol/prompt-responses.ts::ownerFields`，`RequestUserInputCard` 将 request.taskId 传入；backend `UserCommand.from_ws_message` 把全部非 type 的 envelope 字段原样保存到 command.data。仅给这个回复类型补可选字段。`ControlCancelRequestCommand` 的实际 producer 没有 task_id，通知 helper 按 command.type 收窄，未给取消命令增加假字段。后端审批 guard 仍消费 conversation/turn/message/digest，task_id 是已有前端 prompt/notification receipt 身份，不声称新增了后端 task 授权校验。

修正后 **tsc + production Vite build 成功**，日志 `output/playwright/codex-control-audit-20261013/settings-select-production-build02.log`，Vite 8.10 秒。最新入口 bundle 为 `index-BYefmvJz.js`；index.html SHA256 为 `39ce462df98d4e836be42569e04f7e6db43fdaef86c0c885bf90d2fa8f1f9c9c`，2026-10-14 20:56:17 local。统一结果不把已通过的 312 项重复全跑；实际 production 界面截图由 desktop agent 在独立 profile 验证，不在这里提前计作完成。

## 明确保留的 Source 差距

此次没有根据“失败 / 无文本”编造完成通知过滤。Source `clc::f` 还检查 threadStartKind/threadSource、内部 agent、remote task、hasPendingContinuation、chatgpt_hidden/dreaming 等；`tlc` 验证 durable root thread。`ulc` 仅在 automation/heartbeat DONT_NOTIFY，或文本解析为 skipped completion 且 status=completed 时跳过；其他无文本情况也可返回真实 Source 的默认完成文案。

后续同日 [完成通知、自动未读与完整控制标记](2026-10-14-source-completion-unread-chain.md) 已补真实 done 来源、最终正文、待续跑资格、Source heartbeat/notification policy、终态原子未读、实际阅读清未读与完整控制标记显示消费。请以该报告的最新生产实现、受影响验证与未完成实机状态为准；本文以上记录是较早的请求通知阶段，验证集合不可直接相加。Codex 的 durable cloud root/account 校验、远程/共享账号资格、OS 请求快捷审批和通知直接回复仍未在本地服务中闭合，不将这些外部链路算作通过。
