# MiniCode 后端 Agent 优化方案

> 审计日期：2026-07-10  
> 审计对象：`backend/agent`、LLM 适配层、工具系统、Memory、WebSocket runner、SDK、协议与相关测试  
> 方法：按一次真实用户请求从进入后端到 UI 完成显示的全链路审阅；遵循 Ponytail 原则，优先删除隐式工作、收窄入口、复用现有模块，不引入新的 Agent 框架、事件总线、DI 容器或工作流 DSL。

## 实施状态（前三批，2026-07-10）

前三批已完成并通过验证，本文后续基线数据用于保留审计证据，实施结果以本节为准：

- **请求前 Memory 选择 LLM 已删除**：`ContextBuilder.build()` 不再为挑选 Memory 文件额外调用 `simple_chat()`；Memory index、`read_memory`、`save_memory` 和显式工具路径保留。
- **回合后自动 Memory 抽取已删除**：成功回合结束后不再执行隐藏模型请求，`memory/extractor.py` 及其孤立测试一并移除。
- **工具摘要死路径已删除**：固定返回空值、没有生产 emitter 的 prepare/runtime/action summary 分支和对应 service/test 已移除；兼容协议事件暂时保留。
- **QueryEngine 入口已收敛**：事件过滤合并到 `QueryEngine.submit()`，删除 `submit_filtered()`，WebSocket runner 与 SDK 共用同一生产入口。
- **用户回合改为单次写入**：新增 `ContextBuilder.start_turn()`，先渲染 runtime context、附件和最终用户内容再一次写入 history；`build(state)` 不再接受 `user_message`，raw user 的 matching/replace/dedupe 路径已删除，Hook 上下文通过显式 `append_user_context()` 保持顺序。
- **输入排队已完成**：`SessionRunManager` 按会话、按 conversation 维护最多 20 条的内存 FIFO。忙碌消息不再返回 `agent.busy`，而是发送 `user_message.queue.updated`；支持自动出队、单条取消、删除会话时清空，Stop 当前回复后继续处理队列。运行注册一直拥有 conversation 到 cleanup 完成，避免停止过渡期的新消息越过旧队列。
- **工具结果已统一收口**：成功、失败、拒绝、权限 Hook、部分拒绝、并行取消和超时都经过 `_finalize_tool_result()` 完成持久化、结果投影和 runtime span；`execute_tool_batch()` 是唯一公共批处理入口，串行和 flush 调度函数已私有化。
- **QuerySubmission 已收窄**：删除重复 session 字段和二次转换，直接持有 `AgentLoopSessionContext`；SDK、WebSocket runner 与测试共用同一上下文对象。
- **展示分类已结构化**：后端补齐 `not_found` 等工具问题分类，实时工具结果提供稳定的 projection 字段；前端实时路径不再依赖工具名或错误文本正则。
- **服务端消息 ID 已加固**：客户端未提供 assistant message ID 时使用完整 UUID 熵，队列事件、stream 与最终结果保持同一稳定 ID。

相关 Context、Prompt metadata、流式输出、QueryEngine、SDK、WebSocket、输入队列、工具终态和 runtime protocol 测试均通过，协议同步为 115 个服务端事件、97 个客户端命令。Python 全量测试跑至 100%；本批暴露的 UUID 熵和私有调度契约两项已修复并单独复验，剩余仍是审计时已存在的 3 项 Skill catalog/resolver 失败。它们涉及工作树中由用户删除或缺失的 `skills/*/SKILL.md`，本批未擅自恢复或改变其发布集合。

## 1. 结论先行

MiniCode 的 Agent 已经不是一个简单循环。它具备流式输出、工具并发、权限审批、恢复阶梯、checkpoint、Subagent、Skill、Memory、Hook、Prompt cache、协议投影和前端活动时间线。当前最主要的风险不是“能力不足”，而是一次请求的生命周期被分散在多个超大模块中，同一信息又在后端、WebSocket 和前端重复解释。

本轮后端重点优化建议围绕五个结果展开：

1. **先恢复契约绿灯**：协议同步和全量测试必须通过，不能在红色基线上做结构重构。
2. **每次模型调用都可解释**：删除相关 Memory 选择、回合后 Memory 抽取和工具摘要中的隐式/无效模型调用。
3. **一个用户回合只入历史一次**：消除 `append_user()` 后又 `build(user_message=...)` 的查找、替换、去重路径。
4. **工具执行只有一个公共入口和一个结果收口点**：`execute_tool_batch()` 负责完整契约，串行/并行只是内部调度策略。
5. **让 `QueryEngine` 成为真正的单次查询边界**：它拥有准备、事件过滤和最终化规则；`run_agent_loop()` 聚焦“模型决定 -> 工具执行 -> 继续或结束”。

不建议另起一个 Agent Runtime v2。现有模块已经覆盖所需能力，正确方向是深化已有模块、删除重复路径并建立可验证的不变量。

## 2. 审计基线

### 2.1 代码规模

| 项目 | 当前值 | 结论 |
| --- | ---: | --- |
| 后端生产文件 | 308 个 | 能力边界已较多，不应继续横向加服务 |
| 后端生产代码 | 约 81,348 行 | 优化重点应是调用链和所有权 |
| Agent/工具/LLM/WS 文件 | 138 个 | 跨层契约需要更明确 |
| Agent/工具/LLM/WS 代码 | 约 50,997 行 | Agent 主链已经占后端大部分复杂度 |
| 后端服务端事件类型 | 114 种 | 事件面很宽，新增前必须证明没有现有事件可复用 |
| 前端发往后端命令 | 96 种 | 当前同步脚本能完整覆盖命令 |
| 注册工具名 | 72 个，无重复 | Registry 基础契约当前正常 |
| `AgentEvent` 工厂方法 | 约 38 个 | 事件创建已有集中入口，但仍有直接构造和字段漂移 |
| `QuerySubmission` 字段 | 约 23 个 | 同时混合请求、依赖和 session 上下文 |
| `ContextBuilder` 方法 | 约 67 个 | 历史、Prompt、附件、Memory、预算和 compaction 混在一起 |

### 2.2 超大文件

`python scripts/check-large-files.py` 当前报告 21 个源文件超过 50 KB。与 Agent 主链直接相关的最大文件如下：

| 文件 | 大小 | 主要职责 |
| --- | ---: | --- |
| `backend/agent/loop.py` | 186.4 KB | 装配、Prompt 前处理、流式、工具、恢复、验证、checkpoint、Memory |
| `backend/llm/openai_adapter.py` | 146.4 KB | 多种 OpenAI 兼容协议和流式适配 |
| `backend/agent/prompting.py` | 119.9 KB | Prompt 组装与策略 |
| `backend/agent/context.py` | 116.6 KB | 历史、预算、附件、Memory、压缩、缓存形状 |
| `backend/agent/tool_execution.py` | 108.7 KB | 修复、权限、并发、执行、结果落库和事件投影 |
| `backend/ws/handler.py` | 85.0 KB | WebSocket 命令与 session 协调 |
| `backend/ws/agent_runner.py` | 76.7 KB | 查询启动、turn 累积、持久化、fallback 和 WS 发送 |

文件大小只是症状，不是拆分目标。只有当一个提取动作让调用方更简单、隐藏更多细节或删除重复路径时才值得做。

### 2.3 已执行验证

| 检查 | 当前结果 |
| --- | --- |
| Python 全量测试 | 跑至 100%，3 项失败、3 项跳过 |
| 前端单测 | 103 文件、1,060 项全部通过 |
| 前端 build | 通过 |
| 协议同步 | 失败，4 个服务端事件仅存在于后端运行时集合 |
| 工具名重复检查 | 通过，72 个工具名无重复 |
| 大文件检查 | 警告 21 个文件超过 50 KB |

### 2.4 工作树说明

审计时仓库存在大量未提交修改和删除项。本文针对 **2026-07-10 当前工作树**。尤其是 `skills/` 下若干 `SKILL.md` 正处于删除或缺失状态，测试失败可能同时反映正在进行的用户改动和真实的解析边界问题。实施时不得擅自恢复这些文件，应先明确哪一组 Skill 才是产品希望发布的内置集合。

## 3. 当前 Agent 生命周期

### 3.1 端到端链路

```text
WebSocket user_message / SDK query
  -> AgentRunner 或 SDK 组装 QuerySubmission
  -> QueryEngine.submit_filtered
  -> run_agent_loop
     -> setup runtime / state / context / permissions
     -> expand references / hooks / skills / prompt
     -> ContextBuilder.build
     -> LLM stream
     -> AgentEvent
     -> execute_tool_batch
     -> tool result 写入 context / state
     -> 下一次模型调用或终止
     -> checkpoint / memory extraction
  -> AgentRunner 累积 AgentTurnState
  -> transcript persistence
  -> WebSocket ServerEvent
  -> frontend runtimeEvents / chatStreamEvents
  -> Zustand blocks
  -> UI projection
```

### 3.2 当前事件结束顺序

现有设计大致分两层结束：

- `run_agent_loop()` 负责 `agent.run.started`、phase 更新和 `agent.run.completed`；
- `AgentRunner` 负责 turn 累积、fallback、`done` 只发一次，以及 `session.state_changed` 的 working/idle。

这个分层可以保留，但必须形成稳定顺序，不能让某条错误路径只结束一层。

建议统一顺序：

```text
session.state_changed(working)
-> agent.run.started
-> 0..N 个过程/工具/文本事件
-> agent.run.completed(completed|failed|cancelled)
-> done(同一终态和用量)
-> transcript 已持久化
-> session.state_changed(idle)
```

如果需要先持久化再发送 `done`，应明确这一顺序并写集成测试。核心是 UI 看见完成时，后端不应仍被一个非必要模型调用占用。

## 4. 必须守住的不变量

后续任何拆分都应先用测试固定以下行为：

### 4.1 查询生命周期

- 一个已接受的用户提交只创建一个主 `run_id`。
- 每个 run 恰好有一个终态：`completed`、`failed` 或 `cancelled`。
- `agent.run.completed` 和 `done` 各最多一次。
- `session.state_changed(idle)` 在所有正常、错误和取消路径都能到达。
- 取消必须向子 Agent、工具任务和流式 Provider 传播。

### 4.2 上下文

- 当前用户消息在模型历史中只出现一次。
- 当前轮附件只绑定当前用户消息一次。
- Hook 反馈和运行时上下文顺序稳定。
- 工具调用与工具结果严格配对，不留下 dangling `tool_call_id`。
- compaction 不改变最近回合语义，也不破坏 Prompt cache 前缀。

### 4.3 工具

- 每个 `tool_call_id` 最多执行一次，除非恢复协议明确要求重试。
- 权限检查发生在执行前，拒绝路径也产生可关联的最终结果。
- 串行、并行、prefetch、approval 和 timeout 最终经过同一个结果收口逻辑。
- 一个最终工具结果只写入 context/state 一次，只发一个最终结果事件。
- 并行执行可以乱序完成，但对模型和 UI 的可见顺序必须可预测。

### 4.4 事件

- 所有 UI 可见工具结果都有 `result_kind`、`activity_kind`、`display_summary` 和关联 ID。
- 实时事件与 transcript replay 得到相同的 UI 投影。
- 未知事件不会让整个 session 崩溃，但协议漂移会在 CI 失败。
- 事件中不得泄漏 Provider 内部 reasoning 或未经处理的敏感参数。

## 5. 优先级总表

| ID | 优先级 | 问题 | 直接风险 | 最短处理 |
| --- | --- | --- | --- | --- |
| AG-01 | P0 | 前后端协议 Set 漂移 | 运行时验证和 UI 处理不一致 | 同步 4 个事件，固定 CI；后续删除事件时跨端原子删除 |
| AG-02 | P0 | 全量测试有 3 项失败 | 无法区分重构回归和已有故障 | 统一 Skill 解析来源并隔离用户全局环境 |
| AG-03 | P1 | QueryEngine 是浅转发层 | 生命周期政策散落在 loop 和 runner | 收拢过滤、规范化、准备与最终化；合并两个 submit 方法 |
| AG-04 | P1 | 用户消息 append 后又 build/replace | 上下文路径复杂，缓存和附件更难证明 | 增加单一 `start_turn` 入口，`build` 不再接 user_message |
| AG-05 | P1 | ContextBuilder 约 67 个方法 | Memory、Prompt、历史、压缩互相影响 | 删除隐式 Memory 路径，复用 PromptBuilderV2，聚焦历史与预算 |
| AG-06 | P1 | 相关 Memory 选择每轮额外调用 LLM | 增加 TTFT、成本和不可见失败 | 删除选择器，保留 Memory 索引和已有 `read_memory` 工具 |
| AG-07 | P1 | 完成回合后自动调用 LLM 抽取 Memory | 完成状态后仍占锁，增加成本 | 删除自动抽取，使用已有 `save_memory` 显式持久化 |
| AG-08 | P1 | 工具摘要存在未接通的 LLM 路径和死代码 | 未来误接后产生额外调用，当前增加维护面 | 删除未使用 wrapper/event/service 部分及专属测试 |
| AG-09 | P1 | 工具串行/并行分支多次自行收尾 | 重复落库、事件漏发和行为漂移风险 | 一个公共 batch 入口，一个 finalize 结果函数 |
| AG-10 | P1 | 后端已分类，前端仍二次正则分类 | 新工具跨端修改范围大 | 后端字段变成实时契约，前端仅在旧 transcript hydration 回退 |
| AG-11 | P2 | loop.py 同时承担过多 phase | 修改和测试定位成本高 | 按生命周期提取深模块，不按行数拆 |
| AG-12 | P2 | 114 种服务端事件继续增长 | 协议认知成本和兼容负担 | 新增前先复用现有事件；区分 UI、控制面和 SDK passthrough |
| AG-13 | P2 | AgentRunner 同时做适配、累积、fallback、持久化 | 错误路径难证明 | 保留 WS 边界，逐步把查询政策下沉 QueryEngine |

## 6. P0：先恢复绿灯

### 6.1 AG-01：协议同步失败

`python scripts/check-protocol-sync.py` 当前输出：

```text
[DRIFT] ServerEventType
  only in backend: ['rate_limit', 'session.state_changed', 'stream_event', 'tool_use_summary']
[OK] ClientCommandType: 96 entries match
[OK] Backend registered commands: 96 covered
[OK] Backend literal event payloads: 27 covered
```

前端 `streaming-types.ts` 已声明这 4 种事件，`runtimeEvents.ts` 也有处理，但 `events.ts` 的运行时 `SERVER_EVENT_TYPES` Set 缺值。

建议：

1. 立即让同步脚本变绿。
2. `stream_event`、`rate_limit`、`session.state_changed` 是当前生产路径，应加入前端 Set。
3. `tool_use_summary` 当前生成路径近似死代码。最小风险方案是先同步，再在独立删除提交中从后端 Literal、AgentEvent、runner allowlist、前端类型和 handler 一起删除。
4. CI 同时运行协议脚本；不能只依赖 TypeScript 编译，因为类型 union 正确而运行时 Set 仍可能错误。

不要为此引入 protobuf、OpenAPI 或自制 schema compiler。Python 和 TypeScript 跨语言镜像需要人工维护，但已有同步脚本正是低成本保护。

### 6.2 AG-02：3 项失败测试

当前失败为：

```text
tests/test_slash_command_catalog_alignment.py::test_template_commands_are_backed_by_skill_files
tests/test_slash_command_catalog_alignment.py::test_template_variables_expand_workspace_and_skill_dir
backend/tests/test_skill_layer1_budget.py::test_builtin_core_skills_are_discoverable
```

#### 失败暴露的真实问题

当前存在三种不同的 Skill 来源语义：

- `backend/commands/catalog.py::_skill_template()` 直接读取仓库 `skills/<name>/SKILL.md`，缺失时使用内嵌 fallback；
- `SkillLoader` 按项目、插件、全局、内置优先级解析“有效 Skill”；
- 测试直接使用当前开发机的真实全局/插件目录。

因此当本地内置 `code-review/SKILL.md` 缺失或被删除时：

- Catalog 返回内嵌 `# Code Review Expert` fallback；
- Loader 可能从全局插件解析到另一份 `# CodeRabbit Review`；
- 同名但不同来源的内容被错误地假设为相等。

`docs-writer` 和测试期待的 `init`、`commit-message`、`simplify`、`verify` 当前也没有可被仓库 Loader 发现的实际 `SKILL.md`，导致变量展开为空和内置列表不真实。

#### 推荐修复

1. **确定一个解析入口**：Catalog 和 slash 执行都通过同一个 `SkillLoader`/resolver 获取有效 Skill。
2. **删除内容副本**：移除 `_BUILTIN_SKILL_FALLBACKS` 中与 `SKILL.md` 重复的完整 Prompt。缺文件时命令应明确 unavailable，不应悄悄使用过期副本。
3. **内置列表必须真实**：测试中期待的内置 Skill 要么作为仓库文件存在，要么从产品清单和测试中删除；不能只留空目录或名称。
4. **测试隔离环境**：通过 `tmp_path`/monkeypatch 指定 search dirs，不能读取开发机的 `~/.codex/plugins` 后再断言内置内容。
5. **保留覆盖语义**：若项目/用户 Skill 覆盖同名内置 Skill，Catalog 展示和 slash 执行必须使用同一份有效内容。

这三个失败应在 Agent 重构前解决。否则每次改 Skill、命令或 Loader 都会受到开发机环境干扰。

## 7. 深化 QueryEngine

### 7.1 当前形状

`backend/agent/query_engine.py` 当前主要做两件事：

1. 用 workspace root 重新绑定 `permission_checker`；
2. 将约 23 字段的 `QuerySubmission` 转发给 `run_agent_loop()`。

同时存在：

- `submit()`：返回原始事件；
- `submit_filtered()`：调用 `submit()` 后用 `should_emit_event()` 过滤。

生产调用方 `backend/ws/agent_runner.py` 和 `backend/sdk.py` 都只使用 `submit_filtered()`，原始 `submit()` 没有生产消费者。这是典型浅模块：接口没有隐藏复杂度，反而增加一个调用层。

### 7.2 最短第一步

合并为一个生产方法：

```python
async def submit(self, submission: QuerySubmission) -> AsyncIterator[AgentEvent]:
    permission_checker = submission.permission_checker.with_workspace_root(
        submission.workspace_root
    )
    async for event in self._runner(...):
        if should_emit_event(event):
            yield event
```

然后更新 WS 和 SDK，删除 `submit_filtered()`。如果测试需要原始 runner 事件，应直接测试 runner，而不是保留第二个生产 API。

### 7.3 目标职责

完成后 `QueryEngine` 应隐藏一次查询的外部复杂度：

```text
validate submission
-> normalize workspace / permissions / session context
-> prepare state and context
-> start run
-> execute model/tool iterations
-> enforce terminal event invariants
-> checkpoint when required
-> filter public events
```

它不应负责：

- WebSocket JSON 编码；
- transcript UI shape；
- Electron session 状态；
- Provider 具体协议；
- 工具内部实现。

### 7.4 收窄 QuerySubmission

`QuerySubmission` 当前同时包含：

- 本次请求：`user_message`、state、metadata；
- 稳定依赖：LLM、registry、artifact store、permission checker、settings、budget；
- session 上下文：workspace、session/task ID、managers、callbacks、cancel event。

项目已经存在 `AgentLoopSessionContext`，但 `QuerySubmission` 又平铺了它的大部分字段，再通过 `to_session_context()` 重新组装。建议直接让 `QuerySubmission` 持有一个现有 session context，删除重复字段和转换方法。

不要再创建 `AgentDependencies`、`QueryOptions`、`RuntimeServices` 三个新容器。一个请求对象加一个已有 session context 足够。

### 7.5 渐进迁移

由于测试和 Subagent 中有大量 `run_agent_loop()` 直接调用，不能一次强制全部迁移。建议：

1. 先合并 QueryEngine 的两个 submit。
2. 固定 QueryEngine 的集成测试：过滤、取消、终态、权限 workspace。
3. 从 `run_agent_loop()` 提取纯迭代核心，但保留原函数签名作为兼容包装。
4. WS 和 SDK 只走 QueryEngine。
5. 新测试优先测试 QueryEngine；旧的细粒度 loop 测试在相关行为迁移时逐步更新。

兼容 wrapper 必须很薄，并有删除条件，不能长期维护两份生命周期。

## 8. 简化 ContextBuilder

### 8.1 当前重复路径

首次回合当前大致执行：

```text
ctx.append_user(user_message)
-> 追加 Hook feedback/additional context
-> next_user_message = user_message
-> ctx.build(user_message=active_user_message, state=state)
-> 查找 history 中相同 raw user message
-> 替换为带 runtime context / attachments / memory 的 rendered content
-> 再检查 history 是否已有同一内容
-> 必要时才 append
```

这导致 `ContextBuilder` 需要维护：

- raw 和 rendered 两种用户消息；
- 倒序查找相同 user 内容；
- 重复内容防护；
- first turn 与 continuation turn 两种 append 规则；
- Prompt cache 前缀替换说明。

这些复杂度来自“先写入，后渲染”。

### 8.2 目标 API

建议形成两个清晰动作：

```python
await ctx.start_turn(user_message, state)
ctx.append_user_context(hook_feedback)
messages = await ctx.build(state)
```

语义：

- `start_turn()` 负责附件 plan、运行时上下文、必要 Prompt section、最终模型可见 user content，并只 append 一次；
- Hook feedback 保持顺序，但使用明确的 runtime/context 方法，不冒充第二条用户请求；
- `build(state)` 只负责系统 Prompt、预算、compaction、history 输出，不再接受 `user_message`；
- 工具结果后的下一次迭代直接 `build(state)`；
- 恢复提示等真正的新 user-role 控制消息通过一个明确方法追加。

这个新方法是为了删除现有查找/替换复杂度，不是为了再加一层 Builder。

### 8.3 Prompt 所有权

项目已有 `PromptBuilderV2` 和 `PromptParts`。应继续复用：

- `PromptBuilderV2`：系统 Prompt section 选择与组合；
- `ContextBuilder`：history、预算、附件、compaction、provider stateful history；
- `AgentState`：当前运行状态和 prompt_context 诊断。

不要再引入 `PromptService`。如果 ContextBuilder 中某段只是 Prompt section 的纯构造，应移回现有 PromptBuilder；如果涉及历史和 token budget，则留在 ContextBuilder。

### 8.4 缓存与附件验收

API 简化不能牺牲现有 Prompt cache 优化。必须增加测试证明：

- 首次发送给 Provider 的 rendered user bytes 与 history 保存内容相同；
- 后续工具迭代复用相同前缀；
- 图片和文档只绑定一次；
- Hook context 顺序固定；
- stateful history 模式不重写旧工具结果；
- compaction 前后最近用户回合内容一致。

## 9. 删除隐式模型调用

### 9.1 相关 Memory 选择器

`ContextBuilder._build_relevant_memory_context()` 当前：

1. 扫描最多 200 个 Memory header；
2. 构造选择 Prompt；
3. 调用 `llm.simple_chat()`；
4. 解析模型返回的文件名；
5. 最多读取并注入 5 个 Memory 文件。

这个调用发生在主要模型调用之前，因此直接增加首 token 时间。它还存在这些问题：

- 用户和 UI 看不到这次调用；
- 失败只记 debug，行为不可预测；
- 选择本身消耗 token 和 Provider 配额；
- 200 个 header 规模会进一步扩大选择 Prompt；
- 项目已经有 `read_memory`、`save_memory`，以及可选 vector memory 工具。

#### 建议

删除：

- `_build_relevant_memory_context()`；
- `_select_relevant_memory_headers()`；
- 只服务于选择器的 parse/render/age helper；
- `build()` 中对应 await 和注入参数。

保留：

- 小型 Memory index/说明放入系统上下文；
- `read_memory` 由 Agent 在确有需要时调用；
- `save_memory` 由 Agent 在用户明确偏好或耐久项目事实出现时调用；
- Memory 工具自己的陈旧提醒和路径限制。

这让检索成为可观察工具动作，也让“没有必要的请求”不支付额外模型调用。

### 9.2 回合后自动 Memory 抽取

`run_agent_loop()` 在 run completion 事件后，如果回合成功且有回复，会调用 `extract_turn_memories()`。该函数再次使用 LLM，并有最长约 4 秒的等待路径。

直接后果：

- UI 可能已经看到 run complete，但 generator 尚未返回；
- WS runner 的 `done`、锁释放或 idle 转换可能继续等待；
- 每个成功回合增加一次不可见模型成本；
- 自动抽取出的事实未必值得长期保存；
- 与已有显式 `save_memory` 能力重复。

#### 建议

删除成功回合后的自动抽取调用、`backend/memory/extractor.py` 及只覆盖该生产路径的测试。将长期 Memory 写入交给已有工具和 Agent Prompt 指导。

如果未来数据证明用户确实需要自动 Memory：

- 必须是完成状态和 session 锁之外的后台任务；
- 必须可关闭、可观测、可去重；
- 失败不得影响当前回合；
- 先用产品指标证明收益，再恢复，而不是保留未验证复杂度。

### 9.3 工具动作摘要 LLM

当前存在：

- `backend/services/tool_use_summary.py::generate_tool_use_summary()`；
- `backend/agent/loop.py::_generate_tool_use_summary()` wrapper；
- `backend/agent/loop_process_events.py::runtime_action_summary_event_async()`；
- `AgentEvent.tool_use_summary()` 和前端 handler；
- 一组 `test_runtime_action_summary.py` 测试。

但当前生产调用链没有使用 async summary 生成器；`action_summary_for_tool_calls()` 还固定返回空字符串。也就是说，代码和测试维护了一条“可能额外调用模型”的路径，却没有真实消费者。

建议：

1. 删除未使用 wrapper、async event generator 和对应测试。
2. `tool_prepare_process_event()` / `runtime_action_summary_event()` 目前固定返回 `None`，删除其调用分支和函数。
3. 工具展示摘要直接使用已有 `display_summary_for_result()` 和结构化工具元数据。
4. 若 `tool_target_label()` 仍被流式工具准备逻辑使用，将这个小纯函数放到已有 `tool_projection.py`，不要为它保留整个 summary service。

最终每次模型调用应属于以下可观察类别之一：主决策、明确重试、明确 compaction 或用户触发的工具。默认目标是隐藏调用数为 0。

## 10. 收敛工具执行

### 10.1 当前结构

生产主调用只从 `run_agent_loop()` 进入 `execute_tool_batch()`，这已经是正确方向。但 `tool_execution.py` 内部的 `flush_queue()` 和 `execute_serial()` 仍表现为公共函数，并且不同拒绝、审批、并行、串行、超时分支多次直接调用 `store_result_events()`。

风险不是函数数量本身，而是“结果收尾规则分散”：

- 某个分支可能漏掉 runtime span；
- 某个分支可能没有 `display_summary`；
- 某个分支 append context，另一个分支不 append；
- timeout、reject、approval hook 和普通执行可能形成不同事件顺序；
- 新工具策略需要修改多处。

### 10.2 目标流水线

```text
prepare
  normalize id/name/arguments
  repair deferred or malformed calls
  dedupe / stagnation guard
-> authorize
  permission policy
  hook decision
  user approval
-> schedule
  serial or parallel
  prefetch / timeout / sibling cancellation
-> execute
  registry.run
-> finalize
  normalize ToolResult
  append context/state once
  emit output delta and final result once
  close runtime span once
```

### 10.3 最短代码方向

- 保留 `execute_tool_batch()` 作为唯一公共 API。
- 将 `flush_queue()` 改为 `_flush_queue()`，`execute_serial()` 改为 `_execute_serial()`，明确它们只是调度细节。
- 提取一个 `_finalize_tool_result()`，让成功、失败、拒绝、timeout、duplicate 和 cancelled 都经过它。
- `_finalize_tool_result()` 复用现有 `store_result_events()`，先集中调用，不必立即重写后者。
- 将 permission wait/start/completed span 的顺序写成一个小状态表并测试。
- 并行 batch 保持“并行执行、确定顺序提交结果”的现有意图。

### 10.4 结果契约

每个最终 `ToolResult` 至少应得到：

```text
tool_call_id
tool_name
status
is_error
content 或 artifact reference
display_summary
result_kind
activity_kind
display_scope
panel_hint
started_at / ended_at / duration_ms
```

字段可分布在事件 data 和 runtime span 中，不需要创建一个新的巨型 DTO。关键是所有终止路径都能提供 UI 和诊断必需的信息。

### 10.5 必测场景

- 单个读工具成功；
- 单个写工具需要批准、允许和拒绝；
- auto 模式 diff review；
- 并行读工具乱序完成但顺序提交；
- command 失败导致 sibling cancellation；
- batch timeout；
- prefetched result；
- duplicate idempotent call；
- malformed/deferred call 被修复或拒绝；
- Subagent scope / coordinator guardrail；
- 大结果转 artifact；
- 每个场景 context 只出现一个最终 result。

## 11. 让后端成为展示分类的事实来源

### 11.1 当前重复

后端已有 `backend/agent/tool_projection.py`，并在结果事件中生成：

- `result_kind`
- `activity_kind`
- `display_summary`
- `display_scope`
- `panel_hint`
- `display_label`

前端 `chatSurfaceState.ts` 和相关 projection 仍按工具名、参数和错误文本做大量正则推断。这让一个新工具的显示分类跨越多个语言和模块。

### 11.2 目标契约

后端对 **实时事件** 提供完整分类；前端只做组件映射：

```text
result_kind=edit       -> Diff / file change cell
result_kind=command    -> Command cell
result_kind=search     -> Search/source cell
result_kind=subagent   -> Subagent cell
result_kind=skill      -> Skill cell
unknown                -> Generic tool cell
```

### 11.3 渐进迁移

1. 统计所有 `ToolResult` 创建点缺失字段的比例。
2. 在 Registry metadata 和 `tool_projection.py` 补齐缺失分类。
3. `_finalize_tool_result()` 保证默认值完整。
4. 前端实时协议将字段逐步改为必填。
5. 旧 transcript 的缺失字段只在 `transcriptHydration.ts` 推断一次。
6. 删除前端实时投影中的工具名和错误文本正则。

不要让后端返回 React component 名称。后端负责领域分类，前端负责视觉选择。

## 12. 缩小 run_agent_loop 的正确方式

### 12.1 当前承担的职责

`run_agent_loop()` 当前同时处理：

- session context 解包；
- settings、budget、state、runtime 初始化；
- reference 展开；
- Hook；
- Skill 自动激活；
- Prompt/tool schema 准备；
- checkpoint 恢复；
- context build；
- Provider stream；
- tool call 聚合和 repair；
- 工具执行；
- recovery ladder；
- verification / coordinator；
- run complete；
- checkpoint 保存/清理；
- Memory 抽取。

### 12.2 不按行数拆

以下拆法没有价值：

- 把 500 行搬到 `loop_helpers.py`，参数仍有 15 个；
- 每个 phase 新建一个 class；
- 用 EventBus 让模块互相订阅；
- 用通用工作流 DSL 表达 while loop；
- 为了 `check-large-files` 低于 50 KB 机械切文件。

### 12.3 推荐边界

优先使用已有深模块：

| 生命周期部分 | 所有者 | loop 只保留什么 |
| --- | --- | --- |
| submission/session 规范化 | `QueryEngine` | 接收已准备对象 |
| 系统 Prompt section | `PromptBuilderV2` | 调用并记录摘要 |
| 历史、附件、预算、compaction | `ContextBuilder` | `start_turn` / `build` |
| Provider 流式协议 | LLM adapter | 消费统一 StreamEvent |
| 工具修复、权限、执行、结果 | `execute_tool_batch` | 传入 calls，消费 events |
| run 状态 | `AgentRuntime` / `AgentState` | 更新有限状态 |
| checkpoint | 现有 checkpoint module | 终态时调用一个入口 |
| WS turn/transcript | `AgentRunner` | 累积、持久化、发送 |

删除隐式 Memory 及死摘要路径后，loop 自然会缩小。随后再提取真正有闭合输入/输出的 phase。

### 12.4 推荐的迭代核心

目标代码形状应仍然容易顺序阅读：

```python
while not state.is_terminal:
    messages = await context.build(state)
    response = await stream_model(messages, tool_schemas, state)

    if response.tool_calls:
        async for event in execute_tool_batch(...):
            yield event
        continue

    finalize_answer(response, state, context)
```

恢复、预算和验证策略可以在三个明确检查点插入，但主循环仍应看得出这个核心，而不是被框架回调隐藏。

## 13. AgentRunner 边界

`backend/ws/agent_runner.py` 当前做了大量必要工作：

- 建立 `QuerySubmission`；
- 给事件附 conversation/message/turn ID；
- 累积文本、工具记录、引用和 artifact；
- 处理 compaction persistence；
- 合成“工具完成但无最终回复”的 fallback；
- `done` 去重；
- 保存 transcript；
- session working/idle。

这些并非都应搬进 QueryEngine。推荐边界：

### QueryEngine 负责

- Agent run 是否开始、完成或取消；
- 公开事件过滤；
- Agent 内部 checkpoint；
- 事件内部 run/tool 关联完整性。

### AgentRunner 负责

- WebSocket/session/conversation ID 装饰；
- turn projection 和 transcript persistence；
- WS fallback 文案；
- `done` 与 session working/idle；
- 网络发送失败的处理。

当 QueryEngine 保证终态后，AgentRunner 的 fallback 和 `done` 逻辑会更容易证明，但无需把它们重写成另一个 service。

### 13.1 会话输入队列

输入排队属于 WebSocket session 的运行所有权，不属于 Agent 推理核心。实现集中在 `SessionRunManager`，`handler.py` 只负责协议装饰和启动下一回合，避免把 FIFO 逻辑散落到 AgentRunner、ConversationRepository 和前端。

#### 契约

| 项目 | 当前规则 |
| --- | --- |
| 隔离单位 | 同一 WebSocket session 内按 `conversation_id` 隔离 |
| 顺序 | 每个 conversation 严格 FIFO |
| 上限 | 每个 conversation 最多 20 条待执行消息 |
| 持久性 | 内存队列；30 秒重连宽限期内复用 session，session 最终销毁时不恢复 |
| 取消 | `user_message.queue.cancel` 按稳定 assistant message ID 删除单条 |
| Stop | 取消当前 run，不清空队列；cleanup 后继续下一条 |
| 删除会话 | 立即清空该 conversation 的待执行队列 |

没有增加数据库表或持久化队列。排队输入只在当前交互 session 中有意义；如果以后确实需要跨服务重启恢复，再将同一契约落到 ConversationRepository，而不是同时维护两套队列。

#### 生命周期

```text
user_message while busy
  -> enqueue
  -> user_message.queue.updated(status=queued, position=N)

current run cleanup
  -> dequeue oldest
  -> user_message.queue.updated(status=dequeued)
  -> normal user_message handler
  -> register next run
```

出队到 run 注册之间使用 conversation dispatch guard，避免 cleanup、取消和新命令同时触发重复 dispatch。更关键的不变量是：run 从 register 到 cleanup 一直拥有 conversation；即使底层 `asyncio.Task` 已取消或完成，也不能在 cleanup 前把它视为无主状态。否则停止过渡期刚到达的新消息会越过已有队列。

#### 终态与错误

- `queued`、`dequeued`、`cancelled` 共用一个事件类型，避免为三个瞬时状态扩张协议；
- 队列满时先发送 `cancelled(reason=queue_full)`，再发送可恢复的 `agent.queue_full` 错误；
- 客户端没有传 assistant ID 时，服务端生成完整 UUID，并在 queue、stream、done 中保持稳定；
- 单条取消找不到目标时幂等返回，不影响当前 run；
- conversation A 的运行和队列不阻塞 conversation B。

测试覆盖忙碌入队、FIFO、单条取消、停止过渡期顺序、真实 WebSocket 自动出队，以及运行结束后无悬挂 cleanup task。

## 14. 恢复、取消与终态

### 14.1 当前值得保留的设计

- `_complete_run_record()` 已有幂等保护；
- loop 末尾有兜底 completion，防止部分 break 路径留下 running；
- AgentRunner 有 `_send_done_once()`；
- 非自然终止会保存 checkpoint；
- 成功完成会清理 checkpoint；
- 子 Agent 有取消传播入口。

这些都是正确机制，不应在重构中删掉。

### 14.2 需要补的集成测试

对以下每条路径断言完整事件顺序和最终持久化：

- 正常最终回答；
- 工具成功但 Provider 没有最终文本；
- 空回复恢复后失败；
- max iterations；
- token budget exceeded；
- stream timeout；
- rate limit；
- 用户 interrupt；
- Python task cancellation；
- approval 等待期间取消；
- 并行工具执行期间取消；
- checkpoint resume 成功和再次失败。

### 14.3 Checkpoint 接口收敛

loop 当前直接决定何时 save/clear，这是合理的第一版。深化 QueryEngine 时，可将终态政策收拢为一个现有模块入口：

```python
finalize_checkpoint(session_id, state, context_snapshot, run_record)
```

这个函数只隐藏 save/clear 分支，不要创建 checkpoint manager class。验收重点是成功不会留下旧 checkpoint，失败/中断可以恢复。

## 15. 可观测性与性能指标

项目已有 runtime spans、usage、cache metrics 和 Agent events。应复用它们，不引入新的 telemetry SDK。

### 15.1 每轮必须记录

| 指标 | 用途 |
| --- | --- |
| `time_to_first_model_request_ms` | 区分 setup/context 变慢 |
| `time_to_first_token_ms` | 用户实际等待时间 |
| `time_to_done_ms` | 完整锁占用时间 |
| `model_calls_total` | 控制成本和隐式调用 |
| `model_calls_by_reason` | primary/retry/compaction/other |
| input/output/cache tokens | Prompt 和 cache 效果 |
| context build duration | ContextBuilder 重构验收 |
| tool calls success/failure/timeout | 工具可靠性 |
| approval wait duration | 区分用户等待和执行慢 |
| checkpoint save/resume result | 恢复能力 |
| terminal event count | 发现重复或缺失终态 |

### 15.2 目标

- 普通无工具问答：只有 1 次主模型调用，没有 Memory/summary 隐式调用。
- 有 N 轮工具决策：模型调用数等于可解释的决策/重试/compaction 次数。
- 删除相关 Memory selector 后，TTFT 不应回退，并应消除 selector 耗时长尾。
- 删除 post-turn extraction 后，`agent.run.completed` 到 `done/idle` 的非必要等待接近 0。
- 所有工具终止路径关联字段完整率为 100%。
- 终态事件重复率为 0，缺失率为 0。

先在当前版本采基线，再用相同 fixture 比较。不要为漂亮数字压缩必要的 Provider 重试或 compaction。

## 16. 逐文件修改清单

| 文件 | 建议操作 | 验收重点 |
| --- | --- | --- |
| `backend/agent/query_engine.py` | 合并 submit；持有 session context；逐步收拢生命周期 | WS 与 SDK 行为一致 |
| `backend/agent/loop.py` | 删除隐式 Memory、死摘要/prepare 分支；主循环聚焦迭代 | 所有终态和恢复测试通过 |
| `backend/agent/context.py` | `start_turn` 单次写入；`build` 去 user_message；删除 Memory selector | Prompt bytes、附件、缓存不回退 |
| `backend/agent/tool_execution.py` | batch 唯一公开入口；串/并行私有；统一 finalize | 每个 call 恰好一个最终结果 |
| `backend/agent/tool_projection.py` | 成为结构化展示分类事实来源 | 所有工具结果字段完整 |
| `backend/agent/loop_process_events.py` | 删除固定返回 None 和未使用摘要逻辑；保留真实 process text 处理 | 不再制造假进度事件 |
| `backend/services/tool_use_summary.py` | 删除 LLM summary；必要小 helper 移入已有 projection | 无生产/测试悬空引用 |
| `backend/memory/extractor.py` | 删除自动回合抽取路径 | 成功回合不再有尾部 LLM 调用 |
| `backend/tools/memory_tools.py` | 保留并明确 `read_memory`/`save_memory` | Memory 操作可观察、受权限控制 |
| `backend/ws/agent_runner.py` | 保留 WS/persistence 边界；依赖 QueryEngine 终态契约 | done/idle 恰好一次 |
| `backend/ws/run_manager.py` | 集中 conversation run 所有权与 FIFO | Stop、cleanup、新输入不破坏顺序 |
| `backend/ws/handler.py` | 忙碌时入队；cleanup 后调度下一条 | 每条只注册一个 run，消息 ID 稳定 |
| `backend/ws/handlers/misc.py` | 增加单条 queue cancel 命令 | 取消队列不影响当前 run |
| `backend/agent/message.py` | 删除最终确认无消费者的事件 factory；补必需字段 | protocol sync 和 replay 通过 |
| `backend/ws/events.py` | 与实际事件原子同步 | 115 类型与前端一致 |
| `backend/commands/catalog.py` | Skill 内容只从统一 resolver 获取；删内嵌 Prompt 副本 | Catalog 与执行内容一致 |
| `backend/skills/loader.py` | 提供可注入/可隔离的 search dirs | 测试不读取用户全局插件 |
| `backend/commands/slash_commands.py` | 变量展开使用与 Catalog 相同的 resolved Skill | `CLAUDE_SKILL_DIR` 永不静默变空 |
| `frontend/src.v2/protocol/events.ts` | 同步运行时事件 Set | 同步脚本通过 |
| `frontend/src.v2/chat/transcriptHydration.ts` | 仅在旧记录水合时兼容缺失分类 | 实时路径不再猜测 |

## 17. 分阶段实施与小提交顺序

每一步都应独立可回滚、全量测试可解释。

### 阶段 0：恢复基线

1. 修复协议 Set 漂移。
2. 统一 Skill resolver，并用临时目录隔离测试。
3. 明确实际内置 Skill 清单，修复 3 项失败测试。

完成标准：Python 全量测试、前端单测、build、协议同步全部通过。

### 阶段 1：删除无价值工作

1. 删除固定返回空/None 的工具准备与 action summary 路径。
2. 删除未使用的工具摘要 LLM generator 和专属测试。
3. 删除相关 Memory selector。
4. 删除 post-turn Memory extraction。

完成标准：普通回合无隐藏模型调用；Memory 工具仍可用；完成到 idle 不再等待抽取。

### 阶段 2：上下文单次写入

1. 为当前行为补 prompt byte、附件、Hook 顺序和 cache 测试。
2. 引入 `start_turn()`。
3. `build()` 删除 `user_message` 参数。
4. 删除 matching/replace/dedupe 分支。

完成标准：相同 fixture 的模型请求消息语义不变，代码路径减少，cache 指标不回退。

### 阶段 3：工具结果单一收口

1. 将串行/并行函数私有化。
2. 引入一个 finalize helper。
3. 逐个迁移 reject、timeout、approval、parallel、serial 分支。
4. 补齐投影字段。

完成标准：工具矩阵全部通过，context/事件无重复，前端实时分类不需要工具名正则。

### 阶段 4：深化 QueryEngine

1. 合并 submit API。
2. QuerySubmission 直接持有已有 session context。
3. 将准备和终态政策从 loop 外围迁到 QueryEngine。
4. 保留薄的 `run_agent_loop()` 兼容入口。

完成标准：WS、SDK、Subagent 的生命周期一致；QueryEngine 的接口比实现明显更简单。

### 阶段 5：按真实边界缩小超大文件

1. 删除后重新运行 large-file 检查。
2. 只提取仍有闭合输入/输出的 phase。
3. 将历史兼容集中到边界，删除内部双路径。

完成标准：文件缩小是职责收敛的结果；没有新增 `helpers.py`、manager class 或参数搬运层。

## 18. 测试策略

### 18.1 单元测试

- QueryEngine：workspace permission 重绑定、事件过滤、取消、终态；
- ContextBuilder：单次 user、附件、Hook、compaction、cache prefix；
- tool execution：prepare/authorize/schedule/finalize 矩阵；
- projection：每个 tool metadata 得到稳定 kind/summary；
- Skill loader：项目、插件、全局、内置覆盖优先级，全部使用临时路径；
- protocol：Python Literal 与 TypeScript runtime Set 同步。

### 18.2 集成测试

- SDK 和 WebSocket 对同一 fake LLM/tool fixture 产生一致核心事件；
- transcript replay 与实时流投影一致；
- interrupt 时主 run、子 Agent 和工具都停止；
- checkpoint 保存和恢复；
- Provider rate limit、timeout 和空回复恢复；
- 工具成功但最终文本缺失时 fallback 恰好一次。

### 18.3 性能回归 fixture

固定三类场景：

1. 无工具短问答；
2. 3 次只读工具后回答；
3. 读、写、批准、验证完整回合。

比较：模型调用数、TTFT、总时间、Prompt token、cache hit、事件数、context message 数。测试不需要真实 Provider，可用带可控延迟的 fake adapter。

### 18.4 每阶段命令

```powershell
python -m pytest -q
python scripts/check-protocol-sync.py
python scripts/check-large-files.py

cd frontend
npm test -- --run
npm run build
```

`check-large-files.py` 当前是警告型质量门槛。在相关文件尚未低于阈值前，应至少保证不继续增长，并在每阶段记录变化；不要为了让脚本变绿做无意义拆分。

## 19. 可直接删除候选

以下项有明确证据支持删除，但仍应在提交前用 `rg` 和测试再次确认当前工作树没有新消费者：

- `QueryEngine.submit_filtered()`，合并进 `submit()`；
- `action_summary_for_tool_calls()` 的恒空实现；
- `run_agent_loop()` 中仅供测试调用的 `_generate_tool_use_summary()` wrapper；
- `runtime_action_summary_event_async()`；
- 固定返回 `None` 的 `runtime_action_summary_event()`；
- 固定返回 `None` 的 `tool_prepare_process_event()` 及围绕它的无效分支；
- 工具摘要 LLM Prompt、normalize 和 timeout 逻辑；
- 最终无生产消费者的 `tool_use_summary` 事件；
- `_build_relevant_memory_context()` 和其 LLM selector helper；
- 成功回合后的 `extract_turn_memories()` 调用；
- 若无其他生产消费者，`backend/memory/extractor.py` 及专属测试；
- Catalog 中复制 `SKILL.md` 内容的 `_BUILTIN_SKILL_FALLBACKS`；
- 前端投影中已由后端字段覆盖的实时工具名/错误文本正则；
- 没有 UI 消费者的 `project-turn.summaryItems`。

删除顺序应从叶子开始：先删调用，再删 helper、类型和测试，最后跨端删事件字面量。每一步保持协议和测试一致。

## 20. 明确不做

本轮不建议：

- 引入 LangChain、LangGraph、Temporal 或另一套 Agent framework；
- 用通用 EventBus 代替直接 async iterator；
- 为每个 lifecycle phase 新建 class；
- 新建 DI container 打包所有依赖；
- 将 Python/TypeScript 协议迁到复杂代码生成体系；
- 为降低行数创建无领域含义的 `helpers.py`；
- 保留隐式 LLM 调用但只把它们放到后台；
- 同时维护 raw/filtered 两套 QueryEngine 生产 API；
- 让前端组件名进入后端协议；
- 为历史 transcript 的所有旧形状污染实时主路径；
- 在没有指标前增加自动规划、反思、Memory 或 summary 模型调用；
- 以恢复用户已删除文件的方式“修复”当前脏工作树。

## 21. 最终完成定义

当以下条件全部满足，可以认为本轮后端 Agent 重点优化完成：

- Python 全量测试、前端单测、build 和协议同步全部通过；
- Skill Catalog、slash 执行和变量展开使用同一 resolver，测试不依赖用户全局插件；
- 普通回合没有 Memory 选择、Memory 抽取或工具摘要的隐式模型调用；
- 当前用户消息和附件在 context 中只写入一次；
- 同一 conversation 的运行中输入严格 FIFO，Stop 和停止过渡期都不会让新消息越过旧队列；
- `ContextBuilder.build()` 不再负责查找并替换相同 raw user message；
- `execute_tool_batch()` 是唯一公共工具执行入口；
- 所有工具终止路径通过同一个结果收口点；
- 每个 UI 可见工具结果都有完整结构化展示字段；
- 前端实时投影不再按工具名和错误文本重复猜测；
- QueryEngine 只有一个生产 submit API，并拥有清晰的查询生命周期；
- 正常、失败、取消、预算、timeout 和恢复路径各有且只有一个终态；
- `agent.run.completed` 后没有非必要模型调用阻塞 `done/idle`；
- 超大文件的缩小来自职责删除和深化，而不是参数搬运；
- 本轮没有新增 Agent 框架、运行时依赖或预留式抽象。
