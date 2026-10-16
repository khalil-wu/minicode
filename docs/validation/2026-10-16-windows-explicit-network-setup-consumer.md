# 2026-10-16 Windows 显式网络设置消费链

## 范围和依据

本记录覆盖 `desktop/windows-sandbox-setup.js`、`desktop/scripts/setup-desktop-user-sandbox.ps1`、前端现有 SandboxSettings 按钮和相应必要测试。后端 prerequisite producer、原生 readiness/RequireExisting 实现及构建由 environment_harness 负责；远端 IPC 拒绝分支由 Root 实现。

固定 Source：`822e58cc3d666166c7446c5b1ea2e52f5d09594c`。依据是 `windows-sandbox-rs::WindowsSandboxProvisioningSettings` 的两个字段、真实 managed ingress 的非零实际端口，以及 Source setup marker 与 offline proxy settings 的比较。原有按钮总使用默认空 ports/false，会使当前代理配置与 marker 不一致；执行时隐式 reconcile/UAC 不是已启动 workload 的证据。

## 完整消费行为

- 原有无参数主进程 IPC 保持不变。按钮调用后由主进程携 runtime token 读取认证 `/api/sandbox/status`，renderer 不能传递 backend、home、executable 或权限 JSON。
- 当 `network_provisioning.required=true`，只用 nested 的实际 `sandbox_home`、`sandbox_executable` 和 `settings`。缺少原 owner/runtime 不退到无关 top-level 值。
- 进入提权进程的网络设置只有 Source 的 `proxy_ports` 与 `allow_local_binding`。API/提权脚本两个跨进程边界分别检查实际非零 u16 端口和 bool，拒绝额外文件系统等权限字段。
- 设置以 UTF-8 JSON 的 Base64 穿过 Start-Process 参数层。脚本重建 Source 两字段 JSON，处理 Windows PowerShell 5.1 native binder 的 JSON 引号规则，再调用 `sandbox setup --elevated --user USER --codex-home HOME --settings-json JSON`。
- Installer 和没有当前 network prerequisite 的 base filesystem 初始化保留 Source 默认设置，不虚构代理端口。
- 界面仍用现有按钮、状态和检测详情；网络 prerequisite 没有确认时不能显示 ready。setup 子进程 exit 0 之后仍重新读取认证 status，只有实际 native/filesystem/network 都确认后才刷新工具能力。
- 远端 Windows 窗口的原按钮禁用并解释应在远端主机设置；remote Linux 不显示 Windows 设置。Root 的主进程 IPC 也拒绝 remote window 初始化本机，renderer payload 不能改变此归属。

## 只读验证

| 记录 | 结果 | 证明范围 |
| --- | --- | --- |
| `windows-explicit-setup-desktop-batch01.xml` | 38 pass / 0 skip | setup JS 与现有 security 回归；认证 status、实际 owner/runtime/settings、UAC 完成/取消/错误结果、严格字段、无参数 IPC |
| 同上 PowerShell native argv 三节点 | 3 pass，计入上述 38 | 真实 Windows PowerShell 5.1 仅启动 Node 参数回显 stub：base argv、network JSON 完整传递、额外 filesystem 字段在启动前被拒绝 |
| `windows-explicit-setup-remote-batch02.xml` | 新增 1 pass；其它节点仅按 name pattern 未执行 | 真实 VM 加载现有 IPC handler，remote window 的 setup 请求连同伪造 home/settings payload 都不能调用本机 setup callback |
| `windows-explicit-setup-frontend-batch01.json` | 14 pass / 1 fail | 新增远端说明未加入严格翻译表导致真实渲染错误；初败保留 |
| `windows-explicit-setup-frontend-batch01r1.json` | 受影响 1 pass；其它 14 仅未选中 | 补中英原生翻译 key 后，仅重跑失败节点；不计未选中节点为新增 skip/pass |
| `npm exec -- tsc -b --pretty false` | exit 0 | 前端完整类型检查，无 bundle 重建 |
| `windows-explicit-setup-producer-consumer.json` | pass | 使用 R0 实际只读 Source producer 的 `network-prerequisite-readonly.json`，经自有认证 HTTP status 到 desktop，确认实际 ports/binding/home/exe 完整进入 argv 检查 callback；未启动 PowerShell 或 native setup |

现行节点合并后：**desktop/security 39 pass；frontend 15 pass；类型检查通过**。历史重复和未选中的节点不重复相加。

交付索引：`output/source-windows-explicit-setup-delivery-20261016/index.json`。

## 明确未执行

没有执行真实 `codex.exe sandbox setup`、Start-Process RunAs、账号创建、ACL/防火墙修改或自动提权。PowerShell 测试的 RuntimePath 是 Node，只回显 argv。

这份只读交付不把此前两个 Windows HTTP POST allow/deny 的 skip 改成通过；它们仍需要用户明确完成真实系统 provisioning 后独立验收。后端/native 当前 workload 准入和 UI/桌面实际运行也须引用各自验证记录，不能用此处参数回显冒充系统设置已经完成。
