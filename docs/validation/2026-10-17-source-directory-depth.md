# Source 即时目录枚举与最终安装候选

主题联测时，实际文件树把 `.git` 下的 hooks、objects、HEAD 等显示成了项目根项。真实目录只有 `.git` 和三个项目文件；前端没有提升子项，错误来自后端即时目录枚举的深度参数。

## Source 合同与修复

参考 checkout 仍为 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。`codex-rs/exec-server/src/local_file_system.rs:741-826` 根深度是 0，先加入该目录的子项，再按深度决定是否打开子目录；`codex-rs/exec-server/tests/file_system/shared.rs:514-534` 的 shallow 请求使用 `max_depth:0`。`codex-rs/app-server-protocol/src/protocol/v2/fs.rs:88-116` 也明确 readDirectory 返回直接子项名称。实际 Source 程序 `0.162.0-alpha.17.2` 与本地实现的 0/1/2 深度结果分别核对通过。

因此通用 walk 原实现符合 Source，不能将它改为另一种深度定义。真正修复为 `ExecutorEditorService.directory_entries` 和 `executor_tool_io._directory_entries` 两个即时枚举调用改用 `max_depth=0`，保留原 owner、权限、错误/截断预算和通用递归语义。这样既修复文件树，也避免 List/Glob/Grep 在自己递归时重复读入下一层；没有添加前端 `.git` 祖先过滤来掩盖数据错误。

实现冻结提交：`19058aef6fe98f928fd962a24159d78a710841c1`，已推送。

## 验证

相关 8 文件共 173 个场景的最新去重结果为 **168 passed、5 skipped、0 failed**。首批为 166 passed、5 skipped、2 failed；两个失败均来自新增测试漏写 Glob 公开输出的 `- ` 项目符号，只修该测试断言后新文件完整 6/6 通过，没有重写原失败记录，也没有把重复用例叠加成总通过数。

五项跳过分别为 Windows symlink 权限两项、POSIX 原始字节路径一项、worktree symlink 权限一项，以及实际安装 Codex 的 environmentConfigRead stub 一项；均不算通过。新六项覆盖本地/实际 Source 的通用 walk 0/1/2、readDirectory、真实 HTTP 树与 owner、逐层展开路径、List/Glob/Grep 去重和忽略子树。

原始记录在 `output/file-tree-depth-readonly-20261017/` 的 `DIRECTORY-DEPTH-DELIVERY.md`、`FINAL-RESULTS.json` 和两次 JUnit/log。

Root 冷重启同一自有 Electron profile，使用同一前端 `assets/index-WzIWjdjR.js` 和新后端复验。新增自有 `src/nested/theme.ts` 后，根仅出现 src、index.html、main.ts、README.md；展开 src 只多出 nested，继续展开才出现 theme.ts，实际打开的完整路径正确。原 main.ts 的未保存代码、活动草稿、隐藏工作区草稿和两套主题全部保留，page errors=[]。

截图和真实状态见 `output/playwright/file-tree-followup-20261017/validation.json`、`final-receipt.json` 及 `02`、`03` PNG。最初 QA 使用精确名称 theme.ts，未匹配真实带 Git 状态的可访问名称 theme.ts U，其超时保留；根据新 ARIA 快照更正定位后完成验证，没有修改生产标签来迁就测试。普通用户窗口没有重启，系统剪贴板未操作。

## r15 与 CI

最新 Windows backend 候选为 `output/source-executor-remote-final-wheel-r15-20261017/minicode-0.2.0-py3-none-win_amd64.whl`，16,099,684 bytes，SHA256 `b521322710a34eac397722757a0948472d47618ab0345af3cf1f1695a08cdb30`。冻结于上述 19058aef，1273 个输入、1252 个 backend 成员；源码、快照、wheel、全新安装字节一致，Git 核对只有 166 项历史 CRLF 差异，无内容差异或未提交输入。

基础/Node/SDK/启动冷验 **41/41**、已安装 MCP 媒体 **7/7**、已安装目录边界 **6/6** 分别在新目标运行通过，startup errors=[]。目录组实际核对 Source exe 的版本与 SHA，并从安装包导入；没有借 r14 的结果代替。r13、r14 原样保留。详见该目录 `REPORT.md`、`installed-scope-receipt.json` 和 `delivery-receipt.json`。这仍是限定范围的 Windows 后端候选，不是完整桌面安装器或全产品等价证明。

完整 CI 独立绑定 [19058aef 的 run 38932478631](https://github.com/khalil-wu/minicode/actions/runs/38932478631)，**8/8 job 全部成功**。前端 370 文件、4393 passed；backend 6956 passed / 111 skipped；root 1768 passed / 51 skipped；Windows 362 passed / 15 skipped；desktop unit 161 passed / 9 skipped、smoke 6 passed；Electron Playwright 1 passed；两个 native 构建均成功。backend 中未配置 owned Source peer 等原因跳过的 68 项互操作场景没有算通过，r15 的实际 Windows Source 验证另列，不能混并范围。

最终日志、JUnit 与计数收据保存在 `output/ci-theme-media-20261017/ci-receipt.md` 及 `.json`，另保留八份 job 日志、完整 run 日志压缩包和两个 Python suite 的原始 XML 制品。此前主题主体 cf718846 和焦点修复 bf429062 的两轮 8/8 成功分别留在 `output/ci-theme-heads-20261017/`，不借用为目录修复后的通过证明。后续验收文字的文档提交不修改上述已验证生产输入，不另触发重复 CI。
