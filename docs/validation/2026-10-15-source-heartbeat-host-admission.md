# Source heartbeat 调度、真实 owner 和等待语义

实施基线：MiniCode `18e8bd8dc097aa892c76ede818beb15d9b8659f1`，其 CI `38749202449` 已 7/7 通过后才实施本批；Rust 参考为 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。桌面消费者为此前从安装版 Codex `26.1007.9446.0` 保存并核对 ASAR 字节的 main/src 代码，身份与原代码片段见 `output/source-heartbeat-next-readonly-20261015/`。

本批是 H4 后端完整链的交付，不表示整个产品与 Codex 的全部行为已经一致。H1–H3 Guardian、Memory WS 与 frontend observer 分别由其他 owner 实施；本批没有操作用户窗口、认证配置、Git index、commit 或 push。

## 实际改变

- scheduler 在 durable claim 前读取真实目标会话、renderer 资格、用户提问/审批/MCP elicitation、实际 query owner 和 collaboration model。scheduled busy 只持久化下次检查，不创建 failed run，不改 lastRunAt/last run/deleted_at；manual 跳过 renderer gate，但使用同一真实 thread gate，忙时返回 Source 对应错误。
- Source controller 时序为 30 秒 tick、每 tick 至多 3 项、同 target 串行与 lastRunAt 后至少 60 秒的 scheduled 间隔。busy 下次检查按当前 tick 的 wall clock 与下一名义点取最多 60 秒，保留真实 cron/项目存储机制。
- manual/retry 保存真实 `automation_heartbeat_run_now`，scheduled 保存 `automation_heartbeat_scheduled`。逐条 input origin 仍仅后者为 Heartbeat；manual、未知旧 trigger 仍 User，不从 XML 或公开 data 提升来源。
- 原有项目文件 pending claim + schedule cursor 的单一提交点、保存失败回滚、原 run id 的 restart replay、worker/borrower 所有权继续保留。主 host 在 WS drain 开始前关闭准入；关闭、claim 与 start-or-steer 使用同一原 query guard 锁，旧 lifecycle callback 不能在新 host 生命周期开始后再启动。
- query guard 直接绑定真实 RunContext。检查后才出现的 Regular turn 通过原 owner 的同一 durable input queue 接收；WS 与 detached/REST 实际 owner 都支持。没有另造 engine 或 registry，不撤销真人审批，不更换原 turn id、epoch、上下文或 trigger。
- `ScheduledInputCommand` 是实际 scheduler 内部创建的类型。既有 shared durable queue 在独立的 host record 中保存/恢复它；公开 UserCommand 解码不会得到该内部类型。公开消息保存原 task.prompt，内部 model history 使用实际 heartbeat envelope；steered 路径不再独立提交第二份 assistant 或覆盖原 owner 的 snapshot。
- canonical terminal receipt 来自 TurnKernel 实际 durable commit；关联的真实 turn id 保存到 run history。手动服务等待实际 started/steered 回执，30 秒超时仅结束其等待，不取消 worker。
- Source `wu` 的 600 秒计时仅结束 scheduled 等待。若原 turn 仍执行，既有 `_run_tasks` 接续监测其真实 terminal 与 cleanup，run 保持 running，不伪造 completed/failed；scheduled target gate 释放，后续 due 继续经真实 busy 检查延期。真实 borrower 未结束前不提前记完成。
- renderer 两帧沿冻结的 `renderer-wire.md` 接入；接收在普通 client durable/backlog/lifecycle 锁之前。状态绑定实际 session/连接 generation、接收 wall clock，120 秒过期/缺失不阻止；重连移除旧资格，缓存弱引用 reporter，不持有退休 executor。两帧不进入 ACK、离线队列、replay 或聊天。
- task 创建/更新保留 prompt 首尾空白，仅判断其是否非空。安装版 `rI`/更新函数、SQL 和 TOML writer 均保存原 `e.prompt`；原始片段与 SHA 在 `output/source-heartbeat-h4-plan-20261015/prompt-save-evidence.json`。

## NEXT_TURN 的 Source 修正

Rust `core/src/session/turn_input.rs:705` 在实际 active lock 内接收 Regular 输入；`core/src/session/input_queue.rs:368` 同锁追加 pending UserInput 并重新接受当前 turn 的 mailbox delivery。因此已显示 completed assistant item 后的 `NEXT_TURN` 是 mailbox 策略，不等于用户输入已封口。

本批普通人类输入与 heartbeat 共用这个规则：真实 Regular turn 尚未完成时，新输入重新打开当前 mailbox，保留原 turn id/epoch；canonical terminal 或 SEALED 才拒绝。Review/Compact、明确要求但不匹配的 final schema 仍精确拒绝。旧 Mini 测试中把 `defer_mailbox_to_next_turn()` 当成关闭 turn 的期望，已分别改为 Source 的重新接收语义或真正的 `seal_and_drain_commands()`；原 follow-up/附件与 epoch 断言保留。相邻 AST admission fixture 改用真实 RunContext，生产没有为旧 fake 补兼容层。

前后证据使用相同的真实 WS/QueryEngine/provider completed-item 场景：

- 受控 before：仅在独立 pytest 进程加载 `18e8bd8d` 的原始 `TurnInputQueue.enqueue_command` 方法，生产文件不改。普通人类与 scheduled heartbeat 两项均真实失败（2 failures，0 errors/skips）；不是通过项，也不是整个旧版本的全量结果。见 `heartbeat-h4-old-mailbox-negative.xml/.log`。
- 当前原生实现：同两项 2/2 通过。provider 实际发出 `item_completed` 先产生 NEXT_TURN，再准入输入；同一个 engine/turn/epoch 继续，最终 canonical 完成后再提交输入被拒绝。见 `heartbeat-h4-native-mailbox-after.xml/.log`。

## 验证范围与限制

统一批 `heartbeat-h4-batch03.xml`：104 passed，0 failures/errors/skips。之后按实际新增的 owner 边界和 Source queue 修正，仅验证受影响集合，所有原失败节点均有对应最终通过结果；最终去重结果与每批原始失败记录见交付目录的 `validation.json`。

新的 `test_source_heartbeat_host_admission.py` 包括 20 个参数化场景：实际 WS 与 detached owner 检查后竞态、真实读取文件/模型安全边界、冷 history/origin/公开 prompt、durable host input 冷恢复、manual busy/start ACK/等待超时、scheduled 等待超时与真实 borrower/下次检查、同 target/maxPerTick、关闭/重开旧 callback、真实 session 请求 lanes、renderer restore/reconnect/TTL、精确 prompt 保存，以及 provider completed item 后的人类与 heartbeat 输入。

其中 Review/Compact/schema/SEALED 四项是实际 shared core/queue 的拒绝契约验证，不宣称覆盖所有 review/compaction UI。provider 上游为受控 adapter，wire 回归使用本地真实 HTTP；不宣称验证了外部模型服务、计费或完整桌面截图流程。跳过与失败不计通过。最新组合提交的完整 CI 仍由 Root 统一运行，本批局部结果不能代替全量 CI。

已有 input_origin 旧范围没有重新实现；相邻回归仅加强 manual 的真实 run_now wire 与保留 User origin 的断言，不把此前 176 项当作本批新增能力。

交付 manifest 是冻结文件的 `path`/`sha256` 数组。后续 R0 的 thread/attachment owner 工作必须在本批 checkpoint 之后推进，避免把新范围混进同一份交付。
