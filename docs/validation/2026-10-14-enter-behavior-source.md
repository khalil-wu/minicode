# Source Enter 设置、实际发送与跟进链

本批补设置中缺失的第三种 Enter 行为，视觉沿已通过的共享 SelectMenu；字体、字号和字重未改。基于实际已提取桌面 `general-settings-161845fad443.js`、`app-primary-19fe57fd8c00.js`、`app-initial-b8e1c8014bb4.js`，解析片段保存在 `output/playwright/codex-control-audit-20261013/source/enter-behavior-source-functions.json`，仅读取源码数据，没有执行参考应用代码。

## 实际依据与实现

- Source `Ec/Oc` 与 `src::Dn` 按 `enter → cmdIfMultiline → cmdAlways` 提供三种选择。`sKr` 的 Windows/Linux modifierSymbol 是 `^`，macOS 是 `⌘`；中文标签来自实际桌面语言资源。General 用完整标签与 Source 顺序，保留既有 `mod-enter` 的持久化 ID 代表 `cmdAlways`，新增 `cmdIfMultiline`，不改用户已有偏好。读写和冷恢复使用相同合法值。
- `cBc` 的 `Enter` keymap 在 `cmdAlways` 下换行，在 `cmdIfMultiline` 且 `eUr(doc)` 为 true 时换行，其他情况 submit。`eUr` 只根据多个文档块或真实 hard_break 判断多行，布局软换行不算；MiniCode textarea 对应真实 `\n`，不 trim 文本后再判断。Shift/Alt+Enter不触发发送；IME事件与229不发送。
- Source `nut/Ylt` 的运行中相反跟进快捷键：`enter` 用 CmdOrCtrl+Enter，另两模式用 CmdOrCtrl+Shift+Enter；根据真实当前 queue/steer 取相反提交方式。MiniCode main 与 side 分别传自己的真实运行状态，临时提交不改变全局偏好。普通、附件及 runtime slash（含 `/compact`）继续沿原用户准入链；wire 使用真实 `streaming_behavior=steer/follow_up`，不增加不存在的协议字段。
- 首次send时即捕获本次跟进方式，后续异步构造上下文不会借用后来改变的全局偏好。main/side共用实际ComposerTextarea；快捷键帮助与side现有tooltip同时消费第三种模式。

## 统一验证

8个相关文件首批212节点为208 passed / 4 failed。4项新fixture误把实现参数 `busyBehavior` 写为wire `busy_behavior`；真实协议一直为 `streaming_behavior`。保留首轮完整JSON，纠正fixture并只复跑受影响admission文件为49/49。按实际文件/fullName/同名参数ordinal取最新值，最终 **212 passed / 0 failed / 0 skipped**，见 `enter-behavior-latest-results.json`；不将212与49相加。

新增节点覆盖真实设置→持久化读、硬换行/尾部换行/软换行、IME/Alt/Shift/重复modifier、实际main/side发送→WS outbox→semantic准入→原草稿清理，检查主会话未发送内容、side归属与全局偏好不被改变。旧的草稿/附件准入、失败恢复和菜单文件一起检查，未用实现镜像测试代替既有用户链。

TypeScript编译与production Vite构建成功，日志为 `enter-behavior-tsc01.log` 和 `enter-behavior-production-build01.log`。本批最初验收bundle为 `index-Djp2grfF.js`；index SHA256 `664cfb528b2ff66f56672950876051e3905f8a4b71d26ea3af7bb94d1962eda3`。独立Electron真实模型HTTP/WS与cold验收正在运行，不能把unit结果当作该验收通过。

## 实际边界

MiniCode现有输入区是textarea；Codex rich editor的列表、代码块与expanded composer专属keymap仍需对应编辑器实现，不在本批冒称通过。闲置状态的Source submitInBackground还涉及新任务/页面切换等owner链，本批没有将普通Ctrl+Enter提交冒称为该后台任务行为。完整语言与WSL agent环境仍按各自真实producer另行实现。
