# ASGI transport 与执行 core 的关闭归属

GitHub `2b59e6ab` 的 Root suite 已完整运行：1807 tests，1715 passed / 41 failed / 51 skipped。33 个失败在应用关闭时再次关闭已退休 WebSocket；其余 8 个来自另一个测试留下的 FileWatcher，详见 `2026-10-15-watcher-fixture-owner.md`。两类不能靠 closed-loop 兜底或跳过失败处理。

## 根因与修复

CI 安装 FastAPI 0.143.1、Starlette 1.8.0、AnyIO 4.15.1；bundled runtime 为 FastAPI 0.115.6、Starlette 0.41.3、AnyIO 4.15.1。新 TestClient 会真正关闭 AnyIO memory streams，而 ASGI WebSocket 的两份 CONNECTED 状态可以保留旧值。执行 core 在 transport 退休后仍可保留，因此不能从这两份状态反推连接仍归执行 host 关闭。

`shutdown_execution_host` 现在仅捕获当时仍挂载且真实连接的原 WebSocket，并只关闭这份 transport。原来的 session shutdown、task drain、pending 检查、journal persistence 与 core release 全部保留。没有添加 catch、改写 Starlette 状态、关闭 sandbox、降级依赖或忽略未完成清理。

另两处测试使用已移除的 TestClient 私有 `_send_queue`。它们改为公共 `receive_json` 消费真实协议，无版本兼容分支，原消息顺序与持久化断言保留。

## 证据

冻结交付为 `output/ci-repair-20261015/asgi-transport-owner-delivery-20261015/manifest.json`，含三份源码原字节、SHA、命令与全部 XML/log。隔离的新依赖安装在 `.tmp/ci-asgi-0143-deps`，没有修改 bundled runtime、用户 profile 或未保存内容。

- 最小回归使用真实 Starlette WebSocket 和真实 AnyIO stream：旧实现的活跃连接对照通过，已退休但状态仍 CONNECTED 的分支稳定抛 `ClosedResourceError`；公共接收的会话单例独立通过。原结果为 1 failed / 2 passed，未把它记成通过。
- 修改后，在 CI 同版依赖下，execution-lifetime/cold-connection/retirement-handoff/conversations 四文件 **108/108 通过，0 skip/error**。
- bundled 旧依赖下，同样四文件 **108/108 通过，0 skip/error**。两种运行环境分别留证，不相加为更多场景。
- 两种依赖均只有已记录的导入弃用警告，没有未处理任务或 unraisable；`git diff --check` 通过。

这些必要验证不替代后续 HEAD 的 GitHub 全量结果。backend 仍有另外的跨循环 fixture/真实 Git sandbox 失败在单独收敛；CI 全通过后才继续 Source 内部证据交付文档的剩余实现。
