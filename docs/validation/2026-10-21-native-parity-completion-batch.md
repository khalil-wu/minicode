# 原生能力补全批次

基线为 `8b828000`，公开 Source 为 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`。本批不运行本机 computeruse、GUI、Playwright、CDP、截图或输入，不重启用户的 MiniCode，不改用户草稿或个人配置。云端隔离 Electron 验收另列。

## 网站设置到原权限请求

当前调用链是已有网站设置/旧权限接口 → `setSitePolicy` / `setBrowserSettings` → 持久设置 → 原 guest 的 permission callbacks。检查发现原实现只在下一次新请求或用户回答原生框后重新判断设置：已经排队的同类请求可能再次弹框，已显示请求不能在设置中阻止后立即结束；重置网站权限还会保留本页的临时授权。

现将当前设置与原页面临时授权应用于全部原 pending callbacks：

- 任一组合权限被阻止，立即拒绝该原请求并中止其原生展示；全部已获准则直接完成，不再弹第二个同类框。
- 设置变更同样作用于隐藏页面；仍需询问的请求保留原页和 callback，回到该页继续展示。
- 旧 `setBrowserSettings` 的权限编辑接入同一判定，其他 origin 的请求不借用该决定。
- 重置单项只清除相应临时类别，重置网站清除该 origin 的全部临时授权；相机/麦克风组合请求仍需满足完整决定。
- 原设置写入失败时不发布或完成权限；导航、关闭及迟到的原框回复仍只完成原请求一次。

没有新增按钮、开关、计时器、兜底或权限状态系统。Native API 仍为真实 `setPermissionCheckHandler` / `setPermissionRequestHandler`，不据此声称补齐 Source 私有通知推荐分类器。

本地一次五文件集中验证 **88 passed / 0 failed / 0 skipped**，收据 `output/native-browser-completion-20261021/browser-permission-batch.log`。这些是非GUI消费者验证，不是设备或像素验收。

## 真实下载暂停、恢复与更新重启阻断

既有云端 `browser-capture.test.js` 新增受控 HTTP 流，保留原 DownloadItem 和保存路径，使用真实 `runDownloadAction` 暂停/恢复。传入 updater 原生产 `buildUpdatePreflight` 的是 manager 的实时下载ID；暂停时必须阻断更新重启，原下载完成且字节一致后必须解除下载阻断。服务器支持真实 range 重续，没有重新创建下载记录或模拟 DownloadItem。

`caccdeff` 的云端 Desktop job已success，新增原DownloadItem暂停/恢复、精确字节及更新下载阻断/解除均经过原fixture的实际断言；未在本机执行，原20秒fixture截止与25秒case上限保持不变。整轮随后由仅修测试facade的新候选替换并主动取消，因此不能把该轮算完整CI通过；原Desktop job的实际成功范围独立保留。最新整轮CI另以最终HEAD收据为准。

## 自有 Windows CUA 接收端

独立 `minicode-win32` provider 已接入现有 CUA MCP、Node REPL、实际 app approval、原执行环境及 turn/thread 关闭链，使用MiniCode自有Win32接收端，无私有Source文件。最终107个不同相关用例有通过证据，完整生产源码体及parent捕获owner的最终相关组47/47通过；尚未做真实桌面操作，受限profile也未证明可用。选择方式、原PID回收和实际边界见 `2026-10-22-independent-windows-cua.md`。这只完成 DIST-CUA1 的 Windows 独立接收端部分，不把整组或所有平台算完成。

## Goal / Plan 的完整入口

当前Desktop consumer规定启用Goal退出Plan；启用Plan清pending goal，并实际调用原host/thread的clearThreadGoal。MiniCode三处Goal入口现共用切换路径，Plan也接原owner的保存目标清除API；原草稿、附件、引用保留，失败保留实际目标与错误，迟到A回执不改B。前端一次集中 **75/75，0 skip**，TypeScript通过。

当前普通构建已通过，入口为 `index-D96xHrJW.js`；原 `index-DGnB5Tq5.js` 及旧hashed assets保留，未重载或重启用户窗口。bundle与UI debt检查通过，原门槛未放宽。

另确认 **UI-CUA1** 设置消费者缺失，详见 `2026-10-22-computer-use-settings-source-gap.md`；当前明确未完成组为12，不能继续把11作为穷尽清单。

完整生产体SDK测试在Linux CI中还需将receiving platform作为受控OS facade的一部分；现仅给该生产代码namespace提供win32视图，宿主sys/Pillow/psutil仍保留真实平台。测试不改变生产端非Windows拒绝，也不调用真实Win32。该目标1/1通过；先前Windows SDK/Windows harness成功不被扩大为Linux原生接收端实现。

## 仍需原生引擎的范围

本批继续读取 Electron **v43.4.0** 的实际 C++：`ElectronBrowserContext::GetBackgroundSyncController()` 返回 `nullptr`，没有注册 Chrome content-settings 的完整 profile服务；当前 Desktop **26.1016.6573.0** 仍使用 `permissionHandling:"browser"`、`setPermissionPromptHandler` 和原生 `chrome://settings/content` 页面。仅添加 React 行或允许一个未知 permission 无法启动缺失的浏览器服务。

本批读取的固定版本源码和当前 ASAR 五份浏览器bundle位于 `output/native-browser-completion-20261021/`，没有执行其初始化器。BR-G1–G10 的完整原生边界仍以 `2026-10-21-browser-native-api-producer-audit.md` 为准，不把以上消费者修复扣作十项整组完成。
