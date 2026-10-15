# Source Guardian：保留式 Conversation 与 Decisions 比较

Source checkout `.tmp/codex-src`，提交 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。本批按实际 `classname + parameterized node` 取最新执行，**180 passed、0 failed、0 error、0 skipped**。其中新范围 68 个节点，关联 Snapshot/独立 WS/authority/producer 回归 112 个节点；不累计重叠批次，不重复加入旧 297、112、50 或 13。

逐节点明细：[latest-results.json](../../output/guardian-retained-decisions-20261015-latest-results.json)。

## Source 入口与实际行为

- `ext/guardian-v2/src/async_scorer/extension.rs` 在真实 thread 生命周期创建/替换 ConversationBackend；配置和模型改变使旧 generation 失效。`conversation.rs` 先 reserve 固定顺序，再异步准备；active 与 queued 合计 16。单 worker 只提交完整成功响应，早期评分不释放该 slot。
- `request.rs`、`guardian-context/src/cursor.rs` 选择真实新增 transcript，游标只在完成后提交。复用身份覆盖授权、权限、环境、模型、配置、classifier instructions 与 parent checkpoint。父 history generation 变化、游标越界或阈值超限，从完整当前证据重建；完整输入仍必须满足 Luna 自己的 hard limit。
- `guardian-context/src/retained_instructions.rs`、`reviews.rs` 的交付证明属于 host envelope。MiniCode 的 `AnnotatedItem` 把真实 source revision、review completion identity 与 omission 状态保留在私有 envelope，跟随已完成历史；相同 wire 字段、工具字符串或复制的消息不能冒充交付。新图片按真实引用去重，已提交 prefix、原始 ID 和 opaque reasoning 不重写。
- `sampler.rs::sample_retained` 与 `sampler/execution.rs` 要求 `reasoning.encrypted_content`。完整输出只允许合法 assistant output_text 与非空 encrypted reasoning，逐项/总预算都受限；完整 high/low 必须与早期 delta 一致。late error、EOF、无合法输出、预算溢出或 early low/final high 只阻止历史提交，不能修改已发布的 early baseline。
- `classification.rs` 仅在 Snapshot 启动 Decisions，Conversation 真实记为 unsupported。Decisions 完成后只生成 private comparison telemetry，baseline 必须先经过正常授权复验与 score publication。比较的 disagreement、timeout、HTTP/error、无凭据、unsupported、被取代或任务异常均不改变审批。
- `decisions.rs` 的固定模型为 `gpt-6-luna`，端点 `/v1/decisions`，生产 deadline 6 秒，16 并发，images 8 MiB、response 16 KiB。完整 evidence 不可表示时跳过整份请求，不删除片段。MiniCode 尚无该链的真实 TrustedTool host producer，因此 developer 证据，包括伪造同名 marker，均 unsupported，不抬入 developer rubric。
- `startup.rs::decisions_api_key` 的专用 key 优先；只有 provider 精确为 `openai` 且真实 raw 配置证明未设 base URL 时才允许普通 API-key fallback。新增 `ModelRuntime.get_configured_provider_base_url` 读取注册、models.json、原始 builtin settings 与环境 URL 来源，保留显式空串；解析后默认 endpoint 不能冒充来源事实。SDK 无 runtime/raw config 层时只接受专用 key。

旧 worker 完成或退出后从强引用 owner set 移除，并释放 retained history、cursor、prepared request 与 reservation；Host 关闭仍取消并 await 所有在途 worker/分类/比较。禁用配置也会使旧 generation 失效，不因不再预热而保留旧历史。

## 真实验证

本地测试使用真实 TCP POST/SSE、WS server、实际 httpx response 与受控模型输出。没有读取/打印用户真实凭据，没有向真实外部模型或 Decisions 服务发请求。

| class | 最新 passed | failed / error / skipped |
| --- | ---: | --- |
| `test_codex_guardian_conversation` | 18 | 0 / 0 / 0 |
| `test_codex_guardian_retained_request` | 7 | 0 / 0 / 0 |
| `test_codex_guardian_decisions` | 43 | 0 / 0 / 0 |
| `test_codex_guardian_async` | 63 | 0 / 0 / 0 |
| `test_codex_guardian_websocket_pool` | 29 | 0 / 0 / 0 |
| `test_codex_guardian_authority_chain` | 9 | 0 / 0 / 0 |
| `test_guardian_async_producer` | 5 | 0 / 0 / 0 |
| `test_guardian_checkpoint_producer` | 6 | 0 / 0 / 0 |

关键证据包括连续两次真正 HTTP/WS 的不变 prefix、server IDs 和 encrypted reasoning，第二次仅新增 transcript；实际返回 usage 进入 parent side-call 记账。Root 补充的全链从 HTTP early low，经冻结 FinalExecutableToolRequest 到中央审批缓存，用户真实 revision 改变后同步审查读取新证据并拒绝；原 HTTP 最后完成也不提交旧 history。取消会关闭真实 socket 并释放 permit；旧 generation 的 weakref 可回收；队列按 reservation 而非异步准备完成次序执行。

## 原始批次与修正

| 批次 | passed | failed | error / skipped | 进程 exit | 证据 |
| --- | ---: | ---: | --- | ---: | --- |
| batch01 | 165 | 15 | 0 / 0 | 1 | [JUnit](../../output/guardian-retained-decisions-20261015-batch01.xml)、[log](../../output/guardian-retained-decisions-20261015-batch01.log) |
| batch02 | 61 | 0 | 0 / 0 | 0 | [JUnit](../../output/guardian-retained-decisions-20261015-batch02.xml)、[log](../../output/guardian-retained-decisions-20261015-batch02.log) |

batch01 的 14 个失败是新增测试把真实 float `progress.score` 当作 `.value`；改为使用实际 `inspect().action_risk`，没有添加 production 兼容字段。另一个取消测试错误地把网络首到的 POST 当成最早 admission；改为给每个实际请求独立标签，验证被取消的 Task 0 对应的真正 HTTP response 关闭与 permit 释放，保留原取消断言。该并发 case 独立固定测试 deadline，6 秒生产上限不变，timeout 用例单独验证。

仅重跑受影响两个新文件 61 个节点，其余已通过 119 个节点保留第一次实际结果。合并为 180 个唯一节点；日志没有 pending task、unretrieved exception、unclosed 或 async-generator 资源警告。原始失败不删除，不计作当时通过。

官方账号及 hosted history 按用户要求在本轮范围外，不计待办或阻塞。真实外部服务语义/计费未运行；Source NodeREPL/增强 transcript 与 managed `ignore_rules` 由各自范围单独审计，本批结果不推导这些能力已完成。
