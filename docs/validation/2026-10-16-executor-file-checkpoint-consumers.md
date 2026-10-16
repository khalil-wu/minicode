# Executor 文件、Git 与 checkpoint 消费链

本批承接尚未提交的 R2；不属于 `ae278f910148f9c731a0dfaa51453431d66179c7` 的已通过 CI。最终证据索引是 `output/executor-file-chain-r2-delivery-20261016/manifest.json`。没有修改前端字体、会话、附件、dirty editor 或隐藏 workspace 草稿，没有操作项目 Git index/commit/push，也没有运行清理脚本。

## 行为依据和修复范围

Source checkout 为 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`：`core/src/tools/handlers/apply_patch.rs:330` 先选择真实 turn environment，再用其 filesystem/sandbox 验证补丁；`core/src/turn_diff_tracker.rs:25` 用 environment ID 与 PathUri 组成身份，`:373` 仅在多个 display roots 时显示环境前缀；`exec-server/src/server/file_system_handler.rs` 与 filesystem handle 契约提供实际远端读写；`sandboxing/src/manager.rs:310` 在无需平台 sandbox 时首先选择 None。MiniCode 的独立 Git 工具和文件 rewind 是既有产品能力，本批为它们接入该 executor 权限/URI 契约，不将其宣称为 Source 同名工具。

- read/write/edit/apply_patch/list/glob/grep/fuzzy/notebook 和 Git status/diff/log/commit 都在任何 controller Path 操作前进入 executor 消费链。configured empty snapshot 是无可用环境，不进入旧本机调用路径。未装配 executor 的旧调用保留原路径。
- 每次操作只消费已接受的 ToolExecutionContext 附件与 permission scope，克隆该真实 lease。文件 I/O 使用显式 filesystem sandbox；进程使用 Source 初选语义对应的 process sandbox。Git 的已授权 metadata 写入只给本次进程增加已有语义中的精确 metadata grant，不修改普通 filesystem scope。
- 文件 read hash 的 key 包含本进程随机 epoch、真实 registration 编号和 URI 身份；registry 同 ID 替换或冷启动不会命中旧 registration 的读证据。文本与 notebook 编辑保留 CRLF，补丁完成的变更和部分 move 失败均保留真实已写入字节、diff 和 URI 身份。
- checkpoint blob 仍在隔离 store 保存原始字节，record 明确保存 environment ID/cwd URI/各文件 URI；历史 record 不持有高权限 lease。恢复用这一次实际接受的 attachment/current scope，开始后保持本次 lease，不再查 manager。新的 executor record 可从冷 store 恢复；旧无环境 record 仅在本次真实 local 附件与原 workspace 一致时按 URI 恢复，remote 缺少身份时真实报错。
- turn diff 按 environment ID + ExecutorPath 区分同路径文件，结构化文件项保存 environment_id/path_uri/old_path_uri。显示前缀不会再被当成相对文件路径解析。
- compaction 文件恢复通过本次 snapshot 中真实 registration 匹配的观察 URI 获取文本，沿原文件数/token 限额生成编号片段，不用本机 Path 读取，也不会从片段恢复自动授予 write-safe read hash。ContextBuilder 生命周期和 bootstrap 共享由同批 harness/Root 接入，另有该调用层的验证。

搜索使用远端 filesystem 的 walk URI，而不把 readDirectory 的显示文件名重新编码为文件身份；gitignore、分页、内容/文件名/count 模式、glob 和常见扩展类型沿现有文件搜索语义。仅 ripgrep 原生支持、扩展表中没有的 type 会真实提示改用 glob，不悄悄扩大为全类型搜索。没有新增宿主搜索 fallback。

## 验证与初败

统一使用 bundled Python 3.11.9 和现有 `pytest-python311.py`，Source peer 为安装版 `OpenAI.Codex_26.1007.9446.0` 的真实 `codex.exe exec-server --listen stdio --concurrent-requests 4`。每个 fixture 使用自己的 Temp cwd/CODEX_HOME，不发送模型请求或账号授权。

| 批次 | 结果 | 结论 |
| --- | --- | --- |
| first | 108 pass / 26 setup error / 0 skip | 新 fixture 缺 ReadFileTool 必需的 artifact_store 参数，尚未进入这些 Source/local 场景；setup 失败还暴露未关闭 RPC/pipe 警告。原输出保留，不计整批通过。其中 105 项为既有文件/补丁/checkpoint 回归，另含 tracker 1 项和 admission 2 项。 |
| second | 28 pass / 9 fail / 0 skip | grep 测试误把默认 files_with_matches 期望为 content；Source 进程把 Disabled profile 的 FS sandbox context 强送 restricted-token，得到真实拒绝；admission 还暴露默认 ask 规则与 Never 的测试配置冲突、empty 结果存储再次选环境。没有把 Source 拒绝标为安装版限制。 |
| search-legacy | 4 pass / 0 fail/error/skip | glob/grep/fuzzy/list 与旧 checkpoint 当前 local scope 恢复在 local/Source 逐项窄验。 |
| compaction | 2 pass / 0 fail/error/skip | local/Source 实际 URI 内容恢复，旧 registration 不读取，不重新授予 read hash。 |
| final | 23 pass / 2 fail / 0 skip | 文件链 15 项和 committed turn diff 8 项通过；2 fail 仅 empty 测试重复注册 fixture 已有的 read_file。 |
| empty-final | 2 pass / 0 fail/error/skip | 测试改用 fixture 中已有 read/write，真实 empty snapshot 的 read/write/list/Git 全部在 host I/O 前被拒绝。 |

最新唯一范围为 **test_executor_file_chain.py 17/17** 与 **test_committed_turn_changes.py 8/8**，没有跳过或最终 warning；这是 final 批 23 项加两项修复后的窄复验，不宣称重跑了一次全 25 项。上述生产代码在该 final 批和 empty-final 窄复验之间未变；只改了重复注册的测试步骤。Root 的 admission 后续批单独保存，不与本批相加。

Source Git 已实际执行隔离仓库的 init/config/add/commit/status/diff/log。补丁测试实际验证 move/add、CRLF、部分 move 失败后的两个真实文件、冷 checkpoint store、当前 readonly scope 拒绝恢复后再按可写 scope 恢复。最终 manifest 冻结本批文件身份及各 XML/log 的真实计数；初败日志保留，不用通过结论覆盖。
