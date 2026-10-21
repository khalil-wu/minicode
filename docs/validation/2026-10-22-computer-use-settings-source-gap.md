# UI-CUA1：Computer use 设置的真实消费者缺口

当前参考为 Desktop `26.1016.6573.0`，仅静态读取 ASAR 和保留的 Codex 操作记录。MiniCode 当前没有该设置目录及完整 provider-aware 管理后端；这是**已确认的功能缺失**，不再仅列为“未做物理验收”。此前11组是已确认清单，并非全产品穷尽；本组使当前确认的未完成组增至12。

## 当前 Source 实际行为

- 目录可见性由真实 computer/external-browser availability 或明确 policy-denied状态决定，不因为一个模块存在而无条件显示。
- Any App、Office live control和浏览器插件行取决于真实能力与插件记录，Install/Manage/enable调用原插件/扩展服务，没有无条件安装按钮。
- 永久授权读取为 `computerUseSettings.getAppApprovals()`，返回 `{approvedApps:[{bundleIdentifier,displayName,iconDataURL}],approvedBundleIdentifiers}`；确认撤销后 `removeAppApproval(bundleIdentifier)` 返回的新列表才替换缓存。
- 当前 Windows main 读取选定 CodexHome/config.toml 的 `[computer_use.windows.always_allowed_app_ids]` bool表；旧 `[apps].allowed`迁移是其特定版本合同，不能凭UI标签推断任意安装helper都使用该格式。
- Windows 的 `getSoundMode()` 返回null，设置页不呈现声音行。声音选项及锁定操作等macOS条件不能伪造为Windows缺少的控件。
- Remove extension打开该浏览器真实扩展管理页，不直接卸载；真实安装URL、extension ID、加载与检测错误分别决定动作。

## MiniCode 两个 provider 的实际存储

`installed-source` 显式选择用户安装的runtime版本。其 CODEX_HOME/codexHome 为 MiniCode 自己的 `agent_runtime_root()/computer-use`，不是用户全局 Codex home。设置只能管理该选定helper的真实store，不能读写另一个产品的全局授权冒充接通。

`minicode-win32` 捕获 MiniCode effective config 的 `computer_use.windows.aumids`（allow/deny）与 default_app_access；新增授权目前仅为真实session grants。这既不是当前Desktop的bool表，也不是永久always授权服务。普通配置编辑器不能代替真实应用身份/图标、授权清单和撤销消费者。

当前没有可直接连接前端的完整统一契约，因此本批不加空页面、假Install/Manage、永久授权按钮或Windows声音设置。后续需先完成provider能力、真实授权store与应用元数据、读取/确认撤销、插件/扩展管理的原host归属，再接设置页面。

完整静态范围、符号与精确片段索引：`output/full-parity-20261021/frontend-current-static-audit.md` 及 `frontend-current-reference/`。同批已修复的Goal/Plan模式互斥是独立已交付链路，75/75通过，不能抵消本组缺失或代表所有菜单已实机核完。
