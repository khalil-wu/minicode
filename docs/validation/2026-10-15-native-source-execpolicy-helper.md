# 固定 Source 执行策略原生引擎

本记录只覆盖独立 native helper；本页的 helper 测试不能作为 MiniCode
工具执行、审批、模型权限提示词或桌面打包消费链已完成的独立证据。

后续完整消费链已落地，独立 backend、桌面与已安装包证据见
[`2026-10-15-native-source-execpolicy-consumer.md`](2026-10-15-native-source-execpolicy-consumer.md)。
本页保留 helper 阶段原始验证边界，不能代替消费链报告。

## Source 与编译边界

- 固定 `openai/codex` commit：`822e58cc3d666166c7446c5b1ea2e52f5d09594c`。
- 项目：`desktop/native-execpolicy/`，Rust 1.95.0，Starlark 0.14.2。
- `source-manifest.json` 记录 35 份未修改 Source 文件及原始 SHA256：execpolicy、
  absolute-path、bash/PowerShell/shell_detect/危险命令分类，以及原始
  `config/src/requirements_exec_policy.rs`。
- `src/source_requirement.rs` 明确记录 core 算法适配；输入只替换进程内类型和
  已解析的 host 权限事实，危险命令和 shell AST 算法来自未修改的 Source。
- 原始 Source 的 amendment 提议、禁止宽泛前缀建议及 `format_allow_prefixes`
  排序、JSON 转义、截断均保留。Windows x64 使用静态 MSVC CRT。
- 构建仅读取 checked-in vendor 与 `Cargo.lock`，运行时不读取 `.tmp/codex-src`，
  不需要 Rust、Cargo 或 PowerShell AST 子进程。PowerShell 子进程是测试 oracle。
- 当前 lock 中与固定 Source 同名的依赖版本全部一致。

## 已实现的 native 接口

- `identity`：真实编译二进制返回 Source commit、Starlark 和协议版本。
- `inspect`：真实 Starlark 解析、模型 Allow PrefixRule 过滤、可用前缀与原始
  Source 格式化文本；保留 Prompt、Forbidden、network、host executable 规则。
- `evaluate`：低层原生匹配测试入口，明确由调用者传入 unmatched 决策。
- `command_requirement`：实际 Source bash/PowerShell 解析、动态语法保持 opaque、
  shell 可执行文件身份、危险命令、Never/Granular/Windows sandbox 分支，产出
  Skip/NeedsApproval/Forbidden。普通安全 unmatched 命令保留 Source Skip。
- 模型过滤之后才合并 environment overlay；提示词使用 filtered base。
- 原始 requirements `[rules]` producer 禁止 Allow，处理 token/any_of 和真实错误。
- `approved_prefixes` 表示已成功持久化的内存 amendment，以 Source 的
  `Policy::add_prefix_rule` 与 already-allowed 判断应用；不会重读无关外部修改。
- `append_allow_prefix` 使用原始 Source advisory file lock、写入和去重算法。

## 真实验证记录

`output/native-execpolicy-20261015-shell-batch01.log`：统一编译和测试 exit 0，
共 174 通过、0 failed、0 ignored：Source execpolicy 34、absolute-path 22、
Source shell 90、低层 bridge 14、command requirement 14。

新增 requirements 与内存 prefix 后，仅重跑受影响的 bridge 范围；结果以
`output/native-execpolicy-20261015-state-batch01.log` 为准：exit 0，34 通过、
0 failed、0 ignored，其中原 bridge 14、command requirement 14、新 state 6。
未修改 Source suite 保留上一批真实结果，当前去重 180 通过、0 failed、
0 ignored。逐节点记录为 `output/native-execpolicy-20261015-current-results.json`。

`desktop/scripts/prepare-execpolicy-runtime.py` 的真实 release 构建/stage exit 0，
产物为独立 `output/native-execpolicy-runtime-stage-20261015/`，含实际 exe、
Source manifest/LICENSE/NOTICE、第三方 license 文本及 `runtime.json`。实际
exe 为 10,413,056 bytes，SHA256 为
`82448962cf8c535b126defa5c20791a9ae0ca92af0dffe492096b47754c21ecc`。

`verify-execpolicy-runtime.py` 把这些真实文件复制到独立 cold 目录，PATH
仅保留系统目录、移除 Rust/Cargo 环境后运行：9 场景全部通过，包括实际
Starlark、shell 分解、普通安全 Skip、ignore_rules、危险命令 Never、原始
文件/行号报错、真实落盘 amendment 的冷读取。结果为
`output/native-execpolicy-20261015-release-cold.json`。dumpbin 核实静态 CRT，
没有 VCRUNTIME/MSVCP 动态依赖。首次验证脚本在进程启动前因 Windows 环境
变量大小写键访问报错，保留 batch01；改用实际 SYSTEMROOT 后 batch02 exit 0。
尚未计为实际 wheel/桌面 resource 通过。

原始依赖准备失败日志保留：离线完整重生成遇到 Source 已锁定的 yanked spin；
随后保留原 lock 增量解析，新增依赖实际 fetch。tree-sitter-language 固定回
Source 0.1.7；不换 Starlark，不用 Python/regex 模仿 `.rules`。

## helper 阶段移交的消费链范围

1. trusted config layer 的 `rules/*.rules` 低到高载入与会话内策略 owner。
2. captured model 的 ignore_rules / cyber 判定及 managed requirements producer。
3. 最终可执行参数冻结后，在同一 digest 上连接 native requirement、真实审批、
   sandbox bypass 和执行；替换旧字符串命令分类，保留非 shell 能力边界。
4. 审批 accepted amendment 先持久化、再更新原策略 owner，写失败保留真实证据。
5. 模型 permission instructions / compact 增量读取同一 filtered base policy。
6. release 二进制 stage、实际 wheel 与桌面 resource 冷启动验证。

上述接入的后续状态及独立验收记录已转入消费链报告；不再将这份历史移交清单
当作当前未完成项。

共享 WS/manager/agent_runner 冻结期间本批没有修改它们。terminal
`source_event_seq` 可选；不因为字段缺失创造新的 mandatory contract。
