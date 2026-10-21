# Source 长任务与续跑链路核对

参考公开 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。本次核对实际实现及真实执行证据，不以工具名称相似或进程曾存在代替完整链路。

## 任务及测试时长

原验收让主agent实现Decimal金额汇总，由独立oracle持续校验重复ID、跨account冲突、正负/零金额和输入不变；子agent实际审阅，并持有自己的后台校验进程。期间切换A/B、排队消息、断线90秒和同一子agent followup。主3600秒/child3660秒是自选耐久观察窗口，Source没有这个时长要求。

原主/child的独立OS时长分别3600.609/3660.297秒，退出均为0；业务分别2,152,150/1,094,700行。整轮仍失败，原因是首次B把截断文件头630误报为当前708–731范围内的进度；不能将已完成的计算重新包装为整轮通过。

## 调用链对应

| 行为 | Source 实现 | MiniCode 实现与结论 |
|---|---|---|
| 首次等待结束后继续同一命令 | `core/src/tools/handlers/unified_exec/exec_command.rs:307` 交互lifetime不设completion timeout；`core/src/unified_exec/process_manager.rs:719` Alive返回同process_id；`tools/handlers/unified_exec/write_stdin.rs:101` 把原session_id交回原manager | `backend/tools/command_tool.py:463` managed命令使用原background manager；`:644` wait后detach并返回原command_id；`backend/tools/monitor_tool.py:178` 轮询原conversation下的原句柄。yield结束等待，不重启进程。 |
| 区分消息和新的子任务 | `core/src/tools/handlers/multi_agents_v2/send_message.rs:46` QueueOnly；`followup_task.rs:46` TriggerTurn；`message_tool.rs:64` resolve原target后统一AgentControl.send | `backend/tools/swarm_tools.py` 的 SourceSendMessageTool与FollowupTaskTool使用不同delivery_mode，实际送达原mailbox。原sender、recipient、actor path及epoch仍参与归属。 |
| 同actor新turn | `core/src/agent/control/api.rs:100` send保留target thread_id，必要时ensure_v2_agent_loaded；`core/src/tasks/mod.rs:438` 同session启动新turn | `backend/tools/agent_tools.py:1564` 恢复原subagent_id，`:2573` acquire原AgentSession，`:3442` 每query分配新run/turn，`:3612` session_id仍为subagent_id；`backend/agent/runtime.py:1015` 新incarnation递增epoch。 |
| 一次final与资源关闭分离 | `core/src/session/mod.rs:2345` → `agent/control/completion.rs:27` 通知父级；不是close actor。`residency.rs:291` 完成actor仍可因容量被驱逐 | `backend/tools/agent_tools.py:2416` retained session已admitted时不在turn cleanup关闭；`backend/agent/runtime.py:1254` 复用session，`:1268`容量驱逐会真实close。不会承诺无限保留进程。 |
| 断线与执行host关闭分离 | `app-server/src/request_processors/thread_processor.rs:3588` connection_closed移除connection，已有core thread不在此被直接终止 | `backend/ws/manager.py:891` disconnect清理transport订阅；`:929` 仍有context/admission/borrower时只retire_transport，显式shutdown_execution_host独立处理。原90秒断线期间业务持续推进。 |
| 后续输入与持久会话恢复 | `core/src/codex_thread.rs:408` start_or_steer_turn；`core/src/session/turn.rs:565` 在完成前检查pending input；`thread_processor.rs:3986` resume_thread_with_history | `backend/agent/conversation_query_guard.py:122` 原owner原子start/steer；`backend/ws/handler.py:1381` follow-up等原owner/cleanup释放后派发；`backend/ws/handlers/session.py:140` 从repository恢复会话和原事件cursor。两者传输协议不同，这里验证相应用户行为。 |

## 实际修正与边界

Source `message_tool.rs:98` 成功send/followup返回空文本。MiniCode已完成actor恢复成功的分支原先额外返回“resumed it in the background...”说明；现按source_v2返回空文本，保留legacy行为、真实启动等待、错误和内部回执，不新增UI或虚构启动状态。

相关startup/thread services/swarm三文件集中164例中162通过，两个新增V2例因旧fixture缺少root turn_id/assistant_message_id失败；补齐实际上下文后两组四例定向4/4通过，0 skip，不将其描述成新跑了一份164/164收据。原同步fixture还在另一event loop关闭runtime，现改为原loop的async fixture await aclose；不修改产品关闭行为，不吞错误。收据为 `source-followup-empty-receipt-final.xml`、`source-followup-four-case-repair.xml`。

另修验收脚本的数字判定：队列实际read_file取得3575并回答 `3,575`，应按完整整数比较；不能按子串拒绝千分位，或错误接受13575。首次B的严格进度区间仍不变；新B请求明确使用当时已知尾部行号，结果到达立即验证，避免等到整小时末尾才发现错误。

## 已完成的真实冷续跑

`output/full-parity-20261021/hour03-cold-resume-01/result.json`：`passed=true`，131.516秒，使用原03持久state的副本和新的隔离backend，不重启用户桌面实例。

- 原A/B conversation ID恢复；B发出新provider/read_file调用，按实际尾部3575行读取并正确回答。
- A通过canonical `/root/reviewer` 找回原 `subagent-8c7c2dd8`，没有spawn替代actor。同actor epoch2→3，真实followup完成，原child实际read_file读取child-result.json。
- A实际重新读取verification.json并给出正确3575批；新主/侧final均可从repository冷读。
- 关闭退出0；运行期间backend输入、原workspace和原03 result均未改变。原失败result仍为false。

这证明持久会话与原actor可继续；原小时OS进程已结束，不声称冷启动复活了旧PID。补验也不追溯为03“一次完整无失败的一小时通过”。RT11保留整轮失败及分段成功证据，后续不重复已完成的小时计算来代替针对失败环节的核对。
