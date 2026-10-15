# 原执行起点、实际时长与最终事件使用同一所有者

本批完成，自动检查按节点最新结果见 `output/source-run-timing-owner-20261015-latest-results.json`。实际前后端 companion 在最终 `index-BT3XeGQn.js` 上通过，详见物理回合报告。本文件不宣称所有 Codex 行为等价。

## 实际问题与 Source 依据

生命周期 run04 的实际截图先显示56秒，最终却显示55秒。原始 `agent.run.completed` 的起点 `1792046315127`、终点 `1792046372258` 相差57131ms，而 `done.duration_ms` 只有55969ms。WS runner 在后面的准备阶段另起 monotonic timer，漏计了真正运行准入后的1162ms。

Source `core/src/tasks/mod.rs:302` 与 `core/src/turn_timing.rs::TurnTimingState` 在同一真实任务起点保存 monotonic Instant 和 Unix 时间，并在任务完成时从原 Instant 实测 duration。不能以 viewer 时间、另一个准备计时器或 wall-clock subtraction 代替，也不能在前端用 max/clamp 掩盖倒退。

## 完整修改链

- 真正新建的 `AgentRuntime.start_run` 为原 `AgentRunRecord` 保存运行时 monotonic 起点，重入实际已运行 owner 不重置它。
- record 实测 elapsed；完成后冻结整数毫秒 `duration_ms`，随原始 record 持久化。monotonic 起点不写入持久记录或 public payload；恢复的未知原始 clock 不从本地 now 补造时长。显式 epoch 0 和已持久 duration 0 仍是有效事实。
- partial snapshot 从同一 run 获取原 timestamp/实测 elapsed，不再借 WS setup 时间算运行耗时。
- runtime terminal → QueryTerminalTransaction → durable runtime receipt → canonical done 使用同一个真正时长；startup failure 同样接入。provider/runner 自带的无对应 owner 时长不覆盖 canonical fact。
- WS 和最终 conversation projection 只消费原 runtime completion 时长；未知时省略，移除了自己的后置 timer。前端 `AgentRunRecordPayload` 仅补同一可选数字字段，无新界面状态。

## 验证

新7个真实调用链节点覆盖原准入准备时间、setup重入、真正runtime持久化/冷读、未知旧clock和epoch 0、系统时间跳变、实际partial/done producer、实际TurnKernel+QueryTerminal+ExecutionJournal，以及真实startup-failure factory。

统一必要回归还包含已有resume起点、admission、event envelope、runtime和canonical terminal/recovery范围。首轮新fixture误以为修改函数名会替换dataclass已捕获的epoch factory，现改为保留实际原始起点来断言；新数字字段初漏public integer字段集合，真实事件变成字符串，已修实际producer类型。既有一个MCP缓存测试仍要求旧纯文本，已仅更新为核当前Source结果中的真实不同值与两次实际调用，不改变MCP产品实现。原始失败/窄复验XML全部保留；各次结果按节点最新值，不相加重复数量。

最终实机 `production-physical-turn-clock-run05` 记录：原 started_at/resume/frontend 起点一致；runtime completion、done、frontend raw duration 同为44687ms，界面44秒；历史阅读内容anchor偏移0px。该真实任务还核验原输入/附件、同轮前后读取和最终默认折叠。构建/实际图片与后续阅读修复范围见 `2026-10-15-source-physical-turn-clock-projection.md`。
