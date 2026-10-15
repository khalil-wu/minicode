# 实际 shell 与命令投影

依据 `.tmp/codex-src/codex-rs/shell-command/src/parse_command.rs:43`：先从实际 argv 识别 Bash 或 PowerShell，再解析操作；不能按运行机器是 Windows 就一律按 PowerShell 解读。Source 没有将 cmd 的 `type` 自动转换成 PowerShell 文件读取。

MiniCode 的 shell/login/tty 执行链已由单独批次接通，原 `command_actions_for_tool` 仍固定 `powershell=os.name == 'nt'`，会使 Windows 上实际选用的 Bash/sh 命令错误分类，或把 cmd 误标为读取。

现在 producer 复用同一 `command_shell_argv` 的真实 shell 选型：PowerShell 解析简单原读取，Bash/zsh/sh 使用原 POSIX parser，cmd 和其他无法提取的类型保留 unknown 与原命令。识别时不运行 shell、不读取 profile、不改变工具参数或授权；UI 不显示内部 UTF-8/退出码包装。真实 stdin、输出、错误、退出码及原始日志仍由执行器记录。

`root-shell-projection-run01` 的一个文件66项一次通过，退出0、0跳过。新增5项同时核对 tool_call producer 与真实 store_result consumer；原范围包括实际 PowerShell 文件读取、exit7、空stdout/stderr、WS主过程、journal重开、子过程与REST投影一致。保存的实际 wire/history 为该目录 `real-command-actions-wire-history.json`。与旧范围重叠，不称新增66项测试。

这些通过不替代 shell/login/tty 的独立执行/PTY/取消验证，也不代表新 native runtime 已在用户当前进程激活。后者由对应批次按真实 fingerprint 和运行证据记录。
