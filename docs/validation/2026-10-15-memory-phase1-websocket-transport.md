# Memory Phase 1 独立 WebSocket 传输

在 CI 绿灯基线 `18e8bd8dc097aa892c76ede818beb15d9b8659f1` 上，接通此前明确保留的 Phase 1 Responses WS 分支。后端仍使用 MiniCode 原有的 Memory coordinator、job store 和模型 adapter。

行为依据为 `.tmp/codex-src` commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`：`memories/write/src/runtime.rs:314` 每次提取创建独立 `ModelClient` / session，`core/src/client.rs:1030、2237` 按 provider 能力和该 client 的实际 fallback 状态选择 WS / HTTP，没有官方账号 gate。

## 结果与资源归属

- 有真实 side-call context 且 Responses WS 已启用时，使用独立的短期连接池；没有 context 的原有 simple_chat 调用保持原路径。没有根据 operation 名称分派传输。
- 原主回合的活动连接、idle 连接、replay baseline 和 HTTP fallback 不借给后台提取。两个并发提取各有自己的连接和请求身份，主回合仍能在原 socket 上继续增量请求。
- 真实握手不支持 WS 时，沿既有运输规则进入 HTTP；401/403 保留原错误，不改发 HTTP。fallback 及 beta header 依据当前 request 的 pool，不能读写主池的状态。
- 完成、失败、取消以及 adapter 关闭均关闭实际 side pool；实际 transport、重试身份和 partial usage 仍记在原 side-call owner。取消不能清掉已经发生的 usage。
- 启动会话 A 的 session/thread、独立 UUID-v7 turn/root、`A:0` window 兼容身份和 Source blob/header 保持一致；被分析的 B/C 继续拥有各自的持久提取结果。前台 history、side-call records 与计费不被提取任务修改。

同批 heartbeat admission 增加真实 native user item ID，因此最终复验另外核对前台 provider 实际收到的 input ID、turn、正文和 content kinds。Phase 1 自己的普通 side input 不被伪装成 accepted user input。

## 验证

正式解释器为项目 bundled Python **3.11.9**，通过独立测试依赖目录运行，没有修改应用 runtime 安装。测试使用同一真实 FastAPI/Uvicorn 端口的 HTTP 与 WS；不是 MockTransport，也没有访问官方账号服务。

| 批次 | 原始结果 | 说明 |
| --- | --- | --- |
| `memory-phase1-ws-batch01.xml` | 50 passed / 33 failed | 共 83 节点；共享 admission hook 接线时 `ContextBuilder.record_admitted_user_input` 尚未落地。保留真实失败，没有给缺方法加 fallback。 |
| `memory-phase1-ws-retry33.xml` | 33 passed | 共享 hook 完成后仅补验原失败节点；与首轮通过节点去重为 83。 |
| `memory-phase1-ws-native-final.xml` | **83 passed / 0 failed / 0 skipped** | native input producer 最终冻结后一次完整复验上述五文件，135.47 秒；这才是当前代码的最终范围，不累加前两轮数量。 |

五文件为 `test_memory_phase1_websocket_chain.py`、`test_memory_phase1_source_request_chain.py`、`test_internal_request_provenance_chain.py`、`test_memory_generation.py` 和 `test_responses_websocket.py`。原始日志、XML 和节点身份位于 `output/ci-repair-20261015/`。

本批新增 10 个真实 transport 场景：实际 scheduled startup 两提取；WS 关闭和 405 的真实 HTTP；主池已 fallback 时独立提取仍用 WS；活动主请求与两提取并发后同 socket continuation；cancel、adapter close、provider failure 的 partial usage/关闭；401、403 不生成 HTTP。

Uvicorn 的旧 WS adapter 原样产生 DeprecationWarning，保留于日志；没有将它们隐藏或计作资源失败。上述结果仅覆盖明确的 Memory/运输范围，不代表全部 remote 或桌面交互已完成。文件身份、Source 身份与 XML hash 见 `output/source-memory-phase1-ws-delivery-20261015/manifest.json`。
