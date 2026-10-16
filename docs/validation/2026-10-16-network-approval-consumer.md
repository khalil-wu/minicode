# 2026-10-16 网络审批消费链

本记录仅覆盖 `backend/network/approval.py` 及其必要测试。原生 Source 网络代理、执行器进程/RPC、命令 runner、永久规则写入和前端审批由同轮其它工作接入；不能由本记录推断这些相邻部分全部通过。

## 依据

- 固定 Source commit：`822e58cc3d666166c7446c5b1ea2e52f5d09594c`。
- `codex-rs/core/src/tools/network_approval.rs`：原进程归属、环境/主机/协议/端口缓存、同执行去重、原环境/当前审阅设置、拒绝结果先于进程取消、ready 结果优先、正常进程结束清理。
- `codex-rs/core/src/tools/approvals.rs`：Hook → Guardian → User；网络默认 available decisions 为 approved、approved_for_session、allow amendment、abort。
- `codex-rs/core/src/guardian/approval_request.rs`：network action 的 JSON、原触发命令、camelCase trigger、executor native cwd、`target_item_id=None`。
- `codex-rs/protocol/src/approvals.rs` 与 `protocol.rs::ReviewDecision`：网络协议 wire 使用 snake_case；network amendment 的 enum/struct 双层序列化。
- `codex-rs/core/src/session/mod.rs::persist_network_policy_amendment`：先应用当前 proxy，再执行 on_policy_applied，最后写 execpolicy；写盘失败不能抹去真实已应用结果。

## 实现范围

- 每个 thread 拥有一个 service，注册原始 execution ID；冻结实际 command admission、argv、cwd、权限和环境身份。注册时 clone 原 turn environment snapshot，直到完成实际 pending review 后关闭。
- 只消费 Source 已允许进入审批的 miss；不实现 Python 域名 matcher、不自动允许 miss、不重跑原命令或 HTTP 请求。
- 当前活跃轮次提供 model、settings、用户审批通道及 Guardian；原始执行提供环境 snapshot 和 trigger。没有当前活跃审阅上下文时拒绝新的 miss。
- PermissionRequest 使用 Source 的 Bash payload，fork 当前 hook 配置并绑定原执行环境的 review context。Guardian 私有环境归属与 JSON 展示信息分开。
- 去重键为环境/ASCII folded host/协议/端口 + 原 turn ID + 原 execution ID。session denied cache 优先；普通单次允许不成为 session grant。
- 持久 amendment 将原请求 approved_host 和实际 amendment.host 分别交给 owner 的 Source 原生规范化及持久化回调。consumer 不自行规范化主机名。
- runtime apply ACK 触发 on_policy_applied；后续写盘失败展示实际 warning，并保留已应用的 allow/session 结果。deny 先记录明确拒绝再取消原执行。
- hard deny 的 Source 格式化消息通过 observe_blocked 进入原 registration。后续断连/普通阻断不覆盖明确审阅拒绝；不会把网络拒绝冒充用户取消整个 turn。
- ProcessFinished 取消尚未完成的 review，不制造新的失败；真实 reader/start failure 可通过原原因完成。已就绪的 review 结果优先于同时发生的 process finish 或 request disconnect。

## 验证记录

正式 Python 入口：`desktop/python-runtime/python.exe -u output/ci-repair-20261015/pytest-python311.py`，`PYTHONUTF8=1`。

| 记录 | 结果 | 含义 |
| --- | --- | --- |
| `network-approval-consumer-batch01.xml` | 16 pass | 初批 consumer 行为；使用真实 Source FrozenExec authorizer，未冒充原生 HTTP 流量验收 |
| `network-approval-query-batch02.xml` / `batch02r1.xml` / `batch02r2.xml` | 初败保留 | 新 fixture 未配置实际 EnvironmentManager，snapshot 为 None；逐步收窄了失败证据，未添加生产 fallback |
| `network-approval-query-batch02r3.xml` | 1 pass | 实际 QueryEngine + local attachment + command 执行；包含真实 builder/state/self-ref metadata，原 snapshot 跨 terminal 持有，finish 后关闭 |
| `network-approval-review-batch03.xml` | 2 pass | 当前 strict turn grant、原 trigger、Bash hook 绑定及 Source Guardian JSON |
| `network-approval-lifecycle-batch04.xml` | 7 pass | 原生主机规范化 callback 入参、runtime applied/disk failure、hard deny observer、ready/cancellation 竞态 |
| `network-approval-final-batch05.xml` | 20 pass / 3 fail | 现行 consumer 与 QueryEngine 20 节点通过；3 原生 fixture 尚把 legacy native FS path 交给 executor URI serializer，未到原生请求 |
| `network-approval-native-batch06.xml` | 3 fail | fixture 改成实际 executor URI 后，原生入口仍以 legacy core PermissionProfile serde 接收，真实 prepareLocal/prepareRemote 返回 PermissionProfileDe 错误；已交原生链修根因 |
| `network-approval-native-batch07.xml` | 1 pass / 2 fail | build06 使用 Source ExecPermissionProfile → core From 修正后，真实 prepareRemote 原生决策、hard deny、consumer ask、runtime apply、Source 永久规则写入/重新 inspect 通过；2 raw TCP sender 不符合 Windows shared SID ingress，未到 consumer，timeout/reset 原记录保留 |
| `network-approval-native-admission-batch08.xml` | 2 skip | POST fixture 已改真实 sandbox child，启动前要求显式已预置的 owned Windows proxy ports；本轮不自动创建账号/修改防火墙，未启动此修改后的 child |

现行测试节点以 batch05 为基线、后续仅覆盖同节点最新结果：**23 节点，21 pass、2 skip、0 未解决 consumer fail/error**。初败、错误入口尝试及跳过分别保留，不能把历史 pass 数直接相加，也不能把 2 skip 算成通过。

原生验收使用 `output/source-environment-r0-plan-20261015/network-runtime-build06-consumer` 独立副本，固定 Source identity；未占用 R0 继续构建的 candidate。build06 的真实非空 executor URI 权限 profile 已越过反序列化，Source host policy/永久规则用例通过。

交付索引：`output/source-network-approval-consumer-delivery-20261016/index.json`，生成脚本 `output/ci-repair-20261015/index-network-approval-consumer.py`。

## 边界

未做外部 SSH/WSL 目标连接或账号授权。真实 prepareRemote 验证是 controller 自身的原生决策 RPC，不冒充远端执行器已建立 HTTP 连接。consumer 当前已证明的 QueryEngine 命令执行不是配置网络策略后的端到端命令代理验收；原生代理和 runner 的整体结果须引用各自实际记录。

Windows shared managed ingress 按真实 sandbox SID 归属，不接纳普通 raw TCP sender。合法 POST fixture 已改为原 Frozen argv、原 execution ID 的 LocalProcessBackend sandbox child，并准备 PID marker、pause/allow/deny、正文一次/零次断言；这些断言本轮未执行。启用它需要显式已完成 provisioning 的固定端口：`MINICODE_NETWORK_WINDOWS_PROVISIONED`、`MINICODE_TEST_NETWORK_HTTP_PORT`、`MINICODE_TEST_NETWORK_SOCKS_PORT`。不会为让测试通过而自动运行 Source 系统账号/防火墙设置。
