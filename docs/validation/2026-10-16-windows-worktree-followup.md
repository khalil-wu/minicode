# Windows 工作树权限与模型快照后续修复

本批修完整执行器消费链，没有切换到控制端、无沙箱或另一执行器来制造成功。没有手工改写 ACL、账号、provisioning、用户项目路径或普通窗口状态。

## 修复后的行为

实际 `process.cwd` 与捕获的 `sandbox.policyContext.cwd` 分别传递。读取 linked checkout 不再覆写原策略目录，也不再因隐含 cwd 写权限而错误触发 split writable roots 拒绝。

固定 Git 读操作只读；stage、commit、branch 等 metadata 操作只写经双向注册关系核对的 common Git metadata。工作树 add/remove/restore 额外声明其已验证目录池。原对话权限和主仓库普通文件均不被放宽。真实普通 workspace-write 命令先留下 metadata SID 拒绝后，新的 stage、commit、branch 仍已实测成功。

提交与 PR 正文的临时文件、快照 index 采用本次动作实际允许的 metadata 目录、原执行器和原清理租约。模型 `worktree_snapshot`、`list_worktree_snapshots`、`worktree_restore` 复用既有 GUI 事务和 Git refs，移除其控制端 TEMP index/JSON store 消费；没有执行器的原本地路径保留。快照与恢复保留原 index、LF/CRLF、二进制及 ignored 文件字节。

## 验证与边界

集中批次 `final-05.xml` 为 **76 passed / 1 skipped / 0 failed**；原 skip 是本用户缺少创建 Windows symlink 的权限，未改为通过。其后将显式快照 inventory 移入既有 mutate 锁，当前源码的模型完整回归、retarget commit/branch 和 GUI 权限三项在 `final-06.xml` 全部通过；不称为重新执行过整个集中批次。

模型回归将旧 snapshot/list/restore 消费者设置为一旦触达即失败，并核对中文 worktree、原 index bytes、LF/binary/ignored 字节、GUI/模型共用 ref、删除恢复和 metadata index 清理。原始失败、测试目录被识别成主仓库的 fixture 布局错误及后续修正分别保留在 `output/windows-worktree-followup-20261016/validation.json` 和 `followup-report.md`。

原“Unicode 受限写入失败”现已排除编码归因：自有 ASCII 路径 12/12、自有 Unicode 路径 18/18 写入成功；旧私有 pytest Temp 路径 18 次真实 OS5 保留。只读 `CreateFileW` 和内存 `AccessCheck` 对照确认，原 Source restricted token 与私有目录的 OWNER RIGHTS ACL 是拒绝原因；没有把内存对照 descriptor 写回磁盘。这仍是原生边界，不能说 MiniCode 已支持所有私有 Temp 写入。

Unelevated deny-read 也保留原 Source 的明确拒绝。测试同时验证组合后的 DENY 不丢失、原执行器拒绝且文件未改；拒绝通过不等于已实现 deny-read 隔离，该能力仍需适用的 elevated backend。

实际 installed Source 为 `0.162.0-alpha.17.2`，exe SHA256 `e98c516f1c8935789aabe9122ee7e05ec4c3efd1ba6acf179242e031c495b99e`；只读参考 checkout 仍为 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。版本和原生归档身份不混用，源码冻结见同证据目录的 `source-freeze.json`。
