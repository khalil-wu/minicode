# Native wheel host packaging contract — 2026-10-16

## r5 正式 Windows 包

Linux native-network 的真实 target-only cache 初始化根因修复后，全部输入重新冻结。r5 从新的独立树构建，在新的私有 Temp target 完整重验：**26 pass / 0 fail**，没有复用旧包的通过记录。当前 `manifest.json` 为 `changed_inputs=[]`、`candidate_only=false`。相对 r4，1247 项冻结输入仅 `desktop/scripts/prepare-network-runtime.py` 改变；后端、原生资源及 setup.py 字节未变。新增 4 个 root 构建缓存回归由测试总账单列。

| 项目 | 最终记录 |
| --- | --- |
| wheel | `output/source-executor-remote-final-wheel-r5-20261016/minicode-0.2.0-py3-none-win_amd64.whl` |
| SHA-256 | `2843f8df9dc1699bb67dafa3e5eeb3166dfa697c8fcdf08797de0a308fea722c` |
| 大小／标签 | 15,994,583 bytes；`py3-none-win_amd64`，正常构建，无手动 retag |
| 冻结输入／backend 成员 | 1247／1228 |
| 新安装根目录 | `C:/Users/ago/AppData/Local/Temp/minicode-executor-remote-final-ed0wyv6o/installed` |
| 原始证据 | 同一 r5 目录的 `build.log`、`install.log`、`installed-cold.log`、`installed-cold.json` |
| 一致性证据 | 同一 r5 目录的 `inputs.json`、`manifest.json`、`final-consistency.json` |

`final-consistency.json` 确认当前工作区输入成员集合与冻结清单完全一致，1247 项冻结快照字节均一致，1228 个 backend wheel 成员均与冻结源码、独立安装后的字节一致。全部 backend Python 源文件已分发。12 个现有非包素材没有纳入 wheel：环境及 eval Markdown、`backend/sandbox/Dockerfile`、测试用 `checkpoint_schema4.json`；完整清单在一致性证据中，没有误称这些非包素材也已分发。

最终输入还精确验证三个生产文件身份：

| 输入 | SHA-256 |
| --- | --- |
| `setup.py` | `9384b01b51247a96c437566cac6fa21c654dd1c959d09c6f70b274e1d922d0e0` |
| `backend/agent/tool_environments.py` | `1f98255749b445ed2ad2bb2bf4a5b340cd5dcf28ec4cb090fc71f1ec4944bbd3` |
| `backend/mcp/environment_runtime.py` | `6c446f0bddff6a4d4e7ba98bbc35f9a3bab53ba922fb1a1479e9462033403052` |

独立冷验使用 bundled Python `-I`，cwd 与 state 均在新的私有 Temp 目录，PATH 限为 Windows System32，依赖复用已安装的 bundled Python 运行库。26 项检查覆盖安装字节与标签、Source 执行策略和提示词、真实 PowerShell 审批及持久规则冷重用、`minicode-host` 的真实 CLI help、六个网络资源及 builder/bridge 来源、真实 native URI policy 编译、明确拒绝／既有允许／miss 审批顺序、原 environment/execution 身份、运行期 amendment、Source network rule 持久化冷读取及真实 lease 关闭。

原生 Source 固定为 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。网络 exe SHA-256 仍为 `f14c6d27630f3791bee6e50578e1eee8506bf0e75ad8f971c407287d7402b9fc`，source-manifest SHA-256 仍为 `c1180c18e04cf0513c33eb805022accefa37d83a9152b32d8054b0ac65ebbf63`，分发文本为 LF；本轮没有重编原生二进制。

这份通过结果不包含外部机器、SSH/WSL daemon 或远程 executor HTTP 部署验收；CLI entry/help 不等同这些服务验收。网络策略检查使用 IP literal，不建立 TCP/HTTP 连接，没有放宽 local-binding 策略，没有执行 Windows 账号、ACL、防火墙 provisioning，也没有把先前需要 provisioning 的 WFP skip 算作通过。

## 历史包与 Git 发布字节

r3 仍保留其自身真实通过记录：SHA-256 `3819717f2666831bae801b919f0263fe6afd13d08b8cbf0e8b70e064eacd1671`，15,994,043 bytes，独立 26/26。它没有包含随后两处 CI fixture 修复，不再代表最新 backend 输入；r4 当时与含两处 CI fixture 修复的输入逐字节一致。r4 自身真实记录继续保留：SHA-256 `085d18b5431a1ef074d231eb9e1750d33afbded2c470ae8ee180a4608df468b7`，15,994,583 bytes，独立 26/26。随后 builder 修复改变其冻结输入；当前输入由 r5 承接。

旧 CI 的 Windows execution-policy 资源哈希失败定位于 Git clean 的 LF 规范化：二进制未变，五个文本 blob 精确等于原 producer 字节 CRLF→LF 的结果。本机 producer 全部五个 `manifest.files` SHA-256 仍正确。完整 Windows 发布 bundle 使用 `-text` 保存原始 Git blob，不修改 producer、manifest、测试校验或运行时策略，也不只依赖 checkout 的 CRLF 转换。`native-execpolicy-byte-preservation-independent.json` 保存独立前后 Git blob 审计，`native-execpolicy-byte-preservation-r3-recheck.log` 保留修复规则时 r3 输入仍一致的时点证据；后续测试字节变化由 r4 承接，builder 字节变化再由 r5 承接。

Linux native-network 的首次真实编译已在旧 run `38868950710` 的 job `116868228925` 成功，release 编译 8m22s，Linux 实际原生身份检查 Source commit 精确匹配。下载的六文件 artifact 全部清单哈希通过，ELF64 x86_64，builder/bridge 字节与冻结源码一致；报告为 `output/ci-repair-20261015/native-network-linux-ci-38868950710-report.json`。这证明实际 Linux 原生构建，不能扩大为整个旧 CI、Linux backend 或 Linux wheel 安装已通过。Linux 合法 Attribution TCP 认证测试分支仍须由新 CI 实际证明，未加入 skip 或放宽认证。

## 缓存初始化修复与真实 producer 来源

新 run `38872672167` 的网络 job `116880223891` 在 Cargo 前真实失败：CI 只恢复 `source/codex-rs/target`，由此产生的父目录使旧 `source.exists()` 为真，但 `source-identity.json` 从未恢复，随后读取失败。完整原始日志与元数据保留在 `output/ci-repair-20261015/native-network-linux-ci-38872672167-job116880223891.*`。同一 run 的 execution-policy Linux job `116880223983` 真实 release 与原生 identity 已成功；不把这次整体 run 或 network job 算作通过。

builder 现在由真实 Source identity 判断初始化状态：无 identity 时仍对精确 `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 运行 `git archive` 并解压，保留 target 缓存，解压成功后才写身份；已有错误 pin 继续拒绝。没有 catch 文件不存在，没有伪造 identity、吞错、清缓存或放宽 Source pin。`tests/test_network_runtime_build_cache.py` 新增 4 项，和原生包装 8 项集中 **12/12 pass**；实际固定 Git object 的 target-only 归档、缓存保留与初始化复用也通过，证据 `verify-network-cache-source-init-actual.json`。该本机归档验证没有执行 Cargo 或重编 native；修复后的完整 Linux cache-hit 构建由新 CI 验证。

当前 builder SHA-256 为 `a518b01a4da8e965576de30c9fa79638fb4a856763274f4451fdd96e2087dd7a`。现有 Windows native 仍来自真实旧 producer `66e8b471f483cb04148660e32d09d76be387409a322c68e183420979a91e78cd`，资源 `source-manifest.json.build_script_sha256` 保持原值。r5 在 `retained-network-producer.py` 归档 r4 冻结树中的实际旧脚本，SHA 精确等于这个真实 producer。冷验严格核 installed manifest 的 producer SHA 等于归档旧脚本字节，同时核当前冻结 builder 为新的 a518 身份；bridge/source/native 哈希继续逐项严格校验。没有修改历史资源 metadata 来冒充由新脚本构建，也没有为本次修复重编 Windows 二进制。

## 修复与集中验证

`setup.py` 现在按真实 `sysconfig` 主机平台选择资源目录：

| Python platform | 原生资源 target | wheel tag |
| --- | --- | --- |
| `win_amd64` | `x86_64-pc-windows-msvc` | `py3-none-win_amd64` |
| `linux_x86_64` | `x86_64-unknown-linux-gnu` | `py3-none-linux_x86_64` |

实际非 editable wheel 构建会在复制包文件之前，逐项验证执行策略与网络两个原生资源族的 `runtime.json.target` 和 `files` 哈希。没有导入依赖后端运行时的模块，没有加入其他平台的支持声明或资源兜底。

`output/ci-repair-20261015/native-wheel-platform-contract-batch01.xml` 与同名日志记录集中 **16 pass / 0 skip / 0 fail**：新增 8 个原生包装合同，以及原有 8 个桌面包装合同。新增合同覆盖 Windows/Linux 主机选择与标签、两个资源族的目标不符及字节变动在复制前拒绝、未提供平台拒绝。

Linux 合同使用明确的模拟主机与已知清单/文件字节样本；没有执行 Linux 二进制，不能视为 Linux 编译或 Linux 冷安装通过。只读 Cargo Linux metadata 成功；完整离线依赖解析因本机没有 `addr2line v0.25.1` 缓存而失败。真正 Linux 构建和资源运行由 CI 执行。

当前验证环境的 setuptools `editable_wheel._safely_run` 会将自定义 `build_py.run` 的异常降为 warning。这是依赖本身的行为，未另加包装或吞错。`pip install -e` 成功不能证明原生资源完整性；CI 仍需要实际调用原生资源。正式非 editable wheel 的双资源校验失败会直接终止构建。

测试批次保留原有 `wheel.bdist_wheel` 导入的 FutureWarning，没有将 warning 算作失败。

## Root 测试唯一节点

`output/ci-repair-20261015/root-tests-final-summary.json` 合并原全套运行、失败节点窄验和本次新增合同，现行唯一 **1819 = 1757 pass + 62 skip + 0 fail/error**。本次 16 个节点中的 8 个旧包装节点只更新证据，没有重复计数。新增 4 个 Source build-cache 合同在 12 项批次中实测通过，重复的 8 个包装合同不重复计数；最终 collect-only 1819 与唯一节点集合对齐。该结果不是第二次全套重跑。

## 冻结与数据排除

Windows 最终包必须在生产与 fixture 修改冻结后，从新的版本化目录构建，再安装到新的私有 Temp target，用 bundled Python `-I`、源码目录以外的 cwd 和私有 `MINICODE_STATE_ROOT` 完整冷验。旧候选及其日志保留为历史证据，不能代替新包验证。

冻结脚本只复制 `backend` 源文件及明确列出的构建输入。遍历进入之前排除 `backend/data`、`backend/storage`；不读取或复制 `.env`、`.env.*`、Python bytecode、日志及缓存。根目录 `data/` 不在复制范围内。构建和冷验不打开现有会话、附件、草稿、dirty editor 或用户运行状态。

安装使用 `--no-index --no-deps --no-compile --target`，不改系统 Python、不重编 Windows native、不做账号/ACL/防火墙 provisioning。最终须同时核对冻结输入、wheel 内 backend 字节与独立安装后字节，并要求 `changed_inputs=[]`、`candidate_only=false`。

## r2 候选与新的 harness 验收

r2 已从独立冻结树构建并在新的私有 Temp target 完成独立 **26 pass / 0 fail** 冷验，且当时冻结输入没有差异。wheel 为 `output/source-executor-remote-final-wheel-r2-20261016/minicode-0.2.0-py3-none-win_amd64.whl`，SHA-256 `ac2417099466000fd78a61516d1061ee9944c9efe6bf41ffc3a0d3a2b8ad9d23`，15,993,118 bytes，1247 个冻结输入及 1228 个 backend 包成员。所有 backend Python 源文件已分发；12 个现有非包素材没有纳入 wheel，逐项记录于该目录的 `final-consistency.json`。`data`、`.env` 和其他运行状态仍未读取或复制。

随后 Root 因新发现的 registry drain 子任务交接生命周期边界撤回最终冻结。r2 的 `manifest.json` 已标 `candidate_only=true`、`delivery_status=held_pending_harness_handoff_review`；原 wheel、日志与 26 项结果完整保留。该暂缓来自新的 harness 验收范围，不是原生冷验失败，也不能使用既有 26 项结果证明新发现的边界。

当时没有最终可交付包结论。后续生命周期验收通过后曾构建并验证 r3，再由含 CI fixture 修复的 r4、含 builder 修复的 r5 依次承接输入；最终身份与独立验证结果见本报告开头。r2 继续保留 hold 状态。
