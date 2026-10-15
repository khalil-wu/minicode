# Source 完成通知、自动未读与完整控制标记

本轮按实际本地 Codex 桌面端生产链实现，当前安装包为 `OpenAI.Codex_26.1007.9446.0`。ASAR 为 `C:/Program Files/WindowsApps/OpenAI.Codex_26.1007.9446.0_x64__2p2nqsd0c76g0/app/resources/app.asar`；读取后保存的参考文件与函数在 [current Source](../../output/playwright/codex-control-audit-20261013/codex-open-source-current-notifications/source.json)。参考 Rust checkout 为 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`，`ThreadSource` 是真实来源元数据，不由正文、空回复、失败或工具结果推测。

## 实际 Source 依据

- `app-shared` 的 [Rnn](../../output/playwright/codex-control-audit-20261013/codex-open-source-current-notifications/Rnn.txt) 在每个 `turn/completed` 上令 `automationKind = notificationMetadata?.automationKind ?? null`，经 [Inn](../../output/playwright/codex-control-audit-20261013/codex-open-source-current-notifications/Inn.txt) 调用 [Pnn](../../output/playwright/codex-control-audit-20261013/codex-open-source-current-notifications/Pnn.txt)。`Pnn` 明确选择 `automationKind === heartbeat ? n2t : Sz`：所有线程均可解析合法结构化 XML 决策；普通线程的来源仍是 user，不能因此改为 automation。普通纯 `DONT_NOTIFY` 和空回复不因此安静，heartbeat 的纯控制词和成功空回复才有专门规则。
- [Sz](../../output/playwright/codex-control-audit-20261013/codex-open-source-current-notifications/Sz.txt)、[n2t](../../output/playwright/codex-control-audit-20261013/codex-open-source-current-notifications/n2t.txt) 采用最后一个完整 `<heartbeat>` 决策，分别保留可见正文与通知正文。`failed_runs_only` 成功不通知，失败通知；遗漏更新保留偏好，显式 null 解禁。
- [llc](../../output/playwright/codex-control-audit-20261013/codex-open-source-current-notifications/llc.txt) 与 [dlc](../../output/playwright/codex-control-audit-20261013/codex-open-source-current-notifications/dlc.txt) 根据真实线程来源、待续跑状态、窗口焦点与偏好决定资格。完成通知标题是聊天标题，正文来自真正的最后一条 agent 回复。普通空回复或失败仍有通用正文；成功的完整 `::SKIP_COMPLETION::` 不发送完成通知，失败不按这个规则假装成功。
- [uhn](../../output/playwright/codex-control-audit-20261013/codex-open-source-current-notifications/uhn.txt) / `eU` 的 assistant-message 调用链只完整匹配控制标记；合法 padded CDATA 仅在 `agentMessage.phase === final_answer` 解包。代码、引用、命令 stdout、工具报错和真实 error item 不属于这个过滤入口。
- Native 自动任务生产者 `_u → pu → Cu` 使用真实 `<heartbeat><automation_id>...<current_time_iso>...<instructions>...</heartbeat>` 输入与显式 kind/policy；`target_thread_id` 对应 heartbeat，而 cron 创建独立任务。

## 已贯通的 MiniCode 生产链

1. [turn_notifications.py](../../backend/services/turn_notifications.py) 提供 Source 控制决策和实际来源/最终正文投影；[scheduled_task_runner.py](../../backend/services/scheduled_task_runner.py) 与 [chat_api_service.py](../../backend/services/chat_api_service.py) 将真实 heartbeat 输入、kind、policy 传给 QueryEngine，保存原始结果和可见正文。Scheduler 持久化实际 message id 和结果决策，未启动的 setup failure 不虚构一条已完成模型回合。
2. [agent_runner.py](../../backend/ws/agent_runner.py) 在 canonical terminal 后，从真实最终文本、真实队列头及线程来源产生完成事实。排队的实际未暂停续跑不弹完成通知；暂停的队列不伪装为仍在续跑。真实 `done` 与最终 transcript 使用同一份事实。
3. [repository.py](../../backend/conversations/repository.py) 将终态消息、上下文与新未读状态原子提交；以同一消息已经原子提交的 completion_notification 事实判重，而不是按流式快照的 terminal_status 猜是否处理过。相同终态重放不会重新标记用户已经读过的回合。安静完成不清除先前手工未读。来源经模型、公开投影、摘要与列表传到前端，不在读取日志时猜测。
4. 完成摘要通过真实 WS 向其他 live renderer 发布；scheduler 状态广播同时刷新会话清单。修复了 `send_conversation_list` 实际发送成功却返回 None、导致广播误报 warning 的根因，直接返回已有 sender 的实际 bool。
5. [completionNotifications.ts](../../frontend/src.v2/chat/completionNotifications.ts) 消费后端事实，完成通知使用真正文与真聊天标题。Scheduler 初始快照保持安静，之后真实终态变化才通知。Native 相同会话/message id 去重，设置仍决定是否实际显示系统通知。
6. [useDesktopEvents.ts](../../frontend/src.v2/hooks/useDesktopEvents.ts) 以实际 native focus、可见聊天区域、文档可见性和真实切换完成状态清未读。Code 隐藏聊天、设置页、待切换状态与失焦不提前清除；网页模式使用实际 document focus。点击侧栏只发切换，不把尚未打开的会话当成已读。手工标为已读/未读仍走现有真实命令。
7. [source-assistant-text.ts](../../frontend/src.v2/lib/source-assistant-text.ts) 只在 assistant 的最终或流式显示投影入口完整匹配 Source 控制标记。`ChatMessage`、`ContentBlock`、provider/raw inspector、后端原始最终结果及上下文保留；包含该字符串的说明、代码、引用、非法 CDATA、命令结果与真实报错仍可见。

## 验证状态

- 前端当前受影响批次 [273 项全部通过](../../output/completion-unread-frontend-20261014-current.json)，进程 exit 0，0 failed / 0 skipped。包含早先 180 项；不能相加。覆盖 typed item→done→实际 turn 投影、重放、隐藏/焦点、未读、草稿、Source 标记及工具/error 证据。
- Native 通知 producer / IPC / preload / native focus publisher 11 项全部通过，OS Notification 对象被拦截以免测试弹出用户系统通知。它验证实际生产函数与契约，不等价于 Windows 通知中心实投。
- 后端按 case 取最新结果 [167 项全部通过](../../output/completion-unread-backend-20261014-current-results.json)，0 failed / error / skipped。原 batch01 165 项为 128 passed / 37 failed；其中 3 项在整批 Guardian 施工期间遇到 missing `async_service` / 等待模型超时，34 项是旧 `_Session` 夹具缺实际 owner mixin。复用实际 `SessionApprovalRuntimeMixin`、等后端稳定后只复测原失败的 [37 项](../../output/completion-unread-backend-20261014-retry01.xml)，37/37 通过。新增 Source 普通 XML case 和实机发现的 partial-terminal→final-commit case，加上受影响仓库/runner/sidebar [35 项复验](../../output/completion-unread-backend-20261014-terminal-receipt02.xml)，均包含在 167 中，不相加。
- TSC 和 Vite build02 成功，入口 `index-B5o7lS_j.js`。[build log](../../output/completion-unread-frontend-20261014-build02.log)。新控制词显示测试最初的工具 fixture 使用了非生产的 output 字段，改为实际 stdoutPreview 后通过；没有以修改生产兼容层来放行。
- 隔离 Electron/真实 HTTP/独立认证 WS/cold profile 的 [run08](../../output/playwright/codex-control-audit-20261013/production-completion-unread-run08/verdict.json) 5/5 场景通过，errors=[]，exit 0。实际 capability inspection 确认该默认 profile 的 guardianv2=false、guardian_approval=true；不把 Async 启用分支混算。场景为未看回复的实际未读点、真实 provider 完整控制词的主区显示消费、真实 heartbeat 输入/安静完成、真实 HTTP500 的 failed_runs_only 决策/未读、冷启动恢复 reader 原稿与未读。原始模型请求事实、实际终态/通知/任务记录、WS 结果和截图片段全部保存，已用 view_image 查看当前构建截图。
- 首次实机 run01 抓到真实根因：最终提交之前，partial=true 的流式快照已带 terminal_status=completed，状态式判重漏标未读。改为已提交事实判重、补必要过渡测试后，在 run08 实际链路通过。run02—07 的其余未完成场景包括检查尚未主动请求的 Home capabilities、在任务真正完成前读取 done、误用非公开 conversation.activate、等待 switch 的错误回执类型、复制主 UI session 导致测试连接争抢，以及误把合法 info 结果视为错误；它们是测试客户端错误，原始失败记录保留，不算产品通过。修正客户端使用真实 conversation.switched 语义、独立 session、实际 commandResultSucceeded 规则后完整验证。

当前实机图： [未看到回复的未读点](../../output/playwright/codex-control-audit-20261013/production-completion-unread-run08/01-real-unseen-completion-unread-dot.png)、[控制标记只保留原始证据](../../output/playwright/codex-control-audit-20261013/production-completion-unread-run08/02-source-whole-marker-hidden-with-original-evidence.png)、[真实失败仍未读](../../output/playwright/codex-control-audit-20261013/production-completion-unread-run08/02-real-failed-only-completion-keeps-unread.png)、[冷启动原稿与未读恢复](../../output/playwright/codex-control-audit-20261013/production-completion-unread-run08/03-cold-restart-restores-unread-and-reader-draft.png)。正常用户 profile、已有窗口、未保存代码和上传内容没有被这个隔离验证修改。

此范围对齐本地已存在的线程、QueryEngine、队列和 scheduler 链路。Codex 的 durable cloud root-thread/account 校验、共享账号身份、远程任务与系统原生通知回复/审批按钮仍不是本轮的实际验证范围，不能用本地受控 HTTP 或 JS 对象替代这些外部服务并宣称一比一完成。
