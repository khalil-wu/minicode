# 剩余浏览器原生能力的可实现路线

这份记录给出下一阶段的真实引擎入口，不代表已经交付 BR-G1–G10。当前 stock Electron 43.4 仍缺相应 browser-mode服务；本批没有用同名设置、脚本或URL过滤冒充。

固定审计对象为 CEF `150.0.22+gc1a097e+chromium-150.0.7871.256`，源码commit `c1a097eaa7738483abe2ea9d6297a937d6a884c0`。它与现有Electron的Chromium `150.0.7871.224` 是不同patch。CEF源码和二进制允许按其许可条件再分发，不能把这一点扩大成所有附属在线服务已经可用。

## 已证实的底层入口

| 行为 | 实际源码链 |
|---|---|
| 原右侧栏内嵌候选 | [delegate工厂](https://github.com/chromiumembedded/cef/blob/c1a097eaa7738483abe2ea9d6297a937d6a884c0/libcef/browser/browser_platform_delegate_create.cc#L78) 对Chrome style与parent HWND选择ChromeChildWindow；[child实现](https://github.com/chromiumembedded/cef/blob/c1a097eaa7738483abe2ea9d6297a937d6a884c0/libcef/browser/chrome/views/chrome_child_window.cc#L227) 创建真实BrowserView与指定bounds，默认不显示Chrome toolbar。不能简单断言只有Alloy可作为子窗口。 |
| 内容设置 | CefRequestContext的Get/SetContentSetting及observer实际写Profile的HostContentSettingsMap；Chrome原生settings WebUI有对应服务，而非一组空枚举。 |
| 后台同步 | ProfileImpl有BackgroundSyncControllerFactory与真实controller，观察被阻止站点并取消注册。仍需真实注册、离线/联网、宿主生命周期验证。 |
| 多下载限制器 | ChromeDownloadManagerDelegate → DownloadRequestLimiter → CEF CanDownload可先否决 → CanDownloadImpl继续原initiator/origin/count/permission链；CEF回调没有取代真实limiter。 |
| 密码服务 | ChromePasswordManagerClient、ProfilePasswordStore、LoginDatabase/OSCrypt及原生save/update bubble存在真实委托；必须是持久非OTR Profile，不能使用会禁用保存UI的enable-automation旗标验收。 |
| 权限与popup | 有原prompt_id/origin/callback及popup frame/user_gesture/create/abort事件；公共自定义prompt不包含Codex私有完整presentation/variant/availableActions或AcceptThisTime数据。不能制造这些字段。 |

## 尚未完成的集成

CEF支持接收parent HWND，并不等于已经验证跨进程Electron宿主中的DPI、IME、焦点、遮挡、拖动、原生气泡和销毁归属。现有Electron webContents/session也不会自动变成CEF browser/request_context，既有控制、下载、权限、tab/owner事件必须贯通。

关闭归属尤其需要在原位原型中验证：公开Chrome CloseBrowser先走TryCloseBrowser/TabStripModel；另外某些WindowDestroyed/platform helper分支会对GA_ROOT发送WM_CLOSE。不能据此直接断言普通CloseBrowser必然关闭Electron，也不能跳过该分支、通过吞消息掩盖未查清的归属。

下一步最小原型应由独立native helper承载Chrome style与匹配的sandbox/bootstrap，以原Electron HWND和右侧栏矩形为唯一嵌入目标。先证明无额外主窗口、关闭pane不关闭owner、持久Profile，再验证同pane原生settings/password/download页面、真实权限、多下载和暂停恢复。只在独立Chrome窗口或Alloy下成功不满足目标。

在线信誉、广告规则或其他provider服务仍需各自真实依赖与回执，不能由普通下载成功或设置页可打开推导。原生默认Chrome UI可作为验证路线；若需要投影Codex私有prompt元数据，还须评估引擎扩展。

本批只读收集小型公开源码，没有下载大型SDK、构建、启动浏览器或修改用户runtime。完整27处引用、103份源码清单和分发/服务边界在 `output/full-parity-20261021/cef-native-route-audit/report.md`；其数量不作为产品完成率。
