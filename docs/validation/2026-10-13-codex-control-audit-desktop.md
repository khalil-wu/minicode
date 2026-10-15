# Codex 桌面控件审计：shell / native / settings / browser

本轮以当前 MiniCode 源码和最新 production 构建为准，参考安装版 Codex `26.1007.2314` ASAR 与明确截图；后端参考 `.tmp/codex-src` commit `822e58cc`。字体、字号和正常用户内容保持。

逐实例结果写入 `output/playwright/codex-control-audit-20261013/desktop-reviewed.json`，以 root inventory 的 entry id 映射，不覆盖原 inventory。1063 为 JSX 控件/事件节点出现数，不是可见按钮数量；动态 menu 项另列实例。可见状态、label/icon、click/change/keyboard/hover、源码依据、IPC/backend consumer、实机证据和差异逐项记录。

状态使用 `source-reviewed`、`runtime-verified`、`difference-confirmed`、`conditional-not-rendered`、`not-yet-verified`，未测项不算通过。旧截图只作为明确日期、构建和范围的引用。本轮使用新独立 QA profile，未操作正常用户或 Codex 消息，未删除用户 history。

| 顺序 | 区域 | 主要检查状态 |
|---|---|---|
| 1 | Shell 与 native window/menu | 默认、菜单/标签、禁用态、返回/前进、侧栏开合、拖动/键盘、窗口最小化/还原/关闭取消、动效 |
| 2 | Settings 入口/导航/搜索 | 入口当前页、返回/Escape、搜索、visited 页重开、菜单选择/取消、tab 与高级披露、减少动效 |
| 3 | Settings 各页 | 每个实际 field/button/menu 与持久化 consumer；无配置/安装/设备的能力明确标未验证 |
| 4 | Browser/native/header | 页面/地址/导航/新增关闭、原生权限/数据、批注截图、工具菜单、视口/设备、取消/保存/用户草稿归属 |
| 5 | History / 分组 | 来源筛选、搜索、日期折叠、真实页面菜单、active refetch、URL/日期移除 ACK 与原生账本一致 |
| 6 | File Open 与其他 panels | installed target menu、configured 默认、临时目标、Terminal/WSL、reveal/save；诊断/activity 各按钮 |

## 已确认待补差异

当前 `minicode:openPreferredPath` 只从持久 preference 取得 configured target，调用没有一次性 target 参数。native 可发现目标仅 MiniCode/默认应用/LocalAppData VS Code/Cursor；不能据此宣称已复刻本版 Codex 的实际 target dropdown。需要先读取 Codex 外部目标 discovery/open 调用链并向 root 明确扩 IPC scope。

## 优先级调整后已完成的 MCP 执行链

逐控件首批 `desktop-reviewed.json` 覆盖 ConnectorsTab/McpToolsEditor 的 45 个静态实例，每条都有 id/file/line、原 label/state/event、局部图标、具体 consumer、source/reference 和未检范围。只有 source-reviewed，未把这些按钮统称实机通过。Plugin/MCP 入口图可引用，单独配置字段不能据该图算匹配。剩余 shell/native/settings/browser 清单继续待逐项检查。

真实修复 1：官方 SDK 宣告 form 与 URL elicitation，旧 producer 却丢掉 mode/url/原 elicitationId，把 URL 当空 form。现在保留 URL、原 message/meta/server、公共唯一路由 id、真实 conversation/message/turn/task owner，pending replay保留请求；URL accept/decline/cancel无content。WS 通用审批曾把 decline 变 reject 最终变 cancel，现在 elicitation 独立保留标准动作。没有合法显式 accept 不自动批准，MCP complete notification 不自动批准。本版ASAR mcp-plugin-logo::ut要求 Open link→Continue，未自动登录或打开外部浏览器。

producer→official SDK callback→实际 bootstrap/WS pending/replay/response consumer 批次 29 项通过、0 skip；控制了 session owner resolver，不代表公网 OAuth 或真实外部 MCP/浏览器已验证。首批 fixture拒绝mcp:elicitation日志lane、第二批暴露真实decline归一化，失败日志保留，最终 `mcp-url-owner-chain-final2.xml/log`。

真实修复 2：只改 enabled_tools 的 before 诊断证明 manager仍close旧transport一次，registry2→5。保留raw catalog后，allow/deny/per-tool/default approval/parallel policy仅发布filtered catalog与真实版本，复用现有connection lock；在途call继续完成、旧prepared禁用工具不能再次执行，支持重启用。只有连接契约改变或整server停用继续关闭。policy与实际catalog refresh串行发布，没有旧raw库存重新暴露。

policy/config/catalog/settings/lifecycle/harness批次 40 项通过、0 skip，含3个新回归，证据 `mcp-policy-catalog-chain.xml/log`；before=`mcp-policy-reload-before.json`。与既有测试存在重叠，不与URL29相加为独立测试总数。本轮未show/showInactive/activate任何窗口，未访问用户profile或删除history。公网OAuth、设备验证、真实外部target仍待实机。
