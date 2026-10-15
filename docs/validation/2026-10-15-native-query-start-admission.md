# 原生命令启动事实的实际 QueryEngine 准入

本批是 `test_native_command_activity_persistence_chain.py` 原 5 场景之后的独立生产入口补齐，不重算原 Node349、原 native71 或旧 wheel 证据。已闭合：新增真实入口 7/7 通过，必要运行去重后 67 场景的最新结果全过，0 跳过。

## 真正遗漏及修复

只读 review 发现：直接命令的 `runtime.span/tool.started` 通过实际 `ToolExecutionContext.emit_event` 回调到宿主，并不经过 QueryEngine 的 yield stream；原 `QueryJournalRecorder.record_event_async` 也未分流非 nested runtime span。原 5 场景的测试消费者手动调用 `record_event`，不能证明直接命令执行途中、尚无 result 时已写入启动事实。

现由 QueryEngine 的原 `QueryJournalRecorder` 包装原宿主 emitter。直接工具的实际 `tool.started` 先 await 同一 owner 的持久写入，再交原宿主 callback。SDK 没有宿主 callback 时也安装这个入口。CodeMode/extension 继续走原 acknowledged nested 路由，不另建队列、owner、执行器或时钟。`QueryTurnContext` 仍为 immutable，使用 `dataclasses.replace` 换 emitter，原 run、journal、session 和 turn kernel 对象保持同一引用。

`record_event_async` 仅增加 `tool.started` 这条必要分流。`ExecutionJournal.append_tool_execution_started` 对实际 span 和原 open use 的 `turn_id`、`iteration_id` 作同一请求匹配，才写入原 clock；call id 可在后续物理 turn 复用，旧 callback 不能把旧 clock 写到新同 id 的 open 请求。closed 记录仍不更新。

主 agent 同批对 unresolved/synthetic 透传补齐 `command_actions`；真实结果仍保留 `stdout_preview`、`stderr_preview`、`exit_code`。synthetic missing-result 没有制造 duration 或 exit code。

## 实际验证边界

新增测试使用真实 QueryEngine、真实 Source native exec-policy owner、真实 PowerShell 子进程和独立临时 profile。直接工具、直接工具加原 nested stream、CodeMode 三种路径，各覆盖有/无宿主 callback。命令真实写入 entered 文件后等待 release；在执行中以新的 journal 实例冷读，确认没有 tool result，但已有唯一的实际启动事实及原 command actions。放行真实进程后再核 stdout、exit 0、原 clock 与 actions。

宿主 callback 内直接冷读日志，确认实际启动已先持久化；它没有手动调用 journal。另在同一 journal 的两次真实物理 turn 复用 provider call id，在第二个真实 open/start 回调中交付第一 turn 保留的实际 `ToolExecutionContext.emit_event` 闭包及原 event，确认新 clock 和 use 数没有被旧事件改写。跨 turn 的 iteration 标签可以同为 `iter:1`，实际请求身份使用完整 turn/iteration tuple。

## 验收记录

| 运行证据 | 通过 | 失败 | 跳过 |
| --- | ---: | ---: | ---: |
| `output/native-query-start-admission-batch-initial-20261015.xml` | 47 | 19 | 0 |
| `output/native-query-start-admission-correction-20261015.xml` | 31 | 3 | 0 |
| `output/native-query-start-admission-final-20261015.xml` | 7 | 0 | 0 |

首批 19 个失败均来自本批实现直接给 frozen `QueryTurnContext.emit_event` 赋值，已改为保留相同原 owner 引用的 dataclass replacement，随后全部相关 QueryEngine/lifecycle/recovery 和 Node 实际 stdio/HTTP 场景通过。后续三个新 fixture 失败为：两个 Never 场景沿用 `setup` 明确要求确认的默认列表，故 CodeMode catalog 正确屏蔽命令；本全权限 fixture 改为显式不附加 ask 列表，真实 Source native exec-policy owner 仍存在。另一场景错误预期跨 turn 的 iteration 标签不同，已按实际 turn/iteration tuple 核对。没有为测试放宽生产权限或增加兼容兜底。

上述运行不相加计数。`output/native-query-start-admission-delivery-20261015.json` 逐场景合并最新结果，记录 67 个独立场景及证据位置；保留 19/3 项历史失败。原 native persistence 5、相关 Source emitter/runtime span、lifecycle/recovery 及 Node 真实 stdio/HTTP 消费链均有必要通过证据。未重跑旧 349。

冻结生产 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `backend/agent/query_engine.py` | `3799d7ea5f0b80a008308c6224548adb6dc06ea000bbf1a06096ebe7dc0d48b6` |
| `backend/agent/query_journal.py` | `a68877993e771a88ebb348849dd3dd4fc469c3fb9929b916c4142cf7adac5df0` |
| `backend/agent/execution_journal.py` | `c851bc47f9c8f1b2e644826c4e7f75a9df3a3c265bc54987e8411517f99368d2` |

新 wheel、bundled Python 和原用户 profile 的最终桌面验收归主 agent；本批结果没有冒称这些交付步骤已经通过。
