# CI 生命周期与 Linux mount target 收尾

本记录对应 `3ab0f797b102e1bd18c6b43bed8a1912baa90a19` 之后的修复批。冻结基线 `18e8bd8dc097aa892c76ede818beb15d9b8659f1` 的 GitHub run `38749202449` 已七项全部成功。新会话和项目行按钮已在此前批次修复，不能用本批测试数重复计算。

## 真实 CI 取证

GitHub run `38743267369` 的 Linux Root、前端、Electron 集成、Desktop 和原生执行策略构建成功。Backend 6519 项中 6489 passed、3 failed、27 skipped；Windows harness 在第 184 项附近因认证刷新 fixture 的进入等待超时而终止，未执行部分不计通过。

临时 opt-in `ci_trace_session_owners` 插件仅在这个 Backend job 记录创建栈和原回调异常，未改变原回调返回或错误。`github-3ab0f797-backend/pytest-backend-session-owners.jsonl` 证明：

- 同一 `SessionRunManager`（owner `139857664057616`、loop `139857664009360`）来自 `test_long_stream_canonical_chain.py::test_real_image_only_result_keeps_artifact_and_has_no_fabricated_text`。940 次回调发生于原 loop 关闭以后，三个失败节点各一次原样抛出 `Event loop is closed`。
- `test_ws_lifecycle_cleanup_ownership.py::test_cancelled_shutdown_waiter_does_not_dispose_borrowed_generation` 的另一个 owner 留下 80 次已关闭 loop 回调，本次没有成为三个失败节点的抛错来源，仍需由原创建 fixture 正常释放。

原 XML/log、完整创建栈和汇总 JSON 均保存在 `output/ci-repair-20261015/`。定位完成后移除临时 CI 插件接入和脚本，不将 closed-loop 检查、清全局订阅或吞异常加入生产。

两个发源 fixture 现在在原 loop 结束前执行真实 `shutdown_notification_wakes`，取消/失败路径也等待自身 borrower 和 cleanup task。两个完整文件加三个未改失败节点统一 18/18 passed，关闭 loop 回调、回调抛错与结束时遗留 owner 均为 0；不加载插件的原 image→victim 精确组合由 1 passed/1 failed 变为 2/2 passed。临时插件接入和脚本已移除，只在 output 留存原字节。见 `real-owner-fixture-delivery.json`，不靠改生产回调屏蔽错误。

## Linux 沙箱目标

依据为 `.tmp/codex-src` 的 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`，具体为 `linux-sandbox/src/bwrap.rs` 与实际子进程等待/登记生命周期。

旧实现创建工作区内的假普通 metadata 文件，并在 namespace ready 时删除 host target。真实 Linux mount namespace 已复现：删除 backing target 后，子进程对目标的 `stat` 可返回 ENOENT；Git `add -A` 枚举和 stat 之间因此有竞态。前后 CI 中该真实 Git 节点曾失败、也曾偶然通过；不能把单次通过当作根因消失。

Bubblewrap 现独立使用私有 tmpfs/`ro-bind-data`，并把精确 host target 与 FD 持有至真实子进程结束。重叠 runner/进程共享登记，先退出者不能删除后者仍使用的目标；原有真实文件身份与内容不属于清理对象。每个 bind-data 使用独立真实 FD，经正常、interactive 和 PTY 启动链透传。Source 自己使用 `/dev/null` 读取空 bind-data；没有用 host `/dev/null` 的 bind mount 冒充普通文件。

登记目录的只读保护紧跟暴露它的 WRITE bind，后续 parent deny 仍覆盖该目录，不追加重开 denied parent 的尾部 bind。`.gitmodules` 等是 MiniCode 既有 metadata 保护名单，不声称这些名字都来自 Source。

Root 审阅还发现 generic missing DENY 文件漏了 Source 的 `--perms 000`，已在同批补齐；新 Linux 节点同时核对普通文件类型、mode 000、读写拒绝，以及 host 后写入真实内容仍不可见且不会被最终清理删除。

本地最新独立节点为 96 passed、12 skipped、0 failed。9 个新 Linux 实机节点在 Windows 明确跳过，等待 GitHub Linux 真实 bwrap；其余 3 个是原平台 skip。具体哈希、原始结果和 Linux 场景入口在 `git-mount-target-delivery.json`、`git-mount-target-report.md`。

随后真实 run `38746809493`（`d0912a5c`）六项 job 通过；Backend 为 6535 tests、6506 passed、2 failed、27 skipped。原三个通知失败已消失，真实 Git、普通/interactive/PTY 完成、interactive 终止、重叠 runner、registry parent deny 和 generic DENY 实机节点均通过。两处失败来自新专项误以为 `SandboxRunner.run` 在 `Task.cancel()` 后向调用者抛错；生产实际等待进程/输出回收后返回 `SandboxResult(exit_code=-1, cancelled=True)`。这两项原失败保留，不计作通过，按真实契约继续修 fixture，生产取消行为不因此更改。

取消 fixture 已改验精确 cancelled/exit/无错误/无 cleanup pending、原进程 returncode/wait 和真实文件保全，并补一场跨平台真实子进程取消回执验证。本文件本地 8 passed、9 Linux skipped；两个 Linux 取消节点仍等下一轮 CI。上轮实际环境为 Linux 6.17.0-1022-azure、Git 2.55.0、bubblewrap 0.9.0、Python 3.11.17，原 job 日志另存 `github-d0912a5c-backend-job.log`。

最终真实 Linux run `38749202449`：Backend 6536 tests、6509 passed、27 skipped、0 failed/error；本专项 17/17 passed，含全部 9 个 Linux 实机节点以及两个此前失败的取消节点。真实原 XML/log 位于 `github-18e8bd8d-backend/`，逐条 Linux 输出为 `github-18e8bd8d-mount-target-results.json`，七项 job 的原始状态与精确 HEAD 为 `github-18e8bd8d-green-state.json`。没有把 Windows 的平台 skip 或旧失败重新标成通过。

## 桌面 smoke 正常退出

旧 HTTP peer 没有提供真实关闭必经的 `POST /api/workspace/language/lifecycle`。生产窗口资源释放收到 404 后正确拒绝报告关闭成功；测试却等三秒后强杀，并在仅收到主进程 exit 后删除仍可能被 Chromium 写入的 profile，造成 Linux ENOTEMPTY。

只修测试 peer 与 ownership：提供真实 release 响应、核对原 window owner/instance 及两个 token；从 spawn 开始持有 child close receipt。成功要求 code 0、无 signal，只有真实正常 close 后删除该测试自己的 profile。失败 profile 保留，没有 rm 重试、增加等待时间或关闭生产 sandbox。

Windows 完整桌面 E2E 6/6，最后 timer/profile 所有权修改后的受影响 smoke 为 1/1；两组重叠。完整日志中另一个 multiwindow 测试的 Windows node-pty helper `AttachConsole failed` 原样保留，不借主测试通过声称该 helper 单独验收。证据为 `electron-owned-close-delivery-20261015/manifest.json`，Linux 新代码仍待后续 CI。

## 认证取消 fixture

Windows CI 原 fixture 以固定三秒等待进入 authentication refresh；这错误地把 journal/prompt 准备时间计入取消延迟，超时退出后又遗留正在执行的 QueryEngine。现等待实际 refresh 进入或原 query 自身有界终态，原四秒取消/完成断言与两秒 turn deadline 不变；断言失败也结束自己创建的任务与 adapter。

只在 output 中复制旧/新 fixture，并对真实 `_prepare_http_request` 注入 3.25 秒准备延迟：旧 fixture 因原三秒等待失败，新 fixture 通过。该负例原始异步清理报错保留，不算通过。正式认证文件统一 19/19 passed、无 skip；没有修改生产 auth/cancel/retry/deadline。记录为 `auth-refresh-slow-start-comparison.xml` 与 `auth-refresh-barrier-final.xml`。

## 顺序与边界

七项 GitHub job 全部通过后，已开始继续 `2026-10-15-source-internal-evidence-delivery.md` 中确认的 heartbeat、Memory Phase 1 可选 WS 与 remote 范围；这些后续修改须单独验证，不借本次绿色基线宣称交付。只读审计不计实现，官方账号及依赖账号的 hosted 服务继续排除。手动旧产物清理脚本仍留给用户执行，当前 dist、会话、草稿、附件和未保存编辑内容不在清理范围。
