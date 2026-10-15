# Antigravity 打开目标图标 producer

当前真实 preferences IPC 发布 `apps/antigravity.png`，图像解码与仓库现有 Source 品牌资源完全一致，并实际查看确认品牌 A。只修改 `desktop/open-targets.js` 中 Antigravity 的 iconPath producer，检测方式、目标可用性、CLI driver 与参数未改；没有在前端覆盖图片。

## Source 与真实安装证据

保存 Source `main-BklS_2Y2.js::yLe/iQ` 为 Antigravity 声明 `apps/antigravity.png`，generic editor descriptor 没有 Antigravity native iconPath。MiniCode 原先把检测到的 command 直接当 iconPath；本机目标实际为 `D:/Antigravity/bin/antigravity.cmd`，其 native icon 是齿轮。

本机 `Antigravity.exe` 与启动脚本实际引用的 `Antigravity IDE.exe` 都存在，但真实 Electron `app.getFileIcon` 返回的均为 generic window。已经实际查看两份 PNG，不能用“hash 与 cmd 不同”冒称品牌通过。这些原始 native 图像以及最初 exe 路径的 IPC 错配结果保留。

最终按 Source 注册品牌 descriptor 令 `iconPath:()=>null`；现有 preferencesSnapshot 直接消费已有 icon 字段，不请求 `.cmd` 或两个无品牌的 native exe 图标。没有修正常用户 Windows icon cache，没有解析并执行启动脚本，也没有增加不存在的安装目标。

## 验证

- `desktop/open-targets.test.js` 当前 **8 passed / 0 failed / 0 skipped**，日志 `output/playwright/codex-control-audit-20261013/antigravity-icon-node02.log`。包含 bin、resources/app/bin、只有 CLI 三种真实布局契约；各场景保留 launcher 与原 `--goto file:line:column` 参数，现有发现、perPath/global 冷恢复、temporary target 与真实 IPC 消费断言保留。
- 独立临时 profile 的真实 Electron app + 当前真实 preferences IPC handler 验证，结果 `antigravity-icon-native-results.json`：`windowCount=0`、`externalLaunches=0`；native icon requests 不包含 Antigravity launcher/exe。
- IPC 图像解码 SHA256 与 Source 资源均为 `b8168e764e3dbd031b511372f61c975014adf1a5b906b700b8feba178f710109`，尺寸 64×64，`publicIconIsSourceBrand=true`、`publicIconIsCmdGear=false`。图像 `antigravity-native-ipc-icon.png` 已实际查看。
- 旧 exe 的 wrong native result 保存为 `antigravity-icon-native-exe-mismatch.json`，其 PNG `antigravity-native-app-icon.png` / `antigravity-native-ide-icon.png` 与原 cmd `antigravity-native-cmd-icon.png` 仅为失败参考，不计品牌通过。

前端无此修复引起的 rebuild；已通知 desktop agent 对 build03 做真正冷启动 after03，最终画面以该轮实际生产截图为准，保留原字体 400 的对照断言。正常用户 profile、草稿、编辑器内容均未操作。
