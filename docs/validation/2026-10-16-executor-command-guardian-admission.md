# Executor 命令、权限与 Guardian 调用链

本批以 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 为行为依据。MiniCode 保留自己的后端；安装版 Source `codex.exe exec-server` 仅作为隔离的实际互操作目标。官方账号登录、授权与 hosted 服务不在范围内。

## 已接通的调用链

- 文件 URI、Windows/POSIX 路径与原始字节路径使用 `ExecutorPath`。审批、grant、diff、工具调用与最终结果使用该次接受的环境、注册对象和权限快照，不能把远程 URI 交给控制端 `Path.resolve`，也不能在任务结束时重新选择另一个环境。
- `FrozenExecCommand` 绑定真实目标 argv、URI cwd、权限 profile 和原始工具请求 digest。接收方的 exec-policy 约束交给固定 Source 原生 parser，不能用 Python 命令前缀匹配代替其语义。
- 进程沿原 `BackgroundCommandManager` 管理。首输出 observer 在实际启动前注册；启动确认、stdout/stderr、exit、取消、stdin 和关闭都属于原 attachment。远程 process/session ID 与本机 PID 分开；冷恢复没有真实远程关闭证据时保留待清理回执。
- FS 请求始终携带显式权限上下文。进程初始 sandbox 选择按 Source `sandboxing/src/manager.rs::select_initial`：Disabled/External 及完全不受限的 managed profile 选择无 OS sandbox；这不是启动失败后退回无沙箱。此前错误地把 Disabled profile 送进 Windows sandbox 的失败记录保留，不能据此断言安装版 Source 不支持 Git。R2 的实际 Source Git 全链已另外通过。
- 工具 schema 的环境选择参数、Windows shell 提示和 login 参数来自当前真实选择及 `stable_environment_tools` gate。没有可用环境时，工具和最终错误不回落到本机执行。

## Guardian 的实际目标与只读权限

Source `ext/guardian-reviewer/src/settings.rs::ReviewerTurn::into_request` 对继承环境施加只读 ceiling；`protocol/src/models.rs::intersect_with_read_only` 保留拒读限制并限制网络。MiniCode 同时在 reviewer 环境输入及命令冻结阶段施加这项限制，避免外层只改 SandboxPolicy、实际 frozen command 仍持有原来的完全访问权限。

Guardian 的 full-access 判定、异步分数授权身份、复用 key、权限证据来自真实目标快照。remote/empty 选择不再解引用不存在的本机 sandbox policy。图片从原 executor 读取字节后验证，拒读图片不发起文件读取，也不拿控制端的同名文件替代。

超时且仍在执行的工具继续持有原 attachment 和清理任务。能力刷新时退休 manager 等待已捕获的 CodeMode cell、工具清理和原 RPC；它不能动态等待包含自己在内的新清理集合。用户终态返回与真实后台清理分别保留，会话占用在原执行释放前持续存在。R0 原 cancellation-resistant 场景的后续窄复验见其 batch08；没有扩展原 2 秒时限。

## 本批验证与边界

`output/ci-repair-20261015/executor-root-guardian-batch04-20261016.xml` 包含 **262 项，260 passed / 2 failed / 0 skipped**。两项初败来自新测试错误地读取完成后已释放的 `_processes` 临时表；改为读取真实完成记录 `_commands` 后，`executor-root-guardian-batch04r1-20261016.xml` 两项均通过。按节点取最新结果为 **262/262，无跳过**，没有把重复运行相加。

新增 Guardian 场景分别使用真实本地和安装版 Source executor，验证实际只读命令输出、冻结后的 managed profile、原目标图片字节、拒读证据，以及没有本机 SandboxPolicy 时的 full-access/身份判定。该范围还包括既有 Guardian 同步、异步、权限和 checkpoint 回归。此前 batch03 的 setup error 与修复历史保留，不算业务场景通过。

R0、R2 和 R3 的独立报告分别覆盖接收方环境继承、文件/checkpoint、MCP/Skill。它们与本报告存在重叠，不能相加为全产品通过数。SSH/WSL 桌面 host、编辑器/API 和 structured network 的后续批已接通，各自报告由主交付文档索引；本报告不是整款产品等价、最新提交 CI 或安装器交付声明。

## 收尾验证：原进程恢复与网络记录

真实 executor reader 失败、原 peer 尚不能确认关闭时，前台任务把同一个 runner 转交现有 BackgroundCommandManager，保留环境、process/session ID 和实际网络决策。后续原 proxy 释放产生的新 audit 同样进入完成记录；本地 orphan 与 owner reconciliation 再次保存时也保留这些字段。没有以新连接或本机 PID 冒充原进程的关闭确认。

`executor-network-recovery-final-20261016.xml` 首批 65 个节点为 63 passed / 2 failed；两项旧测试替身仍 patch 已退休的 `SandboxRunner` 调用位置，实际入口已是共享 `command_runner`。仅修替身入口后，整份清理回归与打包合同一起 14/14，见 `recovery-packaging-final-r1-20261016.xml`。65 个原节点按最新结果均通过，0 skipped；打包合同另有 8 个唯一节点，不把重叠节点相加。

新增四项包含实际 QueryEngine → 冻结网络策略/argv/digest → 原生 Windows 网络前置条件拒绝。未 provision 的隔离状态没有启动 workload、没有伪造 PID/exit，没有遗留 network registration 或 lease；这只验证真实前置条件，不代表 Windows WFP 流量通过。另三项验证前台转交、冷远端回执及两种本地 reconciliation 保存真实网络证据。

统一回归另外修复 Monitor 直接访问可空 lifecycle 字段的问题：使用 BackgroundCommand 既有的生命周期入口读取原错误。历史 notes 的私有 namespace 断言与新增公开 `skills` namespace 分开核对；文件输出 worker 的测试替身跟随共享入口。三个受影响文件集中 78/78，见 `root-backend-final-r2-20261016.xml`；原诊断失败及一次测试缩进错误保留，不追写成初始通过。

## 原生分发字节与 Git

Windows 初始 builder 用系统行尾写入分发文本，而仓库按 `.gitattributes` 统一 LF，导致 Git checkout 后文本 SHA 与 `runtime.json` 不同。builder 现在明确用 LF 生成 notices、source manifest 和 runtime manifest。复用原编译缓存重新执行正式 builder 后，exe 仍为 `f14c6d27630f3791bee6e50578e1eee8506bf0e75ad8f971c407287d7402b9fc`；没有重编或替换其实现。

`native-network-staged-resource-final-20261016.json` 逐项核对暂存 Git blob、工作区、runtime manifest、bridge 源码及 builder SHA；全部字节一致。`verify-network-runtime.py` 也实际通过。该验证避免仅在本机 CRLF 文件上通过、提交后失配；Linux 原生组件仍由独立 CI job 从同一 Source pin 构建。
