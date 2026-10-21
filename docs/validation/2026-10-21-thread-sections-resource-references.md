# 会话分组与持久资源关联

本批依据公开 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 和已提取 Desktop `26.1013.4894.0` 消费代码，按两个功能组记录，不按 API、工具或测试数量计算产品完成率。

## 会话分组

`ThreadSectionStore` 使用稳定 UUIDv7 与当前 conversation repository 保存分组、混排的项目/聊天 membership 和顺序。侧栏接入 Source 已证实的名称弹窗、移动到分组子菜单、当前分组选中后移出、新建聊天、编辑、全部标记为已读、归档聊天、新建分组和移除。

项目移入分组后复用原项目的聊天排序、展开/收起与拖动消费者；不会显示另一套项目行。选中行和数字快捷导航使用实际可见顺序。分组批量动作包含所属项目中当前真实的聊天，而不只处理直接挂在分组下的聊天。归档保留位置，恢复后回到原槽；固定移出自定义分组，删除聊天去除 membership；fork 复制分组位置。删除分组只返回未分组列表，保留聊天与草稿。

分组外观参数保留公开协议的 UTF-8 64 字节约束，但没有添加 Source 桌面菜单未证实的外观按钮。HTTP 初始目录读取不会覆盖随后收到的已提交 WebSocket 投影；写入后复用现有 conversation inventory 广播。

Source 依据：`app-server/src/request_processors/thread_sections.rs`、`thread_processor.rs::thread_section_move`；Desktop `app-initial-5851df3f6cb1.js` 的 `OOc`、`s2`、`R9n`、custom-section header consumer（约 9990244、9994643、3652497、11189700）。

## 资源关联

`ThreadResourceReferences` 将资源与上传附件、聊天历史分开保存。`attachment_type + identity_key` 在原会话内唯一；重复添加返回原记录；反查包含没有消息的会话并可按归档状态过滤。列表使用稳定 keyset 顺序，游标绑定原会话或原资源/归档查询，不能跨查询沿用。引用上限、UTF-8 身份字段和 JSON payload 界限来自 Source；移除立即释放容量。

新增真实 `list_artifacts`、`attach_artifact`、`remove_artifact` 消费者。创建 PR 的 Git delivery 成功回执也关联到原请求会话。GitHub、Enterprise 和 GitLab 多层 namespace 使用原 host/project/number 身份；存储规范 URL。清空历史、归档和冷读保留引用；fork 创建新的 attachment ID；永久删除会话后不再进入反查。

普通 Git、hook、恢复的 worktree 和 UI worktree binding 接入相同引用。实际移除/解绑去除活动引用，已有快照保存真实恢复记录。引用本身不会为 fork 授予独占 checkout 删除权限。

Source 依据：`app-server/src/request_processors/thread_attachments.rs`；`state/src/runtime/thread_attachments.rs`；Desktop `HE`、`iXn`、`CC/kC`（约 3425416、3446550、3041701）。

## PR 状态消费者

侧栏使用 Source 已证实的 PR icon badge 和悬浮信息，未加入额外资源面板。状态请求只接受原会话当前已关联的 resource ID，显式查询保存的 URL，不从另一个选中聊天的 active branch 推断 PR。重复出现的项目/最近聊天行共享同一个可见会话请求；旧 owner 退休后不会投影其迟到结果。真实失败保留为失败，未得到上游状态时不会显示成功图标。

本地请求使用现有 GitHub CLI 配置，远端请求通过原会话 `capture_workspace_execution` 借用原 executor 的 `gh` 或 `glab`，并在结束时关闭借用。GitLab 使用显式 `glab api --hostname <original-host> projects/<encoded-project>/merge_requests/<iid>`；Source 的 `opened/locked → open`、draft、closed、merged 映射均有消费者。没有可用 CLI 或授权时返回真实错误，不创建账号、不隐式登录。

Source 依据：Desktop `pVc/SVc` 和 GitLab `nBc/uBc`（约 10332572、10336862、10303811、10307183）。官方 GitLab CLI/API 文档核对：<https://docs.gitlab.com/cli/api/>、<https://docs.gitlab.com/api/merge_requests/>。

## 验证记录

定向后端现有回归与新增链路共 **52 个不同的当前用例**有最终通过证据；前端 **45 个不同用例**有最终通过证据。这是按最后一次执行合并的证据，不宣称一次重新执行了 52/45 项。原失败和针对性未执行项保留在收据中。

- `output/full-parity-20261021/sections-resources-backend.xml`：首批 46 项，45 通过、1 个真实 URL 解析边界失败。
- `sections-resources-backend-repair.xml`：新模块 19 项通过，包含 URL 修复。
- `sections-resource-bounds.xml` 与 `sections-resource-bounds-repair.xml`：容量测试新增时一次夹具插入位置错误，修正后 2 项通过；失败记录保留。
- `sections-resources-provider-delta.xml`：GitHub/GitLab 原主机查询及失败合同 10 项通过。
- `sections-resources-remote-providers.xml`：两个 provider 的原 executor 借用与关闭通过。
- `sections-resources-frontend.json`：首批 45 项，44 通过、1 个项目名称夹具错误。
- `sections-resources-frontend-repair.json`：失败项修正后通过，另外 3 项为该次定向筛选未执行，不计成新通过。

仅尝试过一次全局 TSC，提前暴露本批漏写的连接符，已修正。最终全局 TSC、构建、bundle budget 和 CI 由主 agent 在全部生产编辑冻结后统一执行。没有进行本机 GUI、computeruse、Playwright、CDP、真实账号状态或全部视觉状态验收；合同测试中的 CLI 上游是受控回执，不能替代外部账号/设备验收。
