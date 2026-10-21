# 代码主题目录现状纠正与完整数据验证

当前代码已经包含完整的 **29 个主题 ID、44 个明暗变体（dark 28 / light 16）**。`aba4a45c` 已在既有 15 个社区主题、23 个变体之上接入 14 个定制主题、21 个变体。因此旧报告 `2026-10-16-code-themes-mcp-media.md` 中的“定制主题未进入生产”是历史状态，不能继续据此将 `DATA-THEME1` 计为当前技术缺功能。本批没有再次复制主题或改动生产主题、字体、后端及长任务输入。

## 实际来源与当前安装版

原始主题来自已保留的安装包 `OpenAI.Codex_26.1007.9446.0`，完整静态解码数据、源资产 SHA 和 Source 注册表位于 `output/code-theme-20261016/`。原 `app-initial-b8e1c8014bb4.js` SHA256 为 `799331dde54905c4df5e360f23a9621141759e2c0acb230aeab909f9904b646f`；`dba` 注册表给出真实 ID、变体和加载名称。

本次只读核对当前安装目录：

`C:/Program Files/WindowsApps/OpenAI.Codex_26.1016.6573.0_x64__2p2nqsd0c76g0/app/resources/app.asar`

该包内部 `package.json` 的版本字段为 `26.1016.51333`。当前 `webview/assets/app-initial-2b6b8063022d.js` SHA256 为 `6cb0ab2a8d03a47f95c4df668aa8e5a3004e42614a7d596cb239442344f1a497`，其 `Tos` 注册表仍为相同的 29 ID、44 变体。逐资产核对发现 **44/44 原始主题 JS 的 SHA256 均与历史审计一致**，不是仅文件名相似。

已入仓的 21 份定制 JSON 与独立静态解码数据完整语义一致。Linear Dark、Vercel Dark 的原资产没有 `type`，本地数据按 Source 注册表补为 `dark`；除此之外没有用近似配色替代或丢弃规则。对象 key 顺序可忽略，TextMate 数组和 scope 规则顺序保留。

当前注册表原文及审计收据：

- `output/full-parity-20261021/code-theme-current-registry.raw.txt`
- `output/full-parity-20261021/code-theme-current-reference.json`
- `output/full-parity-20261021/generate-code-theme-current-reference.cjs`

## 许可和 notice 的准确边界

23 个社区变体直接使用 `@shikijs/themes@3.23.0`。该包 `package.json` 明确标注 `license: "MIT"`，完整版权声明已保留为 `frontend/src.v2/lib/code-themes-LICENSE.txt`。此前对 3.23.0 的逐份完整语义比较仍有效，本次再次验证全部加载数据。

21 个定制资产没有被声明为 Shiki MIT 主题。现有 `codex-themes/PROVENANCE.md` 与运行时来源元数据明确记录其 Desktop 参考来源。当前 Desktop 的 `package.json` 没有 `license` 字段，定制主题对象也没有独立的 `license`、`author` 或 `origin` 字段。

本次沿真实许可证页核对：`open-source-licenses-page-2ec31dde25fc.js` → `appInfo.getThirdPartyNotices` → main 的 `pue`，读取安装目录 `resources/THIRD_PARTY_NOTICES.txt`。该文件为 2,579 bytes，内容包括 node_repl 使用 Meriyah 的 ISC notice、rust-analyzer 的 MIT notice，以及 Copyright (c) 2013 Cal Henderson 的 MIT 声明；第三段未标注具体包名。它没有关联这 21 份定制主题或单独赋予它们 MIT 许可。

`.tmp/codex-src/LICENSE` 是 Apache-2.0，但当前公开 tree 未包含这些定制主题资产，不能仅据该仓库的根许可给安装包资产赋予 Apache 许可。在实际检查的包元数据、主题对象与 notice 中，没有找到对 21 份定制资产单独授予 MIT/Apache 许可的声明，也没有找到明确禁止本项目再分发这些主题数据的具体条款。本报告精确保留这一来源和 notice 状态；不据“私有”字样增加许可审批，不将其继续计算成缺少可执行主题目录。

## 独立 Source 期望与集中验证

本批只修改 `code-themes-reference-seeds.json` 与 `code-themes.test.ts`。期望从保留的 Source `dba` 注册表和独立静态解码的主题 JSON 生成，不调用 MiniCode 的主题实现：

1. 静态解析原注册表，取得每个 ID、变体和名称，不运行主题 import/load 回调。
2. 使用已审阅的原始纯函数按 Source `Mya` → `Nya` 顺序推导颜色与主题自身 chrome override；按既有用户约定剔除 fonts，不改变生产字体。
3. 对完整主题对象作 SHA256 比较，覆盖全部 colors、tokenColors、settings、semanticTokenColors、chromeTheme 和其他元数据；对象 key 作确定性字典序排列，数组和规则顺序完全保留。
4. 44 份独立 seed、规则数量、语义规则数量、来源元数据和完整数据摘要全部进入测试。原定制分支仅断言规则非空的浅检查已移除。

一次集中运行 `code-themes.test.ts`：**45 passed、0 failed、0 skipped**。包括一项目录/fixture 完整覆盖检查及全部 44 变体的实际 lazy load 与完整数据验证。收据为 `output/full-parity-20261021/code-theme-current-catalog-tests.json` 和同名 `.log`。本批未运行全局 TSC、构建或 CI，交由主 agent 统一执行；也未进行 GUI 或像素验收，不把数据与 seed 一致扩大为所有桌面视觉状态一致。
