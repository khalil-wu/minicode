# Windows Node companion 启动调查（2026-10-24）

**结论：Windows 原 UI 审计的 iteration 前阻塞真实存在。小型隔离 probe 很快成功；随后完整桌面首轮诊断捕获了约 27 秒的 Node 连接迟滞，但仍在原有 30 秒超时前成功。** 已把迟滞缩小到 executor 接受 `initialize` 后、首条响应被观察到之前；仍未捕获一次带完整关联数据的真实 timeout，现有证据不足以限定生产修复。没有增加 timeout、吞掉异常、关闭 node_repl 或改成未经 Source caller 证实的懒启动。生产文件未改。

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

## 实际桌面首轮临时诊断（19:53–19:54）

在单独拥有的 Windows Electron QA app/backend 中，通过 `PYTHONPATH` 注入该进程专用 `sitecustomize.py` 临时 monkeypatch。只沿原 `audit-ui-alignment.mjs` 的首轮入口执行到第一个真实 turn 的受控 loopback provider POST；没有继续其余 UI 审计，也没有付费 provider 请求。没有修改生产 Python/JS，未改初始化超时、权限 profile、Node companion 注册时机或生产功能开关。

复现入口：[run.mjs](../../output/playwright/node-startup-isolated-20261024/run.mjs)，在 repo 根运行 `node output/playwright/node-startup-isolated-20261024/run.mjs`；使用现有生产 frontend dist，不构建。该脚本自行创建 QA profile/workspace 并正常关闭其 app。临时 hook 只在自身传入的 diagnostic 标识生效，没有注册到用户 Python 或系统环境。当前运行所读 `index.html`、`node_repl.py`、`executor_local.py`、`loop_bootstrap.py` 的字节数和 SHA-256 均保存在 [receipt](../../output/playwright/node-startup-isolated-20261024/receipt.json)，不把这次当前工作树运行冒充为早先 `2af69cd9` 的完全相同产物。

该次**成功但明显迟滞**，不能计为重现原 timeout，也不能用成功抵消原失败。完整记录：[backend-trace.jsonl](../../output/playwright/node-startup-isolated-20261024/backend-trace.jsonl)。下表使用同一后端的 monotonic elapsed 和带时区 ISO 记录；时刻按 Asia/Shanghai 列出。

| 同一 attempt 的事件 | 时刻 | 说明 |
|---|---|---|
| 首轮提交 | 19:54:01.281 | 自有 QA composer，通过原会话创建和 Enter 发送入口 |
| register / client connect 开始 | 19:54:04.123 / .130 | registration 1，client 尚未连接 |
| Node sandbox spawn 进入 / 返回 | 19:54:04.572 / 19:54:05.099 | `windows-restricted-token`，argv 9515 字符；bootstrap SHA 与小型 probe 相同 |
| executor spawn ready | 19:54:05.102 | `accepted=true`，native sandbox owner PID 9888 |
| SDK initialize 写入 / 接受 | 19:54:05.165 / .167 | JSON-RPC method `initialize`、id 0，真实 stdin 296 bytes |
| 第一条 stderr | 19:54:30.926 | 131 bytes；不记录或推断文本内容 |
| 第一条 stdout / SDK response | 19:54:31.117 / .123 | 原 chunk 571 bytes；SDK 解析后 response id 0，570 bytes |
| client connect 返回 / 首次 register 返回 | 19:54:31.130 / .153 | **连接耗时约 27.000 秒**，写入至首 stdout 约 **25.953 秒** |
| 新 manager 重认领 | 19:54:31.714–.750 | 同一 scope、thread owner、companion owner、client；只新增 tools/list id 2 |
| 第一个真实 turn provider POST | 19:54:39.040 | 1 个本地请求，107413 bytes、4 个 tool definitions；不记录 body。提交后约 37.759 秒 |
| 正常关闭 app 开始 / 返回 | 19:54:39.042 / 19:54:40.861 | 只关闭脚本拥有的 QA app |
| executor exited / closed | 19:54:40.130 / .131 | app 关闭之后 owner exitCode 1；不能作为启动时退出错误 |
| backend atexit | 19:54:40.168 | PID 38744；关闭后独立查询 backend PID 38744 与 owner PID 9888 均已不存在 |

本次实际关联数据：两个 manager 分别为 `2895103643232`、`2895104338176`，共用 thread owner `2895100984496`、companion owner `2895102293200`、client `2895103656592`，并保持同一 environment/scope 指纹。唯一 Node executor handle 为 `3120ec9a2061468b996ef8ff0fec63e3`，对应 runner `2895103658560`。只出现 **一次 Node spawn、一次 initialize 写入**。新 manager 的 `connect` 立即返回 connected，不再 initialize。SDK send/receive task 继承首次注册的 context，因此重认领触发的 tools/list id 2 仍带 session 起源 `registration=1`；不能据该字段误判再次初始化。

已记录真实 sidecar 继承的 141 个环境变量及 Node sandbox 捕获环境的名称、长度、SHA-256；不保存变量值、凭据、RPC params/results body、provider request body 或用户内容。RPC 仅记录方法、id、字节数和时间。通用 sandbox hook 同时留下该 QA 自身后台 Git 探测的元数据，它们没有被计为 Node spawn。restricted PID 仍是 native sandbox owner，不声称直接观察到 Node child PID。

该证据排除了这一次迟滞由第二 manager 再启动一个 Node 引起，也显示真实 executor 已在约 1 秒内 ready，initialize 也已 accepted。它尚不能区分其后约 26 秒是在 Node/Electron 启动、Node inspector/bootstrap、OS 安排，还是 stdout/SDK 观察路径的调度中消耗。没有因猜测上述分支而提交生产补丁。

## 仍缺少的证据

需要在**下一次实际失败的同一 attempt**取得以下关联数据，才能定位并限定生产修复：

- 上述临时诊断已经在一次迟缓成功的同一 attempt 取得 register/manager/companion/scope、spawn-ready、实际 initialize 写入和首 stdout/stderr/close 关联。仍需要在真实 timeout attempt 留下同样记录，不能从成功 attempt 外推失败时也是相同阶段或相同身份。
- 还需以同一 attempt 的 Node 入口及 backend 消费阶段时间限定 `initialize accepted → 首 stdout` 这段迟滞；现有记录仅限定两端观察点，不能直接证明 Node 内部某个 bootstrap 操作耗时。
- 当前完整 QA 与旧小型 probe 的环境尚未做同次、逐项指纹比较；旧 probe 没有等量环境元数据。不能仅凭新的 141 项元数据就宣称某个变量已是根因。

没有这段失败现场数据，不能把问题定性为 Source 懒启动差异、并发重复注册、native runtime 污染或某一条 Windows stdio 缺陷。Root 的 Linux UI 成功记录也不抵消此 Windows 未闭合项。

## 后续只读定点结果

同一次成功但迟缓的独立 QA 又记录了实际 Node `process.pid`、Windows `GetProcessTimes` 的 OS 创建时刻、最早 JS 和 response 写入/接收标记。A 的 outer launcher ready → Node OS 创建为18.150658秒，Node 创建 → JS 为411.055毫秒；B 仅从 Node child 环境移除已确认继承的 `CHROME_CRASHPAD_PIPE_NAME`，相应数值为20.029878秒和358.869毫秒。B 的固定 Crashpad CreateFile 拒绝日志消失，但主要迟滞保留。

因此当前定位边界是 **outer launcher 已返回 ready 后、实际 Node workload OS 创建前**；不是已创建的 Node 在 inspector/JS 里卡住，也不是结果写出后缓冲了二十多秒。仍未证明 launcher 的具体前置准备操作，不按 Crashpad 猜测修生产。详细对照、方法边界与6个 owned PID 均已关闭的记录保存在 `output/playwright/node-startup-isolated-20261024/marked-comparison.md`、`.json` 和 `timed-close-check.json`。这些成功迟缓记录不把原 timeout 改写为通过。
