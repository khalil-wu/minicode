# Codex 打开目标列表顺序

已修改真实 `desktop/open-targets.js::fileOpenTargets` producer：保留内置 MiniCode 编辑器的既存首位例外，核心已安装编辑器之后放 Default app，再放 File Explorer 等实用目标。检测、启动 argv、目标可用性、perPath/global 偏好和 Antigravity Source 品牌资源均保留。

保存 Source `main-BklS_2Y2.js` 的 `YRe` 原数组经 `XRe(process.platform)` 生成 `jQ`；`WQ/PQ/LQ` 保持顺序，General `tc` 只过滤 unavailable，没做字母或 kind 排序。Windows 核心顺序为 VS Code、VS Code Insiders、Visual Studio、Cursor、Sublime Text、Zed、Antigravity、GitHub Desktop、systemDefault、fileManager、Terminal、Git Bash、Cmder、WSL；后续 factory 仍保持 Source 位置。因此本次移动 System 的固定 slot，没有按“所有 editor 都排 utility 前”自行全局 sort。

验证 `desktop/open-targets.test.js` 最新 **9 passed / 0 failed / 0 skipped**，日志 `output/playwright/codex-control-audit-20261013/open-target-order-node01.log`。新增真实 facade 测试还验证后续 WebStorm factory 不被移动、Default app 可继续持久化；原启动和目录偏好断言保留，不能与前轮 8 项相加。

新独立临时 profile 的真实 Electron + 当前 preferences IPC snapshot 保持如下实际本机目标顺序：`minicode → vscode → visualStudio → cursor → antigravity → system → fileManager`。证据 `open-target-order-native-results.json`，Window count / external launches 都为 0，Antigravity image decode 仍与 Source 品牌资源一致。没有广告未安装目标，也没有动正常用户 profile。

已通知 desktop agent 使用其原 final profile 真冷启动、新 native snapshot 独立补菜单图；此次 native producer 修改无需前端重新构建，既有字体和前端验证结果不重复运行。最终菜单截图以该实际补图为准。
