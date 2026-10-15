# 2026-10-14 设置选择控件：Source 对照与生产验收

本批依据是用户新图 `C:/Users/ago/AppData/Local/Temp/codex-clipboard-ff50df00-fe70-4eee-90b1-fb65584abbce.png`，保存的 Codex General/菜单前端源码，以及现有真实 native preferences、安装目标发现和终端消费者。普通用户 profile、草稿、附件与编辑器未用于测试。完整功能实机记录为 `general-select-after06`；后续只对精确 Source modal 颜色与最新 native target 顺序补必要的 cold companion，不重跑整批语义检查。

## 已落实的 Source 调用链

- `general-settings::tc` 查询 `cwd:null` 的全局打开偏好与实际安装目标；`nc` 只排除 `available:false`，不能照工作区菜单把 hidden Default app 去掉。`sc` 为应用图标、完整目标 label；选中项是右侧 check。MiniCode General 继续从真实 `fileOpenTargets` 生成选项，复用 `FileOpenTargetIcon`；仅提交全局 `fileOpenTarget`，不清除 `fileOpenTargetPerPath`。
- `general-settings::ic/oc` 是集成 Shell 的 compact/end 菜单，前端标签来自 `Ot = app-shared::qMt` 的 PowerShell、Command Prompt、Git Bash、WSL。Native `Gqe/Y8/Kqe` 选择 PATH 上 pwsh→powershell、COMSPEC、真实 Git Bash 与 WSL。终端 producer/真实 PTY 由另一批对照修复；General 不造不可执行的选项。
- `hNs` 是普通 compact trigger，图标和 label 在左，chevron 静态向下。`p7→Sji→DAi` 是菜单表面，没有额外 scale/fade 进入；Source sideOffset 1 与 margin 1 对应约 2 px 空隙。保存的原始片段见 `output/playwright/codex-control-audit-20261013/source/select-trigger-motion-source.json`。仅 Settings 选择去掉 MiniCode 自创的开栏箭头翻转与缩放进入。
- `general-settings::go` 的默认权限始终 checked/disabled；Full access 关闭立即写 false，开启先进入 `vo`。`vo` 是普通 modal dialog，form submit，title/body/availability 说明、Cancel 与带风险图标的 danger Confirm；Confirm 只使 Full access 选项可见，不切当前任务权限。取消、Escape 和外部关闭不写偏好。普通说明链接沿现有网址偏好，确认框 Learn more 强制外部浏览器。
- `general-settings::Ec` 使用 compact/end 菜单。现有真实 sendShortcut/followUp/remoteImagePolicy 全部接同一个 SelectMenu，保留它们现有合法值和真实消费者。共 9 个 General 选择控件；不添加尚无完整 driver 的 Win/WSL agent 环境按钮。

源码功能片段通过 AST 提取至 `source/general-select-source-functions.json`，没有执行保存的 Source JavaScript。中文版确认文案来自保存的 `zh-CN-b05cfd55c534.js`，仅将产品名称替换为 MiniCode。

## 完整文字与原字体

共享 SelectMenu 保留原 `option children/value/onValueChange` 契约，新增可选 `renderOptionIcon(value)`。Trigger 和 option 复用真实图标；文字按内容适配，菜单根据最长实际选项取得自然宽度，再受 viewport 界限约束；空间不足时换行，不用省略号截断真实 selected label。旧 native select 留下的外层 40 px padding 和 240/260 px 上限已移除。路径与 selected label 分开：路径按 Source `w-48` 独立显示，并保留完整 title。

真实 `before02` 记录 General 原字体为 Anthropic Sans / Noto Sans SC 等、14 px、400。`after01` 实跑发现新 settings-select 继承旧 native medium，使原 400 变为 500；严格 QA 失败，没有放宽断言。已用 `settings-select-regular` 只恢复本轮旧 generic 的 400 控件，既有 native/medium 消费者不改。General 9 控件及 Root 的相应新 class 消费者共用该变体。

## 已完成的统一检查与保留证据

初次统一 22 文件为 302/312；10 个失败来自 3 个旧 fixture 文件。`SettingsCenter.test.tsx` 的 2 个旧 segmented 点击改为真实 trigger→option；其余 2 个 browser 测试及 `BrowserPanel.nilOwner.test.tsx` 的 4 个测试补真实新增订阅 mock；`FullAccessVisibility.chain.test.tsx` 的 2 个测试补真实 runtime available modes 和当前 trigger 标识，原 mode/draft/attachment/owner 检查保留。只重跑这 3 文件为 73/73。

按文件、fullName 和同名参数案例 ordinal 合并最终 **312 passed / 0 failed / 0 skipped**，见 `settings-select-latest-results.json`。两批结果范围重叠，不能相加。原始 `settings-select-integration-unit01/02.json` 完整保留。

`build01` 在新增通知 contract 的 `task_id` 类型处失败；真实 `ownerFields` 已发该字段，通知批补实际 ControlResponseCommand 字段并收窄 Cancel，未加假字段或生产兜底。`build02` TypeScript/Vite 通过，随后实机捕获上述字体回归。恢复字体后的 `build03` TypeScript/Vite 通过；构建日志保存在同一审计目录，未再重复全量语义测试。

`general-select-before01` 漏初始化 QA workspace trust，实际后端正确拒绝 `workspace_untrusted`，失败保留且不算通过。补仅属于临时 QA workspace 的 trust 后，`before02` 真实创建 owner、保留草稿、读取 native 目标并通过界面选择 VS Code，2 张当前构建截图已查看。`after01` 真实新构建首图中图标/check、静态向下箭头已到达，但由于 400→500 字体回归明确失败；该轮不作为最终通过依据。

`after02` 的 General 与菜单实机组通过，但临时 QA profile 仅写当前 `llm.custom`，没有 `provider_history`；当前真实 ProviderTab 卡片只来自已保存 history，因此旧 QA 假设已有 active card 的点击失败。后续改成真实「添加提供商→填写本地接口/测试密钥→获取模型列表→分别添加两个真实候选→保存→重开高级默认模型」，不改变生产 Provider 的行为。

Antigravity 原 command native icon 是齿轮，两份真实 exe native icon 也都是 generic window，不能算品牌通过。最终 producer 按保存 Source `yLe/iQ` 发布现有 `apps/antigravity.png`，不请求该 command/exe native icon。`after03` 真正冷启已截图并实际查看品牌 A，但 QA 新断言遗漏生产 `BASE_URL=./`，将 `./apps/antigravity.png` 误比为 `apps/antigravity.png`；该轮明确保留失败。现改为比较真实解析的绝对 asset URL。独立 producer 的 8 项检查与真实 IPC 品牌解码记录见 `2026-10-14-antigravity-open-target-icon.md`，不能和前端 312 项相加。

`after04` 已通过真实 `GET /v1/models`、两个模型映射、保存与发起会话 `llm.config.set`，但默认模型 QA 点击之后菜单被收起。独立同一临时 profile 的 `general-select-model-diag.json` 记录实际顺序：Playwright 把屏幕外 trigger 从 settings scrollTop 358 滚入 938 后同帧点击，待发送的外层 scroll 在 listbox 打开后才抵达；原来要求保留的外滚动关闭正确触发。真实滚动稳定后 Enter 打开保持菜单，两张诊断图已查看。QA 改为先真实 `scrollIntoViewIfNeeded`、等待两帧处理已发生的 scroll、再鼠标点击；没有添加生产忽略滚动、延时兜底或放宽外滚关闭断言。`after04` 保留失败，不计最终通过。

`after05` 三组通过（真实菜单、General 操作、长模型与窄窗口完整换行），并到达真实第二 registered window 的 download policy block→allow 同步截图。浏览器权限清理的临时本地 URL 触发了真正 `confirmNavigationUrl` 私网原生审批；测试没有人回答，`create` 未返回。通过该临时 profile 的明确 DevTools endpoint 做只读观察：两窗 tabs 为空，peer permission snapshots 为空，未擅自注入“已批准”。只结束已核实属于该临时 profile 的 QA Electron 进程，`after05/evidence.json` 留下关闭错误，权限清理与重启不算通过。后续改为真实加载公开 `https://example.com/`，不 mock 私网审批。

查看 `after05` 每张图时发现新的真实视觉问题：确认框 body portal 不在 `#root` 下，按钮漏继承 ui-polish 的几何；原 danger override 还把 Source 浅红按钮改成了实心红。再次按 AST 核对 `General _=app-shared::Ck→r5→Jbi`：默认 `kbi=rounded-full`、`Qbi=px-2 py-0.5 text-sm leading-[18px]`，secondary=text/5、danger=chart-red/10 红字与 hover/20、透明边框。只在 full-access footer 补实际 8/2 px padding、18 px line-height、完整圆角与对应浅红色；现有字体、全局按钮未改。证据 `source/full-access-button-source.json`，最新 `build04` TypeScript/Vite 通过。QA 用真实按钮几何及 canvas 读取背景 alpha 验收；这一 CSS 修复没有重跑此前 312 个语义检查。

## 完整生产功能实机结果

`general-select-after06/evidence.json` 为 **6 组通过、errors=[]、进程退出 0**，该轮 10 张 PNG 已逐张实际查看。运行的是完整 production `frontend/dist/index.html`，`build04` index SHA256 为 `37073da5db143fe76145889c450fb579f8b63dc0e6e35423bf4c588153db0c75`，时间 `2026-10-14T13:54:34.454Z`。

| 实际链路 | 结果与证据 |
| --- | --- |
| 真实安装目标→General 图标/label→native global preference | 选中 VS Code 的 native 图标、完整 label、右侧 check；Antigravity 来源为 Source 品牌 A。静态向下 chevron、无自创 scale/fade、Home/End/Arrow/Escape、外点击和真实外滚动关闭全部通过。选项集合等于该轮真实 native producer 集合；没有把该轮旧 producer 顺序写成 Source 等价。 |
| 真实 General 操作、焦点、权限可见性 | 实际选择 Command Prompt、关闭快捷键确认、仅未聚焦通知、中英语言；真实 Tab/Shift+Tab 顺序；Full access Cancel/Confirm/hide 只改 visibility，当前任务保持 confirm。Anthropic Sans / Noto Sans SC 等、14 px、400 与 `before02` 精确相同。 |
| 设置→模型 discovery→保存→发起会话配置→完整选择 | 真实 `GET /v1/models` 返回两个声明的 QA 模型，用户界面逐个添加映射并保存，真实 `llm.config.set` 应用于发起 owner。91 字符模型 ID 完整出现在 trigger 和 option；920 px 窗口下 trigger 换成两行，无省略号或越界。此模型 catalog 是自有控制服务，不冒称验证真实外部供应商任务。 |
| 实际 native 浏览器→真实 origin grant/clear→另一 registered renderer | Native 创建 `https://example.com/`，返回 Example Domain；代码等待真实 tab loading 完成后才设置/清理权限。creation snapshot 中 loading=true 是初始返回样本，不能当作 final sample。Peer 原生订阅先收到 `sitePermissions[https://example.com]=[notifications]`，清理后收到 `{}`；原生读取确认不再有被清除的 grant。没有 mock 私网审批，也没有 renderer 状态/选项/偏好注入。 |
| 原生 File→新建窗口→浏览器策略 live 广播 | 实际第二个 registered renderer 中 block→allow 动态同步，完整“保存到下载目录”可读；两份真实未发送草稿 A/B 均保留；全局文件目标变化不清除原 workspace→fileManager 的 per-path 偏好。 |
| 真退出进程→原临时 profile 冷启动 | General 文件目标、Command Prompt、关闭快捷键确认、zh-CN、false Full access 以及持久化浏览器下载 allow 都恢复，完整文字显示；不是热重载。 |

`after06` modal 的柔红来自 MiniCode 原有 `state-danger-soft`，canvas alpha=0.12156862745098039；它证明没有实心红与几何丢失，但不是 Source 精确 /10。原始记录不改写为精确通过。最终只对该 scoped 背景改为 `color-mix(... 10%, transparent)`，hover 为 20%；hover selector 同时匹配真实按钮与 `:not(:disabled)`，避免输给通用 danger hover。原颜色基变量与字体不改，QA 改为相对 0.10/0.20 的 8-bit canvas ±1/255 精度，不继续用 alpha<0.3 的宽条件。

## 最终 Source modal 与目标顺序补验

`general-select-after07/evidence.json` 为 **4 组通过、errors=[]、退出 0**，7 张最终 PNG 均已逐张实际查看。本轮只做 General/modal companion，复用 `after06` 的明确临时 profile/workspace，不重新 seed 或覆盖其 settings/data；正常用户 profile 与窗口未操作。之前的 312 主批与 after06 完整模型/browser-peer/草稿/cold 场景未重复运行。

精确默认 /10 实测 canvas alpha 为 `0.10196078431372549`（26/255），hover /20 为 `0.2`（51/255）；narrow 与新的 cold process 默认均为 26/255。判据仅允许 canvas 8-bit 的 ±1/255 舍入，未用宽容区间替代 Source 值。实际按钮为 padding 8/2 px、18 px line-height、24 px height、9999 px full-radius，颜色基变量与字体保持原 MiniCode。920 px 原生窗的实际 viewport 为 921 px，modal 宽 380 px、left 270.5、right 650.5，完整文字及 footer 无裁切。Cancel/Confirm/hide 后仍只改变 visibility，当前 task permissionMode 保持 confirm；真正关闭进程再启动后 global/per-path 偏好与 Source modal 仍正确。

最新 producer 已由 native 批按 `yLe/iQ` 固定 factory 顺序修正，不做 kind/字母排序。最终实际菜单顺序为 `minicode → vscode → visualStudio → cursor → antigravity → system → fileManager`：保留用户允许的 MiniCode 内置编辑器；真实安装的核心编辑器之后才是 Default app 和 File Explorer。PNG 01 已查看并确认该顺序、品牌 A 与 400 原字体。没有把当前未发现的 Terminal/WSL file targets 塞进菜单。Native Source-order 9 项验证与原始 IPC 样本属于另一批，见 `open-target-order-native-results.json`，不与前端测试相加。

`settings-select-production-build05.log` 的 TypeScript/Vite 通过。随后只有 scoped hover selector 变更，`settings-select-css-build06.log` 仅做必要的 Vite CSS 刷新并通过，没有再次跑 tsc 或 312。after07 实际 bundle 为 `assets/index-DQRQ8DWm.js`，CSS 为 `assets/index-CpBOf0LT.css`；生产 index SHA256 `97de63736586dbde972ac1a302507ac2970213f48b0badc3f61415585d4d3a47`，构建时间 `2026-10-14T14:12:21.829Z`。该 hash 与本批最终 QA 冻结记录一致，后续 Enter、通知和侧栏批次已产生新 dist，不能将这些旧图冒称为后续新功能验收。

关键新图位于 `output/playwright/codex-control-audit-20261013/general-select-after07/`：`01-real-installed-app-menu-and-selected-label.png`、`04-source-full-access-visibility-confirmation.png`、`05-source-full-access-confirm-hover20.png`、`06-narrow-source-full-access-dialog.png`、`07-cold-source-full-access-dialog.png`。完整功能仍以 after06 的 6 组、10 张真实截图为依据；4 组 companion 与其有重叠，不能相加冒称 10 个独立场景。

## 本批不能冒称已等同的行为

Source Win/WSL agent 环境切换涉及真实 `runCodexInWsl`、bash 可用性、重启以及 remote WSL 连接；本批没有该完整 driver，因此没有摆假环境项。after07 固定时点的 Source 第三个 Enter 多行模式和更多语言资源未据下拉样式更换算作完成；第三 Enter 行为已在后续独立批补齐并完成25项真实功能验收，见`2026-10-14-enter-behavior-desktop.md`，不倒填进本批结果。WSL integrated Shell 仅依据发现与 argv 核对，不将没有运行的 distribution 算通过。用户明确官方账号不做，官方账号登录、授权与接入已排除。其余完整服务与 agent harness 需要各自真实任务验收。
