# MiniCode 1M 默认窗口与 harness 源码对照

日期：2026-09-25。参考本地 Codex `588b781a`（`.tmp/codex-src`）与 CC `f7a3ea1`（`cc`）。CC 的 AGENTS.md 明确说明该目录是重建源码；不能把它当成官方发行版本的完整实现。工作区接手时已有大量未提交修改，本报告只说明本轮新增改动与验证。

## 产品要求与默认窗口

用户明确要求所有模型（包括旧模型和小窗口本地模型）默认使用 **1,000,000 tokens**。普通配置、内置 Responses 目录、扩展注册、models.json 缺省值共享 `backend/llm/model_catalog.py` 的同一常量。已有显式 provider 窗口、模型覆盖和 host override 仍有优先级；本机 settings.json 的 `token_budget.total` 从 200,000 更新为 1,000,000，避免本地固定预算覆盖新默认值。

本地默认值与服务端容量分开：`context_window` 表示应用使用的预算；`max_context_window` 保留提供商声明或既有目录中的容量记录。缺省的 1M 不标记为提供商已验证；models.json 与 Anthropic 传输现在也保留这个来源信息。未通过联网文档重新核验既有模型容量目录。

**与 Codex 的明确差异**：`codex-rs/models-manager/src/model_info.rs:25` 的 `with_config_overrides` 会将显式上下文覆盖限制在模型元数据的最大值之内。MiniCode 的显式 host override 在收到 provider max 或 Responses 目录 max 时保持已有的限幅规则；用户要求的“所有模型默认 1M”是应用默认策略，不代表小模型的服务端容量被提升。

## 已定位并修复的恢复根因

原 `TurnKernel._save_checkpoint` 在保存前调用：

```python
context_builder.export_snapshot(max_messages=160, max_chars=131072)
```

`export_snapshot` 按完整消息组截取历史后缀，协议项未必损坏，但最初要求、早期约束与已完成工作的前缀会被直接丢弃，且没有摘要替代它们。随后 `prepare_query_recovery` 将这个后缀视为完整上下文恢复。这是恢复语义错误，而非 UI 展示问题。

与此同时，`_fit_checkpoint_payload` 把权威上下文和诊断数据共用 2 MiB 限制。独立复现中，3,000,000 字节的正常文本历史直接触发 `authoritative context snapshot exceeds checkpoint byte budget`。

直接对照 Codex：

- `rollout/src/model_context.rs:18`：只有同时找到有效 compaction replacement history 和匹配的 turn context，逆向扫描才可提前停止；否则读取完整重放历史。
- `core/src/session/rollout_reconstruction.rs:345`：从保存的 replacement history 开始，按顺序重放后续 response items；保留压缩与 rollback 的语义。
- `core/src/session/mod.rs:3760`：压缩边界记录 `replacement_history` 与恢复所需的上下文元数据。
- `rollout/src/recorder.rs`：记录写入失败保持 pending items，以便后续 flush 重试；不是截断历史假装持久化成功。

最终修改：停止任务时保存完整的**当前模型上下文**（已压缩过的上下文仍是压缩后的版本），权威 snapshot 不参与附属诊断的 2 MiB 裁剪。原子写入、校验和、context revision、会话归属和失败事件沿用现有实现。没有新增恢复框架，也没有保留临时设想的 32 MiB 固定上限。

新增回归 `backend/tests/test_one_million_context_resume.py` 经真实 TurnKernel 保存和 QueryEngine 恢复入口验证超过 200 条、超过 2 MiB 的历史；逐项比较恢复后的完整 history，并验证旧 run 只保留为来源，新 run 身份不被覆盖。

### 继续验证发现的恢复预算错误

通过新增的评测恢复入口，生产 `QueryEngine` 回归用例复现：`max_iterations=0`（不限轮数）恢复一个已有 3 轮的检查点时，被 `prepare_query_recovery` 算成上限 3，下一次准入立即以 `max_iterations` 结束，provider 调用次数为 0。根因是把表示“不限”的 0 与历史轮数直接相加。

修复保持 `max_iterations_budget == 0` 时仍为 0；显式有限预算仍沿用原来的历史轮数加新预算算法。没有增加重试或兜底。Codex 的 rollout 恢复逻辑用于核对历史重建与运行配置分离的原则；这里的 0 值契约属于 MiniCode 自己的 iteration_budget 定义，不能伪称是复制了上游同名算法。

`MINICODE_EVAL_RESUME_FROM_CHECKPOINT=true` 现在可让现有评测器走生产恢复入口，使用原来的 task id、state root 和工作区。默认不恢复。回归通过真实保存/加载验证旧要求仅出现一次，旧诊断仍在下一次请求中。

### 容量投影与工具说明

补齐两处元数据路径：models.json override 和扩展模型 UI 事件原先使用 `max(context_window, max_context_window)`，会把默认窗口误写成已验证的最大容量；现在保留已声明的最大值。Anthropic adapter 同样保留配置来源。

全量测试中的唯一失败是旧测试把 `read_file` 的模型说明固定为一句话；活实现已经带有必要的范围读取和截断结果指引。说明保持简短，并将“范围读取返回完整文件哈希”修正为“可用时返回”，符合大文件/非 UTF-8 的实际行为。测试改为检查必要语义及 400 字符上限，没有删除这些用户操作指引来迎合旧字符串。

## 链路对照结论

| 环节 | MiniCode 检查位置 | Codex 对照与结论 |
| --- | --- | --- |
| 模型与预算 | config_helpers → ModelRuntime → model_selection → TurnIterationAdmission | 与模型元数据、显式 override、压缩预算分别管理的结构一致；本轮统一默认值并保留来源 |
| 提示词与完成要求 | prompting 的 Doing tasks、工具说明、compaction prompt | 对照 models-manager/prompt.md；已经要求追根因、覆盖全部症状、保留验证输出。没有证据支持再堆提示词 |
| 工具提交 | StreamingToolExecution.submit、QueryJournalRecorder、ToolBatchRunner | `core/src/stream_events_utils.rs:298` 在完整工具项到达后先记录再排队执行。MiniCode 已有相同边界 |
| 并行与副作用顺序 | streaming_tool_execution 的 reads/write 依赖和 batch_tool_calls | `core/src/tools/parallel.rs:116` 按工具并行能力选择读锁或独占写锁。MiniCode 有对应的读写准入与并发上限；本轮没有重写 |
| Provider 失败 | provider_stream_error_event、stream_retry、provider_response_recovery | `core/src/session/turn.rs:1435` 重试使用更新后的会话历史；需要保留已提交工具结果。MiniCode 区分无提交重试、已有提交及不完整工具流 |
| 压缩 | context.compact、manage_context_budget、loop_recovery | 对照 core/src/compact.rs 与 CC services/compact/autoCompact.ts；压缩成功后更新上下文，失败显式上报。不同实现的重试次数不自动构成缺陷 |
| 最终答案 | final_answer_orchestrator、answer_acceptance、terminal_validation、TurnKernel | 有空答案恢复、Stop hook、partial/failed 与终态提交。`completed` 只说明运行生命周期结束，不能证明用户问题全部解决 |
| 持久化与恢复 | checkpoint、query_recovery、execution_journal、ConversationRepository | 修复固定后缀冒充完整恢复上下文的问题；沿用既有增量 journal 与原子终态投影 |
| 性能 | 历史 token 缓存、工具 schema 缓存、受限工具输出、增量事件 | 与 Codex 的上下文快照、工具执行队列和缓存稳定性原则对照。不能仅凭结构相似或单元测试通过宣称更快 |

## 本轮验证

验证日志与真实模型轨迹保存在 `artifacts/harness-audit-2026-09-25/`。

首批关联测试：237 passed，2 failed。两个失败分别是原 200K 默认值断言、Anthropic 测试替身缺少新增来源字段；按新的配置契约更新，显式 200K provider 测试仍保留原值。

后端全量：**4,628 passed、54 skipped、1 failed**，耗时 1,492.78 秒；唯一失败为上述工具说明旧断言。该断言更新后，关联验证 **47 passed**；这一批额外发现两个新恢复测试的替身缺失 abstract method，修正替身后定位并修复上面的真实不限轮数恢复问题。最终恢复、配置、模型 runtime、检查点与 turn boundary 集合 **150/150 通过**。后续只运行受修改影响的集合，没有重复整套 4,683 项；不能把分批验证表述成最终版本重新全量通过。

真实任务使用现有配置的 `glm-5.3-flash` / Chat 接口，在独立历史代码快照上运行生产 MiniCode harness，外部 oracle 不放入 agent 工作区。开始前分别确认有缺陷的基线失败、当前参考代码通过：文件快照任务 oracle 5 项，压缩任务 oracle 3 项。文件任务使用 1M 窗口；压缩任务明确设置 24K 以触发压力条件。每项运行预算 600 秒、最多 50 轮；这些限制是测量条件，不是生产默认值。

| 真实任务 | 运行结果 | 外部验收 | 轨迹说明 |
| --- | --- | --- | --- |
| real_file_snapshots / 1M | partial / max_turn_seconds，约 611 秒（含评测收尾） | 0/5，失败 | 14 轮、26 次工具调用；没有形成代码修改，原测试未改 |
| real_compaction / 24K | partial / max_turn_seconds，约 612 秒（含评测收尾） | 0/3，失败 | 15 轮、20 次工具调用、7 次压缩；压缩后继续，但没有形成修复，原测试未改 |
| checkout_ledger / 1M | partial / max_turn_seconds，约 620 秒（含评测收尾） | 8/10，与基线相同；可见测试通过 | 没有完成实现修复；原测试未改 |

后续使用同一 provider 上配置可用的 **deepseek-v4-flash**，在新的原始结算工作区、相同 1M 窗口、600 秒预算和原任务文本下对照：**约 116.14 秒，completed，外部 oracle 10/10、可见与新增测试 12/12 通过**。原有测试未改。11 轮、14 次工具调用，其中一次编辑匹配失败后重新读取并成功修正，证明该场景走通错误反馈、自我修正和外部验收。模型请求占用 90,101 ms，工具占用 8,931 ms，思考文本 3,948 字符。这个单次模型对照说明结果高度依赖模型行为，不能把 GLM 的耗时归因于 harness，也不能据一次成功推出普遍优势。

评测恢复另使用原 GLM 检查点与工作区继续运行，只输入“Continue the original task from the saved checkpoint.”，追加 1,200 秒预算。实际 **902.78 秒完成**，`resumed=true`、终态 completed，外部 oracle **10/10**、可见及新增测试 **17/17**，原测试未改。恢复通知明确记录旧 run `run_49132887dcc5`、原 conversation/session 与 checkpoint revision；新 run 从旧约束和诊断继续。22 次工具调用中出现一次新增测试失败，模型修正后重验通过。初始运行加恢复总耗时约 1,523 秒（含评测收尾），这是恢复实验成功，不改变最初 600 秒任务失败的记录。轨迹在 `checkout-ledger/resume/`。

DeepSeek 的较大仓库对照 `deepseek-file-snapshots/` 也未完成：1M 窗口、600 秒预算，约 608.44 秒后 partial / max_turn_seconds，oracle **0/5**。12 轮、19 次只读工具调用、工具失败 0 次、没有代码修改，模型占用 590,783 ms，工具占用 7,057 ms；思考记录达到评测器 120,000 字符保留上限。可见结算题的快速成功不能外推到大仓库修复。没有为迎合成功率删掉这次失败，也没有凭这种模型行为给生产循环加入强制编辑或猜测性的终止规则。

这两项均不能算任务成功。`evaluation-metrics.json` 显示：文件任务模型请求占用 580,482 ms / 600,281 ms，工具占用 20,630 ms（部分与模型重叠），生成 77,249 字符思考；工具失败数为 0。压缩任务的 7 次摘要调用累计 407,358 ms / 600,078 ms。前者暴露当前模型配置下的决策迟缓，后者表明 24K 压力条件下频繁压缩会占据主要时间；这不是 1M 默认窗口下必然发生的成本。

前端配置、底栏、协议校验与会话恢复关联测试 **5 文件 / 84 项通过**。另外启动独立生产后端，使用可控本地 provider 执行 60 次工具读取：采集 796 个 wire 事件、623 个重放事件，终态 completed。实际 `llm.model.updated` 的窗口为 1,000,000、来源 fallback、verified=false。将新抓包交给真实前端 hook/store 回放，实时交付和断线恢复 **2/2 通过**。`item.completed` 中有一个标记为截断的快照，回放断言确认最终完整文本保留；没有仅凭这个标志判定回复丢失。

`checkpoint-metrics.json`：202 条消息，序列化检查点 4,730,116 字节。旧参数只能导出末尾 6 条，原始要求丢失；新路径完整 history 往返相等。一次本机测量中 snapshot export 154.79 ms、save 204.92 ms、load 141.67 ms。当时后台还有回归测试和模型评测，数字不是独占性能基准，更不是 Codex/CC 对照排名。

## 能力结论的边界

这轮能验证默认配置贯通、具体恢复缺陷修复、已覆盖故障的回归，以及结算题的完整解决与跨进程恢复。两个模型在大仓库文件快照任务上均未于 600 秒内完成，因此大仓库快速修复能力是本轮明确未达到的结果。尚无同模型、同任务、同权限、同预算下 MiniCode/Codex/CC 多轮对照数据，不能据此声称“吊打”或总体能力已经持平。1M 配置也不等于已经把 1M 实际输入发给所有提供商测过。
