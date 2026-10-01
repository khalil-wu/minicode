# Windows 原生执行面

MiniCode 在 Windows 上优先使用 `windows-elevated-wfp`。控制面只把本轮
`SandboxPolicy` 翻译成权限 profile 和根目录；从公开 Codex 源码构建的 MiniCode runtime
负责专用账户登录、ACL、deny-read、Job Object、私有 desktop、Firewall 与
WFP。原生后端不可用时，runner 才检查容器；受管策略没有可执行后端时在创建
用户命令进程前失败。

## 发布与安装

桌面构建执行：

```powershell
npm --prefix desktop run runtime:windows-sandbox
```

脚本校验并解压固定的 `rust-v0.158.0-alpha.2.1` 源码归档，为 MiniCode
替换专用账户、组、Firewall 规则和 16 个 WFP GUID（新增离线账户全出站阻断），再编译
`codex.exe`、`codex-command-runner.exe` 和
`codex-windows-sandbox-setup.exe` 放入桌面资源，并记录每个文件的 SHA-256。
这三个文件缺一不可；主程序会从相邻位置物化两个 helper。安装器随后运行
runtime 的 elevated setup，在 `%APPDATA%\minicode-desktop\data\windows-sandbox`
创建 MiniCode 自己的 DPAPI 状态并安装/修复账户、ACL 与 WFP 过滤器。
安装器要求管理员权限；setup 失败会终止安装。

开发环境可显式准备或检查另一份 home：

```powershell
python scripts/prepare_windows_native_sandbox.py `
  --target-home C:\path\to\minicode-native-home `
  --runtime C:\path\to\codex.exe

python scripts/prepare_windows_native_sandbox.py `
  --status `
  --target-home C:\path\to\minicode-native-home `
  --runtime C:\path\to\codex.exe
```

在已提升的 PowerShell 中运行上述 setup 命令。MiniCode 不复制密码、
不解密 DPAPI 内容，也不伪造 setup marker。更新与 repair 重跑同一命令。
开发环境若后端 Python 安装在用户 profile 下，准备脚本会为其安装目录授予
MiniCode 沙箱组读取与执行权限，避免后台全盘读取 ACL 刷新尚未完成时命令找不到解释器。

## 运行时路径

1. `MINICODE_WINDOWS_SANDBOX_EXECUTABLE`（测试或管理员覆盖）；
2. Electron 传入的 `MINICODE_APP_RESOURCES_DIR/windows-sandbox/codex.exe`；
运行时只接受 MiniCode 账户状态；它不会自动借用本机 Codex 安装或账户。

home 默认是 `MINICODE_STATE_ROOT/data/windows-sandbox`，也可用
`MINICODE_WINDOWS_SANDBOX_HOME` 覆盖。它不再读取或写入用户的 `.codex`。

每次执行创建私有 TEMP。正常结束由专用账户在 PowerShell `finally` 清理。
超时或取消杀掉进程树后，如宿主无法删除 owner-only 内容，runner 用同一
沙箱账户运行清理命令，再删除空根目录；清理未证实时返回 `cleanup_pending`。

## 验收

```powershell
$env:MINICODE_WINDOWS_SANDBOX_HOME='C:\path\to\prepared-home'
$env:MINICODE_WINDOWS_SANDBOX_EXECUTABLE='C:\path\to\windows-sandbox\codex.exe'
python scripts/check_execution_boundary.py --out .tmp/native-acceptance
```

验收必须同时通过工作区写入、cwd、退出码、声明根读取、相邻工作区拒写、
受保护的 Git 元数据拒写、直接 socket 与回环连接禁网、沙箱内 pytest、
超时清理和取消清理。完成陈述或 helper 启动成功都不能替代这 11 项结果。
