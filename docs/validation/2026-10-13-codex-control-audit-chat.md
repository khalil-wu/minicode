# 主对话控件与 harness 交接逐项审计

基准为 MiniCode 当前源码、已有最新生产证据、Codex 26.1007.2314 实际桌面原图和只读 ASAR，以及 codexsrc 822e58cc。用户字体字号保留。`output/playwright/codex-control-audit-20261013/inventory.json` 是静态 JSX/CSS 出现清单，不能当作可见按钮数量或通过证明；动态菜单、分支状态和后端契约另外审查。

| 项目 | 当前链路与依据 | 本轮状态 |
| --- | --- | --- |
| 用户图片、文件、引用、正文气泡 | UserMessageCell → owner-scoped attachment/browser raw URL → 实际预览；图片和引用位于独立文字气泡之前。原 Codex 用户截图与 upload/source reference；生产 Files run04 与可见恢复补图。 | 已读当前组件；既有真实证据仅覆盖该记录范围。长消息展开、复制、撤回的每个分支继续检查。 |
| 过程正文、单项/组合摘要间距 | hydrate → ChatTurn → ThinkingCell/MarkdownRenderer/AgentTimeline；实际 md-body 首尾 block margin 为0，间隔8px、图标/文字/箭头6px。 | 前轮独立真实组件几何验证4种样本×收起/展开/重收起通过；不是后端任务证明。 |
| 过程总开关与 elapsed | AgentProcessSummary → 实际 startedAt/shared tick；停止、失败、partial保留状态，最终完成按阅读选择收起。 | 已读当前组件，前轮生产过程 run03；本轮其他非正常终态仍需分支核对。 |
| Read/Search/List/Edit/Run组合、滚动、扫描 | canonical command_actions/typed result → hydrate/project → WorkGroup；ASAR split-items、timeline-activity-body、active-tool labels。 | 当前源码已读；前轮生产 run03 和 commandActionsProjection测试复用。没有把运算包装当真实操作。 |
| 命令展开、复制、原命令/typed streams、原退出状态、完整 artifact | ExecCell → stdout/stderr/exit、originalCommand → clipboard/raw owner preview。确切 ASAR source 为 sites-end-resource::aC → user-message-attachments 的 un/vn/yn（导出 l），embedded Shell 的两套 copy、两行命令展开、9rem反向输出滚动、footer Success/Stopped/Exit code。command-row-849...实际是键盘设置，不能用作工具卡参考。 | 本轮已按真实嵌入组件对齐，保留现有字体字号。命令和输出分别复制；elapsed只在操作摘要，不重复塞进toolbar；live空输出不造“等待输出”或“运行中”徽章，失败展开保留原退出码、不标红或加失败标记。37项相关批次通过；最新生产截图待统一构建。 |
| 委派真实启动与名字头像点击 | tool delegations → actual started_at → CollaborationCell → setFocusedSubagentId → SubagentsTab → journal transcript；codex protocol CollabAgentSpawnEnd/SubAgentActivityEvent。 | 修正无时间状态不能被推断为开始。具有真实时间的启动记录即使随后失败仍保留；未知/未启动身份不虚构。 |
| 同一真实agent的深浅色身份头像 | subagent-row的import gg经app-initial实际export定位KOc（先前import x2 as gg是另一模块的局部变量，不是此导出）；KOc→XOc的28组原SVG→qOc UTF-16 rolling31 %2147483647。 | 已移除自绘六组/FNV近似，使用28组原图及真实source哈希，主行、摘要、列表、详情共享identityKey。56个深浅色SVG、source manifest与独立source执行fixtures保存。90项受影响范围最终通过；未把静态artwork测试计为生产截图。 |
| Send/Followup/Stop错误详情与父消息 | 真实tool_result status/error → CollaborationCell；用户明确禁止工具失败红色与追加失败标签。 | 去掉父消息失败红色、详情summary追加失败标记，取消/partial不冒称已发送；原错误与状态保留。不是隐藏真实错误。 |
| 子agent结果与工作记录 | projectAgentViews → child journal/live delta → ChatTurn isTranscriptMode，parent conversation负责资源owner。 | 当前调用链已读，原 run03 真实子过程折叠/重启证据；孙级导航未检，不记通过。 |
| 最终回复复制/引用/重新生成/派生 | AssistantMarkdownCell → clipboard / composer quote / retryFromMessageId / context.fork → WS journal/owner。 | 当前代码已读；fork的create_branch是会话分支，不是创建Git分支。仍需对照正确动作菜单源与取消/owner分支证据。 |
| 图片复制/保存/放大及Markdown文件、URL、目录链接 | MarkdownRenderer/AssistantMarkdownCell → scope → native SaveFile/copy/open；独立真实native保存已在前轮完成。 | 资源相关行为与composer资源审计交叉；实际远程服务未检，不宣称通过。 |
| 完整assistant item后的邮件边界 | Responses output_item.done → normalized item_completed → TurnKernel → TurnInputQueue；codex stream_events_utils.rs final/None完成项延后邮件，真实ToolCall receipt重新开当前turn。 | 已补完整生产者/消费者。保持文本原文及原有前端投影，不由单个content-part结束猜整条消息完成。 |

## 本轮 harness 与主对话批次

`root-harness-batch-run01` 共75项，72通过、3个新增断言误把tool_result的真实id字段写成tool_call_id。原失败与XML保留；改为实际事件契约后仅重跑6个相关组合全部通过。对应范围最终75项、零跳过，不累加重跑。包含真实QueryEngine、RunContext、owner queue、工具执行、journal，受控模型；另三种phase经真实HTTP/SSE→OpenAIAdapter验证单个message完成项在response完成之前抵达并保留原文。

`root-chat-batch-run01` 3文件59项，58通过、1个既有“真实启动”fixture缺失实际时间。补该fixture的真实时间后仅重跑CollaborationCell39项通过；对应范围最终59项，重跑不累加。新增分支覆盖无时间running/done/error不虚构开始、父消息failed/partial/cancelled保持中性并展示原错误、既有真正启动后失败保留事实。

本轮未统一构建或重新跑生产GUI。原子批次完成后统一整合。任何待检查、失败和未执行项都不算通过；公网provider/MCP/OAuth尚未核对部分如实记录。当前未向用户窗口发送输入，未修改或清理用户资料。

补充：kernel构造现在直接将同一个TurnInputQueue写回RunContext，消除直接调用者创建队列却mailbox消费者看不到的真实owner交接问题。`root-kernel-owner-run01`16项通过，含不由调用者预设queue的真实mailbox案例。与前75项有重叠，不相加为独立总数。

`root-command-batch-run01`4文件37项全部通过，覆盖actual wire/history/reopened child command_actions→真实单元格、字节保留、独立clipboard复制、退出码与空live状态、原命令展开和中断详情。`root-avatar-batch-run01`4文件90项，89通过、1条既有fixture误数每个主题图片为两个头像；改断言验证一位真实agent只有一个avatar容器后，只重跑CollaborationCell39项通过（run02）。最终对应90项无失败/跳过，重跑不累加。exactsource头像fixture包含中文和surrogate pair，避免codePoint与UTF-16差异。

canonical request_user_input分组已直接核对：projectToolBlock每条真实record独立，chatSurfaceState还拆开多记录envelope；AgentTimeline的generic workLabel为undefined，所以提问独立于Read/Run组合。Codex agent-activity-item把user-input-response标为standalone、userInput不入普通活动；前端专用Asking/Asked N questions与question/answers由composer agent本轮补齐。内部Skill安装询问没有真实model tool_call，不在stream_state虚构一条工具过程。
