# Desktop 语音工具的真实客户端门控

本记录只读核对公开 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c`、原冻结 Desktop `26.1013.4894.0` 的 retained bundle，以及当前安装 Windows Desktop `26.1016.6573.0` 的 ASAR。未运行 bundle、操作 GUI、截图其他应用或调用 computeruse。当前安装包的精确文件、UTF-16 范围和摘录见 `output/full-parity-20261021/voice-current-readonly-manifest.json` 及同目录 `voice-current-*.raw.txt`。

## 目录与结束工具

原 retained `app-initial-5851df3f6cb1.js` 的 `ver()` 和当前 `app-initial-2b6b8063022d.js` 的 `sir()` 都按 active voice 能力和客户端 flags 生成动态工具。end/transfer flags 的 parser 默认 false；当前 voice route 在允许时提供 deferred 工具，不能推成普通文字会话的常驻工具。

`end_realtime_voice_call` 的 schema 是空对象，Source 描述明确只有用户请求结束语音时才调用。停止工作、停止说话或暂停不等同结束通话。MiniCode 的模型 consumer 已实现并验证，完整证据见 `2026-10-21-realtime-source-chain.md`。

## 当前页面上下文

当前包 `app-initial-2b6b8063022d.js` 中 `Ghu()` / `Yhu()`（摘录范围 12577341–12581111）先核 active voice 与原 host/thread locator。macOS 还核用户 Voice chat 设置中的 screenContextEnabled。只有主 Codex 窗口前台时才走 `app.get_summary`，返回：

- page：thread、home、settings 或 other。
- 当前 thread 的 id、title（该页属于 thread 时）。
- rightSidebarVisible。
- rightPanelTabs 中的 type、focused。

Windows 在 Codex 不处于前台时明确返回外部应用不受支持；不是任意桌面截图 consumer。macOS 外部应用才走受现有 Appshots 用户开关控制的前台 screenshot + accessibility consumer。execution bundle 保留原 capture callId；host 会跨 await 再核原 claim、cleanup、transfer 和 active 状态，拒绝迟到结果。

MiniCode 已补齐同等 Windows client-owned summary consumer：原active voice claim、host/thread/window/call ID门控与await前后真实前台复核，经已认证native bridge取得当前页面、线程及实际侧栏/标签摘要。内嵌浏览器 `capturePage()` 仍是独立能力，未被当作该摘要，也没有增加截图按钮。本批本地链路与未运行的设备/上游范围见 [实现与验证报告](2026-10-21-voice-desktop-control-chain.md)。

## 通话转移与返回

当前 `execution-d04ca1d0b929.js` 的 `Tm()`（103490–105764）为 transfer 验证 schema：目标 threadId、可选 hostId/context，或者 return=true、可选 context；context 最多 4000 字符。它调用 `voice.transfer(originalLocator, destinationOrNull, context)`；transfer 不传 rtc callId。原 host 是未指定目标 host 时的默认值；真实返回 false 保留「没有此前目标」或「不能转到目标」的错误。

`main-CT5a1yCd.js` 的 `TKe.transfer`（2805329–2806283）要求原 active locator 且没有 cleanup/orbit。return 使用保留的此前 locator；接收转移后发 transfer control，携带目标 locator/context。renderer 的 host-control consumer 调用 `onTransfer`。

实际 stop→start consumer 位于 `avatar-overlay-native-page-7d29dfe41d37.js`（69134–69881）：先解析目标/展示 surface，durable 目标需加载目标并确认原 source 仍当前；需要时打开目标主窗口；再 stop 原会话（playEndSound=false、preserveGoal=true），随后 start 目标（isTransfer=true、handoffContext、目标 locator、原 productExperience、目标 host 能力）。错误清理 transfer 状态并保留真实错误。

因此 transfer 是 Desktop owner coordinator→原 voice stop→目标 voice start 的完整客户端链，公开 app-server 没有同名 transfer 方法。MiniCode 已补原locator/目标解析/return、原媒体释放与真实stop回执、canonical会话切换、目标started及原claim复核，不靠更换后端service owner实现，也未新增普通UI入口。原目标检查或媒体/stop/start失败继续保留失败，不虚报转移成功。最终native63、frontend27、backend30项通过，0 skip；真实设备、外部provider及外机验收单列于 [实现与验证报告](2026-10-21-voice-desktop-control-chain.md)。
