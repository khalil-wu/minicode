# UI-CUA1：native provider 的真实设置与授权消费者

本批交付 MiniCode 自有 `minicode-win32` provider 的永久应用授权、设置读取、确认撤销和当前窗口目录消费者。它是 UI-CUA1 的已实现分支，不代表 installed-source、remote、插件/扩展及全部平台已等价。此前调查保留在 [Source gap](2026-10-22-computer-use-settings-source-gap.md)。本批没有操作 GUI、真实桌面枚举、截图、输入，也没有重启用户实例或改用户配置。

## 原授权到真实 store

`cua_repl.py` 捕获原 receiving scope，`native_cua_runtime.py` 为本机接收端传递 MiniCode 自有 `agent_runtime_root()/computer-use-native`。`native_cua_bridge.mjs` 的原 app elicitation 现在明确提供 `session` 和 `always`；只有原调用收到真实 `accept` 与受支持的 `content.persist` 后才继续。

`always` 不进入会话 `grants` 集合。parent 固定原 target nonce 后调用内部 `grant_app_approval`；`NativeCuaSession` 重新核对该 target 的窗口和进程身份，从它保存的真实 identity 取 app ID、实际名称、exe 和 AUMID，写入 `NativeAppApprovalStore`。worker 不能自报应用身份来写权限。

store 是 `computer-use-native/app-approvals.json`，格式为 `schema_version:1` 与按真实 `app_id` 索引的 `apps`。记录含 `app_id/display_name/icon_data_url/executable/aumid`；当前独立 provider 没有图标 producer，`icon_data_url` 真实为 null，不虚构图标。写入和撤销复用既有跨进程 `file_mutation_locks` 与 `atomic_write_text`，保留其他应用记录。

新的 provider client 从同一 store 读取永久授权，原 session-only grants 仍只属于其会话；`computer_use.windows.aumids` 和 `default_app_access` 继续是独立配置策略，不混入设置页的永久授权列表。captured deny 先于 store/session grant 生效。

活 target 的 `approval_scope=always` 每次操作都读取同一 store；截图返回前再次读取。撤销、store 合法缺失或损坏均不能保留原 `approved` 布尔值而继续执行。合法缺失表示没有永久授权；损坏保留真实读取错误。实际 Win32 input 仍由原 target 检查约束，本批未执行该 OS 路径。

## 当前窗口与 API

`GET /api/settings/computer-use` 要求原 `session_id`，并核对当前 `conversation_id/workspace_root`；Home 的 conversation 可以为空。请求走当前窗口实际 backend，不能传入另一个 host ID 来选择本机 store。

响应含原 `owner`、明确 `provider_id/selection_id`、`navigation_visible/available/status/reason`、真实能力 flags、`approved_apps` 和 `controls`。永久条目仅公开 `app_id/display_name/icon_data_url`。真实读取失败是错误，不变成空列表。

`POST /api/settings/computer-use/app-approvals/remove` 携带同 owner、原 provider/selection 和 app ID。owner 或选择变化返回409；session 不存在404；不支持的消费者503。成功先修改同一 store，再返回 fresh 设置快照。它不读取或修改另一窗口选择的 runtime，也不变更工作区、草稿或附件。

`routes_computer_use.py` 通过 `backend.main` 原 API/auth 集成。前端 scoped hook、页面、SettingsCenter 目录与搜索消费上述契约；确认捕获原 scope/selection，迟到读取或撤销结果不能覆盖新 scope。空列表、读取失败和不支持能力分别呈现。没有 session 授权分组、假 Install/Manage、无消费者 Control 行或 Windows 声音行。

目录图标按当前 Desktop `26.1016.6573.0` 的 `use-visible-settings-sections-57ea2a800d85.js` 精确映射 `Ha → Ua[computer-use] → xa`，使用16×16填充指针。UTF-16范围、路径和保留参考图索引见 `output/full-parity-20261021/frontend-current-reference/computer-use-navigation-glyph.json`；未进行当前实机截图验收。

## worker 与 parent 边界

native 请求仍匹配 parent 保存的同 kernel/execution/request 记录，只使用 parent 捕获的 metadata/signal。parent extension 的 worker 方法只允许公开 computer 方法；`authorize/end_turn/lookup_app_approval/grant_app_approval` 等内部控制和 store 方法不能从 worker 直达。

新增真实 worker 测试先取得已明确 session 批准的 target，从真实 reply 观察 nonce，再伪造内部 grant/authorize/lookup 请求。它们在进入 helper 之前被拒绝，没有把 session 授权升级成 always。这里没有声称任意未知新窗口此前都能被猜中 nonce 操作。

## 本批验证

- 后端五文件一次集中 **63 passed / 0 failed / 0 errors / 0 skipped**，46.135秒：`output/full-parity-20261021/ui-cua1-backend.xml` 与 `.log`。覆盖真实 MCP always 回复、原 helper/store、第二 client 复用授权、HTTP fresh list/revoke、两个活 client 原 target 拒绝、capture 中撤销、missing/corrupt store、并发写入、Home/owner/selection、installed/remote capability 与真实 worker 私有方法升级尝试。
- 最后 receiving-owner 收口只重跑 settings 文件，**14/14通过，0 errors/skipped**，7.794秒：`ui-cua1-settings-owner-final.xml/.log`。原 conversation 的现存 thread selections/active companion 纳入选择身份；raw config 仍为 local 而实际 thread 已 remote 时不读本机 store，旧确认返回409。冷会话没有原 receiving authority 时明确 unsupported，不从 cwd/提示词推断 executor；Home 明确 local 仍可管理。两份收据合并为65个不同用例有通过证据，不冒称最新整套65/65单次运行。
- SDK/executor 收据携带完整17,277字节生产 `native_cua_windows.py`，仅替换 OS facade，再执行原 main/dispatch；production bootstrap22,599字符，受控完整源码体23,199字符，实际 argv23,266字符。真实启动、工具目录、原 turn 和 helper 关闭通过，不由较短 fixture 推断生产大小。生产源码哈希为 `afdebac062e042fa78cd98030642681067950eb7e3e89d9e3bed212be6807215`。
- 前端集中 **133 passed / 0 failed / 0 skipped**：`ui-cua1-frontend.json/.log`；glyph最后修改后 SettingsCenter 定向77/77通过，重复范围不累加为210个不同用例。最终 TypeScript、既有 UI-debt、diff 均通过，文件清单与收据见 `ui-cua1-frontend-verification.json`，并保留 `ui-cua1-typescript.log`、`ui-cua1-ui-debt.log`。覆盖作用域切换、A→B→A、过期确认/响应、原 selection、错误保留列表及不造无能力控件。
- Node syntax、Python compile、diff 检查通过。所有新 store 测试受根/后端 autouse 隔离 runtime root 约束，helper 接收同一个测试 home；没有写真实 AppData 或官方 Codex home。

## 未覆盖边界

`installed-source` 仍显式选择安装 runtime，并只使用 MiniCode 自有 `agent_runtime_root()/computer-use`。当前 Desktop 的 bool table 不足以证明任意选定 helper 采用相同格式；本批不猜写、不迁移该 store。其永久设置能力为 false，`approved_apps=null`；没有其他真实设置消费者时目录不显示空页。执行能力和永久管理能力分别报告。

显式 remote executor 的 native provider 仍仅支持 session 授权，不传本机 home 或 module root；本批设置 read/revoke 不开放远端 store。运行在另一 backend host 自己的 local 接收端则只管理该 backend 的自有 home。没有真实外机/设备/OS验收。

Any App/Office live control、插件与浏览器扩展 Install/Manage/Remove 需要原插件和扩展消费者；当前 `controls=[]`，不以 native provider 名称造插件行。macOS/Linux native adapter、高级 Windows signer/per-window hosted identity、完整图标 producer及相应物理状态仍未交付。UI-CUA1 整组继续按真实剩余分支记录，不以本地测试通过冒称全产品等价。

## 本批统一构建

生产构建和原bundle预算通过，入口 `index-6VrBV1St.js`；旧 `index-D96xHrJW.js`、`index-DGnB5Tq5.js` 及其hashed assets实际保留，未重载用户窗口。协议同步、工具重名、模块大小和agent边界检查通过。收据为 `output/native-browser-completion-20261021/frontend-cua-settings-*.log`。本批最终提交的完整CI结果另存精确HEAD原始收据，不继承旧0a6c64cb的通过。

主CI只对CEF原型路径及其独立工作流的push跳过重复生产测试；pull_request仍执行原完整验证，CEF工作流按自己的push路径实际编译和运行。没有降低测试或预算门槛。
