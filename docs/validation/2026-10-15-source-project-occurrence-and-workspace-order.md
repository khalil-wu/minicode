# 项目内聊天与工作区标题排序

本批补齐项目内 occurrence 手动排序、共用排序菜单涉及的 Recents 手动顺序，以及独立的工作区标题 `PROJECT_ORDER`。官方账号登录、授权和接入按用户要求排除。全局 Pinned 继续使用已有独立状态；本批没有把项目顺序写进置顶顺序、活动时间或执行 cwd。

## 当前 Source 与真实 caller

依据已安装 Codex `26.1007.9446.0` 的当前 ASAR。原始目录、包身份见 [已有 Source 记录](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/source.json)，新增精确函数、offset、bundle SHA256 与调用点见 [本批 Source 提取](../../output/playwright/codex-control-audit-20261013/codex-project-sort-source-current/source.json)。提取时按 bundle 区分同名 minified 标识符。

- `h9o` 实际项目/聊天菜单的 `Sort by` 子菜单只有 `updated_at`、`manual` 两种 radio。caller 是 `qoo → IOr`，一次共同设置 `chatSortMode` 和 `projectSortMode`；不是每个项目单独设置排序模式。
- 初始模式为 `updated_at`。`qoo → Joo/sAr` 首次进入 manual 时为尚未保存的可见项目取前 5 组、每组前 5 条 seed；已有 manual 顺序保留，切回最近活动再进入 manual 会恢复它。不能每次切 manual 都覆盖之前的人工调序。
- 本地项目真实链是 `exs → Xoo → $kr/Rdr → SIDEBAR_PROJECT_THREAD_ORDERS`，按 `projectId` 保存。`gis`、`rLo` 和 `Xoo` 在项目非 manual 模式下禁用同组重排。Recents 的 occurrence 状态独立，不能借用某个项目或 Pinned 的顺序。
- `Rdr → Hu/_P` 只替换本次可见槽位。隐藏、归档或暂未展开的条目保留自己的槽位。
- 工作区标题链是 `jbs/rSs → Jlr → Ylr → vcr → setProjectOrder`，对应独立 native `PROJECT_ORDER`。它不受聊天排序模式限制，也不通过重开项目或改 MRU 时间来伪造顺序；`Ylr` 保留未显示项目的槽位。
- Source 创建聊天和移入项目时会把真实的新 occurrence 放到 manual 列表开头；后续活动时间不应重排已经保存的 manual 顺序。

## 真实图标与菜单呈现

[图标提取记录](../../output/playwright/codex-control-audit-20261013/codex-project-sort-source-current/source-menu-icons.json)保存完整 Source 路径、offset、函数和每份 SVG SHA256：

- `ha` 的真实导入是 `Iet → upr=$(z5n)`，即 `ellipsis-horizontal-light-16`。
- `xIe` 的真实导入是 `i7 → Zlr=$(W2n)`，即 `arrow-down-arrow-up-light-16`。
- `s9o` click 菜单的 radio 使用 `menuitemradio/aria-checked` 和右侧 `r7` check；其真实 SVG 为 17×17。无图标的 radio 子菜单不保留空的 leading icon 列。

三个原始 SVG 已用于产品，使用现有 Electron 相对 `BASE_URL`。字体、字号、字重和原行缩进的数值保持。拖动复用 Pinned 已有 Source 实现：6 px 起拖、20% ghost、真实 DOM inert、70% 浮层、2 px/8 px 指示线、连续跨中线更新、Escape 取消和 Alt+Arrow 调序；没有加拖动把手或 Space/Enter 起拖入口。

## MiniCode 完整生产链

`.sidebar-occurrence-order.json` 独立保存模式、项目标题顺序、按项目索引的聊天顺序、Recents 顺序与自身 revision。repository 的创建、工作区首次绑定和移入项目同时维护真实 manual occurrence；排序写入使用现有 repository store lock 和原子发布，只更新导航元数据及 inventory revision。

前端真实菜单与拖动发出四种本地 facade 命令，经 sidebar handler、repository 产生 canonical `conversation.list.sidebar_order`，所有窗口按真实 inventory 与 sidebar revision 接收。旧 snapshot 不能覆盖较新顺序。拒绝使用真实命令结果，结束 pending 后恢复 canonical 顺序；不吞掉真实失败。

全局 Pinned、项目内 occurrence、Recents、工作区标题顺序分别保存和消费。排序不切换 reader，不改变 workspace、cwd、模型、手动 unread，不重写聊天 manifest/generation，也不保存、清空或替换现有草稿与附件。

## 批量验证

- 后端一次统一运行新排序专项、原 Pinned、sidebar metadata 三文件：[实际 XML](../../output/source-project-sidebar-order-backend-20261015.xml)，**29 passed，0 failed / errors / skipped**。新专项 16 条，包含隐藏槽位、模式恢复、创建和移项目 prepend、工作区首次绑定、同 store 并发 unread、真实 handler peer 广播、真实 inventory producer 与冷读，以及所有原聊天 generation 字节不变。
- desktop agent 统一运行 Source、Root 与排序共 11 个前端文件，[实际 JSON](../../output/playwright/codex-control-audit-20261013/follower-sort-final-unit-batch01.json) **312/312 passed**。本批相关四文件为 `SidebarProjectOrder.chain.test.tsx`、`SidebarPinnedOrder.chain.test.tsx`、`ConversationsTab.component.test.tsx`、`ContextMenu.test.tsx`，共 **43 条**，是 312 的子集。
- 统一协议校验、含 TypeScript 的唯一共享生产 build、bundle budget 已通过。验收构建为 `assets/index-CLFATioc.js`；`dist/index.html` SHA256 为 `8c5a4247adc6e05c7a3675443be8c8732e2c4147872bfe5b12af22e5200a78b4`。实际 JS 已确认包含相对 `./apps/sidebar-${e}.svg`。

## 实际 Electron QA 状态

真实桌面 QA 使用独立 offscreen profile、A/B 临时项目、真实 IPC、sidecar、WebSocket、repository 和两次真实文件上传；只把模型 HTTP 回答设置为可控 fixture。普通用户窗口、编辑器、剪贴板和草稿未被操作。脚本为 [project-sidebar-sort-desktop-qa.cjs](../../output/playwright/codex-control-audit-20261013/project-sidebar-sort-desktop-qa.cjs)。

[按场景去重后的最终记录](../../output/playwright/codex-control-audit-20261013/project-sidebar-sort-latest-results.json)为 **6 个不同场景全部通过**。前五项取自 [run04 的实际成功检查](../../output/playwright/codex-control-audit-20261013/production-project-sidebar-sort-run04/verdict.json)，最后一项取自 [沿用 run04 原 profile、原聊天和原上传的 cold continuation](../../output/playwright/codex-control-audit-20261013/production-project-sidebar-sort-cold04-final/verdict.json)，后者 `passed=true / errors=[] / exit 0`。没有把先前重复执行的同一场景累加，也没有把 run04 原始总体失败改成成功。

1. 实际点击项目选项、排序子菜单与 manual radio，确认共同模式真实命令；三份原始 Source 图标在当前 Electron 中实际解码为 16×16、16×16、17×17。
2. 实际鼠标跨过 6 px 后起拖项目聊天，保存准确 moved/before/visible IDs，隐藏归档与全局 pin 槽位保留；原行和浮层同为原字体、13 px、400 字重、17.55 px 行高、40 px 缩进。ghost 为 20%、真实 inert，浮层为 70%，指示线 2 px、圆环 8 px/2 px。
3. Escape 不发重排命令；实际 Alt+Arrow 保留主按钮焦点，项目 occurrence 与 Recents 各自保存，Pinned 保持独立。原聊天 generation/manifest 字节、手动 unread、前台 reader、草稿和两次实际上传均保持。
4. 实际鼠标拖动工作区标题，`PROJECT_ORDER` 持久化而 MRU JSON 字节不变，不改变聊天模式、reader 或项目内顺序。
5. 从聊天菜单切最近活动、项目菜单切回 manual，恢复已保存顺序；实际创建新聊天后仅在它真实所属的 manual occurrence 中 prepend。
6. 只沿用原 profile 补完冷核对，没有第五次 profile 或重 seed。工作区标题、项目聊天、Recents、Pinned、manual 模式、原 reader/cwd、A/B 非空草稿、隐藏工作区草稿以及两份真实上传的 artifact ID、会话/工作区归属、ready 状态与文件字节均恢复。归档聊天恢复后回到原槽位；聊天和上传存储字节保持。

五张排序验收 PNG 已逐张查看。随后沿用同一 profile 补实际 A→B owner 往返：两次激活后的 **main** 草稿与附件全字段均匹配原证据，A 实际输入区和原 `owner.txt` 已截图；最后只补当前 B 可见 `reader.txt` 节点，[main 补证 exit 0 / errors=[]](../../output/playwright/codex-control-audit-20261013/production-project-sidebar-sort-cold-reader-photo04/verdict.json)，两张补图也已查看。这仍属于第六项 cold 场景，不增加场景数。当前 reader 的可见 main 是权威，inactive shadow 可旧、可省略；没有以其是否相等判断丢失。B 补图只用于输入区证明，完整排序快照依据仍是 cold-final 第五张图。未操作普通用户窗口、编辑器、剪贴板或个人 profile。

所有未通过的原始 QA 记录保留，且没有修改产品迎合断言：run01 用原始 `C:/Temp` 索引已规范为 `C:\temp` 的合法 seed key；run02 原始顺序期望漏了应保留的隐藏 pin；run03 创建真实新聊天后漏算其合法空草稿 entry；run04 cold 期望要求空 entry 被持久化；首次 cold companion 又把 main/hidden 中同一当前 reader 草稿的合法镜像当成新增会话。补充往返首次读 metadata 早于 inventory 到达，第二次 A/B main 的精确比较已经通过，后续全页 `.first().isVisible` 却选中了隐藏同名节点；原总体失败没有改写，只补真实可见 B composer 节点。最终按 conversation 对全部原非空内容和真实持久上传归属核对，并以切离/恢复后的实际 main 作补证。空 entry 的省略符合既有 `composer-drafts.ts` 的 `hasDraftContent` 契约，不是草稿丢失。

本批闭合上述本地排序范围。跨项目拖动转移、远端或共享项目、自定义分组，以及仍在独立审查的执行 context 与连接退场生命周期不由这些验证推导完成。
