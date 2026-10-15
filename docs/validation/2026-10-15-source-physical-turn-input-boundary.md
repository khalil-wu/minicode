# 同一物理回合中的引导输入位置

状态：后端完整生产调用链已补齐，必要后端检查按节点最新值 57 passed / 0 failure / 0 error / 0 skipped；前端物理回合投影、滚动及实机图尚在衔接，不据此声称全链验收。

## 依据及根因

- Source checkout `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。
- `codex-rs/protocol/src/items.rs` 的 `TurnItem::UserMessage` 是真实有序回合 item。
- `codex-rs/core/src/session/mod.rs:4939–4956` 在保存 prepared conversation items 后，依次发送同一用户 item 的 started / completed。
- 当前桌面端的真实物理回合调用链留存于 `output/playwright/codex-control-audit-20261013/source/steer-physical-turn-chain.json`：用户 item 分隔其前后的 activity，steered 标记本身不渲染，不创建一个新回合。
- MiniCode 之前仅保存独立 user 行和 steer target；聚合 assistant 的有序 blocks 没有该输入的位置。工具时间不能覆盖所有文字与思考，不能按时间或 raw 消息数组次序猜插入点。

## 产品修改

- consumed-steer 的既有 admission producer 先发完真正待发送的 reasoning batch，再形成真实有序边界。
- `commit_turn_admission` 在同一个仓库 generation 保存原 assistant 的 partial projection、`user_input/userMessageId` 引用和原 user 行/context；写入未成功时不改变本地 turn_state，也不发布用户 item。
- 成功后原 turn_state 记录同一用户引用。report 在 receipt / 订阅同步前发既有 `item.started`、`item.completed`；item 类型为 `user_message`，id 为原用户 id，`item.message` 为已经提交的完整 public user 投影，外层仍属于原 assistant / turn。
- 原始提交窗口和正在阅读 B/Home 的订阅者接收同一真实正文、附件与引用，不靠一次额外 fetch 填造输入。canonical transcript 数组不重排。
- stream reducer 对两个生命周期事件幂等保存同一引用；public history 和 frozen resume 页面保留它。引用也隔开前后相同 metadata 的 thinking，避免继续拼接跨越输入边界。
- 现有 partial projection producer 共用同一实际 message builder，保留工具、附件、引用、diff、真实时长和原运行状态。
- 另修真实并发竞争：partial writer 原在 conversation lock 外拍摄 blocks，排在 admission 后拿到锁时会覆盖刚提交的 user marker。现在拍摄与提交都在同一实际 projection boundary 内，不让旧快照覆盖新准入。

## 验证

统一后端批检查 admission、AgentTurnState、public conversation projection、stream state 和 conversation projection replay。首批 55/57 通过，两个既有 narrow caller fixtures 因生命周期批新增实际 `ws_manager` 属性而失败；只补真实 no-manager fixture 并窄复验。后续补入 pending reasoning 顺序证据时，一次 fixture 错把 batcher 的第一帧当待发送帧；已按真实首帧立即发送/后帧暂存语义校正，只复验该节点。

最终结果以 `output/steer-physical-turn-backend-20261015-latest-results.json` 为准，原六批 XML 保留；第五、六批只补真实 partial writer 并发及 journal receipt 确证，未重复整套。新增链证据实际执行仓库和 journal、当前嵌套 admission/report/partial-writer callback、真实 reasoning batcher、turn/stream reducer 和 frozen resume producer，包括：

- user + assistant marker 同一 generation；存储错误向上传播且无假 marker；
- 真实 admission 与 partial writer 先后等待同一锁，后执行的 partial commit/journal receipt 保留已经准入的 marker；并非以 swallowed partial error 当通过；
- 原输入/提交窗口/目标身份和 durable consumption 未改；
- pending reasoning 在用户 item 之前发布；
- started/completed 原 assistant/turn 身份与完整 public user 一致；
- 后续 thinking 不跨过用户边界合并；local turn、live stream 和 follower frozen resume 顺序一致。

这些是受控后端链检查；前端 live/canonical/resume 物理投影和真实桌面 companion 仍须分别记录，不能把此处通过替代桌面行为对齐。
