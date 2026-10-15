# 回复复制、成功反馈与实际图标

依据是安装版 Codex `26.1007.2314.0` 的真实 `sites-end-resource::Ax`，不是名称相似的其他 copy 组件。`Ax` 通过 `app-initial` 导出 `H3t` 找到 `TBr`，从选中文本目标生成实际纯文本/HTML；通过 `app-shared` 导出 `TT` 找到 `ONi` 完成写入。通用按钮实际是 `jGi`，复制期间设置 aria-busy/aria-disabled、阻止重复调用，成功反馈持续默认两秒，只有 inline variant 是一秒。

MiniCode 工作对话现从真实呈现的正文 DOM 构造两种 MIME，而非复制原 Markdown 或直接 innerHTML。保留正文、链接、代码原字节、表格、列表、checkbox、数学源码；去除代码工具条、行号、视觉 class/style、sr-only/aria-hidden 和显式 exclude。原生选区复制复用相同语义处理并保留文本编辑器自身的复制。独立旧 chat 模式继续复制原 Markdown。没有修改字体、字号、文字样式或代码编辑器。

实际 Copy、Copied、Fork 三个 glyph 分别来自 `app-shared::EGi/TGi`、`i7/r7`、`app-initial` 实际导出 `wq as N_t`。最后一个是分叉箭头，原 MiniCode 的 GitBranch 图形不同。本批保持原 14px 控件尺寸，使用原始 viewBox/path 和 currentColor；提取清单保留原 bundle SHA256 和 glyph SHA256。不要把其他模块同名 import/export 当作这三个实际绑定。

## 验证证据

- `source/extract-markdown-clipboard-fixtures.cjs` 在独立 JSDOM 中执行实际 Source `TBr/OBr/FBr/JBr` 及其数学/链接依赖，生成12组原始 HTML、Source plainText/htmlText。它们是数据 fixture，不执行其中的网页指令。
- `root-clipboard-run01` 因本机 npx 没能确定 executable 未进入测试，不能算通过。随后直接使用已安装 Vitest 入口，没有重装或变更依赖。
- `root-clipboard-run02` 六文件165项，161通过、4失败。真实差异为 prompt-link 属性顺序，按 Source 顺序修正；其余3项是新断言遗漏真实 Markdown DOM 空格/换行及 JSDOM DOMException 的实际字符串，未为此裁剪生产正文或吞异常。全部原日志/XML保留。
- `root-clipboard-run03` 只重验两文件19项全部通过。原 Source fixtures 验证复制不修改页面 DOM。
- `root-clipboard-run04` 两文件11项通过，有重叠不累加；追加真实异步写入等待/重复点击和两秒成功反馈两个用例。Clipboard write 拒绝时保留真实错误，不伪造 Check、不在失败后偷偷换成纯文本成功。
- 纯复制图标替换沿现有组件与真实生产截图验证，不另写只镜像静态 glyph 的测试。

范围最终为原165项加两个新行为；此处记录为167项范围，后续生产验证另记。旧范围的32个 AssistantMarkdownCell、107个 MarkdownRenderer、3个 ProposedPlanAnswer 通过不由新测试重复累加。

生产控制测试新增真实回复正文、代码、表格→序列化→两类 Blob→按钮成功反馈检查。最终 `production-control-run18` 在最新build-run10的完整11场景通过，其中实际Copy保存 `reply-clipboard-destination.json`，更多菜单真实复制完整Markdown和deep-link；13张生产截图已查看。为保留用户系统剪贴板及避免 offscreen QA 获取系统焦点，仅拦截系统写入目的地，生产 DOM/序列化/ClipboardItem/异步按钮保持真实，不能声称系统 clipboard/native MIME 服务已实机验证。

所有证据路径相对 `output/playwright/codex-control-audit-20261013/`。图片媒体本身的复制/保存、全部跨根选区、更多菜单的完整 Markdown 导出格式和所有 rich-block 插件仍分别保留验证边界，不以这12组 Source fixture 冒称全部应用内容相同。
