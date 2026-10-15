# CI Root 尾段修复

本批针对原先被取消 fixture 挡住、尚未执行的 Root 尾段，不代表整个产品达到 Codex 等价。官方账号仍排除；CI 全通过前继续优先处理实际失败，不提前交付 heartbeat/remote executor/Memory WS 的剩余实现。

此前仅收集 `tests/`，以旧完整运行的精确大写终态作差集，取得 Runtime architecture 后的 40 文件/675 项。一次 baseline 执行到 `test_ws_terminal_fallback.py` 的倒数 5 项时挂起；60 秒 faulthandler 与独立有界 await 链诊断均保留。确认后停止旧运行，没有重跑前面 670 项。已完成 outcome 为 621 passed / 24 failed / 25 existing skipped；中止未产生完整 XML，见 `output/ci-repair-20261015/root-tail-before-summary.json`。24 个失败另一起获取完整 trace，结果 24 failed，见 `root-tail-failures-before.xml`。

## 实际契约

- 本轮 14 个修改文件均为测试。取消与 terminal fallback 使用真实 QueryEngine/AnswerCommitter、实际任务清理 owner 和唯一 done/idle 断言；fixture 明确执行目录与 Full access，并隔离不属于 terminal 契约的 memory producer，没有提高产品超时或吞异常。
- terminal create/exec 是真实执行边界，projectless 调用通过原 `SessionLifecycle.bind_execution_for_operation` 固定 cwd 与归属；客户端路径本身不提供授权。挂载失败、其它命令的拒绝及归属边界保留。
- `/review` 使用隔离的真实 Git 工作区和实际创建的 conversation ID，精确核 focus 与 review scope。模型未声明 native search 能力时，MCP 工具直接可见；天气/疑问句不改变工具能力。MCP readonly hint、风险审批与 destructive 标记分别按 Source 核对。
- 子 agent 的 QueueOnly 消息真实入 mailbox 并保持 epoch，不冒充新 turn；FollowupTask 才以 TriggerTurn 启动第二回合，原 ID、checkpoint、start/done 继续断言。
- 命令测试走实际 native 冻结与审批链，managed 禁止 unsandboxed 时在任何进程启动前拒绝。工作目录可在 workspace 外，权限由原捕获的 filesystem profile 决定；测试明确使用 ROOT read/workspace write，避免把系统 Temp 默认写授权误判为 cwd 授权。
- 原始 blocked/error/stream 内容与工具 ID 保留，UI 摘要核操作名；elicitation 的 required/NotRequired 契约与实际 wire 对齐。

## 分区验收

各分区不与以前重叠批次累计；首轮失败保留，未把窄补验伪称为整批一次通过。

| 范围 | 最新证据 | 边界 |
| --- | --- | --- |
| 6 个 WS/workspace/API 文件 | 85 个当前节点均有最终 pass，0 skip | 宽组 82 passed / 3 failed，定点 3/3；`root-tail-desktop-delivery-20261015/manifest.json` 含逐节点映射与原字节 |
| 5 个 tool/model/protocol 文件 | 155 个当前节点均有最终 pass，0 skip | 宽组 154 passed / 1 fixture failure，slash 文件 18/18；`source-root-tail05-delivery.json` |
| 3 个 sandbox/subagent 文件 | 当前无失败；3 个既有 OS sandbox skip 未验证 | 宽组 44 passed / 1 错误状态断言 / 3 skip，修正后的完整审批执行链 1/1；`root-tail-harness-frozen-files.json`、`2026-10-15-root-tail-harness-contracts.md` |

表中未带前缀的证据均位于 `output/ci-repair-20261015/`。所有修改通过 `git diff --check`。这些结果不能替代最新 HEAD 的 GitHub 全量结果。

GitHub `3b184a29` 已实际通过前端、Linux native build、Windows harness、Electron integration、Desktop unit/smoke 五项。Linux backend 6517 项为 6487 passed / 3 failed / 27 skipped，剩 3 个事件循环关闭错误正在追真实订阅 owner 的发源 fixture；没有在 production 增加 closed-loop fallback。Root job 与之后推送的整体结果仍以 GitHub 实际记录为准。
