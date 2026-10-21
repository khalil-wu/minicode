# 远端安装的当前桌面边界

本次只读比较 retained Desktop `26.1013.4894.0` 与当前安装 `26.1016.6573.0`，没有操作GUI、执行bundle或修改连接/外机。完整ASAR哈希、选定文件哈希、精确UTF-16范围及扫描计数在 `output/full-parity-20261021/remote-install-current-readonly.json`。

旧 `main-C4DYk222.js` 确有 `install-remote-codex` RPC、`installRemoteCodex` 和 `async installCodex()`；旧连接设置行的实际consumer又明确过滤该action。这证明旧内部API存在，不能据此加一个安装按钮。

当前ASAR的15231个JavaScript文件静态扫描未发现上述RPC、coordinator或方法；`install-codex` 的三处引用分别是 `app-initial-2b6b8063022d.js` 的两个状态action descriptor，以及 `remote-connections-settings-e7f2b0adb1c4.js` 76909附近明确的 `case install-codex: return null`。当前 `main-CT5a1yCd.js` 仍有实际远端PATH探测、version探测及 `remote-codex-not-found` 的手动安装提示。文案或descriptor存在不能证明当前可执行安装功能。

因此将 NR-INSTALL1 从“当前已确认缺功能”改记为“旧版本内部API差异，当前reference已被覆盖”。没有新增Source当前没有的按钮或空API，也没有声称MiniCode实现了受管远端安装。MiniCode现有实际CI wheel、冷安装和手动部署继续按 [Linux后端安装](../linux-backend-install.md) 的合同；当前远端/WSL全矩阵仍须单列验收。

本结论只覆盖远端安装，不代表全部新版桌面或全部remote功能已经重新核完；其他差距继续保留各自来源与验收范围。
