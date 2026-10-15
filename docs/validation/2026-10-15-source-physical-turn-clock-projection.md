# 2026-10-15：原执行时钟与物理 turn 的输入投影

本批仅补真实原始开始时间、已经准入的同轮用户 item、分段过程与阅读位置。前五条 follower 功能链的已有结果仍见 `2026-10-15-follower-current-turn-recovery.md`，不重复计作本批验收。

## Source 依据

- `output/playwright/codex-control-audit-20261013/source/steer-physical-turn-chain.json`：`conversation-turn-mutations` 的 `bn/Nn/Rn/K` 绑定真实 target turn 与有序 items；`generated-image-output-visibility` 的 `M/N/F` 仅过滤不可见的 steered marker，每个真实 user-message 分隔前后 activity，仍在同一物理 turn 中。
- `local-conversation-turn` 的 `Lc → M/F → as` 渲染同一 turn 中的有序 user/assistant blocks。用户输入不能替代原 response 的最新阅读与跟随目标。
- `output/playwright/codex-control-audit-20261013/source/worked-for-clock-chain.json`：`nxr` 在原始执行开始未知时不构造 worked-for；`fwr` 保留 raw duration，用原始开始加真实 duration 表示结束，不能用 viewer 的接收或恢复时间替代。
- 后端用户准入与仓库顺序依据 `.tmp/codex-src/codex-rs/core/src/session/mod.rs:4939–4956`，沿用真实 user TurnItem started/completed，未添加新的 lifecycle 事件名；详见 `2026-10-15-source-physical-turn-input-boundary.md`。

## 前后端衔接

后端原 main `agent.run.started.started_at` 经 original/follower frozen resume 与各页保留；未知则省略。前端将其保存为独立 `runStartedAt`，仅用于原 assistant/turn 的实时耗时，0 与小数精度均保留。普通 message creation、transport timestamp、viewer restore 以及完成观察时间不能补造运行起点。

完成态使用后端原始 `duration_ms`，没有 `completedAt - startedAt` 的替代推算。现有整数秒显示、未满一秒不显示耗时的规则未变，raw history 保留真实精度。

consumed-steer 使用后端真实 `item.started/item.completed` 的完整 public user body、原 user ID、artifact ID、context refs 和 quote。原 assistant 的有序 blocks 中只有 `user_input/userMessageId` 引用；其对应正文仍是 canonical transcript 中的真实 user 行。两个 item 事件幂等保存同一个引用，不重排 canonical 消息，不凭工具时间推算插入点。

纯界面投影把原 assistant 的内容分成 `process → user → process`，多个用户输入保留各自原始边界与同一 final。输入文字、附件在完整 final 到达后仍可见；过程自动折叠，用户主动展开或正在阅读的选择仍按原 turn key 保留。子 agent 工作记录不产生主用户消息气泡。工具图像、独立 terminal error 与最后的 diff 仍各自保留一次。

queue/dequeued 与 pending snapshot 仅清理真实队列状态、保存 target metadata；不再通过将 user 行移动到 assistant 前面改变 canonical 顺序。新轮定位排除已经绑定同轮的用户输入，实时增长跟随原物理 assistant。手动上翻仍阻止自动跳回。

## 必要检查

- `source-clock-interleave-unit-batch01.json`：12 个真实文件，331/331 passed、0 failed/skipped。包括新物理 turn 链首 14 项与 MessageList 的 2 个底部/上翻阅读项；其余是本批受影响的既有 session、hydration、协议、composer、投影与计时测试。
- `source-clock-interleave-protocol-batch01.log`：协议同步 exit 0，既有 item union 的前后端变化一致。
- 首次 `source-clock-interleave-types-batch01.log` 保留了一个实际 ErrorCell filter 的 TypeScript 推断错误。显式标明真实 ErrorCell 谓词后，`source-clock-interleave-types-batch02.log` exit 0。没有通过添加伪造时间或宽泛类型跳过它。
- 后续代码检查发现：只有 marker、正文行尚未载入时，旧单 cell fast path 会更新 aggregate cells 却留下 segmented work 的旧 receipt。本批将真实 input boundary 作为分段投影边界；仅对这个新增问题窄复验，不重复跑前五条实机功能链。
- `source-clock-interleave-unloaded-input-batch02.json`：该新增边界和物理 turn 链 15/15 passed、0 failed/skipped。它覆盖并替代首批该文件的 14 项，不能与 331 相加宣称唯一用例数。
- 实机发现阅读问题后，`source-clock-interleave-reading-root-batch03.json` 对 3 个实际受影响文件统一运行，91/91 passed、0 failed/skipped；其中物理 turn 文件现为 18 项。新增真实 pending tail、hidden queued、cancelled、steered 与普通旧 unpaired user 的身份边界，旧 user 在 live/settled 间复用同一无虚构过程的投影。本批与之前文件重叠，不能相加为唯一用例总数。

## 实机验收边界

阅读根因修复前的首统一构建 `source-clock-interleave-final-build.log`（TSC/Vite）、`source-clock-interleave-final-ui-debt.log` 和 `source-clock-interleave-final-bundle-budget.log` 均 exit 0。`source-clock-interleave-final-build-identity.json` 记录：

| 项目 | 身份 |
| --- | --- |
| entry | `assets/index-CC81C9Cg.js` |
| index SHA256 | `bb69f2b5eb17923690ac17fbe789485a2ca20b02f0c37971ac82727de33b4e94` |
| entry SHA256 | `93f6c5676d86655258e7068edb624a6bc79dc3886db0b218ca59f7f871327dda` |
| 主 CSS | `assets/index-DuSpLBS9.css` |
| 主 CSS SHA256 | `6db148bac3182dff147c275aec3a35baf125cbf1eb4a2883dce8c3f753855e80` |

主 CSS 与上一构建逐字节相同，本批未改字体、字号或风格。entry 为 869.4 KiB，低于既有 900 KiB budget。

本批 `physical-turn-clock-desktop-qa.cjs` 只使用保留的隔离 QA profile、隐藏 Electron、真正 WebSocket、backend admission、真实 `read_file` 与 localhost HTTP provider；检查延迟恢复的原始时钟、两个真实读取之间的附件 steer、同一 final、底部跟随与手动上翻。没有 store/lifecycle 注入或普通用户窗口操作。

### 原始实机结果与阅读根因

- `production-physical-turn-clock-run01` 的原始 clock 断言通过，但未定位到当前 turn 的首张截图不能作视觉通过；user item 等待失败是 QA 把当前 HTTP 采样一直 held。真正 steer 在采样边界消费，finally 释放后 raw wire 已出现完整原 user item。本轮保留为失败，没有让产品适配错误的“steer 必须 abort HTTP”假设。
- `production-physical-turn-clock-run02` 的前两项通过：真实恢复 `started_at=1792045721458`，observer resume 原样保留，原时钟约 11.79 秒时显示 11 秒；真实 read → 用户正文/原附件 → read 在一个原物理 turn 中，canonical marker 两侧工具 ID 有序，DOM 只有一个用户输入。两张当前 turn 图片已审阅。整轮第三项上翻阅读断言失败，不能称整轮通过。
- `run03` 在真实 wheel 停止后保存同一旧正文 anchor；final 前后它从 180.203px 下移到 242.203px。scrollTop 为 2854 → 2668，scrollHeight 为 4658 → 4534。没有跳到底，仍然是实际阅读漂移，不能将这项降格为“未跳到底即可”。
- `run04` 的只读 DOM 布局确证：当前 tail 始终 831px；virtual spacer 从 2093px 降至 1969px。两个旧的未配 assistant 的 steer user 行各从 200.67px 降为 138.67px，各少 62px；它们被全局 busy 错标为 streaming 并虚构过程 heading，final 到达后又删除。TanStack 与浏览器 native anchoring 对同一 124px 尺寸变化各补偿一次，scrollTop 从 3243 降至 2995，原内容在屏幕下移 124px。原两张 anchor 前后图和完整尺寸记录保留。

因此按真实布局归属修复：仅最后可见、尚无 assistant、且非队列/取消/steer 的主输入可视为 pending；旧 user 不随全局 busy 获得过程。只有历史使用 TanStack virtualizer 时停用 native overflow anchoring，让 virtualizer 负责尺寸补偿；普通未虚拟化视图保留浏览器原行为。历史虚拟化后仍保留的最近 8 个真实 DOM turn 去掉额外 `content-visibility:auto` 和 120px 惰性占位，避免两套历史高度来源；短对话的真实 DOM 同样无占位。没有固定 scrollTop、写死 62/124px 或忽略 anchor 差值。

上述阅读修复在当时尚待与同批审批 UI 源修合并构建。`CC81C9Cg` 是阅读修复前的构建，不能用于宣称这个问题已通过。

同批新构建首次在 TSC 停止：`InlineAgentPrompt.tsx:393:95` 的可选 prefix label 不能传入要求真实 string 的模板；`source-clock-interleave-reading-final-build.log` 保留该失败，未运行 Vite、未改变 dist。native UI owner 已用真实 available decision 的 amendment 数组收窄 prefix action 与必填 label，未加入空字符串或虚构命令兜底。必要的 `source-clock-interleave-reading-final-build-batch02.log` 续构建 exit 0，包含最新 type-only `AgentRunRecordPayload.duration_ms` 合同；对应 `reading-final-ui-debt.log` 与 `reading-final-bundle-budget.log` 均 exit 0。

最新 `source-clock-interleave-reading-final-build-identity.json`：entry `assets/index-DcH7jovU.js`，index SHA256 `2ca8abb30862de7f89187250bc32937655d7aec871134dd63d711ba538caab6e`，entry SHA256 `07e22b5ab174ba81d3626f237caafb0a9809f68b130b605d8f091c94086cf411`；主 CSS `assets/index-CpG3V_Lq.css`，SHA256 `1c87eb577857c9aa7a7bbd66a71660172858449eaa7b12a6714529ec7011d621`。本轮 CSS 改动包含审批 UI owner 的实际 Source 投影修复，时钟/阅读批仍未改变字体与字号。新 GUI companion 等后端 clock/auxiliary shutdown 批稳定后执行。

`DcH7jovU` 的成功构建与检查保留。native UI owner 随后补核 Source Aml 提交期间保留原按钮文字并显示现有 loading spinner 的真实过渡与 glyph；Root 已将它纳入同批，故此构建不能作该过渡的显示证明。三个 owner 最终 freeze 后，统一进行最后一次必要构建与受影响实机检查，没有逐行修改逐次构建。

### 最终构建与受影响实机复验

`source-clock-interleave-reading-final-build-batch03.log` 的 TSC/Vite exit 0，`source-clock-interleave-reading-final-ui-debt-batch03.log` 与 `source-clock-interleave-reading-final-bundle-budget-batch03.log` 均 exit 0。最新身份在 `source-clock-interleave-reading-final-build-identity-batch03.json`：

| 项目 | 最终身份 |
| --- | --- |
| entry | `assets/index-BT3XeGQn.js` |
| index SHA256 | `c625d46c2ddaead0ad95fa6b5a2c857dc8cf81462b57089686a5ed9ba6cada28` |
| entry SHA256 | `227644880d5824ce3cb15218fab23cb880bb9b1f154a695018af0b490238e1ea` |
| 主 CSS | `assets/index-CpG3V_Lq.css` |
| 主 CSS SHA256 | `1c87eb577857c9aa7a7bbd66a71660172858449eaa7b12a6714529ec7011d621` |

`production-physical-turn-clock-run05/evidence.json` 对最后未通过的阅读/final 节点复验：1/1 passed、`errors=[]`、exit 0，实际执行源与所载构建的前后 hashes 全部相同。`notRepeated` 引用 run02 的前两项窄通过；为了产生新的真实 final 所做的同轮准备不新增计数，未重跑此前五条 follower 功能链。

真实 wheel 已停止后，同一个旧 user 的 id、正文与屏幕位置在 final 到达前后完全相同：屏幕 top 均为 `132.546875px`，scrollTop 均为 `3584`，scrollHeight 均为 `5389`。它不是只检查“未跳到底”，也未将 `62/124px` 差值当容差放行。实际 DOM 同时确认历史 user 不随全局 busy 出现虚构过程，virtualizer 是唯一尺寸补偿 owner。

DOM 记录还直接保留了之前触发缺陷的两个相同旧 user id：`m-526b291636ed-u` 与 `m-2391e0f916ed-u` 在 busy 和 settled 时都为 `138.671875px`，不再先增高 62px 再缩回；native overflow anchor 为 `none`，最近真实 DOM 的 content visibility 均为 `visible`、intrinsic size 均为 `none`。

原执行 `started_at`、observer resume 和 frontend `runStartedAt` 都为 `1792048552900`。同一个原 run/assistant 的 `agent.run.completed.duration_ms`、canonical `done.duration_ms` 与 frontend raw duration 都为 `44687`；完成标题显示真实整数 `44秒`。这次实际共有 4 个受控 HTTP 前台请求：两次真实 `read_file`、在采样边界被取消输出的旧 sample、同一原 run 的最终答复；未虚构另一个 assistant 或任务。

通过真实“回到底部”控件定位后，最终输入文字和原上传附件在同一 final 之前可见，整体过程默认折叠；真实点击“展开处理步骤”后，两次读取分别在该输入的前后，最后仍是同一 final。三个 PNG 已逐张审阅：

- `03a-actual-upstream-content-before-original-final.png`
- `03-latest-physical-final-input-visible-and-process-collapsed.png`
- `04-user-expanded-original-interleaved-work-and-final.png`

原 A/B unsent draft、原 artifact IDs、隐藏工作区草稿、两个 dirty Monaco buffers、`note.ts` 磁盘 bytes 与既有 partial 的原始 content/blocks/duration/termination reason 均保持；自有 app/provider 已正常退出。失败 run01–04、两次 TSC 原始失败及所有旧构建身份均保留。

Root 续做时独立逐张查看以上三张最终构建 PNG，确认附件/正文在折叠后仍位于同一最终回复前，重新展开后真实两次读取分别位于该输入两侧；阅读中的截图保留原上文且未跳回底部。此项是现有实机证据的交叉审阅，没有重跑或新增功能用例。

这些图证明显式定位后的同轮顺序、折叠与手动上翻阅读保持；不由此推断新窗默认滚动位置、全部未测动画、所有桌面区域或整个 Codex 产品的完整全等。

本文件的自动检查不表示整套 Codex 桌面端视觉或全部 harness 能力已相同；实机结果应在真实运行与图片审阅后补充。
