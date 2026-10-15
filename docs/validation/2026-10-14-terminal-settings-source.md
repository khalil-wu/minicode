# 终端设置到真实 PTY 的 Source 对齐

日期：2026-10-14。视觉/桌面行为依据是本地已保存的 Codex 桌面前端快照；此处不将旧快照的版本号冒充当前安装版本。

## 精确源码证据

参考文件：`output/playwright/codex-control-audit-20261013/codex-open-source/main-BklS_2Y2.js`，SHA-256：`5cf1544463be9497b85b69051aa5ad32320f9ebdca063ec2ab41abb75b6cadf7`。文件为 minified 单行，以下是 UTF-8 解码后 Python 字符索引：

| 符号 | 字符位置 | 实际行为 |
| --- | ---: | --- |
| `Y8` | 3147364 | 依次在 PATH 查找 `pwsh.exe`、`powershell.exe` |
| `X8` | 3147465 | 使用 `process.env.COMSPEC?.trim()`，为空时 `cmd.exe` |
| `Z8` | 3147552 | PATH 的 `git-bash.exe`；否则 PATH `git.exe` 同目录 `bash.exe` 或上级 `bin/bash.exe`；再查安装目录 `Git/bin/bash.exe` |
| `Q8` | 3147826 | PATH 中的真实 `wsl.exe` |
| `Gqe` | 3146461 | Windows 基础 PowerShell/Command Prompt，加实际发现的 Git Bash/WSL；非 Windows 不提供这组设置 |
| `Kqe` | 3146595 | PowerShell/Command Prompt/WSL 无额外 argv；Git Bash 为 `--login,-i`；普通 choice 消失时解析到实际 PowerShell，再 Command Prompt |
| `J8` | 3146885 | 依据实际命令识别 PowerShell、Command Prompt、Git Bash、WSL 的显示名 |
| `Ga` | 102689 | 可执行文件解析与真实存在检查 |
| `Qa` | 104466 | 安装目录查找；根依次为 LocalAppData/Programs、ProgramFiles、ProgramFiles(x86) |

Source `TerminalManager` 从 `settingsStore.getEffective(integratedTerminalShell.key)` 取得偏好，`resolveTerminalCommand` 调用 `Kqe`；`createLocalTerminalBackend` 将真实命令列表传给 `qS.create`。`qS.create` 将第一个元素和剩余 argv 直接传入 node-pty。终端退出的 `destroySession` 调用 `backend.dispose`，`qS.dispose` 在 Windows 调用真实 `pty.kill()` 释放 native 资源。

## 实现与存储语义

`desktop/preferences.js` 的 `terminalShellTargets` 是发现和执行共同的 producer，包含实际 executable 与 argv；`desktop/pty-manager.js` 直接消费 `resolveTerminalShell` 的同一结果。

- canonical `powershell` 修正为 Source 的 PATH `pwsh.exe` → `powershell.exe`，显示 PowerShell；不再把这个已授权对齐修复做成新的 Windows PowerShell 兼容选项。
- 已有 `cmd` 是 MiniCode 原有 wire ID，继续映射 Source 的 Command Prompt；实际使用 trimmed COMSPEC 或 PATH cmd。
- Git Bash 按上述真实优先级发现，PTY 使用 `--login,-i`。WSL 只依赖实际 PATH executable，不附加 Windows Terminal/Ubuntu 探测条件。
- 普通菜单只提供实际发现的四种 Source shell，不提供虚构的可执行项。Source 常见 Windows 环境中的两个基础项若在异常环境不存在，会按用户“真实可用才出现”的要求不列出。
- 已保存的显式 `pwsh` 仍代表 pwsh：保留以前的 ProgramFiles/PowerShell/7 executable 优先级，其次为真实 PATH pwsh。消失时不会降级为 powershell.exe，也不会改写保存值。只在其实际可用且当前已选时显示这个既有值。
- 已保存的 `system` 保留系统 shell 意图：使用 SHELL，Windows 未声明 SHELL 时使用真实 COMSPEC/cmd；非 Windows 保留已有 SHELL/bash 行为。只在当前已选时显示其 Windows 既有选项，不把它变成 agent 后台驱动。
- 普通 choice 在保存后消失时，Source Kqe 的真实默认解析行为适用，保存的 choice 不偷偷改写。

删除 PowerShell 的额外 `-NoLogo/-NoProfile/-NoExit/-Command`、UTF-8 初始化和 Clear-Host 包装，真实执行 argv 与 Source 一致。不会把 terminalShell 偏好传给 agent harness，也没有新增 Windows/WSL agent driver 控件。

## 真实退出资源修复

第一轮真实 native 验证中，三个 shell 自身退出后，test child Electron 仍保留三个 owned headless conhost。node-pty 1.1.0 的 Windows agent 在自然退出时关闭 output socket，但 PseudoConsole/worker 仍需要公开 `kill()` 释放；只清 MiniCode JS session 无法完成 native disposal。

现有 onExit 先保存真实 exitCode/signal 并标记 settlement，再按 Windows 生命周期发出一次尚未请求的 native kill。重复 exit callback 不能覆盖原来的退出结果。主动 kill 的真实调用先设置 `nativeKillRequested`，退出不重复释放；已存在的 timeout/强制 kill 重试仍可继续。输出和 exited tombstone 继续保留。

这修复的是生产资源根因，没有在测试末尾强制 `process.exit` 来制造通过，也没有加入 catch 吞掉 native 错误。

## 验证

使用实际桌面 Electron runtime、已安装 node-pty，一次统一批量执行：

`ELECTRON_RUN_AS_NODE=1 electron.exe --test preferences.test.js pty-manager.test.js pty-cleanup-regressions.test.js`

最新完成记录：`output/terminal-settings-source-20261014-batch02.xml`。**28 passed，0 failed，0 error，0 skipped**，runner 真实退出码为 0。

去重明细：`output/terminal-settings-source-20261014-latest-results.json`。

验证包括真实发现优先级、COMSPEC trim、Git 安装目录顺序、WSL 仅 exe 条件、显式旧值冷恢复与不降级、六种选择到 PTY executable/argv 的消费、普通选择消失后 Source 默认解析、所有权/输入/退出/tombstone/超时重试，以及：

| native shell | 真实链 | 结果 |
| --- | --- | --- |
| PowerShell | 保存 canonical 选择 → 冷恢复 → PATH pwsh → 原生 PTY → ASCII char code 命令实际产生“终端中文” → exit | 通过，2.26 秒 |
| Command Prompt | 保存选择 → 冷恢复 → COMSPEC/cmd → 原生 PTY → 实际变量展开输出 → exit | 通过，1.29 秒 |
| Git Bash | 保存选择 → 冷恢复 → Git executable → `--login,-i` → 原生 PTY → 实际 printf 输出 → exit | 通过，3.17 秒 |

原始第一轮 `output/terminal-settings-source-20261014-batch01.xml` 仅有 JUnit prefix，因真实 native 资源泄漏导致 runner 未结束而中断。它作为 interrupted 证据保留，不能计作 passed 或完整 failed 用例统计。清理只针对经过 command line、父子 PID 和 conhost owner 校验的该测试树，不触碰正常用户 app/profile。所有 preference 文件和 shell history 验证使用临时位置，正常用户 preference 与未保存内容保持不变。

## 剩余差距与未实测部分

- **WSL guest 实际启动未执行**：本轮验证真实 binary 的发现、选择和 argv，不将 executable 存在当作 distro 已安装或运行成功。未启动 Linux guest，也未安装任何 WSL 组件。
- Source 对 WSL UNC 路径的 distro/`--cd` 映射、remote terminal command/host override、worktree shell environment exclude/set、跨窗口 terminal transfer 是另外的实际生产链，本次没有冒充已完成。
- 下拉菜单的真实桌面 UI、选中状态与操作视觉验证由桌面 QA agent 负责；本报告的 28 项是 producer、真实 PTY 与生命周期范围，不累加其他前端或后端验证数。
