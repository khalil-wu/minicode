# 浏览器设置的真实共享状态

本批是设置选择对齐的一部分。视觉依据为用户提供的 Codex 常规设置截图 `codex-clipboard-ff50df00-fe70-4eee-90b1-fb65584abbce.png` 和已保存桌面参考；行为先检查 MiniCode 现有真实设置 producer、宿主执行和各窗口 consumer。没有凭截图增加新权限或下载策略。

## 原因与完整修复

`desktop/embedded-browser-manager.js::setBrowserSettings` 原本只写入文件并向发起方返回。下载执行的 `will-download` 已消费新全局值，但 `BrowserIntegrationTab` 与 `BrowserPanel` 各自保存旧值，其他窗口的已打开设置面板不会收到变更。清除站点数据或全部浏览数据同样会删除宿主权限，却留下其他面板的旧勾选。

现在同一 `saveBrowserSettings` 在真实文件原子提交后发布完整的 `downloadPolicy/sitePermissions`，通过当前注册的主窗口与实际 preload 的 `onSettingsChanged` 订阅传到前端。设置页只消费全局下载策略；站点面板按自己当前页面的 HTTP/HTTPS origin 投影权限。清除单站点和全部数据沿用相同保存边界，不靠轮询或重新画标签掩盖执行状态。

已有请求版本继续负责异步顺序：真实新设置变更会使早先读取及迟到写入 ACK 失效；本窗口失败的保存不会回滚其他窗口之后提交的新值。真实保存错误仍展示。没有改下载模式、权限枚举、conversation/page owner、未提交地址、批注或草稿。

## 当前验证

- `browser-settings-sync-node01.log`：`browser-lifecycle-audit`、`embedded-browser-manager`、`embedded-browser-bridge`、`security` 四文件统一 **54 passed / 0 failed / 0 skipped**。新增两个用例走生产 manager 的真实文件提交与实际 preload 订阅，覆盖两个窗口同步、单 origin 清除、全部清除、取消订阅和写盘失败不发布。
- 新增前端用例覆盖设置页迟到读/写及写盘失败后的真实共享值，站点面板跨窗口变更、origin 隔离、清空权限和旧 ACK；已纳入本批统一312项通过的范围，不和前面的54项累加。
- 最新 `general-select-after06` 六场景、10张图、真实 exit0 / errors=[]：通过真实菜单第二主窗口、下载选择 live 同步、公开 Example Domain 原生 tab、跨窗 origin 权限 grant/clear、两份未发送草稿和冷进程恢复。真实 private-navigation 审批等待的旧 after05 保留失败，没有伪造批准或计通过；详细与最新构建补图见 `2026-10-14-settings-select-source.md`。

此报告不代表全部 Codex 浏览器服务或所有桌面控件已等价。通知、WSL agent environment 等分别按实际 Source 调用链审计与实现。
