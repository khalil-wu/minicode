# 侧栏固定、已读/未读与项目归属

依据为保存的 Codex ASAR `desktop-regions/source/app-initial-b8e1c8014bb4.js`、`app-shared-737655e1fb23.js` 及 `.tmp/codex-src` commit `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。没有读取当前安装包或用旧 MiniCode 图判断当前构建。

## Source 行为与实际实现

| 控件 | Source 依据 | MiniCode 生产链 |
|---|---|---|
| 固定 / 取消固定 | `nJ/BNi/$Mi` → native `pinnedThreads.set` / `set-thread-pinned`；`b0/fHo` 区分行内已固定实心图标、未固定轮廓图标、未读时的状态占位 | `conversation.pin.set` → repository 元数据 → 全窗口 canonical inventory → 独立 Pinned 区。被固定的对话不会在项目或最近中重复；取消固定恢复真实项目归属。实际固定时间保持追加顺序并冷恢复。快捷按钮是直接操作，不宣告不存在的 popup |
| 标记已读 / 未读 | `Jda` → host `markConversationAsRead/Unread`；共享 `unread-thread-ids-by-host-v1` 与 `thread-read-state-changed`；`QVo` 按真实状态选择菜单文案 | `conversation.read_state.set` → 持久 `has_unread_turn` → 全窗口 inventory → 中性未读点与对应菜单。点击打开一个未读对话时提交真实已读操作。后台标记不会切走当前 reader |
| 项目 / 移出项目 | `jJo/yJo` 排除当前项目、提供移出；`iJo` 写 membership；`pJo` 从 Pinned 移入项目时取消固定。官方 `ThreadMetadataUpdateParams` 与 `thread_metadata_update_response_inner` 验证已存在 project，空串清归属，独立于 cwd | `conversation.project.set` 验证真实 recent-project registry，持久独立 `project_id`；不改 `workspace_root/execution_cwd/worktree_path`、资源或 trust。`null` 保留原有推导，空串明确移出；被固定任务移项目同时取消固定。前端按 authoritative `project_id` 分组 |

这是 MiniCode 本地 facade 命令，映射 Source 的实际行为，不声明与官方 RPC 名称逐字兼容。`project_id` 使用已有 MiniCode 项目的 canonical 路径身份，没有伪造不存在的项目。

Repository 三个操作共用已有 metadata commit，保留 transcript/checkpoint 和 content revision；不会把固定、阅读或移动项目计为新活动时间。重复阅读状态不产生新 revision。与实际最终 turn 提交并发时，从最新 metadata 合并，保留新状态和原 turn 证据。

## 字体与 Source 图标

MiniCode 字体、字号与正文样式保留。菜单 Pin / Unpin / Read / Project 的四个 SVG 是 Source `UVo/YVo/NVo/LVo` 的原模板；行内固定按钮使用 `b0` 的 `Vse/Omr/jrr` 实心 16 px 和 `Ohe/Dmr/Nrr` 轮廓 16 px，不能把菜单 20 px 的取消固定叉号当作行内已固定图标。

提取脚本 `source/extract-sidebar-metadata-icons.cjs` 用 TypeScript AST 读取准确模板和 asset body，六个 SVG 实际大小 830–3158 bytes。最初 regex 错将 `</svg>` 与 backtick 之间的换行忽略，吞入后续 JS，导致空白图标；这是本 agent 引入并通过逐图检查发现的真实问题。已修实际 asset，未使用占位图标、CSS 隐藏或吞异常。XML 解析、生产浏览器解码和最终截图均验证。

## 统一验证

- 前端 6 文件 **153 passed / 0 failed / 0 skipped**：`sidebar-metadata-frontend01.json`，包括现有 sessionEvents、Sidebar、SessionRow、焦点与三个新实际 inventory consumer 流程。
- 后端首批 **27 passed / 6 failed**。五个新增 fixture 使用非法短 conversation ID 被现有 repository 规范化；改用真实合法 `conv-*` ID 后四项通过，最后一项 fixture 又错误从 `emit_command_result` 而非真实 `send_event` 看错误回执。修正后新增五项 **5/5**，`sidebar-metadata-backend03.xml`。旧 `test_conversation_payload_contract::test_isolated_worktree_failure_does_not_leave_a_shared_success_record` 的 SimpleNamespace 缺 `execution_cwd` 已单独交 Root/sibling 修，本 agent 未修改其测试；本地最新合并表 **32 passed / 1 known fixture failure / 0 errors / 0 skipped**，`sidebar-metadata-backend-latest.json`，不把该失败算通过。
- 最终 TypeScript 与 Vite 通过：`sidebar-metadata-row-icons-build-final.log`。最新 dist 包含整批控件、协议与正确 Source SVG。

## 最新生产桌面

`sidebar-metadata-desktop-qa.cjs` 使用两个真实工作区和两个真实 WS owner，实际上传 `original.txt`，保留两个 unsent drafts。model HTTP 回复受控，Electron、backend、repository、上传和菜单点击均使用生产实现。

`production-sidebar-metadata-run03/verdict.json`：**passed=true / errors=[]**，四场景、七张 settled 图。验证真实 Pin/Unpin、未读/已读/打开清读；项目菜单不列当前项目；移动项目取消 Pin，移出后在最近保留任务；整个后台操作阶段没有 `conversation.switched`。原执行 cwd 与 `original.txt` 字节不变，reader 与草稿不变。

冷关闭/重新启动 Electron 与 backend，两个已固定任务顺序相同、原未读状态保留；原 A/B 草稿分别恢复。实际上传附件的 ID、artifact ID、conversation owner 与原 workspace root 保持，见 `actual-upload-owner.json`。打开 A 后仍从 A 执行，草稿和 ready attachment 恢复，即使其 sidebar membership 已为 B。

run01 在冷启动 inventory 尚未到达时 QA 过早读 owner metadata，原失败保留；等待实际 owner inventory 后 run02 功能通过，但当时图标提取问题未能算视觉通过。修真实 SVG 后 run03 功能及菜单图标通过。最后还核对行内 Source 实心 pin 与菜单取消固定不同，补准确 16 px asset；`production-sidebar-icons-final01` 单独加载最新 bundle、真实 Pin 快捷操作、六个 SVG 解码与补图，引用 run03 的完整功能/冷启动证据，不重复声明这项补图又运行了四个场景。

所有过程都在独立临时 QA profile/workspace；未读取、控制、重启或保存普通用户窗口、草稿、剪贴板和未保存编辑器内容。

## 边界与剩余

此批最初闭合三个显式控件，不能据此宣称 Source 桌面全等。后续宿主自动未读已继续完成，见 [完成通知与未读生产链](2026-10-14-source-completion-unread-chain.md)；全局固定手动拖动、真实键盘调序、隐藏槽位与冷恢复也已补齐，见 [全局 Pinned 手动排序](2026-10-15-source-pinned-reorder.md)。项目内手动排序、工作区标题顺序、自定义分组和非本地项目源同步仍分别按其实际 Source 范围验收；此处不把全局置顶排序算作项目排序。官方账号登录、授权和接入按用户要求不做。
