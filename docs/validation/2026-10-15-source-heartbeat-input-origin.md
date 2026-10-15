# Scheduled heartbeat：每条真实输入的来源与恢复

本批仅完成输入来源的生产、实际准入、恢复及 Responses 投影；没有新增界面控件。按节点最新结果为176/176通过、0失败/错误/跳过，见 `output/source-heartbeat-input-origin-20261015-latest-results.json`。与其它批次重叠的测试不相加，也不据此宣称所有自动任务行为已等价。

## Source 行为

参考 `.tmp/codex-src`，commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。

`codex-rs/history/src/heartbeat.rs::UserInputOrigin::from_turn_trigger` 只有真实 `automation_heartbeat_scheduled` 映射为 Heartbeat，其余与未知输入都为 User。`core/src/session/turn_input.rs::start_if_idle` 在接受本次输入时保存这个事实；它属于单条输入，不能从后来活跃回合的 trigger 反推。手动执行 heartbeat、普通 cron、看似 heartbeat 的文字及同回合真人 steer 都不能仅凭名字取得 Heartbeat 来源。

## 完整修改链

- scheduler 在真正 due claim 时为 heartbeat 保存 `ScheduledTaskRun.turn_trigger`；手动 run_now/retry 是新手动 claim，不写 scheduled trigger。若已有活动 run，只返回原 run，不虚构第二条输入。原 pending run 的 asdict/from_dict 保存原 trigger；缺失的旧记录保持未知，按 Source User 处理。
- 真实 scheduled runner 将该 run 事实传给自己拥有的 REST/QueryEngine 输入。host 根据本次 trigger 保存显式 `input_origin`，并把实际 scheduled trigger 交给既有 host-authored `_source_responses_metadata`；不依据提示词、会话名称或 task.kind 在后面补猜。
- 原 turn 的初始输入使用自己准入时的 host 来源。新消费的 `TurnInput` 使用自己的来源，public `from_command` 仍为普通 User，不读取命令 data 伪造的 origin/trigger。移除了将通用 `query_source="heartbeat"` 标签直接提升成 Heartbeat 的旧推断；其它既有真实 agent-assignment 归属未重写。
- `LLMMessage.input_origin`、原 Guardian retained evidence 以及现有 ContextBuilder snapshot/load 均保留各输入的来源。没有改用整回合来源覆盖已存历史，也没有添加 public transcript 不支持的内部字段。
- Responses 使用另一批已经接通的 host Source snapshot 合并。scheduled 请求在实际 HTTP body.client_metadata 和对应 header 中保留 trigger；manual 请求不伪造 scheduled trigger。没有给 heartbeat 添加 memory/Guardian 专用标头。

## 验证与失败记录

新增10个必要节点覆盖真实scheduler due/manual、heartbeat/cron四种组合、run冷存储、原pending重启、实际QueryEngine接受与Guardian证据、snapshot冷恢复、同一个heartbeat物理回合中的真人steer和伪造origin忽略、一般query label不能冒充scheduled，以及两场真实localhost Responses HTTP请求。HTTP使用生产OpenAIAdapter和真实socket，捕获实际body/header；没有执行外部模型、操作普通用户窗口或注入前端状态。

第一轮误用bundled Python。该运行环境有pytest而无pytest-asyncio，异步测试明确失败为运行器不支持；XML/log保留，未给bundled安装开发测试插件。随后使用已有dev Python `C:/Users/ago/AppData/Local/Programs/Python/Python312/python.exe`。

dev统一8文件168节点，164通过，4个新测试错误地期望public transcript保留内部turn_trigger。仓库真实metadata投影不支持这个字段，且本批真实来源已由durable run和per-message snapshot持有；删除了没有消费者的额外写入，将断言改为核实际运行记录与来源snapshot，没有扩大public whitelist。受影响新文件窄验9/9通过。

另外7个原host fixture节点使用真正ScheduledTask/ScheduledTaskRun，取代不完整SimpleNamespace记录，全部通过。最后移除泛query_source到Heartbeat的推断并增加对应否定节点后，新文件10/10通过。最新节点按classname/name去重为176全通过；原失败XML和各次log全部保留，没有把重跑数量相加。

独立审阅未发现due/manual/retry/pending或human-steer来源边界缺陷。当前Node及审批投影仍由其它owner继续，176不是整个工作树冻结后的全量验证。

## 未由本批完成的其它 Source 行为

Source 的重复heartbeat版本合并、完整transcript中的heartbeat引用、调度输入在busy turn的自动注入和各自动准入gate是独立的后续行为；本次没有将来源已正确存储等同于这些消费者已全部实现。官方账号和依赖账号的hosted服务仍排除。
