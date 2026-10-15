# 全局 Pinned 手动排序

本批补齐全局 Pinned 的真实拖动、Source Alt-Arrow 调序、后端持久化和冷启动恢复。项目内 occurrence、项目标题排序和自定义分组分别有不同的 Source 状态，本批没有混用全局置顶顺序。官方账号登录、授权和接入按用户最新要求排除，不作为待办或阻塞。

## 当前 Source 依据

读取当前已安装的 Codex `26.1007.9446.0`，包路径和提取目录见 [source.json](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/source.json)。使用 `initial-`、`shared-` 前缀的精确函数记录，避免不同 bundle 中相同 minified 名字覆盖。

- [initial-JMi](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-JMi.txt)、[initial-YMi](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-YMi.txt)：真实 pin/beforeThreadId 与 native `pinnedThreads.setOrder` / `set-pinned-threads-order`。保存失败走真实失败回执。
- [initial-CCs](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-CCs.txt)、[initial-J1o](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-J1o.txt)、[initial-UNi](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-UNi.txt)：显示 canonical 顺序；拖动结束后先显示 pending 顺序，再等待实际保存；较早请求不能清掉较新的 pending；Source 从最近活动排序调序后切 manual。
- [shared-_P](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/shared-_P.txt)：只替换可见位置，隐藏的 pinned ID 保留原位。完整 `[A,H,B,C]`、当前可见 `[A,B,C]`，将 C 放 A 前后得到 `[C,H,A,B]`。
- [initial-fLo](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-fLo.txt)、[initial-oLo](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-oLo.txt)、[initial-gis](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-gis.txt)：鼠标移动距离 6 px 后起拖；原位 ghost 透明度 20%、真实 inert 状态；拖动浮层 70% 透明；原列表拖动时不接收 pointer；行内真实 Alt+ArrowUp/Down；wrapper 显式去掉默认 onKeyDown，不增加 Space/Enter 起拖入口，也不加拖动把手。
- [initial-aI](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-aI.txt)、[initial-s0](../../output/playwright/codex-control-audit-20261013/codex-sidebar-sort-source-current/initial-s0.txt)：以实际 pointerY 和目标行中线决定 before/after，同一行内跨中线也持续更新；指示线 2 px，空心圆 8 px、边框 2 px，左右偏移与 Source 一致。当前安装包 CSS 的 `text-info` 为 light `#3a83f7`、dark `#63a8f8`，由共用颜色 token 提供。

Source 的 `pinned-sidebar-sort-mode-v1` 默认 manual；`flat-project-sidebar-preferences-v1` 的 chat/project 默认 updated_at。项目手动调序只在其真实 manual 模式开启。全局 pinned 顺序、项目内 occurrence 顺序、`PROJECT_ORDER` 工作区标题顺序是三个范围。

## 实际生产链

`PinnedConversationList` 只包裹全局已固定区；按钮仍是原来的 SessionRow 主按钮。前端发送 `conversation.pin.reorder` 的 moved/before/visible IDs，经已存在的语义回执机制等待真实结果；拒绝时恢复 canonical 数据。现有项目和最近列表不变为全局可拖动列表。

后端 sidebar handler 调用 repository，在 `.pinned-thread-order.json` 单独保存完整 thread_ids 和 manual 模式。只替换真实可见槽位，并沿用现有原子写入、文件 mutation lock 和 inventory revision。Pin/Unpin 和移入项目同时维护真实 membership；重新 Pin 追加，移动项目时按 Source 取消 Pin。没有重写 conversation transcript、context checkpoint、pinned_at、updated_at、执行 cwd、workspace 或 unread。

真实 `conversation.list` producer 携带 canonical pinned IDs 和 sort mode，所有窗口按既有 inventory instance/revision 消费。这里只复用 Source 实际行为的本地 facade，不声称 MiniCode 命令名与官方 native IPC/RPC 字面相同。

## 有限批量验证

- 后端三个相关文件 **37 passed / 0 failed / 0 errors / 0 skipped**：[实际 XML](../../output/pinned-reorder-backend-20261015.xml)。包含隐藏槽位、冷读、metadata 和 generation 字节不变、活动时间、Pin membership、实际手动 unread 并发写、真实 handler peer 广播以及真实 WebSocketSession inventory producer。
- 前端七个相关文件 **126 passed / 0 failed / 0 skipped**：[实际 JSON](../../output/pinned-reorder-frontend-20261015.json)。包含真实 Alt 调序到命令、canonical inventory、最近活动转 manual、拒绝恢复和旧 inventory 被拒；原 reader、草稿、附件引用及未读保持。修 Source onDragMove/pending 后另行复验本批新增四项 **4/4**：[最终窄复验](../../output/pinned-reorder-frontend-final-20261015.json)，不与 126 相加。最终 DOM inert 与 portal 的实际呈现由下述当前构建 companion 验证。
- 最终 TypeScript 构建 `npm run build -- --logLevel error` 中的 `tsc -b` 和 Vite 均 exit 0；随后只修共用 CSS scope 并再次 Vite exit 0。本批实际验收 bundle 为 `index-DorY0-30.js`，CSS `index-CCxOW9kY.css`。保留 MiniCode 字体和字号。`frontend/dist/index.html` SHA256 为 `8C2459EDFE2E0330932E7548A7DD7C2519BF3EB55D6D570D5F5EBD1F65E7068E`；该 JS SHA256 为 `5AC8B600614E5FC05C8EC31BCDC91733D1A40F38155C3D0A0FD2B7AAE12F7470`。

## 真实 Electron、HTTP、WS 与冷启动

[production-pinned-reorder-run02/verdict.json](../../output/playwright/codex-control-audit-20261013/production-pinned-reorder-run02/verdict.json) **passed=true，5/5，errors=[]**。使用真实桌面、IPC、backend、repository、上传和 WebSocket；只把模型 HTTP 回复设为可控 fixture。窗口始终 offscreen，profile 与 A/B 工作区独立；没有操作普通用户窗口、未保存编辑器、剪贴板和草稿。

1. 真实鼠标先移动 4 px 不起拖，再跨过 6 px 起拖，观察 20% ghost、70% overlay、2 px/8 px 插入线；在同一目标行上半部与下半部移动，指示线实时改变；真实 drop 发出准确 moved/before/visible IDs 并由后端写完整顺序。隐藏 H 槽位保留。
2. 真实 Escape 取消，不发送 reorder，不改 canonical 顺序和 reader。
3. 真实 Alt+ArrowDown 保存调序并保留主按钮焦点；所有原 manifest/generation 文件 SHA256 不变，pinned_at、活动时间、cwd、手动 unread 均保持。
4. 真实归档/恢复隐藏 pin 后仍回到原槽位，未切换前台 reader。
5. 关闭并重新启动实际 Electron 和 sidecar，canonical order 和隐藏槽位恢复；原 A 对话草稿、B reader 草稿、隐藏 workspace A 草稿以及两次真实上传的附件 ID、artifact ID、conversation owner、workspace root、ready 状态全部保持。

逐图检查 run01 时发现 portal 浮层丢了 Pinned 的 12 px 缩进，文字多偏右 28 px；功能虽 5/5，未把该图记为视觉通过。已把原行与浮层的缩进合并到同一条共享规则，run02 实际断言 `rowIndent=overlayIndent=12px`，六张当前图均生成。随后补完整 Source 行记录，确认并恢复 ghost 的实际 DOM inert，而非通过 React18 不支持的 JSX boolean 写入。

最终 bundle 另作当前构建窄视觉 companion：[production-pinned-reorder-final-visual03/verdict.json](../../output/playwright/codex-control-audit-20261013/production-pinned-reorder-final-visual03/verdict.json) **passed=true，2/2，errors=[]，exit 0**。它复用 run02 的独立 profile，只核对当前 mouse/ESC/Alt、Source inert/透明度/指示线和共享行样式；每次真实 mouse/Alt 移动都等相应 command.result ACK 及 canonical inventory 后继续。原行和浮层都为 12 px 缩进、13 px 容器字号，同一字体；最终三张 PNG 已逐图查看。沿用 run02 的完整 cold 证据，不重复宣称又运行了五项 cold 场景。

前两次 companion 的失败保留，均不算通过：`production-pinned-reorder-final-visual` 发现 portal 容器 15 px 而原行 13 px，是本批真实样式继承问题；已将原 sidebar SessionRow 的字体、字号、行高、圆角等规则共享给 overlay，原值没有改变。`production-pinned-reorder-final-visual02` 视觉项通过，但 QA 连续 mouse/Alt 只等 optimistic UI 就读取磁盘，真实回执尚未处理完；只修 QA 等每一步真实 ACK/canonical，未修改产品来迎合 timing。最终 visual03 通过。

## 范围

本批闭合本地全局 Pinned 调序。项目内手动调序、工作区标题顺序、自定义分组，以及跨远端或共享项目的排序状态属于独立 Source 链路，本批不算完成。官方账号不做。完整桌面和 harness 是否对齐应按各自最新证据判断，不能由这项控件验证推导全等。
