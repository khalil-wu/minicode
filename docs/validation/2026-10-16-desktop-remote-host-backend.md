# SSH / WSL desktop host：MiniCode 后端

2026-10-16。机器索引为 `output/source-desktop-remote-host-delivery-20261016/index.json`。本组件在 bundled Python 3.11.9 的两个相关批次中，按 testcase ID 取最新为 **8 个唯一节点，8 passed，0 failed/error/skipped**。没有借用旧提交 CI，也不据此宣布外部 SSH/WSL 或整款桌面通过。

依据为 `output/remote-executor-readonly-20261015/audit.md` 中当前安装版 `26.1007.9446.0` 的 `startRemoteAppServer`、`createSshProxyStream`、host catalog 和 window dispatch 调用链，以及固定 Source `822e58cc3d666166c7446c5b1ea2e52f5d09594c` 的执行所有权规则。桌面 host 和同一 harness 内的 executor attachment 分开：这里运行 **MiniCode 自己的 backend/harness**，没有用 Source app-server 替代。

## 协议与实际行为

已安装入口为 `minicode-host`，对应 `python -m backend.desktop_remote`。`proxy --desktop-id <32hex> --marker <64hex> [--state-root <remote path>]` 建立或接回同一持久 daemon；`serve` 是内部进程入口，`stop` 是明确停止。proxy EOF 只 detach，不停止 daemon。它不会安装或替换外机程序，也不复制任何账号配置。

stdout 先发 32 bytes marker，再发 uint32 big-endian 长度和 UTF-8 JSON，单 frame 最大 1 MiB。ready 给出 MiniCode version/protocol、daemon generation、真实 backend loopback port、独立 runtime/bridge capability、平台与远端路径，并提供 `stream_window_bytes=262144` / `stream_chunk_bytes=65536`。

桌面发起的 stream ID 为递增奇数，host 反向 bridge 为偶数。`open` 的 backend target 固定连接该 daemon 的 backend；preview target 带原 `host` + `port`，host 只接受 `127.0.0.1`、`localhost`、`::1`，保留实际地址族。data 是 base64 原字节；每方向有独立 256 KiB credit，真实 socket drain 后才发 credit。收帧不会等待某一条慢流的 socket drain，因此 ping、其它 HTTP/WS 和反向 bridge 可以继续。

end 表示半关闭，前面的数据写完后才 write EOF；双方结束后发送 close 并删除 stream/任务记录。显式 close/error 释放原流，断开 attachment 关闭其 tunnel 资源。截断 header/body 保留协议错误，不能伪装成正常 EOF。持久 harness 和 native PTY 不属于这条短连接的析构范围。

native 普通操作都经过该 remote host 的真实反向 `/v1/window-binding` 校验。窗口已经 closed 时，普通 binding 会失效；唯独 `window:release` 要求该 daemon 独立 `x-minicode-desktop-lifecycle-token`，以 constant-time 比较 bridge token。仅 runtime bearer 或调用方 body 字段不足以释放窗口；正确 lifecycle authority 只清理指定 window 的 native PTY。桌面 Root 负责密钥不进 renderer、闭窗待释放列表持久化和重连补发。

真实 native PTY 使用当前 host 的 shell 和已有 PTY runtime，读取原 stdout/stderr，按单调 cursor 保留 UTF-8 输出与真实 exit。detach 期间继续缓存输出，重连 snapshot 读回原 PID/记录；window/conversation 不匹配返回 403。没有退化成 pipe 假终端。

## 实测边界

`output/ci-repair-20261015/desktop-remote-backend-batch01.xml`：6/6，75.34 秒。

- 30 条真实 loopback TCP 流并发收发，FIN 后精确字节相同，registry 全清。
- 一条流耗尽 256 KiB credit 时，真实 ping 与反向 bridge 请求/回复仍完成。
- 实际发送截断 header/body，保留原协议错误并释放 attachment。
- 实际 CLI 启动 daemon 与 Windows ConPTY，native 子进程先写 started 文件，断开 proxy 后继续写 finished 文件；重连为同一 generation、同一 shell PID，snapshot 和远端 native FS 读回原输出/文件。测试没有靠终端命令回显冒充执行成功。closed-window secret 的隔离释放和另一个窗口的终端保留均通过；显式 stop 时 proxy stdin 仍开着也正常退出。
- 实际 MiniCode WebSocket 通过真实反向 binding，使用自有 loopback Chat provider 派出 run_command。关闭 WebSocket 和 proxy，等待 31 秒跨过普通 transport 的 30 秒退役边界，再释放原命令；started/finished PID 一致，第二次实际 provider 请求收到原工具 stdout，接回同一 generation 并 session.sync 到原 conversation。没有官方账号或付费模型请求。

`desktop-remote-backend-ipv6-batch02.xml`：3/3，其中 30 流节点与上批重叠，新增两个唯一节点。实际仅绑定 `::1` 的 preview 原 UTF-8 字节和地址族通过；非 loopback target 在入口拒绝且不连接。IPv6 修复也对应 Root JS listener key 和 open frame 保留 host，不能只改一端。

Root 另拥有并验证真实 desktop JS daemon / 8 MiB 传输 / bridge / window 接线；本报告不混加那些结果。独立审阅指出 JS 原 data 异步写可能被 END 越过，Root 已接发送顺序修复并另测大 POST；这里不替代其 JS 验证记录。

## 尚未覆盖或不支持

**Windows resize 仍是实际能力余项。** 当前已有 ConPTY wrapper 不提供 resize 控制通道；本组件返回明确 HTTP 409，并验证这个真实错误。它不是尺寸调整功能通过，也没有声称 Windows resize 已实现。Unix 的 ioctl 分支存在，但本批 Windows 机器没有实际运行 Unix PTY；外部 SSH/WSL 发行版没有被部署或连接，不能记通过。

普通 WS transport 自行 launch 的 UI terminal/preview 继续按已有退役语义处理。本组件没有用 caller 的“remote”标记扩权。持久 agent command、native PTY 与这类 transport 自有资源分别由原 owner 管理。

本批没有重新构建 wheel/安装器，没有修改字体、字号或字重，没有操作用户普通窗口，没有 stage/commit，也没有删除项目 data/ 或清理用户草稿。所有测试 daemon/PTY 通过自己的 stop 生命周期退出；最终组件和证据文件的真实字节身份在机器索引。
