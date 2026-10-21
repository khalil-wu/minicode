# 真实一小时任务验收

本轮目标是实际模型和生产 backend 持续完成至少 3600 秒的独立业务计算，同时保留原 child 进程至其真实 follow-up。任务、会话、草稿、仓库与持久状态均在隔离审计目录中；只从已配置 provider 捕获实际模型和凭据，凭据不写入命令行或测试 fixture，不操作或重启用户桌面实例。

## 独立依据

生产 `ModelRuntime` 必须实际声明 Responses 与 V2，不能从模型名字或一个 namespace HTTP 200 推断。原模型公开定义、Source provenance 和 hash 随收据保存。

父 auditor 持续核对 backend、shell、主 verifier 和 child 的原 PID/create-time、原 conversation/run/task/command owner，并持有独立 OS wait；断线期间继续采样。模型可写的 `verification.json` 与 child JSON 仅提供业务结果，不能替代原进程、时长或退出证明。启动第二个进程、换句柄、缩短时长、借用其他会话或伪造成功 JSON 均被拒绝。主任务和同一个 child 的最终 follow-up 必须真实轮询原 command handle。

原 `verify.py` 与 `child_probe.py` 内容保持不可变；每轮按独立 Decimal oracle 校验重复 ID 首条生效、跨 account 冲突、正负金额、零余额和输入不变。A 运行时会在 B 发出新的实际 provider/read_file 请求；A 的队列在原回合结束后接续。计划断线 90 秒，每 600 秒进行一次真实 B 往返，最终核对冷读、任务树/board、关闭退出码与全部 backend 输入未改变。

## 已保留的失败与范围

- `hour-live/`：7.032 秒，在握手时发现 `WebSocketManager.connect` 漏接 `runtime_skill_roots`。关闭退出 0；尚未启动业务任务。真实入口修复与 3 项验证见 `2026-10-21-runtime-catalog-and-queue.md`。
- `hour-live-02/`：13.563 秒，第一条 provider 请求返回 HTTP 400：`Function 'collaboration.create_channel' is not allowed in reserved namespace 'collaboration'`。原 `list_agents` 两次 HTTP 200 只证明那个工具的 native 往返，不证明完整 board 目录准入。Mini 的默认 namespace 与 Source 一致；不能为了通过而静默改名或改走 legacy/flat 工具。

后续验收会显式选择 Source 支持的 `multi_agent_v2.tool_namespace`，先发送完整六个协调工具与九个 board 工具的生产 schema 目录，执行实际 readonly `list_agents` 并回传原结果。收据必须绑定 namespace、完整工具目录及原 endpoint/model/wire；旧子集收据不能代替。只有此后真实至少一小时运行完成，才能将 RT11 标为通过。

完整目录真实预检已通过：`namespace-provider-preflight-full/namespace-preflight.json` 明确使用隔离配置中的 `minicode_agents` namespace，保留全部15个生产schema；实际 `list_agents` 执行、原结果回传与模型确认均完成，两次HTTP 200，耗时7.14秒。MiniCode生产默认仍为`collaboration`，没有自动改名或legacy回退。

当前状态：正式一小时尚未取得通过收据；前述短时失败不计通过。最新构建、GitHub HEAD 与九项 CI 由最终验收记录补充。

## 真实短校准与正式长跑失败

`runtime-live-short-05/result.json` 的真实gpt-6.1-sol/Responses/V2完整目录校准通过：总232.406秒，主业务75.453秒、75批共45150行，child100.282秒；36秒断线期间批次7→43。B使用新的真实read_file读当前进度；同child epoch[1,2]、初次final后原进程持续70.825秒、真实board turn与原句柄轮询、冷读及shutdown exit 0均有独立证据。backend输入未改变。该校准不能替代一小时验收。

`hour-live-final/` 已失败退出，最后独立采样为779.485秒：主原PID8552/handle bg_58477a2b观测701.078秒，child原PID31624/handle bg_b1930d90观测690.219秒，业务699批。原wire在713.766秒及765.5秒记录实际provider error：网关HTTP500、internal_server_error、TLS handshake EOF，既有5次重试耗尽，主回合在oracle完成前结束；审计器据此拒绝通过。完整失败wire、result和console保留，不能把进程曾持续运行等同于模型及最终任务通过。未因此扩大重试、缩短时限或放松验收。

审计失败收据现在保留原conversation/message/turn的实际done、error及provider attempt，不借用其他owner或旧turn，也不把侧会话恢复快照虚构为done。24个client审计用例通过；对上述真实失败wire另生成 `hour-live-final-outcomes.json`，精确关联主轮/队列两组failed和各6个实际provider失败attempt。未改原失败结果、成功断言或重试预算。

应用网络与语音客户端功能冻结后，完整15工具目录再次真实预检通过，收据为 `namespace-provider-preflight-post-network/namespace-preflight.json`。2026-10-21 18:59 +08已启动 `hour-live-final-02/`：主3600秒、同child3660秒、turn budget5400秒、90秒断线、每600秒B实际provider/read_file往返。此时仍是运行中；最终result/原PID/原句柄/board/followup/冷读/关闭/冻结输入全部通过后才记RT11通过。
