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
