# Runtime/tool gates 实施交接

2026-10-24，代码 **READY**。本批范围：T05、T07/T08、T12、T13、M05、FF-MULTI-AGENT、FF-GOALS。只改对应后端调用链和必要 fixture。初始交接未运行验证，随后获 Root 授权的一次四文件回归及剩余修复见文末；未编译、构建、操作 GUI、调用真实模型或读写用户数据。原 parity ledger 由 Root 统一更新，本批未改它。

## Source 依据

- `.tmp/codex-src/codex-rs/core/src/tools/spec_plan.rs:1228`：`update_plan_enabled` 注册 PlanHandler，无 child 身份硬禁；`handlers/plan.rs:87` 保留 Plan mode 执行拒绝。配置默认来自 `config/mod.rs:2727`，未显式启用即 false。
- `spec_plan.rs:1253–1288`：两个 async 用户工具独立选择，均为 root-only、DirectModelOnly。request 接受 catalog 的 `request_user_input_async` 或真实旧名 `send_user_message_async`；send 由独立 `send_message_to_user_async` feature 或同名模型能力开启。
- `spec_plan.rs:1300–1325`：CurrentTimeReminder feature 或模型 clock 开 `curr_time`；sleep 先经过 `sleep_tool` feature，再按 `always_on` 或 `model_driven` 决策。reminder 开启时，model_driven 取其 `sleep_tool`，不会又用模型 clock 把 false 覆回。mode 默认见 `features/src/feature_configs.rs:466`。
- `core/src/agent/role.rs:91–103` 经 `feature_for_key` 保存 canonical 禁用项；`features/src/lib.rs:1232` 的 MemoryTool key 是 `memories`，`features/src/legacy.rs:41` 明确支持旧名 `memory_tool`。
- `core/src/config/mod.rs:1601–1627`：V2 feature 优先，其次 agents.enabled；显式模型版本优先于 `multi_agent` fallback。不能把 `multi_agent=false` 扩成覆盖所有显式模型/V2 声明。
- Source feature 默认：`multi_agent`、`goals`、`sleep_tool` 为 true；`current_time_reminder`、`send_message_to_user_async` 为 false。它们没有普通设置 display_name。
- `app-server/src/request_processors/thread_goal_processor.rs:139,259,281` 对 goals off 拒绝 set/get/clear；goal extension 另以 enabled 控制运行时消费。

## 本批实现

- `backend/tools/source_tool_gates.py` 提供 registry construction 和每 step policy 共同使用的 utility gates。factory 按当前配置注册；最终 policy 按实际 model metadata/config 补入新启用工具、屏蔽旧注册工具；schema、目录和实际执行沿现有 policy guard，无新重试/兜底系统。
- child 不再 hard-deny `update_plan`，受约束后台 allowlist 允许该真实工具。配置、父级 deny 和 Plan mode 限制继续生效。未移除其他 child 限制。
- async 两工具不再绑在同一 catalog 条件上，并改为 DirectModelOnly；已注册也不能从 code mode 嵌套调用。
- role 把 `memories=false` 和 Source 明确支持的 `memory_tool=false` 归一为 canonical `memories=false`，经过 project/inherit/cold-resume 配置继续影响 memory tool denials；不会改父级配置或保存的记忆。
- `source_spawn_version/source_agent_options` 消费 canonical multi_agent fallback 和 agents.enabled。`TaskTool.get_spec` 只保留静态能力定义，最终 policy 决定当前曝光；`Task.execute` 继续从 `context.model_execution.config/model/model_info` 读取当前 step 快照，不会因旧构造配置永久拒绝 off→on。未改 Task 启动、持久化或工作记录实现。
- goals off 同时约束 factory、step policy、直接 GoalTool、目标 owner 的 WS/slash 操作、自动 queue/admission、普通回合的目标提示词和计费。保存的 goal、预算、pending 标志及用户草稿不清空。`conversation.agent_mode.set` 后端只改 agent_mode，没有隐式 goal.clear；前端配套由 Root 处理。
- 新五个真实 Source defaults 只追加进 `DEFAULT_FEATURE_FLAGS`；沿现有 internal 排除表保留 effective/config/API 值，不新增普通设置/Beta 控件，不覆盖前批 namespace 归一化。

## 配置刷新与运行快照

原链为 `FeatureFlagsTab` 保存 → `runtime.capabilities.inspect` → `handlers/session.py` → `SessionLifecycle.send_runtime_capabilities` → `WebSocketSession.runtime_capabilities_payload/runtime_capability_snapshot`。workspace activate 也经过同一发布入口。

原 full snapshot 单独调用全局 `feature_flags_payload()`，而工具目录使用旧 session config，存在 scope/版本不一致。本批 full snapshot 按当前 conversation workspace 读取一次无凭据配置，将同一份配置显式传给展示 toolset，并直接投射它的 flags；goal composer command 同样消费该值。未做 session.config 的局部回写。

显式刷新展示的是当前 scope 的下一步目录。没有显式新配置的执行消费者仍使用已 admitted 的 `RunContext.toolset_policy`；现有 `ModelExecutionSnapshot`、active registry/policy 不回写。新 WS 回合仍在 `agent_runner.py` 的 `load_config(cwd=run_workspace_root)` 后建立新的执行快照。fixture 覆盖同一 registry off→on→off、工作区切换、inspect 更新与原 admitted snapshot 保持。

## 必要 fixture 与 Root 回归清单

新增 `backend/tests/test_runtime_tool_gates.py`：独立 async gate/root/DirectModelOnly、clock/sleep 条件、factory→step 重新选择、前后台 child plan、canonical multi-agent fallback/执行拒绝、role memories 继承、目标 owner 的 goal 拒绝、GoalTool 无副作用、scope inspect 与 admitted snapshot。

`test_goal_websocket_lifecycle.py` 补 goals off 自动队列、自动 admission、普通用户回合不注入/不计费且保存 goal 不变。旧 nested async UI/journal fixture 改为 Source 支持的直接模型调用；sleep/deferred fixture 显式开启所测试的工具，不再借旧默认曝光。

Root 的回归文件清单如下；整体最终结果由 Root 统一记录，区域回归不与重叠用例累加：

```text
backend/tests/test_runtime_tool_gates.py
backend/tests/test_feature_flags.py
backend/tests/test_sleep_tool.py
backend/tests/test_subagent_context.py
backend/tests/test_source_agent_roles.py
backend/tests/test_goal_websocket_lifecycle.py
backend/tests/test_goal_permission_chain.py
backend/tests/test_source_alignment_runtime.py
backend/tests/test_source_v2_configuration_chain.py
backend/tests/test_code_execution.py
backend/tests/test_deferred_tool_exposure.py
backend/tests/test_skill_catalog_scope_chain.py
backend/tests/test_step_capability_snapshot.py
backend/tests/test_sandbox_status_api.py
backend/tests/test_unconfigured_session_protocol.py
tests/test_plan_tool.py
tests/test_subagent_lifecycle.py
```

生产改动集中在 `source_tool_gates`、tool registry factory、tool_schema_derivation、subagent_context、agent_user_tools、source_roles、TaskTool.get_spec、GoalTool、feature defaults/internal visibility、goal WS/runner/queue 和 full runtime capability projection。共享文件内其余 owner 的修改保留。

T12 本批只补工具曝光；外部时钟注入/自动时间提醒的其他契约不据此宣称完成。R08、T19、FF-CODE-MODE 等不在本批范围；R09 和 goal 前端由 Root 处理。真实模型、GUI、多窗口及长任务验收仍另记。

## 首轮回归与 direct async 收尾

Root 首轮 core 结果保留在 `output/full-parity-audit-20261023/phase3-core.log/xml`。本区域先集中修正 fixture 契约：TaskTool 注入必需的临时 ArtifactStore；AppConfig 显式提供 LLMSettings；测试默认曝光的 plan/sleep 改为显式启用；child 搜索按父级 deny 规则验证限制；async 模型能力由 fixture 明确声明，未放松 UI、journal 或 parent-deny 断言，也未给生产构造器加兜底。

获准一次运行 `test_runtime_tool_gates / test_sleep_tool / test_code_execution / test_deferred_tool_exposure`：**113 项，112 passed、1 failed、0 errors、0 skipped，91.374 s**。原始结果为 `phase3-runtime-retest.log/xml`，未覆盖成通过。唯一失败仍为 direct async UI/journal fixture。

该失败包含两个不同原因，不能全部归为 fixture：

1. **fixture 缺真实 UI 回调。** QueryEngine 的 direct tool callback 经过 `AgentLoopSessionContext.emit_event` 交给 host，和 yielded query stream 是不同入口；真实 WS 在 agent_runner 安装 `_emit_runtime_event`，负责 owner 投射及发送 UI。测试 helper 原来没有安装它，因而只收集 yield 会漏掉真实回调。helper 现支持该原生 callback 参数，测试从实际 UI 回调收集 async items，并继续检查 accepted 结果、启动/完成顺序、questions、call_source 和 journal 非模型历史注入。
2. **产品 direct journal 缺口。** QueryEngine 的该 direct 桥原先只记录 `runtime.span/tool.started`；async item 已可发送给真实 host，但没有和 nested route 一样进入 QueryJournalRecorder。现沿同一桥先对 `delivery=async` 的 `item.started/completed` 执行 `record_event_async`，再调用原 host；不改原 delivery、owner/payload 或顺序。CodeMode/extension 的既有 acknowledged nested route 跳过此新增记录，避免重复持久化或双发。

另外，`feature_flag_settings_service._settings_overrides` 已改为和 loader 相同地读取表式 feature 的 `enabled`，修正 `{enabled:false}` 被误报为默认 true；fixture 保留并核对原文件 mode/其他选项不变。此 service 的最终验证由 Root 统一记录。

上述 direct 桥及 callback fixture 收尾后，本区域没有再次运行四文件。Root 接续执行 `test_code_execution` 和必要 QueryEngine/async/protocol 回归；**112/113 是修复前区域重跑结果，不是最后收尾后的通过结论**。
