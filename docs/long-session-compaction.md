# 长会话实测：压缩触发、缓存命中、压缩后约束记忆

日期：2026-09-20
范围：`backend/agent/context.py`（预算快照修正）、`backend/services/context_budget.py`、
`backend/agent/turn_iteration_admission.py`（去掉两处压缩后估算拦截）、`backend/tests/test_loop_context_budget.py`
上游对照：cc `services/compact/autoCompact.ts`、`query.ts`；codex `core/src/session/context_window.rs`、
`core/src/compact.rs`、`core/src/session/turn.rs`
前置：`docs/session-gate.md`（同日第 1 项）

---

## 1. 实验设置

- 模型 glm-5.3-flash（supertoken，chat 线），驱动器 `backend.evals.minicode_driver`，
  权限 AUTO，无沙箱。
- 工作区 `.tmp/longrun/make_workspace.py` 生成：7 个源文件 + 6 个测试模块的 `inventory` 库，
  埋 5 个单行 bug（`store.ship` 减法反了、`format_cents` 空格补零、`clamp_quantity` max/min、
  `csvio` 分隔符不一致、`report` 依赖前者），16 个测试中 5 个红。
- 提示词含 5 条硬约束（勿动 `tests/`、最小改动、逐模块跑、最后写带
  `# CHANGELOG (marker: FIXED-ALPHA-7)` 首行的变更日志、报告通过数），用来检验压缩后模型是否还记得。
- 用 `MINICODE_EVAL_PROFILE_JSON` 的 `token_budget.total/response_reserve` 人为缩窗口逼出压缩。

## 2. 结果

| 运行 | 窗口/预留 | 迭代 | 压缩次数 | 终态 | 工作区结果 | cache 命中率 中位(范围) | 首字节 中位 |
|---|---|---|---|---|---|---|---|
| natural | 200K/16K | 35 | 0 | completed | 16/16 通过，CHANGELOG 正确，tests/ 未动 | 97.7% (86–99) | 11.9s |
| compact40k | 40K/4K | 23 | 0 | completed | 同上 | 96.5% (80–99) | 12.7s |
| compact34k | 34K/2K | 25 | 0 | completed | 同上 | 96.5% (88–99) | 10.9s |
| compact36k | 36K/16K | 10 | 1 | **failed/budget_exceeded** | 未修 | 97.1% | 12.4s |
| compact26k | 26K/2K | 18 | 1 | **failed/budget_exceeded** | 未修 | 95.8% | 12.3s |
| compact28k | 28K/2K | 33 | 6 | **failed/budget_exceeded** | 16/16 通过，CHANGELOG 正确 | 96.5% | 11.5s |
| compact36kb（修①后） | 36K/16K | 20 | 10 | failed（压缩器空返回后熔断） | 4/5 修 | 92.0% (67–98) | 11.0s |
| compact36kc（修①②后） | 36K/16K | 27 | 10 | failed（压缩器空返回后熔断，见 §5） | — | — | — |
| compact28kc（修①②后） | 28K/2K | 25 | 0 | completed | 16/16 通过，CHANGELOG 正确 | 96.3% (89–99) | 11.3s |

自然窗口下 35 次迭代、47 次工具调用、prompt cache 命中率始终 ≥86%，首字节 10–13s 全是模型侧推理
（chunk 间最大间隔 ≤2.1s）。两次 `[PromptCacheBreak] prompt unchanged` 是供应商侧驱逐，不是我们的前缀漂移。

**压缩后模型记住约束：** compact28k 经 6 次压缩后仍然逐模块跑测试、没碰 `tests/`、
CHANGELOG 首行 marker 精确、最后全量跑一次；摘要（`## Goal / Constraints / Progress / Critical Context`）
把 5 条约束和 marker 原文都带过去了。这一项没有缺口。

## 3. 缺陷：压缩成功后本地估算仍超阈值就判死（P1，真机 3/3 复现）

三次 budget_exceeded 都是同一条链：

1. 触发 `needs_compaction`：`used > total - response_reserve`。
2. `ctx.compact()` 成功，历史从 ~17–24K 压到 ~8–13K（`context_compacted.after_tokens`）。
3. `context_budget.py:217` 和 `turn_iteration_admission.py:247` 各自再算一次 `needs_compaction`，
   **估算**仍 > trigger，直接 `stopped_reason=budget_exceeded`，回合以 failed 结束，
   模型一次都没再被调用。

用 checkpoint 重建压缩后的上下文、原样发给供应商（`.tmp/longrun/actual_probe.py`）：

| | 值 |
|---|---|
| trigger（36000−16384） | 19 616 |
| 本地估算 `used` | 20 390（其中 tool schemas 估 12 118） |
| 供应商实际 `input_tokens` | **18 757** |

即供应商接受这个 prompt，我们自己算错了 8.7% 就把回合杀掉。根因两层：

- **估算器**：`estimate_text_tokens = utf8_bytes/4`，对 51 个工具 schema 的 JSON 高估约十分之一；
  与 cc/codex 一样是粗估，但他们不拿粗估当死刑依据。
- **修正只朝上**：c9e802f8 把 `get_budget_snapshot` 改成
  `used = max(estimated + max(0, actual − estimate_at_request), actual)`。压缩前
  `actual(17215) < estimate`，修正为 0；压缩后估算本来就高，没有任何向下修正，于是"压缩后仍超"永远为真。
  28k 那次 6 连压（每次压完立刻再压）就是这样来的。

上游对照：
- cc `query.ts:637-647`：只有 `tokenUsage >= blockingLimit`（= 有效窗口 − 3000）才拒发请求，
  自动压缩阈值（窗口 − 13000）压完之后**不复查**，直接 `messagesForQuery = postCompactMessages` 发请求；
  `compact.ts:657` 只把 `willRetriggerNextTurn` 记进遥测。token 计数 `tokens.ts:226` 锚定在最近一条
  assistant 的 `usage`，只对其后的新消息做粗估（有符号增量）。
- codex `session/turn.rs:506-540`：`token_limit_reached` → `run_auto_compact` → `continue` 回到采样循环，
  不复查；`compact.rs:337` 压缩本身超窗时从最旧一条逐条剪、`session/mod.rs:4437 recompute_token_usage`
  压完用估算重置计数；供应商真报 `ContextWindowExceeded`（`turn.rs:1480`）才把窗口标满并终止。
- 我们已有反应式路径：供应商 400 `provider_error_type=prompt_too_long`（实测 supertoken 真返回这个，
  `.tmp/longrun/overflow_probe2.py` 走 `provider_error_details` 分类为 withholdable）→
  `loop_recovery.try_error_withholding_recovery` → `emergency_compact`。也就是说"发出去让供应商判"
  是有兜底的，估算拦截是多余且更差的一层。

### 修法

① 删除两处压缩后的估算拦截（`context_budget.py:217`、`turn_iteration_admission.py:247`），
压缩成功即继续发请求，由供应商裁决；日志记一条 info。压缩**失败**（异常/空返回）的熔断保留。
② `get_budget_snapshot` 的修正改为有符号：`used = estimated + (actual − estimate_at_request)`，
锚定供应商上次报告的 prompt 大小，只对增量做估算，与 cc `tokenCountWithEstimation` 同构。
测试：`test_loop_context_budget.py` 的 `stops_immediately_if_compaction_is_insufficient` 改为
`lets_the_provider_decide_when_the_estimate_stays_high`。

修①后的 compact36kb：不再 budget 判死，但 10 次压缩（每轮一次，因为估算仍卡在阈值上）后压缩器
返回空文本触发失败熔断；同时每次压缩重写前缀，cache 命中率跌到 67%。这正是修②要解决的：
估算跟着供应商实际走之后，压完应当明显低于阈值，不会轮轮重压。

## 4. 与 codex remote-compaction 的对照结论

codex `run_auto_compact`（`turn.rs:1270-1330`）按 `provider.capabilities().remote_compaction` 分三路：
V2 远程（Responses `/compact` 端点，仅 OpenAI/Bedrock 自家供应商声明 `RemoteCompactionSupport::V2`）、
V1 远程、本地摘要。我们 `capabilities.py:205` 的 `native_compaction` 同样只在 responses 线 + api.openai.com
（或显式配置）时开，走 `_compact_native_context`；其他供应商走本地摘要。**结构等价，不需要新做 remote 对应物**。
本轮用的是 chat 线自定义供应商，走的本地摘要，本文缺陷与远程/本地无关。

## 5. 修②后复跑

**compact36kc（36K/16K）**：27 轮、10 次压缩、最后压缩器空返回熔断。但这次的数字说明了另一件事：

| 压缩序号 | 压缩前供应商 input | 压缩后估算 | 压缩后**供应商实际** input |
|---|---|---|---|
| 1 | 19 046 | 8 181 | 20 058 |
| 2 | 20 058 | 8 261 | 19 123 |
| 5 | 19 488 | 8 713 | 19 704 |
| 10 | 20 615 | 8 906 | — |

估算与实际已经一致（修②生效），但压缩后的真实 prompt 仍在 19–20K：系统提示 ≈4.6K、51 个工具
schema ≈11K（实际）、压缩摘要 + 结构化状态 + 恢复的文件 ≈3–4K、保留尾巴 ≈2K。这个**不可压缩底座**
≈ 阈值 19 616，所以每轮都触发压缩，直到 glm 对一个 max_tokens 仅 ~1–2K 的摘要请求返回空文本。
36K/16K 是退化配置：cc 在 36K 窗口下阈值会是 36000−13000−20000=3000，同样不可用；codex 用窗口的
90%（32.4K）就有 13K 余量。§6 第一条说的就是这个。

`compact_context.after_tokens`（8.2K）与压缩后实际 input（20K）差 12K，差额就是 tool schemas：
`context_ledger` 不计工具 schema，`context_compacted` 事件报的是不含工具的数字。前端显示的"压缩到 8K"
和供应商看到的 20K 不是一回事，这是账本口径问题，不影响触发判断（触发用的 `get_budget_snapshot` 含工具）。

**compact28kc（28K/2K，阈值 25 952）**：修①②后 25 轮、**0 次压缩**、completed，16/16 通过，
CHANGELOG 正确，tests/ 未动；供应商 input 从 14.8K 线性涨到 23.5K，`budget.warning` 在 60% 处发出一次，
cache 命中率中位 96.3%（89–99）。修前同配置（compact28k）是 33 轮 6 次压缩后 budget_exceeded。
差别全部来自修②：估算不再虚高 8–10%，真实用量没到阈值就不压。

测试：`tests/test_regressions_context.py`、`backend/tests/test_loop_context_budget.py`、
`test_architecture_review_repairs.py`（有符号修正在无 usage 时退回 `max(estimated, actual)`）；
backend/tests 与 tests 两半全绿（`tests/test_llm_diagnostics_api.py::…without_preset_success`
访问真网 api.deepseek.com 期望 401，是既有网络 flake，单跑通过）。

## 6. 未做 / 观察

- `response_reserve` 缺省 16 384 对小窗口模型偏大（36K 窗口只剩 4.6K history_budget，
  keep_recent 只有 2.3K）；cc 是 `min(model max_output, 20000)`，codex 用窗口的 90%。
  自定义供应商没有 max_output 元数据时我们没法自动缩，本轮没改，留待模型元数据补齐后处理。
- 压缩摘要输出上限 `_compaction_output_limit(0.8)` 在小窗口下与 history_budget 冲突的边界没有单独验证。
- 复现脚本：`.tmp/longrun/run.sh <name> '<profile json>'`、`budget_probe.py`、`actual_probe.py`、
  `overflow_probe2.py`（均需 `.tmp/e2e-gate/backend.env` 的 key）。
