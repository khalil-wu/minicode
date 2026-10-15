# CI 异步所有权与取消计时边界

远端 `935a3f46204f5b31143828e9ae72a5bd46062539` 的完整 run `38770182075` 为六项成功、一项失败。Frontend 为 4263 passed、1 failed；失败是 `SidebarLeft.test.tsx` 从 Code 新建聊天的测试将冷编辑器恢复产生的新数组引用误归给新建聊天。原始日志保留于 `output/ci-repair-20261015/github-935a3f46/frontend.log`。不把这个提交记为全绿。

该测试现在等待真实的 `waitEditorDraftRecovery()`，先严格核对恢复后的完整 tab 内容，再冻结引用、触发新建聊天并核对引用不变。未保存正文、原正文、活动路径、面板引用、已有会话与草稿的原断言全部保留；没有将引用断言改成宽松比较，也没有修改生产代码。

此前 `5edd26a14eb7d01256a10cf0c870defc0dc34a32` 的 run `38766634628` 仅 Windows harness 失败：busy JavaScript 测试把 context 构建、磁盘记录及 teardown 的总耗时当作取消耗时，实测总计 4.329 秒，超过原 3 秒断言。原始日志保留于 `output/ci-repair-20261015/github-5edd26a1/windows.log`。后续 run 中该 job 成功，不能抹掉原失败。

现在这个测试确认真实 `CodeVM.step("start")` 已进入、Python continuation 在 busy JS 期间仍可运行，随后发出实际 `tool_wait(terminate=True)`，从该请求到原工具终态事件计时。保留小于 3 秒和真实 cancelled 的要求，排除取消区间外的准备及 teardown；没有延长 VM 超时或取消限额。

本地必要验证使用当前工作区：SidebarLeft 41/41；bundled Python 3.11.9 的 busy V8 场景 1/1。证据为 `sidebar-recovery-owner-20261016.log`、`busy-v8-cancel-timing-20261016.xml/.log`。后端测试工作区含未提交 remote 实现，不能据此声称冻结提交完整通过；提交后的 GitHub 全量结果另记。未提交 remote 范围不属于此修复提交。
