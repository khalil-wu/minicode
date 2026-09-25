# Harness 实测审计，2026-09-24

本轮确认并修复 **1 项 P1 性能问题**：工具任务进度更新重复构建整个 session 能力快照，使 Windows 事件循环为任务状态通知执行大量同步文件系统操作。后续真实模型评测还定位并修复了评测规则未事先告知 agent、测试间作用域 key 泄漏、设置页限时用例不稳定三项本地验证问题，并把外部编码任务断言扩充为可复用的 10 项 oracle。没有把单测通过解释成真机可靠，也没有把未复现的源码差异列为缺陷。

## 源码与工作区基线

- `.tmp/codex-src/codex-rs`：3,707 个 `.rs` 文件。
- `cc/src`：2,039 个文件。
- 我方初始计数：backend 下 749 个 Python 文件；frontend/src.v2 下 431 个 TS/TSX 文件；两个后端测试目录共 374 个 `test_*.py` 文件。
- 接手时已有大量未提交修改。本轮逐处重读再编辑，没有回滚、清理或提交他人的修改。报告结论来自活源码、这次运行的 wire 和剖析数据；旧报告不充当证据。

## F1：任务进度携带完整能力快照，放大事件循环开销（P1，已修复）

**我方路径**：`backend/ws/handler.py:384` 把 TaskManager 的变化接到 `schedule_task_runtime_update`；`backend/ws/session_lifecycle.py:927` 调度通知，原实现每次调用 `runtime_snapshot()`。`backend/ws/handler.py:522` 的完整快照包含能力、权限、provider、工作区与队列，`backend/ws/handler.py:647` 又遍历整个工具目录构建 capability summary。工具开始和结束都会触发这条路径。

**上游对应机制**：`cc/src/utils/task/framework.ts:48` 的 `updateTaskState` 只更新目标任务，并保留其余 AppState；`cc/src/tasks/LocalShellTask/LocalShellTask.tsx:226` 用这条增量更新路径发布命令状态。Codex 的 `thread/status/changed` 载荷只包含 thread_id 和 status，见 `.tmp/codex-src/codex-rs/app-server-protocol/src/protocol/v2/thread.rs:1946`。两者都没有在任务状态变化时重新组装模型工具目录。

**判据**：属于 (b) 上游已有成熟增量通知机制、我方实现更差。不是因为 JSON 长相不同而判错：改前已在真后端量出同步 I/O 和延迟，并出现限定时间内工具批次不能结束的情况。判据 (a) 也检查了；此项没有证据表明是照搬残留，因此不归入 (a)。

### 改前复现

`scripts/capture_harness_audit.py` 启动隔离状态目录下的生产 FastAPI/uvicorn 后端，以本机 HTTP/SSE provider 注入确定的工具调用，再通过真实 WebSocket 收取事件。没有替换 backend 类或伪造 renderer 事件。

```powershell
.venv\Scripts\python.exe scripts/capture_harness_audit.py --out artifacts/harness-audit-2026-09-24/before-tools.json --turns 1 --tool-calls 220
```

220 次针对小文件的读取，在 60 秒接收期限内只有 **158 个 tool_result、0 个 done**。这次诊断主动终止了测试进程；它证明此工作负载不能在该期限内完成，不证明永久死锁。

缩小至 60 次调用并剖析事件循环，定位到完整 session 快照更新，而非磁盘文件读取本身：

| 同一 60 次读取场景 | 改前 | 改后 |
| --- | ---: | ---: |
| run.started 至 done | 33.120 s | 16.227 s |
| 首连接事件数 | 784 | 789 |
| 首连接 JSON 事件序列化体积 | 1,402,962 B | 1,169,214 B |
| `_build_schema_view_for_tool` 调用 | 9,440 | 728 |
| `nt.stat` 调用 | 33,476 | 19,670 |
| `nt._getfinalpathname` 调用 | 25,350 | 12,089 |
| `file_mutation_locks` 生成器进入/退出计数 | 5,042 | 2,158 |

耗时为这台 Windows 机器的单次配对运行，含 cProfile 开销，不是统计基准；调用次数和载荷体积说明减少的是哪部分工作。最终版还额外加入了运行中 inventory 快照与 done 后收尾事件，所以事件数略多。体积使用一致的 `json.dumps` 表示法计算，未把它伪称 TCP 总字节数。

### 修复

- TaskManager 的频繁变化发送 `task.update {partial:true, session:...}`，调用 `runtime_snapshot(include_capabilities=False)`，省去能力目录构建。
- 活跃运行、审批计数、工作区、队列等易变字段仍重新投影。只发送 task_summary/running_tasks 的早期方案在“运行中读取完整快照”实测中造成桌面活动状态残留，已撤销该缩减；最终方案只省略 capabilities，不省略这些状态。
- 显式 `send_task_runtime_update()`、会话恢复和权限切换仍使用完整快照。没有增加缓存、TTL、重试或延迟合并设施。
- `frontend/src.v2/chat/runtimeEvents.ts:949` 对显式 partial 事件合并现有 session；完整事件仍替换快照。任务结束时的空 running_tasks/active_stream_conversation_ids、null active_task_id 和审批清零都会生效，已有 capabilities 被保留。
- 契约类型在 `frontend/src.v2/protocol/streaming-types.ts:894` 和 `backend/ws/events.py:347` 同步声明。

### 改后实测

最终实现的 220 次读取在 60 秒观察窗口内完成 **177/220 tool_result，0 done**；延长同一场景的观察窗口并在运行中读取一次完整 inventory 后，完成 **220/220 tool_result，0 工具错误，1 done**，run.started 至 done 为 **110.718 秒**。这条最终版 wire 共 2,715 条事件，随后重连走完整快照恢复；它在“完整实时交付”和“断线漏事件后恢复”两种模式下，通过实际 `useWebSocketConnection`、校验、事件分派、Zustand 投影回放，全文、终态、session 合并与桌面活动状态断言均通过。先前只发送任务字段的中间版本曾在 52.168 秒内完成 220 次读取，但因桌面活动状态回归已撤销；该数字不代表最终实现的性能。

保留的最终版 60 次调用（含中途完整快照）wire 以 gzip 原样压缩在 `frontend/src.v2/hooks/__fixtures__harness_audit.json.gz`，可随普通前端全量测试复跑。gzip 只压缩存储，没有删除或重新构造事件。测试只映射客户端命令 ID；断线模式在记录的 cursor 后故意停止向 renderer 交付第一连接的事件，再执行真实重连逻辑。断言同时检查聊天 isStreaming 和桌面 `buildUpdateActivitySnapshot(...).activeTurns`，避免只有聊天停止、桌面仍认为任务活跃的漏检。

## 其余范围的审计与证据

| 范围 | 这次检查与实测 | 结论 |
| --- | --- | --- |
| 工具层 | 流式工具提交、读写 gate、读取缓存、结果日志；100/220 次真实后端读取、工具结果和最终 done | F1 已修；未凭重复代码另立缺陷 |
| 提示词组装 | stable/context 层、用户运行时上下文、项目规则、工具 schema 派生；对比实际出站请求前缀 | 前缀追加保持稳定的已测请求没有本地缓存破坏证据 |
| 前后端事件 | 生产 WS 抓包，经实际 transport hook 和 store 回放 | F1 的 partial 契约已同时修正和验证 |
| 流式投影 | 26,421 字符回复，单轮/三轮、重连恢复、工具密集流 | 全文保留；未把 replay 文件截断本身列为用户缺陷 |
| 缓存 | 实际 provider usage；连续工具调用的既有消息与 tools 数组比较 | 真实命中有波动，不能把 provider 的 0 命中直接归因到本地 |
| 熔断 | `backend/services/context_budget.py:69` 的三次失败熔断；`backend/agent/policies/stream_retry.py:116` 的请求重试和独立连接退避；与 `cc/src/services/compact/autoCompact.ts:67`、`cc/src/services/api/withRetry.ts:54` 对照 | 未证实需要修改；由全量回归覆盖失败计数、no-op 压缩和连接/流中断区分 |
| 多 agent | TaskTool 预算归还、子任务作用域、mailbox、读写 gate；真实三 explore agent 会话 | 三个子任务全部 completed；重启后父会话继续完成 |
| 记忆 | workspace/worktree 路径归属、索引版本、受限摘要、后台任务资格与锁、工具读写；与 Codex `ext/memories/src/prompts.rs:25`、`ext/memories/templates/memories/read_path.md:1` 对照 | 真实 note 写入、读回、进程重启后的搜索成功；没有仅凭格式差异修改记忆实现 |
| 权限 | 绑定不可变批准请求、审批恢复、批准/拒绝、真实命令执行 | 已测动作没有绕过拒绝；模型在拒绝后重试的倾向单独记录，不虚称已消除 |

### 被证伪的候选：长回复 replay 截断

`backend/ws/event_log.py:120` 截断超过 16,000 字符的 replay 字符串，曾怀疑它会覆盖完整回复。`before-one.json` 和 `before.json` 捕获到 26,421 字符的 live `item.completed`，对应 replay 的 `item.text` 确实被截断；但单轮和三轮的实际 renderer 回放都保留了恢复快照中的全文，且没有错误或强制二次断线。因此本轮**没有修改**这个路径。数据裁剪与用户可见内容丢失是不同事实。

### 真实 provider，会话一

使用用户指定的 supertoken 网关和其 `/models` 实际列出的 `glm-5.3-flash`。通过本机透明 HTTP/SSE 转发保留出站请求和返回帧，凭据只放在转发请求头中，不保存到报告、请求 JSON 或 WS fixture。

完成的场景：修 `calc.py`、运行失败测试并复测、运行中创建/切换会话、中断、断线恢复、审批等待时断线、同一审批重发、批准、拒绝。共 **4,293 条 WS 事件**，`useWebSocket.session-gate.test.tsx` 原样回放通过。

28 个完整 usage 记录共报告 **500,574 输入 token、366,784 缓存读取 token**，加权命中率 **73.27%**，21/28 个响应有缓存命中。捕获另有 1 条因主动中断而不完整的 SSE 行，不参与 usage 计算。多次调用的已有消息前缀、工具数组完全一致时仍出现过 provider 0 命中，所以没有以该次 miss 为由修改提示词。

拒绝场景中，模型收到明确反馈后仍重试过命令。后台继续请求授权或拒绝越界路径，没有擅自执行。当前证据证明权限边界守住了，不证明模型遵从质量足够好，也不足以将一个具体实现差异判成新的 harness 缺陷。

### 真实 provider，会话二：多 agent、记忆与进程重启

在另一隔离项目运行 12 轮工作：三个 explore 子 agent 分别读取三个文件，父 agent 修复加法并运行测试，保存带 `AUDIT_MEMORY_472` 的用户记忆，读回、继续读文件和修改测试，重启后搜索记忆并继续回答，最后收到 `LONG_SESSION_OK`、`FINAL_SESSION_OK`。

第一段在测试驱动的 520 秒上限处结束，最终捕获到 8 个 completed 轮次，另一个已开始的轮次被测试进程停止。第二段启动新后端进程，从已有 conversation 持久化数据恢复，完成剩下 4 个轮次。**这是 12 个成功轮次加一次受控中断尝试，不是一次从未中断的 12 轮运行。**

第一段 4,664 条 WS 事件、第二段 1,954 条，总计 **6,618**；三个 `subagent.start` 在 115 ms 内到齐，按 start/done 计算的同时运行峰值为 3，三个 `subagent.done` 均为 completed。记忆 note 位于该项目对应的 memory 目录，重启后的 `memory_search` 仍能找到标记。第二段的真实恢复/继续工作/再次断线恢复序列，以实时和漏事件重连两种方式在 renderer hook 中回放，两个用例都通过，保留最初回复全文，最终不处于 streaming。

证据为 `live-long/session-part1.json`、`live-long/session.json`、`live-long-run.log`、`live-long-resume.log`。这段测试没有把错误的模型解释当作 harness 正确性证据：模型曾将“decimal input”解释为十进制整数，新增断言实际为 `add(10,4)==14`，所以不宣称它验证了浮点加法质量。

### 困难编码任务的验证边界

为检验真实问题解决能力，另从已有 `checkout-ledger-r3` 基线提交 `0716a90` 复制了隔离的库存、价格、结算多文件任务。原有测试 **3/3 通过**。任务和工作区外的断言现已固定在 `backend/evals/cases/checkout_ledger/task`、`backend/evals/cases/checkout_ledger/oracle/test_regressions.py`；只复制 `task/` 给 agent，不把 oracle 放入工作区。

supertoken 网关在第一次 `/chat/completions` 请求就返回 Cloudflare 403；独立的 `/models` 请求同样返回 Cloudflare 页面。阻断追踪保留在 `eval-checkout-ledger-supertoken-403.trace.jsonl`，没有把这次零工具调用当作模型失败。随后使用用户指定的本机代理 `127.0.0.1:8317` 和其列出的 `gpt-6-luna`，通过生产 `backend.evals.minicode_driver` 从同一基线做了三次隔离运行：

| 任务文字 | MiniCode 完成情况 | 工作区外断言 | 既有测试完整性 |
| --- | --- | --- | --- |
| 原始 README：未规定不一致报价的处理方式和舍入顺序 | 25 轮、31 次工具调用，模型终态 completed，约 189 秒 | 原 8 项 5/8；扩充后 6/10 | 原断言未删改，但在原文件追加新用例，被“任何字节修改都禁止”的评测门禁判失败 |
| 明确数值报价比较、逐行舍入，并要求新用例放新文件 | 12 轮、23 次工具调用，模型终态 completed，约 188 秒 | 原 8 项 8/8；扩充后 **9/10**，漏掉后行报价校验前不可预留前行 | 原测试文件未改，门禁通过 |
| 明确所有报价须在首次预留前校验，使用已修正的评测器 | 17 轮、24 次工具调用，模型终态 completed，约 165 秒 | 扩充后 **10/10**；可见测试 **7/7** | 原测试文件未改，门禁通过 |

三次完整追踪分别在 `eval-checkout-ledger-local6luna.trace.jsonl`、`eval-checkout-ledger-local6luna-clarified.trace.jsonl`、`eval-checkout-ledger-local6luna-oracle-v2.trace.jsonl`；第三次代码和新增测试在 `eval-checkout-ledger-oracle-v2`。前两次扩充后的分数是对保存的候选代码重新运行外部断言，并非让模型预先看到或重新作答。第三次使用约 301,519 输入 token，其中 270,336 为缓存读取；凭据未写入追踪、任务或报告。

旧 oracle 只有 8 项，第二次候选虽全过，却会在第二行报价错误时先临时预留第一行，再回滚。`backend/evals/cases/checkout_ledger/oracle/test_regressions.py:30` 现在观察单项与批量预留入口，验证所有报价通过前均无预留；另补多商品折扣失败回滚，修正空库存辅助函数、舍入算术注释，并让并发用例使用共享库存的不同 Checkout 实例。新 oracle 对原始基线 **8/10**、第一/第二次候选 **6/10、9/10**、第三次候选及参考实现均 **10/10**；并发断言在参考实现上重复 30 次通过。任务文字同步明确了首次预留前全量校验。结果证明 MiniCode 在这项明确契约的多文件任务上完成修复，但单个任务和前述 12 轮长会话仍不足以声称总体能力已与 Codex 或 cc 持平。

本地评测器在 `backend/evals/minicode_driver.py:490` 保存既有测试文件快照，在 `backend/evals/minicode_driver.py:818` 严格执行字节不变门禁；首轮实测表明，若只在结束时判定而不事先告知 agent，正常追加回归用例也会得到不可解释的失败。现在 `backend/evals/minicode_driver.py:491` 在门禁开启时把约束放进任务文本，门禁仍按原规则执行；`backend/tests/test_minicode_eval_driver.py:153` 覆盖开启与关闭两种请求。此改动发生在前两次真模型运行之后、第三次之前。参考的 Codex/cc 源码未找到同类“评测中既有测试文件字节不变”门禁，因此这项只列为本地评测一致性修复，不归入上游残留或上游机制优劣发现。

## 验证记录与复跑

全量测试使用 `artifacts/harness-audit-2026-09-24/validation/*.command.json` 中保存的后端文件清单。新增回归后两个后端测试目录共 375 个文件，按文件排序分为 188 / 187 两半；最终版的每条后端命令硬限制 590 秒。前端全量、build、桌面全量和协议同步检查也在最终版运行。

| 检查 | 结果 | 命令用时 |
| --- | --- | ---: |
| 后端第一半，188 文件，独占串行 | 1,958 passed、18 skipped，退出码 0 | 490.72 s |
| 后端第二半，187 文件，独占双 worker | 2,642 passed、36 skipped，退出码 0 | 390.75 s |
| 前端全量，独占运行 | 180 文件、2,083 tests passed，退出码 0 | 56.91 s |
| 前端 TypeScript / Vite build | 退出码 0 | 21.14 s |
| 桌面全量 | 56 单测 + 1 真实 Electron BrowserWindow / preload / IPC 测试通过 | 4.48 s |
| 协议同步 | 事件、命令、TypedDict 字段、游标集合检查通过 | 0.45 s |
| 最终版 220 次调用 wire 前端原样回放 | 实时交付、断线恢复 2/2 通过 | 8.29 s |

最终后端命令和结果分别保存在 `backend-half-1-oracle-v2.result.json`、`backend-half-1-oracle-v2.log`、`backend-half-2-oracle-v2.result.json`、`backend-half-2-oracle-v2.log`；前端、构建、桌面与协议的最终记录为同目录下的 `frontend-oracle-v2-stable.*`、`frontend-build-oracle-v2.*`、`desktop-oracle-v2.*`、`protocol-oracle-v2.*`。第二半使用 `pytest-xdist -n 2 --dist=loadfile`。仅向已有 `.venv` 安装 pytest-xdist 3.8.0 / execnet 2.1.2，没有改项目依赖声明。

较早的第二半串行命令在 600 秒上限触及前运行到约 80%；同一文件清单改用双 worker 后，2,642 passed、36 skipped。曾把后端第一半和前端全量同时运行，出现三个 1 秒轮次截止用例和一个设置页等待用例失败；对应后端文件独占复跑 13/13、前端文件独占复跑 62/62 均通过。设置页用例在后来独占运行的全量前端测试中再次失败，因此不能只归因于跨套件争用：组件有明确的 500 ms 延迟加载，而用例依赖 `waitFor` 默认约 1 s 的墙钟期限。`frontend/src.v2/overlays/SettingsCenter.test.tsx:193` 现在用受控计时器推进该延迟，测试文件 62/62、最终前端全量 2,083/2,083 均通过；产品代码未改。

评测器改动后的第二半还揭示一处测试隔离问题：`backend/config_providers.py:122` 会把端点作用域 API key 写进进程环境，原全局 fixture 只隔离了通用 key，导致某个保存 DeepSeek 配置的测试影响后面的 `/api/llm/check` 测试。按顺序独立运行这两个现有用例，改前 **1 通过、1 失败**；`conftest.py:11` 现在在每个用例入口和出口隔离作用域 key，同一顺序改后 **2/2 通过**，第二半最终全量 **2,642 通过、36 跳过**。产品的凭据解析与保存路径没有改变。

```powershell
# 针对新的真实捕获回放，不必替换固定 fixture
$env:MINICODE_AUDIT_WIRE='C:\Desktop\MiniCode\artifacts\harness-audit-2026-09-24\final-tools-220-complete.json'
cd frontend
npm exec vitest run src.v2/hooks/useWebSocket.audit-replay.test.tsx

# 现有多会话/审批 gate 也支持指定本次 live capture
$env:MINICODE_SESSION_GATE_FIXTURE='C:\Desktop\MiniCode\artifacts\harness-audit-2026-09-24\live\session.json'
npm exec vitest run src.v2/hooks/useWebSocket.session-gate.test.tsx
```

主要本地证据：`artifacts/harness-audit-2026-09-24/metrics.json`、`before-tools.json`、`profile-tools-60.profile.json`、`final-tools-220-complete.json`、`live/session.json` 和 `live/request-*.json` / `response-*.sse`。运行数据包含测试会话内容，原始大文件留在 artifacts；可重复的回归 fixture 随前端测试保存。

本报告说明已验证场景和可重放证据，不是对任意 provider、任意时长或所有故障组合的无条件可靠性保证。没有模拟一次成功就宣称覆盖系统休眠、断电恢复或真实网关长期故障。
