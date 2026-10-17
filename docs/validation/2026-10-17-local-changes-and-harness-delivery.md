# 本机 Changes 与 harness 收尾交付

本批执行用户明确范围：“完成本机可验证的部分，和 codexsrc”。2026-10-17 开始，2026-10-18 本地收尾。官方账号、官方账号依赖服务不做；当前没有可用 SSH 主机或普通 WSL 发行版，外机验收不计本批通过。本文件区分实现、源码审计、真实桌面与独立安装证据，不据测试数量推算全产品完成率。

## 实现与参照身份

| 对象 | 身份 |
| --- | --- |
| 开源 Codex 参照 | `.tmp/codex-src`，`822e58cc3d666166c7446c5b1ea2e52f5d09594c` |
| 实际桌面参照 | Appx `26.1013.4894.0`，package `26.1013.40934` |
| 桌面 ASAR SHA256 | `bbec1d64e135b718a154b31655bb8ad9a39472bac1b2cabba991a9d97a339dd8` |
| Changes / readiness 实现 | `c85ab36a` |
| SDK/CUA owner 与 MCP close 修复、r20 后端冻结 | `82e659d62a0dae10cd19b7b330b4af92b4f44861` |
| 前端集成、审批和 PDF CSP 修复 | `285b202819c32b97a354ef511c42d678e2f8fc11` |
| 后续仅测试契约修正 | `2f4499e6fad87f2a66a4d6be0c443ce4f27a82e5` |
| 最后路径文字隔离修复 | `37f05e5f77ca6e3721fadd6ebeeb56c6251aceee` |
| 本机最后前端 build 6 | `assets/index-C5LbDzwP.js` |
| `frontend/dist/index.html` SHA256 | `8d443d717887b3710b15eb75e90abcc59f43aad467b1b4ed188ca9a29a8a1f94` |

参照截图为 `output/playwright/ui-reference-20261004-v2/desktop/22-codex-inline-diff-pane.png`；当前桌面叶合同的原始片段、SHA、偏移保存在 `output/source-leaf-audit-20261017/` 与 `output/desktop-services-source-20261017/`。开源 app-server/harness 契约与实际桌面资源分别使用，不把桌面私有实现说成公开源码。

## Changes 完整调用链

后端比较改为五种真实来源：HEAD→disk 的未提交、index→disk 的未暂存、HEAD→index 的已暂存、真实 merge-base→disk 的分支，以及 parent/空树→commit 的提交比较。不再拼接暂存与未暂存 patch。默认来源为分支；没有默认分支时返回真实 expected 状态，不虚构 main 或跳到另一来源。

`diff.git_review_options` 返回真实分支、merge base 和提交；`diff.git_file_contents` 走既有 client command/result，每批最多四个对象，前端合并 40ms，单对象 5MiB。完整文件按原环境、owner、cwd、来源和 snapshot generation 绑定。旧对象不存在时不读当前磁盘冒充该对象。文本保留 EOL/BOM/无终换行，图片与 PDF 两侧使用实际版本字节。

暂存、取消暂存、放弃及全部操作使用对应 snapshot 的未过滤 binary patch 和原 Git apply 调用链。全部操作覆盖整个 snapshot。已暂存没有“全部放弃”；单文件放弃的两步 partial-success 保留真实结果。`review.options/prepare` 与 Changes 同用 captured app-owned executor，子目录 prepare 仍生成 repo-relative patch，scope.cwd 不被更换。模型 Git 沙箱未借这次桌面修复改动。

前端按实际桌面补齐：

- 完整文件读取、20 行与 Shift/全部上下文展开；只有真实 gap 显示未修改行数，删除自造的零 gap 起始行标题。
- 按固定 Source Lezer 表隐藏整个 import-only hunk；混合变更与真实评论/草稿保护该 hunk。过滤不改文件头原始增删数。空白过滤由真实 Git 比较完成。
- 图片/PDF 独立双侧预览与分页。SVG/Markdown 随渲染偏好；Markdown 按 Source 读当前路径，commit 也如此；已暂存不拿 disk Markdown 伪装 index。
- 工具栏 625px 宽窄行为、unified/split/auto 三态。auto 以每个文件正文实际宽度 800px 和该文件两侧变化判定，树占宽影响正文。
- Source 文件搜索评分、360px 跳转菜单、250px 初始文件树、200px 最小宽度与父宽 60% 上限、拖到候选宽度不足 100px 收起，以及真实 spring 过渡。
- 每文件打开与菜单、正文右键请求更改/打开/路径/换行。只有真实可用的本地编辑器作为外部目标；内置目标的显式首选也经过 IPC 保存。
- owner 内保留来源、base/commit、当前文件和展开选择。展开状态为最多 20 个 route 的内存状态；不新增磁盘展开持久化，commit 不写入全局来源偏好。
- 删除 48/96 文件分页和 180/600 行截断。普通比较显示全部文件；真实阈值为文件数 >128、变更行数 >9000 或原 patch UTF-8 字节 >12MiB，超过后单文件配合前后导航与完整文件跳转。

## 本地 harness、CUA 与清理归属

Computer Use 消费用户显式指定的已安装 Source runtime，经真实 thread/turn、随机 pipe、独立 home 和原生 helper 建立伴随服务。不扫描备用 runtime、不发布静态能力 catalog，不把私有 SDK/helper 打进 MiniCode wheel。全局串行、300 秒实际 app 审批、turn consumer drain、thread retire 与 host cleanup 沿原生命周期接通。

空 schema 的 Computer Use app 审批走底部人工审批区；Never/full-write 不替用户批准。处理后恢复原 Composer DOM、草稿、选区与附件。restricted-token readiness 不再误要求 elevated account/setup receipt；原权限边界保留。

独立安装 r19 暴露 SDKSession 原生 CUA owner 字段缺失后，本批修复 SDKSession、ConversationThreadRuntime、AgentSession 的完整 owner/close 链：fork 独立持有 companion，原 consumer 结束后、环境释放前关闭。MCP 主动关闭时的 transport/session 原始异常继续传播，不能因 `_closing` 吞掉或虚报 clean ack。r19 保持 HOLD；用新冻结创建 r20 重新安装验收。

## 验证结果

这些范围存在重叠，不能相加为一个全产品测试数。

| 验证范围 | 最终结果 | 证据目录 |
| --- | --- | --- |
| 前端统一受影响批 | 43 files / 469 passed | `output/source-leaf-audit-20261017/frontend-test-summary.md` |
| 最后零 gap / CSP 窄验 | 实际 3 files / 65 passed | `output/changes-completion-20261017/final-front-narrow.*` |
| Desktop unit | 170 passed | `output/changes-completion-20261017/desktop-unit.log` |
| 最后 security / open-targets | 27 passed | `output/changes-completion-20261017/desktop-final-security.log` |
| 后端初批失败修正后的窄验 | 53 passed / 0 skipped | `output/source-backend-unified-20261017/narrow-fix-batch/` |
| SDK/CUA owner + MCP close 最终批 | 80 passed / 1 prerequisite skipped | `output/source-sdk-cua-owner-repair-20261017/final-fix-batch/` |
| CI 旧测试前提修正 | 14 passed / 0 skipped | `output/ci-source-r20-39045562403-20261017/frontend-fix-receipt.json` |
| build 5 | TSC + Vite exit 0 | `output/changes-completion-20261017/frontend-build-5.*` |
| build 5 后静态核对 | protocol / bundle budget / UI debt 均 exit 0，输入输出无变化 | `output/ci-source-r20-39045562403-20261017/build5-required-receipt.json` |
| 最后路径隔离修复 build 6 | 3 files / 72 passed；TSC/Vite、protocol、bundle/UI debt 均 exit 0 | `output/changes-completion-20261017/frontend-build-6.json` 与 `build6-*` 日志 |
| r20 新 wheel 独立安装 | 226 passed / 0 failed/error / 4 real skipped | `output/source-executor-remote-final-wheel-r20-20261017/REPORT.md` |

最后窄验原命令包含一个不存在的 `DiffPalette.chain.test.tsx` filter，Vitest 忽略它，所以记实际三个文件；正确 palette 测试在前述统一 43 文件批中通过。初始后端 333 passed / 2 failed / 1 skipped、r19 原 SDK 断链、首次构建 TSC 错误和 GBK reporter 诊断均保留，不能用最终结果覆盖。

旧完整 [CI 39045562403](https://github.com/khalil-wu/minicode/actions/runs/39045562403)，head `285b2028`，真实结论为 **7 success / 1 frontend failure**。前端 397 files / 4626 tests 通过、两文件三测试失败；失败是旧 fixture 没声明新默认来源/snapshot、全 mock 缺真实 command ID。只修测试前提并补强 source/generation 断言，不回退生产合同。该旧运行 backend 为 7076 passed / 154 skipped，root 为 1785 passed / 52 skipped；前端 build/budget 因失败未执行。完整原日志、JUnit 与逐项 skip 原因保存在 `output/ci-source-r20-39045562403-20261017/ci-receipt.*`。

仅测试修正后的 [CI 39046401427](https://github.com/khalil-wu/minicode/actions/runs/39046401427) 对应 `2f4499e6`，已 **8/8 success**：frontend 4629 passed、backend 7076 passed / 154 skipped、root 1785 passed / 52 skipped，原日志与官方 JUnit 均独立归档。

最后路径文字修复后的 [CI 39047123759](https://github.com/khalil-wu/minicode/actions/runs/39047123759) 对应精确 head `37f05e5f77ca6e3721fadd6ebeeb56c6251aceee`，2026-10-18 本地收尾时确认 **completed / success，8/8 jobs success**。该次 frontend unit、build、bundle/UI debt、两个 backend 矩阵、Windows harness、Electron、Desktop unit/smoke 与两个 pinned Source native 构建均使用自己的运行结果，不复用上一轮。原八项日志、官方 JUnit、逐项 pass/skip 与哈希绑定在 `output/ci-source-r20-39047123759-20261017/ci-receipt.*`。此后只有交付文档提交，不改变实现、前端构建或 r20 后端冻结身份。

| 最后 CI 的真实 job | Passed | Skipped |
| --- | ---: | ---: |
| Frontend | 4629 | 0 |
| Backend | 7076 | 154 |
| Root tests | 1785 | 52 |
| Windows harness | 362 | 15 |
| Desktop unit + smoke | 167 | 9 |
| Electron Playwright | 1 | 0 |
| Source execution policy native build | 构建/身份校验成功 | 不适用 |
| Source network native build | 构建/身份校验成功 | 不适用 |

上表各组可能重叠，不能累计为去重总数。两个 native job 没有虚构 pytest 数量；backend/root 每个 skip 的实际原因另存原 JUnit 解析结果，Windows/Desktop 也没有把 skip 当作通过。

## 真实 Electron 操作

主要操作使用 build 5；最后路径纠正使用 build 6 新进程。两轮均为独立 profile / Git workspace、生产 backend/IPC/Git，只有原生文件夹选择被定向到该自有 fixture。普通用户 MiniCode PID 9064/backend 8000 与仓库原有未跟踪 `data/` 没有操作。主要操作证据在 `output/playwright/changes-completion-final-20261017/csp-fixed/`，最后路径修正证据在其同级 `path-fixed/`。

| 真实场景 | 观察 |
| --- | --- |
| 暂存与取消暂存 | index→disk 的暂存真实生成 HEAD→index 完整净比较；取消暂存实际更新 Git；未提交只有一份比较，无额外暂存按钮 |
| 关闭重开 | `LOCAL_UI_DRAFT_KEEP_20261017` 草稿保持；未借 QA 覆写普通用户状态 |
| Markdown / SVG | 当前路径 Markdown、双侧 SVG 与纯 import 过滤生效，import 文件头仍 `+1/-1` |
| PDF | 旧版本 2 页、新版本 3 页，两侧独立翻页；修复实际 Electron CSP 拦截 blob 后两个 canvas 均可见，266×195 |
| 提交选择 | 真实 `Owned reviewable commit`；Markdown 显示当前路径文字；已暂存 Markdown 保持代码投影，六行且无当前路径标题 |
| 跳转 / 菜单 | 360px 弹层、Source 匹配、跳转定位；文件头菜单与正文右键菜单分别核对，未写系统剪贴板 |
| 文件树 / 拖拽 | 初始 250px，实际拖到 333px，筛选与选择正常，再拖小收起 |
| 上下文与 auto | gap 193→173，Shift 展开剩余全部；拆分两侧 DOM cells 120→160→506。正文 1029px 有 split，树占宽后 676px 为 unified |
| 大差异 | 自有仓库 132 个真实变更，只 render 一个当前文件；Jump 仍有全部 132 项，目录匹配 125 项，前后导航、末尾树项存在；仅新增文件不误用 auto split |
| 右键评论与导入过滤 | 在真实 R1 请求更改并保存本地评论后，import-only hunk 的两行变化保留；删除该评论后过滤恢复，主输入草稿仍不变 |

原 PDF CSP 四次错误保存在首轮 console；修复后新 Electron session 的 console 为 0 errors / 0 warnings。QA 最初把 `smp` 错当作任意小写跳跃匹配、把拖到关闭后的树宽错当作仍为 333px，两个超时与快照保留；按 Source 真实匹配与 live width 改正 QA，没有改生产兜底。一次 CLI selector 引号和 console 参数错误也不冒称产品缺陷。

逐张查看真实截图又发现文件头目录 `large-case/` 被直接 RTL 排成 `/large-case`。静态核对当前 Source `code-diff-628be5db9fe9.js`（SHA256 `f402c9dcef03dd1a6d238b0ce1709ec366ef8814ae7e7ee10a41274dab8e7d9b`）原 UTF-16 offset 33575/33740：外层 RTL 负责截断，实际文本内层为 LTR/plaintext。`37f05e5f` 仅给目录增加该隔离层及文件名方向，不改字体。build 6 的实际字符坐标为目录首字 X=891.15625、尾斜杠 X=959.25，文件名 X=963.953125 正接斜杠右缘；真实截图与 14px/400 原字体一并核对，草稿保留，console 无错误。初次 build 6 QA shell 被旧 Windows PowerShell 对 npm stderr 的解释中止，原日志保留；改用已有 PowerShell 7 后统一 72 项和完整构建通过，没有为 warning 改依赖。

两轮 QA Electron 都通过实际顶部关闭按钮正常 exit 0，provider 关闭，原 backend/端口释放。CLI 在目标关闭后报无法读取 url 是已关闭 target 的诊断；不当作 app crash，也不把这次诊断掩去。清理凭据为两个目录各自的 `cleanup.json`。

## r20 安装候选与边界

候选为 `output/source-executor-remote-final-wheel-r20-20261017/minicode-0.2.0-py3-none-win_amd64.whl`，16,949,002 bytes，SHA256 `3b353499ab24dcd91b818d8e9334bba9ee1124351bfd840eabac892b521e5097`。1303 冻结输入、1271 backend wheel members 均匹配；13 次实际消费加载审计来自新安装，没有 av/av.libs。226 passed 包含 41 个 direct cold 与 185 pytest，4 skipped 为一个 Windows deny-read 前提和三个 backend-only 缺 desktop 相对资源。

最后在 `37f05e5f` 再次只读核对：1303 冻结输入、当前 backend 文件集合、1271 包成员、既有安装与原 wheel 摘要无差异，后续仅前端/测试变化没有使该候选后端过期。`final-project-head-match.json` 单独记录此一致性核对；没有借它冒称重跑安装验收。r19/r20 私有路径无自有残留进程。

两个全新 profile 消费实际已安装 CUA runtime 的 bootstrap / list_apps / owner / cleanup 通过。原生 pipe host 自然退出 0；CUA launcher 按请求 owned-tree close 实际退出 1，closed/released 确认完整，不写成所有子进程自然退出 0。没有分发 Source 私有 SDK/helper。本轮实际系统 CUA 只读 inventory/metadata，没有执行桌面输入、系统截图或真实 app 审批，不能外推成这些行为均实机通过。

仍需独立证据的范围是外机 SSH/普通 WSL、WFP 实际 provisioning 与流量矩阵、Linux 安装包冷验、同真实模型的大型/长时任务和全平台原生分支；全界面同构建同尺寸的每个 hover/focus/disabled/动画状态也未全量验收。官方账号范围按用户排除。详见[当前差距](2026-10-16-current-desktop-gaps.md)。
