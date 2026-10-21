# 应用网络规则与工具进程网络策略的边界

结论：应独立记为 **APP-NET1 / partial_missing**，一组。MiniCode 已有工具进程的 NetworkExecutionPolicy、原生代理与 Windows WFP 消费者；它们不能代替 Source 的 application/network requirements→应用 HTTP/WS/AWS 准入与撤销。GovCloud 检查是已实现的 advisory consumer，不能作为该 enforcement 已完成的证据。

本次只读核对当前 MiniCode 与公开 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。未读取用户配置、环境变量值或凭据，未发起网络请求、操作 GUI、修改 backend 或重跑旧测试。Realtime 生产代码仍保持上一冻结。

## Source 的独立规则

`codex-rs/config/src/application_requirements.rs:1` 明确声明 managed application destinations 独立于 agent network permissions。这里的 application.network 不是 permissions.network：表存在时 enabled 默认 true，域名按精确 ASCII host 规范化，拒绝 URL、端口和 wildcard。启用后仅允许列为 allow 的 host，未列出与 deny 都不获准；受限策略只允许 HTTPS/WSS。表不存在或显式 disabled 时，application policy 为 unrestricted。

| 边界 | 实际 Source consumer |
|---|---|
| 配置与发布 | `app-server/src/application_network.rs:15` 将有效 requirements 转为 DestinationPolicy；`:68` 在启动/显式配置或账号重载时组合并发布。加载失败调用 unavailable，过期 generation 不能发布新规则。`app-server/src/in_process_bootstrap.rs:44` 在连接前 bind 同一 live policy；`:52` activate 绑定 Config。 |
| 网络前准入 | `http-client/src/network_policy.rs:257` acquire 在代理解析、DNS 和 transport 前检查实际 URL；`:169` publish 撤销不再允许的 permit；`:239` invalidate 撤销旧 generation。 |
| HTTP、redirect、响应流 | `http-client/src/route_aware_client_pool/execution.rs:58` 先 acquire，再 resolve/send；`:89` 每一跳重新取得 permit。`http-client/src/response.rs:21` 的 HttpResponse 持有 permit，bytes/text/json/chunk/stream 都继续检查撤销；不只在初次创建 client 时检查。 |
| 普通 provider | `core/src/config/mod.rs:1712` 的 http_client_factory 绑定 application policy；`core/src/client.rs:1182` build_api_transport 使用其 route-aware Api client。 |
| WebSocket / Realtime | `websocket-client/src/lib.rs:165` 连接前 acquire、`:199` 持 permit 连接；`:261` 读写检测撤销。`codex-api/src/endpoint/realtime_websocket/methods.rs:1050` 使用同一 factory 的 WebSocketConnector。 |
| AWS SDK 凭据与 region HTTP | `aws-auth/src/config.rs:20` 为默认 SDK 注入自有 transport；`aws-auth/src/transport.rs:28` 和 `:69` 将 SDK HTTP 路由到应用的 RouteAwareClientPool（Auth route、不跟 redirect）。`aws-auth/src/lib.rs:259` 签名前对实际模型目标 acquire，并在取得 credentials 时保留 permit。 |
| 无法逐目标控制的外部 SDK / 命令 | `http-client/src/network_policy.rs:270` 的 acquire_for_unsupported_sdk 在 restricted policy 下拒绝，并可撤销原 unrestricted operation。`aws-auth/src/lib.rs:76` 通过此 gate 包住调用方 AwsCredentialsProvider；`model-provider/src/amazon_bedrock/auth.rs:118` 把 credential exporter 接到该 adapter；`amazon_bedrock/mod.rs:363` 在 export/auth refresh recovery 前再次使用该 gate。 |

Source library 的调用方可以显式使用 unmanaged policy；本结论针对 managed app-server/desktop 的配置消费者，不声称 Source 的每一个独立库调用都会自动获得受管规则。规则缺省时 unrestricted 的普通 API-key 模型能力，与显式开启 application.network 后的约束，是两个不同状态。

## MiniCode 当前实际消费者

| 边界 | 当前行为与缺口 |
|---|---|
| requirements | `backend/config_requirements.py:400` 保留通用 raw requirements 并处理顶层 network 等字段；`:191` network_constraints 只读取顶层 network。没有 application.network 的专用投影、精确 host policy 或配置生命周期发布者。保留 raw 表不等于 transport 消费。 |
| 模型 HTTP | `backend/services/llm_adapter_factory.py:31` build_wire_adapter 只向 adapter 传 LLMSettings 等 provider 参数；`backend/llm/openai_adapter.py:2478` 创建普通 HTTPX client，`:2480` 是无代理分支；`backend/llm/anthropic_adapter.py:593` 同样只配置 HTTPX/proxy。没有 application policy/permit 参数、逐请求准入或规则更新撤销。 |
| 代理 | `backend/llm/proxy_policy.py:66` 解析 direct/inherit、NO_PROXY 与代理选择。这是路由选择，不能把其中的 direct、trust_env=False 或代理设置当成 application.network host enforcement；其输入也不包含 application requirements。 |
| 模型 WebSocket / Realtime | `backend/llm/responses_websocket.py:121` 与 `backend/realtime/transport.py:158` 直接调用 websocket connect 或 HTTPX WebRTC POST，只带实际 provider headers/proxy。保留原 provider/thread owner 不自动提供 Source 的 application destination permit。 |
| AWS SDK | `backend/llm/bedrock.py:45` aws_session 只给 botocore 配置 profile/proxy；`:129` get_credentials/get_frozen_credentials 可使用 SDK 自身的 HTTP 凭据链；`:137` 签名并无 application policy。没有 Source 的 policy-aware SDK HTTP transport。 |
| AWS exporter/re-auth | `backend/llm/bedrock_credentials.py:29` 直接启动既有 exporter/refresh 命令；`backend/llm/bedrock.py:201` recovery 使用这些消费者。已有超时、输出大小和凭据缓存契约，不存在 application restrictions 下的 unsupported-SDK gate。 |
| GovCloud advisory | `backend/services/bedrock_service.py:21` 读取 endpoint/region 与 requirements raw，在 `:47` 检查 application.network 并返回 isGovCloud/shouldWarn；`backend/api/routes_llm.py:63` 只返回该结果。它不阻止 provider 创建、SDK 凭据解析、DNS、请求发送或已有流。Source 自身的 `app-server/src/request_processors/account_processor/bedrock_gov_cloud.rs:1` 也明确标注 Results are advisory；真正 enforcement 在上述独立 NetworkPolicy。 |

本次未穷举所有插件、hook、MCP 或其他 HTTP consumer 的 application policy 兼容性；已确认的模型 HTTP/WS、Realtime 和 AWS 链已足以成立这一独立缺口，不把未审路径分拆成更多组。

## 已有工具/WFP 不覆盖此边界

`backend/network/policy.py:21` 捕获的是 effective permissions.network、sandbox.network 和顶层 requirements.network；`:55` NetworkExecutionPolicy 按 environment/permission scope 持有原生编译规则。`backend/network/runtime.py:21` 将它绑定当前 turn 的 execution attachments。

真实执行入口 `backend/terminal/executor_runner.py:263` 为远端/本地命令准备对应 network lease；`:270` 把 network/sandbox 参数交给 environment.process.start。`backend/sandbox/windows_native.py:384` 选择执行 child 的 online/offline account，`:416` 为 child launcher 传 proxy-enforced 和 restricting SID。受管网络原生消费者控制的是这些 tool/workbench process，不会由此把后台 Python host 的 HTTPX、websocket 或直接 botocore/exporter 调用包进同一策略。

因此，不撤回现有工具网络权限的实现证据；同时也不以该证据宣称应用自身出站请求已受 application.network 控制。外部系统代理可能自行限制流量，但那也不是 MiniCode 对 requirements 的等价消费。

## 去重方式

APP-NET1 按一组记：有效 application rules→共享配置生命周期→HTTP/WS/响应流准入与撤销→AWS SDK 和无法逐目标约束的外部调用处理。不要把三个 provider、HTTP/WS、SDK、exporter、re-auth 或 GovCloud 提示拆成多个功能组。

该组与浏览器 origin 内容权限、工具进程 NetworkExecutionPolicy/WFP、NR-A2 的 OS/外机政策物理验收不同；它是当前已证实缺 consumer 的实现差距。它也不属于官方账号排除：显式 application 网络规则对可用 API-key/custom provider 和公开 AWS 链同样有意义。当前缺口并不否定这些 provider 在 unrestricted 状态下的可用实现。
