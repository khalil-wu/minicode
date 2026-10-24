# 继续对齐与中间产物清理

承接 2026-10-24 已看/未看记录及逐组实施账本。用户要求清理中间产物并继续对齐。本次继续使用已保存的桌面操作证据和 `.tmp/codex-src`，没有继续操作界面。

## R09：本地压缩输入

Source `codex-rs/core/src/context_manager/history.rs` 在 history admission 时应用模型/工具的截断策略；`core/src/compact.rs` 克隆该 history 并直接准备 compaction prompt。Mini 的 `format_compaction_history` 原来又对每个工具参数及结果固定裁剪到 500 tokens，导致已经接收的证据在摘要输入中再次丢失。

删除这两处额外裁剪，保留既有历史选择、用户输入保留、summary 输出限制、加密/native window 路由及 artifact 引用。新增自动/手动 compaction 的真实 ContextBuilder 用例，验证超过 500 tokens 的参数、Unicode 错误详情、call ID、错误状态进入实际 summary request，压缩后用户约束仍可冷恢复。代码和 fixture 已补，统一验证结果随后追加。

## FF-GOALS：前端消费者

使用既有 `capabilityFeatureEnabled` 消费当前 scope 的 `goals`。关闭时不提供添加目标、GoalBar 或编辑面板；直接进入/切换目标编辑模式及目标提交入口不再激活。已存目标、输入文字、附件、各 owner 的编辑草稿保留，重新启用后可恢复。Composer 在关闭期间按普通输入消费 dormant goalMode；切 Plan 不再额外发目标清除命令。目标命令的实际 scope 裁决由后端负责，避免使用新会话的 flags 误判原 owner 已开始的操作。

后端工具、命令、注入与 continuation 门控及 scope 刷新由同批 Runtime 分工补齐；SidebarRight 的入口/呈现由 Desktop 分工补齐。新增前端用例验证禁用、启用回显、保留草稿、Plan 和真实普通消息发送。统一验证结果随后追加。

## 中间产物清理状态

已核对的重复构建为 `output/button-perf-20261024/baseline-dist`，93607049 bytes，内部未发现 reparse point。另选定根 `.pytest_cache` 和 `__pycache__`；当前 `frontend/dist`、依赖、参考 checkout、审计截图/AX/日志及用户运行数据保留。

两次删除请求均在执行前被自动审批拒绝：一次为经过工作区路径检查的批量缓存清理，一次缩小为上述三个固定绝对路径的 `Remove-Item -LiteralPath ... -Recurse -Force`。返回原因只有 **blocked by policy**，没有更具体说明。没有改用其他删除方式绕过拒绝；本次未删除文件，清理未完成。保留该事实，不能将已枚举或可重建的大小写成已释放空间。

## Root 统一验证补记

R09 的自动/手动 compaction 用例在首轮 core 批次中通过。Goal 首轮定位到菜单行仅禁用未隐藏，现已分离 feature 显示门与 pending-switch 暂不可用状态；后续聚焦顺序 fixture 保留实际产品行为并通过全部 14 项。Composer 普通发送及草稿/附件/owner 相关文件在区域批次通过，搜索 hook 最后 5 项通过。

配套后端 Runtime gates、Hooks、正文搜索和 direct async journal 修复的逐批失败、修正及最终回归见 checkpoint。最终 TypeScript/生产构建、bundle/UI debt、diff 检查通过，所有重叠数量不相加为完成度。清理仍为被自动审批拒绝、未删除；未做修改后完整 GUI 或真实 provider 验收。
