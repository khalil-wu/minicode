# CI Source 契约修复续批

当前仍在处理 GitHub CI，不能宣布整款产品或 CI 已完成。用户指定的 heartbeat、可配置 remote executor 和 Memory Phase 1 可选 WebSocket 在 CI 全通过后继续；官方账号及依赖服务仍排除。本轮未改变前端字体，也没有新建前端控件。

## 生产调用链

- REST `/api/chat` 原先直接序列化所有内部工具记录，遗漏 `private_model_only`。现在在实际 consumer 的 `from_internal` 前过滤整条私有记录，同时在 REST 字段漂移守卫中明确登记该内部标记。真实 FastAPI 请求回归核 public 保留、private 整条不泄漏、原记录不变；没有只修改测试期望。
- LSP 原先发送 `shutdown`/`exit` 后立即终止子进程，Linux Pyright 因而返回 SIGTERM。现给协议对端 2 秒正常退出窗口，之后继续由既有 runner 回收。真实 Pyright、稍后正常 exit 0 的 OS stdio peer、忽略 exit 的真实 OS peer 均验证，不把任意退出码当成功。
- 未知模型的 fallback 模板重复嵌入了 Windows checkout 的 CRLF，而 Linux 包内 `prompt.md` 为 LF。Source `model_info_from_slug` 使用同一个 `include_str` prompt；MiniCode 现在直接从原 `codex_model_instructions()` 资产构造 fallback，删除 JSON 的重复模板。已知模型与其他 catalog 字段未变；原严格字节与 native prefix 断言未做换行替换。
- 已冻结的 native command/MCP 审批仍归属原 issuing step，切换 session 默认权限不会自动批准或改写它们。MCP resolver 拒绝 generic `auto_approved`/`session_approved`，正常显式回应仍走原 owner/digest。真实 ToolBatchRunner→pending→切 mode→错误 owner/digest→显式回应链已通过；旧实现的实际自动批准负例保留。详见 `2026-10-15-source-frozen-approval-owner.md`。

## 验证证据

各行有交集，不相加为全产品通过数。初始失败与必要窄补验均保留；跳过单列。

| 范围 | 当前结果 | 原始证据 |
| --- | --- | --- |
| WS/workspace/REST 与 LSP | 143 个不同节点均有最终通过记录 | `output/ci-repair-20261015/ws-lsp-delivery-20261015/manifest.json`；宽组先 141/143，后续按受影响范围补齐，未声称首轮整批绿色 |
| Source 能力、MCP、多 agent、模板 | 373 passed / 1 既有 Windows symlink skip | `source-capability-batch02-summary.json`；374 different cases，原 8 failures/2 teardown errors 已补齐；不将重复 teardown 节点额外计数 |
| Root conversation/attachment/WS | 117 个不同节点均有最终通过记录 | `root-conversation-upload-ws-delivery-20261015/manifest.json`；宽组先 115/117，最后附件整文件 6/6 |
| Root SDK、Responses 与 prompt | 144/144，0 skip | `source-root-batch03-delivery.json`；MCP SDK 真实请求、原 Unicode、native_name 与协作模式 developer 层保留 |
| 冻结审批、权限、hook 与 ownership | 244/244，0 skip；另 Source Auto mode 4/4 | `backend-root-owned-source-final.xml`、`source-frozen-approval-mode-after.xml`；旧实现同 4 例在实际 mode 自动批准处失败 |
| Root provider/projection | 168 个不同节点均有最终通过记录 | `root-provider-projection-batch.xml`、必要窄补验及 `root-command-protocol-owned.xml`；主请求绑定用例隔离无关 memory producer，并实际排空旧 restore capability 工作再测 model 快路径 |
| 合并后的整份 Runtime architecture | 91 passed / 2 既有 Windows Temp ACL skip，0 failure/error | `root-runtime-merged-final.xml`；同文件两个 owner 合并后统一复验，没有把 skip 记通过 |
| Root 旧 context/agent loop 的受影响 4 例 | 4/4 | `root-native-legacy-final.xml`；该 XML 另 3 个 Runtime 节点已由上一行合并验收覆盖 |
| GitPanel 与 GitDelivery | 17/17 | `git-panel-delivery-isolation.log`；父组件测试隔离单独受测的子组件，避免未控制 HTTP 请求造成时序漂移 |

以上路径如未带前缀，均位于 `output/ci-repair-20261015/`。新生产模块经实际 bundled Python 3.11 新进程导入，未知 descriptor 与原 prompt 资产严格相同。Python compileall、kernel、protocol、duplicate-tool、module-size 与 staged whitespace gates 通过，未提高预算。

## 全量边界与 GitHub

GitHub `2146695c` 已验证前端、Electron 集成、Windows harness 与 Linux native build 成功；随后 `7c4672c9` 的 Desktop unit/smoke 实际通过，前端新暴露 1/4234 的测试隔离遗漏，Linux backend 为 6363 passed / 116 failed / 27 skipped。该状态早于本文件这些已冻结修复，不能把 116 当作当前产品缺口数；最终仍以修复后 HEAD 的实际 GitHub 结果为准。

旧 bundled 3.11 全量 snapshot 收集 8311 项后一直执行原已加载的旧 fixture。它在取消测试无限等待，确认 faulthandler 后明确中止；7551 个已完成 outcome 为 7292 passed / 203 failed / 53 skipped / 3 errors，见 `backend-root-python311-full-interrupted.json`。未生成完成的全量 XML，不算通过。Root 后面的 40 文件/675 项此前未执行，已单独按精确文件数组运行一次以继续找真实失败；不重新启动旧 8311 snapshot。

先前 wheel `5fa1d189…` 与冻结输入仍是历史交付，本轮源码已改变，不能据此宣称本轮最终安装包已重建。普通用户窗口、未保存编辑、草稿、附件与工作树未被关闭、覆盖或清空；新测试均使用隔离环境。旧构建/QA 删除被自动审批审查以 `blocked by policy` 拒绝的路径仍留在用户手动脚本，未换工具绕过。
