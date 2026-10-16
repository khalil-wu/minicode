# CI 原生资源字节与跨平台验收入口

首次后续批提交为 `722f5b7a2c94b01df2cdcfe44d268231191175ff`，实际 CI 为 [38868950710](https://github.com/khalil-wu/minicode/actions/runs/38868950710)。八个 job 中六项成功，两个 Python job 失败；没有取消、跳过或改写这些原始失败。

- Windows harness：362 passed / 15 skipped。
- 前端：357 文件、4297 passed，构建与两项预算通过；Electron 前端 1/1。
- Linux 桌面单元：161 passed / 9 个原有 Windows 条件跳过；Electron smoke 6/6。
- Linux 原生执行策略构建成功。网络组件首次真实 release 编译 8 分 22 秒，Source identity 与固定 commit 相同；实际 ELF64 x86_64 及六文件 artifact 校验完成。
- 根 tests：1761 passed / 3 failed / 51 skipped；backend：6882 passed / 3 failed / 77 skipped。失败调用链及完整日志保存在 `output/ci-repair-20261015/`，不是必要环境不足后默认通过。

## Windows 执行策略发布字节

三项打包合同失败揭示已有 Windows execpolicy bundle 的真实入库问题：producer 的 LICENSE、NOTICE、source manifest、third-party notices 使用 CRLF，而全局 Git 文本转换把它们存成 LF；`runtime.json` 仍保存 producer 原始 SHA。错误不是 manifest 或新的校验过严。

`.gitattributes` 现在把该目标的完整原生发布目录设为 `-text`，再重新入库 producer 原始字节。Git blob 本身即可匹配校验清单，clone 或 archive 都不需要依靠后续行尾转换修复。没有重写 manifest 哈希、重编 exe、修改本机 payload 或放宽 NativeBuild 校验。暂存的五个 manifest 文件 SHA 和工作区字节均实际一致；修复前远端六个 blob 的独立对照保留在 `native-execpolicy-byte-preservation-independent.json`。

## 冷 HTTP 证据文件

editor cold HTTP 测试已实际得到 HTTP 200，原失败发生在 `finally` 向 fresh clone 不存在的仓库 `output/` 写证据。日志现写入 pytest 自有 `tmp_path`，实际路径和完整内容进入 JUnit；没有吞写入错误、改生产或依赖预先创建的审计目录。显式选择隔离 Source peer 后，整文件 9/9，0 skipped，日志与 JUnit 字节一致。

## 原生网络 POST 的平台入口

原 POST 两用例在 Linux 错把 Windows SID/managed launch 入口当作通用入口。它们的目标是原生代理暂停、审批和原请求体只发送一次，不能据此冒称已验证 Linux OS 隔离。

Windows 保留原 SID 子进程与明确 provision 门槛。非 Windows 测试使用 Source 实际 `prepare_local` lease/env 和 `\0CDXPXY1` execution attribution 帧，启动显式无 OS sandbox 的自有协议驱动，核对原 token、environment/execution、callback、原 handle、一次 origin body、audit 与最终 lease 释放。生产 managed Linux 路由的拒绝边界没有改变，也没有新增平台跳过。

audit 到达是独立真实事件：进程先退出不能代替 policyDecision 已发送。该事件等待计入原有 15 秒预算后才 flush/assert，没有扩大阈值。本机 Windows 集中验证仍为 21 passed / 2 个原有 provision 条件跳过；非 Windows 新分支由修复提交的新 Linux CI 实际执行，不能借本机结果记通过。

两处 CI 测试夹具由独立冻结的 r4 输入承接；后续 Source 缓存初始化修改由新 r5 包承接并完整冷验 26/26。旧包的二十六项仍只属于各自原包，身份和边界见 [原生打包报告](2026-10-16-native-wheel-packaging.md)。修复提交的完整八项结果按 [本分支 CI](https://github.com/khalil-wu/minicode/actions/workflows/ci.yml?query=branch%3Acodex%2Frelease-readiness-20260927)独立核对。

## Source 初始化与 target-only 缓存

修复提交 `0eb959255c5342219eef04ffa593394d49cdbc8e` 的 [复验 38872672167](https://github.com/khalil-wu/minicode/actions/runs/38872672167) 真实恢复了 1.23 GB Cargo 缓存，却在 cargo 调用前因 `source-identity.json` 不存在而失败。缓存只保存 `source/codex-rs/target`，恢复时自然创建了 `source` 祖先目录；原脚本错误使用该目录存在作为 Source 已准备的事实，因此跳过真正的 `git archive`。这是构建初始化与缓存的契约错误，不是 Rust 编译或网络波动。后端与桌面依赖 job 未执行，不能记通过。

修复以原 build 的 Source 身份文件确定是否已完成初始化。target-only 缓存仍走固定 Git object 的真实 archive、解压，成功后才写身份；已有身份仍严格比对原 Source pin。解压保留原编译 target，不清缓存、不伪造身份、不捕获并忽略缺失文件。Windows 已分发原生二进制的 provenance 保留其实际 producer 脚本身份，不随构建脚本修改重写历史来源。构建输入变化由新的冻结与独立安装验证承接，不能继续借 r4 的输入一致性证明当前脚本。

新增冷目录、target-only 缓存、归档失败不发布身份、错误 pin 原样拒绝四项回归，连同原生打包合同统一 **12/12、0 skipped**。另外在自有 target-only 目录实际执行固定 pin 的 `git archive`：解压的 Source Cargo.toml 字节等于真实 Git object，原缓存 sentinel 保留，身份在成功归档后写入，第二次调用复用同一已准备源码。证据为 `network-cache-source-init-contract-batch01.xml` 与 `verify-network-cache-source-init-actual.json`。这个实际归档检查不冒充 Linux 原生编译；完整缓存恢复后的编译和两个 Python job 仍由后续新 CI 执行。
