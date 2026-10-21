# 浏览器 BR-G1–G10 原生 producer 与 Electron 43.4 能力核查

初次核查只读当前 MiniCode、已保留 Desktop Source、当前安装 ASAR 和 Electron 的公开版本源码。**没有操作桌面实机，没有 GUI、computeruse、Playwright、CDP、浏览器请求或物理输入验收，也没有修改后端和小时任务输入。**原始分类是源码与 API 合同证据；文末另记后续消费者修复及云端 CI 的实际边界。

原清单为 `output/full-parity-20261021/browser-audit.md`。十项仍按完整功能组理解，不能把已具备的下载、Cookie 存储、通知权限或密码管理全部说成缺失；也不能将局部 API 的存在扩大成整个功能组已闭合。

## 版本与真实 Source 设置链

本地 `desktop/node_modules/electron/package.json` 为 **43.4.0**。除了本地 `electron.d.ts`，本轮读取官方 `electron/electron` 的精确 tag `v43.4.0`；其 `DEPS` 固定 Chromium **150.0.7871.224**，相关 Chromium 代码也按该版本读取，没有使用 Chromium main 替代版本合同。

原 Desktop `26.1013.4894.0` 的 `main-C4DYk222.js:1577442` 使用 `session.fromPartition(...,{permissionHandling:"browser"})`、`setPermissionPromptHandler` 和 `setOpenURLHandler`。`RNe:2001814` 消费真实 native prompt 的 `presentation`、`variant`、`availableActions` 和原始生命周期。当前安装 `26.1016.6573.0` 的 `main-CT5a1yCd.js` 仍含相同私有 API 与 prompt 投影。它们不在 stock 43.4 的 `Session` JS binding 中。

`browser-use-settings-9e983558442e.js` 直接挂载原生 settings webview；`Zi:56322` 与 `ea:57885` 处理 `chrome://settings/content`、`chrome://settings/cookies`、`chrome://settings/handlers` 的真实路由。当前安装 `browser-use-settings-fa999befd919.js` 保留该 `chrome://settings/content` 链。原生 Chrome settings 页承担内部 content-setting 与浏览器服务消费，不能靠在 MiniCode 增加同名 React 行复现。

当前资产片段、原始字节 SHA、本地文件 SHA、12 份精确版本上游源码及链接保存在 `output/full-parity-20261021/electron43-browser-api-audit/upstream-manifest.json`。本轮没有加载或执行 ASAR bundle initializer。

## 四个优先项

### BR-G3：Background Sync 是实际 controller 缺口

不能仅据 d.ts 没列出名称得出不支持的结论。精确 Electron `shell/common/gin_converters/content_converter.cc:169` 将真实 `BACKGROUND_SYNC` 映射为 `background-sync`，`:191` 也有 `periodic-background-sync`。这证明真实权限类型转换存在，不证明当前 runtime 能成功注册同步任务或必然发出该回调；不能靠内容脚本制造权限事实。

但同版 `shell/browser/electron_browser_context.cc:655` 的 `GetBackgroundSyncController()` **直接返回 nullptr**。更关键的是固定 Chromium 的 `content/browser/background_sync/background_sync_manager.cc:231` 明确在 controller 为空时设置 `parameters->disable=true`，`:719` 再据此将 manager 停用。完整后台同步调度缺失有精确 native 证据，不只是类型声明缺少一项。

MiniCode `permissionCategories()` 目前也没有 `background-sync`，未知类别被拒绝。因此只将它加入 `SITE_CATEGORIES`、保存 allow/block 和返回 callback(true)，仍不能让缺失的 manager 调度、重试或唤醒产生。

可执行的完整方向是原生运行时工作：提供并接入真实 `BackgroundSyncController`，将原 origin/registration 的权限与持久调度、网络恢复、重试和应用生命周期连通，再将实际权限请求接入现有 page owner 的 callback。不能删除 service worker、运行页面定时器或自行发送普通 fetch 冒充。验收应包含真实离线注册、恢复联网后的真实 sync event、block 后拒绝、其他 worker 功能不受影响；stock 43.4 的 JS 修改无法独立完成这一组。

### BR-G7：网站协议注册没有可用的 stock 请求 producer

固定 Chromium `content/public/browser/web_contents_delegate.h:570/578` 的 `RegisterProtocolHandler` / `UnregisterProtocolHandler` 默认实现为空；它们原本带有真正的 requesting frame、scheme、URL 与 user gesture。精确 Electron `api/electron_api_web_contents.h/.cc` 没有覆盖这两个入口，JS binding 和本地 d.ts 也没有对应的网站注册请求 API。

`app.setAsDefaultProtocolClient` 是应用成为 OS 协议客户端；`session.protocol.handle` 是应用安装 URL loader handler。它们均不产生网站调用 `navigator.registerProtocolHandler` 的原 frame/gesture/permission 事实，也不提供网站注册、撤销、默认选择与例外目录，不能替代 G7。

完整方向是从该 native delegate 入口暴露真实注册和注销请求，将原请求交给对应页面的权限生命周期，再接入真实协议注册表、持久选择、撤销与 scheme 导航分发。Blink 的安全 origin、模板与 scheme 校验继续由实际引擎执行。未取得该 producer 前，不能通过覆盖 navigator 方法、监听 URL 黑名单或让 MiniCode 成为 OS handler 宣称完整支持。

### BR-G8：通知权限已消费，推荐呈现的分类仍缺

当前 MiniCode `embedded-browser-manager.js:839` 配置真实 `notifications` check/request handler；`:394–450` 保存原 callback，并在隐藏、恢复、导航、关闭及返回响应时维持原 page owner。ask、allow、block、单次 document grant 与实际 native 权限结果均已有消费。它不是“完全没有通知”。

stock 43.4 的原生 permission manager 将请求 URL、isMainFrame 和 permission 传给 JS，响应是实际 permission status；没有 Desktop prompt 的 presentation、variant、availableActions 或推荐 quiet/expanded 分类。Source 的这组值由 `RNe.show()` 直接从 native prompt 取得，当前 app 不是靠三个选择按钮推断。

可在现有真实 pending callback 上完整实现手动的折叠/展开交互与焦点、生命周期；这是 UI 消费工作。**recommended 模式的真实分类不能由该 bool callback 完成**。Electron 主进程 `new Notification(...)` 控制 OS toast，也不产生通知权限询问的推荐分类。若完整保留三种 Source 策略，需要先有原生浏览器 prompt / quiet UI 分类 producer；不应把缺失分类伪造成已通过。

### BR-G4：储存与 Storage Access 授权不是第三方 Cookie 策略

MiniCode 已有真实 Cookie 计数及 cookies/site-data 清理。stock 43.4 的 check/request API 也明确包含 `storage-access`、`top-level-storage-access`；check details 提供真实 requesting origin 与 embedding origin，可用于该授权边界。但 MiniCode 当前 `SITE_CATEGORIES` 不包含二者，进入通用权限路径时被拒绝。

这两个权限可作为独立的真实 Storage Access 授权消费者补齐，必须保留原 callback 与 origin 关系；它们不会自动实施“允许/阻止第三方 Cookie”的全局默认、双 origin 例外或 partitioned cookie 规则。普通 `cookies.get/set/remove` 是存储管理 API，本地 `Cookie`/`CookiesSetDetails` 也没有完整的 partition-key 策略接口。

精确 Electron `shell/browser/net/network_context_service.cc:79` 创建原生 `CookieManagerParams`，但 `Session` 的公开 JS binding 未提供该层的第三方 Cookie/content-setting 配置消费者。完整方向是在实际 network cookie manager 层接入第三方默认、origin 例外与 partition/storage-access 状态，并提供真实持久设置读写与变更通知；继续使用 Chromium 的实际 site-for-cookies、SameSite 和 partition 计算。不能通过 Cookie/Set-Cookie header 剥离、拦截 document.cookie 或替换整套 cookie 内容模拟。

## 十组的当前分类

| ID | 已有 MiniCode 消费 | 剩余完整链路与结论 |
|---|---|---|
| BR-G1 Intrusive ads | 普通页面导航与资源请求照常消费 | 没有引擎 intrusive-ad 分类及其 content-setting producer。没有证据表明 stock API 能以 JS 补齐；URL 黑名单不等价。 |
| BR-G2 Automatic/multiple downloads | 真实 DownloadItem、控制与原 owner；精确 URL/blob agent admission 已有独立链路 | 仍缺网站 multiple-download limiter 的原 request initiator、队列与待决定交易 callback。will-download/hasUserGesture 不等于完整 limiter；不重复计算普通下载为缺失。 |
| BR-G3 Background Sync | Chromium 权限名称有真实转换；Mini 当前不接此类别 | controller 缺失导致固定版本 manager 真正停用，须原生运行时链路。 |
| BR-G4 Third-party cookies | 真实存储/计数/清理；stock 提供 Storage Access 权限 API | 可补独立授权，但完整第三方 Cookie 默认、例外、partition 策略缺公开 binding。 |
| BR-G5 Federated/embedded identity | 普通嵌入页面可加载 | 未有完整 FedCM/embedded identity 的设置及权限服务消费者。Storage Access 或阻断身份 URL 不能替代身份协议服务。 |
| BR-G6 Popups/redirects | 公共 createWindow 采用原 Chromium guest；无 guest 的 OpenURLFromTab 按原 HandlerDetails 首次导航；background 选择和关闭 opener 后保留原子页按 Source 消费；原 redirect 交易继续/取消已接通 | 已实现的消费者不等于整组原生验收。仍缺 Source 内容策略的真实触发分类及 blocked-popup 展示/再次打开链。stock 内部 -add-new-contents 确有 user_gesture，但它不是已交付的完整公共 policy/blocked-request API，不能仅依据 disposition 拦截。 |
| BR-G7 Website protocol handlers | 应用级 OS/URL loader 能力不计为网站注册 | native Register/Unregister delegate 默认空且 Electron 未覆盖；须真实注册 producer 与 registry。 |
| BR-G8 Notification presentation | 实际权限请求与页面生命周期已接通 | 手动呈现能接现有 callback；recommended 分类与完整 native prompt metadata 仍缺。 |
| BR-G9 Password surfaces | 加密管理、原 frame/form 填入与可信显式提交 Save/Update 已有 | stock d.ts 无完整密码管理器的成功登录、保存 bubble、建议和生成服务。Electron 内部 AutofillDriverFactory 的存在不能证明这些服务已暴露；当前未取得完整 public consumer。 |
| BR-G10 Modifier routing | 主文档 HTML/SVG 链接的原 trusted click 与 modifier 已消费 | 43.4 的 before-mouse-event 与 will-frame-navigate 是真实 API，但没有把某次原 modifier 关联到任意 iframe/script 导航的 Source 交易字段；不能以“下一次导航”或时间窗口猜测。该组仍待可信 producer，未在本轮声称原生不可能。 |

因此本次没有发现能在不补原生 producer 的情况下，仅增加设置或内容脚本就完整闭合某个上述完整组的方案。可以立即规划的真实局部消费者是 Storage Access 授权及通知手动呈现；它们应保留各自明确范围，不能据此删除 G4/G8 的完整边界。

## 后续实施顺序与验收要求

先决定原生运行时路线与明确负责的 producer 合同，优先 G3 controller 和 G7 register/unregister delegate，随后是 G4 network cookie policy 与 G8 native prompt metadata。每条路线都应把实际原请求、origin/frame、owner 生命周期、持久设置和页面投影一起列入实现范围。现有 MiniCode UI/权限保存可作为消费者复用，不需要创建另一套平行状态或补 Source 没有的控制按钮。

初次核查没有运行产品测试，没有把官方版本源码检查计为本地 Electron 行为通过。最终需要隔离实际 Electron 页面验证、权限/设备验证及 UI 截图；当前普通用户桌面保持未操作。

## 后续：真实 child adoption 与 OpenURLFromTab 消费者修复

本节依据 Electron **v43.4.0** 官方调用链，不把现成能力缺少消费者误记成运行时缺少 producer：

- [`lib/browser/api/web-contents.ts:606–642`](https://github.com/electron/electron/blob/v43.4.0/lib/browser/api/web-contents.ts#L606) 把实际 `OpenURLFromTab` 的 URL、referrer、原 POST 数据组成 `HandlerDetails`。[`guest-window-manager.ts:69–103`](https://github.com/electron/electron/blob/v43.4.0/lib/browser/guest-window-manager.ts#L69) 在 custom `createWindow` 后直接返回；有 guest 时必须返回同一对象，无 guest 时消费者负责首次导航。官方 [`web-contents.md:1510–1513`](https://github.com/electron/electron/blob/v43.4.0/docs/api/web-contents.md#L1510) 也明确说明 background-tab 可能没有 `options.webContents`。
- 有 guest 时，MiniCode 采用原 `options.webContents`，不调用 `loadURL`。无 guest 时只发起一次 `loadURL`，传回原 referrer 对象、原 `postBody.data` 数组、原 content type 和 multipart boundary；不把 POST 改为 GET，不自行复制上传字节，不制造 opener。首次请求仍进入原 entry 的 `onBeforeRequest` 私网审批 callback。
- `WebContentsView` 的公开构造选项在没有 guest 时必须省略 `webContents` 字段，不能传 `webContents: undefined`；精确 [`electron_api_web_contents_view.cc:191–202`](https://github.com/electron/electron/blob/v43.4.0/shell/browser/api/electron_api_web_contents_view.cc#L191) 校验真实已有对象，`gin_helper::Dictionary::Get` 区分未提供字段与显式 undefined。MiniCode 只转换这两个真实构造分支。
- 已保留 Source `main-C4DYk222.js` 字节位置 **1927417** 附近设置 `outlivesOpener: true`，并给 background-tab 发送 `active: false` 和 `insertToRightOfTabId`。MiniCode 将这三个事实传到已有 `BrowserPanel`：新页插在来源旁，后台新页不改变当前页、地址草稿或会话，原生加载更新不重放导航。没有新增 UI 控件。

推送 `4356b948` 的云端 Desktop 已实际观察到原生 POST 到达、原 child 对象相等及原 opener 保留。该次运行随后停在 fixture 读取 `getLastWebPreferences().preload`：精确 [`web_contents_preferences.cc:364–383`](https://github.com/electron/electron/blob/v43.4.0/shell/browser/web_contents_preferences.cc#L364) 的 `SaveLastPreferences` 白名单本来就没有该字段；`:231–235` 仍接收并保存绝对 preload 路径，`WebContents::GetPreloadScript` 继续消费它。fixture 因此改用原 POST child 的实际 native modifier click → 现有 `minicode:browserLink:click` → `manager.handleLinkClick` 验证真实 preload 执行、sender/frame/owner 和原 URL，继续保留 sandbox、contextIsolation、nodeIntegration 的真实断言。后续 `3d53dd86` 的原日志记录了正确 DOM trusted ctrl-click 以及产品的 `ReferenceError: SVGAnchorElement is not defined`；精确 Chromium 150 的 [`svg_a_element.idl`](https://github.com/chromium/chromium/blob/150.0.7871.224/third_party/blink/renderer/core/svg/svg_a_element.idl) 暴露的是 `SVGAElement`。产品两处判型及单测中虚构的同名全局已直接修正，没有添加兼容兜底或删除 preload 功能验收。

新增云端 fixture 还保留原 POST、opener、HTTP 下载及动态 blob 断言，在原页对真实链接发出 native middle input，核对实际后台 HTTP 请求、原 referrer、一次首次导航、没有虚构 opener、原活动页与会话，并检查关闭来源后原子页仍存在。它没有在本机运行；本轮本机仅静态阅读与非 GUI 单测。

本批非 GUI 验证：desktop browser/lifecycle/bridge/wait/link/password 六组文件 **92/92 通过、0 skip**；BrowserPanel 与 nil-owner 两组 **76/76 通过**；`tsc -b --pretty false`、manager/capture 语法检查及本批 `git diff --check` 通过。`SVGAElement` 修复后相关 preload 单测 **5/5 通过**。这些非 GUI 结果与下面的实际 native 验收分别记录。

最终云端验收为 **`796ecae5096e2cce38282838bb46690404d79e74`**：[run 39587144510 / Desktop job 119180398222](https://github.com/khalil-wu/minicode/actions/runs/39587144510/job/119180398222)。该 job **success**，六个 Electron smoke **6/6 通过、0 skip**；原始完整日志保存在 `output/full-parity-20261021/ci-796e-desktop-job.log`。同一 browser fixture 实际耗时 **5314.159502 ms**，20 秒截止未改变。原日志及对应断言一起确认：同页 900×600 compositor、原 POST body/child/opener、隔离 preload 的实际 modifier IPC、后台首次 GET 及原 referrer/一次 load/原活动页/无虚构 opener、HTTP DownloadItem 的精确 URL/字节/路径/owner、真实 main-frame `Page.downloadWillBegin` 消费动态 blob 许可，以及关闭来源后两类原子页仍存活。先前失败日志保留在 `ci-b9f3-desktop-job.log`、`ci-8a2c-desktop-job.log` 和 `ci-3d53-desktop-job.log`，没有把失败或中间标记算作最终通过。以上验收没有在本机启动 GUI。

### BR-G6 / BR-G10 仍缺哪些事实

**G6**：公开 `HandlerDetails.disposition` 是实际引擎给出的新页位置，可以直接消费 background 行为；它不是“网页恶意弹窗”判断。原生 `-add-new-contents` 含真实 `user_gesture`，但公开 `setWindowOpenHandler` 未投影该值，`OpenURLFromTab` 的原 `user_gesture` 也没有在公开 `HandlerDetails` 中保留。现有 stock 接口没有 Source browser content setting 的弹窗策略决定、原生 blocked-popup 分类、原请求的可再次开放句柄及完整队列。不能用 disposition、最近一次输入或脚本自报 trusted 状态制造这些事实；本轮不宣称 G6 整组已闭合。

**G10**：stock `before-mouse-event` 产生真实输入，`will-frame-navigate` 产生真实目标与 initiator frame；两者没有同一浏览器导航交易的关联 ID，后者也没有原 modifier 字段。公开窗口 handler 的 disposition 可表达常规后台页，却不能反推用户设定的任意 external-link modifier，也不能将某次主页面输入可靠绑定到未来 iframe、脚本或重定向。MiniCode 现有主文档 HTML/SVG/area 链接 preload 仍消费实际 `event.isTrusted`、anchor 和 modifiers；没有扩大到未具备这些事实的任意导航。缺的是真实 request/input 关联 producer，不能通过一个“下一次导航”窗口或覆盖 navigator 来补写。没有据此声称原生扩展不可实现。
