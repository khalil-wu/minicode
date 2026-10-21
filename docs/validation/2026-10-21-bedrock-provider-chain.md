# Amazon Bedrock provider 与原任务请求链

依据公开 Codex Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 的 `model-provider/src/amazon_bedrock/`、`aws-auth/`、app-server Bedrock setup/discovery/GovCloud consumers 实现。设置页面另见 `2026-10-21-bedrock-setup-ui.md`，其静态参考为已安装 Desktop `26.1016.6573.0`；本批没有操作 GUI。

## 实际链路

AWS 配置沿现有 settings、provider history、模型注册与原回合 adapter 捕获传递，不另建模型循环。Mantle 和 runtime 使用 Source 的真实 OpenAI Responses HTTP 地址、区域与模型目录；不把 Chat 或 Responses WebSocket 当成 AWS 可用协议。模型别名继续继承公开 Source 的上下文、提示词、multi-agent 版本和思考强度，未知模型不会被声明为 V2。

最终请求体先由原 OpenAI adapter 序列化，再由 adapter 自己的 HTTPX client 执行 SigV4；Mantle 在签名前去掉其前门不会保留的 snake_case headers。runtime 保留它们。SDK 凭据链支持 profile、环境和原 SDK 的角色/SSO 流程，继承实际选中的直连或代理策略。明确的 Authorization、profile、托管凭据、API key 与环境 bearer 按已核 Source precedence 消费。

外部凭据导出直接执行 argv，保留 Source 的 64 KiB、30 秒和到期前五分钟缓存规则。显式刷新只接受 `aws` 命令，使用原 generation 锁；原认证错误触发一次实际刷新后，对同一请求体重新签名。错误正文先读完再重试，避免 HTTPX 读取已关闭 stream。导出取消会 kill 并等待原子进程，没有匿名请求或另一供应商兜底。

共享 exporter/refresher 按完整 AWS 配置身份归属，使用弱引用随最后一个原 adapter 释放。exporter 的恢复 generation 独立于登录命令：原缓存锁覆盖清空、登录和重新导出整个事务，只有导出成功才发布新 generation；失败或取消保留空缓存。只有 exporter、没有登录命令时，也能在真实 401 后重新导出。初次临时导出失败在发出请求前恢复一次，非法 JSON、缺文件等真实配置错误不被当作匿名请求继续。

托管密钥使用既有 OS vault 的 `provider_auth` scope，不注入工具环境。每次保存使用新的不可变 credential ID，故旧回合稍后创建 adapter 也不会借用新设置里的密钥。设置 payload、历史与磁盘配置只保存引用，不保存明文密钥。

setup 的 profile、environment、Bedrock API key 和 AWS access keys 四种输入使用相同配置发布与 ConfigChange hook。切换方式保留既有刷新命令、请求头与仍在新目录中的已选模型。存在 `credential_export` 时依 Source 提示先清除，托管密钥尚在时不能静默改用环境凭据；拒绝路径不发布配置。GovCloud 提醒核真实 endpoint hostname 和 managed requirements，不把提醒通过宣称为全面安全认证。

## 验证

当前 **28 个不同后端用例**有修复后的通过证据：

- `root-backend-final.xml` 的 20 个 Bedrock 用例中，19 个通过；代理 fixture 误受测试进程已有高优先级 `LLM_PROXY_URL` 影响，修复 fixture 后该节点在 `root-backend-failures-repaired.xml` 通过。
- 新增 setup 保留配置和两类凭据源拒绝，共 3 个不同用例，见 `root-bedrock-onboarding-final.xml` 与 `root-bedrock-onboarding-repaired.xml`。后一收据修正了测试将不可变 header tuple 与 dict 直接比较的问题；同批托管凭据回归也通过，不重复计数。
- 新增 exporter 独立 401 恢复、初始临时失败恢复，以及导出失败/登录失败/取消期间原缓存锁和隔离身份，共 5 个不同用例。最后一次相关集中运行 `root-bedrock-refresh-final.xml` 为 **12 passed、0 failed/errors/skipped、16 deselected**；其余 7 项是被本次共享缓存与错误类型改动影响的既有回归，不重复累计。

真实 loopback HTTP/SSE 验证实际 factory/stream/side query、独立 HMAC 对原始请求体和签名的核算、原 profile 与 credential generation、真实 exporter 进程/cache/cancel、同体重签名、认证失败、区域/目录、代理与 GovCloud requirements。所有失败收据保留。最终 frontend 48 个不同用例的证据见 UI 报告。

没有使用真实 AWS/GovCloud 凭据或调用上游推理，不能将本地签名/协议测试解释为真实账号、服务区域或设备验收通过。最终全项目 CI 与长任务由主验收记录单列。
