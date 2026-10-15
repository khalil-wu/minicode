# Guardian、权限与终端的真实桌面衔接

参考 checkout 为 `.tmp/codex-src`，HEAD `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。视觉使用既有 Codex 截图和 `codex-open-source` 中固定 ASAR 提取件。没有根据旧 MiniCode 图片判断当前实现，没有改字体或字号。

## 本轮修到执行层的根因

- 普通非零命令退出属于已完成执行，保留实际 exit code / stderr / process status；不进入工具错误预算。原 Bash hook 在命令实际完成时执行一次，stdin / poll 不另造 Bash completion。
- Guardian private runtime 把实际选定的 model / provider 交给 `create_session_llm`。之前静态 model fixture 遮住了真实 ModelRuntime 启动失败，桌面 run01 抓到后修复，没有给 factory 添加缺模型兜底。
- `TurnKernel` 默认使用 issuing run 的真实 turn ID；显式 host turn 从 start event、EventEnvelope、provider lineage 和 frontend runtime binding 保持一致。assistant message ID 只负责消息投影。此前 Guardian wire 到达但被前端正确的 turn gate 丢弃，run02 抓到后修 producer，未放宽 frontend gate。
- 原用户输入在真实 admission 后成为 retained evidence；排队输入和 verified answers 在接受时保留来源顺序。cold startup 在绑定 builder 前从已保存 evidence seed 保留顺序。模型消息按 stream start 预留顺序，工具原文进入独立 review history。
- clear / rollback 单调更新授权和 reset；cold root 使用新的 process-unique review-context revision；普通 WS 下一回合借用同一 owner / reviewer trunk。worker 仅在 Source V2 接收原 root evidence，嵌套 worker 不把中间委派变成 root 用户授权。
- `thread/approveGuardianDeniedAction` 使用真实 denial ledger，先 candidate snapshot / CAS commit，再写 live developer evidence。按钮和 `/approve` 只记录授权；后续真实用户重试仍经过审核。失败、取消、cold owner 与重复回复使用同一条持久化链。
- 待审 patch 的路径来自 frozen action.files。文件和 review 两层独立折叠，approved 仅移除 review，不重置用户展开选择。不存在 diff 时不显示假 `+0/-0`，auto-review 不冒充 human 等待批准。

## 实际验证

`guardian-authority-desktop-qa.cjs` 启动独立 offscreen Electron profile，使用当前 `frontend/dist`、实际 WebSocket / outbox / repo / 文件 I/O。只控制模型 HTTP；bootstrap 使用 production createConversation action，后续发送、文件展开、`/approve` 选择都操作真实 UI。用户正常窗口、草稿、历史与未保存编辑内容没有用于 fixture。

| 场景 | 验证的事实 |
| --- | --- |
| automatic review → patch | 真 private QueryEngine 请求使用 `codex_output_schema` / strict=false；无假子 agent start 或人类 approval request；文件确实写入，未发送草稿保留 |
| deny → original file unchanged | 真 denial 原理由和 tool result 保存，未执行 patch |
| `/approve` → recorded only | 源事件选择菜单；未发送新 user_message、未启动新 turn、未写文件 |
| explicit retry → new review → patch | 新审核收到真实 developer 授权原文，审核完成后同一 patch 才写入 |

run03 / run04 四场景均通过，page errors 为零；run04 的七张 PNG 等有限展开动画结束后采集，仍保留正常扫描动效。证据在 `output/playwright/codex-control-audit-20261013/production-guardian-run04/`。run01 / run02 失败文件保留，不算通过。

Python 的 stdin / command 批次、Guardian 原始证据 / private harness / WS owner / 手动持久化链、前端协议 / controls / 真实 ChatTurn DOM / RuntimeEvents 分别统一验证。每轮 log / XML 保留，旧 fixture 缺契约、编译错误和修复后的定向复验分开记录，计数按 testcase 最新结果去重。最新 TypeScript 与 Vite 构建成功；旧大 bundle / dynamic import / Browserslist 警告保留。

## 边界

这份证据不代表已经复刻 Codex 全部功能。没有以受控模型证明真实模型风险判断质量。QA profile 缺少 Windows native sandbox account state，实际 git prompt probe 有 unavailable warning；这些 patch 场景不等于受限 ConPTY / Guardian shell 的 OS readonly 实测。外部 OAuth、真实 MCP / remote environment、network-approval producer、ThreadOwned reviewer checkpoints、Guardian V2 async 与 Notes encrypted backend 另列，不能算本批通过。

模型 catalog / auth 和 compact hooks 已有独立审计；model-owned token-budget 当前还在接运行 consumer，未完成的链不提前算通过。
