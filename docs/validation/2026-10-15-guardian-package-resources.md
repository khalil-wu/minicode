# Guardian 与模型准入资源的实际安装包验证

本批修复真实分发清单遗漏：`pyproject.toml` 原来没有声明独立子包 `backend.agent.guardian` 的 `resources/*`。源码目录直接导入成功不能证明 wheel 含有运行时策略和分类提示词。现声明该子包完整资源，并将 `backend.llm/SOURCE_MODEL_METADATA_NOTICE.md` 随已经声明的 JSON 资源一起分发；不改运行时错误处理或添加资源缺失兜底。

验证脚本为 `output/verify-guardian-package-20261015.py`，结果保存在 `output/guardian-package-resources-20261015-batch01/evidence.json`。它将当前实际后端与 pyproject 冻结到独立临时目录，用 `pip wheel --no-deps --no-build-isolation --no-index` 真正构建 wheel。没有安装依赖、访问外部服务或修改普通用户 profile。

实际构建 **exit 0**。随后从该 wheel 解包到另一个独立目录，以包内路径执行新的 Python 进程，实际导入 Guardian 分类配置、同步策略/模板、模型 step activation 的 Node policy，以及已封装的 Source 模型元数据。检查 `backend.__file__` 确实来自解包目录，不能借工作区源码满足导入。实际导入 **exit 0**，11 个 Source slug 的 host provenance commit 均为 `822e58cc3d666166c7446c5b1ea2e52f5d09594c`，未知模型不获该证明。

wheel 中逐项核对了 `policy.md`、`policy-template.md`、`async-classifier-instructions.md`、`node-repl-policy.md`、Guardian `NOTICE.md` 与 Apache `LICENSE`，以及 `source-model-admission-metadata.json` 和其 Notice。八项均包含，实际包内内容 SHA256、wheel SHA256、冻结源码 SHA256 见 evidence；冻结输入与记录一致。构建与导入原始输出分别为同目录 `build.log`、`import.log`。

这项只证明本批资源分发与包内导入，不代替行为测试、完整桌面安装验收或所有平台打包。官方账号按用户要求不做，不计待办或阻塞。
