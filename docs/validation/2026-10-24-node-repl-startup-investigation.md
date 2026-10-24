# Windows Node companion 启动调查（2026-10-24）

**结论：Windows 原 UI 审计的 iteration 前阻塞真实存在；本次隔离 probe 未复现，现有证据不足以修改生产。** 没有增加 timeout、吞掉异常、关闭 node_repl 或改成未经 Source caller 证实的懒启动。生产文件未改。

## 原始失败

Root 提供基线 `2af69cd9`。原始失败的 desktop/wire/provider/receipt 已另存为 `output/node-startup-investigation-20261024/failed-qa-*`，不依赖滚动审计目录。desktop log 记录：

- 18:56:54.260：独立 QA profile 中没有本地 `.mcp.json`。
- 18:56:57.248：file-state cache 初始化。
- 18:57:24.615：`Failed to start MCP server 'node_repl': TimeoutError`。
- 18:57:24.910：才到 iteration 1；受控 provider 的记录为 0。

该 QA profile 的 sandbox 日志在 18:56:56.176 / .588 有相邻相同 Node 命令 START，18:57:26 再出现一组。不能把 file-cache 日志当作进程实际启动时刻，也不能只凭两个 START 判断存在两个 Node 进程。原始失败没有被计为通过。

## 已确认的调用路径

1. `desktop/backend-sidecar.js` 给后端设置 `MINICODE_EDITOR_NODE=process.execPath`，指向 MiniCode 桌面依赖中的 Electron。
2. `backend/agent/loop_bootstrap.py` 在首次 iteration 之前准备 receiving environment/network scope，调用 `register_node_companion`；`turn_iteration_runtime.py` 在刷新 step environment 后还会重认领。
3. `backend/mcp/node_repl.py` 为捕获的 receiving environment 建立 `builtin:node_repl:<identity>`。这是 MiniCode 自带 companion，不需要本地 MCP 配置。命令是 Electron 的 Node 模式，`--expose-internals --input-type=module --eval <gzip bootstrap>`，设置 `ELECTRON_RUN_AS_NODE=1`。
4. `MCPEnvironmentRuntime` 为该配置绑定实际 environment；`NodeReplMCPClient` 通过 `executor_stdio_client` 启动 executor-owned process，仍由正常 MCP SDK 发 `initialize`、`notifications/initialized`、`tools/list`。
5. `cua_repl.py` 只有显式 selected CUA 声明才启动；没有声明时直接返回。本次不是 `native_cua_runtime.py` 自动启动 installed Source 或 MiniCode native computer runtime。未启动安装包内的 native/CUA executor。

因此，“没有 `.mcp.json` 却启动 node_repl”可由内置注册路径解释，不能直接认定为 fixture 读到了用户 MCP 配置。

## Source 依据与限度

本地 Source checkout 中，`protocol/src/mcp.rs` 识别 `node_repl/cua_repl`；core 的相关位置处理 MCP tool dispatch、model restriction 与 Guardian evidence。该 public checkout 没有 native Node 执行器的进程初始化实现。

- `core/src/tools/handlers/mcp.rs:288` 的 `get_or_init` 缓存 **Code Mode tool definitions**，不是 Node 进程；不能当作 native Node 懒启动证据。
- `codex-mcp/src/connection_manager.rs` 只有 `LazyWhenCached && previous.is_some()` 才允许延迟普通 MCP 启动；`rmcp_client.rs` 对普通 MCP startup 使用声明值或默认 timeout，实际初始化后取 catalog。
- 这些普通 MCP 规则没有证明安装版 native node_repl 应在 schema 注册时立即拉起，也没有证明应在首次调用时拉起。本次未据此改变 MiniCode 生命周期。

## 实际隔离 probe

证据目录：[node-startup-investigation-20261024](C:/Desktop/MiniCode/output/node-startup-investigation-20261024)。脚本先把 state、HOME、USERPROFILE、APPDATA、LOCALAPPDATA 和 temp 指向该目录，再导入后端；只启动自己拥有的后台 Node/Windows sandbox 进程，没有 GUI、模型调用或用户 profile 操作。

使用实际 `NodeReplCompanions`、`MCPServerManager`、local executor 与正常 MCP SDK，记录 spawn、stdin JSON-RPC 方法、stdout JSON-RPC ID/结果字段、exit/closed。没有记录完整 env、凭据或请求内容。压缩后的 Node argv 为 9515 个命令行字符，bootstrap SHA-256 为 `2c92a8d0c41e1e698e2d800cf97611fd5f6e916529dd673b78cb26f438c969cb`，实际版本为 Electron bundled Node `v24.18.1`。

| Probe | 实际结果 |
|---|---|
| bundled full-access | initialize、三项工具 catalog 与 js 元数据成功；到 js 结果 0.766 秒，随后 exited/closed。 |
| bundled Windows restricted-token | 同样成功；到 js 结果 1.329 秒，随后 exited/closed。 |
| restricted-token + network restricted + UI 两个 disable flags | 同样成功；到 js 结果 1.235 秒，随后 exited/closed。 |
| 实际 register_node_companion 冷并发、同 manager | 两个并发注册完成，只出现一次 executor spawn、一次 initialize、一个 PID `18068`；约 1.56 秒 ready。 |
| 同 thread owner、新 manager 顺序重认领 | 复用同一 PID，只新增 tools/list，没有重新 spawn/initialize；约 1.59 秒 ready。最终 `remainingProcesses=0`。 |

对应 [trace.jsonl](C:/Desktop/MiniCode/output/node-startup-investigation-20261024/trace.jsonl)、[result.json](C:/Desktop/MiniCode/output/node-startup-investigation-20261024/result.json)、[network-restricted result](C:/Desktop/MiniCode/output/node-startup-investigation-20261024/result-bundled-restricted-network-blocked.json)、[register-result.json](C:/Desktop/MiniCode/output/node-startup-investigation-20261024/register-result.json)。这些是诊断 probe，不与项目回归测试累计。

Windows restricted 路径中的 PID 指 executor 持有的 native sandbox launcher 进程句柄；本次没有把它当作从 Node 内部读取的 child PID。关闭证据来自该 owner 的 process exited/closed 与其进程树清理。

成功的单次 restricted-token probe 也留下两条相邻相同 sandbox START；它的 executor trace 只有一次 spawn、一个 initialize 和一个 owned process。因此原 QA 的两条 START 不是重复注册根因的充分证据。

诊断脚本第一次填写 network enum 时误用不存在的 `DISABLED`，在启动进程前退出；随后使用实际 `RESTRICTED` 重跑该分支，以上 network-restricted 数据来自真正执行成功的记录，不把脚本设置失败当作产品失败。

## 仍缺少的证据

需要在**下一次实际失败的同一 attempt**取得以下关联数据，才能定位并限定生产修复：

- register/manager/companion scope 的关联身份，是否为同一个 thread、key、client 和 process；实际 spawn 进入、返回、PID 与 sandbox 就绪时刻。
- SDK 的 `initialize` 是否实际写入并被 executor 接受；首个 stdout、stderr、exit/closed 的时刻及 JSON-RPC identity。现有 desktop.log 只有最终 timeout，不能区分 executor 启动、pipe 转发、Node inspector bootstrap 和 SDK 收取响应阶段。
- 失败进程的环境差异元数据（变量名、长度或不含秘密的指纹），以及其原始 process 关闭 receipt。隔离 probe 与 Electron sidecar 的完整继承环境并未证明逐项相同。

没有这段失败现场数据，不能把问题定性为 Source 懒启动差异、并发重复注册、native runtime 污染或某一条 Windows stdio 缺陷。Root 的 Linux UI 成功记录也不抵消此 Windows 未闭合项。
