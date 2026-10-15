# Source Guardian Snapshot：连接池、预热与传输生命周期

Source checkout 为 `.tmp/codex-src`，提交 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。本批统一执行实际 **112 passed、0 failed、0 error、0 skipped，进程 exit 0**，其中新的连接池文件 29 个参数化节点全部通过。此前 Snapshot 的 297 passed 不累计为本批连接池证据。

## Source 依据与实现

`ext/guardian-v2/src/async_scorer/sampler/connection_pool.rs` 规定生产预热 8 个 idle socket，socket 上限与 classification 上限分别为 16。socket 从 opening 到 idle、leased、drain 全程持有同一个许可；WS 与 HTTP 共用 classification 限制。池为空时不等待握手，直接选择 HTTP 并后台补充；只有连接 timeout 会触发 5 分钟补充冷却，已有健康连接继续使用。连接寿命为 55 分钟，成功完成 drain 后才回到 idle，失败、取消及未完成请求关闭其 socket。

`sampler/execution.rs` 规定同一分类的 input item ID 与 inference turn ID 跨两次重试保持稳定，而 thread ID 来自实际 lease；每个 WS 的 thread ID 固定并彼此独立。Snapshot 发送完整输入，不借主模型的 continuation baseline，不添加 `previous_response_id` 或保留 Conversation。首个非空 delta 原样发布，后续仅 drain 以保留真实 usage 与连接归属；16 个 active 请求满时优先 supersede 最早已评分的 drain，然后才选择更早的未评分请求。

`protocol/src/thread_id.rs` 与 sampler execution 使用 UUID v7。MiniCode 的 classifier thread 与 inference turn 生产器采用 RFC 9562 的 UUID v7 布局；同一个实际 WS handshake thread ID 也进入每次该 socket 的 body/client metadata/window ID。UUID 是本地创建的真实分类身份，不从日志或 token 推测。

`async_scorer/extension.rs` 只在 scoring enabled、AutoReview、非 Full Access 时后台预热。MiniCode `GuardianAsyncService.schedule_prewarm(context)` 同步检查这些实际 gate 与已支持的 Snapshot mode，默认设置不创建后台 task；同一个正在预热的 sampler/session 去重，task 进入 Host 已有的 service 所有权，关闭时取消并 await。Root 将它接到真实 ToolExecutionContext 完成绑定与实际 model snapshot 切换接点。

`backend/agent/guardian/connection_pool.py` 为分类器维护独立池，复用已有 Responses 的真实 connect/proxy/HTTP boundary。没有修改普通主模型 `ResponsesWebSocketPool` 的单 idle 与 continuation replay 协议。raw classifier WS/SSE 保留原事件和 delta，HTTP 等待 headers 与后续 SSE、WS 后续 frame 使用 Source 默认 300 秒 idle deadline；连接默认 15 秒 deadline。`response.failed` 的实际 budget/policy 非重试码与其他可重试码，以及 `response.incomplete` 的真实 interrupted completion，按 Source Responses parser 和 sampler retry 路由处理。

每个 lease 重新读取实际 registered provider：`refresh_provider_auth` 后 `resolve_adapter_spec`，真实 spec 改变才更新 owned classifier adapter。实际 URL、proxy、认证/配置 headers、session 与可信 classifier-route gate 构成复用身份，凭证更新或过期连接在再次使用前关闭。分类器专用 billing header 与 tier/parent-response attribution 仍由上一批真实 runtime/auth/capability gate 决定。

## 批量验证范围

新文件 `backend/tests/test_codex_guardian_websocket_pool.py` 包含 29 个参数化节点，使用真正的本地 WS server、TCP gateway、POST SSE 与实际注册的 provider/auth producer。覆盖：

- 生产 8 个不同 UUID v7 handshake，预热不发送 inference，完整 Snapshot 的两次复用与实际 usage。
- 原样首个非空 delta、后续输出不重判、完成前许可持续占用、late error 保留原事件并关闭 socket。
- 慢 GET upgrade 时 HTTP 立即分类，timeout 冷却保留健康 socket，恢复到 WS，WS/HTTP 共 16 个 classification 许可。
- stalled HTTP headers 的三次真实断开与重试身份；真实 context cancel 在 headers 前与早期评分后的 drain 中关闭连接。
- WS disconnect 与 Source error code 的有限重试；合法 interrupted completion 保留真实 usage 和 socket 复用。
- 16 个 active 满时 supersession 优先选择已经评分的 drain，保留更早尚未评分的实际请求。
- 实际 registered provider credential 更新与 55 分钟过期造成连接失效；预热 Source gate 与 Host 取消/await 收尾。

统一回归的实际结果见 [batch01.xml](../../output/guardian-ws-source-20261015-batch01.xml) 与 [batch01.log](../../output/guardian-ws-source-20261015-batch01.log)。本批包含的真实 class 与节点数量如下；原 log 没有 pending task 或 async-generator 资源警告。

| 实际 class | passed | failed / error / skipped |
| --- | ---: | --- |
| `test_codex_guardian_websocket_pool` | 29 | 0 / 0 / 0 |
| `test_codex_guardian_async` | 63 | 0 / 0 / 0 |
| `test_codex_guardian_authority_chain` | 9 | 0 / 0 / 0 |
| `test_guardian_async_producer` | 5 | 0 / 0 / 0 |
| `test_guardian_checkpoint_producer` | 6 | 0 / 0 / 0 |

跨批结果必须按实际 `classname + parameterized node` 的最新 JUnit 合并，不把重复回归与此前 297 直接相加。此前 Snapshot 的失败、资源警告与修复证据仍保留在 [2026-10-14-source-guardian-async-snapshot.md](2026-10-14-source-guardian-async-snapshot.md) 和对应原始批次中。

## 单独保留的缺口

- 官方账号相关内容已按用户最新决定移出本轮范围，不计待办或阻塞。此前 hosted Apps account、服务解密、classifier billing route 与官方 auth-owner/401 recovery 的 not_run 仅保留为历史验证边界；本地 credential 更新的真实 lease key 不冒充这些官方账号流程。
- 当前注册 provider schema 未将 Source provider 的专用 timeout override、自定义 CA/cookie 的 HttpClientFactory producer 带入本池。本批使用 Source 默认 deadline 与 MiniCode 现有真实 proxy，不从未知 metadata 猜这些配置。
- Retained Conversation classifier 与 decisions comparison 已在后续独立批次实现并验证，见 [2026-10-15-source-guardian-retained-decisions.md](2026-10-15-source-guardian-retained-decisions.md)，不计入本批 112。Source NodeREPL runner/身份与增强 transcript producer 仍属单独范围；hosted conversation history tools 随官方账号内容移出本轮范围。
- managed model 的 `ignore_rules` execution-prefix 与 live-model early Skip 消费链仍不是本批完成范围。

本报告只记录 Snapshot 连接生命周期范围，不声明整个 Guardian 或 Codex 桌面端已经全等。
