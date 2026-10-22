# CEF Chrome style 原位嵌入的云端实测

已验证提交为 `5dfaaca641bb57bd4cc6782e9110e258e355bb94`，独立 [run 39716892614](https://github.com/khalil-wu/minicode/actions/runs/39716892614) 为 **success**。Windows runner 实际编译 sandbox bootstrap DLL、启动 Electron 原宿主和跨进程 CEF Chrome style child，完成两种缩放的写入、关闭、接收端退出及新进程读取。本批没有操作本机 GUI，也没有替换 MiniCode 的生产浏览器。

CEF 固定为 `150.0.22+gc1a097e+chromium-150.0.7871.256`，源码 `c1a097eaa7738483abe2ea9d6297a937d6a884c0`；Electron 为 `43.4.0`。两者 Chromium patch 不同，不描述成与 Codex 私有浏览器二进制相同。完整收据、job log 和原始 artifact 位于 `output/full-parity-20261021/cef-native-route-audit/ci-39716892614/`。

## 保留的失败轮

| 提交 / run | 实际结果与修正 |
|---|---|
| `8488c347` / `39641136413` | 原生编译通过；每种缩放各35个检查中33通过，均在 loaded/resized 的 sidebar bounds 失败。Chrome child 原位置为0，未进入 read 重启阶段。随后在 Chrome widget 创建后的 UI task 及 resize 中，对原 child HWND 使用原 owner 的物理 client rectangle 调用 `SetWindowPos`。 |
| `f2d8f0c8` / `39642693957` | 1×写读78/78通过；1.5×写入37/39通过，仅高度固定788px的两个 bounds 检查失败，未执行1.5× read。两个1× helper 及1.5×原 helper退出后，原 owner 均存活。 |
| `946c652e` / `39715816287` | 原生编译通过；显示环境准备失败，尚未启动 CEF。本轮实际测得屏幕1024×768、工作区1024×728、`SM_CYMAXTRACK=788`，与此前被限制的高度一致。真实枚举含1600×1200和1920×1080，没有当时过度要求的1920×1200。随后仅修正显示条件，使其围绕原子窗的1008×1140和实测工作区边距，不要求整个 owner 在单屏内。 |

上述失败日志和 artifact 保留在各自 `ci-<run>/` 目录，未改写成通过。最后一轮没有缩小原 owner 请求尺寸、删除 bounds/DPR/focus/close/profile 断言、更换 Alloy/独立 Chrome 窗口、关闭 sandbox 或提高45秒 phase期限。

## 最终显示事实

从真实枚举中选择一个满足原子窗尺寸及实测工作区边距的模式，`ChangeDisplaySettingsEx` 的 `CDS_TEST` 与实际 apply 都返回0。没有更新注册表或失败后尝试第二个模式。

| 实测状态 | 屏幕 px | 工作区 px | MaxTrack px |
|---|---|---|---|
| 原 runner | 1024×768 | 1024×728 | 1044×788 |
| 单次实际 apply 后 | 1600×1200 | 1600×1160 | 1620×1220 |

原 `owner.setContentSize(1000,700)` 与 resize 的 `(1120,760)` 调用保留。此条件用于原子窗 geometry 和进程重启验证；owner 可以超出屏幕，不代表完整 owner 或 pane 已做屏内像素截图验收。

## 最终原生结果

| 缩放 | phase | 真实检查 | helper PID / exit | 原 owner 存活 |
|---|---|---|---|---|
| 1× | write | 39/39 | 3096 / 0 | true |
| 1× | read | 39/39 | 3080 / 0 | true |
| 1.5× | write | 39/39 | 8020 / 0 | true |
| 1.5× | read | 39/39 | 2620 / 0 | true |

每种缩放为78/78，两种合计156次实际断言；其中包含重复行为，不能当作156项独立功能或产品覆盖分母。两个 `runs.json` 条目均为 exit0/passed，四个 phase 均收到原 `OnBeforeClose`、原 helper exit0，以及退出后 `ownerAlive=true` 和 `ownerSurvived=true`。

实测 child 的 loaded → resized 物理矩形：1×为 `(400,0,600,700)` → `(448,0,672,760)`；1.5×为 `(599,0,900,1050)` → `(671,0,1008,1140)`。1.5×原 owner client 宽实际为1499/1679px，断言始终依据当时原 HWND 的实际 client rectangle 核对右侧60%，没有把测量结果替换成理想整数。Chrome runtime style、跨进程原 parent/root、WS_CHILD、native/document focus、renderer DPR、viewport以及无额外可见 top-level 均通过。原生 DPI 实测仍为96；forced DPR 1.5不等于物理 DPI 切换。

每种缩放独立使用 `scale-<n>/cef-profile/Sidebar`。同一缩放的 write/read 是不同 PID，读取同一个真实 cache path；第二进程没有重写随机 localStorage token或content settings，实际读回默认 notification block、站点 popup allow及原 token。这个结果不依赖重新创建假的 profile 数据。

## 尚未实施的范围

- 原型未接入生产 BrowserPanel、agent浏览器桥、tab/下载/权限/密码消费者，不能关闭 BR-G1–G10。
- 物理跨屏 DPI、真实键鼠与 IME、pane隐藏、owner主动关闭、原生 password/permission/download UI 尚未实施；强制缩放和上述 geometry 不能代替这些验收。
- 未做当前 MiniCode 的像素/hover/多层菜单对照，未证明 Codex 私有 permission prompt 元数据、推荐分类或在线服务等价。
