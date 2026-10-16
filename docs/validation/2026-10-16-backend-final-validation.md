# 2026-10-16 后端最终验证

最终冻结代码重新收集得到 **6,962 个后端用例**。按精确 node ID 和各次 pytest 启动时间合并实际结果：**6,923 通过、39 跳过、0 个最新失败或错误、0 个遗漏**。

这是原始部分批次、完整余段和修复复验的去重覆盖结果，**不是一次未中断的全量全绿记录**。所有初败、跳过、复验和被替换用例均保留在本地审计索引中。

## 运行环境

- Windows，项目内置 Python 3.11.9，pytest 8.4.2、pytest-asyncio 0.26.0。
- 入口：`output/ci-repair-20261015/pytest-python311.py`。
- Source executor：已安装 Codex 26.1007.9446.0 的真实 `codex.exe exec-server`。
- 网络运行时：正式 `backend/network/resources/x86_64-pc-windows-msvc`。
- 使用 `-vv`、`faulthandler_timeout=60` 和 JUnit；续跑增加只记录结果与 longrepr 的观察插件，不改变超时或执行语义。
- 没有启用 Windows 网络 provisioning 测试开关，没有执行账号、防火墙初始化、官方登录或托管服务接入。

## 原始批次与续跑

| 批次 | 实际结果 | 处理方式 |
| --- | --- | --- |
| 原始全范围尝试 | 收集 6,951；已完成 3,470，其中 3,383 通过、74 失败、13 跳过 | MCP 旧测试配置启动了没有 JSON-RPC 应答的裸 Python，逐参数等待初始化超时；保留原日志后发送 Ctrl+C。进程退出但未生成 pytest 总结或 JUnit。 |
| 原批未完成部分 | 3,481 项 | 明确记为未完成，没有计入跳过或通过；由精确剩余清单续跑。 |
| 完整余段及必要前缀复验 | 3,535 项：3,500 通过、9 失败、26 跳过 | 包含全部未完成项、MCP 整组和需要复验的前缀；失败堆栈即时落盘。9 项初败均有后续通过证明。 |
| 最终当前 collection | 6,962 个唯一 node ID | 纳入新增与替换的 SDK 生命周期用例，并逐 ID 取最新代码快照的结果。 |

上述批次数不能直接相加。原始 74 项失败和余段 9 项失败的最新结果、来源 XML、启动时间及历史结果均可逐 ID 查询。合并使用 pytest suite 的开始时间，而不是 XML 写出时间，避免晚结束的旧批次覆盖修复后的独立复验。

Windows 下逐 node ID 参数触发大量重复路径解析的首次续跑收集尝试，已在任何用例执行前中断并单独保留日志；改为 284 个整文件选择。集合比较确认仍是相同的 3,535 项，没有删减用例。

## 主要修复与复验证据

- 旧会话测试宿主补齐生产构造器已有的 `environment_manager=None` 契约；对话结束、模式与共享宿主消费者两批分别 89/89、35/35，通过后不再添加生产兼容分支。
- 旧“细粒度网络一律不支持”期望更新为真实 capture → Source native inspect，并确认 managed/restricted 网络及代理要求；路径与权限整文件 80/80。
- MCP 审批旧配置改为真实 owned JSON-RPC peer；7 个参数通过，不自动批准或放宽权限。
- Windows multiprocessing 失败来自本地测试入口被 spawn 子进程重导后再次运行 pytest。仅给本地入口补标准 `__main__` 保护；真实跨进程认证存储用例通过，产品 vault 锁未修改。
- Monitor 测试使用真实 `BackgroundCommand` 契约；整文件 8/8。工具超时用例用真实 execute-entered 握手区分未进入执行与执行中取消，原 30ms 超时和 0.15s 写延迟未放大；整文件 17/17。
- 鉴权刷新测试补真实 accepted config layer stack，保留 original/newer 对象身份与实际时序；整文件去重 19/19。
- Vault 环境配置严格使用 Source 的 `local` / `remote` 来源枚举；证明同名透传、显式 TARGET 覆盖及 MCP 独立 scope。旧别名在 public writer/private parser 均实际拒绝，跨 scope 模板仍拒绝；整文件 5/5，无生产权限放宽。
- 最终 SDK 生命周期验收覆盖 bound/unbound × early settlement/late registry handoff 四种真实场景。旧 wrapper 已结束、原 invoke 未完成时，原 lease、capability 和同会话 fence 继续持有；原 source 完成后，实际 capability 关闭屏障仍持有 fence，直到原 snapshot 关闭。最新 16/16 包含这四项。
- Prepared MCP retirement 追踪原 turn 的 context 字典与真实晚交接任务，不扫描共享 lifecycle 池、不读取下一回合 context。对应 R3 独立两批去重 37 项为 36 通过、1 跳过；这些结果按 ID 合并，不重复增加后端总数。

旧单项 SDK 场景及中间诊断项已放入 `retired_or_replaced` 历史区；当前只计算最终四参数场景，避免别名或替换用例重复计数。

## 39 项真实跳过边界

| 原因 | 唯一用例数 |
| --- | ---: |
| Windows 当前账号不能创建符号链接 | 19 |
| 需要真实 Linux namespace / bwrap / POSIX FD 沙箱 | 9 |
| Windows 代理端口尚未显式 provision，禁止测试隐式修改系统设置 | 8 |
| 需要 POSIX 非 UTF-8 原生字节路径 | 1 |
| 需要 Linux inotify access 事件 | 1 |
| 已安装 peer 的 `environment/config/read` 仍为 stub | 1 |
| **合计** | **39** |

每项跳过的 node ID 和原始原因单独保存，没有计为通过。peer 的 stub 限制特指配置读取，不能解释为 Git 能力缺失；此前 Git 沙箱选择问题已有独立修复与真实验证。

## 保留的证据缺口

原批 `test_harness_turn_boundaries` 的 `event_retry` / `transport_retry` 两项留下了 FAILED 标记，但中断前没有输出其断言总结。随后原阈值下三个参数及整文件 13 项通过。原始断言证据缺口仍保留，**不将其归因为并行负载或全局状态污染**，也不删除原失败标记。

## 本地审计索引

以下文件位于 `output/ci-repair-20261015/`：

- `backend-final-current-collection-after-handoff-20261016.collected.json`：最终当前 6,962 项有序 collection。
- `backend-final-current-results-20261016.json`：每项最新结果、全部历史、初败复验映射、退役用例及证据来源。
- `backend-final-skip-audit-20261016.json`：39 项逐节点跳过理由。
- `backend-remote-final-20261016.log` / `.interrupted.json`：原始部分批次及其中断事实。
- `backend-remote-final-20261016.remaining-nodeids.txt` / `.resume-manifest.json`：精确未完成清单与续跑集合证明。
- `backend-remote-final-20261016-rest.log` / `.xml` / `.reports.jsonl`：完整余段和即时失败堆栈。
- `sdk-mcp-final-owner-handoff-after.xml`：最终 16 项实际 SDK 生命周期验收。

独立根目录测试索引原为 1,815 项，其中 1,753 通过、62 跳过；随后 Source 缓存初始化新增四项回归并实际通过，重新收集 1,819 项与合并节点集合精确一致，当前为 1,757 通过、62 跳过。新增批的其余八项是已有打包合同的复验，不重复计数；不把合并结果说成新跑完整 1,819 项。根测试与本报告后端统计分开，不叠加计数。最终 wheel 的冷安装验证也另列于打包证据中。
