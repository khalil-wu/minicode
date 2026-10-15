# Steer 消息的桌面投影

Root 复看实际 `production-model-selection-next-turn-run02/02-real-steer-and-tool-round-stay-on-admitted-model-a.png` 时，发现 MiniCode 在已准入的 steer 用户气泡内额外显示箭头和“已引导当前任务”。这不是既有运行成功证据能覆盖的视觉一致性结论。

本批重新读取当前保存的桌面 bundle。`sites-end-resource-873a414616c6.js` 的 `user-message` 分支只为真实 hook block、goal、hook feedback 生成相应状态；它对 `steered` 明确返回 null。`generated-image-output-visibility-f9fca42e7eb1.js::M` 在渲染前过滤 `steered` 内部标记，真实 steering user message 的文本和附件仍保留。精确 offset、片段和 SHA256 保存在 [Source 证据](../../output/playwright/codex-control-audit-20261013/source/user-steer-message-projection.json)。只读取参考代码，没有执行参考应用脚本。

已移除 `UserMessageCell` 中额外的已接受 steer 状态与 `CornerDownLeft` 图标；纯附件的 steer 也不会因内部标记产生空文字气泡。保留真实准入、队列、`steeredIntoMessageId`、原消息/回合归属和持久历史；不把真实输入或错误从记录中抹去。字体、字号和字重未改。

必要验证与下一次前端批合并：沿用并修正 `UserMessageCell.test.tsx` 中的既有 steer 和纯附件场景，连同 `InputDisplay.chain.test.tsx`、workspace 成功 toast 移除的 `noticeEvents.test.ts` 统一运行。实际三窗口 follower steer 场景另外核对原文本、原上传文件及模型实际收到的输入，不重复之前闭合的 Enter 或模型切换套件。在最新构建和实际任务结果产生前，本报告不记验证通过。
