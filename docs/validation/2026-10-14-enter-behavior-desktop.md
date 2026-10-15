# Source Enter 行为：独立 production desktop 验收

新增批以保存的 Source `Ec/Oc`、`app-initial::cBc/eUr`、`app-primary::nut/Ylt` 为依据。抽取保存在 `output/playwright/codex-control-audit-20261013/source/enter-behavior-source-functions.json`；同名 `app-shared::eUr` 是工具耗时函数，不能误作文档换行判断。没有执行保存的 Source JavaScript。

Source 的顺序是 enter、cmdIfMultiline、cmdAlways；MiniCode 保留已有 mod-enter 存储 ID 对应 cmdAlways。Source `eUr(doc)` 检查多个 block 或 hard_break，视觉软换行不算多行。IME composition/keyCode229 不发送。运行中相反 follow-up 的 chord 是 enter 下 CmdOrCtrl+Enter，其余两模式 CmdOrCtrl+Shift+Enter；只改变此次发送，不改全局 queue/steer 偏好。

实机脚本 `output/playwright/codex-control-audit-20261013/enter-behavior-desktop-qa.cjs` 已准备，语法检查通过。该脚本使用独立临时 profile/workspace 与隐藏的 offscreen Electron production 构建；只控制模型 HTTP 回复。真实键盘输入、原生 CDP IME、WS 命令/semantic receipts、上传附件、运行和队列状态都走生产链路，没有 renderer setState 模拟 running 或 lifecycle events。

本批计划统一跑 25 个闭合场景：

- General 三种合法值/精确中文标签/原 14 px、400 字体/真实 localStorage。
- Main 和 side 各三种 idle 模式，实际 Shift+Enter、硬换行与软换行、原生 composition 不发送。
- Main/side × 三模式 × 两种全局 follow-up，共 12 个真实 running 场景。HTTP 请求被持有时后台真的运行；实际发送字段为 streaming_behavior=steer/follow_up，核对 command.result 与 user_message.queue.updated，不能用内部 busyBehavior 冒充 wire。
- Main 三模式的真实 /compact 键盘 admission，实际 durable ACK、正在压缩/完成、保留真实上传附件及压缩期间的新草稿。
- 三种值分别真正关闭进程再从同一临时 profile 启动，验证设置和 textarea 消费者恢复。

此前的 312 主批与 settings after06/07 不重复运行。本批依 Root 指定的后端默认路径稳定通知才启动，实际 GET feature-flags 的 effective.guardianv2.enabled=false；不能借本批结果宣称新 Async Guardian 启用分支通过。

最终功能结果为 `production-enter-run03` 的 19 个键盘/发送场景通过、`production-enter-cold01` 三种值 cold 通过、`production-enter-compact02` 三种 compact 通过，共 **25 个互不重复的计划场景通过**。旧失败不改写。run03 的 16 张图（含原始 compact 失败）、cold01 三张图和 compact02 六张图均已实际查看。Cold01 原图保留的是 General 上方 viewport，值/label/dataset 的实际断言通过但截图中输入设置尚未滚入；`production-enter-cold-photo01` 又做一次真正 cold 的只读 viewport 滚动照片，使持久化第三选择和完整三项菜单可见，两图已查看，不重跑三次 cold 或 19 场景。

run01 只在 Playwright Response.json 读取 effective 时失败，CDP 报 Network.getResponseBody 无该资源；实验设置已经实际加载，未启动模型。后改为 renderer 内按真实 runtime apiBase/X-MiniCode-Token 只读 GET 同一 endpoint；token 不返回或写入证据。run02 通过 General 与前两个 main 模式，第三模式的实际 wire/admission/model 已到达，但 QA marker regex 不包含 mod-enter 的连字符，使返回标记被截断，完整标记等待失败；只规范 QA 标记为下划线，真实存储 ID mod-enter 不变。两次原始失败均保留，不算完整通过。

run03 General 三项顺序、精确中文 label、localStorage、14 px/400 通过；main/side 六个 idle 场景用真实 CDP composition 与原生键盘验证不误发、Shift 换行、硬换行与视觉软换行。12 个 busy 场景由实际模型 HTTP 请求持有产生 running：Source chord 真正发出 streaming_behavior=steer/follow_up，正确 conversation owner、queue_if_busy、command.result 与 queue.updated/dequeued 通过；global follow-up store 和 localStorage 始终不变。实际上传 artifact 随正确 owner 发送，最终 model request 确实看到新 user follow-up；已接受后重新编辑的草稿保留，side 工作时 main 原稿仍保留。

三次 cold 使用同一明确临时 profile（run03 owner `conv_mv8ablhm_3vl1ei`），不重新 seed settings/data。每种值真正关闭 Electron 后再启动，读取合法 LS 值、精确 label、textarea data-enter-behavior，并等待 COLD_UNSENT_mode 恢复；原真实 artifactId 逐次完全相同，errors=[]、退出 0。不是 renderer reload。

run03 首次 `/compact` 的真实 user_message 已发，后端返回 context.nothing_to_compact 与 native_completion=refused，未请求 model。只读原 owner 的 g76 snapshot 得 history=31（16 user/15 assistant）、frozen=29、compaction_count=0；ledger 历史 estimated4380 tokens。原 profile 没有 compaction_keep_recent_tokens override，当前 config 默认 20000；manual caller 读取真正 current.context_snapshot，非 native custom-chat 路径误用自动 compaction tail 阈值，cut=0 后提前 CompactionNoop。Root 已核 Source core/compact.rs:272 手动 caller 克隆整个当前 history 发 model，确认该行为差异，正在修显式 manual trigger。此失败完整保留，不通过注入历史、降低 QA 阈值或多次重复键盘场景掩盖；待修复统一 checks 后仅补同 owner 三个 compact。

Root 对照 Source core/compact.rs:272 修复显式 manual caller，并统一处理 in-turn compaction 的真实 inflight tool call 对象保留。Root 自己的 50 个 lifecycle/native/local 场景属于另一范围，不借本桌面结果宣称该整批或新 Async Guardian 验收。

`production-enter-compact01` 的真正 HTTP 请求已 held，wire 已有 conversation.compaction.updated state=compacting；QA 仍等待旧 projection 的 `.status-notice-cell`，所以失败。当前生产组件实际为 `.context-compaction-cell[data-processing=true]`，cowork 文案正在优化对话，code 文案正在压缩上下文。仅改 QA selector，不把产品改回旧泛化行。原轮 finally 释放 gate 并关闭应用/服务，原记录没有 completed，不能冒称成功；下一轮从该 profile 真正 cold 恢复并启动新真实 operation。

`production-enter-compact02/evidence.json` **3/3、errors=[]、退出0**：复用原 owner `conv_mv8ablhm_3vl1ei` 的真实历史与已上传 artifact，不补造 history，仅把该 QA 自己的 custom model URL 接至新的本地受控端口。三种模式均实际发送 `/compact`，收到 durable ACK，输入清除，模型 `POST /v1/chat/completions` 被持有，当前 Source processing cell 实际存在；释放模型后均收到 native_completion=completed 并有 Source 完成行。三个新草稿分别为 COMPACT_NEW_UNSENT_enter / cmdIfMultiline / mod-enter，artifact `art_82fd81e9fe9a476b8e398a9f7edab954` 始终 ready。没有重跑 19 或 cold3。

查看 compact02 六图时，processing 名称的原始照片仍保留用户阅读位置，viewport 显示上一条“上下文已压缩”；实际 processing DOM/HTTP/收据通过并不代表它在图内。没有把这些原图描述成 active 文案照片。

最终 `production-enter-compact-photo01/evidence.json` 的单次可视照片 companion 已通过（1/1、errors=[]、退出 0），只复核 cmdIfMultiline，不重跑三个 compact 或 19 个键盘场景。真正发送 `/compact` 后，受控模型 HTTP 在 1792033088812 held，将新 `.context-compaction-cell[data-processing=true]` 滚入 viewport，`compact-running-cmdIfMultiline.png` 清楚显示“正在优化对话”；1792033089552 released 后，同一个新 cell 的 `compact-completed-cmdIfMultiline.png` 清楚显示“上下文已压缩”。两张原图均已实际查看，均位于 `output/playwright/codex-control-audit-20261013/production-enter-compact-photo01/`。新草稿 COMPACT_NEW_UNSENT_cmdIfMultiline 和原 artifact 继续保留。照片 companion 与已通过功能范围重叠，不计入新增计划场景。

这 25 场景实际 frontend bundle 为 `index-B5o7lS_j.js`，index SHA256 `0df90ae743a199b216dda27fc72dfa2c2f3a9c0dd464bd32e437799dd30db02e`。run03/cold01/compact02 保存开始/结束关键 backend hashes；compact02 八个被测文件起止一致，context.py SHA256 `d6e29c3e6a9d06935dee4fdbb6756c19027fa9553f195c8abe5e731562c58476`、compaction_coordinator.py `c7448227ca5b9419b3778e3c3e3a76efc9fcf63dc056a29b41417ecb384c302e`。普通用户窗口、profile、剪贴板和文件未操作。按用户最新范围不做官方账号，本批模型仅为明确的本地受控 HTTP；不拿它冒称供应商实测。

最终可视 companion 的开始/结束 index SHA256 也均为 `0df90ae743a199b216dda27fc72dfa2c2f3a9c0dd464bd32e437799dd30db02e`，实际仍加载 `index-B5o7lS_j.js`。Root 随后发布的 pin 构建 `index-BxpzT6X2.js` 不借本批结果宣称已验证。本轮所有 QA Electron、受控 HTTP 服务和执行进程均已退出；dist 已释放给后续构建。结论限定为这 25 个计划场景及两组可视照片通过，不代表整个 MiniCode 已与 Codex 等价，也不涵盖官方账号或启用 Async Guardian v2 分支。
