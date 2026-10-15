# Codex 设置偏好与实际消费者核对

本轮核对当前源码，未操作正常用户 profile。视觉依据为用户的 `codex-clipboard-ff50df00-fe70-4eee-90b1-fb65584abbce.png`；桌面 Source 保存于 `output/playwright/codex-control-audit-20261013/codex-open-source/`，后端参考 `.tmp/codex-src` commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。此审计不是整套 Codex 已完成的声明。

此表保留起始审计时点的发现与归属。后续实际完成状态以`2026-10-14-active-work.md`及各批最新报告为准：终端发现/PTY、完整访问权限确认、通知completion/heartbeat筛选和自动未读、第三种Enter均已分别完成生产验证，不再计为未实现。2026-10-15用户明确官方账号不做，官方账号登录、授权、接入及其hosted服务线上验收移出本轮范围，历史not_run不记为通过。

| 控件 | 真实 producer / 持久化 | 实际 consumer / Source 依据 |
|---|---|---|
| 无项目任务目录 | General → preferences.chooseTaskDirectory → 原生目录选择 → preferences.json | `stores/chat-slice.ts` 创建无工作区任务时发送 `projectless_base`；repository → `create_projectless_directory`。已保存目录不是只显示在设置页。 |
| 默认文件打开位置 | General → preferences.set；工作区 Open in 更新 global 与 perPath | `open-preferred-target.ts` → `minicode:openPreferredPath` → perPath 后 global；真实 catalog / native 扩展规则 / target launcher。Source General `tc` 查询 `cwd:null`，`nc` 仅过滤 `available:false`，不是工作区菜单的 hidden 过滤。不能清空 perPath 来伪装全局默认生效。 |
| 集成终端 Shell | preferences.json 的 terminalShell → native shell catalog | `pty-manager.spawnSession` 消费实际 descriptor。审计发现原先固定 powershell.exe 且另列 pwsh 与 Source 的 PowerShell 首选解析不同；已交独立 agent 修 producer 与 PTY。Source main `Gqe/Kqe/Y8/X8/Z8/Q8/J8`：PowerShell 首选 PATH pwsh.exe 再 powershell.exe，Command Prompt 用 COMSPEC，Git Bash / WSL 依真实 exe 发现。 |
| 智能体环境 | MiniCode 当前没有 runCodexInWsl 偏好与 WSL 后端启动链 | Source General `ac/_s` 是有条件的 legacy WSL 控件；设置键 `runCodexInWindowsSubsystemForLinux`，默认 false。需实际支持环境、`wsl-bash-availability` 检查和重启后生效，remote WSL connections 还影响展示条件。MiniCode sidecar 当前直接启动 native Python；不能因 WSL opener 或终端选项存在就声称 agent 在 WSL。 |
| 语言 | localeOverride = null / zh-CN / en-US；preferences.set 持久化并广播 | `DesktopLanguageProvider`、应用原生菜单、关闭确认实际消费；自动检测规则一致。当前仅提供真实已实现的两种语言，未把 Codex 全部语言列成假选项。 |
| 默认权限显示 | 固定开启且禁用，没有可变偏好 | Source General `go` 精确 `checked:true, disabled:true`；accessible text 为 “Default permissions are always shown”。当前静态开关是有源码依据的，不新增 alwaysShowDefaultPermissions。 |
| 完整访问权限显示 | showFullAccess；独立 UI agent 补启用确认 | Source `go/vo`：关闭直接保存，开启先确认，confirm 才保存；只控制 Composer 菜单可选项，不切当前任务权限。Source atom `uWi=Fu('composer-permission-mode-visibility',true)`，默认 true；旧 object 读取由 Source `oWi` 定义。 |
| 完成 / 权限 / 问题通知 | preferences.json → shouldNotify → native Notification | Source `notifications-settings::lt` 默认 unfocused / true / true；`app-initial::ylc/clc` 消费真实 thread/request/focus/visibility。此次补当前可见请求抑制、真实请求解决及查看会话后 hide；完成线程筛选仍有明确差距，见通知报告。 |
| 网页 / 本地 URL 打开位置 | Desktop preferences 原子保存并广播 | `openPreferredWebLink` 按 localhost/127.0.0.1/::1 分类，实际外部浏览器或 IAB 打开；异步返回检查真实原会话 owner。 |
| 完整网址 | showFullUrl | BrowserPanel 地址栏实际消费，编辑地址时仍使用完整 URL。 |
| 批注截图 | annotationScreenshotsMode | `browser-annotation-capture` 每次读取最新偏好；necessary 仅区域，always 无区域时截整页；capture/upload 保持实际 target、URL、conversation owner。 |
| 下载 / 网站权限 | embedded browser settings 文件 | download event / permission handler 实际消费。审计发现多个已打开菜单缓存旧值及清除后不失效；Root 已修真实保存/清除 producer 的广播与 consumers，独立 native scope 54 passed，前端由统一批次核验，不将重叠结果相加。 |

Source `general-settings::go/vo` 的完整访问启用弹窗是普通 modal + form，取消只关闭，Confirm danger submit 才保存。说明明确 “Turning this on adds Full Access to the permissions menu. It does not turn Full Access on.”；不能把设置开关接成后端 mode 切换。

设置页选择控件已全部使用 SelectMenu，没发现裸 select 的遗漏。审计时仍缺 `settings-select / align=end` 的位置已发 Root：ProviderTab 1254/1309/1432/1445/1450/1464/1507/1697、ConnectorsTab 786、PluginsTab 739/766、SchedulerTab 433/442/448/453/457/480、VoiceSettings 121；嵌套编辑器 / AgentEditor / LiveArtifacts 是不同表单语境，单独列出供实现者判断。硬宽来源包括 SelectMenu label ellipsis、SettingsCenter 240/260px、Plugins 180px、Connector 150px；由共同组件与实际 consumers 一并修，不能只修 General 的一个入口。

本批设置/浏览器/通知前端统一结果为 312 个去重节点全部通过，tsc 与最新 production build 成功；完整记录为 `settings-select-latest-results.json` 与 `settings-select-production-build02.log`。通知 native scope 50 个去重节点通过，详情见 `2026-10-14-notifications-source.md`，这些重叠范围不能累加成全产品覆盖数。

本轮仍未闭合的设置相关能力包括：WSL agent 后端真实owner/路径/启动/恢复链，以及OS通知快捷审批与直接回复。上表审计时发现的Source completion/heartbeat筛选和canonical未读链已在`2026-10-14-source-completion-unread-chain.md`完成独立实际验证。未为其余能力增加虚构UI，也未把只读发现当作执行通过。
