# Codex 打开方式调用链核对

参考为安装版 `26.1007.2314` 的只读 ASAR：`C:/Program Files/WindowsApps/OpenAI.Codex_26.1007.2314.0_x64__2p2nqsd0c76g0/app/resources/app.asar`；后端 checkout 为 `.tmp/codex-src` commit `822e58cc`。此功能属于 Electron desktop service，不能把 app-server checkout 当成其安装发现实现。

原图 `output/playwright/ui-reference-20261004-v2/desktop/01-codex-chat-sidepane.png` 的顶层 Header 没有独立 Open 按钮。本版 `app-initial-b8e1c8014bb4.js::jJo` 的 conversation header/sidebar overflow → `OJo` → `kJo` 生成 `Open in` 工作区子菜单。`zh-CN-b05cfd55c534.js` 对应原文为“打开方式”；loading/failure 原文为“正在加载可用应用…”/“无法加载应用。请重新打开菜单以重试”。`kJo` 仅显示 `available=true && hidden=false` 的目标，在 Work 模式排除 editor/terminal，图标使用 resolvedIcon 或原 catalog icon。Open in 图标来自本版 `DJo::EJo` 原 SVG。

`use-target-apps-303d0da6d0e7.js::B` 传 explicit `target`，仅 `persistPreferred` 存在时带 `persistPreferredTargetPath=cwd`；工作区 overflow 的 `OJo` 明确传 `openMode=workspace`、`persistPreferredTargetPath=cwd`。`main-BklS_2Y2.js::JQ.open` → `$Ie` → `RQ` → `$Re` → target open/args；`UQ` 更新 global 和 perPath；普通默认解析使用 perPath[cwd] 再 global。Windows native 文件类型规则来自 `kZ`/`XIe`/`MZ`，目录走 fileManager，native 文档/媒体/Office/归档默认走 systemDefault，显式 target 保留选择。

MiniCode 当前 producer 为 `ChatActionsButton.tsx`，通过 `openPreferredWorkspace` → runtime → preload → `minicode:openPreferredPath(path,line,options)` → desktop preference/target consumer；没有新增无依据 HeaderBar Open 按钮。工作区选择更新 global/perPath，并广播完整 preference。文件显式 target 不持久化。Markdown 和 ConversationFilesTree 均带原 execution workspace，项目外的 projectless 浏览根不被错当成首选应用的 cwd。保留已保存的 MiniCode 内部编辑器/预览默认，仅 implicit 外部打开使用 native 类型规则。

Windows catalog/detection/args 与本版有 win32 entry 的目标对应：VS Code、VS Code Insiders、Visual Studio、Cursor、Sublime Text、Zed、Antigravity、GitHub Desktop、File Explorer、Terminal、Git Bash、Cmder、WSL、Android Studio、IntelliJ IDEA、Rider、PyCharm、WebStorm、PhpStorm。路径发现包括 PATH CLI、LocalAppData/ProgramFiles/ProgramFiles(x86)、Start Menu shortcuts、Visual Studio/vswhere 和版本化 JetBrains 目录。Windsurf、Warp、GoLand、RustRover 在本版没有 Windows entry，未按品牌自行添加。未实现 Codex 的 customFileHandlers 配置，也未声称该配置已对齐。

新增 target module 和共享 native 扩展 JSON 已进入 desktop package files；必要 tests 已进入 test:unit。文件/目录参数区分 code `--goto file:line:column`、location editor `file:line:column`、JetBrains `--line/--column`、File Explorer file reveal/directory open、Git Bash/Terminal/Cmder cwd、WSL distro。终端按 Source detached spawn ACK 结束，不等终端窗口退出。

验证记录：

- 首批桌面 `open-targets.test.js + file-surface-audit.test.js + menu-popup.test.js`：15 passed，0 skipped；首批前端 8 文件：54 passed，0 skipped。
- 补齐 native 默认规则、内部默认保留、projectless execution cwd 后：桌面 open-targets 5 passed，0 skipped；前端受影响 6 文件 27 passed，0 skipped。包含先前用例，不将重跑计为新的独立测试总数。
- 最后按 Source extname 规则核对 literal `#`/大写后缀/无扩展名 dotfile：helper + FilesTree 2 文件 11 passed，0 skipped；这同样包含前一批用例。
- 原生只读发现：`output/playwright/codex-control-audit-20261013/open-targets-native-discovery.json`，独立临时 profile，BrowserWindow 0，externalLaunches 0。实机发现 VS Code=`C:/vscode/Microsoft VS Code/Code.exe`、Visual Studio=`C:/Program Files/Microsoft Visual Studio/2022/Community/Common7/IDE/devenv.exe`、Cursor=`E:/cursor/Cursor.exe`、Antigravity=`D:/Antigravity/bin/antigravity.cmd`、File Explorer=`C:/WINDOWS/explorer.exe`，5 个 native icons 均为 16×16。
- 生产菜单截图和统一 production build 由 root 后续 offscreen QA 生成。此记录不把 unit fixture 的 spawn interception 算作实际应用启动，也不把未安装 VS Code Insiders/Sublime/Zed/GitHub Desktop/Terminal/Git Bash/Cmder/WSL/JetBrains 目标算作实机通过。未创建/显示/激活普通窗口，未打开实际外部应用，未操作用户 profile、draft/history/editor 内容。
