# 全链路审计修复与验证 · 2026-10-10

用户授权“ok修复”后，修复本轮重新验证的 8 组缺陷，并完成发行资源 staging 验证。行为依据仍为 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`；差异、未实现能力和待验证问题未转成 bug。原审计及对照调用链见本地收据 `output/full-chain-evidence-20261009/full-chain-audit.md`。

审计验证完成时尚未提交、安装或更新用户当前实例；保留活跃 `frontend/dist` 和所有用户修改，`design.md` 仍为原有 224 行新增、28 行删除。验证使用独立临时目录、桌面 profile、数据库及本地 provider/stub。本文的 `output/...` 路径相对仓库根目录，指向本地验证收据，不随仓库提交。

## 修复结果

| 原问题 | 根因修复与覆盖边界 | 主要入口 |
|---|---|---|
| P1 崩溃后 completed 却缺最终答案 | 精确 terminal intent 随既有完成 CAS 提交；partial 不再覆盖 final；cold restore/switch/sync 在读取公开快照前回收投影；终态 journal 记录完成后清 checkpoint。复用 runtime 的 journal，阻塞 journal 读写移到线程。 | [query_terminal.py:222](../../backend/agent/query_terminal.py#L222)、[conversation_projection_service.py:88](../../backend/services/conversation_projection_service.py#L88) |
| P1 网页草稿丢失 / P2 桌面更新卡顿 | 正文、标签索引、元数据、选择同事务写入 IndexedDB v2，按文件增量提交；迁移成功才释放旧正文。冷恢复保留新编辑、关闭选择和隐藏工作区；关闭等待 commit；失败明确显示。冷文件树重命名前先恢复合并，再派发磁盘操作；等待期间切工作区则取消。 | [editor-drafts.ts:24](../../frontend/src.v2/stores/editor-drafts.ts#L24)、[index.ts:162](../../frontend/src.v2/stores/index.ts#L162)、[FileTreeContextMenu.tsx:165](../../frontend/src.v2/shell/FileTreeContextMenu.tsx#L165) |
| P2 durable 建会话重放重复创建 | creation command 身份随创建记录原子持久化；占用请求 ID 时采用确定性 command ID，重放取回实际创建的 ID；删除 tombstone 保留身份，clone 清除身份。非法/缺省 ID 在仓库层保持确定性；WS 非法 ID 仍在既有边界拒绝。 | [repository.py:148](../../backend/conversations/repository.py#L148) |
| P2 UI 续传的长子任务结果无法补读/清理 | 结果外部化显式携带父会话和实际产物工作区，贯通存储、分页读取和删除；存储失败不再返回假成功。 | [subagent_support.py:121](../../backend/tools/subagent_support.py#L121)、`agent_tools.py` |
| P2 MCP 不同运行身份误去重 | 复用完整运行/鉴权身份进行去重，包含 env、cwd、transport、headers、helper 和 OAuth 配置；HTTP 账号身份包含 server name；等价 stdio 仍遵守原优先级。 | [manager.py:2051](../../backend/mcp/manager.py#L2051) |
| P2 模型 downshift 先破坏历史再失败 | 跨 endpoint/protocol 时用原模型的可读摘要链压缩；native replacement 安装前用目标模型验证。已有不可转换的加密窗口提前报告可恢复能力错误并保留历史；native 媒体检查覆盖 message 与 tool output。 | [context.py:3289](../../backend/agent/context.py#L3289)、`native_compaction.py` |
| P2 冷模糊搜索过慢 | 索引只收词法搜索元数据；排名后逐个实时解析并调用原权限规则，补足允许的结果数。应用根每次查询取一次；扫描中/缓存后的 junction 变化仍拒绝。没有缓存授权结果。 | [fuzzy_search.py:183](../../backend/workspace/fuzzy_search.py#L183) |
| P3 delivery fence 无界增长 | cleanup 释放每个 run 的 pending marker，只保留会话最后一次完成身份；旧 done/cleanup 不覆盖新身份，legacy marker 在新运行注册时清除。 | [run_manager.py:180](../../backend/ws/run_manager.py#L180) |
| 发行资源残留 | release 使用独立、清空旧产物的 staging，再映射为包内 `frontend/dist`；保留活跃开发 dist。已构建并启动真实 unpacked Windows 包。 | [vite.config.ts:125](../../frontend/vite.config.ts#L125)、[desktop/package.json:103](../../desktop/package.json#L103) |

## 实测

同一 Windows 环境；耗时为单机探针数据，不代表所有机器或供应商。草稿耗时测的是同步 store 更新，未冒称磁盘提交耗时、真实输入帧或整页渲染耗时。

| 场景 | 修复前 | 最终结果 | 收据 |
|---|---|---|---|
| Electron 43.4 / Chromium 150，20×600K 字符草稿，15 次更新 | 平均 189.58 ms；P95 241.80 ms | 平均 **0.807 ms**；P95 **1.400 ms**；零 LS 写入。独立进程重启后 20 条正文、baseline、索引元数据和选择全部匹配，最新编辑保留。 | `output/full-chain-fixes-20261009/ui-electron-editor-persistence-v2.json` |
| Chrome 154，20×600K 草稿及重载 | 5×600K 即出现 LS 配额失败 | 20 条提交并重载恢复，最新编辑保留；20 条同步更新均值 1.107 ms / P95 1.5 ms。 | `output/full-chain-fixes-20261009/browser-editor-persistence-v2.json` |
| Chrome，LS 填满 5,242,851 字符后编辑并重载 | v1 正文提交，但 index/meta/location 写入失败；重载变为 0 tab 并删除正文 | v2 正文、索引、选择同事务提交；重载后正文与 baseline 精确保留，选择仍为 `app.ts`。 | `output/full-chain-fixes-20261009/browser-index-quota-v2.json` |
| Python 3.12.10，1 万文件真实 FuzzySearchTool 冷查询 | 14,900.84 ms / CPU 14,656.25 ms；重复取应用根 10,000 次 | **265.56 ms / CPU 265.63 ms**；应用根 1 次。暖查询 48.38 → 34.06 ms。 | 前：`output/full-chain-evidence-20261009/workspace-perf.json`；后：`output/full-chain-fixes-20261009/fuzzy_perf.json` |
| 300 个已投影 settled run 的暖 journal 恢复，3 样本 | 每次 SQLite 300 读；1,178.44–1,500.80 ms | 每次 **0 读；7.89–8.78 ms**。cold 未完成投影仍查真实运行事实。 | 前：`output/full-chain-evidence-20261009/journal-restore-before.json`；后：`output/full-chain-evidence-20261009/journal-restore-after.json` |
| 最终包内 frontend 资源原始大小 | 活跃 dist：182,235,363 bytes / 1,347 文件 / 36 entry chunks | staging 和真实包：**75,483,149 bytes / 348 文件 / 1 entry chunk**。活跃 dist 未清理。此差额不等同压缩后的 NSIS 体积。 | `output/full-chain-fixes-20261009/package-resources-v2.json` |

## 分批验证

| 范围 | 最终有效结果 | 证据 |
|---|---|---|
| 后端：恢复/终态投影/创建与删除、模型迁移、多 agent 续传/owner、MCP 身份、搜索/权限、资源清理 | **597 通过、0 失败/错误/跳过，28 模块**。这是各批次现存 testcase 的最新结果去重，不是一次全仓测试。 | `output/full-chain-fixes-20261009/backend-current-coverage.json` |
| 最后崩溃/重放批次 | **16 通过、0 跳过**；真实子进程退出，configured 和 injected runtime 分别运行。 | `output/full-chain-fixes-20261009/harness-closure-results.xml` |
| 前端：编辑器存储/恢复/效率/隐藏工作区/模型采用/面板/文件树，会话删除与 ChatPane scope | **180 通过、0 失败/跳过，9 文件**。 | `output/full-chain-fixes-20261009/frontend-v2-closure-results.json` |
| 桌面批次 | **40 通过、0 跳过**。 | `output/full-chain-fixes-20261009/validation-status.json` |
| 最终 unpacked Windows 包 | **1 通过、0 跳过，23.72 s**：renderer/preload/IPC、managed Python sidecar、本地模型→CodeMode 音频产物→UI 播放、shutdown cleanup。 | `output/full-chain-fixes-20261009/packaged-smoke-v2-closure.txt` |
| 最终静态检查与构建 | `tsc -b`、`vite build --mode desktop-release`、`git diff --check` 通过。构建原有 Browserslist、动态 import 和 chunk-size 警告保留。 | 最新 staging 构建与验证汇总 |

历史失败收据保留：后端首批 2 失败、targeted 1 失败；前端首批 2 失败、v2 首批 11 失败、v2 后续 2 失败，最终均由对应修改后的用例覆盖。后端补测中一次路径写错导致 **0 项运行**，另一次新增 `invalid/id` WS 夹具在既有所有者边界被拒绝，未触发创建；随后改为仓库层真实重放/删除参数测试。旧 media、tombstone 未参数化身份及该 WS 参数已移除，未伪装成通过。修正 crash fixture 中遮蔽 runtime 参数的变量后，两种恢复入口重新真实通过。原 v1 Electron/Chrome 收据保留，最终结论使用 v2 收据。

提交前另做 CI 门禁复核：发现 `loop.py` 为 506 行，超过既有 500 行迁移预算；将具体错误到终态事件的映射移入现有 `terminal_projection.py`，外层 loop 仍拥有终态提交。修改后 kernel boundary 与 protocol sync 检查通过，模型迁移、复杂 loop 错误、终态事务和 TurnKernel 四模块 **48 项通过、0 跳过**（与上表部分重叠，不相加）；收据为 `output/full-chain-fixes-20261009/prepush-terminal-results.xml`。该次验证前一次错误测试路径导致 0 项运行，记录为 `prepush-terminal-collection-error.xml`。

## 验证缺口

- 未验证真实付费供应商的首字延迟、token 账单、远端 OAuth/HTTP 鉴权；协议迁移用真实 adapter 与本地 MockTransport，不能替代供应商服务验证。
- 未做多日 soak、断电/fsync、签名与完整 NSIS 安装/升级；仅验证真实 unpacked 包。
- 没有新的 Cursor 热退出实现/完整视觉对照，编辑器缺陷和修复依据 MiniCode 自己的草稿契约与实测。
- 原审计中的 steer 入模后 ACK 写失败、恢复后工具重复副作用等待验证项仍未确认；未宣称本次解决。Skill 摘要保真、队友/worker 取向等产品差异也没有转成缺陷修复。

原始失败、成功、未验证范围分别保留在本地审计收据 `output/full-chain-evidence-20261009/full-chain-audit.md` 和修复验证汇总 `output/full-chain-fixes-20261009/validation-status.json`。
