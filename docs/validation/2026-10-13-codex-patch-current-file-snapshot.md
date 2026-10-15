# Patch 当前文件与实际审批快照

依据 `.tmp/codex-src` commit `822e58cc`：`codex-rs/apply-patch/src/invocation.rs:180` 的 `try_verify_apply_patch_args` 对 Delete 调用真实 `fs.read_file_text`，Update 进入 `file_update.rs:25` 的 `derive_new_contents_from_chunks`，工具自身读取当前文件并匹配上下文。此链不要求模型此前调用某个 typed read 工具，不使用模型读取历史 hash 作为补丁准入条件。

MiniCode 原来在 `ApplyPatchTool.execute` 中强制所有已有目标必须出现在 `_read_file_hashes`，并把上次模型读取 hash 用作审批和提交快照。即使补丁上下文匹配当前文件且可以保留后来的其他改动，也会被这个 CC 来源条件拦截。

现在补丁仍建议先了解文件，但执行器直接用实际规划读取的完整内容匹配和生成 diff。没有先读或上次读取已经过时，都不再单独阻止上下文有效的补丁；未匹配的 hunk 仍返回真实错误。增加、删除、移动、实际部分提交、路径策略、恢复点、缓存与精确 diff 继续走现有链，没有虚构模型读取事件或隐藏报错。

审批保留完整真实快照：`build_apply_patch_diff_payload` 从同一 `_ChangePlan.old_content` 构造源 hash 和真实缺失目标。`_detached_tool_diff` 把此内部快照绑定到运行中的同一工具调用；最终执行只在私有 execution args 中传递它，不修改模型参数、冻结请求或审计 digest，也不接受模型伪造的 `_expected_hashes`。因此模型没有先读也能接受真实补丁审批，用户在等待审批期间的磁盘改动仍会明确拒绝本次旧 diff，不被覆盖。已有 canonical 参数变化重授权链继续生效。

验证整批覆盖 parser/patch lifecycle、直接未读与过时读取、实际 ToolBatchRunner 审批、审批期间改动保留、native freeform 协议及 query 的精确提交 diff，原四文件69项范围最终通过。首轮67/69通过：两个新fixture错把审批callback的实际call id当对象；run02修正后一个通过，另一个误读公共tool_result不存在的content字段。生产错误已经按真实developer_detail保留，修正fixture按实际wire字段核对，没有改变生产逻辑、补兼容字段或裁剪错误。原失败日志/XML保留。

run03只重验这两个审批用例并运行15项现有冻结请求授权用例，17/17通过、退出0；模型私有参数不会成为执行快照，审批期间有效参数变更仍需重授权。四文件范围与此处两个重跑项不相加，当前这批为69项加15项冻结请求范围。全部证据位于 `output/playwright/codex-control-audit-20261013/root-source-patch-run01–03/`，与历史整批存在重叠，不称新增84项测试。

这项修复只对齐当前文件匹配和实际审批快照；不代表所有 patch parser 边界、受保护路径策略、shell patch interception、remote 文件系统已经与 Source 完全一致。用户正常工作区、草稿、未保存编辑器及历史未被验证过程操作。
