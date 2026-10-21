# APP-NET1 应用网络规则消费者与验证收据

本轮补齐 `application.network` requirements → 应用 owner/config generation → HTTP/WS/Realtime/AWS 的实际消费者链。按一组 APP-NET1 记录，不把三个 provider、各个 transport、凭据链和设置页拆成多组。

固定 Source 为 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。本轮直接核对了 `config/src/application_requirements.rs`、`app-server/src/application_network.rs`、`http-client/src/network_policy.rs`、`http-client/src/route_aware_client_pool/execution.rs`、`http-client/src/response.rs`、`websocket-client/src/lib.rs` 和 `aws-auth/src/{lib,transport}.rs`。原边界调查见 `2026-10-21-application-network-source-boundary.md`。

## 最终行为

- `backend/config_requirements.py` 在 requirements 解析边界独立规范化 application network：表存在而 enabled 缺省时为 true；精确 ASCII host、小写并去掉一个尾点，拒绝 URL、端口、wildcard、重复规范化域名和未知字段。正常 TOML 层级优先级保留，缺省 enabled 不会覆盖下层显式 false。启用后只允许列为 allow 的 HTTPS/WSS host；未列出和 deny 均拒绝，包括 loopback。缺表或 disabled 保持 unrestricted。
- `backend/application_network.py` 提供独立 owner/controller、generation 和弱引用 permit。发布移除权限时撤销原操作；加载失败进入 unavailable；失效代际不能发布，保留旧配置/凭据的 client 不能借用新代际。配置在 `load_config` 入口捕获原 generation，返回前拒绝已被替代的快照。
- `AppBootstrap` 启动时先绑定 managed owner 再加载配置；显式配置重载发布规则，provider/凭据变更使旧代际失效；关闭时撤销并释放原 owner。LLM factory 和 Realtime config 传递同一捕获的 policy，未增加前端开关或内部状态展示。
- HTTP client 在获取 permit 后才创建实际 proxy/transport；逐跳重定向重新准入。HTTP 响应保留原 permit，头等待、body 等待、流式 chunk，以及已缓存的 bytes/text/json 均保留撤销语义。OpenAI Chat/Responses、Anthropic、图片下载前的 DNS 检查、音频/图像 API、模型发现和连接检查均消费实际边界。
- Responses WS 与 Realtime 使用同一策略连接器。Python `websockets.connect` 会自主跟随升级重定向，因此对其实际 create_connection 和 handshake 逐跳准入，最终 socket 持最终目标 permit。受管路径只接受真实 SDK 类合同；未知 connector 在调用和代理解析前明确拒绝，不为测试 fake 留跳过路径。读写等待可撤销；原 socket 的撤销 watcher 也会关闭 idle socket，停止 SDK 自主 ping，并阻止池复用。普通 unmanaged 路径保留既有行为。
- AWS 在原 credential worker 和其 owner event loop 之间接入实际 HTTP transport。每个 botocore Session 的 before-send、默认 resolver 的 IMDS/container fetcher 以及 SDK STS/SSO 类请求进入受管 HTTP；没有全局 monkeypatch。模型目标 permit 在取得凭据和签名期间持续有效，取消或撤销会结束对应 SDK HTTP/进程并回收 worker。受管 transport 继续不跟随 SDK 凭据请求重定向。
- 无法控制实际出站目标的自定义 credential exporter、re-auth 命令和 SDK credential_process 使用 unsupported gate；受限策略下不创建进程，原 unrestricted 命令在权限撤销时结束。
- 策略拒绝、不可用、撤销和不支持 transport 保留真实 reason。LLM 走 `blocked/application_network` 的不可重试分类，raw code 为 `application_network_<reason>`，不会进入五次网络重试或 HTTP/WS fallback。设置页的 discovery/check 返回不可重试的权限结果，Responses probe 保留 typed policy 原因；Bedrock profile setup 的直接策略拒绝返回 HTTP 403。

## 实际验证

最终新增两个测试文件统一运行：**42 passed in 41.62s**。覆盖真实本机 gzip/SSE、真实本机 TLS/WSS 跳转、idle 自主 ping 关闭、双向阻塞、响应流撤销、失效 generation、旧配置不能借新 owner、原进程取消，以及实际 botocore 默认凭据链/STS 消费受管 transport。AWS 响应来自明确的测试 HTTP fixture，没有请求真实 AWS 或读取用户凭据配置。TLS 材料是本轮生成的公开本地测试 fixture，测试运行不依赖 cryptography。

11 个受影响文件共 **222 个不同用例**已有通过记录：

| 测试文件 | 不同用例数 |
|---|---:|
| test_application_network.py | 34 |
| test_application_network_aws.py | 8 |
| test_llm_adapter_factory.py | 26 |
| test_responses_websocket.py | 40 |
| test_bedrock_provider_chain.py | 28 |
| test_managed_config_requirements.py | 34 |
| test_openai_proxy_resolution.py | 12 |
| test_image_generation_proxy_mode.py | 1 |
| test_anthropic_reasoning_off_wire.py | 6 |
| test_bootstrap_shutdown_ownership_chain.py | 4 |
| test_llm_provider_service.py | 29 |

先集中补齐功能和测试，再进行相关批次验证。首批出现的九项同因失败为分类漏接 ProviderErrorType 枚举；修复枚举及用户消息后相关文件 25/25 通过。设置消费复核另发现 Responses probe 把真实 policy StreamEvent 转成 RuntimeError，丢失权限分类；按真实 provider_error_code 保留 typed 原因后三项完整设置 service 用例通过。受管 WS 未知 connector 的静默分支已删除，旧 fake 改为真实 connect 子类合同，关键五项复验通过。最终完整新增 42 项再次统一通过；没有把重复运行计为新增用例。

相关 Python 编译和 scoped `git diff --check` 均通过。没有跑全测试套件、调用真实模型、改动工具 NetworkExecutionPolicy/WFP、使用 GUI/CDP/Playwright、提交或推送。原 hour-live-final 的真实网络失败独立保留，不算一小时通过；最终 hour-live-final-02 由主 agent 另行执行，不纳入这份单元与本机协议测试的通过数。

后续完整CI核实到发布依赖范围问题：开发环境及sidecar lock均为websockets16.0，原pyproject却允许17.3；17改变了所用SDK连接合同。正式依赖现统一为经过验证的 `websockets==16.0`，不在17上跳过逐跳准入，也不改变正在运行的用户sidecar。ROOT后续143项相关集中验证全部通过，其中保留上述真实WSS重定向和idle撤销断言；最终发行/冷安装仍由修复后CI确认。

## 冻结与文件范围

生产修改为：`backend/application_network.py`、`backend/api/routes_llm.py`、`backend/bootstrap/app.py`、`backend/config.py`、`backend/config_requirements.py`、`backend/llm/anthropic_adapter.py`、`backend/llm/bedrock.py`、`backend/llm/bedrock_credentials.py`、`backend/llm/bedrock_network.py`、`backend/llm/errors.py`、`backend/llm/openai_adapter.py`、`backend/llm/responses_websocket.py`、`backend/realtime/transport.py`、`backend/services/bedrock_service.py`、`backend/services/feature_flag_settings_service.py`、`backend/services/llm_adapter_factory.py`、`backend/services/llm_provider_helpers.py`、`backend/services/llm_provider_service.py`、`backend/services/llm_settings_service.py`。共享 `backend/realtime/session.py` 仅接 capture_config 的 network_policy 参数，其余 voice consumer 变更由原 owner 负责。

新增测试为上述两个 application network 测试文件，以及 `backend/tests/fixtures/application_network_tls/{cert,key}.pem`。后端在最终 42 项通过后冻结；主 agent 启动 hour-live-final-02 后，本 agent 只写此文档和 output 机器收据，没有再修改任何 backend、审计脚本或后端资源/JSON/提示词。
