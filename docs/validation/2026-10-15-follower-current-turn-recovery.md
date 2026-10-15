# 2026-10-15：跨窗口 current turn 恢复与真实执行验证

本记录只确认下列五条功能链。它不表示 MiniCode 已与 Codex 桌面端整体相同，也不覆盖关闭原执行窗口超过断线宽限期的生命周期行为。

## 根因与修复

真实 HTTP/Electron 轮次 `production-follower-live-run04` 中，原窗口与 follower 都收到相同本轮 `message_id`、`turn_id` 的真实 `read_file` 调用及成功结果，`source_event_seq` 分别为 10、16、22，界面却没有本轮 assistant。

历史中保留着前轮的真实 checkpoint：`terminal_status=partial`、`termination_reason=run_in_progress`、`duration_ms=22520`。会话级 `task.update` 先于本轮 `agent.run.started` 到达。原实现仅凭会话运行集合复活最近的旧 checkpoint，随后 run-start 与 resume 的 other-stream guard 把新轮拒绝了。

修复范围：

- `frontend/src.v2/chat/sessionEvents.ts`：会话运行集合只更新会话 busy 与已知 live/精确 steer target，不再任选历史 checkpoint、清除其真实 partial 元数据。新轮边界排除未准入、取消的队列输入与绑定原 turn 的 steer 输入。
- `frontend/src.v2/chat/runtimeEvents.ts`：真实、非 replay 的 main run-start 按其准确 message/turn 建立当前 assistant，旧 renderer streaming flag 不能阻挡它。
- `frontend/src.v2/chat/chatStreamEvents.ts`：准确 resume 可以承接新轮；已有较新用户轮、较新 live assistant、旧 turn、终态与过期 snapshot 仍受原边界约束。同轮 steer 不被当作新轮。
- `frontend/src.v2/stores/chat-slice.ts`：准入准确当前 assistant 时，只收敛其它 assistant 的过期 streaming flags，保留历史 partial、duration、termination reason、blocks、工具结果。

`run05` 的真实附件 steer 又揭示一个必要边界：后端的 canonical transcript 会把 steer user 追加在其原 assistant 之后。仅排除 queued/cancelled 仍会把同轮 steer 当作新轮。两处边界现在都依据真实 `steeredIntoMessageId` 排除它；没有修改后端存储顺序或伪造取消原因。

## 必要自动检查

最新记录：

- [必要链测试 batch05](../../output/playwright/codex-control-audit-20261013/current-turn-recovery-unit-batch05.json)：8 个文件，239/239 passed，0 failed/skipped。
- 其中新 `currentTurnRecovery.chain.test.ts` 共 9 项，覆盖 active/background run-start、真实 session sync 的旧 partial、`task.update → run-start` 原始顺序、steer 追加顺序与附件、旧 snapshot/late done、queued/cancelled 输入。
- 原有 reload、分片 resume、side-chat selection 与相关 runtime/session/chat 投影测试均包含在该批中。
- [TSC/Vite build03](../../output/playwright/codex-control-audit-20261013/current-turn-recovery-build-batch03.log)：exit 0。
- [最终 UI debt](../../output/playwright/codex-control-audit-20261013/current-turn-recovery-ui-debt-final.log)、[最终 bundle budget](../../output/playwright/codex-control-audit-20261013/current-turn-recovery-bundle-budget-final.log)：均 exit 0。

更早的统一 11 文件 312/312 记录在 [follower-sort-final-unit-batch01.json](../../output/playwright/codex-control-audit-20261013/follower-sort-final-unit-batch01.json)。它和本批存在重叠，不能相加作为唯一用例数。没有重跑已关闭的 Enter、七窗、设置、正常模型选择或排序实机场景。

## 实际构建身份

最终 [build identity](../../output/playwright/codex-control-audit-20261013/current-turn-recovery-build-identity-final.json)：

| 项目 | 身份 |
| --- | --- |
| entry | `assets/index-EzUkoIfX.js` |
| index SHA256 | `3da662e5dbd01f49c143266350a0fc0cf28b7a37a2be9efe55b49be78a45c809` |
| entry SHA256 | `2fcdb2b5edaa62ebf1f864bbb59f0d897e1250d145859696cdb58af5680326e9` |
| CSS | `assets/index-DuSpLBS9.css` |
| CSS SHA256 | `6db148bac3182dff147c275aec3a35baf125cbf1eb4a2883dce8c3f753855e80` |

该构建包括 Root 的 10 文件 debt 尾批。steer 边界修复前后的 CSS 逐字节相同，没有本批字体、字号或样式调整。

## 真实三窗功能链

使用原保留 QA profile `minicode-window-profile-jzqEJn`，没有重 seed 历史、草稿、上传或编辑器。原窗口执行 A，并在运行时真实切到 B；第二原窗口保留自己的 B 与隐藏 A 内容；新 observer 用自己的空输入区域发送附件、steer、queue 与 Stop。

所有任务均经原生已鉴权 renderer WebSocket、真实 backend admission/harness 与 localhost HTTP provider。读取来自真实声明的 `read_file`，上传来自实际文件输入与服务端 artifact。没有注入 store、模拟 running 或手造 lifecycle 事件。

| 功能链 | 实际证据 | 结果 |
| --- | --- | --- |
| 新窗恢复运行、真实 read_file、相同 final、默认过程折叠 | [run05 evidence](../../output/playwright/codex-control-audit-20261013/production-follower-live-run05/evidence.json)，entry `index-CLf2Pdyu.js` | 通过该窄功能断言 |
| 附件 steer 归原 A owner，原窗口保持 B | [run06 evidence](../../output/playwright/codex-control-audit-20261013/production-follower-live-run06/evidence.json)，entry `index-EzUkoIfX.js` | 通过执行、状态、输入与 artifact 保留断言 |
| 附件 queue 仅增加一个预期 follow-up run | 同 run06 | 通过 |
| observer 选择 B 再到真实 Home，A 过程/final 只更新后台记录 | 同 run06 | 通过 |
| observer Stop 使用原 message/turn fence，原 HTTP 被取消，原窗口保有原 PTY/browser | 同 run06 | 通过 |

run06 为接续轮：4/4 passed、`errors=[]`、exit 0。其 `notRepeated` 明确引用 run05 已通过的第 1 项；没有把省略项计作 run06 通过。run06 所有执行产品及所载构建的前后 hash 一致。

run06 的前台 HTTP 样本为 steer 2、queue owner 1、queue follow-up 1、background 2、Stop 1。Stop 的那一个原请求实际 abort，没有新增采样。

原 A/B unsent draft、原 artifact IDs、隐藏工作区草稿、两个 dirty Monaco buffers、`note.ts` 磁盘 bytes 与既有 partial 的 content/blocks/duration/termination reason 均逐字段保留。结束后 B 默认权限已恢复，自有 Electron/backend/HTTP 均退出。

原与 follower 的 live 工具事件保留相同 `source_event_seq`，每窗保有独立 outbox envelope。终态的 `source_event_seq` 是已有可选字段：存在时核对一致，缺失时核对原样缺失；终态另核对真实 message/turn/task/status，没有为 QA 添造后端必填合同。

## 图片审阅的边界与已发现余项

run05 的 3 张成功图片、run06 的 4 张图片及前面失败轮的图片均已逐张查看。不能仅凭上述 store/执行断言宣称完整视觉通过：

1. **恢复计时尚未传递真实起点。** run05 原 `started_at=1792041671198`，observer assistant 的 `timestamp=1792041681171`。05:21:21 的截图实际已运行约 10 秒，却显示“处理中”；05:21:25 显示 3 秒，最终按真实 duration 跳到 13 秒。需要从原执行 run 的真实起点贯通 resume，不能把本地观察时间当起点。
2. **steer 的最终显示归属需要继续对照。** run06 的 steer 输入与真实附件在 DOM/界面中保留且无额外 steer 标记，但 canonical 追加顺序目前使 final 位于它之前。`chatSurfaceState` 的相邻 user/assistant 配对还没有用这个同轮关系给出完整显示证明，不能写成 Codex 顺序已相同。
3. **queue/Stop 图片未定位到最新 turn。** 当前图片拍到保留的旧阅读位置；raw wire、状态与 owner 断言是真实通过，当前 turn 的最终视口仍需用真实界面操作定位、检查。这些图片不是新 final 的视觉验收。

Root 已要求后续同批补真实时钟合同，并继续核实实际 Source 的 steer/阅读位置行为。这些工作不被本报告写成完成。

后续记录见 [原执行时钟与物理 turn 投影](2026-10-15-source-physical-turn-clock-projection.md)：新 `CC81C9Cg` 的真实 companion run02 已窄验原始 clock 与两次读取之间的原 user item/附件；上翻后的真实内容 anchor 仍在 run03/run04 复现漂移，已取得 DOM 尺寸根因并补源修，待同批构建后复验。原五项功能链不因此被重跑或扩写为完整视觉通过。

最终 `BT3XeGQn` 的 `production-physical-turn-clock-run05` 已只对该未通过的阅读/final 节点复验成功：旧正文屏幕 anchor 前后均 `132.546875px`，三层真实 duration 均 `44687ms`，输入/原附件先于同一 final、过程默认折叠且可真实展开。完整原始失败、构建、图片与范围限制保留在该后续记录；没有把 run02 或 run05 扩写为整个 Codex 桌面端全等。

## 保留的失败记录

| 轮次 | 实际结果 |
| --- | --- |
| run01 | QA setup 的 localhost native browser 触发真实私网审批，尚无 provider 场景。保存只读两窗状态与图片后正常关闭；资源归属验证改为真实 `about:blank` 页面。 |
| run02 | QA marker 没剥除实际 `<system-reminder>` 前缀，误回辅助文本，未形成预期 held 请求。原记录保留。 |
| run03 | QA 在有序 `session.synced/stream_resume` 到达前读取数组；后续原始 wire 已记录正确 resume。修为等待真实 resume。 |
| run04 | 真实产品缺陷：历史 checkpoint 被错误复活，当前 assistant 的公开工具行与 final 未投影。由本批根因修复。 |
| run05 第2项 | 真实 steer 追加输入被误当新轮，第二次 final 未投影；后续同轮身份修复后 run06 对应项通过。 |

所有这些失败轮、旧批测试错误与日志保持原样，没有重命名成通过，也没有通过覆盖/清空原 profile 消除触发条件。

## 排除范围

官方账号与 hosted 服务按用户要求未做。没有操作普通用户窗口、profile 或剪贴板。关闭执行 owner 超过宽限期、多窗口完整生命周期全等、全部桌面 UI/动画全等，均不能由本次五条窄功能链推断。
