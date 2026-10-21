# 自有 Windows Computer Use 接收端

MiniCode 新增显式 `minicode-win32` provider，使用公开 Win32 API、Pillow、psutil 和现有 Node REPL/MCP，不复制或重新分发 Codex 私有 runtime。原 `installed-source` 模式保留；选择失败不会切换 provider。

## 实际接入

现有 `.mcp.json` 配置入口示例（文档不会修改用户配置）：

```json
{
  "mcpServers": {
    "cua_repl": {
      "command": "<接收端 Node 的路径>",
      "args": [],
      "env": {
        "CUA_REPL_ENABLED_SURFACES": "computer",
        "MINICODE_CUA_PROVIDER": "minicode-win32"
      }
    }
  }
}
```

本机 Python 来自当前 backend；Node 使用该 MCP 配置实际捕获的 command。远端还必须显式配置 `MINICODE_CUA_PYTHON` 为原 Windows receiving host 上的绝对 Python 路径。Node 与 Python 都由原 executor 启动，保留 receiving environment、permission scope、lease 和 working directory；不使用本机解释器冒充远端。非 Windows 接收端明确不支持这个 provider。

调用链为已捕获的配置/执行环境 → CUA companion → 既有 Node REPL → 自有协议桥 → 原 receiving Python helper → 原窗口。serverInfo 与说明明确标识 MiniCode 独立实现，没有浏览器 API 或虚构 Source runtime 身份。新 Python/MJS 文件已由既有 backend/wheel package-data 收集，无新私有依赖或前端按钮。

## 实现范围

- 枚举正在运行的可见窗口、选择原 HWND 和进程；每个目标属于原 thread/turn。
- 单窗口 PNG、窗口状态、焦点、鼠标/滚动、Unicode 输入和组合键；不以全屏截图替代失败的目标窗口截图。
- 截图返回前重新核对原窗口/进程；输入在 focus 后、SendInput 前核对目标及真实前台窗口。Win32 全局输入不被描述成原子 HWND 投递。
- 默认 ask，捕获配置中的 deny 优先；仅现有真实 app elicitation 的 `accept + persist=session` 才建立会话授权。未知 provider、无法识别真实 app 的共享宿主及尚未支持的 signer 规则明确报错。
- timeout、取消、reset、turn/thread 结束都回收原 helper；同步阻塞不能靠写 stdin 假称已关闭。保留原 PID、exit code、signal 和原因，并清理原窗口 nonce；不自动重试原动作。

## 验证

本批没有实际枚举用户窗口、截图、输入或操作 GUI。协议测试使用真实 Node/Python 子进程及 OS facade，不把 facade 当实机通过。

- 四文件集中 **60/60 passed，0 error/skip**：`output/full-parity-20261021/native-cua-independent-complete.xml`。
- OS 边界 facade **6/6 passed**：`native-cua-window-boundary.xml`。与先前已通过的原 approval/Source evidence 按用例合并，103个不同相关用例有通过证据，不是一次103/103全量收据。
- 完整生产源码体补验 **2/2 passed，0 error/skip**：`native-cua-full-production-payload.xml`。携带全部15,999字节生产 Python 源码，仅替换 WindowsDesktop facade 后执行原 main/session。实际 SDK/executor启动、工具目录、原turn结束和helper退出通过；受控启动体22,163字符，不小于生产21,595字符，实际Node argv22,230字符。
- 原先 Windows `CreateProcess` 长度失败被保留；现对原文一次压缩，未提高限额或改 execution wrapper。早先较短 fixture 的 SDK 成功没有被用来替代完整生产载荷补验。
- SDK成功使用原 Source SDK 测试的明确 full-access profile和真实测试审批通道。受限 profile 曾真实拒绝 helper spawn，未放宽产品 sandbox 或把它算通过。

后续IPC归属补修将worker请求绑定到parent保存的同kernel/execution/request，身份与取消信号只取parent捕获记录，旧execution不能借新owner。新native/完整生产SDK及generic Node回归 **47/47通过**，`native-cua-parent-owned-ipc.xml`；此时生产bootstrap21,723字符、完整受控体22,291字符，实际SDK Node argv22,358字符。全部本批收据按用例取最后结果为 **107个不同用例通过，0失败/跳过**，并非一次107/107收据。

Windows harness已加入独立provider测试，并显式使用Node22，以持续覆盖完整生产启动体的真实Windows进程创建，而非只在Linux或短fixture下运行。

## 尚未交付的范围

实际设备/窗口验收、accessibility tree、应用启动、音频、高级 signer、永久 always 授权管理、部分 UWP 真实窗口身份，以及 macOS/Linux adapter尚未完成。Source Sky 包确有三平台 adapter，但这不自动证明桌面每种 remote/Linux组合均开放；桌面门控与底层能力分开记录。

因此 **DIST-CUA1 为部分实现，不能整组删除**。Computer use 设置的读取/撤销及管理消费者另见 `2026-10-22-computer-use-settings-source-gap.md`。
