# 新会话、项目操作与 CI 修复

当前状态：本地分区验证和真实桌面检查已完成第一批；GitHub 整体 CI、8311 项后端/root 完整运行仍在复验，不能据此宣布整个产品完成。用户要求先把 CI 修好，再继续 `2026-10-15-source-internal-evidence-delivery.md` 的剩余工作，后续按该顺序推进。官方账号功能仍排除，字体保留现状。

## 用户报告的问题

桌面 bundled Python 为 3.11.9。`create_projectless_directory` 却调用了 Python 3.12 才提供的 `Path.is_junction()`，实际 `conversation.create` 抛出 AttributeError；此前仅用健康检查和开发 Python 验证没有覆盖这个入口。

现在读取真实 `lstat`，以目录类型和 Windows mount-point reparse tag 识别实际目录，不使用缺失方法、不把链接误判为普通目录，也没有添加吞异常或超时隐藏加载状态的路径。Windows CI 明确加入完整 `test_projectless_execution_cwd_chain.py`，避免再次漏掉正式解释器的新会话入口。

项目行原来确实缺少可点击的 `…`，SquarePen 则只在 hover/focus 显示。现在两者作为同一组动作，`…` 接回现有 WorkspaceContextMenu，菜单打开后保持动作；Escape 恢复按钮焦点，不折叠项目。SquarePen 使用实际工作区根创建对话。菜单依旧保留已有 history-preserving 移除语义，没有在验证中执行移除。

旧 ToolCallCard 详情 renderer 仍残留失败/部分完成徽标、合成失败提示与红字。该路径已清理，实际原记录和诊断保留，耗时使用现有整数秒格式。没有为了让过期测试通过而恢复用户已拒绝的标记。

## 本地与桌面证据

- 前端全量：352 文件、4234/4234，通过且无跳过、无 unhandled。首轮 37 个旧失败与两项退出错误保留，按真实 Source 契约修正；见 `output/ci-repair-20261015/frontend-full-results.json`。
- TypeScript、Vite 候选构建、bundle budget 与 UI debt 通过。候选位于 `frontend/.tmp/ci-repair-latest`，没有覆盖用户正使用的 `frontend/dist`。
- 候选 entry `assets/index-srAOTZkG.js`，index SHA256 `37e36bddeaae4eb425c007e2357b8115e71dc3af6604987c4a70f416648b1f04`；完整身份见 `frontend-stage-identity.json`。
- 原生 Electron 项目动作两项实际功能通过，8 张最终图由分区 agent 与 Root 分别逐张查看；真实 bundled 3.11.9、原生 preload/WS/持久 manifest，0 模型 POST、0 工作区移除、用户普通窗口未操作。原项目与项目外草稿均可见地恢复。证据：`output/playwright/sidebar-actions-20261015/`。
- 新会话原生检查使用实际 3.11、真实 UI 新建与 WS 权威回执，包含连续创建和目录落盘。原始第二张图只抓到了切换中间帧；没有把缓存 inputValue 当作可见输入框。随后复用同一原 profile 冷启动与切换，实际等待输入框可见、检查草稿并逐张看图，未新增模型请求或重新创建聊天。证据：`output/playwright/new-chat-python311-20261015` 与 `-settled`。
- 后端 projectless 初批 25 项通过，唯一旧 direct command fixture 改为真实 native authorize/bind/execute 后，在后端窄补验通过；真实新建 WS handler 和两个 REST 入口另在 3.11 下通过。
- 四个 native producer/journal 文件的平台化检查在 Windows 3.11 为 41/41。真实 I/O 用宿主命令；POSIX 使用实际 Python 可执行文件与精确脚本前缀，保留全部审批、digest、落盘、冷读取、输出与执行时钟断言，不跳过 Linux 行为。静态 Source 向量不改。

前端、native、projectless 和后端完整批次有重叠，不能相加成全产品通过数。

## GitHub CI

原 run `38715302345` 六项均失败。Ubuntu 两项被 kernel 体积门槛挡住，尚未执行完整测试；Windows 与前端则揭露了旧 fixture 和真实生产遗漏。生产取消/进程回收修复与准确原始证据见 `2026-10-15-ci-backend-contract-repairs.md`。

CI 增加实际 Linux native execpolicy 构建：固定 Source 的 Rust 1.95.0、锁定依赖、校验原文件哈希、执行真实 identity，artifact 交给两个 Ubuntu consumer 后恢复可执行权限。没有替换成 mock 或让缺少引擎的测试跳过。

Source manifest 原先记录 Windows 参考 checkout 的 CRLF 字节，而 Git 默认 LF 检出会让 35 个原输入指纹全错。现仅给 vendored 输入固定检出行尾，35 个文件经 `git cat-file --filters` 全部匹配原 SHA；没有修改 Source 算法、删除 hash 校验或重写为随意的期望值。验证记录为 `native-source-checkout-bytes.json`。

kernel/protocol/duplicate-tools/module-size、3.11 compileall 与 diff check 均通过，预算未提高。backend failure 展示步骤绑定实际 test step 的失败，不再在前置 gate 失败后读取根本没有产生的测试日志。

最后两个未验的测试改动暂不纳入第一批提交：`frontend/tests/e2e/electron-multi-agent.spec.ts` 与 `backend/tests/test_codex_context_window_ws.py`。它们在各自隔离环境继续修正并验证；第一批推送后以实际 GitHub 结果继续收敛，不宣称全绿。

## 手动清理

用户要求自行删除剩余旧测试产物。已提供本地脚本 `output/cleanup-old-test-fixtures-manual-20261015.ps1`，限定 16 个旧测试路径，执行前核对绝对路径仍位于旧 output 子树、拒绝链接。执行命令：

```powershell
& 'C:\Desktop\MiniCode\output\cleanup-old-test-fixtures-manual-20261015.ps1'
```

本轮没有代替用户执行这个手动脚本。
