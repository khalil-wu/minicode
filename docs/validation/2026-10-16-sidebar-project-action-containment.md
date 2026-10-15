# 项目按钮的真实侧栏宽度修复

在完整 CI 绿灯检查点 2c3cae04 之后，普通客户端启动检查发现：动作按钮虽然在 DOM 中且 opacity=1，仍被挤到侧栏外。真实 Electron 的侧栏可用宽度为 220px，项目列表的隐式 grid 最小轨道却被长聊天标题撑到 382.475px；项目菜单按钮 x=395.275，已在侧栏右边界 290.8 之外。此前项目动作的隔离功能验收没有覆盖这个普通 profile 的内容组合，不能拿 DOM 存在代替可见。

修复实际尺寸传递链：会话滚动容器使用 `minmax(0, 1fr)` 单列，排序列表允许 `min-width: 0`。项目、项目内会话、最近与固定列表继续复用同一排序组件；没有增添控件，也没有改字体、字号、字重、hover 或菜单动作。

## 验证

- 既有项目动作、项目排序及固定排序三个文件统一 **36/36** 通过。没有新增检查 CSS 字符串的镜像测试。
- TSC/Vite 候选构建、bundle budget 与 UI debt 通过。候选构建先放独立 `.tmp/sidebar-grid-latest-20261016`，检查后推广到 `frontend/dist`，旧构建仅移到 `.tmp/dist-before-sidebar-grid-20261016` 保留，没有执行用户的手动删除脚本。
- 普通 Electron 原始 entry 为 `index-BF3-Sk4t.js`，修后真实页面为 **`index-DJ0cGCSj.js`**。两个项目行均恢复为 220px，两个 28px 按钮分别位于 232.8–260.8、262.8–290.8，均在侧栏内。
- 初次 QA 将浮点边界作精确比较，在 0.0000152587891 CSS px 的舍入差处报错；原始 geometry 保留，随后按实际渲染像素边界比较。没有为此修改产品尺寸或把 162px 的原始溢出当作容差。
- 原有 13 份 draft storage 逐值未变，项目标签 computed font family/size/weight 相同。用户在此期间自行发送的真实消息不是 QA 数据；没有保存或清空用户编辑器、替换聊天记录或取消任务。重载前记录没有 dirty editor 或待持久化草稿。
- 客户端 PID15252 随后由正常窗口退出链关闭，原后端 PID29320 在 00:29:22 完整 lifespan shutdown、exit 0。此后没有为补图重新操作普通窗口。

证据目录：`output/playwright/heartbeat-memory-latest-client-20261016/`，含原始 settled 截图、CLI snapshot、before/after 私有本地保全记录、geometry result、启动与关闭日志。最终功能范围是普通 profile 的几何与状态保全；不把用户的模型请求计为自动化场景，也不据此扩张成所有按钮/所有窗口宽度的验收。
