# 真实 Node 持久执行服务与原 MCP 消费链

公开 Codex `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 提供 Node/CUA 注册身份、Code Mode/已接受 MCP 结果和 Guardian 文本/图片消费契约，没有提供 Node 执行器实现。本批是 MiniCode 依据可核对工具契约实现的本地 MCP companion，不宣称等同未公开的 Codex 内部执行器；Computer Use 配套服务仍未实现。

## 实际接通的范围

`js`、`js_reset`、`js_add_node_module_dir` 经真实 MCP initialize、catalog 和 registry 注册。实际 Node kernel 支持可重声明顶层绑定、top-level await、动态 import、模块目录、输出与图片。用户代码在独立 VM context 中求值，普通 npm 包在正常 Node module realm 中运行；直接 static import/`node:process` 按工具契约拒绝。没有新造全局 `tools` 桥接。

服务由原 receiving-thread/AgentSession 持有，沿捕获的执行环境、权限、shell 环境及网络策略启动；远端同样使用原 executor process/stdin，没有控制端裸 Node 回退。普通 MCP 的管理策略及 captured `apply_authority` 均在启动前生效，模型禁用使用本轮 execution snapshot。用户配置的同名服务优先，替换会退役原内置实例。

同一环境和作用域跨回合保留原 PID/状态；权限、MCP authority、环境或服务身份改变后，新调用不会借旧实例。原已准入调用完成、最后 prepared view 释放后才关闭原 kernel、网络和环境 lease。关闭必须取得原 process handle 的真实 ACK；不能因为 wrapper 已结束就宣布清理完成。

实际 active reset 曾触发 Node/V8 fatal。修复后通过原 `NodeWorker` inspector channel 发送 `Runtime.terminateExecution`，等真实 ACK 后回收 Worker。同 generation 的 reset/timeout/cancel 共用原中断 promise，只结算原 pending，新执行等待该中断完成。system Node 和桌面 Electron Node 均覆盖活跃 reset、timeout、cancel。

AsyncLocalStorage 保留异步回调的原 execution/request metadata。完整工具结果后的晚输出带原 ID 进入已有 MCP server logger，不归给下一次调用，不写入 Guardian 的 accepted-result 证据。它们是原始 server-log，不冒称 conversation journal 中新增了工具结果。

## 验证

最终集中 `runtime-regressions.xml` 为 **86 passed / 5 skipped / 0 failed**，其中 Node 核心 **18 passed / 1 skipped**。真实 QueryEngine 自动注册→Code Mode→MCP 原结果→图片 provider/Guardian→跨回合原 PID→线程关闭均已通过；权限收紧、环境同 ID 替换、同名普通 MCP 覆盖及旧 timer 停止均有真实进程/文件结果。

Root 随后显式提供同一个 installed Source，补跑此前因未指定 Source 而跳过的 stdio/Skill 四项，`root-source-native-consumers.xml` **4/4 passed**。与集中批按原节点替换后是 **90 passed / 1 skipped**，不是又一轮完整套件执行。剩余 skip 为该 Windows 宿主的 unelevated filesystem read restrictions 原生限制，没有改成无沙箱测试。

完整调用链、版本、原 fatal/初败、晚输出真实客户端日志属性、独立审阅问题与修正、进程清理结果见 `output/node-runtime-followup-20261016/AUDIT.md`、`audit-facts.json` 及对应 XML。内核边界与工具去重检查通过。`.mjs` 已纳入正式 wheel package data；独立安装包验证由后续交付记录另行确认，本页不借源码测试算安装通过。

## 完整 CI 发现的拒绝启动终态

`ee80a807` 的 Windows harness 发现原 SDK 用例退出失败：该 CI 没有 native sandbox account state，Node 启动被真实拒绝；执行器已关闭 job、runner、网络并释放记录，却因没有 watcher 一直保留 `closed=false`。本批修正实际执行器终态：只有原资源清理全部 ACK、无错误且无 watcher 时发布 `process/closed`；保留原启动失败、`accepted=false` 和未知 exit code。没有配置账号、绕过沙箱或在 Node 外层吞错。

`rejected-start-close.xml` 的当前聚焦回归 **46 passed / 1 native skip**，覆盖 missing executable、observer 拒绝、已真实 spawn 后 readiness 拒绝的 pipe/PTY、清理未 ACK 阴性、Windows 缺账号的真实 Node owner 拒绝及原 SDK CI 用例。该修复不追写成 `ee80a807` 已通过；原 CI 失败和过期 r10 候选保留，后续包从新源码重建。

## Node 22 活跃重置与过程标题

同一次 Linux CI 还暴露 Node `22.23.3` 的 `InternalCallbackScope::Close` 原生 abort。本机使用官方便携版同版本，复现了相同断言；原生崩溃与 timeout 分开记录。定位为 Inspector 中断旧 ALS async-hooks 后端的微任务回调后，async ID 栈未展开。正式实现保留 ALS 和原 Inspector terminate/ACK/Worker-close 顺序：Node 22 使用其实际存在的 `--experimental-async-context-frame`，Node 24 已默认启用该机制且移除了该参数，因此通过官方 `allowedNodeEnvironmentFlags` 决定是否传入。没有按失败重试、移除请求归属或降级执行。

官方 Node 22 源码、下载 SHA、诊断变体和正式修复分开保存在 `output/node-runtime-followup-20261016/NODE22-RESET.md`。`node22-after.xml` **20 passed / 1 native skip**；`node24-regressions.xml` **53 passed / 1 native skip**。实际 Node 22.23.3、系统 24.5.0、Electron 24.18.1 均核实 Frame 已启用，并覆盖 active reset、timeout/cancel 和晚输出的原 request metadata。两批有重叠，不相加；Windows 实测不替代修复后的 Linux CI。

桌面参考 `mcp-tool-activity-label` 的 `Sa` 会优先使用 `js` 参数中的 `title`，清除首尾空白、合并空白并限制 80 个字符。MiniCode 此前总先显示 `server.js`，现在主/子过程和工具详情共用该标题合同，其余工具保持实际身份，原参数和输出仍保留。实际 DOM 与相关四文件 **80/80**，含六项新增标题边界；Source SHA、函数原文和范围限制见同目录 `js-title-evidence.json`。这不是新一轮真实 Node→Electron WS 实机验收。

## SDK 会话持有与实际审批入口

CI 四项 cleanup owner 失败揭示真实寿命问题：SDK 保留会话环境，但每次 prompt 的临时 `AgentSession` 曾持有 Node，导致 prompt 结束就销毁内核，并为已有 borrowers 额外挂关闭任务。现在由实际 `SDKSession` 持有 Node，`RunContext` 显式传递该 owner；WS 仍归原 conversation thread。SDK 关闭先等原 borrowers，关闭原 Node 并取得 ACK，再释放环境，没有放宽原四项精确 task-set 断言。

真实 SDK 验收另发现公开 `query` 入口没有传递审批回调。已补 `approval_handler` → `_query_unclaimed` → `AgentSession`；两次真实 prompt 均经回调允许后执行，不用 Never 绕过 MCP 审批。原 PID 24960、原 owner/handle 和绑定从 41 到 42 保持一致；两次 marker 在审批前均不存在，实际进程关闭及 lease ACK 先于环境关闭，原始属性为 JUnit `actual_sdk_node_owner`。

`output/ci-ee80a807-followup-20261016/focused-03.xml` **75/75**，`sdk-api-04.xml` **14/14**，均无跳过。首批新增用例因缺审批通道失败、旧 Never 尝试和最终真实回调复验分开保留；两批范围不累加为全产品通过数。notes 的六项 namespace 断言按实际已注册的三个 Node 工具核对完整额外 namespace，原 private/journal/cold replay 断言保留。

同批修正 MCP 在 client 尚未创建时取消启动的清理路径：没有 client 就没有待关闭资源，保留原取消，不再以 `None.close` 覆盖。observe scheduler 原 CI 的 callback 等待则曾把配置/仓库/journal 启动也算进一秒；现在以真实 `Observer.start` 为阶段起点，仍保留原 callback 和取消的一秒约束，提前失败显示原终态。没有证据证明该初始超时由 Node 引起，两者不混为同一因果。
