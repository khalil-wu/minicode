# 桌面区域：本版源码与原生服务链核对

参考当前安装的 `OpenAI.Codex_26.1007.2314.0` app.asar。区域提取在 `output/playwright/codex-rebuild-20261012/desktop-regions/source/`；多 agent 选择器另在 `process-parity/source/subagent-runtime-kat.readable.js`。截图用于视觉核对，源码用于控件、默认折叠、行为及持久化依据。MiniCode 字体、字号、主题和未发送用户内容保持。

## 变化

- 子 agent 概览沿本版 Active/Done 分组，活动初显 4、完成初显 10，真实 objective/startedAt/整数秒；真实启动与通知分开，已关闭、系统错误、inactive failed/interrupted 不虚构概览条目，原记录及详情仍可查看。展开列表不截断实际数据。
- 设置插件工具栏位于标题同行，停留过的隐藏页不再重复挂工具栏。字体、编辑器与减少动态效果归入原有高级披露，默认值不变。
- 常规页权限采用本版两个主控：始终显示默认权限的禁用勾选开关，以及提供完整访问选项的开关。新任务默认权限、远程 Markdown 图片和更新归入高级。
- 常规页真实原生服务：默认任务目录、语言与 native menu、文件打开位置、PTY shell、关闭确认和三类通知。原子持久化、多窗口广播与实际消费均接入，没有只写 renderer 本地开关。
- 浏览器默认页沿本版六行：URL、本地 URL、完整 URL、浏览数据、浏览历史、批注截图。旧打开页面状态摘要、下载策略及权限快捷入口归入高级，既有服务和值保留。
- 浏览历史由原生导航与标题事件生成，来源、搜索、选择移除、清理及重启持久化贯通。历史页按日期分组、首日默认展开；紧凑行包含 checkbox/图标、标题/hostname 和 Agent/time/菜单。筛选位于搜索框尾部，历史打开显式进入原生内置浏览器，真实移除走 IPC。
- 浏览器完整地址显示在编辑时恢复全部 URL，保存未提交的编辑值；非编辑时可显示 hostname。批注实际 compositor PNG 经上传 API 回到原会话 ready attachment，保存和发送保留原草稿、附件与 owner。
- 默认任务目录 picker 仅保存 realpath，不把任务 base 注册为可写工作区。原生 trust 只接纳后端已 mint 的精确 task cwd；native picker 合并最新 ledger，不覆盖其他来源的新根。

## 验证口径

以下存在重复范围，不能把数字相加。

| 批次 | 结果 |
|---|---|
| 原生单元 | 63/63、0 跳过，`desktop-regions/native-unit-run01.log` |
| 前端既有受影响批次 | 最终 246 项通过；首轮 240/246、后续 95/96 与最后 overlay 24/24 日志均保留。最终修复闭合披露的 JSDOM 可见性断言、visited hidden pane 定位、语言 Provider/WorkspaceGit 渲染及旧 Git fixture；不是删断言掩盖真实失败 |
| 浏览器默认高级调整 | 既有 5 项重跑全部通过，`desktop-regions/browser-history-final-unit.log`；属于上方 246 范围 |
| 真正生产 native IPC run04 | 六组服务检查、8 图、两次重启，failures=[]；`desktop-regions/native-ipc-run04/evidence.json`。此前 run01/02 暴露 projectless 执行 cwd 链断，后端修 mount/switch/snapshot；run03 为 General 双标题 locator，失败轮原样保存 |

run04 使用真正 Electron、production renderer、主进程 IPC、backend sidecar、原生网页 compositor、实际上传与 cmd PTY。只有 localhost navigation 的安全对话框及 close 用户选择由测试输入控制。通知验证关闭返回 false，没有弹真实系统 toast；file opener 验 MiniCode 路由，没有启动外部编辑器。重启草稿和附件恢复是真实断言，但 06/08 截图拍在会话恢复 loading 期间，不能作为加载完成的视觉证据。04 是真正浏览器 compositor PNG；窗口 capturePage 可能不包含原生子 view，不能凭窗口图空白判定网页白屏。

## 用户资料保护

所有实机验证使用独立 QA profile；未复制、清空或保存用户正常 profile 中的草稿、附件、工作区及未保存编辑器。未 reset、commit 或整目录覆盖主工作区。已有 QA 资料中的旧 recent 项也不作删除；projectless 不再生产 phantom 项目的修复在后端 producer，需用 fresh QA 判断。

## 补充区域 QA 暴露的 History 根因

`native-region-visual-run06` 实际打开历史菜单页面后新产生一个 visit。重新进入已访问的设置页没有读取最新历史；renderer 用旧数组过滤移除的 id 后显示空，原生历史仍留下同 URL 的新 visit。最后 `1 !== 0` 为真实产品链失败，原始 native 文件另存 `raw-history-after-renderer-removal.json`。本版源码的 `refetchOnMount: always`、以 URL/本地日期聚合的 `Mr` 与 `Pr` 是修复依据。

修复后隐藏页面重新 active，以及 query/source、真实 owner/pending 切换均读取真实历史，旧读取 effect 回执不提交。移除 ACK 后通过修订触发当前作用域的 canonical 读取，不再本地过滤旧数组；按源码在移除期间禁用筛选和行操作。原生 store 由真正选中的记录计算 URL/日期组，删除当天同 URL 的所有来源访问，保留其他 URL 和其他日期。普通用户 profile 历史没有被清理；测试操作均在独立 QA profile。

集中必要验证：前端 `BrowserIntegrationTab.test.tsx` 7/7（原 5 + 2 个 active/owner/ACK 回归），原生 `preferences.test.js` 7/7（原 6 + 1 个同日跨来源/跨日期/重启回归），0 错误/跳过。日志为 `history-lifecycle-unit.log`、`history-lifecycle-native-unit.log`，与先前 246/63 存在重叠，不相加。

补图失败轮保留：run01 是 Appearance 外层高级与内层模板 details 的 locator；run02/03 在面板入场动画中未真正按下 resize handle，事件证据没有 got/lost pointer capture，不能算产品拖动失败；run04 点击 handle 被裁剪的外缘，真实聊天区截取指针；run05 实际拖动/写盘/切标签通过，但重开设置入口先到 General，历史 visited pane 隐藏，需要真实点击 Browser 导航。run06 实际右侧宽度 595.875→555.875 CSS px，pointer-up 前未写盘、之后 ratio=0.4732861538923481，两个标签保持宽度；History 原生残留失败如上。

run06 的 01/03/04/05 是实际 General 顶部、Appearance 闭合/展开、Plugins 标题同行工具栏。02 名称写了通知/高级，但画面仍在 General 顶部，仅露通知第一行，不能作为下半区完成的证据；最终补图必须用真实 group/三控件/高级 summary 的 viewport 包含断言，再采 settled PNG。

## 最终补充 QA：run07 通过

统一 production build run06 后，`desktop-regions/native-region-visual-run07/evidence.json`、`native-region-visual-run07.log` 均完成，退出 0、failures=[]。没有重跑六组原生服务；没有操作正在运行任务的正常用户窗口或通过 Computer Use 发送桌面输入。

当前补拍 4 图全部逐张看过：`02-final-production-general-three-notifications-and-advanced.png` 真正显示三类通知与闭合高级；`06` 为紧凑日期组、`07` 为真实页面菜单、`08` 为日期组实际折叠。General viewport CSS 范围 104.80–992.80，通知 group 404.04–649.90，任务完成/权限/问题通知与高级 summary 全部落在 viewport 内。另引用未变化的 run06 01/03/04/05（General 顶部、Appearance 闭合/展开、Plugins 标题同行 toolbar），共 8 个区域视图；不把 run06 错误的 02 计入。

实际历史菜单“打开页面”新生成第二个当前 URL visit。重新激活历史后，DOM 同 URL 数量与真实 IPC 返回的两个 visit 一致；日期折叠确实隐藏记录。移除 ACK 后等待 canonical 空状态，再读取真实 IPC，`remainingAfterCanonicalRemoval=[]`，不再留原生记录。测试原草稿 `ORIGINAL UNSENT DRAFT` 和现有 ready attachment 均保留。

run06 已验证的真实拖动/松手写盘/切两个标签宽度不变在 evidence.references 中明确引用，不重复记作本轮运行。run07 对实际进程退出/重启后读取 ratio 做新验证，值仍为 `0.4732861538923481`。QA App 最终关闭，普通用户 App 未被重启或接管。
