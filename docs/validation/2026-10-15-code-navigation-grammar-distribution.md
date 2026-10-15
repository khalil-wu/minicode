# 代码导航语法包分发

此前原生工具统一批有 19 个节点因缺少 JavaScript、TypeScript、TSX、Go、Rust、Java grammar 跳过。实际 `pyproject.toml` 和 sidecar lock 只包含核心 tree-sitter、Bash 与 PowerShell grammar；桌面运行包不会获得代码导航已经实现的这些语法能力。此前的 parser 代码修复不能代替缺失的运行资源。

本批将五个真实 Windows wheel 依赖加入普通安装和 sidecar lock，并安装到项目自己的 `desktop/python-runtime`，未升级现有 tree-sitter 或更改全局 Python：

| 依赖 | 固定版本 |
|---|---|
| tree-sitter-javascript | 0.25.0 |
| tree-sitter-typescript（含 TSX） | 0.23.2 |
| tree-sitter-go | 0.25.0 |
| tree-sitter-rust | 0.24.2 |
| tree-sitter-java | 0.23.5 |

没有新增语法解析器、正则替代、异常吞掉或降级层。实际 `get_parser` 在 bundled Python 中为六种语言返回原生 grammar；导入路径、版本和 grammar ABI 见 [分发证据](../../output/ast-grammar-runtime-20261015-evidence.json)。TSX 使用自己的 grammar，非普通 TypeScript parser。

全部依赖补齐后，以 **bundled Python** 统一运行既有 `backend/tests/test_optional_tool_chain_audit.py`，结果 **37 passed / 0 failed / 0 errors / 0 skipped**：[JUnit](../../output/ast-grammar-runtime-20261015-batch01.xml)。覆盖定义与使用区分、解构声明、TSX、同一行引用、成功零匹配不得退回正则，以及真实 ToolRegistry 和工作区权限 consumer。没有新增或削弱测试，没有把先前 19 项跳过当作通过。该 interpreter 未安装 pytest-asyncio，配置产生已保留的 unknown asyncio option warning；本文件使用同步测试与 `asyncio.run`，37 个节点实际全部执行。

这是代码导航资源分发验证，不代表完整桌面安装包已重新构建，也不代表 Source Starlark `.rules` / execpolicy、增强 Node runner 或远程环境已实现。先前原生工具批仍保留其冻结时的 363 passed / 19 skipped 记录，两个重叠范围不直接累加。
